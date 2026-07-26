"""
TradeOS v6 — Signal Generation Engine v5
==========================================
Pipeline position: [15] — after compute_msl, before msl_history.

WHAT CHANGED FROM v4 → v5
===========================

THE CORE PROBLEM FIXED:
  v4 had 40+ threshold values hardcoded as Python constants. These constants
  have never been calibrated against outcomes and never change. The system_config
  table already has 40+ signal_threshold_* keys (from the original setup) but
  v4 NEVER READ THEM. This script fixes that completely.

DYNAMIC THRESHOLD SYSTEM:
  All gate thresholds now read from system_config at the start of generate().
  Defaults match v4 constants exactly — so on first run nothing changes.
  Step 20 (calibration_engine) writes updated values to system_config weekly.
  Next pipeline run picks them up automatically. Zero code changes needed.

SIGNAL_THRESHOLD_* KEYS NOW WIRED IN:
  The following system_config keys were written but never used in v4:
    signal_threshold_prime_rsi_daily_min/max → PRIME_SETUP RSI gate
    signal_threshold_prime_rsi_weekly_min/max → PRIME_SETUP weekly RSI gate
    signal_threshold_prime_adx_min → PRIME_SETUP ADX gate
    signal_threshold_prime_vol_min → PRIME_SETUP volume ratio gate
    signal_threshold_prime_del_min → PRIME_SETUP delivery % gate
    signal_threshold_prime_sma50_max → PRIME_SETUP SMA50 distance gate
    signal_threshold_reentry_* → REENTRY_SETUP gates
    signal_threshold_staged_* → STAGED_ENTRY gates
    signal_threshold_prebreak_* → BREAKOUT_SETUP gates
  These are now applied in Gate 5 subtype classification.

ATR CHASE MATRIX NOW IN SYSTEM_CONFIG:
  Key: atr_chase_matrix (JSON blob)
  Step 20 calibration updates this based on win rates per momentum state.
  Falls back to v4 defaults if key not found.

WATCH SIGNALS NOW CARRY NEAR-MISS METADATA:
  Every WATCH signal now has near_miss_data: {reason, filter_reason,
  original_signal_type, score_adjusted, dist_to_qualify}
  Step 19 reads this to evaluate whether today's macro context upgrades any.
  A stock that was WATCH yesterday due to "chase_2.1atrs_limit_1.8" might
  qualify today if FII is strongly buying that sector.

SCORE ADJUSTMENT BONUSES DYNAMIC:
  Keys: score_bonus_momentum_high, score_bonus_institutional, etc.
  Step 20 calibrates these based on which bonuses actually predict wins.

NO GATE LOGIC CHANGES:
  All 11 gates from v4 are preserved exactly. Only thresholds are now read
  from system_config rather than Python constants. Gate order unchanged.
"""

import sys
import json
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import (
    get_supabase, get_strategy_config,
    cfg, cfg_bool, cfg_float, cfg_int, today_ist,
    buy_candidate_threshold, get_max_positions, DRY_RUN,
    is_kill_switch_active,
)

def _num(d: dict, key: str) -> float | None:
    """
    Null-preserving numeric read.

    `float(d.get(k) or 0)` maps None, "", AND a genuine 0.0 to the same value.
    For an indicator, "not computed" and "computed as zero" are different facts
    and must not collapse — see the note in classify_entry_signal.
    """
    v = d.get(key)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _chk(value: float | None, lo: float | None = None,
         hi: float | None = None) -> bool | None:
    """
    Three-valued range check: True (passes), False (fails), None (unknown).

    None must never be coerced to True by the caller. Callers use _tech() so
    unknowns are counted rather than silently waved through.
    """
    if value is None:
        return None
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _tech(checks: list[bool | None], max_unknown: int = 2) -> tuple[bool, int]:
    """
    Combine three-valued checks into a verdict.

    Returns (passed, n_unknown). A signal passes only when no check FAILED and
    at most `max_unknown` could not be evaluated. Missing data therefore
    weakens a setup toward WATCH instead of strengthening it toward BUY, which
    is the correct direction for a system whose upstream feeds can degrade
    partially without failing loudly.
    """
    n_unknown = sum(1 for c in checks if c is None)
    failed    = any(c is False for c in checks)
    return ((not failed) and n_unknown <= max_unknown), n_unknown


def _implied_rr(msl_row: dict) -> float | None:
    """
    Reward:risk available at the CURRENT price, using the persisted trade plan.

    Recorded on every signal — including WATCH — so post-trade analysis can ask
    "what R:R did we actually accept on winners vs losers?" and the Brain can
    calibrate min_rr from evidence instead of the seeded guess. It is also the
    number the R:R gate rejects on, so storing it makes those rejections
    auditable rather than only appearing inside a filter_reason string.
    """
    stop   = msl_row.get("planned_stop")
    target = msl_row.get("planned_target")
    cp     = msl_row.get("current_price")
    if not (stop and target and cp):
        return None
    try:
        stop_f, target_f, cp_f = float(stop), float(target), float(cp)
    except (TypeError, ValueError):
        return None
    risk = cp_f - stop_f
    if risk <= 0:
        return None
    return round((target_f - cp_f) / risk, 3)


def _build_event_context(symbol: str, event_map: dict) -> dict:
    """
    Returns pre_results_flag, upcoming_event_type, upcoming_event_days
    for a symbol. All nullable — no event = all None/False.
    """
    evt = event_map.get(symbol)
    if not evt:
        return {
            "pre_results_flag":    False,
            "upcoming_event_type": None,
            "upcoming_event_days": None,
        }
    days     = int(evt.get("days_to_event") or 99)
    purpose  = (evt.get("purpose") or "").upper()
    is_results_event = any(
        k in purpose
        for k in ("RESULT", "DIVIDEND", "BOARD MEETING", "AGM", "EGM",
                  "BONUS", "SPLIT", "RIGHTS", "FUND RAISING", "BUYBACK")
    )
    return {
        "pre_results_flag":    is_results_event and days <= 5,
        "upcoming_event_type": evt.get("purpose") if is_results_event else None,
        "upcoming_event_days": days if is_results_event else None,
    }

# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC CONFIG LOADER — reads all thresholds from system_config at runtime
# ─────────────────────────────────────────────────────────────────────────────

