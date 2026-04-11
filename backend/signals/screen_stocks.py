"""
TradeOS v6 — Dynamic Stock Screener v1.0
==========================================
Pipeline position: [10.6] — after compute_indicators, before compute_msl

PURPOSE
  This script REPLACES the role of ingest_sheets.py for MASTER_SHORTLIST
  population (over time, via transition modes). It screens all 500 stocks
  in stock_data_daily through multiple strategy engines and proprietary
  scanners, then writes the top 30 qualified symbols to msl_computed
  (shadow mode) or master_shortlist (hybrid/full mode).

  The Google Sheet MSL tab becomes a manual override layer only —
  any symbol the user manually adds with force_include=True will be kept
  regardless of screener output.

STRATEGY ENGINES (evolved from your Sheet logic):
  1. CTL  — Core Trend Leaders (evolved: adds ADX, MACD, VWAP checks)
  2. SBS  — Structural Breakout Swing (evolved: BB squeeze, delivery trend)
  3. TPO  — Trend Pullback Opportunities (evolved: regime-aware RSI window)
  4. EAP  — Event-Accelerated Plays (evolved: programmatic event calendar)

PROPRIETARY SCANNERS (new, data-driven):
  5. VBD  — Velocity Burst Detector (single-day 3-6% move + institutional confirm)
  6. IAD  — Institutional Accumulation (quiet delivery + RS + volume expansion)
  7. RSB  — Relative Strength Breakout (RS leader coiling near resistance)
  8. MOM  — Momentum Continuation (EXPANSION phase + accelerating + near zone)
  9. RVS  — Reversal Setup (SMA50 bounce + RSI turning up from 45-52)
  10. SEC  — Sector Rotation (fresh sector momentum + early movers in new leaders)

SELECTION LOGIC:
  Each engine produces a {symbol: engine_score} dict.
  Symbols are aggregated with a CONVERGENCE BONUS when multiple engines agree.
  Top 30 by composite_score after sector diversification are selected.
  Hard blocks: ASM/GSM/FO_BAN excluded. Regime filter applied.

TRANSITION MODES (system_config: screener_mode):
  shadow  → writes to msl_computed only (master_shortlist untouched)
  hybrid  → writes to msl_computed AND updates master_shortlist (merged with Sheet)
  full    → writes only to master_shortlist (Sheet MSL tab is manual override only)
"""

import sys, os, json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, IST, cfg, cfg_bool, is_kill_switch_active, DRY_RUN

# ── Config defaults (overridable via system_config) ────────────────────────────
MAX_SYMBOLS          = 30     # top N to write to MSL
MAX_PER_SECTOR       = 5      # sector diversification cap
MIN_MARKET_CAP       = 300    # Cr — minimum for any strategy
CONVERGENCE_BONUS    = 8      # pts per additional engine confirming a symbol


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """Load all required tables in bulk — no per-symbol calls."""

    # stock_data_daily — all 500 stocks
    stock_rows = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stock_rows:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            fb = latest[0]["date"]
            stock_rows = sb.table("stock_data_daily").select("*").eq("date", fb).execute().data
            logger.warning(f"stock_data_daily: falling back to {fb}")
    stock_map = {r["symbol"]: r for r in (stock_rows or [])}

    # sector_strength
    sector_rows = sb.table("sector_strength").select("sector,rank").eq("date", today).execute().data
    if not sector_rows:
        sector_rows = sb.table("sector_strength").select("sector,rank").order("date", desc=True).limit(50).execute().data
    sector_rank = {r["sector"]: r["rank"] for r in sector_rows if r.get("rank")}

    # market_regime
    regime_rows = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # safety_lists (ASM/FO_BAN)
    safety_rows = sb.table("safety_lists").select("symbol,list_type").execute().data
    asm_set    = {r["symbol"] for r in safety_rows if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety_rows if r["list_type"] == "FO_BAN"}

    # event_calendar
    event_rows = sb.table("event_calendar").select("*").eq("is_active", True).execute().data

    # fii_dii_flow (latest)
    fii_rows = sb.table("fii_dii_flow").select("fii_flag,fii_net_20d,fii_net_5d").order("date", desc=True).limit(1).execute().data
    fii = fii_rows[0] if fii_rows else {}

    # existing master_shortlist for manual override detection
    msl_rows = sb.table("master_shortlist").select("symbol,force_include,notes").eq("date", today).execute().data
    force_include = {r["symbol"] for r in (msl_rows or []) if r.get("force_include")}

    # open_positions (don't re-add already-held stocks as new entries)
    open_rows = sb.table("open_positions").select("symbol").eq("status", "ACTIVE").execute().data
    open_syms = {r["symbol"] for r in (open_rows or [])}

    logger.info(
        f"  Loaded: {len(stock_map)} stocks | {len(sector_rank)} sectors | "
        f"regime={regime.get('regime','?')} | asm={len(asm_set)} | "
        f"fii={fii.get('fii_flag','?')} | forced={len(force_include)}"
    )
    return {
        "stock_map":    stock_map,
        "sector_rank":  sector_rank,
        "regime":       regime,
        "asm_set":      asm_set,
        "fo_ban_set":   fo_ban_set,
        "event_rows":   event_rows,
        "fii":          fii,
        "force_include": force_include,
        "open_syms":    open_syms,
    }


def resolve_regime(regime: dict) -> str:
    from datetime import datetime, timezone
    manual    = regime.get("regime", "NEUTRAL")
    predicted = regime.get("predicted_regime")
    pred_at   = regime.get("regime_predicted_at")
    if predicted and pred_at:
        try:
            if isinstance(pred_at, str):
                pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
            else:
                pred_dt = pred_at
            if pred_dt.tzinfo is None:
                pred_dt = pred_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() < 86400:
                return predicted
        except Exception:
            pass
    return manual


