"""
TradeOS v6 — MSL Computation Engine v2.0
==========================================
Pipeline position: [10.5] — after compute_indicators, before generate_signals.

DESIGN PHILOSOPHY — Swing Trading 1-4 Weeks
============================================
Every number this script produces is calibrated for one purpose:
find stocks that will move meaningfully over 1-4 weeks, rank them
so the BEST opportunity is always at the top — including ranking
NEW candidates above existing holdings when the evidence demands it.
composite_score = screener aggregate, final_score = intelligence-weighted output.

KEY PRINCIPLES:

1. REGIME-AWARE THRESHOLDS
   RSI 85 in a TRENDING market with ADX 40 is a continuation signal.
   RSI 85 in a NEUTRAL market with ADX 15 is an exhaustion signal.
   Every oscillator threshold in this script adjusts dynamically based
   on the current market regime and trend strength. Static thresholds
   are the #1 source of false signals in the Sheet formula approach.

2. ALL 70+ FIELDS ARE EVALUATED
   Every field in stock_data_daily has an explicit use or explicit
   exclusion with a reason. Nothing is ignored by default.
   New composite signals are derived from underused fields:
   - MA alignment (SMA + EMA stack)
   - Heikin Ashi candle quality
   - Bollinger Band squeeze + position
   - MACD momentum direction + acceleration
   - PSAR + supertrend dual confirmation
   - Weekly structure (higher highs + higher lows)
   - Multi-timeframe VWAP alignment
   - Volume trend (20d average vs 50d average)
   - Fundamental quality filter (earnings direction)

3. HOLDING SCORE vs ENTRY SCORE
   Existing positions need a DIFFERENT score from new candidates.
   A position you've held for 2 weeks with +12% P&L and intact trend
   should NOT be replaced by a slightly higher-scoring new setup —
   there's friction, tax, and execution cost. The holding_score
   gives existing positions appropriate stickiness unless a new
   candidate is meaningfully better AND the existing trend shows
   first signs of weakening.

4. QUALITY OVER QUANTITY
   final_score is designed to produce MEANINGFUL separation between
   stocks. A 73 should genuinely be better than a 58. The scoring
   is designed so that with 11-15 MSL stocks, you typically see
   3-4 scores above 70 (strong setups), 4-6 in 55-70 (watchable),
   and 2-4 below 55 (ready to drop from MSL).

TRANSITION MODES (system_config: compute_msl_mode):
  shadow → writes to msl_computed only (safe, default)
  hybrid → writes computed fields to master_shortlist
  full   → master_shortlist computed entirely here (Sheet = symbol list only)
"""

import sys, json, os
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, IST, cfg, cfg_bool, is_kill_switch_active, DRY_RUN


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

PRESERVE_FIELDS = frozenset({
    "symbol", "company_name", "sector", "industry", "strategy_source",
    "notes", "is_ipo", "trade_allowed", "pos_type", "suggested",
    "ai_conviction", "ai_conviction_reason", "ai_risks", "ai_suggested_action",
    "ai_note", "ai_provider", "ai_fallback_used", "ai_shortlist_rank",
    "ai_shortlist_reason", "created_at",
    "event_bias", "event_sectors", "upcoming_news",
    "entry_ready", "exec_eligibility", "entry_action", "opp_type",
    "base_rank", "entry_mode",
})


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_data(sb, today: str, mode: str = "shadow") -> dict:
    """6 bulk queries — no per-symbol calls.
    
    Symbol source priority:
      shadow mode  → master_shortlist (Sheet-driven, as before)
      hybrid/full  → msl_computed (screen_stocks output) FIRST,
                     fall back to master_shortlist if msl_computed empty
    This ensures compute_msl enriches the SCREENER'S symbol selection, not the Sheet's.
    """
    mode = cfg("compute_msl_mode", "shadow")

    # ── Determine symbol source based on mode ─────────────────────────────────
    screener_rows = []
    if mode in ("hybrid", "full"):
        screener_rows = (sb.table("msl_computed")
                           .select("symbol,sector,strategy_source,current_price,final_score,composite_score")
                           .eq("date", today)
                           .execute().data)
        if screener_rows:
            logger.info(f"  Symbol source: msl_computed (screen_stocks) — {len(screener_rows)} symbols")

    # Fall back to master_shortlist (shadow mode or if screener hasn't run yet)
    source_table = "msl_computed" if mode == "shadow" else "master_shortlist"
    msl_rows = sb.table(source_table).select("*").eq("date", today).execute().data
    if not msl_rows:
        latest = sb.table(source_table).select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            fb = latest[0]["date"]
            msl_rows = sb.table(source_table).select("*").eq("date", fb).execute().data
            logger.warning(f"{source_table}: falling back to {fb}")
    msl_map = {r["symbol"]: r for r in (msl_rows or [])}
    logger.info(f"  Source table: {source_table} ({len(msl_map)} symbols)")

    # Build msl_map: prefer screener rows for symbol list, use MSL for Sheet fields
    msl_map = {r["symbol"]: r for r in (msl_rows or [])}

    if screener_rows:
        # Merge: screener provides strategy_source + scores; MSL provides Sheet fields
        for r in screener_rows:
            sym = r["symbol"]
            if sym in msl_map:
                # Update strategy_source from screener (engines list is more accurate)
                msl_map[sym]["strategy_source"] = r.get("strategy_source") or msl_map[sym].get("strategy_source")
            else:
                # Screener found a symbol not in Sheet MSL — add it (full mode)
                msl_map[sym] = r
        logger.info(f"  Symbol source: merged screener({len(screener_rows)}) + MSL({len(msl_rows)}) = {len(msl_map)} total")
    else:
        logger.info(f"  Symbol source: master_shortlist only ({len(msl_map)} symbols) — shadow mode or screener not yet run")

    symbols = list(msl_map.keys())
    if not symbols:
        logger.error("No MSL symbols")
        return {}

    # All stock_data_daily fields — we use 60+ of 70+
    stock_rows = sb.table("stock_data_daily").select("*").eq("date", today).in_("symbol", symbols).execute().data
    if not stock_rows:
        latest_s = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest_s:
            fb_s = latest_s[0]["date"]
            stock_rows = sb.table("stock_data_daily").select("*").eq("date", fb_s).in_("symbol", symbols).execute().data
            logger.warning(f"stock_data_daily: falling back to {fb_s}")
    stock_map = {r["symbol"]: r for r in (stock_rows or [])}

    # msl_history — last 60 days for velocity + tenure + HA trend context
    cutoff    = str(today_ist() - timedelta(days=60))
    hist_rows = (sb.table("msl_history")
                   .select("*").in_("symbol", symbols)
                   .gte("snapshot_date", cutoff)
                   .order("snapshot_date", desc=True)
                   .execute().data)
    history_map: dict = defaultdict(list)
    for r in hist_rows:
        sym = r.get("symbol")
        if sym:
            history_map[sym].append(r)

    # market_regime — latest (provides regime + predicted_regime)
    regime_rows = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # sector_strength — latest available
    sector_rows = sb.table("sector_strength").select("sector,rank").eq("date", today).execute().data
    if not sector_rows:
        sector_rows = sb.table("sector_strength").select("sector,rank").order("date", desc=True).limit(50).execute().data
    sector_rank = {r["sector"]: r["rank"] for r in sector_rows if r.get("rank")}

    # open_positions — live status
    open_rows = sb.table("open_positions").select("symbol").eq("status", "ACTIVE").execute().data
    open_syms = {r["symbol"] for r in (open_rows or [])}

    logger.info(
        f"  Loaded: {len(symbols)} symbols | {len(stock_map)} stock_data | "
        f"{len(hist_rows)} history | {len(sector_rank)} sectors | "
        f"regime={regime.get('regime','?')} | {len(open_syms)} open positions"
    )
    return {
        "msl_map":     msl_map,
        "stock_map":   stock_map,
        "history_map": dict(history_map),
        "regime":      regime,
        "sector_rank": sector_rank,
        "open_syms":   open_syms,
    }


# ─────────────────────────────────────────────────────────────────────────────
# REGIME CONTEXT — the foundation that calibrates all other computations
# ─────────────────────────────────────────────────────────────────────────────