def load_signal_thresholds() -> dict:
    """
    Load all signal gate thresholds from system_config.
    Defaults are identical to v4 constants — no behaviour change on first run.
    Step 20 calibration updates these values over time.

    Called once per generate() invocation. All downstream functions receive
    the thresholds dict rather than reading module-level constants.
    """
    return {
        # ── Hard block thresholds ──────────────────────────────────────────
        "risk_block":              cfg_float("risk_block_threshold",           80.0),
        "risk_penalty":            cfg_float("risk_penalty_threshold",         65.0),
        "holding_exit":            cfg_float("holding_score_exit_threshold",   30.0),
        "add_holding_min":         cfg_float("add_holding_min",                60.0),

        # ── Entry quality gates ────────────────────────────────────────────
        "min_rr":                  cfg_float("min_rr_to_enter",                1.0),
        "min_rr_TRENDING":         cfg_float("min_rr_to_enter_TRENDING",       0.9),
        "min_rr_NEUTRAL":          cfg_float("min_rr_to_enter_NEUTRAL",        1.0),
        "min_rr_RISK ON":          cfg_float("min_rr_to_enter_RISK_ON",        1.1),
        "min_rr_RECOVERING":       cfg_float("min_rr_to_enter_RECOVERING",     1.3),
        "min_rr_RISK OFF":         cfg_float("min_rr_to_enter_RISK_OFF",       1.5),
        "min_rr_PRIME_SETUP":      cfg_float("min_rr_prime",                   0.9),
        "min_rr_BREAKOUT_SETUP":   cfg_float("min_rr_breakout",               1.4),
        "min_rr_REENTRY_SETUP":    cfg_float("min_rr_reentry",                1.1),
        "min_rr_STAGED_ENTRY":     cfg_float("min_rr_staged",                 1.0),
        "min_rr_MOMENTUM_CONTINUATION": cfg_float("min_rr_momentum_cont",          0.9),
        "min_score_adj":           cfg_float("min_score_adjusted",             45.0),
        "prime_breakout_min":      cfg_float("prime_breakout_min",             55.0),
        "stop_buffer_pct":         cfg_float("stop_buffer_pct",                0.03),
        "atr_fallback":            cfg_float("atr_fallback_pct",               3.0),

        # ── PRIME_SETUP signal thresholds (from system_config) ─────────────
        "prime_rsi_d_min":         cfg_float("signal_threshold_prime_rsi_daily_min",   48.0),
        "prime_rsi_d_max":         cfg_float("signal_threshold_prime_rsi_daily_max",   82.0),
        "prime_rsi_w_min":         cfg_float("signal_threshold_prime_rsi_weekly_min",  52.0),
        "prime_rsi_w_max":         cfg_float("signal_threshold_prime_rsi_weekly_max",  78.0),
        "prime_rsi_m_min":         cfg_float("signal_threshold_prime_rsi_monthly_min", 48.0),
        "prime_adx_min":           cfg_float("signal_threshold_prime_adx_min",         18.0),
        "prime_vol_min":           cfg_float("signal_threshold_prime_vol_min",          1.15),
        "prime_del_min":           cfg_float("signal_threshold_prime_del_min",         50.0),
        "prime_sma50_max":         cfg_float("signal_threshold_prime_sma50_max",       12.0),
        "prime_consol_max":        cfg_float("signal_threshold_prime_consol_max",       8.0),

        # ── REENTRY_SETUP signal thresholds ────────────────────────────────
        "reentry_rsi_d_min":       cfg_float("signal_threshold_reentry_rsi_daily_min",   38.0),
        "reentry_rsi_d_max":       cfg_float("signal_threshold_reentry_rsi_daily_max",   68.0),
        "reentry_rsi_w_min":       cfg_float("signal_threshold_reentry_rsi_weekly_min",  42.0),
        "reentry_rsi_w_max":       cfg_float("signal_threshold_reentry_rsi_weekly_max",  65.0),
        "reentry_rsi_m_min":       cfg_float("signal_threshold_reentry_rsi_monthly_min", 46.0),
        "reentry_adx_min":         cfg_float("signal_threshold_reentry_adx_min",         14.0),
        "reentry_vol_min":         cfg_float("signal_threshold_reentry_vol_min",          0.55),
        "reentry_del_min":         cfg_float("signal_threshold_reentry_del_min",         44.0),
        "reentry_ret6m_min":       cfg_float("signal_threshold_reentry_ret6m_min",        3.0),
        "reentry_sma50_min":       cfg_float("signal_threshold_reentry_sma50_min",       -8.0),

        # ── STAGED_ENTRY signal thresholds ─────────────────────────────────
        "staged_rsi_d_min":        cfg_float("signal_threshold_staged_rsi_daily_min",   44.0),
        "staged_rsi_d_max":        cfg_float("signal_threshold_staged_rsi_daily_max",   82.0),
        "staged_rsi_w_min":        cfg_float("signal_threshold_staged_rsi_weekly_min",  48.0),
        "staged_adx_min":          cfg_float("signal_threshold_staged_adx_min",         15.0),
        "staged_vol_min":          cfg_float("signal_threshold_staged_vol_min",          0.80),
        "staged_del_min":          cfg_float("signal_threshold_staged_del_min",         46.0),

        # ── BREAKOUT_SETUP signal thresholds ───────────────────────────────
        "prebreak_rsi_d_min":      cfg_float("signal_threshold_prebreak_rsi_daily_min",  42.0),
        "prebreak_rsi_d_max":      cfg_float("signal_threshold_prebreak_rsi_daily_max",  65.0),
        "prebreak_rsi_w_min":      cfg_float("signal_threshold_prebreak_rsi_weekly_min", 44.0),
        "prebreak_adx_min":        cfg_float("signal_threshold_prebreak_adx_min",        10.0),
        "prebreak_adx_max":        cfg_float("signal_threshold_prebreak_adx_max",        26.0),
        "prebreak_vol_max":        cfg_float("signal_threshold_prebreak_vol_max",         1.4),
        "prebreak_consol_max":     cfg_float("signal_threshold_prebreak_consol_max",      9.0),

        # ── Score adjustment bonuses (calibrated by step 20) ───────────────
        "bonus_momentum_high":     cfg_float("score_bonus_momentum_high",     3.0),
        "bonus_momentum_mid":      cfg_float("score_bonus_momentum_mid",      1.0),
        "bonus_institutional":     cfg_float("score_bonus_institutional",     2.0),
        "bonus_breakout_ready":    cfg_float("score_bonus_breakout_ready",    2.0),
        "bonus_psar_dual":         cfg_float("score_bonus_psar_dual",         2.0),
        "bonus_weekly_strong":     cfg_float("score_bonus_weekly_strong",     1.0),
        "penalty_risk_high":       cfg_float("score_penalty_risk_high",      -5.0),
        "penalty_risk_mid":        cfg_float("score_penalty_risk_mid",       -2.0),
        "bonus_prime":             cfg_float("score_bonus_prime_setup",       5.0),
        "bonus_breakout":          cfg_float("score_bonus_breakout_setup",    3.0),
        "bonus_reentry":           cfg_float("score_bonus_reentry_setup",     2.0),

        # ── Industry bonus (calibrated by step 20) ─────────────────────────
        "industry_bonus_top5":     cfg_float("industry_bonus_top5",          10.0),
        "industry_bonus_strong":   cfg_float("industry_bonus_strong",         5.0),

        # ── ATR chase matrix — loaded from JSON blob ───────────────────────
        "atr_matrix": _load_atr_matrix(),
    }


def _load_atr_matrix() -> dict:
    """
    Load ATR chase matrix from system_config.
    Falls back to v4 defaults if key not found or JSON is invalid.
    Key in system_config: atr_chase_matrix
    Updated by step 20 calibration based on win rates per momentum state.
    """
    defaults = {
        "HOT_EARLY_ACCELERATING":      3.0,
        "HOT_EXPANSION_ACCELERATING":  2.5,
        "HOT_EXPANSION_FLAT":          1.8,
        "HOT_ANY_ANY":                 1.8,
        "BUILDING_ACCELERATING":       2.0,
        "BUILDING_FLAT":               1.2,
        "BUILDING_ANY":                1.2,
        "LATE_ANY":                    0.5,
        "EXHAUSTED_ANY":               0.5,
        "EXTENDED_ANY":                0.0,
        "DEFAULT":                     1.0,
    }
    raw = cfg("atr_chase_matrix", "")
    if not raw:
        return defaults
    try:
        loaded = json.loads(raw)
        # Merge loaded over defaults so new keys from calibration appear
        return {**defaults, **loaded}
    except Exception:
        logger.warning("atr_chase_matrix JSON parse failed — using defaults")
        return defaults


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS (structural — not threshold values, not calibrated)
# ─────────────────────────────────────────────────────────────────────────────

ENTRY_SIGNAL_TYPES = frozenset({
    "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
    "REENTRY_SETUP", "STAGED_ENTRY", "MOMENTUM_CONTINUATION",
})

LATE_MATURITY  = frozenset({"LATE", "EXHAUSTED"})
WEAK_STRUCTURE = frozenset({"WEAK", "BEARISH"})

# ─────────────────────────────────────────────────────────────────────────────
# TRADING DAY RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_last_trading_day(sb, reference_date: str, max_lookback: int = 10) -> str:
    """
    Walk backwards from reference_date to find the last valid trading day:
      - Not a Saturday or Sunday
      - Not in nse_holidays table
      - Has actual data in master_shortlist (compute_msl step 14 must have run)

    Raises RuntimeError if no valid day is found within max_lookback days.
    """
    from datetime import datetime, timedelta as _td
    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}
    candidate    = datetime.strptime(reference_date, "%Y-%m-%d").date()

    logger.info(f"  Resolving trading day from {reference_date} (holidays loaded: {len(holidays)})")

    for _ in range(max_lookback):
        date_str = str(candidate)
        dow      = candidate.weekday()   # 0=Mon … 6=Sun

        if dow in (5, 6):
            logger.debug(f"  {date_str} is {'Saturday' if dow == 5 else 'Sunday'} — stepping back")
            candidate -= _td(days=1)
            continue

        if date_str in holidays:
            occasion = next(
                (r.get("occasion", "NSE holiday") for r in holiday_rows if r["date"] == date_str),
                "NSE holiday"
            )
            logger.debug(f"  {date_str} is an NSE holiday ({occasion}) — stepping back")
            candidate -= _td(days=1)
            continue

        probe = (
            sb.table("stock_data_daily")
              .select("date")
              .eq("date", date_str)
              .limit(1)
              .execute().data
        )
        if probe:
            if date_str != reference_date:
                logger.info(f"  Trading day resolved: {reference_date} → {date_str}")
            else:
                logger.info(f"  Trading day confirmed: {date_str}")
            return date_str

        logger.debug(f"  {date_str} — no stock_data_daily rows (bhavcopy not yet run?) — stepping back")
        candidate -= _td(days=1)

    raise RuntimeError(
        f"No valid trading day with stock_data_daily data found "
        f"within {max_lookback} days before {reference_date}. "
        f"Ensure compute_msl has run for a recent trading day."
    )



# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING (identical to v4)
# ─────────────────────────────────────────────────────────────────────────────

def load_today_data(today: str | None = None):
    sb    = get_supabase()
    if today is None:
        today = str(today_ist())
        logger.warning("load_today_data called without resolved today — using today_ist() fallback")

    stocks = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stocks:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            last_date = latest[0]["date"]
            stocks    = sb.table("stock_data_daily").select("*").eq("date", last_date).execute().data
            logger.info(f"stock_data_daily using last trading day {last_date}")

    msl = sb.table("master_shortlist").select("*").eq("date", today).execute().data
    if not msl:
        latest_msl = sb.table("master_shortlist").select("date").order("date", desc=True).limit(1).execute().data
        if latest_msl:
            last_msl_date = latest_msl[0]["date"]
            msl = sb.table("master_shortlist").select("*").eq("date", last_msl_date).execute().data
            logger.warning(f"MSL using last available date {last_msl_date} ({len(msl)} rows)")

    open_p   = sb.table("open_positions").select("*").execute().data
    regime   = sb.table("market_regime").select("*").eq("date", today).execute().data
    if not regime:
        regime = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data

    events      = sb.table("event_calendar").select("*").eq("is_active", True).execute().data
    safety      = sb.table("safety_lists").select("symbol,list_type").execute().data
    industry_str = sb.table("industry_strength").select("*").eq("date", today).execute().data
    industry_map = {r["industry"]: r for r in industry_str}

    stock_map  = {s["symbol"]: s for s in stocks}
    open_map   = {p["symbol"]: p for p in open_p}
    regime_obj = regime[0] if regime else {}
    asm_set    = {r["symbol"] for r in safety if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety if r["list_type"] == "FO_BAN"}

    fii_rows    = sb.table("fii_dii_flow").select("fii_flag,fii_net_20d").order("date", desc=True).limit(1).execute().data
    fii_flag    = fii_rows[0].get("fii_flag", "NEUTRAL") if fii_rows else "NEUTRAL"
    fii_net_20d = fii_rows[0].get("fii_net_20d") if fii_rows else None

    compute_source_counts = {}
    for r in msl:
        src = r.get("compute_source") or "unknown"
        compute_source_counts[src] = compute_source_counts.get(src, 0) + 1
    logger.info(f"  MSL: {len(msl)} rows | compute_source: {compute_source_counts}")

    return (stock_map, msl, open_map, regime_obj,
            events, asm_set, fo_ban_set, industry_map,
            fii_flag, fii_net_20d)


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY RULE ENGINES (identical to v4)
# ─────────────────────────────────────────────────────────────────────────────

def run_ctl(stocks, sectors_by_name, cfg_ctl):
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_ctl.get("max_sector_rank", 4): continue
        if (s.get("rsi_monthly") or 0) < cfg_ctl.get("min_monthly_rsi", 58): continue
        if (s.get("rsi_weekly")  or 0) < cfg_ctl.get("min_weekly_rsi",  58): continue
        if (s.get("ret_6m")      or -999) < cfg_ctl.get("min_6m_return",  0): continue
        if (s.get("atr_pct")     or 999) > cfg_ctl.get("max_atr_pct",    4): continue
        if (s.get("market_cap")  or 0) < cfg_ctl.get("min_market_cap", 500): continue
        candidates.add(sym)
    return candidates


def run_sbs(stocks, sectors_by_name, cfg_sbs):
    candidates = set()
    for sym, s in stocks.items():
        if s.get("sector") and sectors_by_name.get(s["sector"], 99) > cfg_sbs.get("max_sector_rank", 7): continue
        if (s.get("rsi_daily")    or 0)   < cfg_sbs.get("min_daily_rsi",  55): continue
        if (s.get("rsi_weekly")   or 0)   < cfg_sbs.get("min_weekly_rsi", 56): continue
        if (s.get("vol_ratio")    or 0)   < cfg_sbs.get("min_vol_ratio",  1.3): continue
        if (s.get("consol_range") or 999) > cfg_sbs.get("max_consol_pct", 12): continue
        if (s.get("atr_pct")      or 999) > cfg_sbs.get("max_atr_pct",    5):  continue
        if (s.get("market_cap")   or 0)   < cfg_sbs.get("min_market_cap", 300): continue
        candidates.add(sym)
    return candidates


def run_tpo(ctl_candidates, stocks, cfg_tpo):
    candidates = set()
    for sym in ctl_candidates:
        s   = stocks.get(sym, {})
        rsi = s.get("rsi_daily") or 0
        if not (cfg_tpo.get("min_rsi", 42) <= rsi <= cfg_tpo.get("max_rsi", 55)): continue
        if abs(s.get("dist_sma50") or 999) > cfg_tpo.get("max_dist_sma50", 3): continue
        if (s.get("atr_pct") or 999) > cfg_tpo.get("max_atr_pct", 4): continue
        candidates.add(sym)
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# EAP (identical to v4)
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
            if 0 <= days <= pre_days:    return "AVOID_ENTRY"
            if -pre_days <= days < 0:    best_action = "PRIORITISE"
        elif impact == "MEDIUM":
            if 0 <= days <= pre_days:
                if best_action != "AVOID_ENTRY": best_action = "AVOID_ENTRY"
        elif impact == "LOW":
            if -pre_days <= days < 0:
                if best_action == "NO_CHANGE": best_action = "PRIORITISE"
    return best_action


# ─────────────────────────────────────────────────────────────────────────────
# OPEN POSITION SIGNAL (v4 logic, thresholds now from T dict)
# ─────────────────────────────────────────────────────────────────────────────

def classify_open_position_signal(msl_row: dict, pos: dict, T: dict) -> tuple:
    holding_score  = float(msl_row.get("holding_score") or 0)
    lifecycle      = (msl_row.get("lifecycle")      or "").upper()
    velocity_state = (msl_row.get("velocity_state") or "").upper()
    trend_maturity = (msl_row.get("trend_maturity") or "").upper()
    st_cushion_pct = msl_row.get("st_cushion_pct")

    if holding_score > 0 and holding_score < T["holding_exit"]:
        return "EXIT", "OPEN_POSITION", f"holding_score_low_{holding_score:.0f}"
    if lifecycle == "EXIT":
        return "EXIT", "OPEN_POSITION", "lifecycle_exit"
    if st_cushion_pct is not None and float(st_cushion_pct) <= 0:
        return "EXIT", "OPEN_POSITION", "supertrend_broken"
    if lifecycle == "REDUCE":
        return "REDUCE", "OPEN_POSITION", "lifecycle_reduce"
    if lifecycle == "ADD":
        if trend_maturity in LATE_MATURITY:
            return "HOLD", "OPEN_POSITION", "add_suppressed_late_trend"
        if velocity_state == "ACCELERATING" and holding_score >= T["add_holding_min"]:
            return "ADD", "OPEN_POSITION", "lifecycle_add_accelerating"
    action = (pos.get("action_required") or "").upper()
    if "EXIT" in action or "SELL" in action:
        return "EXIT", "OPEN_POSITION", "manual_action_exit"
    if "ADD" in action:
        return "ADD",  "OPEN_POSITION", "manual_action_add"
    if velocity_state == "DECELERATING" and holding_score < 60:
        return "HOLD", "OPEN_POSITION", "hold_deceleration_monitor"
    return "HOLD", "OPEN_POSITION", "holding"


# ─────────────────────────────────────────────────────────────────────────────
# ATR ALLOWANCE — now reads from dynamic matrix in T dict
# ─────────────────────────────────────────────────────────────────────────────

