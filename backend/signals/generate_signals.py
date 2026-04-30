"""
TradeOS v6 — Signal Generation Engine v4
=========================================
Pipeline position: [15] — after compute_msl v3, before msl_history.

DATA SOURCE CLARIFICATION
==========================
All Gates 3 and 4 logic reads from master_shortlist rows ONLY.
In full-mode, compute_msl (step 14) enriches master_shortlist directly, so
SELECT * from master_shortlist contains all compute_msl fields (risk_score,
lifecycle, holding_score, bb_squeeze, st_cushion_pct, weekly_structure, etc.)
No separate msl_computed table is referenced in this script.
msl_computed is only written in shadow-mode (step 13 routing) and is never
read here. If fields arrive as None, they are treated as unknown, not blocked.

WHAT CHANGED FROM v3 → v4
===========================

─── GATE 4: ZONE PROXIMITY (complete rework) ─────────────────────────────────

FIX 1 — ATR-normalised distance replaces fixed 3%/8% thresholds
  v3: dist_from_high > 8% → hard block regardless of stock volatility.
      A 3%-ATR stock at 8% above zone = 2.7 ATRs (very extended).
      A 1%-ATR stock at 8% above zone = 8 ATRs (unreachable). Same rule,
      wildly different risk profiles.
  v4: dist_in_atrs = (cp - entry_zone_high) / (cp × atr_pct/100).
      max_chase_atrs is a function of momentum_phase + velocity_state (0–3x).
      Structure quality bonuses (struct_edge, weekly_structure, bb_squeeze,
      psar_dual_confirmed) add up to +1.6 ATRs on top. A high-conviction HOT
      + EARLY + ACCELERATING + struct_edge stock can now legitimately chase
      up to 3.5 ATRs above zone, which is ~10.5% on a 3%-ATR stock —
      still disciplined but matches real swing-trade behaviour.
      Uses existing: atr_pct (stock_data_daily), momentum fields (master_shortlist).

FIX 2 — R:R viability gate (new — was entirely absent in v3)
  v3: No R:R check existed anywhere in entry classification. An in-zone stock
      with expected_r=0.5 still generated a BUY_CANDIDATE. A PRIME_SETUP
      that had already run 30% above zone still qualified if momentum was hot.
  v4: stop_price   = entry_zone_low × (1 − STOP_BUFFER_PCT)
      ideal_target  = entry_zone_low + (entry_zone_low − stop_price) × expected_r
      implied_rr    = (ideal_target − cp) / (cp − stop_price)
      if implied_rr < MIN_RR_ENTRY (config, default 1.0): → insufficient_rr
      As price chases above zone, implied_rr degrades naturally. This single
      check replaces the need for any upper distance ceiling — if R:R still
      works at 25% above zone on a 4x expected_r stock, it's valid.
      Also catches the inverse: an in-zone stock with tiny expected_r that
      was always a poor trade.
      Uses existing: expected_r and entry_zone_low (master_shortlist).
      Special case: if ideal_target <= cp, the target is already hit → blocked.

FIX 3 — Below-zone: pullback vs breakdown distinction (was a hard block)
  v3: cp < ep × (1 − threshold) → below_zone_wait. Full stop. Every below-zone
      case was identical regardless of whether the stock was resting on
      supertrend or breaking through its 50 SMA.
  v4: Three-way evaluation using existing fields:
      BREAKDOWN  → above_sma50=False OR weekly_structure in (WEAK,BEARISH)
                   OR st_cushion_pct ≤ 0 OR velocity_state=DECELERATING
                   → filter: structural_breakdown (hard block)
      VALID PULLBACK → above_sma50=True AND weekly_structure not WEAK/BEARISH
                       AND st_cushion_pct > 0 AND velocity_state not DECELERATING
                       AND (reentry_mode=ELIGIBLE OR entry_timing_type=REENTRY)
                       → falls through to R:R check → REENTRY_SETUP if passes
      MONITORING → mixed signals → below_zone_monitoring (soft WATCH)
      Uses existing: above_sma50 (stock_data_daily), weekly_structure,
      st_cushion_pct, velocity_state, reentry_mode (master_shortlist).

─── SIGNAL SUBTYPE CLASSIFICATION ───────────────────────────────────────────

FIX 4 — PRIME_SETUP now includes EARLY phase (was EXPANSION only)
  v3: momentum_phase == "EXPANSION" — this missed the best entry point.
      A stock in EARLY phase with HOT+ACCELERATING+struct_edge aligned is the
      highest quality setup possible. It was being downgraded to BUY_CANDIDATE.
  v4: momentum_phase in ("EXPANSION", "EARLY") — EARLY is now eligible.
      The trend_maturity guard (not LATE/EXHAUSTED) prevents false EARLY
      signals that are actually re-starts of exhausted trends.

FIX 5 — BREAKOUT_SETUP gains trend_maturity guard
  v3: No trend_maturity check on BREAKOUT_SETUP. A bb_squeeze on a LATE/
      EXHAUSTED trend still generated BREAKOUT_SETUP — a dangerous "late
      breakout" that typically traps buyers near the top.
  v4: trend_maturity not in ("LATE", "EXHAUSTED") required for both
      BREAKOUT_SETUP paths (squeeze+volume and readiness≥60).

─── OPEN POSITION SIGNALS ───────────────────────────────────────────────────

FIX 6 — Supertrend broken triggers EXIT for held positions
  v3: st_cushion_pct was written to signal_log but never checked for open
      positions. A held stock could break below supertrend and still signal HOLD.
  v4: st_cushion_pct ≤ 0 → EXIT with reason "supertrend_broken".
      Checked after holding_score gate, before lifecycle gates.
      Uses existing: st_cushion_pct (master_shortlist from compute_msl).

FIX 7 — ADD suppressed when trend is LATE or EXHAUSTED
  v3: lifecycle=ADD + ACCELERATING + holding_score≥60 → ADD signal regardless
      of how old the trend was. Adding to a LATE trend is poor risk management.
  v4: trend_maturity in ("LATE", "EXHAUSTED") demotes ADD → HOLD with reason
      "add_suppressed_late_trend". The MSL sheet's ADD flag may lag real-time
      trend maturity from compute_msl.

FIX 8 — DECELERATING velocity annotated on HOLD signals
  v3: DECELERATING velocity on a held position → generic "holding" HOLD signal.
      No downstream visibility that the momentum was weakening.
  v4: velocity_state=DECELERATING + holding_score<60 → HOLD with reason
      "hold_deceleration_monitor". Alerts and AI steps can act on this.

─── GATE SCOPING FIXES ───────────────────────────────────────────────────────

FIX 9 — ASM override scoped to new entries only
  v3: asm_flag overrides ALL signals to BLOCKED_ASM including EXIT/REDUCE on
      open positions. This suppressed critical exit guidance on ASM stocks.
  v4: BLOCKED_ASM only applied when position_state != "OPEN_POSITION".
      Open positions retain their EXIT/REDUCE/HOLD/ADD signal; asm_flag=True
      remains in the signal dict as an informational field for the operator.

FIX 10 — Industry scoring applied before classification gates
  v3: Industry bonus (+10 for ind_top5, +5 for STRONG) applied after signal
      classification. The bonus affected score_adjusted but not the gates
      that already ran on the pre-bonus score. Edge case: a stock just below
      min_score threshold with ind_top5 would be dropped even though the
      bonus would have cleared it.
  v4: Industry fields extracted and bonus applied before the min_score gate
      so the same adjusted score is used consistently throughout.

─── SCORE GATE ───────────────────────────────────────────────────────────────

FIX 11 — score_adjusted floor for entry signals
  v3: No minimum on score_adjusted for entry signals. A CAUTION regime penalty
      (×0.85) combined with a high risk_score penalty (−7) could produce a
      PRIME_SETUP with score_adjusted = 38, which is meaningless as an
      actionable entry but still appeared in output as PRIME_SETUP.
  v4: Entry signals with score_adjusted < MIN_SCORE_AFTER_ADJ (config, default 45)
      are downgraded to WATCH with reason "low_adj_score_N". Signal type and
      subtype are preserved in signal_subtype for analysis.

─── NO NEW DATA POINTS ───────────────────────────────────────────────────────
All v4 logic uses fields already present in master_shortlist (compute_msl
enriched) or stock_data_daily. No new table queries. No new columns required.
New constants MIN_RR_ENTRY and MIN_SCORE_AFTER_ADJ are config-readable so
they can be tuned without code changes.
"""