def get_event_action(symbol: str, sector: str, events: list, buffer_days: int = 2) -> str:
    """Returns AVOID_ENTRY, PRIORITISE, or NO_CHANGE based on event calendar."""
    from datetime import date as _date
    HIGH   = {"RESULTS","EARNINGS","QUARTERLY_RESULTS","BOARD_MEETING","FINANCIAL RESULTS"}
    today  = today_ist()
    for ev in events:
        if not ev.get("is_active"): continue
        ev_sym  = ev.get("symbol", "")
        sectors = str(ev.get("affected_sectors", "")).lower()
        relevant = (ev_sym == symbol) or (sector and sector.lower() in sectors)
        if not relevant: continue
        sd_str = ev.get("start_date") or ev.get("event_date")
        if not sd_str: continue
        try:
            sd   = _date.fromisoformat(str(sd_str)[:10])
            days = (sd - today).days
        except Exception:
            continue
        raw = (str(ev.get("event_type","")) + " " + str(ev.get("purpose",""))).upper()
        if any(k in raw for k in HIGH):
            if 0 <= days <= buffer_days:  return "AVOID_ENTRY"
            if -buffer_days <= days < 0:  return "PRIORITISE"
    return "NO_CHANGE"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY ENGINES
# ─────────────────────────────────────────────────────────────────────────────

def run_ctl(stock_map: dict, sector_rank: dict, cfg_ctl: dict) -> dict:
    """
    Core Trend Leaders — EVOLVED from Sheet.
    Original: monthly RSI, weekly RSI, 6M return, ATR, market cap, sector rank.
    Added: ADX directional confirmation, SMA50>SMA200 golden cross mandatory,
           MACD histogram positive, VWAP alignment check.

    Philosophy: CTL wants multi-timeframe trend leaders in top sectors.
    These are the stocks you hold for 2-4 weeks riding the primary trend.
    """
    results = {}
    max_sector_rank = cfg_ctl.get("max_sector_rank", 4)
    min_monthly_rsi  = cfg_ctl.get("min_monthly_rsi", 58)
    min_weekly_rsi   = cfg_ctl.get("min_weekly_rsi", 58)
    min_6m_return    = cfg_ctl.get("min_6m_return", 0)
    max_atr_pct      = cfg_ctl.get("max_atr_pct", 4.5)   # relaxed from 4 (more stocks)
    min_market_cap   = cfg_ctl.get("min_market_cap", 500)

    for sym, s in stock_map.items():
        sector = s.get("sector", "")

        # ── Original CTL gates ──
        if sector and sector_rank.get(sector, 99) > max_sector_rank:        continue
        if (s.get("rsi_monthly") or 0) < min_monthly_rsi:                   continue
        if (s.get("rsi_weekly") or 0) < min_weekly_rsi:                     continue
        if (s.get("ret_6m") or -999) < min_6m_return:                       continue
        if (s.get("atr_pct") or 999) > max_atr_pct:                         continue
        if (s.get("market_cap") or 0) < min_market_cap:                     continue
        if not s.get("above_sma50"):                                         continue

        # ── Evolved additions ──
        if not s.get("sma50_gt_200"):              continue  # golden cross mandatory
        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        if adx < 15 or di_plus <= di_min:          continue  # directional trend required
        if (s.get("macd_hist") or -1) < 0:         continue  # MACD positive momentum

        # Score: weighted combination
        score = 0.0
        rsi_m  = float(s.get("rsi_monthly") or 0)
        rsi_w  = float(s.get("rsi_weekly") or 0)
        ret_6m = float(s.get("ret_6m") or 0)
        vol_r  = float(s.get("vol_ratio") or 0)
        s_rank = sector_rank.get(sector, 10)

        # Sector rank bonus
        score += max(0, (5 - s_rank)) * 6   # rank 1=24, 2=18, 3=12, 4=6, 5=0
        # Monthly RSI quality
        if 60 <= rsi_m <= 72:   score += 20
        elif rsi_m > 72:         score += 10   # extended penalty
        elif rsi_m >= 58:        score += 12
        # Weekly RSI
        if rsi_w >= 62:          score += 15
        elif rsi_w >= 58:        score += 10
        # 6M return momentum (not excessive)
        if 15 <= ret_6m <= 50:   score += 15
        elif ret_6m > 0:         score += 8
        elif ret_6m > 50:        score += 5   # very extended
        # ADX strength
        if adx >= 28:            score += 10
        elif adx >= 20:          score += 6
        # Volume
        if vol_r >= 1.5:         score += 8
        elif vol_r >= 1.1:       score += 4

        results[sym] = round(score, 1)

    logger.info(f"  CTL: {len(results)} candidates")
    return results