def _max_chase_atrs(momentum_state: str, momentum_phase: str,
                    velocity_state: str, trend_maturity: str,
                    struct_edge: str, weekly_structure: str,
                    psar_dual: bool, bb_squeeze: bool,
                    atr_matrix: dict) -> float:
    """
    Read ATR chase allowances from the dynamic atr_matrix (system_config).
    Step 20 calibration updates this JSON based on actual win rates.
    Structure bonuses capped at +1.6 ATRs — same as v4.
    """
    # Build lookup key from momentum state
    ms  = momentum_state.upper()
    mp  = momentum_phase.upper()
    vs  = velocity_state.upper()
    tm  = trend_maturity.upper()

    if tm == "EXTENDED":
        base = atr_matrix.get("EXTENDED_ANY", 0.0)
    elif tm in LATE_MATURITY:
        base = atr_matrix.get("LATE_ANY", 0.5)
    else:
        # Try most specific key first, fall back to progressively broader
        key1 = f"{ms}_{mp[:9]}_{vs[:11]}"
        key2 = f"{ms}_{mp[:9]}_ANY"
        key3 = f"{ms}_ANY_ANY"
        base = (atr_matrix.get(key1)
                or atr_matrix.get(key2)
                or atr_matrix.get(key3)
                or atr_matrix.get("DEFAULT", 1.0))

    # Structure quality bonuses (unchanged from v4)
    bonus = 0.0
    if struct_edge == "YES":          bonus += 0.5
    if weekly_structure == "STRONG":  bonus += 0.3
    if psar_dual:                     bonus += 0.3
    if bb_squeeze:                    bonus += 0.5
    return round(float(base) + min(bonus, 1.6), 2)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY SIGNAL CLASSIFICATION v5
# Major addition: system_config signal_threshold_* keys now applied in Gate 5.
# All gate ordering from v4 preserved exactly.
# ─────────────────────────────────────────────────────────────────────────────