import sys, json
from pathlib import Path
from datetime import date
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, get_strategy_config, get_system_config,
    cfg, cfg_bool, cfg_int, cfg_float, today_ist,
    buy_candidate_threshold, get_max_positions, DRY_RUN,
    is_kill_switch_active
)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

RISK_BLOCK_THRESHOLD   = 80    # hard block regardless of momentum
RISK_PENALTY_THRESHOLD = 65    # score_adjusted penalised

HOLDING_SCORE_EXIT_THRESHOLD = 30   # open position: exit if below
ADD_HOLDING_MIN              = 60   # minimum holding_score for ADD signal

PRIME_BREAKOUT_MIN     = 55    # breakout_readiness threshold for PRIME_SETUP

STOP_BUFFER_PCT        = 0.03  # 3% below entry_zone_low used as stop proxy for R:R calc
ATR_FALLBACK_PCT       = 3.0   # used when atr_pct is 0 or unavailable from stock_data_daily

# v4 new — both are config-readable for tuning without code changes
# MIN_RR_ENTRY: minimum implied R:R from current price. 1.0 = break-even on risk.
# MIN_SCORE_AFTER_ADJ: entry signals below this after all adjustments → WATCH.
_MIN_RR_DEFAULT          = 1.0
_MIN_SCORE_ADJ_DEFAULT   = 45.0

# Signal types that represent new entry intent (used in gate scoping)
ENTRY_SIGNAL_TYPES = frozenset({
    "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
    "REENTRY_SETUP", "STAGED_ENTRY",
})

# trend_maturity states that indicate a trend is too old to ADD or BREAKOUT into
LATE_MATURITY = frozenset({"LATE", "EXHAUSTED"})

# weekly_structure states that indicate structural breakdown
WEAK_STRUCTURE = frozenset({"WEAK", "BEARISH"})


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING  (unchanged from v3)
# ─────────────────────────────────────────────────────────────────────────────

def load_today_data():
    """
    Load all required data in bulk from master_shortlist (full mode) and
    supporting tables. master_shortlist SELECT * includes all compute_msl
    enriched fields in full/hybrid pipeline mode.
    """
    sb    = get_supabase()
    today = str(today_ist())

    stocks = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stocks:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            last_date = latest[0]["date"]
            stocks = sb.table("stock_data_daily").select("*").eq("date", last_date).execute().data
            logger.info(f"No stock_data_daily for {today} — using last trading day {last_date}")

    # Full SELECT * — in full mode this includes all compute_msl enriched fields:
    # holding_score, risk_score, lifecycle, bb_squeeze, st_cushion_pct,
    # weekly_structure, velocity_state, momentum_state, etc.
    msl = sb.table("master_shortlist").select("*").eq("date", today).execute().data
    if not msl:
        latest_msl = sb.table("master_shortlist").select("date").order("date", desc=True).limit(1).execute().data
        if latest_msl:
            last_msl_date = latest_msl[0]["date"]
            msl = sb.table("master_shortlist").select("*").eq("date", last_msl_date).execute().data
            logger.warning(f"No MSL for {today} — using last available date {last_msl_date} ({len(msl)} rows)")

    open_p   = sb.table("open_positions").select("*").execute().data
    regime   = sb.table("market_regime").select("*").eq("date", today).execute().data
    if not regime:
        regime = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data

    events   = sb.table("event_calendar").select("*").eq("is_active", True).execute().data
    safety   = sb.table("safety_lists").select("symbol,list_type").execute().data
    industry_str = sb.table("industry_strength").select("*").eq("date", str(today_ist())).execute().data
    industry_map = {r["industry"]: r for r in industry_str}

    stock_map  = {s["symbol"]: s for s in stocks}
    open_map   = {p["symbol"]: p for p in open_p}
    regime_obj = regime[0] if regime else {}
    asm_set    = {r["symbol"] for r in safety if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety if r["list_type"] == "FO_BAN"}

    fii_rows = (
        sb.table("fii_dii_flow")
        .select("fii_flag,fii_net_20d")
        .order("date", desc=True).limit(1).execute().data
    )
    fii_flag    = fii_rows[0].get("fii_flag", "NEUTRAL") if fii_rows else "NEUTRAL"
    fii_net_20d = fii_rows[0].get("fii_net_20d") if fii_rows else None

    compute_source_counts = {}
    for r in msl:
        src = r.get("compute_source") or "unknown"
        compute_source_counts[src] = compute_source_counts.get(src, 0) + 1
    logger.info(f"  MSL rows loaded: {len(msl)} | compute_source: {compute_source_counts}")

    return (
        stock_map, msl, open_map, regime_obj,
        events, asm_set, fo_ban_set, industry_map,
        fii_flag, fii_net_20d
    )


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY RULE ENGINES  (unchanged from v3)
# ─────────────────────────────────────────────────────────────────────────────

