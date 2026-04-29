"""
TradeOS v6 — MSL Computation Engine v3.0
==========================================
Pipeline position: [14] — after screen_stocks, before signals.

WHAT CHANGED FROM v2.0 → v3.0
================================
All 12 critical bugs from the v2 audit are fixed. Key changes:

BUG FIXES:
  #1  load_data no longer re-reads cfg — mode is resolved by main() and passed in
  #2  Duplicate msl_map assignment removed (dead code that silently killed screener merge)
  #3  sector_strength fallback now fetches the LATEST DATE's rows, not a mixed 50-row grab
  #4  hybrid/full mode screener source fixed — screen_stocks writes to master_shortlist
      in those modes, not msl_computed; query routing corrected accordingly
  #5  ASM/GSM/FO_BAN gate added — safety_lists integrated into load_data + symbol loop
  #6  FII/DII data (fii_net_5d, fii_net_20d, fii_flag) now feeds compute_institutional_score
  #7  NSE event bias + upcoming_news now feeds compute_risk_score (event risk)
  #8  Pipeline ordering note documented (step 10 regime runs before step 12 indicators —
      breadth is one run stale; accepted design trade-off, documented)
  #9  Wrong log message in shadow mode fixed
  #10 OPTIMAL checked before REENTRY in compute_entry_timing_type
  #11 FRESH/DEVELOPING conflict resolved with explicit trend-strength guard
  #12 shadow mode write now merges over existing msl_computed rows (preserves screen_stocks fields)

NEW IN v3.0:
  - Global macro context: crude oil, GIFT Nifty, CPI/WPI bias from global_cues + macro_indicators
  - Market news sentiment: bearish/bullish news count from market_news feeds risk_score
  - Liquidity gate: VERY_LOW liquidity stocks flagged, configurable min_value_cr gate
  - Regime ordering awareness: explicit note when step 10 runs before step 12
  - trade_allowed propagated to result rows for step 15 (signals) to gate on
  - compute_source upgraded to "compute_msl_v3"

AI INTEGRATION DESIGN (answered in detail below):
  compute_msl is the FEEDER to AI steps 18-20. It does NOT call AI itself.
  The 12 fields that most influence AI quality are documented in AI_FEED_FIELDS.
  compute_msl's job is to produce CLEAN, SEPARATION-RICH scores so the AI
  sees meaningful signal, not noise. A stock with final_score=72 should
  genuinely be a better setup than one at 54. If the scores are flat (all
  65-68), the AI cannot differentiate and defaults to generic conviction.

DESIGN PHILOSOPHY — Swing Trading 1-4 Weeks
============================================
composite_score = screener aggregate (from screen_stocks)
final_score     = intelligence-weighted output (this script)

KEY PRINCIPLES remain from v2 — regime-aware thresholds, all 70+ fields
evaluated, holding_score vs entry_score, quality over quantity.

DATA COVERAGE ASSESSMENT (what we have vs what we need):
  ✅ Price/OHLCV:        close, high, low, open, volume — complete
  ✅ Momentum:           RSI daily/weekly/monthly, MACD, Stochastic — complete
  ✅ Trend:              ADX, DI+/DI-, SMA/EMA stack, Supertrend, PSAR — complete
  ✅ Volume:             vol_ratio, avg_vol_20d/50d, delivery_pct, value_cr — complete
  ✅ Structure:          wk_hi_high/low, consol_range, bb_upper/lower — complete
  ✅ Multi-TF:           VWAP today/20d/50d, RSI monthly, HA candles — complete
  ✅ Fundamentals:       quarterly_net_profit, quarterly_variance, ttm_net_profit, eps — complete
  ✅ Macro/FII:          fii_net_5d/20d, fii_flag — NOW INTEGRATED (was missing in v2)
  ✅ Safety:             ASM/GSM/FO_BAN — NOW INTEGRATED (was missing in v2)
  ✅ Event risk:         event_bias, upcoming_news — NOW INTEGRATED (was missing in v2)
  ⚠️  Options data:      PCR, max pain, OI buildup — NOT yet in stock_data_daily
                         (Phase 3 addition — would significantly improve breakout_readiness)
  ⚠️  Promoter holding:  shareholding pattern — NOT yet integrated
                         (quarterly, would improve institutional_score)
  ⚠️  Sector rotation:   FII sector-level flow — NOT yet in pipeline
                         (sector_strength is rank-based not flow-based)

TRANSITION MODES (system_config: compute_msl_mode):
  shadow → writes to msl_computed only (safe, default)
  hybrid → writes computed fields to master_shortlist
  full   → master_shortlist computed entirely here (Sheet = symbol list only)
"""

import sys, os
from pathlib import Path
from datetime import datetime, timedelta, timezone
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

# Fields that compute_msl produces and feeds directly to AI steps 18-20.
# Keep these well-separated and meaningful — flat scores = weak AI output.
AI_FEED_FIELDS = [
    "final_score",          # primary ranking signal for generate_shortlist
    "momentum_state",       # HOT/BUILDING/STABLE/WEAK — narrative anchor
    "momentum_phase",       # EXPANSION/EXTENDED/EXHAUSTED — phase narrative
    "trend_maturity",       # FRESH/DEVELOPING/LATE/EXHAUSTED — runway context
    "lifecycle",            # ADD/HOLD/REDUCE/EXIT/DEVELOPING — action signal
    "struct_edge",          # YES/NO — structural quality gate
    "entry_timing_type",    # OPTIMAL/REENTRY/CHASING/EXTENDED — timing
    "holding_score",        # for positions: is trend still intact?
    "risk_score",           # what can go wrong?
    "institutional_score",  # smart money involved?
    "velocity_state",       # ACCELERATING/FLAT/DECELERATING
    "weekly_structure",     # STRONG/CONSOLIDATING/CAUTION/WEAK
]

# Minimum traded value (Cr) to be a viable swing trade position
MIN_VALUE_CR_DEFAULT = 10.0

# Safety list types that block scoring
BLOCK_LIST_TYPES = frozenset({"FO_BAN", "ASM_ST1", "ASM_ST2", "GSM"})

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS: DATA NORMALIZATION + SIGNAL DERIVATION
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_flow_flag(raw_flag: str, net_5d: float) -> str:
    """
    Normalize heterogeneous FII/DII flag values from fii_dii_flow into the
    standard BUYING / NEUTRAL / SELLING enum used by all scoring functions.

    DB values observed: "CAUTION", "STRONG_BUYING", "BUYING", "SELLING", "NEUTRAL"
    CAUTION is ambiguous — resolve via actual net_5d flow direction.
    This normalization must happen at load time so every downstream function
    sees a consistent enum without defensive branching.
    """
    flag = (raw_flag or "").upper().strip()
    if flag in ("STRONG_BUYING", "BUYING"):
        return "BUYING"
    if flag in ("SELLING", "HEAVY_SELLING", "STRONG_SELLING"):
        return "SELLING"
    if flag == "CAUTION":
        # CAUTION = sustained net selling below the upstream script's SELLING threshold.
        # Resolve by actual 5-day net flow. -500 Cr is a meaningful threshold in NSE context.
        return "SELLING" if net_5d < -500 else "NEUTRAL"
    return "NEUTRAL"


# News sentiment classification — rule-based, no NLP dependency
_BEARISH_CATS    = frozenset({"REGULATORY", "GEOPOLITICAL", "MACRO_NEGATIVE", "RATE_HIKE",
                               "DOWNGRADE", "FRAUD", "INVESTIGATION", "SANCTIONS"})
_BULLISH_CATS    = frozenset({"POLICY_EASING", "MACRO_POSITIVE", "RATE_CUT", "RESULTS_BEAT",
                               "UPGRADE", "ACQUISITION", "BUYBACK", "CENTRAL_BANK_POSITIVE"})
_HIGH_IMP_TYPES  = frozenset({"POLICY", "MACRO", "EARNINGS", "RATING", "REGULATORY"})
_MED_IMP_TYPES   = frozenset({"BULK_DEAL", "CORPORATE", "MARKET", "ANNOUNCEMENT"})
_BEARISH_WORDS   = frozenset({"ban", "penalty", "fine", "loss", "losses", "decline", "slump",
                               "probe", "fraud", "default", "downgrade", "suspended",
                               "violation", "warning", "seized", "concern", "selling"})
_BULLISH_WORDS   = frozenset({"buy", "buying", "upgrade", "profit", "growth", "record",
                               "acquisition", "dividend", "beat", "surge", "rally",
                               "recovery", "approved", "launch", "expansion", "order", "win"})

def _classify_news_sentiment(category: str, impact_type: str, headline: str = "") -> tuple:
    """
    Returns (sentiment: str, impact_level: str).
    Priority: category-level classification > headline keyword scan > NEUTRAL default.
    Bulk deals resolved from NSE headline format ("BUY SYMBOL" / "SELL SYMBOL").
    """
    cat = (category or "").upper()
    imp = (impact_type or "").upper()
    hl  = (headline or "").lower()

    impact_level = (
        "HIGH"   if imp in _HIGH_IMP_TYPES else
        "MEDIUM" if imp in _MED_IMP_TYPES  else
        "LOW"
    )

    # Bulk deal: NSE format has BUY/SELL directly in headline
    if imp == "BULK_DEAL":
        hl_words = f" {hl} "
        if " buy " in hl_words:  return "BULLISH", impact_level
        if " sell " in hl_words: return "BEARISH", impact_level
        return "NEUTRAL", impact_level

    if cat in _BEARISH_CATS: return "BEARISH", impact_level
    if cat in _BULLISH_CATS: return "BULLISH", impact_level

    # Keyword scan as fallback (medium precision)
    words        = set(hl.split())
    bearish_hits = len(words & _BEARISH_WORDS)
    bullish_hits = len(words & _BULLISH_WORDS)

    if bearish_hits > bullish_hits: return "BEARISH", impact_level
    if bullish_hits > bearish_hits: return "BULLISH", impact_level
    return "NEUTRAL", impact_level