def classify_entry_signal(msl_row: dict, open_map: dict,
                           stock_data: dict, T: dict,
                           threshold: float = None,
                           regime_name: str = "NEUTRAL") -> tuple:
    """
    Returns (is_entry: bool, signal_type: str, filter_reason: str, near_miss_data: dict)

    near_miss_data is populated for WATCH signals so step 19 can evaluate upgrades.
    It contains: original_signal_type (what it would have been), dist_to_qualify,
    and which specific threshold blocked it.

    Gate order (unchanged from v4):
      3a. already_in_position
      3b. missing price data
      3c. risk_score hard block
      3d. liquidity block
      3e. lifecycle EXIT block
      3f. EXTENDED timing no reentry
      4a. below zone: pullback vs breakdown
      4b. above zone: ATR-normalised distance
      4c. R:R viability
      4d. target already reached
      5.  signal subtype + threshold validation
    """
    sym = msl_row.get("symbol")
    near_miss = {}

    if sym in open_map:
        return False, None, "already_in_position", {}

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

    atr_pct     = float(stock_data.get("atr_pct") or T["atr_fallback"])
    above_sma50 = bool(stock_data.get("above_sma50"))

    # ── Indicators for Gate 5 — NULL-PRESERVING (fixed 2026-07) ──────────────
    # These were read as `float(stock_data.get("rsi_daily") or 0)`, collapsing
    # a missing value to 0.0. Every Gate 5 check was then written as
    #     (rsi_d == 0 or LOW <= rsi_d <= HIGH)
    # so a field that FAILED TO COMPUTE short-circuited the check to True.
    # The practical effect was inverted risk: on a day where ingest_bhavcopy
    # partially failed and delivery_pct came back NULL, the delivery gate
    # vanished for the affected stocks and the system emitted MORE entry
    # signals, not fewer — with no indication in the logs that quality checks
    # had been skipped.
    #
    # _num() preserves None. _chk() returns True / False / None, where None
    # means "cannot evaluate", and _tech() counts the Nones so a signal can be
    # required to carry a minimum amount of real evidence.
    rsi_d        = _num(stock_data, "rsi_daily")
    rsi_w        = _num(stock_data, "rsi_weekly")
    rsi_m        = _num(stock_data, "rsi_monthly")
    adx          = _num(stock_data, "adx")
    vol_ratio    = _num(stock_data, "vol_ratio")
    delivery_pct = _num(stock_data, "delivery_pct")
    dist_sma50   = _num(stock_data, "dist_sma50")
    consol_range = _num(stock_data, "consol_range")
    ret_6m       = _num(stock_data, "ret_6m")

    max_unknown = cfg_int("signal_max_unknown_indicators", 2)

    # ── GATE 3: Hard blocks ───────────────────────────────────────────────────
    if not ep or not cp or ep <= 0:
        return False, None, "missing_price_data", {}

    if risk_score >= T["risk_block"]:
        near_miss = {"blocked_by": "risk_score", "value": risk_score, "threshold": T["risk_block"]}
        return False, None, f"blocked_high_risk_{risk_score:.0f}", near_miss

    value_cr_known = float(msl_row.get("value_cr") or 0) > 0
    if value_cr_known and (low_liquidity or liquidity_qual == "VERY_LOW"):
        return False, None, "blocked_low_liquidity", {}

    if lifecycle == "EXIT":
        return False, None, "exit_lifecycle_no_entry", {}

    if entry_timing_type == "EXTENDED" and reentry_mode != "ELIGIBLE":
        return False, None, "extended_timing_no_reentry", {}

    # ── GATE 4A: Below-zone ───────────────────────────────────────────────────
    threshold_val = threshold or buy_candidate_threshold()
    below_zone    = cp < (ep * (1 - threshold_val))

    if below_zone:
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
            and weekly_structure != ""
            and st_ok
            and velocity_state not in ("DECELERATING",)
            and (reentry_mode == "ELIGIBLE" or entry_timing_type == "REENTRY")
        )
        if is_breakdown:
            near_miss = {"blocked_by": "structural_breakdown", "below_zone_pct": round((ep - cp) / ep * 100, 2)}
            return False, None, "structural_breakdown", near_miss
        elif not is_valid_pullback:
            near_miss = {"blocked_by": "below_zone_monitoring", "below_zone_pct": round((ep - cp) / ep * 100, 2)}
            return False, None, "below_zone_monitoring", near_miss

    # ── GATE 4B: Above-zone ATR distance ─────────────────────────────────────
    if cp > eh:
        if atr_pct <= 0:
            if (cp - eh) / eh > 0.08:
                near_miss = {"blocked_by": "far_above_no_atr", "above_zone_pct": round((cp - eh) / eh * 100, 2)}
                return False, None, "far_above_no_atr_data", near_miss
        else:
            dist_in_atrs = (cp - eh) / (cp * atr_pct / 100)
            max_atrs     = _max_chase_atrs(
                momentum_state, momentum_phase, velocity_state, trend_maturity,
                struct_edge, weekly_structure, psar_dual, bb_squeeze,
                T["atr_matrix"]
            )
            if dist_in_atrs > max_atrs:
                near_miss = {
                    "blocked_by":     "atr_chase",
                    "dist_atrs":      round(dist_in_atrs, 2),
                    "max_atrs":       round(max_atrs, 2),
                    "gap_atrs":       round(dist_in_atrs - max_atrs, 2),
                    "would_be":       "BUY_CANDIDATE",      # Gate 5 would have determined final type
                    "momentum_state": momentum_state,
                    "momentum_phase": momentum_phase,
                    "velocity_state": velocity_state,
                    "struct_edge":    struct_edge,
                    "score":          float(msl_row.get("final_score") or 0),
                }
                return False, None, f"chase_{dist_in_atrs:.1f}atrs_limit_{max_atrs:.1f}", near_miss

    # ── GATE 4C: R:R viability ─────────────────────────────────────────────────
    #
    # REWRITTEN 2026-07. The old version reconstructed the trade levels here
    # using a flat 3% stop (`ep * (1 - stop_buffer_pct)`) and then expanded the
    # target as `ep + (ep*0.03) * expected_r`. But expected_r had been computed
    # in compute_msl against a 1.5xATR stop and a 3xATR target. The two models
    # disagreed by ~2.5x on target distance, so the gate measured remaining
    # upside against a target far closer than the one the setup was designed
    # around. Any stock more than ~2% above the zone low scored an implied R:R
    # under 0.5 and was demoted to WATCH.
    #
    # Evidence (2026-07-24): 17 of 27 WATCH signals were `insufficient_rr_*`
    # at 0.05x-0.47x, and all 11 passing signals had subtype `in_zone` — the
    # gate could only ever pass a stock sitting exactly inside its zone.
    #
    # Now: read the levels compute_msl persisted, and re-anchor to the live
    # price. The stop stays where the SETUP puts it, so chasing costs R:R
    # rather than silently dragging the stop upward.
    planned_stop   = msl_row.get("planned_stop")
    planned_target = msl_row.get("planned_target")

    if planned_stop and planned_target and cp > 0:
        stop_price = float(planned_stop)
        target     = float(planned_target)

        if cp <= stop_price:
            near_miss = {"blocked_by": "price_below_stop",
                         "cp": cp, "stop": stop_price}
            return False, None, "price_at_or_below_planned_stop", near_miss

        if target <= cp:
            near_miss = {"blocked_by": "target_reached",
                         "target": target, "cp": cp}
            return False, None, "target_already_reached", near_miss

        current_risk     = cp - stop_price
        remaining_upside = target - cp
        implied_rr       = remaining_upside / current_risk
        effective_min_rr = T.get(f"min_rr_{regime_name}", T["min_rr"])

        if implied_rr < effective_min_rr:
            near_miss = {
                "blocked_by": "rr",
                "implied_rr": round(implied_rr, 2),
                "min_rr":     effective_min_rr,
                "cp":         cp,
                "stop":       stop_price,
                "target":     target,
                "risk_pct":   round(current_risk / cp * 100, 2),
            }
            return False, None, f"insufficient_rr_{implied_rr:.2f}x", near_miss

    elif expected_r > 0 and ep > 0 and atr_pct > 0:
        # Fallback for rows written before migration 002 backfilled the planned_*
        # columns. Uses the SAME risk model rather than the old flat-3% stop, so
        # the two paths cannot diverge again.
        from analysis.risk_model import compute_trade_levels
        lv = compute_trade_levels(
            entry_price  = cp,
            atr_abs      = cp * (atr_pct / 100.0),
            anchor_price = ep,
            regime       = regime_name,
        )
        if not lv.valid:
            near_miss = {"blocked_by": "risk_model_invalid", "reason": lv.reject_reason}
            return False, None, f"rr_unavailable_{lv.reject_reason}", near_miss
        effective_min_rr = T.get(f"min_rr_{regime_name}", T["min_rr"])
        if lv.rr < effective_min_rr:
            near_miss = {"blocked_by": "rr_fallback",
                         "implied_rr": lv.rr, "min_rr": effective_min_rr}
            return False, None, f"insufficient_rr_{lv.rr:.2f}x", near_miss

    # ── GATE 5: Signal subtype + threshold validation (v5 ADDITION) ───────────
    # Now checks system_config signal_threshold_* keys for each signal type.
    # A stock that passes gates 3-4 is validated; gate 5 determines quality tier.

    # PRIME_SETUP — all momentum conditions + system_config RSI/ADX/vol thresholds
    #
    # BUG FIX 2026-07 (operator precedence). This was:
    #     momentum_state == "HOT"                  * 3,
    #     momentum_phase in ("EXPANSION","EARLY")  * 2,
    #     velocity_state == "ACCELERATING"         * 2,
    #     struct_edge == "YES"                     * 2,
    # `*` binds tighter than `==`/`in`, so Python evaluated these as:
    #     momentum_state == "HOTHOTHOT"                                -> always False
    #     momentum_phase in ("EXPANSION","EARLY","EXPANSION","EARLY")  -> 1, never 2
    #     velocity_state == "ACCELERATINGACCELERATING"                 -> always False
    #     struct_edge    == "YESYES"                                   -> always False
    # Maximum achievable prime_score was therefore 3, against a threshold of 8.
    # PRIME_SETUP could never fire — confirmed against production: zero
    # PRIME_SETUP rows in signal_log across the last five trading days, and the
    # "★ PRIME SETUPS" log line has always been empty. It also meant
    # final_snapshot's TIER_1 inference (which keys off signal_type ==
    # "PRIME_SETUP") never triggered either.
    prime_score = sum([
        3 if momentum_state == "HOT"                        else 0,
        2 if momentum_phase in ("EXPANSION", "EARLY")       else 0,
        2 if velocity_state == "ACCELERATING"               else 0,
        2 if struct_edge == "YES"                           else 0,
        1 if breakout_readiness >= T["prime_breakout_min"]  else 0,
        1 if trend_maturity not in LATE_MATURITY            else 0,
    ])
    prime_min_score = cfg_int("prime_gate5_min_score", 8)   # 8 of a possible 11

    # PRIME means "highest conviction, act now". A trend already classified LATE
    # or EXHAUSTED cannot be that, however strong its momentum reads — for a
    # 1-3 week hold you are buying the last leg. Previously maturity was only
    # worth one score point, so a LATE stock could still reach 9/11 and be
    # promoted. Such setups now fall through to MOMENTUM_CONTINUATION, which is
    # the honest label for them.
    prime_momentum_ok = (prime_score >= prime_min_score
                         and trend_maturity not in LATE_MATURITY)
    # PRIME is the highest-conviction label, so it tolerates the least missing
    # evidence: max_unknown-1 (default 1 of 6 checks).
    prime_technical_ok, prime_unknown = _tech([
        _chk(rsi_d,        T["prime_rsi_d_min"], T["prime_rsi_d_max"]),
        _chk(rsi_w,        T["prime_rsi_w_min"], T["prime_rsi_w_max"]),
        _chk(adx,          T["prime_adx_min"]),
        _chk(vol_ratio,    T["prime_vol_min"]),
        _chk(delivery_pct, T["prime_del_min"]),
        _chk(None if dist_sma50 is None else abs(dist_sma50), None, T["prime_sma50_max"]),
    ], max_unknown=max(0, max_unknown - 1))
    if prime_momentum_ok and prime_technical_ok:
        return True, "PRIME_SETUP", "prime_all_aligned", {}

    # PRIME_SETUP blocked by technical thresholds — capture as near miss
    if prime_momentum_ok and not prime_technical_ok:
        near_miss = {"blocked_by": "prime_technical", "would_be": "PRIME_SETUP",
                     "rsi_d": rsi_d, "adx": adx, "vol": vol_ratio, "del": delivery_pct}

    # BREAKOUT_SETUP — with trend_maturity guard + prebreak thresholds
    if trend_maturity not in LATE_MATURITY:
        prebreak_tech_ok, _ = _tech([
            _chk(rsi_d,        T["prebreak_rsi_d_min"]),
            _chk(rsi_w,        T["prebreak_rsi_w_min"]),
            _chk(adx,          T["prebreak_adx_min"], T["prebreak_adx_max"]),
            _chk(vol_ratio,    None, T["prebreak_vol_max"]),
            _chk(consol_range, None, T["prebreak_consol_max"]),
        ], max_unknown=max_unknown)
        # ── Breakout thresholds, recalibrated 2026-07 ────────────────────────
        # These were hardcoded at 70 / 60 / 75. Measured against the real
        # distribution on 2026-07-24 (all 75 master_shortlist rows):
        #     <20: 32 | 20-40: 37 | 40-60: 4 | 60-70: 2 | >=70: 0   max = 68
        # No stock has ever reached 70, so the primary branch could only fire
        # via bb_squeeze (true for 7/75), and the >=60 branch admitted 2 rows.
        # compute_breakout_readiness has a theoretical max of 105, but its two
        # largest components — tight consolidation (22 pts) and BB squeeze
        # (23 pts) — are by construction near-zero for trending stocks, which
        # is most of the shortlist. The scale is real; the thresholds were set
        # against an imagined one.
        #
        # Deliberately NOT lowered far. A low breakout_readiness on a reversal
        # or momentum candidate is CORRECT — those are not breakout setups, and
        # dropping the gate to manufacture BREAKOUT_SETUP volume would just
        # mislabel them. These values track the genuine top of the distribution.
        br_high = cfg_float("signal_breakout_readiness_high", 58.0)   # was 70
        br_mid  = cfg_float("signal_breakout_readiness_mid",  45.0)   # was 60
        br_vol  = cfg_float("signal_breakout_readiness_vol",  62.0)   # was 75

        # For volume_trend NULL → also accept STABLE with strong readiness:
        volume_ok = (
            volume_trend == "EXPANDING"
            or (volume_trend in ("STABLE", "UNKNOWN") and breakout_readiness >= br_vol)
        )
        if (bb_squeeze or breakout_readiness >= br_high) and volume_ok and prebreak_tech_ok:
            return True, "BREAKOUT_SETUP", "breakout_squeeze_volume", {}
        if breakout_readiness >= br_mid and momentum_state in ("HOT", "BUILDING") and prebreak_tech_ok:
            return True, "BREAKOUT_SETUP", "breakout_readiness_high", {}

    # MOMENTUM_CONTINUATION — HOT/BUILDING stock above zone, trend intact
    # Fills the gap between PRIME (all conditions) and BUY_CANDIDATE (no type).
    # Applies to strong trending stocks that passed Gate 4 (within ATR allowance)
    # but missed PRIME (one condition absent) or BREAKOUT (ADX too high).
    if (momentum_state in ("HOT", "BUILDING")
            and momentum_phase in ("EXPANSION", "EARLY")
            and velocity_state != "DECELERATING"
            and trend_maturity not in LATE_MATURITY
            and struct_edge == "YES"
            and entry_timing_type in ("OPTIMAL", "CHASING", "REENTRY")):
        mom_cont_tech_ok, _ = _tech([
            _chk(rsi_d,        52, T.get("prime_rsi_d_max", 82)),
            _chk(rsi_w,        54),
            _chk(adx,          20),
            _chk(vol_ratio,    1.0),
            _chk(delivery_pct, 40),
        ], max_unknown=max_unknown)
        if mom_cont_tech_ok:
            return True, "MOMENTUM_CONTINUATION", "trend_continuation", {}

    # REENTRY_SETUP — with reentry thresholds
    reentry_flag = entry_timing_type == "REENTRY" or reentry_mode == "ELIGIBLE"
    if reentry_flag or below_zone:
        reentry_tech_ok, _ = _tech([
            _chk(rsi_d,        T["reentry_rsi_d_min"], T["reentry_rsi_d_max"]),
            _chk(rsi_w,        T["reentry_rsi_w_min"], T["reentry_rsi_w_max"]),
            _chk(adx,          T["reentry_adx_min"]),
            _chk(vol_ratio,    T["reentry_vol_min"]),
            _chk(delivery_pct, T["reentry_del_min"]),
            _chk(ret_6m,       T["reentry_ret6m_min"]),
        ], max_unknown=max_unknown)
        if reentry_tech_ok:
            reason = "pullback_to_zone" if below_zone else "reentry_pullback"
            return True, "REENTRY_SETUP", reason, {}
        # Near miss: would have been REENTRY but failed technical thresholds
        if not near_miss:
            near_miss = {"blocked_by": "reentry_technical", "would_be": "REENTRY_SETUP",
                         "rsi_d": rsi_d, "adx": adx, "vol": vol_ratio}

    # STAGED_ENTRY — approaching zone with building momentum
    if entry_timing_type == "APPROACHING" and momentum_state in ("HOT", "BUILDING", "STABLE"):
        staged_tech_ok, _ = _tech([
            _chk(rsi_d,        T["staged_rsi_d_min"], T["staged_rsi_d_max"]),
            _chk(rsi_w,        T["staged_rsi_w_min"]),
            _chk(adx,          T["staged_adx_min"]),
            _chk(vol_ratio,    T["staged_vol_min"]),
            _chk(delivery_pct, T["staged_del_min"]),
        ], max_unknown=max_unknown)
        if staged_tech_ok:
            return True, "STAGED_ENTRY", "approaching_zone_building", {}

    # ── BUY_CANDIDATE — the fall-through label ────────────────────────────────
    # Applies no thresholds of its own beyond gates 3-4, which makes it the
    # weakest signal type AND, before this guard, the one most exposed to the
    # null-as-pass defect: a stock whose indicators were entirely missing sailed
    # past every typed branch (each needing real values) and landed here as a
    # tradeable BUY. It is also by far the most common label in production —
    # all 11 entry signals on 2026-07-24 were BUY_CANDIDATE/in_zone.
    #
    # Require a minimum of real evidence. A stock we know almost nothing about
    # belongs on the watchlist, not in the buy list.
    core = [rsi_d, rsi_w, adx, vol_ratio, delivery_pct]
    known = sum(1 for v in core if v is not None)
    min_known = cfg_int("signal_buy_candidate_min_known", 3)
    if known < min_known:
        near_miss = {
            "blocked_by":  "insufficient_indicator_data",
            "would_be":    "BUY_CANDIDATE",
            "known":       known,
            "required":    min_known,
            "missing":     [n for n, v in zip(
                ("rsi_daily", "rsi_weekly", "adx", "vol_ratio", "delivery_pct"), core)
                if v is None],
        }
        return False, None, f"insufficient_data_{known}of{len(core)}", near_miss

    return True, "BUY_CANDIDATE", "in_zone", near_miss