def run_ctl(stocks, sectors_by_name, cfg_ctl):
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_ctl.get("max_sector_rank", 4): continue
        if (s.get("rsi_monthly") or 0) < cfg_ctl.get("min_monthly_rsi", 58): continue
        if (s.get("rsi_weekly")  or 0) < cfg_ctl.get("min_weekly_rsi", 58):  continue
        if (s.get("ret_6m")      or -999) < cfg_ctl.get("min_6m_return", 0): continue
        if (s.get("atr_pct")     or 999) > cfg_ctl.get("max_atr_pct", 4):    continue
        if (s.get("market_cap")  or 0) < cfg_ctl.get("min_market_cap", 500): continue
        candidates.add(sym)
    return candidates


def run_sbs(stocks, sectors_by_name, cfg_sbs):
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_sbs.get("max_sector_rank", 7): continue
        if (s.get("rsi_daily")     or 0)   < cfg_sbs.get("min_daily_rsi", 55):   continue
        if (s.get("rsi_weekly")    or 0)   < cfg_sbs.get("min_weekly_rsi", 56):  continue
        if (s.get("vol_ratio")     or 0)   < cfg_sbs.get("min_vol_ratio", 1.3):  continue
        if (s.get("consol_range")  or 999) > cfg_sbs.get("max_consol_pct", 12):  continue
        if (s.get("atr_pct")       or 999) > cfg_sbs.get("max_atr_pct", 5):      continue
        if (s.get("market_cap")    or 0)   < cfg_sbs.get("min_market_cap", 300): continue
        candidates.add(sym)
    return candidates


def run_tpo(ctl_candidates, stocks, cfg_tpo):
    candidates = set()
    for sym in ctl_candidates:
        s   = stocks.get(sym, {})
        rsi = s.get("rsi_daily") or 0
        if not (cfg_tpo.get("min_rsi", 42) <= rsi <= cfg_tpo.get("max_rsi", 55)): continue
        if abs(s.get("dist_sma50") or 999) > cfg_tpo.get("max_dist_sma50", 3):    continue
        if (s.get("atr_pct") or 999) > cfg_tpo.get("max_atr_pct", 4):             continue
        candidates.add(sym)
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# EAP  (unchanged from v3)
# ─────────────────────────────────────────────────────────────────────────────

def get_eap_action(symbol, sector, events, cfg_eap):
    from datetime import date as _date
    pre_days    = int(cfg_eap.get("pre_event_days", 2))
    HIGH_IMPACT = {"RESULTS","EARNINGS","QUARTERLY_RESULTS","BOARD_MEETING",
                   "BOARD MEETING","FINANCIAL RESULTS"}
    MED_IMPACT  = {"AGM","ANALYST_MEET","CONCALL","INVESTOR_MEET"}
    LOW_IMPACT  = {"DIVIDEND","BONUS","SPLIT","BUYBACK","RIGHTS"}

    def _classify(ev):
        raw = (str(ev.get("event_type") or "") + " " + str(ev.get("purpose") or "")).upper()
        if any(k in raw for k in HIGH_IMPACT): return "HIGH"
        if any(k in raw for k in MED_IMPACT):  return "MEDIUM"
        if any(k in raw for k in LOW_IMPACT):  return "LOW"
        return "MEDIUM"

    best_action = "NO_CHANGE"
    for ev in events:
        if not ev.get("is_active"): continue
        ev_sym   = ev.get("symbol", "")
        affected = str(ev.get("affected_sectors", "")).lower()
        if not ((ev_sym and ev_sym == symbol)
                or (sector and sector.lower() in affected)
                or ev.get("event_category") == "GLOBAL"):
            continue
        start_d = ev.get("start_date") or ev.get("event_date")
        if not start_d: continue
        try:
            sd   = _date.fromisoformat(str(start_d)[:10])
            days = (sd - today_ist()).days
        except Exception:
            continue
        impact = _classify(ev)
        if impact == "HIGH":
            if 0 <= days <= pre_days:       return "AVOID_ENTRY"
            if -pre_days <= days < 0:       best_action = "PRIORITISE"
        elif impact == "MEDIUM":
            if 0 <= days <= pre_days:
                if best_action != "AVOID_ENTRY": best_action = "AVOID_ENTRY"
        elif impact == "LOW":
            if -pre_days <= days < 0:
                if best_action == "NO_CHANGE": best_action = "PRIORITISE"
    return best_action


# ─────────────────────────────────────────────────────────────────────────────
# OPEN POSITION SIGNAL — v4
# ─────────────────────────────────────────────────────────────────────────────

def classify_open_position_signal(msl_row: dict, pos: dict) -> tuple:
    """
    Determine signal_type for an open position.

    v4 changes vs v3:
      FIX 6: st_cushion_pct ≤ 0 → EXIT (supertrend broken)
      FIX 7: lifecycle=ADD + trend_maturity LATE/EXHAUSTED → HOLD
             (prevent adding into dying trend)
      FIX 8: velocity=DECELERATING + holding_score<60 → HOLD annotated
             as "hold_deceleration_monitor" for alerts/AI visibility

    Returns (signal_type, position_state, reason)
    """
    holding_score  = float(msl_row.get("holding_score") or 0)
    lifecycle      = (msl_row.get("lifecycle")      or "").upper()
    velocity_state = (msl_row.get("velocity_state") or "").upper()
    trend_maturity = (msl_row.get("trend_maturity") or "").upper()
    st_cushion_pct = msl_row.get("st_cushion_pct")   # None if compute_msl not enriched

    # ── Hard exits (priority order) ──────────────────────────────────────────

    if holding_score > 0 and holding_score < HOLDING_SCORE_EXIT_THRESHOLD:
        return "EXIT", "OPEN_POSITION", f"holding_score_low_{holding_score:.0f}"

    if lifecycle == "EXIT":
        return "EXIT", "OPEN_POSITION", "lifecycle_exit"

    # FIX 6 — supertrend broken: reliable structural exit signal
    # Guard: only act when value is explicitly set (not None/missing)
    if st_cushion_pct is not None and float(st_cushion_pct) <= 0:
        return "EXIT", "OPEN_POSITION", "supertrend_broken"

    if lifecycle == "REDUCE":
        return "REDUCE", "OPEN_POSITION", "lifecycle_reduce"

    # FIX 7 — ADD suppressed for late/exhausted trends
    if lifecycle == "ADD":
        if trend_maturity in LATE_MATURITY:
            return "HOLD", "OPEN_POSITION", "add_suppressed_late_trend"
        if velocity_state == "ACCELERATING" and holding_score >= ADD_HOLDING_MIN:
            return "ADD", "OPEN_POSITION", "lifecycle_add_accelerating"

    # Ops override (manual action_required preserves operator control)
    action = (pos.get("action_required") or "").upper()
    if "EXIT" in action or "SELL" in action:
        return "EXIT", "OPEN_POSITION", "manual_action_exit"
    if "ADD" in action:
        return "ADD",  "OPEN_POSITION", "manual_action_add"

    # FIX 8 — annotate deceleration for alert/AI visibility
    if velocity_state == "DECELERATING" and holding_score < 60:
        return "HOLD", "OPEN_POSITION", "hold_deceleration_monitor"

    return "HOLD", "OPEN_POSITION", "holding"