def _derive_global_signals(global_rows: list) -> dict:
    """
    Derive crude_direction and global_sentiment from 5-day global_cues lookback.

    WHY 5-DAY: A single day's brent +3% after a -8% week is noise, not trend.
    The macro overlay needs directional signals, not daily ticks.

    crude_direction:  5-day cumulative brent change — trend, not tick.
    global_sentiment: composite US equity + yield signal over 5 days.
                      Rising yields + falling equities = RISK_OFF.
                      MILDLY_BULLISH/BEARISH for intermediate states.
    gift_nifty_gap_pct: today's value directly from DB (gift_nifty_chg_pct column).
    """
    if not global_rows:
        return {"gift_nifty_gap_pct": 0.0, "crude_direction": "FLAT",
                "global_sentiment": "NEUTRAL", "brent_chg_pct_1d": 0.0, "sp500_5d_ret": 0.0}

    today_row   = global_rows[0]
    gift_gap    = float(today_row.get("gift_nifty_chg_pct") or 0)
    brent_1d    = float(today_row.get("brent_chg_pct") or 0)

    # 5-day cumulative sums
    brent_5d    = sum(float(r.get("brent_chg_pct") or 0)    for r in global_rows)
    sp500_5d    = sum(float(r.get("sp500_chg_pct") or 0)    for r in global_rows)
    dow_5d      = sum(float(r.get("us_dow_chg_pct") or 0)   for r in global_rows)
    nasdaq_5d   = sum(float(r.get("us_nasdaq_chg_pct") or 0) for r in global_rows)
    yield_5d    = sum(float(r.get("us_10yr_chg_bps") or 0)  for r in global_rows)

    crude_dir = (
        "RISING"  if brent_5d > 2.5  else
        "FALLING" if brent_5d < -2.5 else
        "FLAT"
    )

    us_composite = (sp500_5d + dow_5d) / 2
    # Rising yields (>20bps over 5d) drag on equities = tighten sentiment
    yield_drag   = -1 if yield_5d > 20 else (1 if yield_5d < -10 else 0)
    adjusted     = us_composite + yield_drag * 0.5

    sentiment = (
        "RISK_ON"        if adjusted > 2.0  else
        "MILDLY_BULLISH" if adjusted > 0.5  else
        "RISK_OFF"       if adjusted < -2.0 else
        "MILDLY_BEARISH" if adjusted < -0.5 else
        "NEUTRAL"
    )

    return {
        "gift_nifty_gap_pct": gift_gap,
        "crude_direction":    crude_dir,
        "global_sentiment":   sentiment,
        "brent_chg_pct_1d":   brent_1d,
        "sp500_5d_ret":       round(sp500_5d, 2),
        "brent_5d_sum":       round(brent_5d, 2),
        "us_composite_5d":    round(us_composite, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING — 9 bulk queries, zero per-symbol calls
# ─────────────────────────────────────────────────────────────────────────────

def load_data(sb, today: str, mode: str) -> dict:
    """
    9 bulk queries — no per-symbol calls.
    mode is resolved by main() (including CLI override) — do NOT re-read cfg here.

    BUG #1 FIX: removed `mode = cfg(...)` override.
    BUG #4 FIX: screener source correctly split by mode.
    BUG #3 FIX: sector_strength fallback fetches latest DATE's rows only.

    Symbol source priority:
      shadow mode  → msl_computed (screen_stocks output), falls back to master_shortlist
      hybrid/full  → master_shortlist (screen_stocks already wrote here), no secondary query needed
    """

    # ── 1. Determine symbol source ────────────────────────────────────────────
    # BUG #4 FIX: In hybrid/full, screen_stocks writes to master_shortlist, NOT msl_computed.
    #             In shadow, screen_stocks writes to msl_computed.
    screener_rows = []

    if mode == "shadow":
        # Shadow: screen_stocks output is in msl_computed
        screener_rows = (
            sb.table("msl_computed")
              .select("symbol,sector,strategy_source,current_price,final_score,composite_score")
              .eq("date", today)
              .execute().data
        )
        if screener_rows:
            logger.info(f"  Symbol source: msl_computed (screen_stocks shadow) — {len(screener_rows)} symbols")
        else:
            logger.warning("  msl_computed empty for today — falling back to master_shortlist for symbols")

    # Determine primary source table
    if mode == "shadow" and screener_rows:
        source_table = "msl_computed"
    else:
        source_table = "master_shortlist"

    msl_rows = sb.table(source_table).select("*").eq("date", today).execute().data
    if not msl_rows:
        latest = sb.table(source_table).select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            fb = latest[0]["date"]
            msl_rows = sb.table(source_table).select("*").eq("date", fb).execute().data
            logger.warning(f"  {source_table}: falling back to {fb}")

    # BUG #2 FIX: Single msl_map assignment — no duplicate overwrite
    msl_map = {r["symbol"]: r for r in (msl_rows or [])}
    logger.info(f"  Source table: {source_table} ({len(msl_map)} symbols)")

    # In shadow mode, merge screener metadata (strategy_source, composite_score) into msl_map
    # without overwriting Sheet fields (company_name, sector, notes, etc.)
    if mode == "shadow" and screener_rows:
        # ── INGEST_SHEETS MERGE (remove this block when ingest_sheets is retired) ──
        _sheet_rows = (
            sb.table("master_shortlist")
            .select("*")
            .eq("date", today)
            .eq("compute_source", "ingest_sheets")
            .execute().data
        )
        _added = 0
        for r in (_sheet_rows or []):
            sym = r.get("symbol")
            if sym and sym not in msl_map:
                msl_map[sym] = r
                _added += 1
        if _added:
            logger.info(f"  ingest_sheets: {_added} symbols merged into msl_map")
        # ── END INGEST_SHEETS MERGE ─────────────────────────────────────────────────
        screener_map = {r["symbol"]: r for r in screener_rows}
        added = 0
        for sym, sr in screener_map.items():
            if sym in msl_map:
                # Only update screener-owned fields
                msl_map[sym]["strategy_source"] = sr.get("strategy_source") or msl_map[sym].get("strategy_source")
                msl_map[sym]["composite_score"] = sr.get("composite_score") or msl_map[sym].get("composite_score")
            else:
                # Screener found a new symbol not yet in master_shortlist
                msl_map[sym] = sr
                added += 1
        if added:
            logger.info(f"  Screener added {added} new symbols not in master_shortlist")

    symbols = list(msl_map.keys())
    if not symbols:
        logger.error("No MSL symbols found")
        return {}

    # ── 2. Stock data (all 70+ fields) ───────────────────────────────────────
    stock_rows = (
        sb.table("stock_data_daily")
          .select("*")
          .eq("date", today)
          .in_("symbol", symbols)
          .execute().data
    )
    if not stock_rows:
        latest_s = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest_s:
            fb_s = latest_s[0]["date"]
            stock_rows = (
                sb.table("stock_data_daily")
                  .select("*")
                  .eq("date", fb_s)
                  .in_("symbol", symbols)
                  .execute().data
            )
            logger.warning(f"  stock_data_daily: falling back to {fb_s}")
    stock_map = {r["symbol"]: r for r in (stock_rows or [])}

# ─────────────────────────────────────────────────────────────────────────────
# FIELD OWNERSHIP MAP — which script is responsible for writing each field
# compute_msl depends on these but does NOT write them.
# If they're missing, upstream script hasn't run — warn loudly.
# ─────────────────────────────────────────────────────────────────────────────

    UPSTREAM_FIELD_OWNERS = {
        # field_name: (owner_script, table, severity)
        "value_cr":      ("ingest_bhavcopy",  "stock_data_daily", "CRITICAL"),
        "delivery_pct":  ("ingest_bhavcopy",  "stock_data_daily", "CRITICAL"),
        "delivery_qty":  ("ingest_bhavcopy",  "stock_data_daily", "WARNING"),
        "adx":           ("compute_indicators", "stock_data_daily", "CRITICAL"),
        "rsi_daily":     ("compute_indicators", "stock_data_daily", "CRITICAL"),
        "ema_20":        ("compute_indicators", "stock_data_daily", "WARNING"),
        "supertrend":    ("compute_indicators", "stock_data_daily", "WARNING"),
        "ha_close":      ("compute_indicators", "stock_data_daily", "WARNING"),
        "vwap_20d":      ("compute_indicators", "stock_data_daily", "WARNING"),
        "macd_hist":     ("compute_indicators", "stock_data_daily", "WARNING"),
        "bb_upper":      ("compute_indicators", "stock_data_daily", "WARNING"),
        "psar":          ("compute_indicators", "stock_data_daily", "WARNING"),
        "rs_vs_nifty":   ("compute_indicators", "stock_data_daily", "WARNING"),
        "above_sma50":   ("compute_indicators", "stock_data_daily", "CRITICAL"),
        "above_st":      ("compute_indicators", "stock_data_daily", "CRITICAL"),
        "sma50_gt_200":  ("compute_indicators", "stock_data_daily", "CRITICAL"),
        "atr_pct":       ("compute_indicators", "stock_data_daily", "WARNING"),
        "high_52w":      ("compute_indicators", "stock_data_daily", "WARNING"),
        "high_30d":      ("compute_indicators", "stock_data_daily", "WARNING"),
        "consol_range":  ("compute_indicators", "stock_data_daily", "WARNING"),
        "wk_hi_high":    ("compute_indicators", "stock_data_daily", "WARNING"),
        "wk_hi_low":     ("compute_indicators", "stock_data_daily", "WARNING"),
    }


    def validate_upstream_fields(stock_map: dict, symbols: list) -> dict:
        """
        Check that fields owned by upstream scripts are actually populated.
        Called at the end of load_data — before any computation begins.

        Returns a dict of field → coverage_pct so main() can log a health report.
        Logs CRITICAL warnings for fields that are >50% missing.
        Logs WARNING for fields that are >20% missing.

        A field counts as "missing" if its value is None, 0 (for numeric fields
        where 0 is not a valid value), or absent from the row.

        WHY: value_cr=0 means ingest_bhavcopy hasn't run today. Without this check,
        compute_msl silently produces VERY_LOW liquidity for every stock and
        generate_signals blocks all entries as "blocked_low_liquidity".
        """
        if not stock_map or not symbols:
            return {}

        n = len([s for s in symbols if s in stock_map])
        if n == 0:
            return {}

        # Fields where 0 is a valid value (boolean-like or signed)
        ZERO_OK_FIELDS = {"above_sma50", "above_st", "sma50_gt_200", "wk_hi_high",
                        "wk_hi_low", "breakout_setup", "bk_trigger"}

        coverage = {}
        critical_missing = []
        warning_missing  = []

        for field, (owner, table, severity) in UPSTREAM_FIELD_OWNERS.items():
            populated = 0
            for sym in symbols:
                row = stock_map.get(sym)
                if not row:
                    continue
                val = row.get(field)
                if val is None:
                    continue
                if field not in ZERO_OK_FIELDS and val == 0:
                    continue   # treat 0 as missing for non-boolean fields
                populated += 1

            pct = round(populated / n * 100, 1) if n > 0 else 0.0
            coverage[field] = pct
            missing_pct = 100 - pct

            if severity == "CRITICAL" and missing_pct > 50:
                critical_missing.append((field, owner, table, pct))
            elif missing_pct > 20:
                warning_missing.append((field, owner, table, pct))

        # Log critical first — these will cause silent wrong results
        if critical_missing:
            logger.error("=" * 60)
            logger.error("UPSTREAM DATA MISSING — COMPUTE RESULTS WILL BE WRONG")
            logger.error("=" * 60)
            for field, owner, table, pct in critical_missing:
                logger.error(
                    f"  CRITICAL: '{field}' only {pct}% populated in {table}"
                    f" — run {owner}.py first"
                )
            logger.error("=" * 60)

        if warning_missing:
            logger.warning("Upstream field coverage warnings:")
            for field, owner, table, pct in warning_missing:
                logger.warning(
                    f"  WARNING:  '{field}' only {pct}% populated in {table}"
                    f" — run {owner}.py to improve signal quality"
                )

        return coverage

    # ── 3. MSL history — last 60 days ────────────────────────────────────────
    cutoff = str(today_ist() - timedelta(days=60))
    hist_rows = (
        sb.table("msl_history")
          .select("*")
          .in_("symbol", symbols)
          .gte("snapshot_date", cutoff)
          .order("snapshot_date", desc=True)
          .execute().data
    )
    history_map: dict = defaultdict(list)
    for r in hist_rows:
        sym = r.get("symbol")
        if sym:
            history_map[sym].append(r)

    # ── 4. Market regime ─────────────────────────────────────────────────────
    # NOTE (BUG #8 DESIGN DECISION): step 10 (compute_regime) runs BEFORE step 12
    # (compute_indicators) in the pipeline. This means above_200dma_pct (breadth) in
    # market_regime reflects the PREVIOUS run's stock_data_daily, not today's freshly
    # computed breadth. This is a known one-run lag. The alternative is to move
    # compute_regime to after compute_indicators in the pipeline (steps swap 10↔12).
    # For now, document and accept — the lag is typically < 0.5% breadth drift.
    regime_rows = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # ── 5. Sector strength ───────────────────────────────────────────────────
    # BUG #3 FIX: Fetch today's rows first; fallback ONLY to the single latest date (not 50-row mix)
    sector_rows = sb.table("sector_strength").select("sector,rank").eq("date", today).execute().data
    if not sector_rows:
        latest_sec = sb.table("sector_strength").select("date").order("date", desc=True).limit(1).execute().data
        if latest_sec:
            fb_sec = latest_sec[0]["date"]
            sector_rows = sb.table("sector_strength").select("sector,rank").eq("date", fb_sec).execute().data
            logger.warning(f"  sector_strength: falling back to {fb_sec}")
    sector_rank = {r["sector"]: r["rank"] for r in sector_rows if r.get("rank")}

    # ── 6. Open positions ────────────────────────────────────────────────────
    open_rows = sb.table("open_positions").select("symbol").eq("status", "ACTIVE").execute().data
    open_syms = {r["symbol"] for r in (open_rows or [])}

    # ── 7. Safety lists — ASM/GSM/FO_BAN (BUG #5 FIX) ───────────────────────
    asm_rows = (
        sb.table("safety_lists")
          .select("symbol,list_type")
          .in_("symbol", symbols)
          .execute().data
    )
    asm_syms: dict = defaultdict(set)  # symbol → set of list_types
    for r in (asm_rows or []):
        sym = r.get("symbol")
        lt  = r.get("list_type")
        if sym and lt:
            asm_syms[sym].add(lt)
    blocked_syms = {sym for sym, types in asm_syms.items() if types & BLOCK_LIST_TYPES}
    if blocked_syms:
        logger.info(f"  Safety gate: {len(blocked_syms)} symbols blocked (ASM/GSM/FO_BAN): {sorted(blocked_syms)}")

    # ── 8. FII/DII — systemic flow signal (BUG #6 FIX) ───────────────────────
# ── 8. FII/DII — systemic flow signal ────────────────────────────────────
    fii_rows = (
        sb.table("fii_dii_flow")
          .select("fii_net_5d,fii_net_20d,fii_flag,dii_net_5d,dii_net_20d,dii_flag")
          .order("date", desc=True)
          .limit(1)
          .execute().data
    )
    fii_data = dict(fii_rows[0]) if fii_rows else {}
    # Normalize flag enums — DB emits CAUTION/STRONG_BUYING; scoring expects BUYING/SELLING/NEUTRAL
    if fii_data:
        fii_data["fii_flag"] = _normalize_flow_flag(
            fii_data.get("fii_flag", ""), float(fii_data.get("fii_net_5d") or 0)
        )
        fii_data["dii_flag"] = _normalize_flow_flag(
            fii_data.get("dii_flag", ""), float(fii_data.get("dii_net_5d") or 0)
        )

    # ── 9. Global cues + macro — additional macro context (NEW in v3) ─────────
    # global_cues: crude oil direction, GIFT Nifty gap → regime reinforcement signal
    # macro_indicators: CPI/WPI trend → broad inflation context
# ── 9. Global cues — 5-day lookback for proper trend derivation ──────────
    # Single-day brent/SP500 changes are noise; trend requires multi-day context.
    global_rows = (
        sb.table("global_cues")
          .select(
              "date,brent_crude,brent_chg_pct,gift_nifty,gift_nifty_chg_pct,"
              "sp500_close,sp500_chg_pct,us_dow_chg_pct,us_nasdaq_chg_pct,"
              "us_10yr_yield,us_10yr_chg_bps"
          )
          .order("date", desc=True)
          .limit(5)
          .execute().data
    )
    global_cues = _derive_global_signals(global_rows)

    # ── macro_indicators: KV store pivot → composite macro_bias ──────────────
    # Table is key-value rows (indicator_name / indicator_value), not columnar.
    # Pivot to dict then derive macro_bias from all available indicators weighted by type.
    macro_rows = (
        sb.table("macro_indicators")
          .select("indicator_name,indicator_value,indicator_date")
          .order("indicator_date", desc=True)
          .limit(30)   # covers multiple indicators; DESC gives latest per name first
          .execute().data
    )
    _seen_m: set = set()
    _macro_kv: dict = {}
    for r in (macro_rows or []):
        name = (r.get("indicator_name") or "").upper()
        if name and name not in _seen_m:
            _macro_kv[name] = float(r.get("indicator_value") or 0)
            _seen_m.add(name)

    # Composite macro_bias — each indicator contributes if available
    _pos = _neg = 0
    _gdp  = _macro_kv.get("GDP_YOY")
    _cpi  = _macro_kv.get("CPI_YOY")
    _repo = _macro_kv.get("REPO_RATE")
    _pcr  = _macro_kv.get("NIFTY_PCR")

    if _gdp  is not None:
        if _gdp >= 7.0:   _pos += 2
        elif _gdp >= 5.5: _pos += 1
        else:             _neg += 2
    if _cpi  is not None:
        if _cpi < 4.0:    _pos += 1   # low inflation = rate cut potential
        elif _cpi > 6.0:  _neg += 2   # above MPC upper band = hike risk
    if _repo is not None:
        if _repo <= 5.5:  _pos += 1   # accommodative stance
        elif _repo >= 7.0: _neg += 1
    if _pcr  is not None:
        if _pcr >= 1.3:   _pos += 1   # high PCR = contrarian buy signal
        elif _pcr <= 0.7: _neg += 1   # low PCR = complacency

    _macro_bias = (
        "POSITIVE" if _pos > _neg + 1 else
        "NEGATIVE" if _neg > _pos + 1 else
        "NEUTRAL"
    )
    macro_data = {
        "macro_bias":  _macro_bias,
        "cpi_trend":   ("RISING" if _cpi and _cpi > 5 else
                        ("FALLING" if _cpi and _cpi < 4 else "STABLE")) if _cpi is not None else None,
        "wpi_trend":   None,   # extend when WPI ingested
        **_macro_kv,
    }

    # ── Market news — rule-based sentiment from category + headline ───────────
    # market_news uses news_date (not date). Include headline for keyword classification.
    news_rows = (
        sb.table("market_news")
          .select("category,impact_type,headline")
          .eq("news_date", today)
          .execute().data
    )
    if not news_rows:
        news_rows = (
            sb.table("market_news")
              .select("category,impact_type,headline")
              .order("news_date", desc=True)
              .limit(50)
              .execute().data
        )
    bearish_news = bullish_news = 0
    for _nrow in (news_rows or []):
        _sentiment, _impact_level = _classify_news_sentiment(
            _nrow.get("category", ""),
            _nrow.get("impact_type", ""),
            _nrow.get("headline", ""),
        )
        if _impact_level in ("HIGH", "MEDIUM"):
            if _sentiment == "BEARISH":  bearish_news += 1
            elif _sentiment == "BULLISH": bullish_news += 1

    logger.info(
        f"  Loaded: {len(symbols)} symbols | {len(stock_map)} stock_data | "
        f"{len(hist_rows)} history | {len(sector_rank)} sectors | "
        f"regime={regime.get('regime','?')} | {len(open_syms)} open positions | "
        f"blocked={len(blocked_syms)} | FII={fii_data.get('fii_flag','?')} | "
        f"news B:{bearish_news}/b:{bullish_news}"
    )

    # ── Field coverage validation — must be last in load_data ────────────────
    field_coverage = validate_upstream_fields(stock_map, symbols)
    # Attach to return dict so compute_all and main() can inspect it
    
    return {
        "msl_map":      msl_map,
        "stock_map":    stock_map,
        "history_map":  dict(history_map),
        "regime":       regime,
        "sector_rank":  sector_rank,
        "open_syms":    open_syms,
        "blocked_syms": blocked_syms,
        "asm_syms":     dict(asm_syms),
        "fii_data":     fii_data,
        "global_cues":  global_cues,
        "macro_data":   macro_data,
        "bearish_news": bearish_news,
        "bullish_news": bullish_news,
        "field_coverage": field_coverage,
    }


# ─────────────────────────────────────────────────────────────────────────────
# REGIME CONTEXT — the foundation that calibrates all other computations
# ─────────────────────────────────────────────────────────────────────────────

def build_regime_context(regime: dict, global_cues: dict, macro_data: dict,
                          fii_data: dict, bearish_news: int, bullish_news: int) -> dict:
    """
    Resolve current regime and return threshold multipliers.
    Uses predicted_regime if fresh (< 24h), otherwise manual regime.

    v3 adds: global_cues + macro_data + fii_data → macro_overlay
    The macro_overlay modulates regime_boost and vix_penalty without
    changing the core regime label (that's compute_regime's job).

    WHY RSI THRESHOLDS VARY BY REGIME:
    RSI 85 in TRENDING + ADX 40 = momentum continuation. Every big winner
    stays at RSI 80+ for weeks in a bull run. Static RSI=70 sell signal
    would exit every winner too early.
    RSI 85 in RISK OFF = exhaustion. Same number, opposite meaning.
    """
    manual    = regime.get("regime", "NEUTRAL")
    predicted = regime.get("predicted_regime")
    pred_at   = regime.get("regime_predicted_at")

    active_regime = manual
    if predicted and pred_at:
        try:
            pred_dt = datetime.fromisoformat(
                pred_at.replace("Z", "+00:00") if isinstance(pred_at, str) else str(pred_at)
            )
            if pred_dt.tzinfo is None:
                pred_dt = pred_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() < 86400:
                active_regime = predicted
        except Exception:
            pass

    vix     = float(regime.get("india_vix") or 0)
    breadth = float(regime.get("above_200dma_pct") or 50)

    rsi_extended = {
        "TRENDING":  80,
        "RISK ON":   78,
        "NEUTRAL":   72,
        "CAUTION":   67,
        "RISK OFF":  62,
    }.get(active_regime, 72)

    momentum_decay = {
        "TRENDING":  0.85,
        "RISK ON":   0.80,
        "NEUTRAL":   0.70,
        "CAUTION":   0.55,
        "RISK OFF":  0.40,
    }.get(active_regime, 0.70)

    regime_boost = {
        "TRENDING":  8,
        "RISK ON":   5,
        "NEUTRAL":   0,
        "CAUTION":  -5,
        "RISK OFF": -12,
    }.get(active_regime, 0)

    # VIX penalty
    vix_penalty = 0
    if vix >= 25:   vix_penalty = 10
    elif vix >= 20: vix_penalty = 5
    elif vix >= 18: vix_penalty = 2

    # Breadth penalty
    breadth_penalty = 0
    if breadth < 35:   breadth_penalty = 8
    elif breadth < 45: breadth_penalty = 4

    # ── NEW v3: Macro overlay ─────────────────────────────────────────────────
    # global_cues: crude rising in RISK ON = mild positive (infra/oil stocks)
    #              GIFT Nifty gap: large positive gap = momentum confirmation
    # macro_data:  CPI_RISING = compression risk on consumption stocks
    #              macro_bias = POSITIVE/NEGATIVE/NEUTRAL
    macro_boost = 0
    gift_gap = float(global_cues.get("gift_nifty_gap_pct") or 0)
    crude_dir = (global_cues.get("crude_direction") or "FLAT").upper()
    global_sentiment = (global_cues.get("global_sentiment") or "NEUTRAL").upper()
    macro_bias = (macro_data.get("macro_bias") or "NEUTRAL").upper()

    if gift_gap > 0.5:    macro_boost += 2   # positive GIFT Nifty = bullish open
    elif gift_gap < -0.5: macro_boost -= 3   # negative GIFT = risk-off open
    # NEW — handles all five values _derive_global_signals can return:
    if global_sentiment == "RISK_ON":           macro_boost += 2
    elif global_sentiment == "MILDLY_BULLISH":  macro_boost += 1
    elif global_sentiment == "RISK_OFF":        macro_boost -= 3
    elif global_sentiment == "MILDLY_BEARISH":  macro_boost -= 2
    # NEUTRAL: 0 (no change)

    # FII systemic flow overlay
    fii_flag   = (fii_data.get("fii_flag") or "NEUTRAL").upper()
    fii_net_5d = float(fii_data.get("fii_net_5d") or 0)
    fii_overlay = 0
    if fii_flag == "BUYING" and fii_net_5d > 500:    fii_overlay = 3    # strong FII buying
    elif fii_flag == "BUYING":                        fii_overlay = 1
    elif fii_flag == "SELLING" and fii_net_5d < -500: fii_overlay = -4  # heavy FII selling
    elif fii_flag == "SELLING":                       fii_overlay = -2

    # News sentiment overlay (dampens score in heavy bearish news day)
    news_overlay = 0
    net_news = bullish_news - bearish_news
    if net_news >= 3:    news_overlay = 1
    elif net_news <= -3: news_overlay = -2

    # Cap macro_boost contribution: ±5 pts total
    macro_boost = max(-5, min(macro_boost + fii_overlay + news_overlay, 5))

    return {
        "regime":            active_regime,
        "rsi_extended":      rsi_extended,
        "momentum_decay":    momentum_decay,
        "regime_boost":      regime_boost,
        "vix_penalty":       vix_penalty,
        "breadth_penalty":   breadth_penalty,
        "macro_boost":       macro_boost,
        "is_bull":           active_regime in ("TRENDING", "RISK ON"),
        "is_bear":           active_regime in ("RISK OFF",),
        "vix":               vix,
        "breadth":           breadth,
        "fii_flag":          fii_flag,
        "gift_gap":          gift_gap,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL FUNCTIONS — one per concept, all fields evaluated
# ─────────────────────────────────────────────────────────────────────────────

def get_rsi_extended_threshold(regime_ctx: dict, adx: float) -> float:
    """
    The single most important function in this script.
    Base regime threshold + ADX bonus.
    ADX > 35 in TRENDING: RSI 87 is not exhaustion.
    ADX < 15 in NEUTRAL:  RSI 72 IS extended.
    """
    base = regime_ctx["rsi_extended"]
    adx_bonus = 7 if adx >= 38 else (4 if adx >= 28 else (1 if adx >= 20 else 0))
    return base + adx_bonus


def compute_ma_alignment(s: dict) -> dict:
    close   = float(s.get("close") or s.get("current_price") or 0)
    ema_10  = float(s.get("ema_10") or 0)
    ema_20  = float(s.get("ema_20") or 0)
    ema_50  = float(s.get("ema_50") or 0)
    sma_20  = float(s.get("sma_20") or 0)
    sma_50  = float(s.get("sma_50") or 0)
    sma_200 = float(s.get("sma_200") or 0)

    score = 0
    levels = []

    if close > ema_10 > 0:
        score += 1
        levels.append("P>E10")

    if ema_10 > ema_20 > 0:
        score += 1
        levels.append("E10>E20")

    if ema_20 > ema_50 > 0:
        score += 1
        levels.append("E20>E50")

    if ema_50 > sma_50 > 0:
        score += 1
        levels.append("E50>S50")

    if sma_50 > sma_200 > 0:
        score += 1
        levels.append("S50>S200")

    if sma_20 > sma_50 > 0:
        score += 1
        levels.append("S20>S50")

    return {
        "ma_alignment_score": score,
        "ma_levels": ",".join(levels)
    }


def compute_ha_signal(s: dict) -> dict:
    """
    Heikin Ashi close position ratio.
    > 0.72: strongly bullish  > 0.62: bullish  > 0.45: neutral
    < 0.32: bearish  else: strongly bearish
    """
    ha_high  = float(s.get("ha_high") or 0)
    ha_low   = float(s.get("ha_low") or 0)
    ha_close = float(s.get("ha_close") or 0)

    if not ha_high or not ha_low or ha_high <= ha_low:
        return {"ha_ratio": None, "ha_signal": "NEUTRAL", "ha_score": 50}

    ratio = (ha_close - ha_low) / (ha_high - ha_low)

    if ratio >= 0.72:   signal, score = "STRONGLY_BULLISH", 90
    elif ratio >= 0.62: signal, score = "BULLISH",          72
    elif ratio >= 0.45: signal, score = "NEUTRAL",          52
    elif ratio >= 0.32: signal, score = "BEARISH",          32
    else:               signal, score = "STRONGLY_BEARISH", 15

    return {"ha_ratio": round(ratio, 3), "ha_signal": signal, "ha_score": score}


def compute_bb_context(s: dict) -> dict:
    """
    Bollinger Band: squeeze detection + position.
    BB Squeeze: (bb_upper - bb_lower) / close < 5% = pre-breakout coil.
    BB Position: where in the band? > 85% = riding upper band (strong trend).
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    bb_upper = float(s.get("bb_upper") or 0)
    bb_lower = float(s.get("bb_lower") or 0)

    if not close or not bb_upper or not bb_lower or bb_upper <= bb_lower:
        return {"bb_width_pct": None, "bb_position_pct": None,
                "bb_squeeze": False, "bb_context": "UNKNOWN"}

    bb_width_pct = round((bb_upper - bb_lower) / close * 100, 2)
    bb_position  = round((close - bb_lower) / (bb_upper - bb_lower) * 100, 1)
    squeeze      = bb_width_pct < 5.0

    if squeeze and bb_position > 70:   ctx = "SQUEEZE_BULLISH"
    elif squeeze and bb_position < 35: ctx = "SQUEEZE_AT_LOW"
    elif bb_position > 85:             ctx = "RIDING_UPPER"
    elif bb_position > 65:             ctx = "UPPER_HALF"
    elif bb_position > 40:             ctx = "MIDDLE"
    elif bb_position > 20:             ctx = "LOWER_HALF"
    else:                              ctx = "NEAR_LOWER"

    return {
        "bb_width_pct":    bb_width_pct,
        "bb_position_pct": bb_position,
        "bb_squeeze":      squeeze,
        "bb_context":      ctx,
    }


def compute_psar_context(s: dict) -> dict:
    """
    Dual trend confirmation: Supertrend + Parabolic SAR.
    Both agreeing = highest trend conviction.
    Cushion = how far before a flip? Large cushion = trend has room.
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
        result["dual_trend_confirmed"] = above_st and result["psar_above"]

    return result


def compute_macd_context(s: dict) -> dict:
    """
    MACD direction + crossing detection.
    For swing trading: histogram direction > level.
    Rising + positive histogram = accelerating momentum.
    """
    macd_h   = float(s.get("macd_hist") or 0)
    macd_l   = float(s.get("macd_line") or 0)
    macd_sig = float(s.get("macd_signal") or 0)

    if macd_h > 0.5:    direction = "POSITIVE_STRONG"
    elif macd_h > 0.05: direction = "POSITIVE"
    elif macd_h > -0.05: direction = "NEUTRAL"
    elif macd_h > -0.5: direction = "NEGATIVE"
    else:               direction = "NEGATIVE_STRONG"

    crossing_up   = macd_l > macd_sig and macd_h > 0
    crossing_down = macd_l < macd_sig and macd_h < 0

    score_map = {"POSITIVE_STRONG": 90, "POSITIVE": 68, "NEUTRAL": 48,
                 "NEGATIVE": 25, "NEGATIVE_STRONG": 8}
    macd_score = score_map.get(direction, 48)
    if crossing_up:   macd_score = min(macd_score + 12, 100)
    if crossing_down: macd_score = max(macd_score - 12, 0)

    return {
        "macd_direction":   direction,
        "macd_score":       macd_score,
        "macd_crossing_up": crossing_up,
    }


def compute_vwap_context(s: dict) -> dict:
    """
    Multi-timeframe VWAP alignment = institutional cost basis analysis.
    Above all VWAPs = all institutional cohorts are profitable = strong support.
    Extended above 50d VWAP = stretched from institutional cost basis.
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    vwap     = float(s.get("vwap") or 0)
    vwap_20d = float(s.get("vwap_20d") or 0)
    vwap_50d = float(s.get("vwap_50d") or 0)

    if not close:
        return {"vwap_alignment": "UNKNOWN", "vwap_score": 50,
                "above_vwap_today": False, "dist_vwap_20d_pct": None,
                "dist_vwap_50d_pct": None}

    above_today = vwap > 0 and close > vwap
    above_20d   = vwap_20d > 0 and close > vwap_20d
    above_50d   = vwap_50d > 0 and close > vwap_50d
    dist_20d    = round((close - vwap_20d) / vwap_20d * 100, 2) if vwap_20d > 0 else None
    dist_50d    = round((close - vwap_50d) / vwap_50d * 100, 2) if vwap_50d > 0 else None
    count       = sum([above_today, above_20d, above_50d])

    if count == 3 and dist_50d and dist_50d > 20: alignment = "ABOVE_ALL_EXTENDED"
    elif count == 3:                               alignment = "ABOVE_ALL"
    elif count == 2 and above_20d:                 alignment = "ABOVE_20D"
    elif count == 1 and above_today:               alignment = "ABOVE_TODAY_ONLY"
    elif count == 0:                               alignment = "BELOW_ALL"
    else:                                          alignment = "MIXED"

    score_map = {
        "ABOVE_ALL": 85, "ABOVE_ALL_EXTENDED": 65, "ABOVE_20D": 62,
        "ABOVE_TODAY_ONLY": 42, "MIXED": 40, "BELOW_ALL": 20, "UNKNOWN": 50,
    }
    return {
        "vwap_alignment":    alignment,
        "vwap_score":        score_map.get(alignment, 50),
        "above_vwap_today":  above_today,
        "dist_vwap_20d_pct": dist_20d,
        "dist_vwap_50d_pct": dist_50d,
    }


def compute_volume_trend(s: dict) -> dict:
    vol_20d  = float(s.get("avg_vol_20d") or 0)
    vol_50d  = float(s.get("avg_vol_50d") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    value_cr = float(s.get("value_cr") or 0)

    vol_trend_ratio = round(vol_20d / vol_50d, 3) if vol_50d > 0 and vol_20d > 0 else None

    if vol_trend_ratio is None:     trend = "UNKNOWN"
    elif vol_trend_ratio >= 1.20:   trend = "EXPANDING"
    elif vol_trend_ratio >= 0.90:   trend = "STABLE"
    elif vol_trend_ratio >= 0.75:   trend = "CONTRACTING"
    else:                           trend = "DECLINING"

    # FIX: value_cr=0 means field not populated — tag as UNKNOWN, not VERY_LOW
    if value_cr <= 0:         liquidity = "UNKNOWN"
    elif value_cr >= 500:     liquidity = "HIGH"
    elif value_cr >= 100:     liquidity = "MEDIUM"
    elif value_cr >= 30:      liquidity = "LOW"
    else:                     liquidity = "VERY_LOW"

    return {
        "volume_trend":      trend,
        "vol_trend_ratio":   vol_trend_ratio,
        "liquidity_quality": liquidity,
        "value_cr":          value_cr,
    }


def compute_weekly_structure(s: dict) -> dict:
    """
    Weekly HH + HL = textbook uptrend. For swing trading, HL is MORE
    important — shallower pullbacks = institutional demand at every dip.
    """
    wk_hh = bool(s.get("wk_hi_high"))
    wk_hl = bool(s.get("wk_hi_low"))

    if wk_hh and wk_hl:          structure, score = "STRONG",        90
    elif wk_hl and not wk_hh:    structure, score = "CONSOLIDATING", 65
    elif wk_hh and not wk_hl:    structure, score = "CAUTION",       42
    else:                         structure, score = "WEAK",          15

    return {"weekly_structure": structure, "weekly_structure_score": score}


def compute_fundamental_quality(s: dict) -> dict:
    """
    Fundamentals are a FILTER for swing trading, not a driver.
    Penalise collapsing earnings — a technically perfect setup with
    negative earnings trend carries hidden blow-up risk.
    """
    quarterly = float(s.get("quarterly_net_profit") or 0)
    q_var     = float(s.get("quarterly_variance") or 0)
    ttm       = float(s.get("ttm_net_profit") or 0)
    eps       = float(s.get("eps") or 0)

    if quarterly > 0 and q_var > 10:      quality, penalty = "IMPROVING",    0
    elif quarterly > 0 and q_var >= -10:  quality, penalty = "STABLE",       0
    elif quarterly > 0 and q_var < -10:   quality, penalty = "DECLINING",    5
    elif quarterly <= 0 and ttm > 0:      quality, penalty = "WEAK",         8
    elif quarterly <= 0 and ttm <= 0:     quality, penalty = "LOSS_MAKING",  12
    else:                                  quality, penalty = "UNKNOWN",      0

    return {"fundamental_quality": quality, "fundamental_penalty": penalty}


def compute_stoch_context(s: dict, regime_ctx: dict, adx: float) -> dict:
    """
    Stochastic is HIGHLY context-dependent.
    In ADX>22 + bull regime: stoch>80 = NORMAL (trend momentum).
    In ranging market: stoch>80 = overbought.
    Sweet spot for entry: stoch 40-75 in uptrend (has room to run).
    """
    stoch    = float(s.get("stoch") or 50)
    is_trend = adx >= 22 and regime_ctx.get("is_bull", False)

    if is_trend:
        if stoch >= 80:   stoch_ctx, stoch_score = "STRONG_TREND",   78
        elif stoch >= 60: stoch_ctx, stoch_score = "TRENDING",       85   # sweet spot
        elif stoch >= 40: stoch_ctx, stoch_score = "MID_TREND",      70
        elif stoch >= 20: stoch_ctx, stoch_score = "PULLBACK",       65   # entry opp
        else:             stoch_ctx, stoch_score = "OVERSOLD_TREND", 55
    else:
        if stoch >= 80:   stoch_ctx, stoch_score = "OVERBOUGHT", 30
        elif stoch >= 65: stoch_ctx, stoch_score = "ELEVATED",   50
        elif stoch >= 40: stoch_ctx, stoch_score = "NEUTRAL",    65
        elif stoch >= 20: stoch_ctx, stoch_score = "LOW",        72
        else:             stoch_ctx, stoch_score = "OVERSOLD",   60

    return {"stoch_context": stoch_ctx, "stoch_score": stoch_score}


# ─────────────────────────────────────────────────────────────────────────────
# CORE SWING TRADE SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum_state(s: dict, regime_ctx: dict) -> str:
    """
    Regime-aware momentum quality: HOT / BUILDING / STABLE / WEAK.
    HOT requires RSI in the "sweet spot" (above hot_min but below extended threshold).
    In TRENDING + ADX 40, HOT requires RSI > 72 to avoid calling everything HOT.
    """
    rsi_d    = float(s.get("rsi_daily") or 0)
    rsi_w    = float(s.get("rsi_weekly") or 0)
    adx      = float(s.get("adx") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    above_50 = bool(s.get("above_sma50"))
    macd_h   = float(s.get("macd_hist") or 0)
    above_st = bool(s.get("above_st"))
    wk_hh    = bool(s.get("wk_hi_high"))
    wk_hl    = bool(s.get("wk_hi_low"))

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    rsi_hot_min = 57 if regime_ctx["is_bull"] else 62

    hot = sum([
        rsi_d >= rsi_hot_min and rsi_d < extended_thresh,
        rsi_w >= 56,
        adx >= 22 and di_plus > di_minus and (di_plus - di_minus) >= 6,
        vol_r >= 1.35,
        above_50,
        macd_h > 0,
        above_st,
        wk_hh and wk_hl,
    ])
    if hot >= 6: return "HOT"

    weak = sum([
        rsi_d < 46,
        di_minus > di_plus and adx > 18,
        not above_50,
        macd_h < -0.3,
        vol_r < 0.85,
        not above_st,
    ])
    if weak >= 3: return "WEAK"

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
    EXHAUSTED → EXTENDED → EXPANSION → EARLY → FLAT
    Regime-aware: in TRENDING, EXPANSION can last months.
    History used to detect persistent extension (≥4 of last 5 days EXTENDED/EXHAUSTED).
    """
    rsi_d      = float(s.get("rsi_daily") or 0)
    rsi_m      = float(s.get("rsi_monthly") or 0)
    adx        = float(s.get("adx") or 0)
    di_plus    = float(s.get("di_plus") or 0)
    di_minus   = float(s.get("di_minus") or 0)
    dist_sma50 = float(s.get("dist_sma50") or 0)
    above_50   = bool(s.get("above_sma50"))
    ret_1m     = float(s.get("ret_1m") or 0)

    extended_thresh  = get_rsi_extended_threshold(regime_ctx, adx)
    exhausted_thresh = extended_thresh + 7

    exhausted_signals = sum([
        rsi_d > exhausted_thresh,
        rsi_m > 78 and rsi_m > 0,
        dist_sma50 > 25,
        ret_1m > 35,
    ])
    persistent_extended = sum(
        1 for h in hist[:5] if h.get("momentum_phase") in ("EXTENDED", "EXHAUSTED")
    ) >= 4
    if exhausted_signals >= 2 or (exhausted_signals >= 1 and persistent_extended):
        return "EXHAUSTED"

    if adx < 17 or abs(di_plus - di_minus) < 3:
        return "FLAT"

    extended_dist = 18 if regime_ctx["is_bull"] else 12
    if (rsi_d > extended_thresh or dist_sma50 > extended_dist) and adx > 20:
        return "EXTENDED"

    if adx >= 18 and di_plus > di_minus and rsi_d >= 54 and above_50:
        return "EXPANSION"

    if adx >= 14 and di_plus > di_minus and above_50 and rsi_d >= 47:
        return "EARLY"

    return "FLAT"


def compute_velocity_state(s: dict, hist: list) -> str:
    """
    Is momentum pace ACCELERATING, FLAT, or DECELERATING?
    ACCELERATING = enter/add with full size.
    DECELERATING = reduce or tighten SL.
    """
    ret_1w = float(s.get("ret_1w") or 0)
    ret_1m = float(s.get("ret_1m") or 0)
    ret_3m = float(s.get("ret_3m") or 0)

    if ret_1m == 0 and ret_3m == 0:
        return "FLAT"

    weekly_pace    = ret_1m / 4.0 if ret_1m != 0 else 0
    quarterly_pace = ret_3m / 3.0 if ret_3m != 0 else 0

    week_accel  = (ret_1w > weekly_pace * 1.40) if weekly_pace > 0.5 else (ret_1w > 2.5)
    week_decel  = (ret_1w < weekly_pace * 0.40) if weekly_pace > 0.5 else (ret_1w < -1.0)
    month_accel = (ret_1m > quarterly_pace * 1.35) if quarterly_pace > 0.5 else (ret_1m > 4.0)
    month_decel = (ret_1m < quarterly_pace * 0.40) if quarterly_pace > 0.5 else (ret_1m < -3.0)
    macd_accel  = float(s.get("macd_hist") or 0) > 0

    accel = sum([week_accel, month_accel, macd_accel])
    decel = sum([week_decel, month_decel])

    if accel >= 2 and decel == 0: return "ACCELERATING"
    if decel >= 2:                 return "DECELERATING"
    if accel >= 1 and decel == 0: return "ACCELERATING"
    if decel >= 1 and accel == 0: return "DECELERATING"
    return "FLAT"


def compute_trend_maturity(s: dict, regime_ctx: dict) -> str:
    """
    Regime-aware trend maturity: FRESH / DEVELOPING / LATE / EXHAUSTED / FLAT.
    FRESH = maximum runway (recent SMA50 cross, low RSI, building ADX).
    EXHAUSTED = avoid new entries, manage exits.

    BUG #11 FIX: FRESH check now includes guard against established strong trends.
    """
    rsi_d      = float(s.get("rsi_daily") or 0)
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
    di_spread        = di_plus - di_minus

    # EXHAUSTED — multiple overextension dimensions
    exhausted_signals = sum([
        rsi_d > exhausted_thresh,
        rsi_m > 0 and rsi_m > 78,
        dist_sma50 > 22,
        ret_6m > 60,
        ret_1m > 20 and rsi_d > extended_thresh,
    ])
    if exhausted_signals >= 2:
        return "EXHAUSTED"

    # FLAT — no directional conviction
    if not above_50 or adx < 14 or di_plus <= di_minus:
        return "FLAT"

    # LATE — trend mature, extension building
    late_thresh = extended_thresh - 6
    late_signals = sum([
        rsi_d > late_thresh,
        dist_sma50 > 13,
        ret_6m > 30,
        ret_1m > 12,
        rsi_m > 0 and rsi_m > 68,
        not wk_hl and wk_hh,
    ])
    if late_signals >= 2:
        return "LATE"

    # FRESH — trend just initiated, maximum runway
    # BUG #11 FIX: guard clause excludes stocks with strong established trends
    rsi_fresh_max = min(extended_thresh - 12, 62)
    has_strong_trend = adx >= 22 and di_spread >= 8   # already an established trend
    if not has_strong_trend:
        fresh_signals = sum([
            14 <= adx <= 26,
            dist_sma50 < 6,
            rsi_d < rsi_fresh_max,
            sma50_200,
            wk_hl and not wk_hh,
        ])
        if fresh_signals >= 3:
            return "FRESH"

    # DEVELOPING — confirmed active mid-cycle trend
    if above_50 and adx > 16 and di_spread >= 4 and rsi_d > 50:
        return "DEVELOPING"

    return "FLAT"


def compute_lifecycle(s: dict, hist: list, regime_ctx: dict,
                       struct_edge: str = "NO",
                       momentum_phase: str = "",
                       velocity_state: str = "",
                       trend_maturity: str = "",
                       entry_timing_type: str = "",
                       in_position: bool = False) -> str:
    """
    Action signal: EXIT / REDUCE / HOLD / ADD / DEVELOPING.
    EXIT requires convergence (3 signals, or 2 in RISK OFF).
    REDUCE only for held positions with maturing/decelerating trend.
    ADD requires strict EXPANSION + ACCELERATING + non-extended.
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
    exit_threshold  = 2 if regime_ctx.get("is_bear") else 3

    exit_signals = sum([
        rsi_d < 42,
        di_minus > di_plus and adx > 18,
        not above_50,
        macd_h < -0.5,
        not above_st,
        psar > 0 and close > 0 and close < psar,
        not wk_hl,
        sma_50 > 0 and close < sma_50 * 0.97,
        struct_edge == "NO" and in_position,
    ])
    if exit_signals >= exit_threshold:
        return "EXIT"

    if in_position:
        reduce_signals = sum([
            trend_maturity in ("LATE", "EXHAUSTED"),
            velocity_state == "DECELERATING",
            rsi_d > extended_thresh,
            entry_timing_type == "EXTENDED",
            not wk_hl and above_50,
        ])
        if reduce_signals >= 2:
            return "REDUCE"

    add_conditions = (
        momentum_phase == "EXPANSION"
        and velocity_state == "ACCELERATING"
        and trend_maturity not in ("LATE", "EXHAUSTED")
        and entry_timing_type not in ("EXTENDED", "WAIT")
        and struct_edge == "YES"
        and rsi_d < extended_thresh
    )
    if add_conditions:
        return "ADD"

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

    return "DEVELOPING"


def compute_struct_edge(s: dict, ma_ctx: dict, bb_ctx: dict) -> str:
    """
    Tiered structural convergence.
    MANDATORY: above_sma50 + sma50_gt_200 + above_st — all three required.
    QUALITY: 5 of 10 additional signals required for YES.
    """
    above_50  = bool(s.get("above_sma50"))
    sma50_200 = bool(s.get("sma50_gt_200"))
    above_st  = bool(s.get("above_st"))

    if not above_50:   return "NO"
    if not sma50_200:  return "NO"
    if not above_st:   return "NO"

    consol   = float(s.get("consol_range") or 999)
    delivery = float(s.get("delivery_pct") or 0)
    rs       = float(s.get("rs_vs_nifty") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    adx      = float(s.get("adx") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)
    wk_hh    = bool(s.get("wk_hi_high"))
    wk_hl    = bool(s.get("wk_hi_low"))
    ma_score = ma_ctx.get("ma_alignment_score", 0)
    squeeze  = bb_ctx.get("bb_squeeze", False)
    close    = float(s.get("close") or 0)
    psar     = float(s.get("psar") or 0)
    psar_above = close > psar if (close > 0 and psar > 0) else False

    quality_signals = sum([
        consol < 12,
        delivery >= 45,
        rs > 2.0,
        vol_r >= 1.2,
        adx >= 18 and di_plus > di_minus,
        wk_hl,
        wk_hh and wk_hl,          # full structure counts as extra signal
        ma_score >= 4,
        squeeze,
        psar_above,
    ])
    return "YES" if quality_signals >= 5 else "NO"


def compute_reentry_mode(s: dict) -> str:
    """
    ELIGIBLE: pulled back 2-10% from 30d high, above SMA50, RSI > 44, trend intact.
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

    pullback_pct  = (high_30d - close) / high_30d * 100
    structural_ok = wk_hl or above_st

    if 2.0 <= pullback_pct <= 10.0 and structural_ok:
        return "ELIGIBLE"
    return "NO"


def compute_entry_zones(s: dict, strategy: str, regime_ctx: dict) -> tuple:
    """
    Strategy-aware + regime-aware entry zone.
    CTL/TPO: SMA50/VWAP_20d anchored.
    SBS: 30d high breakout zone.
    Default: VWAP_20d anchored.
    Bearish regime = more conservative (deeper pullback required before entry).
    """
    close    = float(s.get("close") or s.get("current_price") or 0)
    sma_50   = float(s.get("sma_50") or 0)
    sma_20   = float(s.get("sma_20") or 0)
    ema_20   = float(s.get("ema_20") or 0)
    vwap_20d = float(s.get("vwap_20d") or 0)
    atr_14   = float(s.get("atr_14") or 0)
    high_30d = float(s.get("high_30d") or 0)

    if not close or not atr_14:
        return None, None

    regime_factor = 1.12 if regime_ctx["is_bear"] else (0.95 if regime_ctx["is_bull"] else 1.0)
    strat = (strategy or "").upper()

    if "CTL" in strat or "TPO" in strat:
        anchor = max(sma_50, vwap_20d) if sma_50 > 0 and vwap_20d > 0 else (sma_50 or vwap_20d or close)
        if anchor > 0:
            ez_low  = round(anchor * (1 - 0.012 * regime_factor), 2)
            ez_high = round(anchor * (1 + 0.020 / regime_factor), 2)
        else:
            ez_low  = round(close - atr_14 * 1.5, 2)
            ez_high = round(close - atr_14 * 0.4, 2)

    elif "SBS" in strat:
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
        anchor  = vwap_20d if vwap_20d > 0 else (ema_20 if ema_20 > 0 else (sma_20 if sma_20 > 0 else close))
        ez_low  = round(anchor * (1 - 0.010 * regime_factor), 2)
        ez_high = round(anchor + atr_14 * (0.7 / regime_factor), 2)

    # Safety: price has fallen far below zone → shift zone toward current price
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
    Entry timing: OPTIMAL / REENTRY / APPROACHING / CHASING / EXTENDED / WAIT.

    BUG #10 FIX: OPTIMAL now checked BEFORE REENTRY.
    A price inside the entry zone is always OPTIMAL — even if it also qualifies
    as a pullback. OPTIMAL has the best R:R; REENTRY is the fallback.
    """
    close = float(s.get("close") or s.get("current_price") or 0)
    if not close or not ez_low or not ez_high:
        return "CHASING"

    above_50 = bool(s.get("above_sma50"))
    above_st = bool(s.get("above_st"))
    high_30d = float(s.get("high_30d") or 0)
    rsi_d    = float(s.get("rsi_daily") or 0)
    atr_14   = float(s.get("atr_14") or (ez_high - ez_low) or 1)

    # BUG #10 FIX: OPTIMAL first
    if ez_low <= close <= ez_high:
        return "OPTIMAL"

    # REENTRY: pulled back from 30d high 2-8%, trend intact
    if reentry_mode == "ELIGIBLE" and momentum_phase not in ("EARLY", "FLAT", ""):
        return "REENTRY"

    if high_30d > 0 and above_50 and above_st:
        pullback_pct = (high_30d - close) / high_30d * 100
        if 2.0 <= pullback_pct <= 8.0 and rsi_d >= 44:
            return "REENTRY"

    # APPROACHING: below zone but within 1 ATR
    if close < ez_low and close >= ez_low - atr_14:
        return "APPROACHING"

    # Far below zone
    if close < ez_low - atr_14:
        return "WAIT"

    # Above zone
    dist_from_high = (close - ez_high) / ez_high
    if dist_from_high <= 0.08: return "CHASING"
    return "EXTENDED"


def compute_price_location(s: dict, ez_low: float, ez_high: float) -> tuple:
    close = float(s.get("close") or s.get("current_price") or 0)
    if not close or not ez_low:
        return "UNKNOWN", None, None
    ez_high_safe   = ez_high or ez_low * 1.02
    dist_entry_pct = round((close - ez_low) / ez_low * 100, 2)
    if close < ez_low * 0.98:              location = "Below"
    elif ez_low <= close <= ez_high_safe:  location = "Inside"
    elif close <= ez_high_safe * 1.006:    location = "At Zone"
    else:                                   location = "Above"
    return location, round(dist_entry_pct, 2), round(dist_entry_pct, 2)


def compute_msl_history_context(sym: str, hist: list) -> dict:
    if not hist:
        return {"days_in_list": 0, "rank_vel_3d": None, "score_vel_5d": None,
                "persistent_phase": None}

    days_in_list = len(set(h.get("snapshot_date") for h in hist if h.get("snapshot_date")))
    sorted_hist  = sorted(hist, key=lambda x: x.get("snapshot_date") or "", reverse=True)

    rank_vel_3d = None
    if len(sorted_hist) >= 3:
        r0 = sorted_hist[0].get("base_rank") or sorted_hist[0].get("priority_rank")
        r3 = sorted_hist[2].get("base_rank") or sorted_hist[2].get("priority_rank")
        if r0 is not None and r3 is not None:
            rank_vel_3d = round(float(r3) - float(r0), 0)

    score_vel_5d = None
    if len(sorted_hist) >= 5:
        d0 = sorted_hist[0].get("dist_fv_pct")
        d5 = sorted_hist[4].get("dist_fv_pct")
        if d0 is not None and d5 is not None:
            score_vel_5d = round(float(d5) - float(d0), 2)

    # Dominant phase from recent history (useful for AI narrative)
    recent_phases = [h.get("momentum_phase") for h in sorted_hist[:7] if h.get("momentum_phase")]
    persistent_phase = Counter(recent_phases).most_common(1)[0][0] if recent_phases else None

    return {
        "days_in_list":    days_in_list,
        "rank_vel_3d":     rank_vel_3d,
        "score_vel_5d":    score_vel_5d,
        "persistent_phase": persistent_phase,
    }


# ─────────────────────────────────────────────────────────────────────────────
# COMPOSITE SCORING
# ─────────────────────────────────────────────────────────────────────────────

def compute_momentum_score(s: dict, regime_ctx: dict, macd_ctx: dict,
                            stoch_ctx: dict, ha_ctx: dict, weekly_ctx: dict) -> float:
    """
    0-100: Pure momentum quality across ALL oscillators + structural confirmations.
    Regime-aware RSI interpretation is the critical improvement over v1.
    75+ = powerful confirmed momentum state. Below 40 = WEAK, avoid new entries.
    """
    score    = 0.0
    rsi_d    = float(s.get("rsi_daily") or 0)
    rsi_w    = float(s.get("rsi_weekly") or 0)
    rsi_m    = float(s.get("rsi_monthly") or 0)
    adx      = float(s.get("adx") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    hot_min         = 55 if regime_ctx["is_bull"] else 60
    di_spread       = di_plus - di_minus

    # RSI DAILY — regime-aware (25 pts)
    if hot_min <= rsi_d < extended_thresh:              score += 25   # sweet spot
    elif extended_thresh <= rsi_d < extended_thresh + 8: score += 16  # extended, not exhausted
    elif rsi_d >= extended_thresh + 8:                  score += 6    # exhausted RSI
    elif rsi_d >= hot_min - 6:                          score += 14   # building
    elif rsi_d >= 45:                                   score += 7    # weak
    # < 45: 0 pts

    # RSI WEEKLY (15 pts) — multi-timeframe confirmation
    if rsi_w >= 62:   score += 15
    elif rsi_w >= 57: score += 11
    elif rsi_w >= 52: score += 7
    elif rsi_w >= 47: score += 3

    # RSI MONTHLY (8 pts) — strategic trend direction
    if rsi_m >= 62:   score += 8
    elif rsi_m >= 55: score += 5
    elif rsi_m >= 48: score += 2

    # ADX + DIRECTIONAL (22 pts)
    if adx >= 35 and di_plus > di_minus:        score += 22
    elif adx >= 27 and di_spread >= 8:          score += 17
    elif adx >= 22 and di_plus > di_minus:      score += 12
    elif adx >= 17 and di_plus > di_minus:      score += 7
    elif di_plus > di_minus:                     score += 3

    # MACD (12 pts)
    score += macd_ctx.get("macd_score", 50) * 0.12

    # STOCHASTIC (8 pts) — context-aware
    score += stoch_ctx.get("stoch_score", 50) * 0.08

    # HEIKIN ASHI (5 pts)
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
    Zone proximity is critical — even a perfect stock 15% above zone is not valid.
    """
    score    = 0.0
    rsi_d    = float(s.get("rsi_daily") or 0)
    vol_r    = float(s.get("vol_ratio") or 0)
    delivery = float(s.get("delivery_pct") or 0)
    adx      = float(s.get("adx") or 0)
    above_st = bool(s.get("above_st"))
    wk_hh    = bool(s.get("wk_hi_high"))
    wk_hl    = bool(s.get("wk_hi_low"))
    close    = float(s.get("close") or s.get("current_price") or 0)

    extended_thresh = get_rsi_extended_threshold(regime_ctx, adx)
    rsi_sweet_lo    = 52 if regime_ctx["is_bull"] else 55
    rsi_sweet_hi    = extended_thresh - 4

    # RSI in entry range (18 pts)
    if rsi_sweet_lo <= rsi_d <= rsi_sweet_hi: score += 18
    elif rsi_d >= rsi_sweet_lo - 5:          score += 10
    elif rsi_d >= 45:                        score += 5
    if rsi_d > extended_thresh:
        excess = rsi_d - extended_thresh
        score -= min(excess * 1.5, 12)

    # Volume (14 pts)
    if vol_r >= 2.5:   score += 14
    elif vol_r >= 1.8: score += 11
    elif vol_r >= 1.3: score += 7
    elif vol_r >= 1.0: score += 3

    # Delivery quality (14 pts)
    if delivery >= 65:   score += 14
    elif delivery >= 55: score += 11
    elif delivery >= 45: score += 7
    elif delivery >= 35: score += 3

    # MA alignment (10 pts)
    score += ma_ctx.get("ma_alignment_score", 0) * (10 / 6)

    # PSAR + Supertrend (8 pts)
    if psar_ctx.get("dual_trend_confirmed"): score += 8
    elif above_st:                            score += 4
    st_cushion = psar_ctx.get("st_cushion_pct") or 0
    if st_cushion >= 5: score += 2

    # Weekly structure (8 pts)
    if wk_hh and wk_hl: score += 8
    elif wk_hl:          score += 5
    elif wk_hh:          score += 2

    # VWAP alignment (8 pts)
    score += vwap_ctx.get("vwap_score", 50) * 0.08

    # Zone proximity (14 pts) — CRITICAL
    if ez_low and ez_high and close:
        dist_h = (close - ez_high) / ez_high
        if -0.02 <= dist_h <= 0.01:  score += 14  # inside zone
        elif close < ez_low:          score += 10  # below, approaching
        elif dist_h <= 0.03:         score += 7   # just above
        elif dist_h <= 0.06:         score += 3   # chasing

    # Sector rank bonus (6 pts)
    s_rank = sector_rank.get(sector) if sector else None
    if s_rank:
        if s_rank <= 2:   score += 6
        elif s_rank <= 4: score += 4
        elif s_rank <= 6: score += 2

    return round(min(max(score, 0.0), 100.0), 1)


def compute_institutional_score(s: dict, vwap_ctx: dict, vol_trend: dict,
                                  fii_data: dict) -> float:
    """
    0-100: Is smart money involved?
    BUG #6 FIX: FII/DII systemic flow now integrated.
    Delivery % = strongest per-stock institutional signal.
    FII flag = strongest systemic institutional signal (lifts/depresses all).
    """
    score    = 0.0
    delivery = float(s.get("delivery_pct") or 0)
    rs       = float(s.get("rs_vs_nifty") or 0)
    value_cr = float(s.get("value_cr") or 0)

    # Delivery (35 pts) — actual physical delivery vs intraday squaring
    if delivery >= 72:   score += 35
    elif delivery >= 62: score += 28
    elif delivery >= 55: score += 19
    elif delivery >= 45: score += 11
    elif delivery >= 35: score += 5

    # RS vs Nifty (20 pts) — consistent outperformance = rotation into this stock
    if rs > 20:       score += 20
    elif rs > 12:     score += 15
    elif rs > 6:      score += 11
    elif rs > 2:      score += 6
    elif rs > 0:      score += 2

    # Volume trend (15 pts) — sustained accumulation pattern
    vt = vol_trend.get("volume_trend", "STABLE")
    if vt == "EXPANDING":    score += 15
    elif vt == "STABLE":     score += 8
    elif vt == "CONTRACTING": score += 2

    # Traded value / liquidity (10 pts)
    if value_cr >= 1000:  score += 10
    elif value_cr >= 500: score += 7
    elif value_cr >= 200: score += 5
    elif value_cr >= 75:  score += 2

    # VWAP alignment (8 pts)
    vwap_align = vwap_ctx.get("vwap_alignment", "UNKNOWN")
    if vwap_align == "ABOVE_ALL":          score += 8
    elif vwap_align == "ABOVE_20D":        score += 5
    elif vwap_align == "ABOVE_TODAY_ONLY": score += 2

    # BUG #6 FIX: FII systemic flow (12 pts) — strongest macro signal
    # FII buying = institutional tailwind for all stocks; selling = headwind
    fii_net_5d  = float(fii_data.get("fii_net_5d") or 0)
    fii_net_20d = float(fii_data.get("fii_net_20d") or 0)
    fii_flag    = (fii_data.get("fii_flag") or "NEUTRAL").upper()
    dii_flag    = (fii_data.get("dii_flag") or "NEUTRAL").upper()

    if fii_flag == "BUYING" and fii_net_5d > 1000:    score += 12   # heavy FII inflow
    elif fii_flag == "BUYING" and fii_net_5d > 0:     score += 7
    elif fii_flag == "NEUTRAL":                        score += 2
    elif fii_flag == "SELLING" and fii_net_5d > -500:  score -= 4
    elif fii_flag == "SELLING":                        score -= 7   # heavy FII selling

    # DII counter-flow: DII buying when FII sells = support; adds confidence
    if dii_flag == "BUYING" and fii_flag == "SELLING": score += 4   # DII absorbing FII supply

    return round(min(max(score, 0.0), 100.0), 1)


def compute_breakout_readiness(s: dict, bb_ctx: dict, psar_ctx: dict) -> float:
    """
    0-100: How close to a breakout trigger?
    BB squeeze (new in v2, retained in v3) + consolidation tightness + PSAR proximity.
    """
    score    = 0.0
    consol   = float(s.get("consol_range") or 999)
    delivery = float(s.get("delivery_pct") or 0)
    high_30d = float(s.get("high_30d") or 0)
    close    = float(s.get("close") or s.get("current_price") or 0)
    bk_setup = bool(s.get("breakout_setup"))
    bk_trig  = bool(s.get("bk_trigger"))
    adx      = float(s.get("adx") or 0)
    di_plus  = float(s.get("di_plus") or 0)
    di_minus = float(s.get("di_minus") or 0)
    rs       = float(s.get("rs_vs_nifty") or 0)

    # Consolidation tightness (22 pts)
    if consol < 4:    score += 22
    elif consol < 6:  score += 18
    elif consol < 8:  score += 12
    elif consol < 12: score += 6

    # BB squeeze (18 pts)
    if bb_ctx.get("bb_squeeze"):
        score += 18
        if bb_ctx.get("bb_context") == "SQUEEZE_BULLISH":
            score += 5   # squeeze + near upper band = imminent
    elif bb_ctx.get("bb_width_pct") and float(bb_ctx["bb_width_pct"]) < 8:
        score += 8

    # Proximity to 30d high (20 pts)
    if high_30d > 0 and close > 0:
        pct_to_high = (high_30d - close) / high_30d * 100
        if pct_to_high < 0.3:    score += 20
        elif pct_to_high < 1.5:  score += 16
        elif pct_to_high < 3.5:  score += 11
        elif pct_to_high < 6:    score += 6
        elif pct_to_high < 10:   score += 3

    # Delivery during consolidation (16 pts)
    if delivery >= 65:   score += 16
    elif delivery >= 55: score += 12
    elif delivery >= 45: score += 7

    # ADX directional setup (10 pts)
    if adx >= 20 and di_plus > di_minus and (di_plus - di_minus) >= 8:
        score += 10
    elif di_plus > di_minus:
        score += 4

    # Scanner flags (8 pts)
    if bk_trig:    score += 8
    elif bk_setup: score += 5

    # RS bonus (6 pts)
    if rs > 8:   score += 6
    elif rs > 3: score += 3

    return round(min(score, 100.0), 1)


def compute_risk_score(s: dict, regime_ctx: dict, fund_ctx: dict,
                        msl_row: dict) -> float:
    """
    0-100: Higher = more risk. Used to penalise final_score.
    Regime-aware: same ATR is riskier in RISK OFF than TRENDING.

    BUG #7 FIX: NSE event risk (event_bias, upcoming_news) now integrated.
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

    # RSI overextension (25 pts)
    if rsi_d > extended_thresh + 8:   risk += 25
    elif rsi_d > extended_thresh + 3: risk += 16
    elif rsi_d > extended_thresh:     risk += 8

    # Volatility risk (18 pts)
    atr_mult = 1.25 if regime_ctx["is_bear"] else 1.0
    atr_adj  = atr_pct * atr_mult
    if atr_adj > 6:     risk += 18
    elif atr_adj > 4.5: risk += 13
    elif atr_adj > 3.5: risk += 8
    elif atr_adj > 2.5: risk += 4

    # Distance above SMA50 (15 pts)
    if dist_50 > 25:   risk += 15
    elif dist_50 > 18: risk += 10
    elif dist_50 > 12: risk += 6
    elif dist_50 > 7:  risk += 3

    # 52w high proximity (12 pts)
    if high_52w > 0 and close > 0:
        pct_from_high = (close / high_52w - 1) * 100
        if pct_from_high > -1.5:   risk += 12
        elif pct_from_high > -4.5: risk += 7
        elif pct_from_high > -9:   risk += 3

    # Monthly return extreme (10 pts)
    if ret_1m > 30:   risk += 10
    elif ret_1m > 20: risk += 6
    elif ret_1m > 13: risk += 3

    # Speculative volume (8 pts)
    if delivery < 25:   risk += 8
    elif delivery < 35: risk += 5
    elif delivery < 42: risk += 2

    # Structural breakdown signals (7 pts)
    breakdown = sum([not above_50, not above_st, di_minus > di_plus and adx > 18])
    risk += min(breakdown * 3, 7)

    # Fundamental risk (5 pts)
    risk += fund_ctx.get("fundamental_penalty", 0) * (5 / 12)

    # BUG #7 FIX: Event risk from step 08 (NSE events)
    event_bias   = (msl_row.get("event_bias") or "NEUTRAL").upper()
    upcoming     = (msl_row.get("upcoming_news") or "").lower()
    event_risk   = 0
    if event_bias == "NEGATIVE":                event_risk += 8
    elif event_bias == "POSITIVE":              event_risk -= 2    # positive event = slight tailwind
    if "earnings" in upcoming or "result" in upcoming: event_risk += 5
    if "agm" in upcoming or "dividend" in upcoming:    event_risk += 2
    risk += max(0, min(event_risk, 12))   # capped at 12 pts event risk

    # Regime risk multiplier
    if regime_ctx["is_bear"]:
        risk = min(risk * 1.2, 100)

    return round(min(risk, 100.0), 1)


def compute_holding_score(s: dict, regime_ctx: dict, lifecycle: str,
                           momentum_state: str, risk_score: float,
                           velocity_state: str, ma_ctx: dict,
                           psar_ctx: dict) -> float:
    """
    HOLDING SCORE — separate from entry validity score.
    Answers: "Should I STAY in this position?"
    High = trend intact, momentum persisting. Low = prepare to exit.
    """
    score = 0.0

    # Lifecycle primary (35 pts)
    if lifecycle == "HOLD":         score += 35
    elif lifecycle == "ADD":        score += 35   # also strong hold signal
    elif lifecycle == "REDUCE":     score += 15
    elif lifecycle == "DEVELOPING": score += 18
    elif lifecycle == "EXIT":       score += 0

    # Momentum state (25 pts)
    if momentum_state == "HOT":       score += 25
    elif momentum_state == "BUILDING": score += 18
    elif momentum_state == "STABLE":  score += 10
    elif momentum_state == "WEAK":    score += 0

    # Velocity (20 pts)
    if velocity_state == "ACCELERATING":  score += 20
    elif velocity_state == "FLAT":        score += 12
    elif velocity_state == "DECELERATING": score += 4

    # Structural confirmation (15 pts)
    if psar_ctx.get("dual_trend_confirmed"): score += 15
    elif bool(s.get("above_st")):            score += 8
    if ma_ctx.get("ma_alignment_score", 0) >= 4: score += 5

    # Risk penalty
    risk_penalty = (risk_score / 100) * 20
    score -= risk_penalty

    # Regime multiplier
    regime_adj = regime_ctx.get("regime_boost", 0) * 0.3
    score = score * (0.7 + regime_ctx.get("momentum_decay", 0.70) * 0.3) + regime_adj

    return round(min(max(score, 0.0), 100.0), 1)


def compute_expected_r(s: dict, ez_low: float, ez_high: float) -> float | None:
    """R multiple from ATR-anchored target/SL. Swing target = 3×ATR, SL = 1.5×ATR."""
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
    gap_pct     = abs((close - ez_low) / ez_low * 100)
    if gap_pct < 1.0:
        return 0
    daily_prog  = atr_pct * 0.28
    return min(int(gap_pct / daily_prog) if daily_prog > 0 else 30, 30)


def compute_final_score(momentum_score: float, validity_score: float,
                         institutional_score: float, breakout_readiness: float,
                         risk_score: float, sector: str, sector_rank: dict,
                         base_score: float, regime_ctx: dict,
                         ma_alignment: int, weekly_structure_score: float,
                         fund_penalty: float) -> float:
    """
    Weighted composite — calibrated for 1-4 week swing trading.

    Weights (sums to 1.0):
      Momentum      22% — is it actually moving?
      Validity      20% — is entry actionable right now?
      Trend struct  15% — MA stack intact?
      Institutional 13% — right players involved?
      Breakout      12% — close to trigger?
      Sector         8% — macro tailwind?
      Weekly struct  6% — confirmed on weekly?
      Base score     4% — screener aggregate

    Deductions: risk penalty (-5 to -18) + fundamental penalty (max -6)
                + regime adjustment + VIX/breadth penalties
    v3 addition: macro_boost from global_cues/FII/news overlay (max ±5)
    """
    s_rank = sector_rank.get(sector) if sector else None
    sector_score = (
        100 if s_rank and s_rank <= 2 else
        82  if s_rank and s_rank <= 4 else
        64  if s_rank and s_rank <= 6 else
        46  if s_rank and s_rank <= 8 else
        25
    )
    ma_score_100 = (ma_alignment / 6) * 100
    base_norm    = min(float(base_score or 50), 100)

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

    # Risk penalty — nonlinear
    r       = risk_score or 0
    penalty = 0 if r < 35 else (5 if r < 50 else (10 if r < 65 else (15 if r < 80 else 18)))

    # Fundamental penalty (max -6)
    f_penalty = min(float(fund_penalty or 0) * 0.5, 6)

    # Regime adjustments
    regime_adj   = regime_ctx.get("regime_boost", 0)
    vix_pen      = regime_ctx.get("vix_penalty", 0)
    breadth_pen  = regime_ctx.get("breadth_penalty", 0)
    macro_boost  = regime_ctx.get("macro_boost", 0)   # v3 addition

    final = max(0.0, weighted - penalty - f_penalty + regime_adj - vix_pen - breadth_pen + macro_boost)
    return round(min(final, 100.0), 1)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN COMPUTATION LOOP
# ─────────────────────────────────────────────────────────────────────────────

def compute_all(sb, data: dict, today: str) -> list:
    msl_map      = data["msl_map"]
    stock_map    = data["stock_map"]
    history_map  = data["history_map"]
    sector_rank  = data["sector_rank"]
    open_syms    = data["open_syms"]
    blocked_syms = data["blocked_syms"]
    asm_syms     = data["asm_syms"]
    regime       = data["regime"]
    fii_data     = data["fii_data"]
    global_cues  = data["global_cues"]
    macro_data   = data["macro_data"]
    bearish_news = data["bearish_news"]
    bullish_news = data["bullish_news"]

    min_value_cr = float(cfg("min_value_cr", str(MIN_VALUE_CR_DEFAULT)))

    regime_ctx = build_regime_context(
        regime, global_cues, macro_data, fii_data, bearish_news, bullish_news
    )
    regime_date = regime.get("date", today)
    try:
        sb.table("market_regime").update({
            "macro_boost": regime_ctx["macro_boost"]
        }).eq("date", regime_date).execute()
    except Exception as e:
        logger.warning(f"Could not update macro_boost: {e}")

    logger.info(
        f"  Regime context: {regime_ctx['regime']} | "
        f"RSI extended @{regime_ctx['rsi_extended']} | "
        f"regime_boost={regime_ctx['regime_boost']:+d} | "
        f"macro_boost={regime_ctx['macro_boost']:+.1f} | "
        f"VIX={regime_ctx['vix']} | breadth={regime_ctx['breadth']:.0f}% | "
        f"FII={regime_ctx['fii_flag']} | GIFT={regime_ctx['gift_gap']:+.2f}%"
    )

    results  = []
    skipped  = 0
    no_stock = []
    gated    = []

    for sym, msl_row in msl_map.items():
        s = stock_map.get(sym)
        if not s:
            no_stock.append(sym)
            skipped += 1
            continue

        # ── BUG #5 FIX: Safety gate ───────────────────────────────────────────
        list_types = asm_syms.get(sym, set())
        if list_types & BLOCK_LIST_TYPES:
            gated.append(f"{sym}({','.join(sorted(list_types))})")
            skipped += 1
            continue

        hist      = history_map.get(sym, [])
        sector    = msl_row.get("sector") or s.get("sector") or ""
        strategy  = msl_row.get("strategy_source") or ""
        close     = float(s.get("close") or s.get("current_price") or
                          msl_row.get("current_price") or 0)
        adx       = float(s.get("adx") or 0)
        in_pos    = sym in open_syms
        value_cr  = float(s.get("value_cr") or 0)
        # Liquidity flag: VERY_LOW but not gated — still compute, but flagged
        low_liq   = value_cr > 0 and value_cr < min_value_cr

        try:
            # ── STEP 1: Independent signal contexts ────────────────────────
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

            # ── STEP 2: Core labels (depend only on s + regime_ctx) ────────
            velocity_state = compute_velocity_state(s, hist)
            momentum_state = compute_momentum_state(s, regime_ctx)
            momentum_phase = compute_momentum_phase(s, hist, regime_ctx)
            trend_maturity = compute_trend_maturity(s, regime_ctx)

            # ── STEP 3: Structural edge (depends on ma_ctx + bb_ctx) ───────
            struct_edge  = compute_struct_edge(s, ma_ctx, bb_ctx)
            reentry_mode = compute_reentry_mode(s)

            # ── STEP 4: Entry zone + timing ────────────────────────────────
            ez_low, ez_high = compute_entry_zones(s, strategy, regime_ctx)
            entry_timing    = compute_entry_timing_type(
                s, ez_low, ez_high,
                reentry_mode=reentry_mode,
                momentum_phase=momentum_phase,
            )
            price_loc, dist_fv, dist_entry = compute_price_location(s, ez_low, ez_high)

            # ── STEP 5: Lifecycle (depends on all labels above) ────────────
            lifecycle = compute_lifecycle(
                s, hist, regime_ctx,
                struct_edge=struct_edge,
                momentum_phase=momentum_phase,
                velocity_state=velocity_state,
                trend_maturity=trend_maturity,
                entry_timing_type=entry_timing,
                in_position=in_pos,
            )

            # ── STEP 6: Scores (depend on labels + contexts) ───────────────
            momentum_score      = compute_momentum_score(s, regime_ctx, macd_ctx, stoch_ctx, ha_ctx, weekly_ctx)
            validity_score      = compute_validity_score(s, sector, sector_rank, ez_low, ez_high, ma_ctx, psar_ctx, vwap_ctx, regime_ctx)
            institutional_score = compute_institutional_score(s, vwap_ctx, vol_trend, fii_data)
            breakout_readiness  = compute_breakout_readiness(s, bb_ctx, psar_ctx)
            risk_score          = compute_risk_score(s, regime_ctx, fund_ctx, msl_row)
            expected_r          = compute_expected_r(s, ez_low, ez_high)
            days_trigger        = compute_days_to_trigger(s, ez_low)

            holding_score = compute_holding_score(
                s, regime_ctx, lifecycle, momentum_state,
                risk_score, velocity_state, ma_ctx, psar_ctx
            )

            # ── STEP 7: Final score + history ──────────────────────────────
            base_score  = float(msl_row.get("base_score") or msl_row.get("final_score") or 50)
            final_score = compute_final_score(
                momentum_score, validity_score, institutional_score,
                breakout_readiness, risk_score, sector, sector_rank,
                base_score, regime_ctx, ma_ctx["ma_alignment_score"],
                weekly_ctx["weekly_structure_score"], fund_ctx["fundamental_penalty"]
            )
            hist_ctx = compute_msl_history_context(sym, hist)

            # trade_allowed: False if msl_row says so OR if stock is on watch list
            trade_allowed = bool(msl_row.get("trade_allowed", True))
            if list_types:   # non-blocking safety list types (e.g. ASM_LT1) — warn but allow
                trade_allowed = False

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
                "trade_allowed":        trade_allowed,
                "low_liquidity":        low_liq,
                # ── History ──
                "days_in_list":         hist_ctx["days_in_list"],
                "rank_vel_3d":          hist_ctx["rank_vel_3d"],
                "score_vel_5d":         hist_ctx["score_vel_5d"],
                "persistent_phase":     hist_ctx["persistent_phase"],
                # ── Composite scores ──
                "momentum_score":       momentum_score,
                "institutional_score":  institutional_score,
                "breakout_readiness":   breakout_readiness,
                "risk_score":           risk_score,
                "holding_score":        holding_score,
                "days_to_trigger_est":  days_trigger,
                # ── Signal contexts ──
                "ma_alignment_score":   ma_ctx["ma_alignment_score"],
                "ma_levels":            ma_ctx["ma_levels"],
                "ha_signal":            ha_ctx["ha_signal"],
                "bb_squeeze":           bb_ctx["bb_squeeze"],
                "bb_position_pct":      bb_ctx["bb_position_pct"],
                "bb_context":           bb_ctx["bb_context"],
                "bb_width_pct":         bb_ctx["bb_width_pct"],
                "macd_direction":       macd_ctx["macd_direction"],
                "macd_crossing_up":     macd_ctx["macd_crossing_up"],
                "psar_dual_confirmed":  psar_ctx["dual_trend_confirmed"],
                "st_cushion_pct":       psar_ctx["st_cushion_pct"],
                "vwap_alignment":       vwap_ctx["vwap_alignment"],
                "dist_vwap_20d_pct":    vwap_ctx["dist_vwap_20d_pct"],
                "volume_trend":         vol_trend["volume_trend"],
                "liquidity_quality":    vol_trend["liquidity_quality"],
                "weekly_structure":     weekly_ctx["weekly_structure"],
                "fundamental_quality":  fund_ctx["fundamental_quality"],
                "stoch_context":        stoch_ctx["stoch_context"],
                # ── Macro/regime context ──
                "active_regime":        regime_ctx["regime"],
                "rsi_extended_thresh":  get_rsi_extended_threshold(regime_ctx, adx),
                "fii_flag":             regime_ctx["fii_flag"],
                # ── Metadata ──
                "compute_source":       "compute_msl_v3",
                "computed_at":          datetime.now(IST).isoformat(),
            })

        except Exception as e:
            logger.warning(f"  {sym}: compute failed — {e}")
            skipped += 1

    if no_stock:
        logger.warning(f"  {len(no_stock)} missing from stock_data_daily: {no_stock}")
    if gated:
        logger.info(f"  Safety-gated: {gated}")
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
                f"hold={r['holding_score']} FII={r.get('fii_flag','?')}"
            )
        return

    if mode == "shadow":
        # BUG #12 FIX: Merge over existing msl_computed rows to preserve screen_stocks fields
        symbols    = [r["symbol"] for r in results]
        existing   = (
            sb.table("msl_computed")
              .select("*")
              .eq("date", today)
              .in_("symbol", symbols)
              .execute().data
        )
        existing_map = {r["symbol"]: r for r in (existing or [])}

        merged = []
        for r in results:
            base = dict(existing_map.get(r["symbol"], {}))
            base.update(r)   # compute_msl fields win on conflict
            base.pop("compute_source", None)   # don't overwrite in existing record
            merged.append(base)

        # Re-add compute_source for new rows only
        for r in merged:
            r["compute_source"] = "compute_msl_v3"

        write_rows = []
        for r in merged:
            row = {k: v for k, v in r.items() if k != "base_rank"}
            if r.get("base_rank") is not None:
                row["priority_rank"] = r["base_rank"]
            write_rows.append(row)
        for i in range(0, len(write_rows), 50):
            sb.table("msl_computed").upsert(write_rows[i:i+50], on_conflict="date,symbol").execute()
        logger.success(f"✓ shadow: {len(results)} → msl_computed (merged)")

    elif mode in ("hybrid", "full"):
        current_rows = (
            sb.table("master_shortlist")
              .select("*")
              .eq("date", today)
              .execute().data
        )
        current_map = {r["symbol"]: r for r in (current_rows or [])}
        upsert_rows = []
        for r in results:
            sym  = r["symbol"]
            base = dict(current_map.get(sym, {}))
            for field, val in r.items():
                if field not in PRESERVE_FIELDS and field != "ma_levels":
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
    # BUG #1 FIX: mode resolved HERE — load_data receives it and does NOT re-read cfg
    mode  = cfg("compute_msl_mode", "shadow")
    if os.getenv("COMPUTE_MSL_MODE_OVERRIDE"):
        mode = os.getenv("COMPUTE_MSL_MODE_OVERRIDE")

    logger.info("=" * 70)
    logger.info(
        f"STEP [14]: Compute MSL v3.0 | {today} | mode={mode.upper()}"
        + (" [DRY RUN]" if DRY_RUN else "")
    )
    logger.info("=" * 70)

    logger.info("Pass 1: loading data (9 bulk queries)...")
    data = load_data(sb, today, mode)
    if not data or not data.get("msl_map"):
        logger.error("No data loaded — aborting")
        return {"status": "no_data"}

    # ── Coverage gate — abort if critical upstream fields are missing ─────────
    coverage = data.get("field_coverage", {})
    critical_fields = ["value_cr", "delivery_pct", "adx", "rsi_daily",
                       "above_sma50", "above_st", "sma50_gt_200"]
    abort = False
    for f in critical_fields:
        pct = coverage.get(f, 0)
        if pct < 30:
            logger.error(
                f"  ABORT: '{f}' is only {pct}% populated — "
                f"upstream script has not run. Fix pipeline order and retry."
            )
            abort = True
    if abort:
        return {"status": "missing_upstream_data", "field_coverage": coverage}

    logger.info("Pass 2: computing all fields...")
    results = compute_all(sb, data, today)
    if not results:
        logger.error("No results computed")
        return {"status": "no_results"}

    # ── Diagnostic summary ─────────────────────────────────────────────────────
    n = len(results)
    def avg(field): return round(sum(r.get(field, 0) or 0 for r in results) / n, 1)

    logger.info(f"  Results: {n} symbols")
    logger.info(f"  Avg final_score:         {avg('final_score')}")
    logger.info(f"  Avg momentum_score:      {avg('momentum_score')}")
    logger.info(f"  Avg institutional_score: {avg('institutional_score')}")
    logger.info(f"  Avg breakout_readiness:  {avg('breakout_readiness')}")
    logger.info(f"  Avg risk_score:          {avg('risk_score')}")
    logger.info(f"  Avg holding_score:       {avg('holding_score')}")
    logger.info(f"  Avg MA alignment:        {avg('ma_alignment_score')}/6")

    logger.info(f"  Momentum state:   {dict(Counter(r.get('momentum_state') for r in results))}")
    logger.info(f"  Momentum phase:   {dict(Counter(r.get('momentum_phase') for r in results))}")
    logger.info(f"  Trend maturity:   {dict(Counter(r.get('trend_maturity') for r in results))}")
    logger.info(f"  Lifecycle:        {dict(Counter(r.get('lifecycle') for r in results))}")
    logger.info(f"  Entry timing:     {dict(Counter(r.get('entry_timing_type') for r in results))}")
    logger.info(f"  Weekly structure: {dict(Counter(r.get('weekly_structure') for r in results))}")
    logger.info(f"  BB context:       {dict(Counter(r.get('bb_context') for r in results))}")
    logger.info(f"  VWAP alignment:   {dict(Counter(r.get('vwap_alignment') for r in results))}")
    logger.info(f"  Volume trend:     {dict(Counter(r.get('volume_trend') for r in results))}")
    logger.info(f"  Fund quality:     {dict(Counter(r.get('fundamental_quality') for r in results))}")

    # Score distribution — key quality check for AI feeder health
    bands = {"≥70": 0, "55-70": 0, "40-55": 0, "<40": 0}
    for r in results:
        fs = r.get("final_score", 0) or 0
        if fs >= 70:   bands["≥70"] += 1
        elif fs >= 55: bands["55-70"] += 1
        elif fs >= 40: bands["40-55"] += 1
        else:          bands["<40"] += 1
    logger.info(f"  Score distribution: {bands}")
    if bands["≥70"] < 2:
        logger.warning("  ⚠ Less than 2 stocks scoring ≥70 — regime/data issue or regime is RISK OFF?")

    # Top setups
    top = sorted(results, key=lambda x: x.get("final_score", 0), reverse=True)[:6]
    logger.info("  Top 6 by final_score:")
    for r in top:
        in_p  = "📂" if r.get("in_position") else "  "
        warn  = "⚠️ " if r.get("low_liquidity") else ""
        logger.info(
            f"  {in_p}{warn}{r['symbol']:<12} final={r['final_score']:>5}  "
            f"hold={r['holding_score']:>5}  mom={r['momentum_state']:<8} "
            f"phase={r['momentum_phase']:<10} MA={r['ma_alignment_score']}/6  "
            f"BB={r.get('bb_context','?'):<20} risk={r['risk_score']:>4}  "
            f"struct={r['struct_edge']}"
        )

    # HOT + in zone — prime new opportunities
    hot_in_zone = [
        r for r in results
        if r.get("momentum_state") == "HOT"
        and r.get("entry_timing_type") in ("OPTIMAL", "REENTRY")
        and not r.get("in_position")
        and r.get("trade_allowed", True)
    ]
    if hot_in_zone:
        logger.info(f"  ★ HOT + in zone (new opportunities): {[r['symbol'] for r in hot_in_zone]}")

    # FRESH trend + struct edge = highest expected return per unit risk
    fresh_setups = [
        r for r in results
        if r.get("trend_maturity") == "FRESH"
        and r.get("struct_edge") == "YES"
        and not r.get("in_position")
    ]
    if fresh_setups:
        logger.info(f"  🌱 FRESH trend + struct_edge: {[r['symbol'] for r in fresh_setups]}")

    # Held positions weakening
    held_weakening = [
        r for r in results
        if r.get("in_position")
        and (r.get("holding_score", 100) or 100) < 35
    ]
    if held_weakening:
        logger.warning(f"  ⚠ Held positions weakening: {[r['symbol'] for r in held_weakening]}")

    # Sheet vs computed divergence — always runs independently of mode
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
        computed_symbols = {r["symbol"] for r in results}
        for r in results:
            mrow = sheet_map.get(r["symbol"], {})
            if not mrow: continue
            s_mom = mrow.get("momentum_state") or ""
            c_mom = r["momentum_state"]
            s_scr = float(mrow.get("final_score") or 0)
            c_scr = r["final_score"]
            base_rank = int(mrow.get("base_rank") or 9999)
            if s_mom != c_mom or abs(s_scr - c_scr) > 12:
                diffs.append((base_rank,
                    f"    #{base_rank} {r['symbol']}: mom {s_mom or 'None'}→{c_mom}  "
                    f"score {s_scr:.0f}→{c_scr}"))
        for sym, mrow in sheet_map.items():
            if sym not in computed_symbols:
                base_rank = int(mrow.get("base_rank") or 9999)
                diffs.append((base_rank,
                    f"    #{base_rank} {sym}: NOT COMPUTED — "
                    f"sheet mom={mrow.get('momentum_state') or 'None'}  "
                    f"score={float(mrow.get('final_score') or 0):.0f}"))
        if diffs:
            diffs.sort(key=lambda x: x[0])
            logger.info(f"  Sheet vs computed divergences ({len(diffs)}), top 40 by rank:")
            for _, d in diffs[:40]:
                logger.info(d)
        else:
            logger.info("  ✅ Computed values closely match Sheet")
    else:
        # BUG #9 FIX: Correct log message for shadow mode
        logger.warning(
            f"  Sheet comparison skipped — master_shortlist empty for today "
            f"(mode={mode}, source for this run was "
            f"{'msl_computed' if mode == 'shadow' else 'master_shortlist'})"
        )

    # Sort and assign ranks
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    for rank, r in enumerate(results, 1):
        r["base_rank"] = rank

    logger.info("Pass 3: writing results...")
    write_results(sb, results, mode, today)

    return {
        "status":          "ok",
        "computed":        n,
        "mode":            mode,
        "avg_score":       avg("final_score"),
        "hot_in_zone":     len(hot_in_zone),
        "fresh_setups":    len(fresh_setups),
        "held_weakening":  len(held_weakening),
        "regime":          data["regime"].get("regime", "?"),
        "fii_flag":        data["fii_data"].get("fii_flag", "?"),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute MSL v3.0")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    parser.add_argument("--mode", choices=["shadow", "hybrid", "full"],
                        help="Override compute_msl_mode from system_config")
    args = parser.parse_args()
    if args.dry_run: os.environ["DRY_RUN"] = "True"
    if args.mode:    os.environ["COMPUTE_MSL_MODE_OVERRIDE"] = args.mode
    print(main())