# ─────────────────────────────────────────────────────────────────────────────
# SCORE ADJUSTMENT (v5 — bonuses now from T dict, not hardcoded)
# ─────────────────────────────────────────────────────────────────────────────

def compute_adjusted_score(msl_row: dict, base_score: float,
                            signal_type: str, regime_name: str,
                            T: dict) -> float:
    score = float(base_score or 0)

    momentum_score      = float(msl_row.get("momentum_score")      or 0)
    institutional_score = float(msl_row.get("institutional_score") or 0)
    breakout_readiness  = float(msl_row.get("breakout_readiness")  or 0)
    risk_score          = float(msl_row.get("risk_score")          or 0)
    psar_dual           = bool(msl_row.get("psar_dual_confirmed"))
    weekly_struct       = (msl_row.get("weekly_structure") or "").upper()

    if momentum_score >= 80:      score += T["bonus_momentum_high"]
    elif momentum_score >= 70:    score += T["bonus_momentum_mid"]
    if institutional_score >= 60: score += T["bonus_institutional"]
    if breakout_readiness >= 65:  score += T["bonus_breakout_ready"]
    if psar_dual:                 score += T["bonus_psar_dual"]
    if weekly_struct == "STRONG": score += T["bonus_weekly_strong"]

    if risk_score >= T["risk_penalty"]: score += T["penalty_risk_high"]
    elif risk_score >= 50:              score += T["penalty_risk_mid"]

    if signal_type == "PRIME_SETUP":      score += T["bonus_prime"]
    elif signal_type == "BREAKOUT_SETUP": score += T["bonus_breakout"]
    elif signal_type == "REENTRY_SETUP":  score += T["bonus_reentry"]
    elif signal_type == "MOMENTUM_CONTINUATION":  score += T.get("bonus_momentum_cont", 2.0)

    if regime_name == "RISK OFF":
        score = round(score * 0.80, 1)   # steeper penalty — capital preservation mode
    elif regime_name == "RECOVERING":
        score = round(score * 0.90, 1)   # moderate penalty — bounce not yet confirmed
    elif regime_name == "NEUTRAL":
        score = round(score * 0.95, 1)   # mild penalty — selective positioning

    return round(min(max(score, 0.0), 100.0), 1)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GENERATION LOOP v5
# Key addition: T dict passed through all functions; near_miss_data in signal dict
# ─────────────────────────────────────────────────────────────────────────────