# ─────────────────────────────────────────────────────────────────────────────
# ATR ALLOWANCE HELPER — v4 new
# ─────────────────────────────────────────────────────────────────────────────

def _max_chase_atrs(momentum_state: str, momentum_phase: str,
                    velocity_state: str, trend_maturity: str,
                    struct_edge: str, weekly_structure: str,
                    psar_dual: bool, bb_squeeze: bool) -> float:
    """
    Return maximum allowable ATR-multiples of chase above entry_zone_high.

    Base allowance is set by momentum quality (phase × velocity), then
    structure quality bonuses are added. All inputs are upper-cased strings.

    Momentum quality matrix:
      HOT + EARLY  + ACCELERATING → 3.0  (best: catching a trend before it expands)
      HOT + EXPANSION + ACCELERATING → 2.5
      HOT + EXPANSION + FLAT       → 1.8
      BUILDING + any + ACCELERATING → 2.0
      BUILDING + any + FLAT         → 1.2
      LATE or EXHAUSTED trend       → 0.5  (near end of move, minimal tolerance)
      EXTENDED timing               → 0.0  (no chase, reentry only)
      Default                       → 1.0

    Structure bonuses (stackable, capped at +1.6 total):
      struct_edge = YES            → +0.5
      weekly_structure = STRONG    → +0.3
      psar_dual_confirmed = True   → +0.3
      bb_squeeze = True            → +0.5
    """
    # Base by momentum
    if trend_maturity == "EXTENDED":
        base = 0.0
    elif trend_maturity in LATE_MATURITY:
        base = 0.5
    elif momentum_state == "HOT":
        if momentum_phase == "EARLY" and velocity_state == "ACCELERATING":
            base = 3.0
        elif velocity_state == "ACCELERATING":
            base = 2.5
        else:
            base = 1.8
    elif momentum_state == "BUILDING":
        base = 2.0 if velocity_state == "ACCELERATING" else 1.2
    else:
        base = 1.0

    # Structure bonuses
    bonus = 0.0
    if struct_edge == "YES":           bonus += 0.5
    if weekly_structure == "STRONG":   bonus += 0.3
    if psar_dual:                      bonus += 0.3
    if bb_squeeze:                     bonus += 0.5

    return round(base + min(bonus, 1.6), 2)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY SIGNAL CLASSIFICATION — v4
# ─────────────────────────────────────────────────────────────────────────────

