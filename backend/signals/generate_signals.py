"""
TradeOS v6 — Signal Generation Engine v3
=========================================
Pipeline position: [15] — after compute_msl v3, before msl_history.

WHAT CHANGED FROM v2 → v3
===========================

1. SIGNAL SUBTYPES — PRIME/STAGED/BREAKOUT/REENTRY now actually generated
   v2 only produced BUY_CANDIDATE/WATCH. v3 classifies into:
     PRIME_SETUP      → HOT + EXPANSION + ACCELERATING + struct_edge=YES + breakout_readiness≥55
     BREAKOUT_SETUP   → bb_squeeze or near_30d_high + volume expanding
     REENTRY_SETUP    → REENTRY timing type + pullback confirmed
     STAGED_ENTRY     → APPROACHING zone + BUILDING momentum (pre-position)
     BUY_CANDIDATE    → in/near zone, decent momentum (existing)

2. RISK GATING — compute_msl risk_score now gates entry
   risk_score > 80 → BLOCKED_HIGH_RISK (no entry regardless of momentum)
   risk_score > 65 → score_adjusted penalised, signal_subtype annotated

3. LIQUIDITY GATING — low_liquidity flag from compute_msl blocks entry
   liquidity_quality = VERY_LOW → BLOCKED_LOW_LIQUIDITY

4. OPEN POSITION LOGIC — now driven by compute_msl lifecycle + holding_score
   v2 only used open_positions.action_required (manual field)
   v3 uses holding_score + lifecycle + velocity_state from compute_msl:
     holding_score < 30 OR lifecycle = EXIT → EXIT signal
     lifecycle = ADD + velocity_state = ACCELERATING → ADD signal
     lifecycle = REDUCE → REDUCE signal (new type)
     otherwise → HOLD

5. SCORE ADJUSTMENT — score_adjusted now uses compute_msl sub-scores
   momentum_score, institutional_score, breakout_readiness feed into
   score_adjusted alongside the base final_score and scanner cross-ref.

6. SIGNAL DICT ENRICHMENT — 20+ compute_msl fields added to signal dict
   Feeds steps 18 (generate_shortlist) and 20 (ai_enrich) with full context.
   All AI_FEED_FIELDS from compute_msl are now present in every signal row.
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

# risk_score thresholds for entry gating
RISK_BLOCK_THRESHOLD   = 80   # hard block regardless of momentum
RISK_PENALTY_THRESHOLD = 65   # score_adjusted penalised

# holding_score threshold below which open positions trigger EXIT review
HOLDING_SCORE_EXIT_THRESHOLD = 30

# breakout_readiness threshold for PRIME_SETUP classification
PRIME_BREAKOUT_MIN = 55

# Minimum holding_score for an ADD signal to fire
ADD_HOLDING_MIN = 60


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_today_data():
    """
    Load all required data in bulk.
    Returns all compute_msl-enriched master_shortlist rows — the full SELECT *
    ensures every field written by compute_msl v3 is available downstream.
    """
    sb    = get_supabase()
    today = str(today_ist())

    # ── stock_data_daily ─────────────────────────────────────────────────────
    stocks = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stocks:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            last_date = latest[0]["date"]
            stocks = sb.table("stock_data_daily").select("*").eq("date", last_date).execute().data
            logger.info(f"No stock_data_daily for {today} — using last trading day {last_date}")

    # ── master_shortlist (full SELECT * — compute_msl fields included) ────────
    msl = sb.table("master_shortlist").select("*").eq("date", today).execute().data
    if not msl:
        latest_msl = sb.table("master_shortlist").select("date").order("date", desc=True).limit(1).execute().data
        if latest_msl:
            last_msl_date = latest_msl[0]["date"]
            msl = sb.table("master_shortlist").select("*").eq("date", last_msl_date).execute().data
            logger.warning(f"No MSL for {today} — using last available date {last_msl_date} ({len(msl)} rows)")

    # ── open positions ────────────────────────────────────────────────────────
    open_p = sb.table("open_positions").select("*").execute().data

    # ── regime ───────────────────────────────────────────────────────────────
    regime = sb.table("market_regime").select("*").eq("date", today).execute().data
    if not regime:
        regime = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data

    # ── supporting tables ─────────────────────────────────────────────────────
    events   = sb.table("event_calendar").select("*").eq("is_active", True).execute().data
    safety   = sb.table("safety_lists").select("symbol,list_type").execute().data
    industry_str = sb.table("industry_strength").select("*").eq("date", str(today_ist())).execute().data
    industry_map = {r["industry"]: r for r in industry_str}

    stock_map  = {s["symbol"]: s for s in stocks}
    open_map   = {p["symbol"]: p for p in open_p}
    regime_obj = regime[0] if regime else {}
    asm_set    = {r["symbol"] for r in safety if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety if r["list_type"] == "FO_BAN"}

    # ── FII flow (v2 pattern retained) ───────────────────────────────────────
    fii_rows = (
        sb.table("fii_dii_flow")
        .select("fii_flag,fii_net_20d")
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    fii_flag    = fii_rows[0].get("fii_flag", "NEUTRAL") if fii_rows else "NEUTRAL"
    fii_net_20d = fii_rows[0].get("fii_net_20d")         if fii_rows else None

    compute_source_counts = {}
    for r in msl:
        src = r.get("compute_source") or "unknown"
        compute_source_counts[src] = compute_source_counts.get(src, 0) + 1
    logger.info(f"  MSL rows loaded: {len(msl)} | compute_source breakdown: {compute_source_counts}")

    return (
        stock_map, msl, open_map, regime_obj,
        events, asm_set, fo_ban_set, industry_map,
        fii_flag, fii_net_20d
    )


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY RULE ENGINES (unchanged from v2)
# ─────────────────────────────────────────────────────────────────────────────

def run_ctl(stocks: dict, sectors_by_name: dict, cfg_ctl: dict) -> set:
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_ctl.get("max_sector_rank", 4):
            continue
        if (s.get("rsi_monthly") or 0) < cfg_ctl.get("min_monthly_rsi", 58):
            continue
        if (s.get("rsi_weekly") or 0) < cfg_ctl.get("min_weekly_rsi", 58):
            continue
        if (s.get("ret_6m") or -999) < cfg_ctl.get("min_6m_return", 0):
            continue
        if (s.get("atr_pct") or 999) > cfg_ctl.get("max_atr_pct", 4):
            continue
        if (s.get("market_cap") or 0) < cfg_ctl.get("min_market_cap", 500):
            continue
        candidates.add(sym)
    return candidates


def run_sbs(stocks: dict, sectors_by_name: dict, cfg_sbs: dict) -> set:
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_sbs.get("max_sector_rank", 7):
            continue
        if (s.get("rsi_daily") or 0) < cfg_sbs.get("min_daily_rsi", 55):
            continue
        if (s.get("rsi_weekly") or 0) < cfg_sbs.get("min_weekly_rsi", 56):
            continue
        if (s.get("vol_ratio") or 0) < cfg_sbs.get("min_vol_ratio", 1.3):
            continue
        if (s.get("consol_range") or 999) > cfg_sbs.get("max_consol_pct", 12):
            continue
        if (s.get("atr_pct") or 999) > cfg_sbs.get("max_atr_pct", 5):
            continue
        if (s.get("market_cap") or 0) < cfg_sbs.get("min_market_cap", 300):
            continue
        candidates.add(sym)
    return candidates


def run_tpo(ctl_candidates: set, stocks: dict, cfg_tpo: dict) -> set:
    candidates = set()
    for sym in ctl_candidates:
        s   = stocks.get(sym, {})
        rsi = s.get("rsi_daily") or 0
        if not (cfg_tpo.get("min_rsi", 42) <= rsi <= cfg_tpo.get("max_rsi", 55)):
            continue
        if abs(s.get("dist_sma50") or 999) > cfg_tpo.get("max_dist_sma50", 3):
            continue
        if (s.get("atr_pct") or 999) > cfg_tpo.get("max_atr_pct", 4):
            continue
        candidates.add(sym)
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# EAP (unchanged from v2)
# ─────────────────────────────────────────────────────────────────────────────

def get_eap_action(symbol: str, sector: str, events: list, cfg_eap: dict) -> str:
    from datetime import date as _date

    pre_days      = int(cfg_eap.get("pre_event_days", 2))
    HIGH_IMPACT   = {"RESULTS","EARNINGS","QUARTERLY_RESULTS","BOARD_MEETING","BOARD MEETING","FINANCIAL RESULTS"}
    MEDIUM_IMPACT = {"AGM","ANALYST_MEET","CONCALL","INVESTOR_MEET"}
    LOW_IMPACT    = {"DIVIDEND","BONUS","SPLIT","BUYBACK","RIGHTS"}

    def _classify(ev):
        raw = (str(ev.get("event_type") or "") + " " + str(ev.get("purpose") or "")).upper()
        if any(k in raw for k in HIGH_IMPACT):   return "HIGH"
        if any(k in raw for k in MEDIUM_IMPACT): return "MEDIUM"
        if any(k in raw for k in LOW_IMPACT):    return "LOW"
        return "MEDIUM"

    best_action = "NO_CHANGE"
    for ev in events:
        if not ev.get("is_active"):
            continue
        ev_symbol = ev.get("symbol", "")
        affected  = str(ev.get("affected_sectors", "")).lower()
        is_relevant = (
            (ev_symbol and ev_symbol == symbol) or
            (sector and sector.lower() in affected) or
            ev.get("event_category") == "GLOBAL"
        )
        if not is_relevant:
            continue
        start_d = ev.get("start_date") or ev.get("event_date")
        if not start_d:
            continue
        try:
            sd   = _date.fromisoformat(str(start_d)[:10])
            days = (sd - today_ist()).days
        except Exception:
            continue
        impact = _classify(ev)
        if impact == "HIGH":
            if 0 <= days <= pre_days:
                return "AVOID_ENTRY"
            if -pre_days <= days < 0:
                best_action = "PRIORITISE"
        elif impact == "MEDIUM":
            if 0 <= days <= pre_days:
                if best_action != "AVOID_ENTRY":
                    best_action = "AVOID_ENTRY"
        elif impact == "LOW":
            if -pre_days <= days < 0:
                if best_action == "NO_CHANGE":
                    best_action = "PRIORITISE"
    return best_action


# ─────────────────────────────────────────────────────────────────────────────
# OPEN POSITION SIGNAL — v3: uses compute_msl lifecycle + holding_score
# ─────────────────────────────────────────────────────────────────────────────

def classify_open_position_signal(msl_row: dict, pos: dict) -> tuple:
    """
    Determine signal_type for an open position.

    v3 logic (compute_msl authoritative):
      1. holding_score < HOLDING_SCORE_EXIT_THRESHOLD → EXIT (trend broken)
      2. lifecycle = EXIT (from compute_msl) → EXIT
      3. lifecycle = ADD + velocity = ACCELERATING + holding_score ≥ ADD_HOLDING_MIN → ADD
      4. lifecycle = REDUCE → REDUCE
      5. manual action_required from open_positions (fallback, preserves ops override)
      6. Default → HOLD

    Returns (signal_type, position_state, reason)
    """
    holding_score  = float(msl_row.get("holding_score") or 0)
    lifecycle      = (msl_row.get("lifecycle") or "").upper()
    velocity_state = (msl_row.get("velocity_state") or "").upper()
    momentum_state = (msl_row.get("momentum_state") or "").upper()

    # compute_msl authoritative signals — hard gates first
    if holding_score > 0 and holding_score < HOLDING_SCORE_EXIT_THRESHOLD:
        return "EXIT", "OPEN_POSITION", f"holding_score_low_{holding_score:.0f}"

    if lifecycle == "EXIT":
        return "EXIT", "OPEN_POSITION", "lifecycle_exit"

    if lifecycle == "REDUCE":
        return "REDUCE", "OPEN_POSITION", "lifecycle_reduce"

    if (lifecycle == "ADD"
            and velocity_state == "ACCELERATING"
            and holding_score >= ADD_HOLDING_MIN):
        return "ADD", "OPEN_POSITION", "lifecycle_add_accelerating"

    # Fallback: manual action_required from open_positions table (ops override)
    action = (pos.get("action_required") or "").upper()
    if "EXIT" in action or "SELL" in action:
        return "EXIT", "OPEN_POSITION", "manual_action_exit"
    if "ADD" in action:
        return "ADD", "OPEN_POSITION", "manual_action_add"

    return "HOLD", "OPEN_POSITION", "holding"


# ─────────────────────────────────────────────────────────────────────────────
# BUY CANDIDATE CLASSIFICATION — v3: adds risk_score + liquidity gates
# ─────────────────────────────────────────────────────────────────────────────

def classify_entry_signal(msl_row: dict, open_map: dict, threshold: float = None) -> tuple:
    """
    Classify entry signals with full compute_msl context.

    Returns (is_entry: bool, signal_type: str, filter_reason: str)

    Signal type hierarchy (highest quality first):
      PRIME_SETUP    → HOT+EXPANSION+ACCELERATING+struct_edge+breakout_readiness≥55
      BREAKOUT_SETUP → bb_squeeze or near 30d high + volume
      REENTRY_SETUP  → REENTRY timing type (pullback into trend)
      STAGED_ENTRY   → APPROACHING zone, BUILDING momentum
      BUY_CANDIDATE  → in/near zone, decent setup
      False          → WATCH with filter_reason

    Hard blocks (return False immediately):
      - already in position
      - missing price data
      - risk_score > RISK_BLOCK_THRESHOLD
      - low_liquidity = True
      - lifecycle = EXIT
      - EXTENDED timing without reentry
    """
    sym = msl_row.get("symbol")

    if sym in open_map:
        return False, None, "already_in_position"

    # ── Compute_msl fields ────────────────────────────────────────────────────
    ep             = msl_row.get("entry_zone_low")
    cp             = msl_row.get("current_price")
    risk_score     = float(msl_row.get("risk_score") or 0)
    low_liquidity  = bool(msl_row.get("low_liquidity"))
    liquidity_qual = (msl_row.get("liquidity_quality") or "").upper()

    lifecycle         = (msl_row.get("lifecycle") or "").upper()
    entry_timing_type = (msl_row.get("entry_timing_type") or "").upper()
    momentum_state    = (msl_row.get("momentum_state") or "").upper()
    momentum_phase    = (msl_row.get("momentum_phase") or "").upper()
    velocity_state    = (msl_row.get("velocity_state") or "").upper()
    struct_edge       = (msl_row.get("struct_edge") or "").upper()
    reentry_mode      = (msl_row.get("reentry_mode") or "").upper()
    trend_maturity    = (msl_row.get("trend_maturity") or "").upper()
    breakout_readiness = float(msl_row.get("breakout_readiness") or 0)
    bb_squeeze        = bool(msl_row.get("bb_squeeze"))
    volume_trend      = (msl_row.get("volume_trend") or "").upper()

    # ── Hard blocks ───────────────────────────────────────────────────────────
    if not ep or not cp or ep <= 0:
        return False, None, "missing_price_data"

    if risk_score >= RISK_BLOCK_THRESHOLD:
        return False, None, f"blocked_high_risk_{risk_score:.0f}"

    value_cr_known = float(msl_row.get("value_cr") or 0) > 0
    if value_cr_known and (low_liquidity or liquidity_qual == "VERY_LOW"):
        return False, None, "blocked_low_liquidity"
    elif not value_cr_known and liquidity_qual == "VERY_LOW":
        # value_cr=0 means ingest_bhavcopy hasn't run — don't gate on missing data
        pass

    if lifecycle == "EXIT":
        return False, None, "exit_lifecycle_no_entry"

    if entry_timing_type == "EXTENDED" and reentry_mode != "ELIGIBLE":
        return False, None, "extended_timing_no_reentry"

    # ── Zone proximity check ──────────────────────────────────────────────────
    if threshold is None:
        threshold = buy_candidate_threshold()

    eh             = msl_row.get("entry_zone_high") or ep * 1.02
    chase_soft     = 0.03
    chase_hard     = 0.08
    dist_from_high = (cp - eh) / eh

    below_zone     = cp < ep * (1 - threshold)
    if below_zone:
        return False, None, "below_zone_wait"

    near_zone_low  = cp < ep
    in_zone        = ep <= cp <= eh

    is_strong_momentum = (
        momentum_state == "HOT"
        and momentum_phase in ("EARLY", "EXPANSION")
        and velocity_state == "ACCELERATING"
    )
    is_decent_momentum = (
        momentum_state not in ("WEAK",)
        and momentum_phase not in ("EXTENDED", "FLAT")
        and velocity_state != "DECELERATING"
    )
    is_chasing_type = entry_timing_type == "CHASING"

    # Price is in/near zone — now determine the best signal subtype
    price_qualifies = (
        near_zone_low or in_zone
        or (dist_from_high <= chase_soft and (is_strong_momentum or is_decent_momentum))
        or (dist_from_high <= chase_hard and is_strong_momentum and struct_edge == "YES" and not is_chasing_type)
        or (dist_from_high <= 0.06 and momentum_state == "HOT" and struct_edge == "YES")
    )

    if not price_qualifies:
        if dist_from_high > chase_hard:
            return False, None, "far_above_extended"
        if dist_from_high > chase_soft:
            return False, None, "moderate_above_insufficient_momentum"
        return False, None, "slight_above_weak_momentum"

    # ── Signal subtype classification (highest quality first) ─────────────────
    #
    # PRIME_SETUP: everything aligned — momentum HOT+EXPANSION+ACCELERATING,
    #              structural edge confirmed, breakout imminent
    if (momentum_state == "HOT"
            and momentum_phase == "EXPANSION"
            and velocity_state == "ACCELERATING"
            and struct_edge == "YES"
            and breakout_readiness >= PRIME_BREAKOUT_MIN
            and trend_maturity not in ("EXHAUSTED", "LATE")):
        return True, "PRIME_SETUP", "prime_all_aligned"

    # BREAKOUT_SETUP: BB squeeze firing or approaching 30d high with volume
    if (bb_squeeze or breakout_readiness >= 70) and volume_trend == "EXPANDING":
        return True, "BREAKOUT_SETUP", "breakout_squeeze_volume"

    if breakout_readiness >= 60 and momentum_state in ("HOT", "BUILDING"):
        return True, "BREAKOUT_SETUP", "breakout_readiness_high"

    # REENTRY_SETUP: clear pullback into trend (best R:R after PRIME)
    if entry_timing_type == "REENTRY" or reentry_mode == "ELIGIBLE":
        return True, "REENTRY_SETUP", "reentry_pullback"

    # STAGED_ENTRY: approaching zone with building momentum — pre-position alert
    if entry_timing_type == "APPROACHING" and momentum_state in ("HOT", "BUILDING"):
        return True, "STAGED_ENTRY", "approaching_zone_building"

    # BUY_CANDIDATE: standard in-zone or near-zone entry
    return True, "BUY_CANDIDATE", "in_zone"


# ─────────────────────────────────────────────────────────────────────────────
# SCORE ADJUSTMENT — v3: incorporates compute_msl sub-scores
# ─────────────────────────────────────────────────────────────────────────────

def compute_adjusted_score(msl_row: dict, base_score: float,
                            signal_type: str, regime_name: str) -> float:
    """
    Adjust final_score using compute_msl sub-scores for a more differentiated
    score that AI steps 18-20 can use to rank setups.

    Adjustments (all bounded so final stays 0-100):
      momentum_score ≥ 80:  +3  (powerful momentum confirmation)
      institutional_score ≥ 60: +2  (smart money aligned)
      breakout_readiness ≥ 65: +2  (imminent trigger)
      risk_score ≥ 65:     -5  (elevated but not blocking)
      risk_score ≥ 50:     -2
      PRIME_SETUP:         +5  (highest quality signal)
      BREAKOUT_SETUP:      +3
      REENTRY_SETUP:       +2
      CAUTION regime:      ×0.85 (v2 behaviour preserved)
      psar_dual_confirmed: +2
      weekly_structure = STRONG: +1
    """
    score = float(base_score or 0)

    momentum_score      = float(msl_row.get("momentum_score") or 0)
    institutional_score = float(msl_row.get("institutional_score") or 0)
    breakout_readiness  = float(msl_row.get("breakout_readiness") or 0)
    risk_score          = float(msl_row.get("risk_score") or 0)
    psar_dual           = bool(msl_row.get("psar_dual_confirmed"))
    weekly_struct       = (msl_row.get("weekly_structure") or "").upper()

    # Sub-score bonuses
    if momentum_score >= 80:       score += 3
    elif momentum_score >= 70:     score += 1
    if institutional_score >= 60:  score += 2
    if breakout_readiness >= 65:   score += 2
    if psar_dual:                  score += 2
    if weekly_struct == "STRONG":  score += 1

    # Risk penalties
    if risk_score >= RISK_PENALTY_THRESHOLD:   score -= 5
    elif risk_score >= 50:                     score -= 2

    # Signal type bonus
    if signal_type == "PRIME_SETUP":       score += 5
    elif signal_type == "BREAKOUT_SETUP":  score += 3
    elif signal_type == "REENTRY_SETUP":   score += 2

    # Regime penalty (v2 behaviour preserved)
    if regime_name == "CAUTION":
        score = round(score * 0.85, 1)

    return round(min(max(score, 0.0), 100.0), 1)


# ─────────────────────────────────────────────────────────────────────────────
# SCANNER CROSS-REFERENCE (unchanged from v2)
# ─────────────────────────────────────────────────────────────────────────────

def _apply_scanner_crossref(signals: list, sb, run_date) -> list:
    try:
        scanner_rows = (
            sb.table("scanner_signals")
            .select("symbol,pattern_type")
            .eq("date", str(run_date))
            .execute()
            .data
        )
        if not scanner_rows:
            return signals

        scanner_map = {}
        for row in scanner_rows:
            sym = row.get("symbol") or ""
            pat = row.get("pattern_type") or ""
            if sym:
                scanner_map.setdefault(sym, []).append(pat)

        entry_types = {"BUY_CANDIDATE", "PRIME_SETUP", "STAGED_ENTRY",
                       "REENTRY_SETUP", "BREAKOUT_SETUP"}
        updated = 0
        for sig in signals:
            sym = sig["symbol"]
            if sym in scanner_map:
                sig["in_scanner"]       = True
                sig["scanner_patterns"] = ",".join(scanner_map[sym])
                if sig.get("in_rule_engine") and sig.get("signal_type") in entry_types:
                    current = sig.get("score_adjusted") or sig.get("score") or 0
                    sig["score_adjusted"] = round(float(current) + 5, 1)
                    updated += 1

        logger.info(f"Scanner cross-ref: {len(scanner_map)} hits | {updated} signals +5")
    except Exception as e:
        logger.warning(f"Scanner cross-ref failed (non-fatal): {e}")
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATION LOOP
# ─────────────────────────────────────────────────────────────────────────────

def generate(run_date: date | None = None) -> list:
    run_date  = run_date or today_ist()
    strat_cfg = get_strategy_config()

    (stock_map, msl, open_map, regime,
     events, asm_set, fo_ban_set, industry_map,
     fii_flag, fii_net_20d) = load_today_data()

    # ── Regime resolution (identical to v2) ───────────────────────────────────
    def _resolve_regime(regime_obj: dict) -> str:
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

    # v2 market-context fields (retained)
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

    ctl_set = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_set = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_set = run_tpo(ctl_set, stock_map, cfg_tpo)
    rule_engine_set = ctl_set | sbs_set | tpo_set

    min_score  = cfg_float("min_score_to_show", 50)
    show_watch = cfg_bool("show_watching_stocks", True)

    signals = []

    for msl_row in msl:
        sym   = msl_row.get("symbol")
        score = float(msl_row.get("final_score") or 0)
        if score < min_score:
            continue

        sector     = msl_row.get("sector", "")
        in_pos     = sym in open_map
        pos        = open_map.get(sym, {})
        eap_action = get_eap_action(sym, sector, events, cfg_eap)
        in_engine  = sym in rule_engine_set

        if sym in tpo_set:    strat_tag = "TPO"
        elif sym in ctl_set:  strat_tag = "CTL"
        elif sym in sbs_set:  strat_tag = "SBS"
        else:                 strat_tag = msl_row.get("strategy_source", "")

        # ── Industry context ───────────────────────────────────────────────────
        industry  = stock_map.get(sym, {}).get("industry", "") or msl_row.get("industry", "")
        ind_ctx   = industry_map.get(industry, {})
        ind_rank  = ind_ctx.get("rank")
        ind_top5  = ind_ctx.get("top5_flag", False)
        ind_state = ind_ctx.get("industry_state", "")
        ind_rsi_d = ind_ctx.get("avg_rsi_daily")

        if cfg("industry_scoring_active") == "true":
            if ind_top5:              score = score + 10
            if ind_state == "STRONG": score = score + 5

        # ── Safety flags ───────────────────────────────────────────────────────
        asm_flag    = sym in asm_set
        fo_ban_flag = sym in fo_ban_set

        filter_reason  = None
        signal_subtype = None
        regime_warning = False

        # ── OPEN POSITION: v3 compute_msl lifecycle + holding_score driven ─────
        if in_pos:
            signal_type, position_state, filter_reason = classify_open_position_signal(
                msl_row, pos
            )

        # ── NEW ENTRY: v3 risk + liquidity gating + subtype classification ──────
        else:
            is_entry, signal_type_candidate, filter_reason = classify_entry_signal(
                msl_row, open_map
            )

            if is_entry:
                position_state = "BUY_CANDIDATE"
                signal_type    = signal_type_candidate
                signal_subtype = filter_reason   # e.g. "prime_all_aligned", "in_zone"
            else:
                position_state = "WATCHING"
                signal_type    = "WATCH"
                if not show_watch:
                    continue

        # ── ASM override (after position classification) ───────────────────────
        if asm_flag:
            signal_type = "BLOCKED_ASM"

        # ── Regime warning + block ─────────────────────────────────────────────
        if position_state == "BUY_CANDIDATE":
            if regime_name == "RISK OFF":
                regime_warning = True
                if block_buys:
                    signal_type = "BUY_BLOCKED_REGIME"
            elif regime_name == "CAUTION":
                regime_warning = True
                # Score penalty applied in compute_adjusted_score

        # ── EAP override ──────────────────────────────────────────────────────
        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"

        # ── Score adjustment (v3: uses compute_msl sub-scores) ─────────────────
        score_adjusted = compute_adjusted_score(msl_row, score, signal_type, regime_name)

        # ── Build signal dict ──────────────────────────────────────────────────
        sig = {
            # ── Identity ──
            "date":            str(run_date),
            "symbol":          sym,
            "company_name":    msl_row.get("company_name"),
            "sector":          sector,
            "strategy":        strat_tag,
            # ── Signal classification ──
            "signal_type":     signal_type,
            "signal_subtype":  signal_subtype,
            "position_state":  position_state,
            "score":           score,
            "score_adjusted":  score_adjusted,
            # ── Rule engine + scanner ──
            "in_rule_engine":  in_engine,
            "in_scanner":      False,
            "eap_action":      eap_action,
            # ── Regime ──
            "regime":          regime_name,
            "regime_warning":  regime_warning,
            # ── Safety ──
            "asm_flag":        asm_flag,
            "fo_ban_flag":     fo_ban_flag,
            "fii_flag":        fii_flag,
            # ── Industry ──
            "industry":        industry,
            "industry_rank":   ind_rank,
            "industry_top5":   ind_top5,
            "industry_state":  ind_state,
            "industry_avg_rsi": ind_rsi_d,
            # ── G1: stock technicals at signal time (from stock_data_daily) ──
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
            # ── MSL basic fields ──
            "days_in_list":    msl_row.get("days_in_list"),
            "validity_score":  msl_row.get("validity_score"),
            "expected_r_msl":  msl_row.get("expected_r"),
            "sheet_conflict":       False,
            "sheet_conflict_type":  None,
            "days_to_trigger_est":  msl_row.get("days_to_trigger_est"),
            "filter_reason":        filter_reason if position_state in ("WATCHING", "BUY_CANDIDATE") else None,
            # ── AG2: point-in-time sector rank ──
            "sector_rank_at_entry": sector_rank.get(sector) if sector else None,
            # ── v3 NEW: compute_msl AI_FEED_FIELDS (all 12 + supporting) ──────
            # These are the fields that directly feed steps 18-20 (AI).
            # Presence of all these fields = AI generates stock-specific conviction,
            # not generic observations. Missing any = AI falls back to generic output.
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
            # ── v3 NEW: signal context fields ──────────────────────────────────
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
            # ── v2 market-context at signal time (retained, critical for ML) ──
            "india_vix":          india_vix_ctx,
            "nifty_5d_chg_pct":   nifty_5d_ctx,
            "above_200dma_pct":   above_200dma_ctx,
            "fii_net_20d_ctx":    fii_net_20d,
        }
        signals.append(sig)

    signals = _apply_scanner_crossref(signals, sb, run_date)
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# SAVE
# ─────────────────────────────────────────────────────────────────────────────

def save_signals(signals: list):
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
    logger.info("STEP [15]: Generate Signals v3")
    logger.info("=" * 60)

    signals = generate()

    from collections import Counter
    types    = Counter(s["signal_type"]    for s in signals)
    subtypes = Counter(s.get("signal_subtype") for s in signals if s.get("signal_subtype"))
    logger.info(f"Signal breakdown:   {dict(types)}")
    logger.info(f"Subtype breakdown:  {dict(subtypes)}")

    buy_candidates = [s for s in signals if s["signal_type"] in (
        "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP", "REENTRY_SETUP", "STAGED_ENTRY"
    )]
    prime_setups   = [s for s in signals if s["signal_type"] == "PRIME_SETUP"]
    exits          = [s for s in signals if s["signal_type"] == "EXIT"]
    reduces        = [s for s in signals if s["signal_type"] == "REDUCE"]
    risk_off_warns = [s for s in signals if s["regime_warning"]]
    watch_signals  = [s for s in signals if s["signal_type"] == "WATCH"]

    logger.info(f"  BUY CANDIDATES:  {len(buy_candidates)} "
                f"(PRIME: {len(prime_setups)})")
    logger.info(f"  EXIT signals:    {len(exits)}")
    logger.info(f"  REDUCE signals:  {len(reduces)}")
    logger.info(f"  REGIME WARNS:    {len(risk_off_warns)}")

    if watch_signals:
        reasons = Counter(s.get("filter_reason", "unknown") for s in watch_signals)
        logger.info(f"  WATCH signals ({len(watch_signals)}) by filter reason:")
        for reason, count in reasons.most_common():
            examples = [s["symbol"] for s in watch_signals if s.get("filter_reason") == reason][:4]
            logger.info(f"    {reason}: {count} — e.g. {', '.join(examples)}")

    # Log prime setups inline for quick visibility
    if prime_setups:
        prime_str = ", ".join(
            f"{s['symbol']}({s.get('score_adjusted', 0):.0f})" for s in
            sorted(prime_setups, key=lambda x: x.get("score_adjusted", 0), reverse=True)[:8]
        )
        logger.info(f"  ★ PRIME SETUPS: {prime_str}")

    # v2 market context log (retained)
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
        "signals":       len(signals),
        "buy_candidates": len(buy_candidates),
        "prime_setups":  len(prime_setups),
        "exits":         len(exits),
        "reduces":       len(reduces),
    }


if __name__ == "__main__":
    main()