def generate(run_date: date | None = None) -> list:
    run_date  = run_date or today_ist()
    strat_cfg = get_strategy_config()

    # ── Resolve last valid trading day ────────────────────────────────────────
    # Must happen before load_today_data so all queries use the same resolved date.
    # today_ist() is wrong on weekends, NSE holidays, and days compute_msl hasn't run.
    _sb_resolve = get_supabase()
    try:
        run_date_str = resolve_last_trading_day(_sb_resolve, str(run_date))
    except RuntimeError as exc:
        logger.error(str(exc))
        return []
    from datetime import date as _date
    run_date = _date.fromisoformat(run_date_str)

    # ── Load dynamic thresholds first ────────────────────────────────────────
    T = load_signal_thresholds()
    logger.info(
        f"  Thresholds loaded: risk_block={T['risk_block']} "
        f"holding_exit={T['holding_exit']} min_rr={T['min_rr']} "
        f"min_score_adj={T['min_score_adj']} "
        f"prime_breakout_min={T['prime_breakout_min']}"
    )

    (stock_map, msl, open_map, regime,
     events, asm_set, fo_ban_set, industry_map,
     fii_flag, fii_net_20d) = load_today_data(run_date_str)

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

    # ── Sector ranks ─────────────────────────────────────────────────────────
    # This query had no fallback and no assertion. When sector_strength went
    # stale (it was frozen from 2026-07-02 to 2026-07-24 because the pipeline
    # computed it before compute_indicators had run), this returned [] and
    # sector_rank became {}. Every lookup then defaulted to 99, so run_ctl
    # (max_sector_rank 4), run_sbs (7) and run_tpo all returned empty sets —
    # in_rule_engine was False for 38/38 signals and sector_rank_at_entry was
    # NULL for all of them. Nothing failed; the engines just quietly stopped
    # contributing. Now: fall back to the most recent single date, and refuse
    # to run on ranks too old to be meaningful.
    sectors = (sb.table("sector_strength")
                 .select("sector,rank")
                 .eq("date", str(run_date))
                 .execute().data)
    sector_rank_date = str(run_date)

    if not sectors:
        latest = (sb.table("sector_strength").select("date")
                    .order("date", desc=True).limit(1).execute().data)
        if latest:
            sector_rank_date = latest[0]["date"]
            sectors = (sb.table("sector_strength")
                         .select("sector,rank")
                         .eq("date", sector_rank_date)   # single date, never a mixed grab
                         .execute().data)
            logger.warning(
                f"  sector_strength missing for {run_date} — falling back to {sector_rank_date}"
            )

    sector_rank = {s["sector"]: s["rank"] for s in sectors if s.get("rank")}

    max_stale = cfg_int("sector_rank_max_stale_days", 5)
    stale_days = (run_date - date.fromisoformat(sector_rank_date)).days
    if not sector_rank:
        raise RuntimeError(
            "sector_strength is empty — every engine gates on sector_rank, so "
            "signals generated now would silently exclude CTL/SBS/TPO and "
            "misrank everything else. Run step 11_sector_strength first."
        )
    if stale_days > max_stale:
        raise RuntimeError(
            f"sector_strength is {stale_days} days stale (latest={sector_rank_date}, "
            f"run_date={run_date}, max={max_stale}). Sector leadership rotates far "
            f"faster than that — signals would be built on dead ranks. "
            f"Run step 11_sector_strength, or raise sector_rank_max_stale_days."
        )
    logger.info(
        f"  Sector ranks: {len(sector_rank)} sectors from {sector_rank_date}"
        + (f" ({stale_days}d stale)" if stale_days else " (current)")
    )

    # ── Event map: pre-results / corporate action flag ────────────────────
    # Built here — after load_today_data() so msl list is available,
    # and after sb is defined. Does NOT block signals — enriches them.
    event_map: dict[str, dict] = {}
    try:
        msl_symbols = [r["symbol"] for r in msl if r.get("symbol")]
        if msl_symbols:
            event_rows = (
                sb.table("nifty_upcoming_events")
                  .select("symbol,purpose,details,event_date,days_to_event")
                  .in_("symbol", msl_symbols)
                  .gte("days_to_event", 0)
                  .lte("days_to_event", 7)
                  .execute().data
            )
            for r in sorted(event_rows, key=lambda x: int(x.get("days_to_event") or 99)):
                if r["symbol"] not in event_map:
                    event_map[r["symbol"]] = r
            if event_map:
                logger.info(f"  Event map: {len(event_map)} symbols have events within 7 days")
    except Exception as e:
        logger.warning(f"  Event map load failed (non-fatal): {e}")

    # config.get_strategy_config normalises params to plain dicts, so the
    # local str-only json.loads pass that used to sit here (and ran AFTER the
    # first four assignments, making them dead) is gone. That shim handled only
    # the string form and let migration 004's array form through untouched.
    cfg_ctl = strat_cfg.get("CTL", {})
    cfg_sbs = strat_cfg.get("SBS", {})
    cfg_tpo = strat_cfg.get("TPO", {})
    cfg_eap = strat_cfg.get("EAP", {})

    ctl_set         = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_set         = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_set         = run_tpo(ctl_set, stock_map, cfg_tpo)
    rule_engine_set = ctl_set | sbs_set | tpo_set

    min_score     = cfg_float("min_score_to_show", 50)
    show_watch    = cfg_bool("show_watching_stocks", True)

    signals = []

    for msl_row in msl:
        sym       = msl_row.get("symbol")
        in_pos    = sym in open_map
        pos       = open_map.get(sym, {})
        sector    = msl_row.get("sector", "")
        in_engine = sym in rule_engine_set

        industry  = stock_map.get(sym, {}).get("industry", "") or msl_row.get("industry", "")
        ind_ctx   = industry_map.get(industry, {})
        ind_rank  = ind_ctx.get("rank")
        ind_top5  = ind_ctx.get("top5_flag", False)
        ind_state = ind_ctx.get("industry_state", "")
        ind_rsi_d = ind_ctx.get("avg_rsi_daily")

        score = float(msl_row.get("final_score") or 0)

        # Industry bonus applied before min_score gate (dynamic from T)
        if cfg("industry_scoring_active") == "true":
            if ind_top5:              score += T["industry_bonus_top5"]
            if ind_state == "STRONG": score += T["industry_bonus_strong"]

        if score < min_score:
            continue

        if sym in tpo_set:   strat_tag = "TPO"
        elif sym in ctl_set: strat_tag = "CTL"
        elif sym in sbs_set: strat_tag = "SBS"
        else:                strat_tag = msl_row.get("strategy_source", "")

        asm_flag    = sym in asm_set
        fo_ban_flag = sym in fo_ban_set
        eap_action  = get_eap_action(sym, sector, events, cfg_eap)

        filter_reason  = None
        signal_subtype = None
        regime_warning = False
        near_miss_data = {}

        is_entry = False                  # always initialize before the branch
        signal_type_candidate = None
        near_miss_data = {}
        if in_pos:
            signal_type, position_state, filter_reason = classify_open_position_signal(
                msl_row, pos, T
            )
        else:
            stock_data = stock_map.get(sym, {})
            is_entry, signal_type_candidate, filter_reason, near_miss_data = classify_entry_signal(
                msl_row, open_map, stock_data, T, regime_name=regime_name
            )

        # AFTER:
        if is_entry:
            # Signal-type specific min_rr check — AFTER type is known, BEFORE committing.
            # Uses the SAME persisted levels as Gate 4C. This block previously
            # rebuilt stop/target from the flat 3% buffer, so it applied a
            # different (much harsher) R:R than the gate that had just passed
            # the stock — a second instance of the model-divergence bug.
            if signal_type_candidate in ENTRY_SIGNAL_TYPES:
                sig_min_rr = T.get(f"min_rr_{signal_type_candidate}")
                if sig_min_rr:
                    stop_   = msl_row.get("planned_stop")
                    target_ = msl_row.get("planned_target")
                    cp_     = msl_row.get("current_price")
                    if stop_ and target_ and cp_:
                        stop_f, target_f, cp_f_ = float(stop_), float(target_), float(cp_)
                        if cp_f_ > stop_f:
                            impl_rr = (target_f - cp_f_) / (cp_f_ - stop_f)
                            if impl_rr < sig_min_rr:
                                is_entry = False
                                near_miss_data = {
                                    "blocked_by": "signal_type_rr",
                                    "implied_rr": round(impl_rr, 2),
                                    "min_rr":     sig_min_rr,
                                    "would_be":   signal_type_candidate,
                                }
                                filter_reason         = f"insufficient_rr_for_{signal_type_candidate}"
                                signal_type_candidate = None

        # Re-evaluate after possible signal-type RR gate
        if is_entry:
            position_state = "BUY_CANDIDATE"
            signal_type    = signal_type_candidate
            signal_subtype = filter_reason
            filter_reason  = None
            near_miss_data = {}
        else:
            position_state = "WATCHING"
            signal_type    = "WATCH"
            # Derive descriptive subtype from filter_reason so signal_subtype
            # is never blank — makes Brain queries and manual review cleaner.
            if not signal_subtype:
                _fr = (filter_reason or "").lower()
                if "chase" in _fr:             signal_subtype = "WATCH_ATR_CHASE"
                elif "rr" in _fr:              signal_subtype = "WATCH_RR"
                elif "breakdown" in _fr:       signal_subtype = "WATCH_BREAKDOWN"
                elif "below_zone" in _fr:      signal_subtype = "WATCH_BELOW_ZONE"
                elif "missing" in _fr:         signal_subtype = "WATCH_NO_DATA"
                elif "target" in _fr:          signal_subtype = "WATCH_TARGET_REACHED"
                elif "extended" in _fr:        signal_subtype = "WATCH_EXTENDED"
                elif "lifecycle" in _fr:       signal_subtype = "WATCH_LIFECYCLE"
                elif "risk" in _fr:            signal_subtype = "WATCH_HIGH_RISK"
                elif "liquidity" in _fr:       signal_subtype = "WATCH_LIQUIDITY"
                elif "low_adj" in _fr:         signal_subtype = "WATCH_LOW_SCORE"
                else:                          signal_subtype = "WATCH_OTHER"
            if not show_watch:
                continue

        if asm_flag and position_state != "OPEN_POSITION":
            signal_type = "BLOCKED_ASM"

        if position_state == "BUY_CANDIDATE":
            if regime_name == "RISK OFF":
                regime_warning = True
                if block_buys:
                    signal_type = "BUY_BLOCKED_REGIME"
            elif regime_name == "RECOVERING":
                regime_warning = True    # bounce not confirmed — flag but don't block
            elif regime_name == "NEUTRAL":
                regime_warning = False   # selective positioning is fine, no warning needed

        if eap_action == "AVOID_ENTRY" and position_state == "BUY_CANDIDATE":
            signal_type = "AVOID_ENTRY_EVENT"

        score_adjusted = compute_adjusted_score(msl_row, score, signal_type, regime_name, T)

        # Floor for entry signals (dynamic threshold)
        if signal_type in ENTRY_SIGNAL_TYPES and score_adjusted < T["min_score_adj"]:
            if not near_miss_data:
                near_miss_data = {"blocked_by": "adj_score_floor",
                                  "would_be": signal_type,
                                  "score_adj": score_adjusted,
                                  "threshold": T["min_score_adj"]}
            signal_subtype = signal_type
            signal_type    = "WATCH"
            position_state = "WATCHING"
            filter_reason  = f"low_adj_score_{score_adjusted:.0f}"
            if not show_watch:
                continue

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
            "scanner_patterns": None,
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
            # Stock technicals (stock_data_daily)
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
            # ── Trade plan (migration 002) ────────────────────────────────
            # Carried onto the signal so signal_log is a self-contained decision
            # record. Before this, signal_log had no price columns at all, so
            # final_snapshot's `sl.get("entry_zone_low")` always fell through to
            # master_shortlist — every signal_output_daily row on 2026-07-24 had
            # entry_zone_source='master_shortlist'. It also gives the position
            # lifecycle manager the exact stop the signal was validated against.
            "current_price":        msl_row.get("current_price"),
            "entry_zone_low":       msl_row.get("entry_zone_low"),
            "entry_zone_high":      msl_row.get("entry_zone_high"),
            "planned_entry":        msl_row.get("planned_entry"),
            "planned_stop":         msl_row.get("planned_stop"),
            "planned_target":       msl_row.get("planned_target"),
            "planned_risk_pct":     msl_row.get("planned_risk_pct"),
            "planned_stop_source":  msl_row.get("planned_stop_source"),
            "implied_rr":           _implied_rr(msl_row),
            # MSL fields
            "days_in_list":         msl_row.get("days_in_list"),
            "validity_score":       msl_row.get("validity_score"),
            "expected_r_msl":       msl_row.get("expected_r"),
            "sheet_conflict":       False,
            "sheet_conflict_type":  None,
            "days_to_trigger_est":  msl_row.get("days_to_trigger_est"),
            "filter_reason":        filter_reason,
            "sector_rank_at_entry": sector_rank.get(sector) if sector else None,
            # compute_msl AI_FEED_FIELDS
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
            # v5 ADDITION: near_miss_data for step 19 WATCH upgrade evaluation
            # Tells step 19 exactly WHY this stock is WATCH and how far it is from qualifying
            # Step 19 uses this to decide if today's macro/news context changes the answer
            "near_miss_data": json.dumps(near_miss_data) if near_miss_data else None,
            # ── Pre-results / corporate action context ────────────────────
            # Signals are NOT blocked. These fields surface in send_alerts
            # so the trader can apply their own position-sizing judgment.
            **_build_event_context(sym, event_map),
        }
        signals.append(sig)

    return signals