def classify_entry_signal(msl_row: dict, open_map: dict,
                           stock_data: dict,
                           threshold: float = None) -> tuple:
    """
    Classify entry signal for a new position candidate.

    v4 signature change: stock_data (dict from stock_data_daily) added as
    3rd positional arg. Caller passes stock_map.get(sym, {}).
    Needed for atr_pct and above_sma50 (live from bhavcopy, not in msl_row).

    Returns (is_entry: bool, signal_type: str, filter_reason: str)

    Gate order:
      3a. already_in_position
      3b. missing price data
      3c. risk_score hard block
      3d. liquidity block
      3e. lifecycle EXIT block
      3f. EXTENDED timing, no reentry
      4a. below zone: pullback vs breakdown (v4 NEW)
      4b. above zone: ATR-normalised distance (v4 REPLACED fixed %)
      4c. R:R viability from current price (v4 NEW)
      4d. target already reached check (v4 NEW)
      5.  signal subtype classification
    """
    sym = msl_row.get("symbol")

    if sym in open_map:
        return False, None, "already_in_position"

    # ── master_shortlist fields (compute_msl enriched in full mode) ──────────
    ep             = msl_row.get("entry_zone_low")
    eh             = msl_row.get("entry_zone_high") or (ep * 1.02 if ep else None)
    cp             = msl_row.get("current_price")
    expected_r     = float(msl_row.get("expected_r") or 0)
    risk_score     = float(msl_row.get("risk_score") or 0)
    low_liquidity  = bool(msl_row.get("low_liquidity"))
    liquidity_qual = (msl_row.get("liquidity_quality") or "").upper()
    lifecycle         = (msl_row.get("lifecycle")         or "").upper()
    entry_timing_type = (msl_row.get("entry_timing_type") or "").upper()
    momentum_state    = (msl_row.get("momentum_state")    or "").upper()
    momentum_phase    = (msl_row.get("momentum_phase")    or "").upper()
    velocity_state    = (msl_row.get("velocity_state")    or "").upper()
    struct_edge       = (msl_row.get("struct_edge")       or "").upper()
    reentry_mode      = (msl_row.get("reentry_mode")      or "").upper()
    trend_maturity    = (msl_row.get("trend_maturity")    or "").upper()
    breakout_readiness = float(msl_row.get("breakout_readiness") or 0)
    bb_squeeze        = bool(msl_row.get("bb_squeeze"))
    volume_trend      = (msl_row.get("volume_trend")      or "").upper()
    weekly_structure  = (msl_row.get("weekly_structure")  or "").upper()
    psar_dual         = bool(msl_row.get("psar_dual_confirmed"))
    st_cushion_pct    = msl_row.get("st_cushion_pct")

    # ── stock_data_daily fields (live bhavcopy data, not in msl_row) ─────────
    atr_pct     = float(stock_data.get("atr_pct") or ATR_FALLBACK_PCT)
    above_sma50 = bool(stock_data.get("above_sma50"))

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 3 — Hard blocks
    # ─────────────────────────────────────────────────────────────────────────

    if not ep or not cp or ep <= 0:
        return False, None, "missing_price_data"

    if risk_score >= RISK_BLOCK_THRESHOLD:
        return False, None, f"blocked_high_risk_{risk_score:.0f}"

    # Liquidity gate: only apply when value_cr is known (bhavcopy has run)
    value_cr_known = float(msl_row.get("value_cr") or 0) > 0
    if value_cr_known and (low_liquidity or liquidity_qual == "VERY_LOW"):
        return False, None, "blocked_low_liquidity"

    if lifecycle == "EXIT":
        return False, None, "exit_lifecycle_no_entry"

    if entry_timing_type == "EXTENDED" and reentry_mode != "ELIGIBLE":
        return False, None, "extended_timing_no_reentry"

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 4A — Below-zone: pullback vs breakdown  (v4: was simple hard block)
    # ─────────────────────────────────────────────────────────────────────────

    threshold_val = threshold or buy_candidate_threshold()
    below_zone    = cp < (ep * (1 - threshold_val))

    if below_zone:
        # Determine whether price is on structure or breaking through it
        st_ok = (st_cushion_pct is not None and float(st_cushion_pct) > 0)

        is_breakdown = (
            not above_sma50
            or weekly_structure in WEAK_STRUCTURE
            or (st_cushion_pct is not None and float(st_cushion_pct) <= 0)
            or velocity_state == "DECELERATING"
        )
        is_valid_pullback = (
            above_sma50
            and weekly_structure not in WEAK_STRUCTURE
            and weekly_structure != ""     # don't trust missing structure data
            and st_ok
            and velocity_state not in ("DECELERATING",)
            and (reentry_mode == "ELIGIBLE" or entry_timing_type == "REENTRY")
        )

        if is_breakdown:
            return False, None, "structural_breakdown"
        elif not is_valid_pullback:
            return False, None, "below_zone_monitoring"
        # is_valid_pullback == True → fall through to R:R check then REENTRY_SETUP

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 4B — Above-zone: ATR-normalised distance  (v4: replaces fixed 3%/8%)
    # ─────────────────────────────────────────────────────────────────────────

    above_zone_raw = (cp - eh) / eh if eh and eh > 0 else 0

    if cp > eh:
        if atr_pct <= 0:
            # ATR unavailable: conservative fallback to absolute %
            if above_zone_raw > 0.08:
                return False, None, "far_above_no_atr_data"
        else:
            dist_in_atrs = (cp - eh) / (cp * atr_pct / 100)
            max_atrs     = _max_chase_atrs(
                momentum_state, momentum_phase, velocity_state, trend_maturity,
                struct_edge, weekly_structure, psar_dual, bb_squeeze
            )
            if dist_in_atrs > max_atrs:
                return False, None, (
                    f"chase_{dist_in_atrs:.1f}atrs_limit_{max_atrs:.1f}"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 4C — R:R viability from current price  (v4: was entirely absent)
    # ─────────────────────────────────────────────────────────────────────────
    # Only check when expected_r is meaningfully set (> 0).
    # stop_price = entry_zone_low × (1 − STOP_BUFFER_PCT)
    # ideal_target = entry_zone_low + ideal_risk × expected_r
    # implied_rr = (ideal_target − cp) / (cp − stop_price)
    # If implied_rr < MIN_RR_ENTRY: insufficient reward from here.

    min_rr = cfg_float("min_rr_to_enter", _MIN_RR_DEFAULT)

    if expected_r > 0 and ep > 0:
        stop_price    = ep * (1 - STOP_BUFFER_PCT)
        ideal_risk    = ep - stop_price                   # risk in ₹ at zone entry
        ideal_target  = ep + (ideal_risk * expected_r)    # target price from zone entry

        # FIX 4D: target already reached — price has exceeded the intended target
        if ideal_target <= cp:
            return False, None, "target_already_reached"

        # R:R is only meaningful if current price is above the stop
        if cp > stop_price:
            remaining_upside = ideal_target - cp
            current_risk     = cp - stop_price
            implied_rr       = remaining_upside / current_risk
            if implied_rr < min_rr:
                return False, None, f"insufficient_rr_{implied_rr:.2f}x"

    # ─────────────────────────────────────────────────────────────────────────
    # GATE 5 — Signal subtype classification (highest quality first)
    # ─────────────────────────────────────────────────────────────────────────

    # PRIME_SETUP — FIX 4: now includes EARLY phase (was EXPANSION only)
    # EARLY phase is the best entry: catching a trend before expansion begins.
    if (momentum_state == "HOT"
            and momentum_phase in ("EXPANSION", "EARLY")
            and velocity_state == "ACCELERATING"
            and struct_edge == "YES"
            and breakout_readiness >= PRIME_BREAKOUT_MIN
            and trend_maturity not in LATE_MATURITY):
        return True, "PRIME_SETUP", "prime_all_aligned"

    # BREAKOUT_SETUP — FIX 5: trend_maturity guard added
    # Late breakouts (LATE/EXHAUSTED trend) trap buyers near tops.
    if trend_maturity not in LATE_MATURITY:
        if (bb_squeeze or breakout_readiness >= 70) and volume_trend == "EXPANDING":
            return True, "BREAKOUT_SETUP", "breakout_squeeze_volume"
        if breakout_readiness >= 60 and momentum_state in ("HOT", "BUILDING"):
            return True, "BREAKOUT_SETUP", "breakout_readiness_high"

    # REENTRY_SETUP — explicit timing flag OR valid below-zone pullback
    if entry_timing_type == "REENTRY" or reentry_mode == "ELIGIBLE":
        return True, "REENTRY_SETUP", "reentry_pullback"
    if below_zone:
        # Reached here only if is_valid_pullback passed Gate 4A
        return True, "REENTRY_SETUP", "pullback_to_zone"

    # STAGED_ENTRY — approaching zone with building momentum
    if entry_timing_type == "APPROACHING" and momentum_state in ("HOT", "BUILDING"):
        return True, "STAGED_ENTRY", "approaching_zone_building"

    # BUY_CANDIDATE — standard in/near zone entry (fallback)
    return True, "BUY_CANDIDATE", "in_zone"


# ─────────────────────────────────────────────────────────────────────────────
# SCORE ADJUSTMENT — v4 (structure unchanged, CAUTION penalty placement note)
# ─────────────────────────────────────────────────────────────────────────────

def compute_adjusted_score(msl_row: dict, base_score: float,
                            signal_type: str, regime_name: str) -> float:
    """
    Adjust final_score using compute_msl sub-scores.

    Identical logic to v3. No changes required here because:
    - Industry bonus is now applied to base_score BEFORE this function is called
      (FIX 10), so it flows correctly into all subsequent adjustments.
    - The CAUTION ×0.85 regime penalty is applied last to the already-adjusted
      score, which is the intended behaviour (penalise the whole picture).
    """
    score = float(base_score or 0)

    momentum_score      = float(msl_row.get("momentum_score")      or 0)
    institutional_score = float(msl_row.get("institutional_score") or 0)
    breakout_readiness  = float(msl_row.get("breakout_readiness")  or 0)
    risk_score          = float(msl_row.get("risk_score")          or 0)
    psar_dual           = bool(msl_row.get("psar_dual_confirmed"))
    weekly_struct       = (msl_row.get("weekly_structure") or "").upper()

    if momentum_score >= 80:      score += 3
    elif momentum_score >= 70:    score += 1
    if institutional_score >= 60: score += 2
    if breakout_readiness >= 65:  score += 2
    if psar_dual:                 score += 2
    if weekly_struct == "STRONG": score += 1

    if risk_score >= RISK_PENALTY_THRESHOLD: score -= 5
    elif risk_score >= 50:                   score -= 2

    if signal_type == "PRIME_SETUP":      score += 5
    elif signal_type == "BREAKOUT_SETUP": score += 3
    elif signal_type == "REENTRY_SETUP":  score += 2

    if regime_name == "CAUTION":
        score = round(score * 0.85, 1)

    return round(min(max(score, 0.0), 100.0), 1)


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER CROSS-REFERENCE  (unchanged from v3)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_scanner_crossref(signals, sb, run_date):
    try:
        scanner_rows = (
            sb.table("scanner_signals")
            .select("symbol,pattern_type")
            .eq("date", str(run_date))
            .execute().data
        )
        if not scanner_rows:
            return signals

        scanner_map = {}
        for row in scanner_rows:
            sym = row.get("symbol") or ""
            pat = row.get("pattern_type") or ""
            if sym:
                scanner_map.setdefault(sym, []).append(pat)

        updated = 0
        for sig in signals:
            sym = sig["symbol"]
            if sym in scanner_map:
                sig["in_scanner"]       = True
                sig["scanner_patterns"] = ",".join(scanner_map[sym])
                if sig.get("in_rule_engine") and sig.get("signal_type") in ENTRY_SIGNAL_TYPES:
                    current = sig.get("score_adjusted") or sig.get("score") or 0
                    sig["score_adjusted"] = round(float(current) + 5, 1)
                    updated += 1

        logger.info(f"Scanner cross-ref: {len(scanner_map)} hits | {updated} signals +5")
    except Exception as e:
        logger.warning(f"Scanner cross-ref failed (non-fatal): {e}")
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATION LOOP — v4
# ─────────────────────────────────────────────────────────────────────────────

def generate(run_date: date | None = None) -> list:
    run_date  = run_date or today_ist()
    strat_cfg = get_strategy_config()

    (stock_map, msl, open_map, regime,
     events, asm_set, fo_ban_set, industry_map,
     fii_flag, fii_net_20d) = load_today_data()

    def _resolve_regime(regime_obj):
        from datetime import datetime, timezone
        manual    = regime_obj.get("regime", "NEUTRAL")
        predicted = regime_obj.get("predicted_regime")
        pred_at   = regime_obj.get("regime_predicted_at")
        if not predicted or not pred_at:
            return manual
        try:
            if isinstance(pred_at, str):
                pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
            else:
                pred_dt = pred_at
            if pred_dt.tzinfo is None:
                import datetime as _dt
                pred_dt = pred_dt.replace(tzinfo=_dt.timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() / 3600 <= 24:
                return predicted
        except Exception:
            pass
        return manual

    regime_name = _resolve_regime(regime)
    block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"

    india_vix_ctx    = regime.get("india_vix")
    nifty_5d_ctx     = regime.get("nifty_5d_chg_pct")
    above_200dma_ctx = regime.get("above_200dma_pct")

    sb = get_supabase()
    sectors = sb.table("sector_strength").select("sector,rank").eq("date", str(run_date)).execute().data
    sector_rank = {s["sector"]: s["rank"] for s in sectors if s.get("rank")}

    cfg_ctl = json.loads(strat_cfg["CTL"]) if isinstance(strat_cfg.get("CTL"), str) else strat_cfg.get("CTL", {})
    cfg_sbs = json.loads(strat_cfg["SBS"]) if isinstance(strat_cfg.get("SBS"), str) else strat_cfg.get("SBS", {})
    cfg_tpo = json.loads(strat_cfg["TPO"]) if isinstance(strat_cfg.get("TPO"), str) else strat_cfg.get("TPO", {})
    cfg_eap = json.loads(strat_cfg["EAP"]) if isinstance(strat_cfg.get("EAP"), str) else strat_cfg.get("EAP", {})

    ctl_set         = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_set         = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_set         = run_tpo(ctl_set, stock_map, cfg_tpo)
    rule_engine_set = ctl_set | sbs_set | tpo_set

    min_score         = cfg_float("min_score_to_show", 50)
    show_watch        = cfg_bool("show_watching_stocks", True)
    min_score_adj     = cfg_float("min_score_adjusted", _MIN_SCORE_ADJ_DEFAULT)

    signals = []

    for msl_row in msl:
        sym       = msl_row.get("symbol")
        in_pos    = sym in open_map
        pos       = open_map.get(sym, {})
        sector    = msl_row.get("sector", "")
        in_engine = sym in rule_engine_set

        # ── Industry context ──────────────────────────────────────────────────
        # FIX 10: extracted and applied BEFORE min_score gate so the same
        # score is used consistently throughout classification.
        industry  = stock_map.get(sym, {}).get("industry", "") or msl_row.get("industry", "")
        ind_ctx   = industry_map.get(industry, {})
        ind_rank  = ind_ctx.get("rank")
        ind_top5  = ind_ctx.get("top5_flag", False)
        ind_state = ind_ctx.get("industry_state", "")
        ind_rsi_d = ind_ctx.get("avg_rsi_daily")

        score = float(msl_row.get("final_score") or 0)

        # FIX 10: industry bonus applied before min_score gate
        if cfg("industry_scoring_active") == "true":
            if ind_top5:              score += 10
            if ind_state == "STRONG": score += 5

        if score < min_score:
            continue

        # ── Strategy tag ──────────────────────────────────────────────────────
        if sym in tpo_set:   strat_tag = "TPO"
        elif sym in ctl_set: strat_tag = "CTL"
        elif sym in sbs_set: strat_tag = "SBS"
        else:                strat_tag = msl_row.get("strategy_source", "")

        # ── Safety flags ──────────────────────────────────────────────────────
        asm_flag    = sym in asm_set
        fo_ban_flag = sym in fo_ban_set
        eap_action  = get_eap_action(sym, sector, events, cfg_eap)

        filter_reason  = None
        signal_subtype = None
        regime_warning = False

        # ── Signal classification ─────────────────────────────────────────────

        if in_pos:
            # OPEN POSITION PATH — v4 classify_open_position_signal
            signal_type, position_state, filter_reason = classify_open_position_signal(
                msl_row, pos
            )

        else:
            # NEW ENTRY PATH — v4 classify_entry_signal (now takes stock_data)
            stock_data = stock_map.get(sym, {})
            is_entry, signal_type_candidate, filter_reason = classify_entry_signal(
                msl_row, open_map, stock_data
            )

            if is_entry:
                position_state = "BUY_CANDIDATE"
                signal_type    = signal_type_candidate
                signal_subtype = filter_reason
                filter_reason  = None
            else:
                position_state = "WATCHING"
                signal_type    = "WATCH"
                if not show_watch:
                    continue

        # ── FIX 9: ASM override scoped to new entries ONLY ───────────────────
        # v3 wrongly overrode EXIT/REDUCE/HOLD on open positions to BLOCKED_ASM,
        # suppressing critical management signals on ASM-listed held stocks.
        # asm_flag stays in the signal dict as informational for all signals.
        if asm_flag and position_state != "OPEN_POSITION":
            signal_type = "BLOCKED_ASM"

        # ── Regime warning + block ────────────────────────────────────────────
        if position_state == "BUY_CANDIDATE":
            if regime_name == "RISK OFF":
                regime_warning = True
                if block_buys:
                    signal_type = "BUY_BLOCKED_REGIME"
            elif regime_name == "CAUTION":
                regime_warning = True
                # Score penalty applied in compute_adjusted_score below

        # ── EAP override — scoped to new entries only (unchanged from v3) ─────
        # position_state == "BUY_CANDIDATE" already ensures this only fires
        # for new entries; open positions have position_state == "OPEN_POSITION"
        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"

        # ── Score adjustment ──────────────────────────────────────────────────
        score_adjusted = compute_adjusted_score(msl_row, score, signal_type, regime_name)

        # ── FIX 11: score_adjusted floor for entry signals ────────────────────
        # A PRIME_SETUP with score_adjusted=38 after regime+risk penalties is
        # not a tradeable signal. Preserve the original signal_type in
        # signal_subtype for analysis before overriding.
        if signal_type in ENTRY_SIGNAL_TYPES and score_adjusted < min_score_adj:
            signal_subtype = signal_type     # preserve for downstream analysis
            signal_type    = "WATCH"
            position_state = "WATCHING"
            filter_reason  = f"low_adj_score_{score_adjusted:.0f}"
            if not show_watch:
                continue

        # ── Build signal dict ──────────────────────────────────────────────────
        sig = {
            # Identity
            "date":            str(run_date),
            "symbol":          sym,
            "company_name":    msl_row.get("company_name"),
            "sector":          sector,
            "strategy":        strat_tag,
            # Signal classification
            "signal_type":     signal_type,
            "signal_subtype":  signal_subtype,
            "position_state":  position_state,
            "score":           score,
            "score_adjusted":  score_adjusted,
            # Rule engine + scanner
            "in_rule_engine":  in_engine,
            "in_scanner":      False,
            "eap_action":      eap_action,
            # Regime
            "regime":          regime_name,
            "regime_warning":  regime_warning,
            # Safety
            "asm_flag":        asm_flag,
            "fo_ban_flag":     fo_ban_flag,
            "fii_flag":        fii_flag,
            # Industry
            "industry":        industry,
            "industry_rank":   ind_rank,
            "industry_top5":   ind_top5,
            "industry_state":  ind_state,
            "industry_avg_rsi": ind_rsi_d,
            # G1: stock technicals at signal time (stock_data_daily)
            "rsi_daily":       stock_map.get(sym, {}).get("rsi_daily"),
            "rsi_weekly":      stock_map.get(sym, {}).get("rsi_weekly"),
            "adx":             stock_map.get(sym, {}).get("adx"),
            "vol_ratio":       stock_map.get(sym, {}).get("vol_ratio"),
            "delivery_pct":    stock_map.get(sym, {}).get("delivery_pct"),
            "atr_pct":         stock_map.get(sym, {}).get("atr_pct"),
            "ret_6m":          stock_map.get(sym, {}).get("ret_6m"),
            "dist_sma50":      stock_map.get(sym, {}).get("dist_sma50"),
            "rsi_monthly":     stock_map.get(sym, {}).get("rsi_monthly"),
            "rs_vs_nifty":     stock_map.get(sym, {}).get("rs_vs_nifty"),
            "consol_range":    stock_map.get(sym, {}).get("consol_range"),
            "ret_1m":          stock_map.get(sym, {}).get("ret_1m"),
            "ret_3m":          stock_map.get(sym, {}).get("ret_3m"),
            "above_sma50":     stock_map.get(sym, {}).get("above_sma50"),
            "breakout_setup":  stock_map.get(sym, {}).get("breakout_setup"),
            # MSL fields
            "days_in_list":    msl_row.get("days_in_list"),
            "validity_score":  msl_row.get("validity_score"),
            "expected_r_msl":  msl_row.get("expected_r"),
            "sheet_conflict":      False,
            "sheet_conflict_type": None,
            "days_to_trigger_est": msl_row.get("days_to_trigger_est"),
            "filter_reason":       filter_reason,
            # Point-in-time sector rank
            "sector_rank_at_entry": sector_rank.get(sector) if sector else None,
            # compute_msl AI_FEED_FIELDS (full set — feeds steps 18–20)
            "momentum_state":       msl_row.get("momentum_state"),
            "momentum_phase":       msl_row.get("momentum_phase"),
            "velocity_state":       msl_row.get("velocity_state"),
            "trend_maturity":       msl_row.get("trend_maturity"),
            "lifecycle":            msl_row.get("lifecycle"),
            "struct_edge":          msl_row.get("struct_edge"),
            "entry_timing_type":    msl_row.get("entry_timing_type"),
            "holding_score":        msl_row.get("holding_score"),
            "risk_score":           msl_row.get("risk_score"),
            "momentum_score":       msl_row.get("momentum_score"),
            "institutional_score":  msl_row.get("institutional_score"),
            "breakout_readiness":   msl_row.get("breakout_readiness"),
            "active_regime":        msl_row.get("active_regime"),
            "rsi_extended_thresh":  msl_row.get("rsi_extended_thresh"),
            # Signal context fields
            "bb_context":           msl_row.get("bb_context"),
            "bb_squeeze":           msl_row.get("bb_squeeze"),
            "bb_position_pct":      msl_row.get("bb_position_pct"),
            "vwap_alignment":       msl_row.get("vwap_alignment"),
            "dist_vwap_20d_pct":    msl_row.get("dist_vwap_20d_pct"),
            "volume_trend":         msl_row.get("volume_trend"),
            "weekly_structure":     msl_row.get("weekly_structure"),
            "macd_direction":       msl_row.get("macd_direction"),
            "macd_crossing_up":     msl_row.get("macd_crossing_up"),
            "psar_dual_confirmed":  msl_row.get("psar_dual_confirmed"),
            "st_cushion_pct":       msl_row.get("st_cushion_pct"),
            "ha_signal":            msl_row.get("ha_signal"),
            "stoch_context":        msl_row.get("stoch_context"),
            "fundamental_quality":  msl_row.get("fundamental_quality"),
            "ma_alignment_score":   msl_row.get("ma_alignment_score"),
            "liquidity_quality":    msl_row.get("liquidity_quality"),
            "low_liquidity":        msl_row.get("low_liquidity"),
            "persistent_phase":     msl_row.get("persistent_phase"),
            "reentry_mode":         msl_row.get("reentry_mode"),
            # Market context at signal time
            "india_vix":         india_vix_ctx,
            "nifty_5d_chg_pct":  nifty_5d_ctx,
            "above_200dma_pct":  above_200dma_ctx,
            "fii_net_20d_ctx":   fii_net_20d,
        }
        signals.append(sig)

    signals = _apply_scanner_crossref(signals, sb, run_date)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# SAVE  (unchanged from v3)
# ─────────────────────────────────────────────────────────────────────────────

def save_signals(signals):
    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(signals)} signals")
        return
    sb = get_supabase()
    if signals:
        sb.table("signal_log").upsert(signals, on_conflict="date,symbol").execute()
    logger.info(f"✓ {len(signals)} signals written to signal_log")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.error("KILL SWITCH ACTIVE — aborting")
        return {}

    logger.info("=" * 60)
    logger.info("STEP [15]: Generate Signals v4")
    logger.info("=" * 60)

    signals = generate()

    from collections import Counter
    types    = Counter(s["signal_type"]           for s in signals)
    subtypes = Counter(s.get("signal_subtype")    for s in signals if s.get("signal_subtype"))
    reasons  = Counter(s.get("filter_reason")     for s in signals if s.get("filter_reason"))

    logger.info(f"Signal breakdown:   {dict(types)}")
    logger.info(f"Subtype breakdown:  {dict(subtypes)}")

    buy_candidates = [s for s in signals if s["signal_type"] in ENTRY_SIGNAL_TYPES]
    prime_setups   = [s for s in signals if s["signal_type"] == "PRIME_SETUP"]
    reentry_setups = [s for s in signals if s["signal_type"] == "REENTRY_SETUP"]
    exits          = [s for s in signals if s["signal_type"] == "EXIT"]
    reduces        = [s for s in signals if s["signal_type"] == "REDUCE"]
    decel_holds    = [s for s in signals if s.get("filter_reason") == "hold_deceleration_monitor"]
    risk_off_warns = [s for s in signals if s.get("regime_warning")]
    watch_signals  = [s for s in signals if s["signal_type"] == "WATCH"]

    logger.info(f"  BUY CANDIDATES:  {len(buy_candidates)} (PRIME: {len(prime_setups)} | REENTRY: {len(reentry_setups)})")
    logger.info(f"  EXIT signals:    {len(exits)}")
    logger.info(f"  REDUCE signals:  {len(reduces)}")
    logger.info(f"  DECEL HOLDs:     {len(decel_holds)}")
    logger.info(f"  REGIME WARNS:    {len(risk_off_warns)}")

    if watch_signals:
        watch_reasons = Counter(s.get("filter_reason", "unknown") for s in watch_signals)
        logger.info(f"  WATCH signals ({len(watch_signals)}) by filter reason:")
        for reason, count in watch_reasons.most_common(10):
            examples = [s["symbol"] for s in watch_signals
                        if s.get("filter_reason") == reason][:4]
            logger.info(f"    {reason}: {count} — e.g. {', '.join(examples)}")

    # Log v4-specific breakdowns
    rr_blocks     = [s for s in signals if "insufficient_rr" in (s.get("filter_reason") or "")]
    atr_blocks    = [s for s in signals if "atrs_limit" in (s.get("filter_reason") or "")]
    breakdown_blk = [s for s in signals if s.get("filter_reason") == "structural_breakdown"]
    pullback_ok   = [s for s in signals if s.get("signal_subtype") == "pullback_to_zone"]
    tgt_reached   = [s for s in signals if s.get("filter_reason") == "target_already_reached"]
    adj_score_blk = [s for s in signals if "low_adj_score" in (s.get("filter_reason") or "")]
    st_exits      = [s for s in signals if s.get("filter_reason") == "supertrend_broken"]

    logger.info(f"  [v4] R:R blocks:        {len(rr_blocks)}")
    logger.info(f"  [v4] ATR chase blocks:  {len(atr_blocks)}")
    logger.info(f"  [v4] Breakdowns:        {len(breakdown_blk)}")
    logger.info(f"  [v4] Valid pullbacks:   {len(pullback_ok)}")
    logger.info(f"  [v4] Target reached:   {len(tgt_reached)}")
    logger.info(f"  [v4] Adj score floor:  {len(adj_score_blk)}")
    logger.info(f"  [v4] Supertrend exits: {len(st_exits)}")

    if prime_setups:
        prime_str = ", ".join(
            f"{s['symbol']}({s.get('score_adjusted', 0):.0f})"
            for s in sorted(prime_setups,
                            key=lambda x: x.get("score_adjusted", 0), reverse=True)[:8]
        )
        logger.info(f"  ★ PRIME SETUPS: {prime_str}")

    if signals:
        s0 = signals[0]
        logger.info(
            f"  Market context: VIX={s0.get('india_vix','?')} "
            f"Nifty5d={s0.get('nifty_5d_chg_pct','?')} "
            f"Breadth={s0.get('above_200dma_pct','?')}% "
            f"FII20d={s0.get('fii_net_20d_ctx','?')}"
        )

    save_signals(signals)
    return {
        "signals":        len(signals),
        "buy_candidates": len(buy_candidates),
        "prime_setups":   len(prime_setups),
        "reentry_setups": len(reentry_setups),
        "exits":          len(exits),
        "reduces":        len(reduces),
        "decel_holds":    len(decel_holds),
        "rr_blocks":      len(rr_blocks),
        "atr_blocks":     len(atr_blocks),
        "breakdown_blocks": len(breakdown_blk),
        "pullback_ok":    len(pullback_ok),
    }


if __name__ == "__main__":
    main()