def build_regime_context(regime: dict) -> dict:
    """
    Resolve current regime and return threshold multipliers.
    Uses predicted_regime if fresh (< 24h), otherwise manual regime.

    Why this matters for swing trading:
    - In a TRENDING bull market, RSI can stay 80-90 for weeks.
      Without regime context, the system would flag every hot stock
      as "extended" and miss the entire bull run.
    - In RISK OFF, RSI 65 IS extended because the broader market
      is under distribution. The same number means different things.
    """
    from datetime import datetime, timezone

    manual    = regime.get("regime", "NEUTRAL")
    predicted = regime.get("predicted_regime")
    pred_at   = regime.get("regime_predicted_at")

    active_regime = manual
    if predicted and pred_at:
        try:
            if isinstance(pred_at, str):
                pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
            else:
                pred_dt = pred_at
            if pred_dt.tzinfo is None:
                pred_dt = pred_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() < 86400:
                active_regime = predicted
        except Exception:
            pass

    vix       = float(regime.get("india_vix") or 0)
    breadth   = float(regime.get("above_200dma_pct") or 50)

    # RSI extended thresholds — where does RSI become a sell signal?
    # In TRENDING markets with high ADX, RSI 85 is a continuation signal.
    # In NEUTRAL markets, RSI 73 is extended. In RISK OFF, 65 is extended.
    rsi_extended = {
        "TRENDING":  80,  # extended here = genuinely exhausted
        "RISK ON":   78,
        "NEUTRAL":   72,
        "CAUTION":   67,
        "RISK OFF":  62,
    }.get(active_regime, 72)

    # Momentum persistence — in TRENDING, momentum signals last longer
    momentum_decay = {
        "TRENDING":  0.85,  # score decays slowly (hold conviction higher)
        "RISK ON":   0.80,
        "NEUTRAL":   0.70,
        "CAUTION":   0.55,
        "RISK OFF":  0.40,
    }.get(active_regime, 0.70)

    # Score boost for the regime itself (being in a bull market lifts all boats)
    regime_boost = {
        "TRENDING":  8,
        "RISK ON":   5,
        "NEUTRAL":   0,
        "CAUTION":  -5,
        "RISK OFF": -12,
    }.get(active_regime, 0)

    # VIX adjustment — high VIX raises risk, compresses score
    vix_penalty = 0
    if vix >= 25:   vix_penalty = 10
    elif vix >= 20: vix_penalty = 5
    elif vix >= 18: vix_penalty = 2

    # Breadth adjustment — narrow breadth = leadership narrowing = higher risk
    breadth_penalty = 0
    if breadth < 35:   breadth_penalty = 8
    elif breadth < 45: breadth_penalty = 4

    return {
        "regime":            active_regime,
        "rsi_extended":      rsi_extended,       # threshold where RSI = sell warning
        "momentum_decay":    momentum_decay,
        "regime_boost":      regime_boost,
        "vix_penalty":       vix_penalty,
        "breadth_penalty":   breadth_penalty,
        "is_bull":           active_regime in ("TRENDING", "RISK ON"),
        "is_bear":           active_regime in ("RISK OFF",),
        "vix":               vix,
        "breadth":           breadth,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL FUNCTIONS — one per concept, all fields evaluated
# ─────────────────────────────────────────────────────────────────────────────

def get_rsi_extended_threshold(regime_ctx: dict, adx: float) -> float:
    """
    The single most important function in this script.
    RSI means different things at different ADX levels and in different regimes.

    Formula: base regime threshold + ADX bonus
    ADX > 35 in a TRENDING regime: RSI 88 is still NOT exhaustion.
    ADX < 15 in NEUTRAL regime: RSI 70 IS extended.
    """
    base = regime_ctx["rsi_extended"]
    # High ADX = strong trend = RSI can stay elevated longer
    adx_bonus = 7 if adx >= 38 else (4 if adx >= 28 else (1 if adx >= 20 else 0))
    return base + adx_bonus


def compute_ma_alignment(s: dict) -> dict:
    """
    How many moving averages are in perfect bullish order?

    Perfect swing alignment: price > EMA10 > EMA20 > EMA50 > SMA50 > SMA200
    Score: 0-6 (one point per level in correct order)

    A score of 5-6 = every timeframe aligned = highest conviction holds.
    A score of 2-3 = mixed signals = proceed with caution.
    A score of 0-1 = structural breakdown = avoid new entries.
    """
    close   = float(s.get("close") or s.get("current_price") or 0)
    ema_10  = float(s.get("ema_10") or 0)
    ema_20  = float(s.get("ema_20") or 0)
    ema_50  = float(s.get("ema_50") or 0)
    sma_20  = float(s.get("sma_20") or 0)
    sma_50  = float(s.get("sma_50") or 0)
    sma_200 = float(s.get("sma_200") or 0)

    score = 0
    levels = []

    # Each check: is the faster MA above the slower MA?
    if close > 0 and ema_10 > 0 and close > ema_10:
        score += 1; levels.append("P>E10")
    if ema_10 > 0 and ema_20 > 0 and ema_10 > ema_20:
        score += 1; levels.append("E10>E20")
    if ema_20 > 0 and ema_50 > 0 and ema_20 > ema_50:
        score += 1; levels.append("E20>E50")
    if ema_50 > 0 and sma_50 > 0 and ema_50 > sma_50 * 0.99:   # EMA50 ≈ SMA50
        score += 1; levels.append("E50≈S50")
    if sma_50 > 0 and sma_200 > 0 and sma_50 > sma_200:
        score += 1; levels.append("S50>S200")  # golden cross
    if close > 0 and sma_20 > 0 and sma_20 > sma_50 > 0:
        score += 1; levels.append("S20>S50")

    return {"ma_alignment_score": score, "ma_levels": ",".join(levels)}


def compute_ha_signal(s: dict) -> dict:
    """
    Heikin Ashi candle quality — smoothed trend confirmation.
    We have ha_high, ha_low, ha_close (no ha_open in schema).

    HA Close Position Ratio = (ha_close - ha_low) / (ha_high - ha_low)
    > 0.65: strong bullish HA candle (closed near high — buyers dominant all session)
    0.40-0.65: neutral
    < 0.40: bearish HA candle (closed near low — sellers took control)

    Combined with wk_hi_high/wk_hi_low for multi-session trend confirmation.
    """
    ha_high = float(s.get("ha_high") or 0)
    ha_low  = float(s.get("ha_low") or 0)
    ha_close= float(s.get("ha_close") or 0)

    if not ha_high or not ha_low or ha_high <= ha_low:
        return {"ha_ratio": None, "ha_signal": "NEUTRAL", "ha_score": 50}

    ratio = (ha_close - ha_low) / (ha_high - ha_low)

    if ratio >= 0.72:    signal = "STRONGLY_BULLISH"; score = 90
    elif ratio >= 0.62:  signal = "BULLISH";          score = 72
    elif ratio >= 0.45:  signal = "NEUTRAL";          score = 52
    elif ratio >= 0.32:  signal = "BEARISH";          score = 32
    else:                signal = "STRONGLY_BEARISH"; score = 15

    return {"ha_ratio": round(ratio, 3), "ha_signal": signal, "ha_score": score}


def compute_bb_context(s: dict) -> dict:
    """
    Bollinger Band analysis: squeeze detection + position.

    BB Squeeze: (bb_upper - bb_lower) / close — low = coiling spring
    BB Position: (close - bb_lower) / (bb_upper - bb_lower) — where in the band?

    For swing trading:
    - SQUEEZE + approaching upper band = highest breakout probability
    - Riding upper band (position > 80%) = STRONG TREND (don't sell — this is
      the RSI-at-85 equivalent for BB. In strong trends, price walks the upper band.)
    - Near lower band (position < 25%) in an uptrend = ENTRY OPPORTUNITY
    - EXPANDING BB with price near upper band = confirmed breakout underway
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    bb_upper = float(s.get("bb_upper") or 0)
    bb_lower = float(s.get("bb_lower") or 0)

    if not close or not bb_upper or not bb_lower or bb_upper <= bb_lower:
        return {"bb_width_pct": None, "bb_position_pct": None,
                "bb_squeeze": False, "bb_context": "UNKNOWN"}

    bb_width_pct  = round((bb_upper - bb_lower) / close * 100, 2)
    bb_position   = round((close - bb_lower) / (bb_upper - bb_lower) * 100, 1)

    squeeze = bb_width_pct < 5.0   # tight = pre-breakout coil

    if squeeze and bb_position > 70:       ctx = "SQUEEZE_BULLISH"
    elif squeeze and bb_position < 35:     ctx = "SQUEEZE_AT_LOW"
    elif bb_position > 85:                 ctx = "RIDING_UPPER"    # strong trend signal
    elif bb_position > 65:                 ctx = "UPPER_HALF"
    elif bb_position > 40:                 ctx = "MIDDLE"
    elif bb_position > 20:                 ctx = "LOWER_HALF"
    else:                                  ctx = "NEAR_LOWER"      # entry opportunity in uptrend

    return {
        "bb_width_pct":    bb_width_pct,
        "bb_position_pct": bb_position,
        "bb_squeeze":      squeeze,
        "bb_context":      ctx,
    }


def compute_psar_context(s: dict) -> dict:
    """
    Dual trend confirmation: Supertrend + Parabolic SAR.
    Two different algorithms agreeing = higher trend conviction.

    Also computes cushion from each — how far before a flip?
    Large cushion = trend has room to run.
    Small cushion = trend fragile, tighten SL.
    """
    close      = float(s.get("close") or s.get("current_price") or 0)
    supertrend = float(s.get("supertrend") or 0)
    psar       = float(s.get("psar") or 0)
    above_st   = bool(s.get("above_st"))

    result = {"st_cushion_pct": None, "psar_cushion_pct": None,
              "dual_trend_confirmed": False, "psar_above": False}

    if close > 0:
        if supertrend > 0 and above_st:
            result["st_cushion_pct"] = round((close - supertrend) / close * 100, 2)
        if psar > 0:
            result["psar_above"] = close > psar
            if close > psar:
                result["psar_cushion_pct"] = round((close - psar) / close * 100, 2)

        # Both trend indicators confirming = strongest signal
        result["dual_trend_confirmed"] = above_st and result["psar_above"]

    return result


def compute_macd_context(s: dict) -> dict:
    """
    MACD momentum direction and acceleration.

    For swing trading, MACD histogram direction is more important than level.
    A positive and RISING histogram = momentum accelerating.
    A positive but FALLING histogram = deceleration (hold but prepare to exit).
    Zero-line crossover from below = strong entry signal.

    We can't detect acceleration from one day's snapshot, but we can classify
    the current state meaningfully. Use msl_history to detect trend.
    """
    macd_h   = float(s.get("macd_hist") or 0)
    macd_l   = float(s.get("macd_line") or 0)
    macd_sig = float(s.get("macd_signal") or 0)

    # Classification of current state
    if macd_h > 0.5:                    direction = "POSITIVE_STRONG"
    elif macd_h > 0.05:                 direction = "POSITIVE"
    elif macd_h > -0.05:               direction = "NEUTRAL"
    elif macd_h > -0.5:               direction = "NEGATIVE"
    else:                               direction = "NEGATIVE_STRONG"

    # MACD line vs signal: crossing detection
    # macd_line above signal AND positive = bullish
    crossing_up = macd_l > macd_sig and macd_h > 0
    crossing_down = macd_l < macd_sig and macd_h < 0

    # Score: used as input to momentum_score
    if direction == "POSITIVE_STRONG":     macd_score = 90
    elif direction == "POSITIVE":          macd_score = 68
    elif direction == "NEUTRAL":           macd_score = 48
    elif direction == "NEGATIVE":          macd_score = 25
    else:                                  macd_score = 8

    if crossing_up:    macd_score = min(macd_score + 12, 100)
    if crossing_down:  macd_score = max(macd_score - 12, 0)

    return {
        "macd_direction": direction,
        "macd_score":     macd_score,
        "macd_crossing_up": crossing_up,
    }


def compute_vwap_context(s: dict) -> dict:
    """
    Multi-timeframe VWAP alignment.
    VWAP = the average price weighted by volume = institutional cost basis.

    Interpretation:
    - Close > today's VWAP: buyers were dominant intraday (not just a gap up)
    - Close > VWAP_20d: longs from the past month are profitable → support zone
    - Close > VWAP_50d: 2-month institutional cost basis is supportive

    For swing entries: enter when price is ABOVE the 20d VWAP (institutional support)
    but not EXCESSIVELY above VWAP_50d (which would suggest extension).
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    vwap     = float(s.get("vwap") or 0)
    vwap_20d = float(s.get("vwap_20d") or 0)
    vwap_50d = float(s.get("vwap_50d") or 0)

    if not close:
        return {"vwap_alignment": "UNKNOWN", "vwap_score": 50,
                "above_vwap_today": False, "dist_vwap_20d_pct": None}

    above_today  = vwap > 0 and close > vwap
    above_20d    = vwap_20d > 0 and close > vwap_20d
    above_50d    = vwap_50d > 0 and close > vwap_50d

    dist_20d = round((close - vwap_20d) / vwap_20d * 100, 2) if vwap_20d > 0 else None
    dist_50d = round((close - vwap_50d) / vwap_50d * 100, 2) if vwap_50d > 0 else None

    count = sum([above_today, above_20d, above_50d])

    if count == 3:
        # All above — quality depends on how far above 50d VWAP
        if dist_50d and dist_50d > 20:  alignment = "ABOVE_ALL_EXTENDED"
        else:                            alignment = "ABOVE_ALL"
    elif count == 2 and above_20d:       alignment = "ABOVE_20D"
    elif count == 1 and above_today:     alignment = "ABOVE_TODAY_ONLY"
    elif count == 0:                     alignment = "BELOW_ALL"
    else:                                alignment = "MIXED"

    score_map = {
        "ABOVE_ALL": 85, "ABOVE_ALL_EXTENDED": 65, "ABOVE_20D": 62,
        "ABOVE_TODAY_ONLY": 42, "MIXED": 40, "BELOW_ALL": 20, "UNKNOWN": 50
    }
    return {
        "vwap_alignment":     alignment,
        "vwap_score":         score_map.get(alignment, 50),
        "above_vwap_today":   above_today,
        "dist_vwap_20d_pct":  dist_20d,
        "dist_vwap_50d_pct":  dist_50d,
    }


def compute_volume_trend(s: dict) -> dict:
    """
    Multi-period volume analysis.
    avg_vol_20d vs avg_vol_50d: is RECENT average volume higher than the longer term?
    Sustained higher recent volume = institutional accumulation (not just one spike).

    Also: today's value_cr — large traded value = institutional liquidity.
    """
    vol_20d  = float(s.get("avg_vol_20d") or 0)
    vol_50d  = float(s.get("avg_vol_50d") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    value_cr = float(s.get("value_cr") or 0)

    if vol_50d > 0 and vol_20d > 0:
        vol_trend_ratio = round(vol_20d / vol_50d, 3)
    else:
        vol_trend_ratio = None

    if vol_trend_ratio is None:    trend = "UNKNOWN"
    elif vol_trend_ratio >= 1.20:  trend = "EXPANDING"    # institutions accumulating
    elif vol_trend_ratio >= 0.90:  trend = "STABLE"
    elif vol_trend_ratio >= 0.75:  trend = "CONTRACTING"
    else:                          trend = "DECLINING"     # losing interest

    # Liquidity quality (for position sizing safety)
    if value_cr >= 500:    liquidity = "HIGH"
    elif value_cr >= 100:  liquidity = "MEDIUM"
    elif value_cr >= 30:   liquidity = "LOW"
    else:                  liquidity = "VERY_LOW"

    return {
        "volume_trend":       trend,
        "vol_trend_ratio":    vol_trend_ratio,
        "liquidity_quality":  liquidity,
        "value_cr":           value_cr,
    }


def compute_weekly_structure(s: dict) -> dict:
    """
    Weekly higher highs + higher lows = the most reliable uptrend confirmation.
    Both TRUE = textbook uptrend structure.
    One TRUE = partial structure (see interpretation below).

    For swing trading, wk_hi_low is MORE important than wk_hi_high.
    If pullbacks are getting shallower (higher lows), the uptrend is healthy
    even if the stock hasn't printed new highs yet.
    """
    wk_hh = bool(s.get("wk_hi_high"))
    wk_hl = bool(s.get("wk_hi_low"))

    if wk_hh and wk_hl:
        structure = "STRONG"       # textbook uptrend: buy pullbacks
        score     = 90
    elif wk_hl and not wk_hh:
        structure = "CONSOLIDATING"# pullbacks healthy, highs not yet broken
        score     = 65
    elif wk_hh and not wk_hl:
        structure = "CAUTION"      # new highs but pullbacks deepening = weakening
        score     = 42
    else:
        structure = "WEAK"         # no uptrend structure = avoid
        score     = 15

    return {"weekly_structure": structure, "weekly_structure_score": score}


def compute_fundamental_quality(s: dict) -> dict:
    """
    For 1-4 week swing trading, fundamentals are a FILTER not a driver.
    We use them to penalise stocks with deteriorating earnings.
    A technically perfect setup with collapsing earnings = elevated risk.

    STRONG: positive + improving quarterly earnings
    NEUTRAL: positive but flat/mixed
    WEAK: negative or sharply deteriorating
    """
    quarterly = float(s.get("quarterly_net_profit") or 0)
    q_var     = float(s.get("quarterly_variance") or 0)
    ttm       = float(s.get("ttm_net_profit") or 0)
    eps       = float(s.get("eps") or 0)

    if quarterly > 0 and q_var > 10:     quality = "IMPROVING"; penalty = 0
    elif quarterly > 0 and q_var >= -10: quality = "STABLE";    penalty = 0
    elif quarterly > 0 and q_var < -10:  quality = "DECLINING";  penalty = 5
    elif quarterly <= 0 and ttm > 0:     quality = "WEAK";       penalty = 8
    elif quarterly <= 0 and ttm <= 0:    quality = "LOSS_MAKING"; penalty = 12
    else:                                quality = "UNKNOWN";     penalty = 0

    return {"fundamental_quality": quality, "fundamental_penalty": penalty}


def compute_stoch_context(s: dict, regime_ctx: dict, adx: float) -> dict:
    """
    Stochastic interpretation is HIGHLY context-dependent.

    In a strong trend (ADX > 25, di_plus dominant):
      - stoch > 80 = NORMAL, ignore "overbought" — momentum is strong
      - stoch turning down from > 80 = first warning (not immediate sell)
      
    In a ranging market (ADX < 18):
      - stoch > 80 = overbought (elevated risk)
      - stoch < 20 = oversold (potential entry)
      
    Sweet spot for swing ENTRY: stoch 40-75 in uptrend (has room to run)
    """
    stoch    = float(s.get("stoch") or 50)
    is_trend = adx >= 22 and regime_ctx.get("is_bull", False)

    if is_trend:
        if stoch >= 80:    stoch_ctx = "STRONG_TREND";   stoch_score = 78
        elif stoch >= 60:  stoch_ctx = "TRENDING";       stoch_score = 85  # sweet spot
        elif stoch >= 40:  stoch_ctx = "MID_TREND";      stoch_score = 70
        elif stoch >= 20:  stoch_ctx = "PULLBACK";       stoch_score = 65  # entry opp
        else:              stoch_ctx = "OVERSOLD_TREND"; stoch_score = 55
    else:
        if stoch >= 80:    stoch_ctx = "OVERBOUGHT";     stoch_score = 30
        elif stoch >= 65:  stoch_ctx = "ELEVATED";       stoch_score = 50
        elif stoch >= 40:  stoch_ctx = "NEUTRAL";        stoch_score = 65
        elif stoch >= 20:  stoch_ctx = "LOW";            stoch_score = 72
        else:              stoch_ctx = "OVERSOLD";       stoch_score = 60

    return {"stoch_context": stoch_ctx, "stoch_score": stoch_score}


# ─────────────────────────────────────────────────────────────────────────────
# CORE SWING TRADE SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum_state(s: dict, regime_ctx: dict) -> str:
    """
    Regime-aware momentum quality. RSI threshold adjusts with regime + ADX.
    In a TRENDING market with ADX 40, HOT requires RSI > 72 (not 60).
    This prevents labelling everything HOT in a bull run.
    """
    rsi_d      = float(s.get("rsi_daily") or 0)
    rsi_w      = float(s.get("rsi_weekly") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    vol_r      = float(s.get("vol_ratio") or 0)
    above_50   = bool(s.get("above_sma50"))
    macd_h     = float(s.get("macd_hist") or 0)
    above_st   = bool(s.get("above_st"))
    wk_hh      = bool(s.get("wk_hi_high"))
    wk_hl      = bool(s.get("wk_hi_low"))

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    rsi_hot_min = 57 if regime_ctx["is_bull"] else 62   # bull markets = lower bar for HOT

    # HOT: all primary signals aligned
    hot = sum([
        rsi_d >= rsi_hot_min and rsi_d < extended_thresh,   # strong but not extended
        rsi_w >= 56,
        adx >= 22 and di_plus > di_minus and (di_plus - di_minus) >= 6,
        vol_r >= 1.35,
        above_50,
        macd_h > 0,
        above_st,
        wk_hh and wk_hl,   # structural confirmation
    ])
    if hot >= 6:  return "HOT"

    # WEAK: deteriorating signals
    weak = sum([
        rsi_d < 46,
        di_minus > di_plus and adx > 18,
        not above_50,
        macd_h < -0.3,
        vol_r < 0.85,
        not above_st,
    ])
    if weak >= 3: return "WEAK"

    # BUILDING: positive lean but not yet confirmed
    build = sum([
        rsi_d >= 52,
        adx >= 17 and di_plus > di_minus,
        vol_r >= 1.15,
        above_50,
        macd_h >= 0,
    ])
    if build >= 3: return "BUILDING"

    return "STABLE"


def compute_momentum_phase(s: dict, hist: list, regime_ctx: dict) -> str:
    """
    WHERE in the momentum cycle?

    The regime matters here critically:
    - In TRENDING markets, EXPANSION can last months. Don't label it EXTENDED too soon.
    - RSI extended threshold shifts with regime.
    - Use msl_history to detect if the phase has been EXTENDED for multiple days
      (persistent extension = exhaustion risk even in bull markets).
    """
    rsi_d      = float(s.get("rsi_daily") or 0)
    rsi_m      = float(s.get("rsi_monthly") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    dist_sma50 = float(s.get("dist_sma50") or 0)
    above_50   = bool(s.get("above_sma50"))
    pct_change = float(s.get("pct_change") or 0)
    ret_1m     = float(s.get("ret_1m") or 0)

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    exhausted_thresh = extended_thresh + 7   # another 7 points above extended = exhaustion territory

    # EXHAUSTED: overextended on MULTIPLE dimensions
    # In bull markets this threshold is higher — RSI 88 with ADX 40 is not exhausted
    exhausted_signals = sum([
        rsi_d > exhausted_thresh,
        rsi_m > 78 and rsi_m > 0,
        dist_sma50 > 25,
        ret_1m > 35,    # moved 35%+ in a month = unsustainable
    ])
    # Check persistent extension from history
    persistent_extended = sum(
        1 for h in hist[:5] if h.get("momentum_phase") in ("EXTENDED", "EXHAUSTED")
    ) >= 4

    if exhausted_signals >= 2 or (exhausted_signals >= 1 and persistent_extended):
        return "EXHAUSTED"

    # FLAT: no directional conviction
    if adx < 17 or abs(di_plus - di_minus) < 3:
        return "FLAT"

    # EXTENDED: mature trend — elevated but not yet exhausted
    # Threshold is HIGHER in bull markets
    extended_dist = 18 if regime_ctx["is_bull"] else 12
    if (rsi_d > extended_thresh or dist_sma50 > extended_dist) and adx > 20:
        return "EXTENDED"

    # EXPANSION: the sweet spot — confirmed, active, mid-cycle
    if adx >= 18 and di_plus > di_minus and rsi_d >= 54 and above_50:
        return "EXPANSION"

    # EARLY: just beginning — ADX rising from low, RSI crossing 50
    if adx >= 14 and di_plus > di_minus and above_50 and rsi_d >= 47:
        return "EARLY"

    return "FLAT"


def compute_velocity_state(s: dict, hist: list) -> str:
    """
    Is the pace of momentum INCREASING, FLAT, or DECREASING?
    Uses multi-period return comparison for mathematical detection.

    For swing traders: ACCELERATING = enter/hold with full size.
    DECELERATING = reduce position or tighten SL. FLAT = hold/watch.
    """
    ret_1w = float(s.get("ret_1w") or 0)
    ret_1m = float(s.get("ret_1m") or 0)
    ret_3m = float(s.get("ret_3m") or 0)

    if ret_1m == 0 and ret_3m == 0:
        return "FLAT"

    weekly_pace    = ret_1m / 4.0 if ret_1m != 0 else 0
    quarterly_pace = ret_3m / 3.0 if ret_3m != 0 else 0

    # Week outpacing monthly average by 40%+ = accelerating
    week_accel  = (ret_1w > weekly_pace * 1.40) if weekly_pace > 0.5 else (ret_1w > 2.5)
    week_decel  = (ret_1w < weekly_pace * 0.40) if weekly_pace > 0.5 else (ret_1w < -1.0)
    month_accel = (ret_1m > quarterly_pace * 1.35) if quarterly_pace > 0.5 else (ret_1m > 4.0)
    month_decel = (ret_1m < quarterly_pace * 0.40) if quarterly_pace > 0.5 else (ret_1m < -3.0)

    # Also check from history if MACD hist has been improving
    macd_h = float(s.get("macd_hist") or 0)
    hist_prev_macd = [h for h in hist[:5] if h.get("momentum_phase")]
    macd_accel = macd_h > 0   # simple proxy; enriched by history in future

    accel = sum([week_accel, month_accel, macd_accel])
    decel = sum([week_decel, month_decel])

    if accel >= 2 and decel == 0:   return "ACCELERATING"
    if decel >= 2:                   return "DECELERATING"
    if accel >= 1 and decel == 0:   return "ACCELERATING"
    if decel >= 1 and accel == 0:   return "DECELERATING"
    return "FLAT"


def compute_trend_maturity(s: dict, regime_ctx: dict) -> str:
    """
    Regime-aware trend maturity — 5 states: FRESH, DEVELOPING, LATE, EXHAUSTED, FLAT.
    
    FLAT:      No clear trend direction. ADX low, price near/below SMA50.
    FRESH:     Trend just initiated. ADX recently crossed up, price recently
               cleared SMA50, RSI in 48-60 range — has the MOST runway left.
    DEVELOPING: Trend confirmed and active. Mid-cycle. Most swing trades live here.
    LATE:      Trend mature, extension beginning. Still valid but R:R compressing.
               Reduce size on new entries, hold existing.
    EXHAUSTED: Multiple overextension signals. Avoid new entries. Manage exits.
    """
    rsi_d      = float(s.get("rsi_daily") or 0)
    rsi_w      = float(s.get("rsi_weekly") or 0)
    rsi_m      = float(s.get("rsi_monthly") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    dist_sma50 = float(s.get("dist_sma50") or 0)
    ret_6m     = float(s.get("ret_6m") or 0)
    ret_1m     = float(s.get("ret_1m") or 0)
    above_50   = bool(s.get("above_sma50"))
    sma50_200  = bool(s.get("sma50_gt_200"))
    wk_hh      = bool(s.get("wk_hi_high"))
    wk_hl      = bool(s.get("wk_hi_low"))

    extended_thresh  = get_rsi_extended_threshold(regime_ctx, adx)
    exhausted_thresh = extended_thresh + 7

    # ── EXHAUSTED: multiple overextension dimensions ──────────────────────────
    # ADX removed from exhaustion — high ADX = strong trend, not exhausted
    # ret_12m only counts if RSI also elevated (avoids penalising compounders)
    rsi_truly_exhausted = rsi_d > exhausted_thresh
    exhausted_signals = sum([
        rsi_truly_exhausted,
        rsi_m > 0 and rsi_m > 78,
        dist_sma50 > 22,
        ret_6m > 60,                    # +60% in 6m = genuinely overextended
        ret_1m > 20 and rsi_d > extended_thresh,  # parabolic month + high RSI
    ])
    if exhausted_signals >= 2:
        return "EXHAUSTED"

    # ── FLAT: no directional conviction ──────────────────────────────────────
    # Check early — don't waste time on trendless stocks
    if not above_50 or adx < 14 or di_plus <= di_minus:
        return "FLAT"

    # ── LATE: trend mature, extension building ────────────────────────────────
    # ADX deliberately EXCLUDED — high ADX means trend strength, not lateness
    # late_thresh = extended_thresh - 6 (approaching extended but not there yet)
    late_thresh = extended_thresh - 6
    late_signals = sum([
        rsi_d > late_thresh,                   # RSI approaching extended zone
        dist_sma50 > 13,                        # price stretched from SMA50
        ret_6m > 30,                            # 6m move getting large
        ret_1m > 12,                            # 1m move accelerating late
        rsi_m > 0 and rsi_m > 68,              # monthly RSI elevated
        not wk_hl and wk_hh,                   # new highs but pullbacks deepening
    ])
    if late_signals >= 2:
        return "LATE"

    # ── FRESH: trend just initiated — maximum runway ─────────────────────────
    # Conditions: ADX rising through 14-26 range (not yet fully established),
    # price recently cleared SMA50 (dist small), RSI not yet hot,
    # golden cross present (structural quality) OR weekly HL just formed.
    #
    # Upper ADX bound is 26 not 22 — a stock can have ADX=24 and still be FRESH
    # if RSI is only 52 and dist_sma50 is only 3%. The key is RSI not yet heated.
    rsi_fresh_max = min(extended_thresh - 12, 62)  # regime-aware RSI ceiling for FRESH
    fresh_signals = sum([
        14 <= adx <= 26,                        # ADX building but not established
        dist_sma50 < 6,                         # price close to SMA50 (recently crossed)
        rsi_d < rsi_fresh_max,                  # RSI not yet hot
        sma50_200,                              # golden cross = structural quality
        wk_hl and not wk_hh,                    # higher lows forming, highs not yet broken
    ])
    if fresh_signals >= 3:
        return "FRESH"

    # ── DEVELOPING: confirmed active mid-cycle trend ──────────────────────────
    # Requires directional conviction (DI spread) + above SMA50 + RSI in trend zone
    di_spread = di_plus - di_minus
    if above_50 and adx > 16 and di_spread >= 4 and rsi_d > 50:
        return "DEVELOPING"

    # ── FLAT fallthrough: above SMA50 but no clear trend ─────────────────────
    # (e.g., adx > 14 but di_plus <= di_minus was caught above, 
    #  but could also be adx 14-16 range without enough di spread)
    return "FLAT"


def compute_lifecycle(s: dict, hist: list, regime_ctx: dict,
                       struct_edge: str = "NO",
                       momentum_phase: str = "",
                       velocity_state: str = "",
                       trend_maturity: str = "",
                       entry_timing_type: str = "",
                       in_position: bool = False) -> str:
    """
    Trend lifecycle action signal — 5 states:

    EXIT:       Structural breakdown confirmed. Close or stop-manage position.
                Requires strong convergence — avoids hair-trigger exits.
    REDUCE:     Trend intact but maturing/decelerating. Trim position size.
                For held positions approaching LATE/EXHAUSTED.
    HOLD:       Trend intact, momentum healthy. No action needed.
    ADD:        Trend in confirmed EXPANSION with acceleration. Add to position.
    DEVELOPING: Trend forming, not yet confirmed. Watch only — small initial size.
    """
    rsi_d       = float(s.get("rsi_daily") or 0)
    adx         = float(s.get("adx") or 0)
    di_plus     = float(s.get("di_plus") or 0)
    di_minus    = float(s.get("di_minus") or 0)
    above_50    = bool(s.get("above_sma50"))
    sma50_gt200 = bool(s.get("sma50_gt_200"))
    above_st    = bool(s.get("above_st"))
    psar        = float(s.get("psar") or 0)
    close       = float(s.get("close") or s.get("current_price") or 0)
    macd_h      = float(s.get("macd_hist") or 0)
    wk_hl       = bool(s.get("wk_hi_low"))
    wk_hh       = bool(s.get("wk_hi_high"))
    sma_50      = float(s.get("sma_50") or 0)

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)

    # ── EXIT: structural breakdown — require 3+ signals ──────────────────────
    # In RISK OFF regime, lower the bar to 2 signals (market amplifies breakdowns)
    exit_threshold = 2 if regime_ctx.get("is_bear") else 3

    exit_signals = sum([
        rsi_d < 42,
        di_minus > di_plus and adx > 18,           # directional flip
        not above_50,
        macd_h < -0.5,
        not above_st,
        psar > 0 and close > 0 and close < psar,   # PSAR flipped
        not wk_hl,                                  # pullbacks deepening
        sma_50 > 0 and close < sma_50 * 0.97,      # decisively below SMA50
        struct_edge == "NO" and in_position,        # structural quality lost while held
    ])
    if exit_signals >= exit_threshold:
        return "EXIT"

    # ── REDUCE: held position, trend maturing or decelerating ────────────────
    # Only applies to held positions — no point telling a watcher to reduce
    if in_position:
        reduce_signals = sum([
            trend_maturity in ("LATE", "EXHAUSTED"),
            velocity_state == "DECELERATING",
            rsi_d > extended_thresh,               # RSI extended in current regime
            entry_timing_type == "EXTENDED",        # price far from entry zone
            not wk_hl and above_50,                 # losing HL structure but not yet exit
        ])
        if reduce_signals >= 2:
            return "REDUCE"

    # ── ADD: expansion phase with acceleration — highest conviction ───────────
    # Deliberately strict — ADD means put real money to work
    add_conditions = (
        momentum_phase == "EXPANSION"
        and velocity_state == "ACCELERATING"
        and trend_maturity not in ("LATE", "EXHAUSTED")
        and entry_timing_type not in ("EXTENDED", "WAIT")
        and struct_edge == "YES"
        and rsi_d < extended_thresh          # not already overextended
    )
    if add_conditions:
        return "ADD"

    # ── HOLD: trend intact, no action required ───────────────────────────────
    hold_signals = sum([
        above_50,
        sma50_gt200,
        di_plus > di_minus,
        above_st,
        rsi_d > 48,
        macd_h > 0,
        wk_hh or wk_hl,
    ])
    if hold_signals >= 4:
        return "HOLD"

    # ── DEVELOPING: trend forming, insufficient confirmation ─────────────────
    return "DEVELOPING"


def compute_struct_edge(s: dict, ma_ctx: dict, bb_ctx: dict) -> str:
    """
    Structural convergence — tiered: mandatory floor + quality scoring.

    MANDATORY TIER (all required — structural non-negotiables):
      above_sma50 + sma50_gt_200 + above_st
      These three form the minimum trend structure for a swing trade.
      wk_hl is NOT mandatory (a stock can be building HL without confirming yet)
      but it IS a strong quality signal.

    QUALITY TIER (5+ of 9 = YES):
      Additional confirmation signals. If mandatory tier passes but quality
      is weak (< 5), return "WEAK_YES" — but for simplicity we still use YES/NO.
      The nuance lives in the score, not this binary.
    """
    above_50    = bool(s.get("above_sma50"))
    sma50_200   = bool(s.get("sma50_gt_200"))
    above_st    = bool(s.get("above_st"))

    # Mandatory floor — any one missing = NO, no exceptions
    # Rationale: below SMA50 or below supertrend = trend broken for swing purposes
    if not above_50:   return "NO"
    if not sma50_200:  return "NO"
    if not above_st:   return "NO"

    # Quality signals — now evaluated on a structurally sound stock
    consol     = float(s.get("consol_range") or 999)
    delivery   = float(s.get("delivery_pct") or 0)
    rs         = float(s.get("rs_vs_nifty") or 0)
    vol_r      = float(s.get("vol_ratio") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    wk_hh      = bool(s.get("wk_hi_high"))
    wk_hl      = bool(s.get("wk_hi_low"))
    ma_score   = ma_ctx.get("ma_alignment_score", 0)
    squeeze    = bb_ctx.get("bb_squeeze", False)
    close      = float(s.get("close") or 0)
    psar       = float(s.get("psar") or 0)
    psar_above = close > psar if (close > 0 and psar > 0) else False

    quality_signals = sum([
        consol < 12,                        # price in tight range
        delivery >= 45,                     # institutional participation
        rs > 2.0,                           # outperforming Nifty
        vol_r >= 1.2,                       # above-average volume
        adx >= 18 and di_plus > di_minus,  # directional trend active
        wk_hl,                              # higher lows = healthy uptrend
        wk_hh and wk_hl,                   # full HH+HL structure (counts as 2nd signal if both)
        ma_score >= 4,                      # strong MA stack
        squeeze,                            # BB coiling = pre-breakout
        psar_above,                         # PSAR below price = trend confirmed
    ])

    # 5 of 10 quality signals required (mandatory tier already passed)
    return "YES" if quality_signals >= 5 else "NO"


def compute_reentry_mode(s: dict) -> str:
    """
    ELIGIBLE: extended stock that pulled back 2-10% from 30d high,
    still above SMA50, RSI > 44, trend structure intact.
    This is the "buy the dip in an uptrend" condition.
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    high_30d = float(s.get("high_30d") or 0)
    above_50 = bool(s.get("above_sma50"))
    rsi_d    = float(s.get("rsi_daily") or 0)
    wk_hl    = bool(s.get("wk_hi_low"))
    above_st = bool(s.get("above_st"))

    if not close or not high_30d or not above_50 or rsi_d < 44:
        return "NO"

    pullback_pct = (high_30d - close) / high_30d * 100
    structural_ok = wk_hl or above_st   # at least one structural signal intact

    if 2.0 <= pullback_pct <= 10.0 and structural_ok:
        return "ELIGIBLE"

    return "NO"


def compute_entry_zones(s: dict, strategy: str, regime_ctx: dict) -> tuple:
    """
    Strategy-aware, regime-aware entry zone computation.
    In bearish regimes, zones are set more conservatively (deeper pullback required).
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    sma_50   = float(s.get("sma_50") or 0)
    sma_20   = float(s.get("sma_20") or 0)
    ema_20   = float(s.get("ema_20") or 0)
    vwap_20d = float(s.get("vwap_20d") or 0)
    atr_14   = float(s.get("atr_14") or 0)
    high_30d = float(s.get("high_30d") or 0)
    low_30d  = float(s.get("low_30d") or 0)

    if not close or not atr_14:
        return None, None

    # In bearish regimes, require deeper pullback before entry (more conservative zones)
    regime_factor = 1.12 if regime_ctx["is_bear"] else (0.95 if regime_ctx["is_bull"] else 1.0)
    strat = (strategy or "").upper()

    if "CTL" in strat or "TPO" in strat:
        # CTL: optimal pullback to SMA50 area. Use the BETTER of SMA50 and VWAP_20d.
        anchor = max(sma_50, vwap_20d) if sma_50 > 0 and vwap_20d > 0 else (sma_50 or vwap_20d or close)
        if anchor > 0:
            ez_low  = round(anchor * (1 - 0.012 * regime_factor), 2)
            ez_high = round(anchor * (1 + 0.020 / regime_factor), 2)
        else:
            ez_low  = round(close - atr_14 * 1.5, 2)
            ez_high = round(close - atr_14 * 0.4, 2)

    elif "SBS" in strat:
        # SBS: near breakout of 30d consolidation top
        if high_30d > 0:
            ez_low  = round(high_30d * (1 - 0.025 * regime_factor), 2)
            ez_high = round(high_30d * (1 + 0.013 / regime_factor), 2)
        elif ema_20 > 0:
            ez_low  = round(ema_20 * 0.990, 2)
            ez_high = round(ema_20 + atr_14 * 0.6, 2)
        else:
            ez_low  = round(close - atr_14, 2)
            ez_high = round(close + atr_14 * 0.4, 2)

    else:
        # Default: VWAP_20d anchored (institutional cost basis)
        anchor = vwap_20d if vwap_20d > 0 else (ema_20 if ema_20 > 0 else (sma_20 if sma_20 > 0 else close))
        ez_low  = round(anchor * (1 - 0.010 * regime_factor), 2)
        ez_high = round(anchor + atr_14 * (0.7 / regime_factor), 2)

    # Safety: if price has fallen far below zone, shift zone toward current price
    if ez_low > 0 and close < ez_low * 0.90:
        shift   = close * 0.99
        width   = max(ez_high - ez_low, atr_14)
        ez_low  = round(shift, 2)
        ez_high = round(shift + width, 2)

    return ez_low, ez_high


def compute_entry_timing_type(s: dict, ez_low: float, ez_high: float,
                               reentry_mode: str = "NO",
                               momentum_phase: str = "") -> str:
    """
    Entry timing classification — 5 states:
    
    OPTIMAL:    Price inside entry zone. Best R:R. Enter or add.
    REENTRY:    Price pulled back from extension (2-8% off 30d high),
                trend intact. Valid second entry after missing OPTIMAL.
    APPROACHING: Price below entry zone but within 1 ATR. Wait for zone touch.
                 (Renamed from WAIT — it's not waiting, it's anticipating)
    CHASING:    Price 3-8% above zone. Elevated risk. Reduced size only.
    EXTENDED:   Price >8% above zone. Avoid new entries. Hold only.
    """
    close = float(s.get("close") or s.get("current_price") or 0)
    if not close or not ez_low or not ez_high:
        return "CHASING"   # no zone data = can't assess = treat as extended

    adx      = float(s.get("adx") or 0)
    above_50 = bool(s.get("above_sma50"))
    above_st = bool(s.get("above_st"))
    high_30d = float(s.get("high_30d") or 0)
    rsi_d    = float(s.get("rsi_daily") or 0)

    # REENTRY: pulled back from 30d high 2-8%, trend structure intact
    # Better trigger than adx<26: look at actual pullback from recent high
    if reentry_mode == "ELIGIBLE" and momentum_phase not in ("EARLY", "FLAT", ""):
        return "REENTRY"
    
    # Also detect REENTRY from price action alone (even if reentry_mode not set)
    if high_30d > 0 and above_50 and above_st:
        pullback_pct = (high_30d - close) / high_30d * 100
        if 2.0 <= pullback_pct <= 8.0 and rsi_d >= 44:
            return "REENTRY"

    # OPTIMAL: inside the computed entry zone
    if ez_low <= close <= ez_high:
        return "OPTIMAL"

    # APPROACHING: below zone but close — anticipatory state
    atr_14 = float(s.get("atr_14") or (ez_high - ez_low) or 1)
    if close < ez_low and close >= ez_low - atr_14:
        return "APPROACHING"

    # Far below zone — trend may be breaking, not approaching
    if close < ez_low - atr_14:
        return "WAIT"   # keep WAIT for genuinely distant below-zone price

    # Above zone — how far?
    dist_from_high = (close - ez_high) / ez_high
    if dist_from_high <= 0.03:    return "CHASING"    # just above zone
    elif dist_from_high <= 0.08:  return "CHASING"    # meaningfully above
    else:                          return "EXTENDED"   # >8% above zone


def compute_price_location(s: dict, ez_low: float, ez_high: float) -> tuple:
    close = float(s.get("close") or s.get("current_price") or 0)
    if not close or not ez_low:
        return "UNKNOWN", None, None
    ez_high_safe   = ez_high or ez_low * 1.02
    dist_entry_pct = round((close - ez_low) / ez_low * 100, 2)
    if close < ez_low * 0.98:                 location = "Below"
    elif ez_low <= close <= ez_high_safe:     location = "Inside"
    elif close <= ez_high_safe * 1.006:       location = "At Zone"
    else:                                      location = "Above"
    return location, round(dist_entry_pct, 2), round(dist_entry_pct, 2)


def compute_msl_history_context(sym: str, hist: list) -> dict:
    if not hist:
        return {"days_in_list": 0, "rank_vel_3d": None, "score_vel_5d": None}

    days_in_list = len(set(h.get("snapshot_date") for h in hist if h.get("snapshot_date")))
    sorted_hist  = sorted(hist, key=lambda x: x.get("snapshot_date") or "", reverse=True)

    rank_vel_3d = None
    if len(sorted_hist) >= 3:
        r0 = sorted_hist[0].get("base_rank")
        r3 = sorted_hist[2].get("base_rank")
        if r0 is not None and r3 is not None:
            rank_vel_3d = round(float(r3) - float(r0), 0)

    score_vel_5d = None
    if len(sorted_hist) >= 5:
        d0 = sorted_hist[0].get("dist_fv_pct")
        d5 = sorted_hist[4].get("dist_fv_pct")
        if d0 is not None and d5 is not None:
            score_vel_5d = round(float(d5) - float(d0), 2)

    return {"days_in_list": days_in_list, "rank_vel_3d": rank_vel_3d, "score_vel_5d": score_vel_5d}


# ─────────────────────────────────────────────────────────────────────────────
# COMPOSITE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum_score(s: dict, regime_ctx: dict, macd_ctx: dict,
                            stoch_ctx: dict, ha_ctx: dict, weekly_ctx: dict) -> float:
    """
    0-100: Pure momentum quality. Inputs from ALL oscillators + confirmations.

    Regime-aware RSI interpretation is the critical improvement over v1.
    A score of 75+ = stock is in a powerful, confirmed momentum state.
    """
    score = 0.0
    rsi_d      = float(s.get("rsi_daily") or 0)
    rsi_w      = float(s.get("rsi_weekly") or 0)
    rsi_m      = float(s.get("rsi_monthly") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    vol_r      = float(s.get("vol_ratio") or 0)
    above_50   = bool(s.get("above_sma50"))

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    hot_min         = 55 if regime_ctx["is_bull"] else 60

    # RSI DAILY — regime-aware (25 pts)
    if hot_min <= rsi_d < extended_thresh:        score += 25   # sweet spot
    elif extended_thresh <= rsi_d < extended_thresh + 8:
        score += 16   # extended but not yet exhausted in current regime
    elif rsi_d >= extended_thresh + 8:            score += 6    # genuinely exhausted RSI
    elif rsi_d >= hot_min - 6:                    score += 14   # building
    elif rsi_d >= 45:                             score += 7    # weak
    # < 45: 0 pts

    # RSI WEEKLY alignment (15 pts) — multi-timeframe confirmation
    if rsi_w >= 62:          score += 15
    elif rsi_w >= 57:        score += 11
    elif rsi_w >= 52:        score += 7
    elif rsi_w >= 47:        score += 3

    # RSI MONTHLY — strategic trend direction (8 pts)
    if rsi_m >= 62:          score += 8
    elif rsi_m >= 55:        score += 5
    elif rsi_m >= 48:        score += 2

    # ADX + DIRECTIONAL (22 pts) — trend strength and direction
    di_spread = di_plus - di_minus
    if adx >= 35 and di_plus > di_minus:
        score += 22
    elif adx >= 27 and di_spread >= 8:
        score += 17
    elif adx >= 22 and di_plus > di_minus:
        score += 12
    elif adx >= 17 and di_plus > di_minus:
        score += 7
    elif di_plus > di_minus:
        score += 3

    # MACD (12 pts)
    score += macd_ctx.get("macd_score", 50) * 0.12

    # STOCHASTIC (8 pts) — context-aware
    score += stoch_ctx.get("stoch_score", 50) * 0.08

    # HEIKIN ASHI (5 pts) — smoothed candle quality
    score += ha_ctx.get("ha_score", 50) * 0.05

    # WEEKLY STRUCTURE bonus (5 pts)
    score += weekly_ctx.get("weekly_structure_score", 30) * 0.05

    return round(min(max(score, 0.0), 100.0), 1)


def compute_validity_score(s: dict, sector: str, sector_rank: dict,
                            ez_low: float, ez_high: float,
                            ma_ctx: dict, psar_ctx: dict,
                            vwap_ctx: dict, regime_ctx: dict) -> float:
    """
    0-100: Is this a quality entry setup RIGHT NOW?
    Uses all price action, volume, and structural signals.
    Zone proximity is critical — even a perfect stock at 15% above zone is not valid.
    """
    score = 0.0

    rsi_d      = float(s.get("rsi_daily") or 0)
    vol_r      = float(s.get("vol_ratio") or 0)
    delivery   = float(s.get("delivery_pct") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    above_50   = bool(s.get("above_sma50"))
    sma50_200  = bool(s.get("sma50_gt_200"))
    above_st   = bool(s.get("above_st"))
    wk_hh      = bool(s.get("wk_hi_high"))
    wk_hl      = bool(s.get("wk_hi_low"))
    close      = float(s.get("close") or s.get("current_price") or 0)

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)

    # RSI in quality entry range (18 pts)
    rsi_sweet_lo = 52 if regime_ctx["is_bull"] else 55
    rsi_sweet_hi = extended_thresh - 4   # below extended threshold
    if rsi_sweet_lo <= rsi_d <= rsi_sweet_hi:     score += 18
    elif rsi_d >= rsi_sweet_lo - 5:              score += 10
    elif rsi_d >= 45:                            score += 5
    # Extended RSI = partial penalty
    if rsi_d > extended_thresh:
        excess = rsi_d - extended_thresh
        score -= min(excess * 1.5, 12)   # up to -12 for RSI overextension

    # Volume confirmation (14 pts)
    if vol_r >= 2.5:     score += 14
    elif vol_r >= 1.8:   score += 11
    elif vol_r >= 1.3:   score += 7
    elif vol_r >= 1.0:   score += 3

    # Delivery quality (14 pts) — institutional vs speculative
    if delivery >= 65:   score += 14
    elif delivery >= 55: score += 11
    elif delivery >= 45: score += 7
    elif delivery >= 35: score += 3

    # MA alignment (10 pts)
    score += ma_ctx.get("ma_alignment_score", 0) * (10 / 6)

    # PSAR + Supertrend dual confirmation (8 pts)
    if psar_ctx.get("dual_trend_confirmed"):          score += 8
    elif above_st:                                     score += 4
    st_cushion = psar_ctx.get("st_cushion_pct") or 0
    if st_cushion >= 5:   score += 2   # large cushion = trend has room

    # Weekly structure (8 pts)
    if wk_hh and wk_hl:  score += 8
    elif wk_hl:           score += 5
    elif wk_hh:           score += 2

    # VWAP alignment (8 pts)
    score += vwap_ctx.get("vwap_score", 50) * 0.08

    # Zone proximity (14 pts) — CRITICAL for entry validity
    if ez_low and ez_high and close:
        dist_h = (close - ez_high) / ez_high
        if -0.02 <= dist_h <= 0.01:   score += 14  # inside zone
        elif close < ez_low:           score += 10  # below zone, approaching
        elif dist_h <= 0.03:          score += 7   # just above
        elif dist_h <= 0.06:          score += 3   # chasing
        # > 6% above zone: 0 pts (not valid for new entry)

    # Sector rank bonus (6 pts)
    s_rank = sector_rank.get(sector) if sector else None
    if s_rank:
        if s_rank <= 2:   score += 6
        elif s_rank <= 4: score += 4
        elif s_rank <= 6: score += 2

    return round(min(max(score, 0.0), 100.0), 1)


def compute_institutional_score(s: dict, vwap_ctx: dict, vol_trend: dict) -> float:
    """
    0-100: Is smart money involved? Detects quiet institutional accumulation.
    Delivery % is the strongest free signal available for institutional activity.
    """
    score    = 0.0
    delivery = float(s.get("delivery_pct") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    rs       = float(s.get("rs_vs_nifty") or 0)
    value_cr = float(s.get("value_cr") or 0)
    above_50 = bool(s.get("above_sma50"))
    sma_200  = bool(s.get("sma50_gt_200"))

    # Delivery (38 pts) — % of volume that resulted in actual delivery (not squared intraday)
    if delivery >= 72:   score += 38
    elif delivery >= 62: score += 30
    elif delivery >= 55: score += 20
    elif delivery >= 45: score += 12
    elif delivery >= 35: score += 5

    # RS vs Nifty (22 pts) — consistent outperformance = sector rotation into this stock
    if rs > 20:          score += 22
    elif rs > 12:        score += 17
    elif rs > 6:         score += 12
    elif rs > 2:         score += 7
    elif rs > 0:         score += 3

    # Volume trend (18 pts) — sustained high volume = accumulation pattern
    vt = vol_trend.get("volume_trend", "STABLE")
    if vt == "EXPANDING":   score += 18
    elif vt == "STABLE":    score += 10
    elif vt == "CONTRACTING": score += 3

    # Traded value / liquidity (12 pts)
    if value_cr >= 1000:   score += 12
    elif value_cr >= 500:  score += 9
    elif value_cr >= 200:  score += 6
    elif value_cr >= 75:   score += 3

    # VWAP alignment (10 pts) — above VWAP means institutions in profit
    vwap_align = vwap_ctx.get("vwap_alignment", "UNKNOWN")
    if vwap_align == "ABOVE_ALL":    score += 10
    elif vwap_align == "ABOVE_20D":  score += 6
    elif vwap_align == "ABOVE_TODAY_ONLY": score += 3

    return round(min(score, 100.0), 1)


def compute_breakout_readiness(s: dict, bb_ctx: dict, psar_ctx: dict) -> float:
    """
    0-100: How close to a breakout trigger?
    Incorporates BB squeeze (which v1 missed entirely) and PSAR proximity.
    """
    score    = 0.0
    consol   = float(s.get("consol_range") or 999)
    delivery = float(s.get("delivery_pct") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    high_30d = float(s.get("high_30d") or 0)
    close    = float(s.get("close") or s.get("current_price") or 0)
    bk_setup = bool(s.get("breakout_setup"))
    bk_trig  = bool(s.get("bk_trigger"))
    adx      = float(s.get("adx") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)
    rs       = float(s.get("rs_vs_nifty") or 0)

    # Consolidation tightness (22 pts) — the coil
    if consol < 4:    score += 22
    elif consol < 6:  score += 18
    elif consol < 8:  score += 12
    elif consol < 12: score += 6

    # BB squeeze (18 pts) — independent compression measure
    if bb_ctx.get("bb_squeeze"):
        score += 18
        if bb_ctx.get("bb_context") == "SQUEEZE_BULLISH":
            score += 5   # squeeze + near upper band = imminent
    elif bb_ctx.get("bb_width_pct") and float(bb_ctx["bb_width_pct"]) < 8:
        score += 8

    # Proximity to 30d high / resistance (20 pts)
    if high_30d > 0 and close > 0:
        pct_to_high = (high_30d - close) / high_30d * 100
        if pct_to_high < 0.3:    score += 20
        elif pct_to_high < 1.5:  score += 16
        elif pct_to_high < 3.5:  score += 11
        elif pct_to_high < 6:    score += 6
        elif pct_to_high < 10:   score += 3

    # Delivery during consolidation (16 pts) — accumulation while tight
    if delivery >= 65: score += 16
    elif delivery >= 55: score += 12
    elif delivery >= 45: score += 7

    # ADX directional setup (10 pts)
    if adx >= 20 and di_plus > di_minus and (di_plus - di_minus) >= 8:
        score += 10
    elif di_plus > di_minus:
        score += 4

    # Scanner flags (8 pts)
    if bk_trig:   score += 8
    elif bk_setup: score += 5

    # RS bonus (6 pts) — if outperforming while consolidating = institutional holding
    if rs > 8: score += 6
    elif rs > 3: score += 3

    return round(min(score, 100.0), 1)


def compute_risk_score(s: dict, regime_ctx: dict, fund_ctx: dict) -> float:
    """
    0-100: Higher = more risk. Used to penalise final_score.
    Regime-aware: the same ATR is riskier in RISK OFF than in TRENDING.
    """
    risk     = 0.0
    rsi_d    = float(s.get("rsi_daily") or 0)
    atr_pct  = float(s.get("atr_pct") or 0)
    dist_50  = float(s.get("dist_sma50") or 0)
    close    = float(s.get("close") or s.get("current_price") or 0)
    high_52w = float(s.get("high_52w") or 0)
    delivery = float(s.get("delivery_pct") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)
    adx      = float(s.get("adx") or 0)
    ret_1m   = float(s.get("ret_1m") or 0)
    above_50 = bool(s.get("above_sma50"))
    above_st = bool(s.get("above_st"))

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)

    # RSI overextension — relative to regime threshold (25 pts)
    if rsi_d > extended_thresh + 8:  risk += 25
    elif rsi_d > extended_thresh + 3: risk += 16
    elif rsi_d > extended_thresh:    risk += 8

    # Volatility risk — higher ATR = wider adverse moves (18 pts)
    atr_mult = 1.25 if regime_ctx["is_bear"] else 1.0
    atr_adj  = atr_pct * atr_mult
    if atr_adj > 6:     risk += 18
    elif atr_adj > 4.5: risk += 13
    elif atr_adj > 3.5: risk += 8
    elif atr_adj > 2.5: risk += 4

    # Distance above SMA50 — overextension (15 pts)
    if dist_50 > 25:     risk += 15
    elif dist_50 > 18:   risk += 10
    elif dist_50 > 12:   risk += 6
    elif dist_50 > 7:    risk += 3

    # 52w high proximity — crowded trade (12 pts)
    if high_52w > 0 and close > 0:
        pct_from_high = (close / high_52w - 1) * 100
        if pct_from_high > -1.5:    risk += 12
        elif pct_from_high > -4.5:  risk += 7
        elif pct_from_high > -9:    risk += 3

    # Monthly return extreme (10 pts)
    if ret_1m > 30:      risk += 10
    elif ret_1m > 20:    risk += 6
    elif ret_1m > 13:    risk += 3

    # Speculative (low delivery) = retail-driven, stops can cascade (8 pts)
    if delivery < 25:    risk += 8
    elif delivery < 35:  risk += 5
    elif delivery < 42:  risk += 2

    # Structural breakdown signals (7 pts)
    breakdown = sum([not above_50, not above_st, di_minus > di_plus and adx > 18])
    risk += breakdown * 3   # 3 pts each, max 9 but capped at 7

    # Fundamental risk (5 pts)
    risk += fund_ctx.get("fundamental_penalty", 0) * (5 / 12)

    # Regime risk multiplier — everything is riskier in RISK OFF
    if regime_ctx["is_bear"]:   risk = min(risk * 1.2, 100)

    return round(min(risk, 100.0), 1)


def compute_holding_score(s: dict, regime_ctx: dict, lifecycle: str,
                           momentum_state: str, risk_score: float,
                           velocity_state: str, ma_ctx: dict,
                           psar_ctx: dict) -> float:
    """
    HOLDING SCORE — separate from entry validity score.
    For existing positions: should you continue to hold?

    A position you've been in for 2 weeks with +12% P&L and intact trend
    should have HIGH holding score even if it's not an ideal new entry.
    Entry validity says "should I buy this?" 
    Holding score says "should I stay in this?"

    High holding score = trend intact, momentum persisting, no breakdown signals.
    Low holding score = trend weakening, time to prepare exit.
    """
    score = 0.0

    # Lifecycle is the PRIMARY signal (35 pts)
    if lifecycle == "HOLD":       score += 35
    elif lifecycle == "DEVELOPING": score += 18
    elif lifecycle == "EXIT":     score += 0   # hard signal to exit

    # Momentum state (25 pts)
    if momentum_state == "HOT":       score += 25
    elif momentum_state == "BUILDING": score += 18
    elif momentum_state == "STABLE":  score += 10
    elif momentum_state == "WEAK":    score += 0

    # Velocity state (20 pts)
    if velocity_state == "ACCELERATING": score += 20
    elif velocity_state == "FLAT":       score += 12
    elif velocity_state == "DECELERATING": score += 4

    # Structural confirmation (15 pts)
    if psar_ctx.get("dual_trend_confirmed"):
        score += 15
    elif bool(s.get("above_st")):
        score += 8
    if ma_ctx.get("ma_alignment_score", 0) >= 4:
        score += 5   # bonus for strong MA stack

    # Risk penalty (subtract for high risk)
    risk_penalty = (risk_score / 100) * 20   # max -20 pts for risk
    score -= risk_penalty

    # Regime multiplier
    regime_adj = regime_ctx.get("regime_boost", 0) * 0.3   # max ±2.4 pts
    score = score * (0.7 + regime_ctx.get("momentum_decay", 0.70) * 0.3) + regime_adj
    # RISK OFF (decay=0.40): score × 0.82 — compresses range without collapsing it
    # TRENDING (decay=0.85): score × 0.955 — nearly unchanged

    return round(min(max(score, 0.0), 100.0), 1)


def compute_expected_r(s: dict, ez_low: float, ez_high: float) -> float | None:
    """R multiple from ATR-anchored target/SL."""
    atr_14 = float(s.get("atr_14") or 0)
    if not ez_low or not atr_14:
        return None
    ez_mid = (ez_low + (ez_high or ez_low * 1.02)) / 2.0
    target = ez_mid + 3.0 * atr_14
    sl     = ez_low - 1.5 * atr_14
    if sl <= 0 or ez_mid <= sl:
        return None
    return round((target - ez_mid) / (ez_mid - sl), 2)


def compute_days_to_trigger(s: dict, ez_low: float) -> int | None:
    close   = float(s.get("close") or s.get("current_price") or 0)
    atr_pct = float(s.get("atr_pct") or 0)
    if not ez_low or not atr_pct or not close:
        return None
    gap_pct = abs((close - ez_low) / ez_low * 100)
    if gap_pct < 1.0:
        return 0
    daily_prog = atr_pct * 0.28
    return min(int(gap_pct / daily_prog) if daily_prog > 0 else 30, 30)


def compute_final_score(momentum_score: float, validity_score: float,
                         institutional_score: float, breakout_readiness: float,
                         risk_score: float, sector: str, sector_rank: dict,
                         base_score: float, regime_ctx: dict,
                         ma_alignment: int, weekly_structure_score: float,
                         fund_penalty: float) -> float:
    """
    Weighted composite — calibrated for 1-4 week swing trading.

    Weight rationale:
    - Momentum (22%) — is it actually moving? Most important for swing.
    - Validity (20%) — is the entry setup currently actionable?
    - Trend structure (15%) — is the underlying trend intact across all MAs?
    - Institutional (13%) — are the right players involved?
    - Breakout readiness (12%) — how close to the trigger point?
    - Sector quality (8%) — macro tailwind?
    - Weekly structure (6%) — uptrend confirmed on weekly basis?
    - Fundamentals (4%) — quality filter only, not driver.

    Risk penalty: -5 to -18 pts based on risk_score.
    Regime adjustment: +8 to -12 pts based on market regime.
    """
    s_rank = sector_rank.get(sector) if sector else None
    sector_score = (
        100 if s_rank and s_rank <= 2 else
        82  if s_rank and s_rank <= 4 else
        64  if s_rank and s_rank <= 6 else
        46  if s_rank and s_rank <= 8 else
        25
    )

    # MA alignment → 0-100 scale (6 levels → 100 scale)
    ma_score_100 = (ma_alignment / 6) * 100

    base_norm = min(float(base_score or 50), 100)

    weighted = (
        (momentum_score      or 0) * 0.22 +
        (validity_score      or 0) * 0.20 +
        ma_score_100               * 0.15 +
        (institutional_score or 0) * 0.13 +
        (breakout_readiness  or 0) * 0.12 +
        sector_score               * 0.08 +
        (weekly_structure_score or 30) * 0.06 +
        base_norm                  * 0.04
    )

    # Risk penalty — nonlinear (small risk = small penalty, high risk = heavy penalty)
    r = risk_score or 0
    penalty = 0 if r < 35 else (5 if r < 50 else (10 if r < 65 else (15 if r < 80 else 18)))

    # Fundamental penalty (max -6 pts)
    f_penalty = min(float(fund_penalty or 0) * 0.5, 6)

    # Regime adjustment
    regime_adj = regime_ctx.get("regime_boost", 0)
    vix_pen    = regime_ctx.get("vix_penalty", 0)
    breadth_pen = regime_ctx.get("breadth_penalty", 0)

    final = max(0.0, weighted - penalty - f_penalty + regime_adj - vix_pen - breadth_pen)
    return round(min(final, 100.0), 1)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN COMPUTATION LOOP
# ─────────────────────────────────────────────────────────────────────────────

def compute_all(data: dict, today: str) -> list:
    msl_map     = data["msl_map"]
    stock_map   = data["stock_map"]
    history_map = data["history_map"]
    sector_rank = data["sector_rank"]
    open_syms   = data["open_syms"]
    regime      = data["regime"]

    regime_ctx = build_regime_context(regime)
    logger.info(
        f"  Regime context: {regime_ctx['regime']} | "
        f"RSI extended @{regime_ctx['rsi_extended']} | "
        f"regime_boost={regime_ctx['regime_boost']:+d} | "
        f"VIX={regime_ctx['vix']} | breadth={regime_ctx['breadth']:.0f}%"
    )

    results  = []
    skipped  = 0
    no_stock = []

    for sym, msl_row in msl_map.items():
        s = stock_map.get(sym)
        if not s:
            no_stock.append(sym)
            skipped += 1
            continue

        hist     = history_map.get(sym, [])
        sector   = msl_row.get("sector") or s.get("sector") or ""
        strategy = msl_row.get("strategy_source") or ""
        close    = float(s.get("close") or s.get("current_price") or
                         msl_row.get("current_price") or 0)
        adx      = float(s.get("adx") or 0)
        in_pos   = sym in open_syms

        try:
            # ── STEP 1: Independent signal contexts (no dependencies) ──────
            ma_ctx     = compute_ma_alignment(s)
            ha_ctx     = compute_ha_signal(s)
            bb_ctx     = compute_bb_context(s)
            psar_ctx   = compute_psar_context(s)
            macd_ctx   = compute_macd_context(s)
            vwap_ctx   = compute_vwap_context(s)
            vol_trend  = compute_volume_trend(s)
            weekly_ctx = compute_weekly_structure(s)
            fund_ctx   = compute_fundamental_quality(s)
            stoch_ctx  = compute_stoch_context(s, regime_ctx, adx)

            # ── STEP 2: Core labels that depend only on s + regime_ctx ─────
            # These have no dependency on each other
            velocity_state = compute_velocity_state(s, hist)
            momentum_state = compute_momentum_state(s, regime_ctx)
            momentum_phase = compute_momentum_phase(s, hist, regime_ctx)
            trend_maturity = compute_trend_maturity(s, regime_ctx)

            # ── STEP 3: Structural edge — depends on ma_ctx + bb_ctx ───────
            # Must come before entry_timing and lifecycle
            struct_edge  = compute_struct_edge(s, ma_ctx, bb_ctx)
            reentry_mode = compute_reentry_mode(s)

            # ── STEP 4: Entry zone + timing ────────────────────────────────
            # entry_timing depends on reentry_mode + momentum_phase
            ez_low, ez_high = compute_entry_zones(s, strategy, regime_ctx)
            entry_timing    = compute_entry_timing_type(
                s, ez_low, ez_high,
                reentry_mode=reentry_mode,
                momentum_phase=momentum_phase,
            )
            price_loc, dist_fv, dist_entry = compute_price_location(s, ez_low, ez_high)

            # ── STEP 5: Lifecycle — depends on all labels above ────────────
            # Must be last among core labels
            lifecycle = compute_lifecycle(
                s, hist, regime_ctx,
                struct_edge=struct_edge,
                momentum_phase=momentum_phase,
                velocity_state=velocity_state,
                trend_maturity=trend_maturity,
                entry_timing_type=entry_timing,
                in_position=in_pos,
            )

            # ── STEP 6: Scores — depend on labels + contexts ───────────────
            momentum_score      = compute_momentum_score(s, regime_ctx, macd_ctx, stoch_ctx, ha_ctx, weekly_ctx)
            validity_score      = compute_validity_score(s, sector, sector_rank, ez_low, ez_high, ma_ctx, psar_ctx, vwap_ctx, regime_ctx)
            institutional_score = compute_institutional_score(s, vwap_ctx, vol_trend)
            breakout_readiness  = compute_breakout_readiness(s, bb_ctx, psar_ctx)
            risk_score          = compute_risk_score(s, regime_ctx, fund_ctx)
            expected_r          = compute_expected_r(s, ez_low, ez_high)
            days_trigger        = compute_days_to_trigger(s, ez_low)

            # holding_score depends on lifecycle + momentum_state + risk_score
            holding_score = compute_holding_score(
                s, regime_ctx, lifecycle, momentum_state,
                risk_score, velocity_state, ma_ctx, psar_ctx
            )

            # ── STEP 7: Final score + history context ──────────────────────
            base_score  = float(msl_row.get("base_score") or msl_row.get("final_score") or 50)
            final_score = compute_final_score(
                momentum_score, validity_score, institutional_score,
                breakout_readiness, risk_score, sector, sector_rank,
                base_score, regime_ctx, ma_ctx["ma_alignment_score"],
                weekly_ctx["weekly_structure_score"], fund_ctx["fundamental_penalty"]
            )
            hist_ctx = compute_msl_history_context(sym, hist)

            results.append({
                "date":   today,
                "symbol": sym,
                # ── Core labels ──
                "momentum_state":       momentum_state,
                "momentum_phase":       momentum_phase,
                "velocity_state":       velocity_state,
                "trend_maturity":       trend_maturity,
                "lifecycle":            lifecycle,
                "struct_edge":          struct_edge,
                "reentry_mode":         reentry_mode,
                # ── Entry zone + timing ──
                "entry_zone_low":       ez_low,
                "entry_zone_high":      ez_high,
                "entry_timing_type":    entry_timing,
                "price_location":       price_loc,
                "dist_fv_pct":          dist_fv,
                "dist_entry_pct":       dist_entry,
                # ── Scores ──
                "final_score":          final_score,
                "base_score":           base_score,
                "validity_score":       validity_score,
                "expected_r":           expected_r,
                "in_position":          in_pos,
                # ── History ──
                "days_in_list":         hist_ctx["days_in_list"],
                "rank_vel_3d":          hist_ctx["rank_vel_3d"],
                "score_vel_5d":         hist_ctx["score_vel_5d"],
                # ── Composite scores ──
                "momentum_score":       momentum_score,
                "institutional_score":  institutional_score,
                "breakout_readiness":   breakout_readiness,
                "risk_score":           risk_score,
                "holding_score":        holding_score,
                "days_to_trigger_est":  days_trigger,
                # ── Signal contexts ──
                "ma_alignment_score":   ma_ctx["ma_alignment_score"],
                "ha_signal":            ha_ctx["ha_signal"],
                "bb_squeeze":           bb_ctx["bb_squeeze"],
                "bb_position_pct":      bb_ctx["bb_position_pct"],
                "bb_context":           bb_ctx["bb_context"],
                "bb_width_pct":         bb_ctx["bb_width_pct"],
                "macd_direction":       macd_ctx["macd_direction"],
                "psar_dual_confirmed":  psar_ctx["dual_trend_confirmed"],
                "st_cushion_pct":       psar_ctx["st_cushion_pct"],
                "vwap_alignment":       vwap_ctx["vwap_alignment"],
                "volume_trend":         vol_trend["volume_trend"],
                "weekly_structure":     weekly_ctx["weekly_structure"],
                "fundamental_quality":  fund_ctx["fundamental_quality"],
                "stoch_context":        stoch_ctx["stoch_context"],
                "active_regime":        regime_ctx["regime"],
                "rsi_extended_thresh":  get_rsi_extended_threshold(regime_ctx, adx),
                "compute_source":       "compute_msl_v2",
                "computed_at":          datetime.now(IST).isoformat(),
            })

        except Exception as e:
            logger.warning(f"  {sym}: compute failed — {e}")
            skipped += 1

    if no_stock:
        logger.warning(f"  {len(no_stock)} missing from stock_data_daily: {no_stock}")
    logger.info(f"  Computed: {len(results)} | skipped: {skipped}")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# WRITE STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

def write_results(sb, results: list, mode: str, today: str):
    if not results:
        logger.warning("No results to write")
        return
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(results)} rows | mode={mode}")
        top = sorted(results, key=lambda x: x.get("final_score", 0), reverse=True)[:3]
        for r in top:
            logger.info(
                f"  {r['symbol']}: final={r['final_score']} mom={r['momentum_state']} "
                f"phase={r['momentum_phase']} MA={r['ma_alignment_score']}/6 "
                f"BB={r['bb_context']} struct={r['struct_edge']} "
                f"hold={r['holding_score']}"
            )
        return

    if mode == "shadow":
        write_rows = [{k: v for k, v in r.items() if k != "base_rank"} for r in results]
        for i in range(0, len(write_rows), 50):
            sb.table("msl_computed").upsert(write_rows[i:i+50], on_conflict="date,symbol").execute()
        logger.success(f"✓ shadow: {len(results)} → msl_computed")

    elif mode in ("hybrid", "full"):
        current_rows = sb.table("master_shortlist").select("*").eq("date", today).execute().data
        current_map  = {r["symbol"]: r for r in (current_rows or [])}
        upsert_rows  = []
        for r in results:
            sym  = r["symbol"]
            base = dict(current_map.get(sym, {}))
            for field, val in r.items():
                if field not in PRESERVE_FIELDS:
                    base[field] = val
            base["date"]   = today
            base["symbol"] = sym
            base.pop("compute_source", None)
            base.pop("computed_at", None)
            upsert_rows.append(base)
        for i in range(0, len(upsert_rows), 50):
            sb.table("master_shortlist").upsert(upsert_rows[i:i+50], on_conflict="date,symbol").execute()
        logger.success(f"✓ {mode}: {len(upsert_rows)} → master_shortlist")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_msl skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())
    mode  = cfg("compute_msl_mode", "shadow")

    # CLI override
    if os.getenv("COMPUTE_MSL_MODE_OVERRIDE"):
        mode = os.getenv("COMPUTE_MSL_MODE_OVERRIDE")

    logger.info("=" * 65)
    logger.info(f"STEP: Compute MSL v2.0 | {today} | mode={mode.upper()}"
                + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 65)

    logger.info("Pass 1: loading data...")
    data = load_data(sb, today, mode)
    if not data or not data.get("msl_map"):
        logger.error("No data loaded — aborting")
        return {"status": "no_data"}

    logger.info("Pass 2: computing all fields...")
    results = compute_all(data, today)
    if not results:
        logger.error("No results computed")
        return {"status": "no_results"}

    # ── Diagnostic summary ────────────────────────────────────────────────────
    n = len(results)
    def avg(field): return round(sum(r.get(field,0) or 0 for r in results) / n, 1)

    logger.info(f"  Results: {n} symbols")
    logger.info(f"  Avg final_score:         {avg('final_score')}")
    logger.info(f"  Avg momentum_score:      {avg('momentum_score')}")
    logger.info(f"  Avg institutional_score: {avg('institutional_score')}")
    logger.info(f"  Avg breakout_readiness:  {avg('breakout_readiness')}")
    logger.info(f"  Avg risk_score:          {avg('risk_score')}")
    logger.info(f"  Avg holding_score:       {avg('holding_score')}")
    logger.info(f"  Avg ma_alignment:        {avg('ma_alignment_score')}/6")

    logger.info(f"  Momentum state:    {dict(Counter(r.get('momentum_state') for r in results))}")
    logger.info(f"  Momentum phase:    {dict(Counter(r.get('momentum_phase') for r in results))}")
    logger.info(f"  Lifecycle:         {dict(Counter(r.get('lifecycle') for r in results))}")
    logger.info(f"  Weekly structure:  {dict(Counter(r.get('weekly_structure') for r in results))}")
    logger.info(f"  BB context:        {dict(Counter(r.get('bb_context') for r in results))}")
    logger.info(f"  VWAP alignment:    {dict(Counter(r.get('vwap_alignment') for r in results))}")
    logger.info(f"  Volume trend:      {dict(Counter(r.get('volume_trend') for r in results))}")
    logger.info(f"  Fund quality:      {dict(Counter(r.get('fundamental_quality') for r in results))}")

    # Top setups
    top = sorted(results, key=lambda x: x.get("final_score", 0), reverse=True)[:5]
    logger.info("  Top 5 by final_score:")
    for r in top:
        in_p = "📂" if r.get("in_position") else "  "
        logger.info(
            f"  {in_p} {r['symbol']:<12} final={r['final_score']:>5}  "
            f"hold={r['holding_score']:>5}  mom={r['momentum_state']:<8} "
            f"phase={r['momentum_phase']:<10} MA={r['ma_alignment_score']}/6  "
            f"BB={r.get('bb_context','?'):<20} risk={r['risk_score']:>4}"
        )

# Sheet vs computed divergence — independent master_shortlist query, works in all modes
    # Sheet vs computed divergence — independent master_shortlist query, works in all modes
    sheet_rows = sb.table("master_shortlist").select(
        "symbol,momentum_state,final_score,base_rank"
    ).eq("date", today).execute().data

    if not sheet_rows:
        latest = sb.table("master_shortlist").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            fb = latest[0]["date"]
            sheet_rows = sb.table("master_shortlist").select(
                "symbol,momentum_state,final_score,base_rank"
            ).eq("date", fb).execute().data
            logger.warning(f"  Sheet comparison: falling back to {fb}")

    sheet_map = {r["symbol"]: r for r in (sheet_rows or [])}

    if sheet_map:
        diffs = []
        for r in results:
            mrow = sheet_map.get(r["symbol"], {})
            if not mrow:
                continue
            s_mom = mrow.get("momentum_state") or ""
            c_mom = r["momentum_state"]
            s_scr = float(mrow.get("final_score") or 0)
            c_scr = r["final_score"]
            base_rank = int(mrow.get("base_rank") or 9999)
            if s_mom != c_mom or abs(s_scr - c_scr) > 12:
                diffs.append((
                    base_rank,
                    f"    #{base_rank} {r['symbol']}: "
                    f"mom {s_mom or 'None'}→{c_mom}  "
                    f"score {s_scr:.0f}→{c_scr}"
                ))

        # Symbols in Sheet but missing from compute_msl entirely
        computed_symbols = {r["symbol"] for r in results}
        for sym, mrow in sheet_map.items():
            if sym not in computed_symbols:
                base_rank = int(mrow.get("base_rank") or 9999)
                diffs.append((
                    base_rank,
                    f"    #{base_rank} {sym}: NOT COMPUTED — "
                    f"sheet has mom={mrow.get('momentum_state') or 'None'}  "
                    f"score={float(mrow.get('final_score') or 0):.0f}"
                ))

        if diffs:
            diffs.sort(key=lambda x: x[0])
            logger.info(f"  Sheet vs computed divergences ({len(diffs)} symbols), top 40 by rank:")
            for _, d in diffs[:40]:
                logger.info(d)
        else:
            logger.info("  ✅ Computed values closely match Sheet")
    else:
        logger.warning("  Sheet comparison skipped — master_shortlist empty or unavailable")
            
    # Sort best → worst and assign rank so msl_computed reflects priority order
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    for rank, r in enumerate(results, 1):
        r["base_rank"] = rank

    logger.info("Pass 3: writing results...")
    write_results(sb, results, mode, today)

    hot_in_zone = [
        r for r in results
        if r.get("momentum_state") == "HOT"
        and r.get("entry_timing_type") in ("OPTIMAL", "REENTRY")
        and not r.get("in_position")
    ]
    if hot_in_zone:
        logger.info(f"  ★ HOT + in zone (new opportunities): {[r['symbol'] for r in hot_in_zone]}")

    held_weakening = [
        r for r in results
        if r.get("in_position")
        and r.get("holding_score", 100) < 35
    ]
    if held_weakening:
        logger.warning(f"  ⚠ Held positions with weakening score: {[r['symbol'] for r in held_weakening]}")

    return {
        "status": "ok", "computed": n,
        "mode": mode,
        "avg_score": avg("final_score"),
        "hot_in_zone": len(hot_in_zone),
        "held_weakening": len(held_weakening),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["shadow", "hybrid", "full"])
    args = parser.parse_args()
    if args.dry_run:  os.environ["DRY_RUN"] = "True"
    if args.mode:     os.environ["COMPUTE_MSL_MODE_OVERRIDE"] = args.mode
    print(main())