# ─────────────────────────────────────────────────────────────────────────────
# SAVE
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
    logger.info("STEP [15]: Generate Signals v5 — Dynamic Thresholds")
    logger.info("=" * 60)

    signals = generate()

    from collections import Counter
    types    = Counter(s["signal_type"]        for s in signals)
    subtypes = Counter(s.get("signal_subtype") for s in signals if s.get("signal_subtype"))

    logger.info(f"Signal breakdown:  {dict(types)}")
    logger.info(f"Subtype breakdown: {dict(subtypes)}")

    buy_candidates = [s for s in signals if s["signal_type"] in ENTRY_SIGNAL_TYPES]
    prime_setups   = [s for s in signals if s["signal_type"] == "PRIME_SETUP"]
    reentry_setups = [s for s in signals if s["signal_type"] == "REENTRY_SETUP"]
    exits          = [s for s in signals if s["signal_type"] == "EXIT"]
    reduces        = [s for s in signals if s["signal_type"] == "REDUCE"]
    watch_signals  = [s for s in signals if s["signal_type"] == "WATCH"]
    near_misses    = [s for s in watch_signals if s.get("near_miss_data")]

    logger.info(f"  BUY: {len(buy_candidates)} (PRIME:{len(prime_setups)} REENTRY:{len(reentry_setups)})")
    logger.info(f"  EXIT:{len(exits)} REDUCE:{len(reduces)} WATCH:{len(watch_signals)} (near_miss:{len(near_misses)})")

    if watch_signals:
        watch_reasons = Counter(s.get("filter_reason", "unknown") for s in watch_signals)
        for reason, count in watch_reasons.most_common(10):
            examples = [s["symbol"] for s in watch_signals if s.get("filter_reason") == reason][:4]
            logger.info(f"    {reason}: {count} — {', '.join(examples)}")
        if not DRY_RUN:
            try:
                _sb_wr = get_supabase()
                _signal_date = (watch_signals[0]["date"] if watch_signals else str(today_ist()))
                watch_reason_rows = [
                    {
                        "date":            _signal_date,
                        "filter_reason":   reason or "unknown",
                        "count":           cnt,
                        "example_symbols": [
                            s["symbol"] for s in watch_signals
                            if s.get("filter_reason") == reason
                        ][:6],
                    }
                    for reason, cnt in watch_reasons.most_common()
                ]
                _sb_wr.table("signal_watch_reasons").upsert(
                    watch_reason_rows, on_conflict="date,filter_reason"
                ).execute()
                logger.info(f"  ✓ Watch reasons written: {len(watch_reason_rows)} categories")
            except Exception as _e:
                logger.warning(f"  signal_watch_reasons write failed (non-fatal): {_e}")

    if prime_setups:
        prime_str = ", ".join(
            f"{s['symbol']}({s.get('score_adjusted',0):.0f})"
            for s in sorted(prime_setups, key=lambda x: x.get("score_adjusted", 0), reverse=True)[:8]
        )
        logger.info(f"  ★ PRIME SETUPS: {prime_str}")

    if near_misses:
        logger.info(f"  💡 Near misses for step 19 upgrade evaluation:")
        for s in near_misses[:5]:
            try:
                nm = json.loads(s.get("near_miss_data") or "{}")
                logger.info(f"    {s['symbol']}: {nm.get('blocked_by','?')} — {nm}")
            except Exception:
                pass

    save_signals(signals)
    return {
        "signals":        len(signals),
        "buy_candidates": len(buy_candidates),
        "prime_setups":   len(prime_setups),
        "exits":          len(exits),
        "near_misses":    len(near_misses),
    }


if __name__ == "__main__":
    main()