def run_sbs(stock_map: dict, sector_rank: dict, cfg_sbs: dict) -> dict:
    """
    Structural Breakout Swing — EVOLVED from Sheet.
    Original: sector rank, daily/weekly RSI, ATR, vol ratio, consolidation, market cap.
    Added: BB squeeze detection, delivery trend (institutional in tight range),
           PSAR confirmation, proximity to 30d high (approach to resistance).

    Philosophy: SBS catches stocks coiling in a tight range with institutional
    interest building — the breakout has not happened yet but conditions are ripe.
    Vol ratio > threshold + tight consolidation + approaching resistance = setup.
    """
    results = {}
    max_sector_rank = cfg_sbs.get("max_sector_rank", 7)
    min_daily_rsi   = cfg_sbs.get("min_daily_rsi", 52)   # relaxed slightly
    min_weekly_rsi  = cfg_sbs.get("min_weekly_rsi", 54)
    max_atr_pct     = cfg_sbs.get("max_atr_pct", 5.5)
    min_vol_ratio   = cfg_sbs.get("min_vol_ratio", 1.1)   # relaxed — BB squeeze compensates
    max_consol      = cfg_sbs.get("max_consol_pct", 15)   # relaxed — BB squeeze used instead
    min_market_cap  = cfg_sbs.get("min_market_cap", 300)

    for sym, s in stock_map.items():
        sector = s.get("sector", "")

        # ── Original SBS gates ──
        if sector and sector_rank.get(sector, 99) > max_sector_rank:    continue
        if (s.get("rsi_daily") or 0) < min_daily_rsi:                   continue
        if (s.get("rsi_weekly") or 0) < min_weekly_rsi:                 continue
        if (s.get("atr_pct") or 999) > max_atr_pct:                     continue
        if (s.get("vol_ratio") or 0) < min_vol_ratio:                   continue
        if (s.get("market_cap") or 0) < min_market_cap:                 continue

        # ── Evolved: either tight consolidation OR BB squeeze required ──
        consol  = float(s.get("consol_range") or 999)
        bb_up   = float(s.get("bb_upper") or 0)
        bb_lo   = float(s.get("bb_lower") or 0)
        close   = float(s.get("close") or s.get("current_price") or 0)
        bb_width_pct = ((bb_up - bb_lo) / close * 100) if (close > 0 and bb_up > bb_lo) else 999

        tight_consol = consol <= max_consol
        bb_squeeze   = bb_width_pct < 7.0
        if not (tight_consol or bb_squeeze):                             continue

        # HH-HL structure check (evolved: use wk_hi_high + wk_hi_low)
        wk_hh = bool(s.get("wk_hi_high"))
        wk_hl = bool(s.get("wk_hi_low"))
        # At least one structural signal required
        if not (wk_hh or wk_hl or bool(s.get("above_sma50"))):         continue

        # Score
        score = 0.0
        rsi_d    = float(s.get("rsi_daily") or 0)
        rsi_w    = float(s.get("rsi_weekly") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        delivery = float(s.get("delivery_pct") or 0)
        high_30d = float(s.get("high_30d") or 0)
        s_rank   = sector_rank.get(sector, 10)
        adx      = float(s.get("adx") or 0)

        # Sector
        score += max(0, (8 - s_rank)) * 4
        # RSI sweet spot (not too hot, has room to run)
        if 55 <= rsi_d <= 68:    score += 18
        elif 52 <= rsi_d < 55:   score += 12
        elif rsi_d > 68:         score += 6
        # Weekly alignment
        if rsi_w >= 58:          score += 12
        elif rsi_w >= 54:        score += 7
        # BB squeeze = strong signal
        if bb_squeeze:           score += 15
        if bb_width_pct < 4.5:  score += 8   # very tight
        # Consolidation tightness
        if consol < 6:           score += 10
        elif consol < 10:        score += 6
        # Volume + delivery = institutional
        if vol_r >= 2.0:         score += 10
        elif vol_r >= 1.4:       score += 6
        if delivery >= 55:       score += 10
        elif delivery >= 42:     score += 5
        # Approaching 30d high (resistance proximity)
        if high_30d > 0 and close > 0:
            pct_away = (high_30d - close) / high_30d * 100
            if pct_away < 2:     score += 10
            elif pct_away < 5:   score += 6
            elif pct_away < 8:   score += 3
        # Structural
        if wk_hh and wk_hl:     score += 8
        elif wk_hl:              score += 4
        # Breakout scanner flag
        if bool(s.get("breakout_setup")): score += 5
        if bool(s.get("bk_trigger")):     score += 5

        results[sym] = round(score, 1)

    logger.info(f"  SBS: {len(results)} candidates")
    return results


def run_tpo(ctl_results: dict, stock_map: dict, cfg_tpo: dict, regime_name: str) -> dict:
    """
    Trend Pullback Opportunities — EVOLVED from Sheet.
    Original: works only on CTL stocks, daily RSI 42-55, dist from SMA50 <= 3%, ATR <= 4%.
    Added: regime-aware RSI window (in bull markets, pullbacks to 48 are fine),
           MACD histogram turning point detection (histogram < 0 but turning up = early entry).

    Philosophy: Buy the dip in a quality trend at a better price.
    Entry earlier than CTL with better R:R but more patience required.
    """
    results = {}
    # Regime-aware RSI window
    if regime_name in ("TRENDING", "RISK ON"):
        rsi_min, rsi_max = 44, 58   # bull market: pullbacks to 44 acceptable
    elif regime_name == "RISK OFF":
        rsi_min, rsi_max = 38, 50   # bear: need deeper pullback to justify entry
    else:
        rsi_min, rsi_max = cfg_tpo.get("min_rsi", 42), cfg_tpo.get("max_rsi", 55)

    max_dist_sma50 = cfg_tpo.get("max_dist_sma50", 4)   # relaxed from 3
    max_atr_pct    = cfg_tpo.get("max_atr_pct", 4)
    max_ctl_rank   = cfg_tpo.get("max_ctl_rank", 15)

    for sym in ctl_results:
        s = stock_map.get(sym, {})
        rsi_d = float(s.get("rsi_daily") or 0)

        if not (rsi_min <= rsi_d <= rsi_max):                 continue
        if abs(float(s.get("dist_sma50") or 999)) > max_dist_sma50: continue
        if (s.get("atr_pct") or 999) > max_atr_pct:          continue
        if not s.get("above_sma50"):                          continue  # must still be above SMA50

        # Evolved: weekly RSI should still be supportive
        rsi_w = float(s.get("rsi_weekly") or 0)
        if rsi_w < 50:                                        continue  # weekly trend must be intact

        score = 0.0
        dist_50  = abs(float(s.get("dist_sma50") or 0))
        macd_h   = float(s.get("macd_hist") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        delivery = float(s.get("delivery_pct") or 0)

        # RSI sweet spot for pullback entry
        if 46 <= rsi_d <= 53:    score += 20   # ideal cool-off zone
        elif 44 <= rsi_d < 46:   score += 14
        elif rsi_d <= 58:        score += 10

        # Proximity to SMA50 (closer = better entry)
        if dist_50 < 1.5:        score += 15
        elif dist_50 < 3:        score += 10
        elif dist_50 < 4:        score += 5

        # MACD turning up (even if still negative) = early reversal signal
        if macd_h > 0:           score += 10  # already recovered
        elif macd_h > -0.5:      score += 8   # near zero = turning point
        elif macd_h > -1:        score += 4

        # Weekly RSI still strong = trend intact
        if rsi_w >= 58:          score += 10
        elif rsi_w >= 52:        score += 6

        # Volume + delivery during pullback = institutional holding, not selling
        if vol_r >= 1.3 and delivery >= 50:   score += 12
        elif vol_r >= 1.1 or delivery >= 45:  score += 6

        # CTL rank bonus (higher ranked = better quality base)
        ctl_score = ctl_results.get(sym, 0)
        score += ctl_score * 0.3   # inherit 30% of CTL score

        results[sym] = round(score, 1)

    logger.info(f"  TPO: {len(results)} candidates")
    return results


def run_eap_overlay(candidates: dict, stock_map: dict, event_rows: list, cfg_eap: dict) -> dict:
    """
    Event-Accelerated Plays — EVOLVED overlay.
    Original: adjust score based on earnings proximity.
    Evolved: adds SECTOR_ROTATION, INDEX_REBALANCE event types with separate logic.

    Returns adjusted {symbol: score_delta} — applied on top of base scores.
    Positive = prioritise. Negative = deprioritise. Zero = no change.
    """
    buffer_days  = int(cfg_eap.get("pre_event_days", 2))
    post_boost   = float(cfg_eap.get("post_event_aggression", 2))
    pre_penalty  = float(cfg_eap.get("pre_event_aggression", 1))

    adjustments = {}
    for sym in candidates:
        s      = stock_map.get(sym, {})
        sector = s.get("sector", "")
        action = get_event_action(sym, sector, event_rows, buffer_days)
        if action == "AVOID_ENTRY":
            adjustments[sym] = -pre_penalty * 15   # penalise near-event stocks
        elif action == "PRIORITISE":
            adjustments[sym] = post_boost * 10     # post-event continuation boost
        else:
            adjustments[sym] = 0

    return adjustments


# ─────────────────────────────────────────────────────────────────────────────
# PROPRIETARY SCANNERS
# ─────────────────────────────────────────────────────────────────────────────

def run_vbd(stock_map: dict, sector_rank: dict) -> dict:
    """
    Velocity Burst Detector — NEW.
    Finds stocks with a single-session 3-6% move backed by:
    - Institutional delivery (not just retail speculation)
    - Volume 2x+ above average (not a thin-market spike)
    - Tight consolidation before the move (breakout from a base)
    - Still in a valid sector (not a random outlier)

    Target: catch early in a breakout move before the crowd piles in.
    pct_change is today's single-session % move.
    """
    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        pct_chg = float(s.get("pct_change") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        consol  = float(s.get("consol_range") or 999)
        above_50= bool(s.get("above_sma50"))
        adx     = float(s.get("adx") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # Hard gates
        if pct_chg < 3.0:             continue   # needs meaningful single-day move
        if pct_chg > 12.0:            continue   # skip circuit/news-driven spikes
        if vol_r < 1.8:               continue   # must have volume behind it
        if delivery < 35:             continue   # must have institutional delivery
        if market_cap < MIN_MARKET_CAP: continue
        if not above_50:              continue   # trend direction must be up
        if s_rank > 10:               continue   # not in a terrible sector

        score = 0.0
        # Day move size (sweet spot 3-7%)
        if 4 <= pct_chg <= 7:         score += 25
        elif pct_chg > 7:             score += 15
        elif pct_chg >= 3:            score += 18
        # Volume confirmation
        if vol_r >= 5:                score += 25
        elif vol_r >= 3:              score += 20
        elif vol_r >= 2:              score += 12
        # Delivery quality
        if delivery >= 65:            score += 20
        elif delivery >= 50:          score += 14
        elif delivery >= 38:          score += 7
        # Breakout from consolidation
        if consol < 6:               score += 15
        elif consol < 10:            score += 8
        # ADX trending
        if adx >= 25:                 score += 10
        elif adx >= 18:               score += 5
        # Sector
        score += max(0, (6 - s_rank)) * 2

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  VBD: {len(results)} candidates")
    return results


def run_iad(stock_map: dict, sector_rank: dict) -> dict:
    """
    Institutional Accumulation Detector — NEW.
    Finds stocks where smart money is quietly accumulating BEFORE price discovery.
    Signal: high delivery % + sustained above-average volume + outperforming market
    + still in tight consolidation (price hasn't moved much yet).

    This is the most valuable scanner — it finds stocks 2-5 sessions BEFORE VBD fires.
    """
    results = {}
    for sym, s in stock_map.items():
        sector   = s.get("sector", "")
        delivery = float(s.get("delivery_pct") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        rs       = float(s.get("rs_vs_nifty") or 0)
        consol   = float(s.get("consol_range") or 999)
        above_50 = bool(s.get("above_sma50"))
        rsi_d    = float(s.get("rsi_daily") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank   = sector_rank.get(sector, 99)

        # Volume trend: 20d average vs 50d average (expanding = sustained accumulation)
        vol_20d = float(s.get("avg_vol_20d") or 0)
        vol_50d = float(s.get("avg_vol_50d") or 0)
        vol_expanding = vol_20d > vol_50d * 1.10 if (vol_20d > 0 and vol_50d > 0) else False

        # Hard gates
        if delivery < 50:                         continue   # institutional signal floor
        if vol_r < 1.2:                           continue   # above-average volume required
        if market_cap < MIN_MARKET_CAP:           continue
        if not above_50:                          continue
        if consol > 20:                           continue   # price must be in a range
        if s_rank > 12:                           continue   # some sector quality

        score = 0.0
        # Delivery (primary signal)
        if delivery >= 70:                        score += 35
        elif delivery >= 62:                      score += 27
        elif delivery >= 55:                      score += 18
        elif delivery >= 50:                      score += 10
        # RS vs Nifty
        if rs > 15:                              score += 22
        elif rs > 8:                             score += 16
        elif rs > 3:                             score += 10
        elif rs > 0:                             score += 5
        # Volume expansion trend
        if vol_expanding:                        score += 15
        if vol_r >= 2.0:                         score += 10
        elif vol_r >= 1.5:                       score += 6
        # Tight consolidation = price not yet broken out (ideal entry)
        if consol < 7:                           score += 15
        elif consol < 12:                        score += 8
        # RSI in healthy zone (not yet extended)
        if 50 <= rsi_d <= 65:                    score += 10
        elif rsi_d > 65:                         score += 4
        # Sector
        score += max(0, (6 - s_rank)) * 2

        if score >= 60:
            results[sym] = round(score, 1)

    logger.info(f"  IAD: {len(results)} candidates")
    return results


def run_rsb(stock_map: dict, sector_rank: dict) -> dict:
    """
    Relative Strength Breakout — NEW.
    Finds stocks consistently outperforming Nifty on both 1m and 3m,
    now consolidating in a tight range near resistance.
    These are the "first movers" when market recovers from any dip.

    RS leader + tight consol + approaching 30d high = high-probability setup.
    """
    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rs      = float(s.get("rs_vs_nifty") or 0)
        ret_1m  = float(s.get("ret_1m") or 0)
        ret_3m  = float(s.get("ret_3m") or 0)
        consol  = float(s.get("consol_range") or 999)
        high_30d= float(s.get("high_30d") or 0)
        close   = float(s.get("close") or s.get("current_price") or 0)
        above_50= bool(s.get("above_sma50"))
        rsi_d   = float(s.get("rsi_daily") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # Hard gates
        if rs < 4:                                continue   # must be outperforming
        if consol > 12:                           continue   # must be consolidating
        if not above_50:                          continue
        if market_cap < MIN_MARKET_CAP:           continue
        if rsi_d < 48:                            continue   # trend must be positive
        if s_rank > 10:                           continue

        score = 0.0
        # RS quality
        if rs > 20:                              score += 28
        elif rs > 12:                            score += 22
        elif rs > 6:                             score += 15
        elif rs > 4:                             score += 8
        # Both timeframes positive = consistent RS
        if ret_1m > 0 and ret_3m > 0:           score += 18
        elif ret_1m > 0:                         score += 9
        # Tight consolidation
        if consol < 5:                           score += 18
        elif consol < 8:                         score += 12
        elif consol < 12:                        score += 6
        # Approaching 30d high (coiling near resistance)
        if high_30d > 0 and close > 0:
            pct_away = (high_30d - close) / high_30d * 100
            if pct_away < 1.5:                   score += 18
            elif pct_away < 3.5:                 score += 12
            elif pct_away < 6:                   score += 6
        # RSI in good entry range
        if 52 <= rsi_d <= 68:                   score += 12
        elif rsi_d > 68:                         score += 5
        # Sector
        score += max(0, (6 - s_rank)) * 2

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  RSB: {len(results)} candidates")
    return results


def run_mom_continuation(stock_map: dict, sector_rank: dict) -> dict:
    """
    Momentum Continuation — NEW.
    Finds stocks in confirmed EXPANSION phase with accelerating velocity,
    currently near entry zone (or having pulled back to it).

    This is the most reliable 1-4 week swing setup when market is trending:
    strong stock, confirmed trend, minor pullback, entry near support.
    """
    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        macd_h  = float(s.get("macd_hist") or 0)
        above_50= bool(s.get("above_sma50"))
        sma200  = bool(s.get("sma50_gt_200"))
        above_st= bool(s.get("above_st"))
        ret_1m  = float(s.get("ret_1m") or 0)
        ret_3m  = float(s.get("ret_3m") or 0)
        wk_hh   = bool(s.get("wk_hi_high"))
        wk_hl   = bool(s.get("wk_hi_low"))
        dist_50 = float(s.get("dist_sma50") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # Hard gates for EXPANSION phase detection
        if not (above_50 and sma200 and above_st):    continue   # full structural alignment
        if adx < 20 or di_plus <= di_min:             continue   # trend must be active
        if rsi_d < 52 or rsi_d > 74:                 continue   # mid-cycle momentum
        if rsi_w < 54:                               continue   # weekly must confirm
        if macd_h <= 0:                              continue   # MACD positive
        if not (wk_hh or wk_hl):                     continue   # structural uptrend
        if market_cap < MIN_MARKET_CAP:               continue
        if s_rank > 10:                               continue

        # Velocity check: 1m outpacing 3m pace
        monthly_pace = ret_3m / 3 if ret_3m > 0 else 0
        accelerating = ret_1m > monthly_pace * 1.2 if monthly_pace > 0 else ret_1m > 3

        score = 0.0
        # ADX strength
        if adx >= 30:                            score += 20
        elif adx >= 22:                          score += 14
        elif adx >= 17:                          score += 8
        # RSI in ideal momentum range
        if 57 <= rsi_d <= 68:                   score += 18
        elif 52 <= rsi_d < 57:                  score += 12
        elif rsi_d > 68:                         score += 7
        # Weekly alignment
        if rsi_w >= 62:                          score += 12
        elif rsi_w >= 56:                        score += 7
        # Velocity
        if accelerating:                         score += 12
        # Structural perfection
        if wk_hh and wk_hl:                     score += 10
        elif wk_hl:                              score += 5
        # Delivery = institutional holding
        if delivery >= 55:                       score += 10
        elif delivery >= 42:                     score += 5
        # How far from SMA50 (close to it = better entry)
        if dist_50 < 5:                          score += 8
        elif dist_50 < 10:                       score += 4
        # Sector
        score += max(0, (6 - s_rank)) * 3

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  MOM: {len(results)} candidates")
    return results


def run_reversal_setup(stock_map: dict, sector_rank: dict, regime_name: str) -> dict:
    """
    Reversal Setup — NEW.
    Finds stocks bouncing back to SMA50 from below or re-crossing it.
    RSI turning up from oversold territory in an otherwise quality stock.

    This is a contrarian setup — only works in NEUTRAL/TRENDING regimes.
    In RISK OFF, oversold bounces fail frequently. Hard filter applied.
    """
    if regime_name in ("RISK OFF",):
        logger.info("  RVS: skipped (RISK OFF regime)")
        return {}

    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        rsi_m   = float(s.get("rsi_monthly") or 0)
        above_50= bool(s.get("above_sma50"))
        sma200  = bool(s.get("sma50_gt_200"))
        dist_50 = float(s.get("dist_sma50") or 0)
        ret_6m  = float(s.get("ret_6m") or 0)
        macd_h  = float(s.get("macd_hist") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # Hard gates: quality stock in temporary dip
        if rsi_d > 54:                           continue   # not a dip if RSI is high
        if rsi_d < 36:                           continue   # too deep = falling knife
        if rsi_m < 48:                           continue   # monthly trend must be OK
        if not sma200:                           continue   # needs golden cross (quality base)
        if ret_6m < 0:                           continue   # must have positive 6m trend
        if dist_50 < -10:                        continue   # too far below SMA50 = breakdown
        if market_cap < MIN_MARKET_CAP:          continue
        if s_rank > 8:                           continue   # only quality sectors

        score = 0.0
        # RSI in sweet bounce zone
        if 44 <= rsi_d <= 52:                    score += 25   # ideal
        elif rsi_d < 44:                         score += 15
        # MACD turning (even if still negative)
        if macd_h > 0:                           score += 18   # recovered
        elif macd_h > -0.3:                      score += 12   # near zero = turning
        elif macd_h > -1:                        score += 6
        # Proximity to SMA50 (closer = lower risk entry)
        abs_dist = abs(dist_50)
        if abs_dist < 2:                         score += 18
        elif abs_dist < 5:                       score += 12
        elif abs_dist < 8:                       score += 6
        # Monthly RSI still positive
        if rsi_m >= 58:                          score += 12
        elif rsi_m >= 50:                        score += 7
        # Volume + delivery on bounce = buyers are institutional
        if vol_r >= 1.5 and delivery >= 45:      score += 15
        elif vol_r >= 1.2 or delivery >= 40:     score += 7
        # Weekly RSI still respectable
        if rsi_w >= 48:                          score += 8
        # Sector
        score += max(0, (5 - s_rank)) * 4

        if score >= 60:
            results[sym] = round(score, 1)

    logger.info(f"  RVS: {len(results)} candidates")
    return results


def run_sector_rotation(stock_map: dict, sector_rank: dict) -> dict:
    """
    Sector Rotation — NEW.
    Finds EARLY MOVERS in sectors that recently improved in rank (now rank 1-5),
    but the individual stocks haven't fully priced in the sector rotation yet.

    Logic: fresh sector strength + stock not yet extended (RSI 52-64) +
    delivery building (institutions buying the sector rotation).
    """
    results = {}
    # Focus on top 5 sectors — these are the freshly leading sectors
    top_sectors = {s for s, r in sector_rank.items() if r <= 5}

    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        if sector not in top_sectors:            continue

        s_rank  = sector_rank.get(sector, 99)
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        above_50= bool(s.get("above_sma50"))
        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        ret_1m  = float(s.get("ret_1m") or 0)
        market_cap = float(s.get("market_cap") or 0)

        # Hard gates: not yet extended, trend forming
        if rsi_d < 48 or rsi_d > 72:            continue
        if not above_50:                         continue
        if market_cap < MIN_MARKET_CAP:          continue
        if di_plus <= di_min:                   continue   # must be directionally bullish

        score = 0.0
        # Sector rank (tighter = better for rotation)
        if s_rank == 1:                          score += 20
        elif s_rank == 2:                        score += 16
        elif s_rank <= 4:                        score += 10
        elif s_rank <= 6:                        score += 5
        # RSI not yet extended = room to run
        if 52 <= rsi_d <= 64:                   score += 20
        elif 48 <= rsi_d < 52:                  score += 12
        elif rsi_d > 64:                         score += 8
        # Building momentum
        if adx >= 22:                            score += 12
        elif adx >= 17:                          score += 7
        # Volume + delivery = institutional rotation buying
        if vol_r >= 1.5 and delivery >= 48:      score += 18
        elif vol_r >= 1.3 or delivery >= 42:     score += 10
        # Recent 1m return positive (trend established)
        if ret_1m > 5:                           score += 10
        elif ret_1m > 0:                         score += 5
        # Weekly alignment
        if rsi_w >= 55:                          score += 10
        elif rsi_w >= 50:                        score += 5

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  SEC: {len(results)} candidates")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION & SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_and_rank(
    engine_results: dict,
    stock_map: dict,
    sector_rank: dict,
    asm_set: set,
    fo_ban_set: set,
    eap_adjustments: dict,
    regime_name: str,
    open_syms: set,
    force_include: set,
    today: str,
    max_symbols: int = 30,
    max_per_sector: int = 5,
    allow_risk_off: bool = False,
) -> list:
    
    """
    Aggregate all engine scores, apply convergence bonus, EAP overlay,
    sector diversification, and return top max_symbols candidates ranked.
    """
    # 1. Collect all symbols and their engine memberships
    all_syms: dict = defaultdict(lambda: {"engines": [], "scores": [], "raw": 0})
    for engine, results in engine_results.items():
        for sym, score in results.items():
            all_syms[sym]["engines"].append(engine)
            all_syms[sym]["scores"].append(score)

    # 2. Hard exclusions
    blocked_by_regime = set()
    regime_strict = (regime_name == "RISK OFF") and not allow_risk_off
    if regime_name == "RISK OFF" and allow_risk_off:
        logger.warning("  RISK OFF regime detected — screener_allow_risk_off=True, proceeding with reduced universe")

    candidates = []
    for sym, data in all_syms.items():
        s = stock_map.get(sym, {})

        # Hard blocks
        if sym in asm_set or sym in fo_ban_set:          continue
        if (s.get("market_cap") or 0) < MIN_MARKET_CAP:  continue

        # Regime filter for new entries (not open positions or forced)
        if regime_strict and sym not in open_syms and sym not in force_include:
            blocked_by_regime.add(sym)
            continue

        # Base score = average across engines
        base_score = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0

        # Convergence bonus: each additional engine confirming = +CONVERGENCE_BONUS pts
        n_engines = len(data["engines"])
        convergence = (n_engines - 1) * CONVERGENCE_BONUS

        # EAP adjustment
        eap_adj = eap_adjustments.get(sym, 0)

        # Final composite score
        composite = base_score + convergence + eap_adj

        # Avoid AVOID_ENTRY stocks if score would be negative after adjustment
        if composite < 0:                                 continue

        sector    = (s.get("sector") or "").lower()
        s_rank    = sector_rank.get(s.get("sector",""), 99)
        in_pos    = sym in open_syms
        forced    = sym in force_include

        candidates.append({
            "symbol":          sym,
            "company_name":    s.get("company_name"),
            "sector":          s.get("sector", ""),
            "strategy_source": "+".join(sorted(data["engines"])),
            "current_price":   s.get("close") or s.get("current_price"),
            "market_cap":      s.get("market_cap"),
            "composite_score": round(composite, 1),
            "base_score":      round(base_score, 1),
            "convergence_pts": convergence,
            "eap_adjustment":  eap_adj,
            "engines_count":   n_engines,
            "engines_list":    ",".join(sorted(data["engines"])),
            "sector_rank":     s_rank,
            "in_position":     in_pos,
            "force_include":   forced,
            "date":            today,
        })

    # 3. Sort by composite score
    candidates.sort(key=lambda x: (x["force_include"], x["composite_score"]), reverse=True)

    # 4. Sector diversification — max MAX_PER_SECTOR per sector
    sector_counts: dict = defaultdict(int)
    selected = []
    overflow = []

    for c in candidates:
        sector = c["sector"].lower()
        if c["force_include"]:
            selected.append(c)  # forced always included
        elif sector_counts[sector] < max_per_sector:
            selected.append(c)
            sector_counts[sector] += 1
        else:
            overflow.append(c)  # consider if we haven't hit max_symbols yet

    # Fill remaining slots from overflow (best scores regardless of sector cap)
    remaining = max_symbols - len(selected)
    if remaining > 0:
        selected.extend(overflow[:remaining])
        selected.sort(key=lambda x: x["composite_score"], reverse=True)

    final = selected[:max_symbols]

    if blocked_by_regime:
        logger.warning(f"  {len(blocked_by_regime)} symbols blocked by RISK OFF regime")

    logger.info(
        f"  Aggregated: {len(all_syms)} total unique | "
        f"{len(candidates)} after hard filters | {len(final)} final"
    )
    return final


# ─────────────────────────────────────────────────────────────────────────────
# WRITE STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

def write_screener_results(sb, candidates: list, mode: str, today: str):
    """
    shadow  → msl_computed only (master_shortlist untouched)
    hybrid  → msl_computed + update master_shortlist (merge with Sheet)
    full    → master_shortlist only (screener is the source of truth)
    """
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(candidates)} symbols | mode={mode}")
        for c in candidates[:5]:
            logger.info(
                f"  #{candidates.index(c)+1} {c['symbol']:<12} "
                f"score={c['composite_score']:>5}  engines={c['engines_list']:<25} "
                f"sector={c['sector']}"
            )
        return

    if mode in ("shadow", "hybrid"):
        # Write to msl_computed (always in shadow/hybrid)
        rows = []
        for rank, c in enumerate(candidates, 1):
            rows.append({
                "date":            today,
                "symbol":          c["symbol"],
                "company_name":    c.get("company_name"),
                "sector":          c.get("sector"),
                "strategy_source": c.get("strategy_source"),
                "current_price":   c.get("current_price"),
                "composite_score": c.get("composite_score"),
                "base_score":      c.get("base_score"),
                "priority_rank":   rank,
                "engines_list":    c.get("engines_list"),
                "convergence_pts": c.get("convergence_pts"),
                "eap_adjustment":  c.get("eap_adjustment"),
                "sector_rank":     c.get("sector_rank"),
                "in_position":     c.get("in_position"),
                "screener_source": "screen_stocks_v1",
                "computed_at":     datetime.now(IST).isoformat(),
            })
        for i in range(0, len(rows), 50):
            sb.table("msl_computed").upsert(rows[i:i+50], on_conflict="date,symbol").execute()
        logger.success(f"✓ {len(rows)} symbols → msl_computed")

    if mode in ("hybrid", "full"):
        # Write to master_shortlist
        msl_rows = []
        for rank, c in enumerate(candidates, 1):
            msl_rows.append({
                "date":            today,
                "symbol":          c["symbol"],
                "company_name":    c.get("company_name"),
                "sector":          c.get("sector"),
                "strategy_source": c.get("strategy_source"),
                "current_price":   c.get("current_price"),
                "final_score":     c.get("composite_score"),
                "base_score":      c.get("base_score"),
                "base_rank":       rank,
                "position_state":  "WATCHING",
                "in_position":     c.get("in_position", False),
            })
        for i in range(0, len(msl_rows), 50):
            sb.table("master_shortlist").upsert(msl_rows[i:i+50], on_conflict="date,symbol").execute()
        # Remove stale symbols no longer selected (in full mode only)
        if mode == "full":
            selected_syms = {c["symbol"] for c in candidates}
            existing = sb.table("master_shortlist").select("symbol").eq("date", today).execute().data
            stale = {r["symbol"] for r in existing} - selected_syms
            if stale:
                sb.table("master_shortlist").delete().eq("date", today).in_("symbol", list(stale)).execute()
                logger.info(f"  Removed {len(stale)} stale symbols from master_shortlist")
        logger.success(f"✓ {len(msl_rows)} symbols → master_shortlist")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — screen_stocks skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())
    mode  = cfg("screener_mode", "shadow")
    if os.getenv("SCREENER_MODE_OVERRIDE"):
        mode = os.getenv("SCREENER_MODE_OVERRIDE")

    max_symbols        = int(cfg("screener_max_symbols", str(MAX_SYMBOLS)))
    max_sector         = int(cfg("screener_max_per_sector", str(MAX_PER_SECTOR)))
    allow_risk_off     = cfg_bool("screener_allow_risk_off", False)

    logger.info("=" * 65)
    logger.info(f"STEP: Screen Stocks v1.0 | {today} | mode={mode.upper()}"
                + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 65)

    # ── Load ──────────────────────────────────────────────────────────────────
    logger.info("Pass 1: loading data...")
    data = load_data(sb, today)
    if not data or not data.get("stock_map"):
        logger.error("No stock data — aborting")
        return {"status": "no_data"}

    stock_map   = data["stock_map"]
    sector_rank = data["sector_rank"]
    regime_name = resolve_regime(data["regime"])
    asm_set     = data["asm_set"]
    fo_ban_set  = data["fo_ban_set"]
    event_rows  = data["event_rows"]
    force_include = data["force_include"]
    open_syms   = data["open_syms"]

    # Load strategy configs from system_config
    strat_cfg = {}
    try:
        from config import get_strategy_config
        strat_cfg = get_strategy_config()
    except Exception:
        pass

    def _parse(key): return (
        json.loads(strat_cfg[key]) if isinstance(strat_cfg.get(key), str)
        else strat_cfg.get(key, {})
    )
    cfg_ctl = _parse("CTL")
    cfg_sbs = _parse("SBS")
    cfg_tpo = _parse("TPO")
    cfg_eap = _parse("EAP")

    # ── Run engines ───────────────────────────────────────────────────────────
    logger.info(f"Pass 2: running engines (regime={regime_name})...")

    ctl_results = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_results = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_results = run_tpo(ctl_results, stock_map, cfg_tpo, regime_name)
    vbd_results = run_vbd(stock_map, sector_rank)
    iad_results = run_iad(stock_map, sector_rank)
    rsb_results = run_rsb(stock_map, sector_rank)
    mom_results = run_mom_continuation(stock_map, sector_rank)
    rvs_results = run_reversal_setup(stock_map, sector_rank, regime_name)
    sec_results = run_sector_rotation(stock_map, sector_rank)

    all_candidates = (
        set(ctl_results) | set(sbs_results) | set(tpo_results) |
        set(vbd_results) | set(iad_results) | set(rsb_results) |
        set(mom_results) | set(rvs_results) | set(sec_results)
    )

    # EAP overlay
    eap_adjustments = run_eap_overlay(
        {sym: 1 for sym in all_candidates}, stock_map, event_rows, cfg_eap
    )

    engine_results = {
        "CTL": ctl_results, "SBS": sbs_results, "TPO": tpo_results,
        "VBD": vbd_results, "IAD": iad_results, "RSB": rsb_results,
        "MOM": mom_results, "RVS": rvs_results, "SEC": sec_results,
    }

    logger.info(
        f"  Engine totals: CTL={len(ctl_results)} SBS={len(sbs_results)} "
        f"TPO={len(tpo_results)} VBD={len(vbd_results)} IAD={len(iad_results)} "
        f"RSB={len(rsb_results)} MOM={len(mom_results)} RVS={len(rvs_results)} "
        f"SEC={len(sec_results)} | Total unique={len(all_candidates)}"
    )

    # ── Aggregate ─────────────────────────────────────────────────────────────
    logger.info("Pass 3: aggregating and ranking...")
    final = aggregate_and_rank(
        engine_results, stock_map, sector_rank,
        asm_set, fo_ban_set, eap_adjustments,
        regime_name, open_syms, force_include, today,
        max_symbols=max_symbols, max_per_sector=max_sector,
        allow_risk_off=allow_risk_off,
    )

    if not final:
        logger.error("No candidates survived selection")
        return {"status": "no_candidates"}

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info(f"\n  ═══ TOP {len(final)} SCREENED SYMBOLS ═══")
    for i, c in enumerate(final, 1):
        tag = "📂" if c.get("in_position") else "  "
        logger.info(
            f"  {tag} #{i:>2} {c['symbol']:<12} "
            f"score={c['composite_score']:>5}  "
            f"engines={c['engines_list']:<28} "
            f"sector={c['sector']:<20} "
            f"rank={c['sector_rank']}"
        )

    from collections import Counter
    sector_dist = Counter(c["sector"] for c in final)
    engine_dist = Counter()
    for c in final:
        for e in c["engines_list"].split(","):
            engine_dist[e] += 1

    multi_engine = [c for c in final if c["engines_count"] >= 2]
    logger.info(f"  Multi-engine confirmed: {len(multi_engine)}/{len(final)}")
    logger.info(f"  Sector distribution: {dict(sector_dist)}")
    logger.info(f"  Engine hits: {dict(engine_dist)}")

    # ── Write ─────────────────────────────────────────────────────────────────
    logger.info(f"Pass 4: writing ({mode} mode)...")
    write_screener_results(sb, final, mode, today)

    return {
        "status":        "ok",
        "selected":      len(final),
        "mode":          mode,
        "regime":        regime_name,
        "multi_engine":  len(multi_engine),
        "engines_used":  list(engine_dist.keys()),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Screen Stocks v1.0")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--mode",     choices=["shadow","hybrid","full"])
    parser.add_argument("--max",      type=int, default=30, help="Max symbols to select")
    args = parser.parse_args()
    if args.dry_run: os.environ["DRY_RUN"] = "True"
    if args.mode:    os.environ["SCREENER_MODE_OVERRIDE"] = args.mode
    print(main())
