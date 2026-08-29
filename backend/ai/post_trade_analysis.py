"""
TradeOS v7 — Post-Trade Analysis v3 (Consolidated)
====================================================
Pipeline position: [17] — after step_history, before generate_shortlist.

WHAT CHANGED FROM v1 → v2
===========================

1. SIGNAL CONTEXT AT ENTRY
   v1 only used closed_positions fields.
   v2 joins signal_log on (symbol, signal_date) to pull all 80+ fields
   present at signal time: signal_subtype, score_adjusted, momentum_state,
   velocity_state, struct_edge, breakout_readiness, risk_score, holding_score,
   entry_timing_type, reentry_mode, lifecycle, vwap_alignment, bb_context,
   weekly_structure, psar_dual_confirmed, institutional_score, india_vix,
   nifty_5d_chg_pct, above_200dma_pct, fii_net_20d_ctx, sector_rank_at_entry,
   industry_state — so AI and rule engine see what the system actually believed
   at the moment of signal generation, not a reconstructed approximation.

2. MSL TRAJECTORY ANALYSIS
   v1 had no visibility into how a position evolved during the hold period.
   v2 fetches msl_history snapshots from entry_date → exit_date for the symbol
   and builds a trajectory summary: how lifecycle, velocity_state, momentum_phase,
   and holding_score changed day by day. This enables root-cause analysis like
   "system showed EXIT lifecycle 3 days before stop was hit" or "holding_score
   degraded below 30 but no exit action was taken."

3. TRADE QUALITY GRADING (A/B/C/D/F)
   v1 generated undifferentiated lessons regardless of setup quality.
   v2 assigns a letter grade at entry based on signal_subtype, score_adjusted,
   struct_edge, breakout_readiness, risk_score, and regime at entry:
     A — PRIME_SETUP, score_adjusted ≥ 75, struct_edge=YES, low risk
     B — BREAKOUT/REENTRY_SETUP or PRIME with moderate score
     C — BUY_CANDIDATE or STAGED_ENTRY in decent regime
     D — Any setup with high risk_score or CAUTION regime
     F — Setup that should have been blocked (EXTENDED timing, high risk, RISK OFF)
   Grade is stored in scenario_context for downstream filtering.

4. RICHER RULE-BASED LESSON ENGINE
   v1 had 6 deterministic rules. v2 has 20+ rules that cross-reference:
   - signal_subtype failure modes (PRIME_SETUP loss = structural failure signal)
   - Entry timing type failures (CHASING/EXTENDED entries that lost money)
   - Regime mismatch (entry in CAUTION/RISK_OFF that lost money)
   - Trajectory warnings ignored (holding_score degraded but position not exited)
   - MFE vs realised: high MFE but low realised = trailing stop management failure
   - Delivery pct patterns: low delivery + win/loss
   - RSI context: entries at RSI extremes

5. ENHANCED AI PROMPT
   v1 prompt had ~200 words of context.
   v2 prompt includes: full signal quality summary, trajectory analysis,
   MSL trajectory warnings, trade grade, existing lesson patterns to avoid
   duplication, and structured output schema extended with confidence_note
   and linked_setup_type fields.

6. LESSONS DEDUPLICATION
   v1 had no dedup — could generate identical corrective_rules for same
   scenario+sector+strategy within a short window.
   v2 checks existing lessons in the last 30 days for same
   (strategy, sector, scenario_type) before generating — if a high-confidence
   lesson already exists, it increments times_applied instead of inserting
   a duplicate.

7. SIGNAL_LOG OUTCOME ENRICHMENT (expanded)
   v1 only wrote outcome + outcome_pnl_pct back to signal_log.
   v2 additionally writes:
   - ai_note: 1-line post-trade retrospective (what the AI/rule engine concluded)
   - execution_status: COMPLETED (marks signal lifecycle as done)
   This gives the generate_shortlist and ai_enrich steps historical signal
   completion context when the same symbol re-appears.

8. REGIME HISTORY ENRICHMENT
   v1 fetched only nifty_price + india_vix from regime_history.
   v2 also fetches computed_regime, nifty_5d_chg_pct, above_200dma_pct
   from regime_history so AI sees the full market picture at entry,
   not just a regime label.

9. CONFIDENCE CALIBRATION
   v1 always inserted lessons with confidence=1.0.
   v2 sets confidence based on:
   - Trade grade A loss → confidence 0.9 (clear system failure, learn hard)
   - Trade grade F win → confidence 0.6 (won despite bad setup, don't over-learn)
   - Rule-based vs AI → AI gets 0.1 confidence boost if grade ≥ B
   - Trajectory warning ignored → confidence 0.95

WHAT CHANGED v2 → v3 (LESSON CONFIDENCE LOOP)
===============================================

THE GAP BEING CLOSED:
   v2 writes new lessons after each trade but NEVER updates existing lessons'
   times_applied or times_worked. This means lesson.confidence never changes
   from its initial value. The self-improvement loop is written but not closed.

   Steps 18 and 19 sort lessons by confidence and weight high-confidence rules
   more heavily in their AI prompts. But if confidence never changes, all lessons
   look equally reliable. A rule that's been tested 50 times and works 80% of the
   time looks identical to a rule that's never been tested.

10. LESSON CONFIDENCE LOOP (_find_applicable_lessons + _update_lesson_outcomes)
    For each completed trade, finds existing active lessons that are relevant
    based on sector, signal_type, or scenario_type and increments:
    - times_applied: always (lesson was applicable to this trade's context)
    - times_worked:  if the lesson's intent matches the outcome
    Confidence is then recomputed as times_worked / times_applied.
    Lessons below 25% win-rate after 10+ applications are auto-retired.

DATA SOURCES ADDED vs v1
=========================
  signal_log          — full 80-field signal context at entry
  msl_history         — day-by-day trajectory during hold period
  regime_history      — richer market context at entry date
  (lessons             — dedup check only, read-only)

TABLES READ
===========
  closed_positions, signal_log, msl_history,
  regime_history, nifty_upcoming_events, lessons

TABLES WRITTEN
==============
  lessons             — insert (new) or times_applied++ (dedup); confidence loop updates
  signal_log          — update outcome, outcome_pnl_pct, ai_note, execution_status
"""

import sys
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger
from config import cfg, cfg_bool, cfg_float, cfg_int, fetch_all

# ── AI_KEYS: load once at module level ────────────────────────────────────────
try:
    from config import AI_KEYS
except ImportError:
    import os
    AI_KEYS = {
        "deepseek": os.getenv("DEEPSEEK_API_KEY", ""),
        "claude":   os.getenv("ANTHROPIC_API_KEY", ""),
        "openai":   os.getenv("OPENAI_API_KEY", ""),
        "gemini":   os.getenv("GEMINI_API_KEY", ""),
        "grok":     os.getenv("GROK_API_KEY", ""),
        "copilot":  os.getenv("AZURE_OPENAI_API_KEY", ""),
    }

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

HOLDING_SCORE_EXIT_THRESHOLD = cfg_float("post_trade_analysis_holding_score_exit_threshold", 30)
MFE_TRAILING_FAILURE_RATIO = cfg_float("post_trade_analysis_mfe_trailing_failure_ratio", 0.5)
DEDUP_WINDOW_DAYS = cfg_float("post_trade_analysis_dedup_window_days", 30)

PROVIDER_COST_INR = {
    "deepseek": 0.15, "claude": 0.40, "openai": 0.30,
    "gemini":   0.08, "grok":   0.50, "copilot": 0.60,
}

# Signal subtypes in quality order (used for grading)
PRIME_SUBTYPES     = {"PRIME_SETUP"}
BREAKOUT_SUBTYPES  = {"BREAKOUT_SETUP", "REENTRY_SETUP", "MOMENTUM_CONTINUATION"}
STANDARD_SUBTYPES  = {"BUY_CANDIDATE", "STAGED_ENTRY"}
BLOCKED_SUBTYPES   = {"BLOCKED_ASM", "BLOCKED_HIGH_RISK", "BUY_BLOCKED_REGIME",
                      "AVOID_ENTRY_EVENT"}

# v3: lesson confidence loop configuration defaults
LESSON_LOOP_CONFIG_DEFAULTS = {
    "lesson_retire_min_applications": ("10",   "Min uses before auto-retiring a failing lesson"),
    "lesson_retire_win_rate":         ("0.25", "Auto-retire if win rate below this after min_applications"),
    "lesson_loop_enabled":            ("true", "Enable lesson times_applied/times_worked tracking"),
}


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_signal_context(sb, symbol: str, signal_date: str) -> dict:
    """
    Pull the full signal_log row for this symbol+date.
    Returns all 80+ fields that existed at signal generation time.
    Falls back to {} if no matching row found.
    """
    try:
        rows = (sb.table("signal_log")
                  .select("*")
                  .eq("symbol", symbol)
                  .eq("date", signal_date)
                  .limit(1)
                  .execute().data)
        return rows[0] if rows else {}
    except Exception as e:
        logger.warning(f"signal_log context fetch failed for {symbol}@{signal_date}: {e}")
        return {}


def load_msl_trajectory(sb, symbol: str,
                         entry_date: str, exit_date: str) -> list[dict]:
    """
    Fetch msl_history rows for symbol between entry and exit dates.
    Returns a list of dicts sorted by snapshot_date ASC.
    Fields of interest: snapshot_date, lifecycle, velocity_state,
    momentum_phase, trend_maturity, holding_score, final_score,
    struct_edge, reentry_mode, entry_timing_type.
    """
    try:
        rows = (sb.table("msl_history")
                  .select(
                      "snapshot_date,lifecycle,velocity_state,momentum_phase,"
                      "trend_maturity,holding_score,final_score,struct_edge,"
                      "reentry_mode,entry_timing_type,validity_score"
                  )
                  .eq("symbol", symbol)
                  .gte("snapshot_date", entry_date)
                  .lte("snapshot_date", exit_date)
                  .order("snapshot_date", desc=False)
                  .execute().data)
        return rows or []
    except Exception as e:
        logger.warning(f"msl_history trajectory fetch failed for {symbol}: {e}")
        return []


def load_regime_at_entry(sb, entry_date: str) -> dict:
    """
    Fetch rich regime context from regime_history at entry_date.
    v2: pulls computed_regime, nifty_5d_chg_pct, above_200dma_pct
    in addition to v1's regime/nifty_price/india_vix.
    """
    try:
        rows = (sb.table("regime_history")
                  # predicted_regime is a stock_data_daily column and has never
                  # existed on regime_history, whose ML-side equivalent is
                  # computed_regime. One unknown column fails the whole query,
                  # so this returned nothing and every post-trade analysis ran
                  # without any regime context at all.
                  .select(
                      "regime,computed_regime,nifty_price,india_vix,"
                      "nifty_5d_chg_pct,above_200dma_pct"
                  )
                  .eq("date", entry_date)
                  .limit(1)
                  .execute().data)
        return rows[0] if rows else {}
    except Exception:
        return {}


def load_events_at_entry(sb, symbol: str, entry_date: str) -> str:
    """Events recorded near entry — from nifty_upcoming_events (unchanged from v1)."""
    try:
        rows = (sb.table("nifty_upcoming_events")
                  .select("purpose,event_date")
                  .eq("symbol", symbol)
                  .execute().data)
        if rows:
            return ", ".join(f"{r['purpose']} on {r['event_date']}" for r in rows[:3])
        return "None recorded"
    except Exception:
        return "None recorded"


def load_existing_lessons(sb, strategy: str, sector: str,
                            scenario_type: str) -> list[dict]:
    """
    Fetch recent lessons matching same (strategy, sector, scenario_type)
    within DEDUP_WINDOW_DAYS for deduplication check.
    """
    try:
        cutoff = (today_ist() - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
        rows = fetch_all(lambda: sb.table("lessons")
                  .select("id,corrective_rule,confidence,times_applied,scenario_context")
                  .eq("impacted_sector", sector)
                  .eq("is_active", True)
                  .gte("date", cutoff))
        # Filter for same strategy + scenario_type in scenario_context
        return [r for r in rows
                if strategy in (r.get("scenario_context") or "")
                and scenario_type in (r.get("scenario_context") or "")]
    except Exception as e:
        logger.warning(f"Existing lessons check failed: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# TRAJECTORY ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyse_trajectory(trajectory: list[dict]) -> dict:
    """
    Analyse msl_history snapshots from entry → exit.

    Returns a summary dict with:
      days_held            int
      lifecycle_exit_warned bool   — lifecycle reached EXIT before actual exit
      holding_score_degraded bool  — holding_score fell below threshold mid-hold
      holding_score_min    float
      holding_score_at_entry float
      lifecycle_sequence   str     — e.g. "HOLD→HOLD→REDUCE→EXIT"
      velocity_sequence    str     — e.g. "ACCELERATING→FLAT→DECELERATING"
      trend_maturity_end   str     — trend_maturity at final snapshot
      trajectory_warning   str     — highest-severity warning found
    """
    if not trajectory:
        return {
            "days_held": 0,
            "lifecycle_exit_warned": False,
            "holding_score_degraded": False,
            "holding_score_min": None,
            "holding_score_at_entry": None,
            "lifecycle_sequence": "",
            "velocity_sequence": "",
            "trend_maturity_end": "",
            "trajectory_warning": "NO_HISTORY",
        }

    lifecycles   = [r.get("lifecycle", "") for r in trajectory]
    velocities   = [r.get("velocity_state", "") for r in trajectory]
    hs_values    = [float(r.get("holding_score") or 0) for r in trajectory]
    maturities   = [r.get("trend_maturity", "") for r in trajectory]

    exit_warned   = any(lc in ("EXIT", "REDUCE") for lc in lifecycles[1:])  # skip entry
    hs_degraded   = any(h > 0 and h < HOLDING_SCORE_EXIT_THRESHOLD for h in hs_values)
    hs_nonzero    = [h for h in hs_values if h > 0]
    hs_min        = min(hs_nonzero) if hs_nonzero else None
    hs_at_entry   = hs_nonzero[0] if hs_nonzero else None

    # Compact sequence strings (deduplicate consecutive repeats)
    def _compact(seq):
        out = []
        for v in seq:
            if v and (not out or out[-1] != v):
                out.append(v)
        return "→".join(out) if out else ""

    lc_seq   = _compact(lifecycles)
    vel_seq  = _compact(velocities)
    mat_end  = maturities[-1] if maturities else ""

    # Warning priority
    warning = "NONE"
    if hs_degraded and exit_warned:
        warning = "HOLDING_SCORE_AND_LIFECYCLE_EXIT_BOTH_FLAGGED"
    elif exit_warned:
        warning = "LIFECYCLE_EXIT_BEFORE_ACTUAL_EXIT"
    elif hs_degraded:
        warning = "HOLDING_SCORE_DEGRADED_BELOW_THRESHOLD"
    elif mat_end in ("EXHAUSTED", "LATE"):
        warning = "TREND_MATURITY_EXHAUSTED_AT_EXIT"

    return {
        "days_held":              len(trajectory),
        "lifecycle_exit_warned":  exit_warned,
        "holding_score_degraded": hs_degraded,
        "holding_score_min":      hs_min,
        "holding_score_at_entry": hs_at_entry,
        "lifecycle_sequence":     lc_seq,
        "velocity_sequence":      vel_seq,
        "trend_maturity_end":     mat_end,
        "trajectory_warning":     warning,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TRADE QUALITY GRADING
# ─────────────────────────────────────────────────────────────────────────────

def grade_trade_entry(signal_ctx: dict, trade: dict) -> str:
    """
    Assign A/B/C/D/F letter grade to entry quality using signal_log context.

    A: PRIME_SETUP, score_adjusted ≥ 75, struct_edge=YES, risk_score < 50,
       entry in NEUTRAL/BULLISH regime
    B: BREAKOUT/REENTRY_SETUP or PRIME with score_adjusted 60-74
    C: BUY_CANDIDATE/STAGED_ENTRY, score_adjusted ≥ 55, no major red flags
    D: Any setup with risk_score ≥ 65 or CAUTION regime or LATE trend_maturity
    F: Extended timing without reentry, RISK_OFF regime, blocked setup,
       score_adjusted < 45 — should have been filtered
    """
    if not signal_ctx:
        return "C"   # no signal context — assume standard setup

    signal_subtype  = (signal_ctx.get("signal_type") or "").upper()
    score_adj       = float(signal_ctx.get("score_adjusted") or signal_ctx.get("score") or 0)
    struct_edge     = (signal_ctx.get("struct_edge") or "").upper()
    risk_score      = float(signal_ctx.get("risk_score") or 0)
    regime          = (signal_ctx.get("regime") or "").upper()
    entry_timing    = (signal_ctx.get("entry_timing_type") or "").upper()
    reentry_mode    = (signal_ctx.get("reentry_mode") or "").upper()
    trend_maturity  = (signal_ctx.get("trend_maturity") or "").upper()
    breakout_ready  = float(signal_ctx.get("breakout_readiness") or 0)

    # Grade F — entry should have been blocked
    if (regime == "RISK OFF"
            or signal_subtype in BLOCKED_SUBTYPES
            or (entry_timing == "EXTENDED" and reentry_mode != "ELIGIBLE")
            or score_adj < 45):
        return "F"

    # Grade D — elevated risk or late/exhausted setup
    if (risk_score >= 65
            or regime == "CAUTION"
            or trend_maturity in ("EXHAUSTED", "LATE")
            or score_adj < 55):
        return "D"

    # Grade A — PRIME_SETUP fully aligned
    if (signal_subtype == "PRIME_SETUP"
            and score_adj >= 75
            and struct_edge == "YES"
            and risk_score < 50
            and regime in ("NEUTRAL", "BULLISH", "")):
        return "A"

    # Grade B — PRIME but not quite A, or high-quality BREAKOUT/REENTRY
    if (signal_subtype in (PRIME_SUBTYPES | BREAKOUT_SUBTYPES)
            and score_adj >= 60):
        return "B"
    if breakout_ready >= 70 and score_adj >= 60:
        return "B"

    # Grade C — standard BUY_CANDIDATE in acceptable conditions
    if signal_subtype in STANDARD_SUBTYPES and score_adj >= 55:
        return "C"

    return "C"   # default


def backfill_entry_grades(sb, since: str | None = None,
                          dry_run: bool = False) -> dict:
    """
    One-time backfill for closed SWING trades that predate migration 093 —
    F-68 follow-up, 24-Aug-2026.

    `main()`'s own loop only computes a grade the FIRST time a trade is
    analysed; a trade already carrying a `lessons` row (the common case for
    anything closed before today) is skipped by that loop's own dedup check
    and would never pass through `grade = lesson.pop("_trade_grade", ...)`
    again. Without this, `entry_grade` would only ever populate for trades
    closing from today forward — leaving Stage E2's own "does the grade
    predict outcome" question unanswerable for months.

    Deliberately NARROWER than `analyze_trade()`: no AI call, no `lessons`
    table write, no dedup bookkeeping — `grade_trade_entry()` is a pure
    function of `signal_ctx` and `trade`, and that is all this needs to
    compute the same grade a full analysis run would have produced.
    SWING only, same scoping choice as the forward-fix in `main()`.
    """
    trades = fetch_all(lambda: sb.table("closed_positions").select("*")
                       .is_("entry_grade", "null")
                       .eq("framework", "SWING"))
    if since:
        trades = [t for t in trades if str(t.get("exit_date") or "") >= since]

    dist: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    written = 0
    for trade in trades:
        sym         = trade.get("symbol", "")
        signal_date = str(trade.get("signal_date") or trade.get("entry_date", ""))
        entry_date  = str(trade.get("entry_date", ""))
        signal_ctx  = load_signal_context(sb, sym, signal_date)
        if not signal_ctx and signal_date != entry_date:
            signal_ctx = load_signal_context(sb, sym, entry_date)
        grade = grade_trade_entry(signal_ctx, trade)
        dist[grade] = dist.get(grade, 0) + 1
        if dry_run:
            logger.info(f"  [DRY RUN] {sym} ({entry_date} -> "
                       f"{trade.get('exit_date')}): grade {grade}")
            continue
        try:
            sb.table("closed_positions").update(
                {"entry_grade": grade}).eq("id", trade["id"]).execute()
            written += 1
        except Exception as e:
            logger.warning(f"  {sym}: entry_grade backfill write failed — {e}")

    logger.info(f"  backfill_entry_grades: {len(trades)} trade(s), "
               f"{written} written, distribution {dist}")
    return {"n": len(trades), "written": written, "distribution": dist}


# ─────────────────────────────────────────────────────────────────────────────
# RULE-BASED LESSON ENGINE v2
# ─────────────────────────────────────────────────────────────────────────────

def generate_rule_based_lesson(trade: dict, signal_ctx: dict,
                                 traj: dict, trade_grade: str) -> dict:
    """
    Generate structured lesson using 20+ deterministic rules that cross-reference
    signal context, trajectory analysis, and trade outcome.

    Rule priority (highest specificity first):
      1. Trajectory warnings (system flagged exit — was it acted on?)
      2. Grade F losses (entry should never have happened)
      3. Grade A losses (PRIME_SETUP failed — structural signal failure)
      4. MFE vs realised gap (trailing stop management)
      5. Entry timing type failures
      6. Regime context failures
      7. Strategy-based patterns (v1 fallback)
    """
    pnl_pct      = float(trade.get("pnl_pct", 0) or 0)
    exit_reason  = str(trade.get("exit_reason", "")).upper()
    strategy     = trade.get("strategy", "")
    sector       = trade.get("sector", "")
    lifecycle_e  = (trade.get("lifecycle_at_entry") or "").upper()
    mfe          = float(trade.get("max_favorable_excursion", 0) or 0)
    is_win       = pnl_pct > 0

    signal_sub   = (signal_ctx.get("signal_type") or "BUY_CANDIDATE").upper()
    entry_timing = (signal_ctx.get("entry_timing_type") or "").upper()
    reentry_mode = (signal_ctx.get("reentry_mode") or "").upper()
    regime_entry = (signal_ctx.get("regime") or "").upper()
    risk_score   = float(signal_ctx.get("risk_score") or 0)
    rsi_at_entry = float(signal_ctx.get("rsi_daily") or 0)
    delivery_pct = float(signal_ctx.get("delivery_pct") or 0)
    vol_ratio    = float(signal_ctx.get("vol_ratio") or 0)
    score_adj    = float(signal_ctx.get("score_adjusted") or 0)
    above_200dma = float(signal_ctx.get("above_200dma_pct") or 0)
    fii_flag     = (signal_ctx.get("fii_flag") or "").upper()

    traj_warning   = traj.get("trajectory_warning", "NONE")
    lc_sequence    = traj.get("lifecycle_sequence", "")
    vel_sequence   = traj.get("velocity_sequence", "")
    hs_min         = traj.get("holding_score_min")
    hs_at_entry    = traj.get("holding_score_at_entry")
    exit_warned    = traj.get("lifecycle_exit_warned", False)
    hs_degraded    = traj.get("holding_score_degraded", False)
    days_held      = traj.get("days_held", 0)

    realised_vs_mfe = (pnl_pct / (mfe * 100)) if (mfe > 0 and pnl_pct > 0) else None

    # ── Outcome scenario type ─────────────────────────────────────────────────
    if is_win:
        st_map = {"CTL": "Win/Trend-Follow", "SBS": "Win/Breakout", "TPO": "Win/Mean-Revert"}
        scenario_type = st_map.get(strategy, "Win/Trend-Follow")
        if realised_vs_mfe is not None and realised_vs_mfe < MFE_TRAILING_FAILURE_RATIO:
            scenario_type = "Win/Suboptimal-Exit"
    else:
        if trade_grade == "F":
            scenario_type = "Loss/Invalid-Entry"
        elif trade_grade == "A":
            scenario_type = "Loss/Prime-Setup-Failure"
        elif "STOP" in exit_reason and mfe > 1.5:
            scenario_type = "Loss/Stop-Hunt"
        elif "EVENT" in exit_reason or "RESULT" in exit_reason:
            scenario_type = "Loss/Event-Risk"
        elif "SECTOR" in exit_reason or "ROTATION" in exit_reason:
            scenario_type = "Loss/Sector-Rotation"
        elif lifecycle_e in ("EXTENDED", "OVEREXTENDED"):
            scenario_type = "Loss/Overextended"
        elif regime_entry in ("RISK OFF", "CAUTION"):
            scenario_type = "Loss/Regime-Mismatch"
        elif exit_warned:
            scenario_type = "Loss/System-Exit-Signal-Ignored"
        else:
            scenario_type = "Loss/False-Breakout"

    # ── Trigger event ─────────────────────────────────────────────────────────
    trigger_event = exit_reason or "Unknown"
    if exit_warned and not is_win:
        trigger_event = f"{exit_reason} (MSL lifecycle warned: {lc_sequence})"

    # ── Root cause ────────────────────────────────────────────────────────────
    if is_win:
        if realised_vs_mfe is not None and realised_vs_mfe < MFE_TRAILING_FAILURE_RATIO:
            root_cause = (f"Trade reached MFE of {mfe:.1f}x but realised only "
                          f"{pnl_pct:.1f}% — trailing stop not tightened after "
                          f"favorable excursion.")
        else:
            root_cause = (f"{strategy} setup executed cleanly in {sector}. "
                          f"Signal subtype {signal_sub} at grade {trade_grade} confirmed.")
    elif trade_grade == "F":
        root_cause = (f"Entry should have been blocked: "
                      f"entry_timing={entry_timing}, regime={regime_entry}, "
                      f"risk_score={risk_score:.0f}, score_adj={score_adj:.0f}. "
                      f"Filter logic may have been bypassed or thresholds misconfigured.")
    elif trade_grade == "A":
        root_cause = (f"PRIME_SETUP (grade A) failed — indicates a structural signal "
                      f"failure. Velocity/momentum sequence was: {vel_sequence}. "
                      f"Market context: regime={regime_entry}, above_200dma={above_200dma:.0f}%.")
    elif exit_warned and hs_degraded:
        root_cause = (f"MSL holding_score degraded to {hs_min:.0f} (below threshold {HOLDING_SCORE_EXIT_THRESHOLD}) "
                      f"and lifecycle showed exit signal during hold. "
                      f"Trajectory: {lc_sequence}. Exit action not taken in time.")
    elif exit_warned:
        root_cause = (f"MSL lifecycle transitioned to exit/reduce ({lc_sequence}) "
                      f"during {days_held}-day hold, but position was not closed. "
                      f"Rule-based exit from msl_history not acted upon.")
    elif "STOP" in exit_reason and mfe > 1.5:
        root_cause = (f"Trade moved {mfe:.1f}x in our favour but reversed. "
                      f"Trailing stop not moved to lock in gains after favorable excursion.")
    elif "EVENT" in exit_reason:
        root_cause = "Event risk at entry not adequately priced — exit triggered by event volatility."
    elif regime_entry == "CAUTION":
        root_cause = (f"Entry taken in CAUTION regime for {strategy} in {sector}. "
                      f"Score_adjusted was {score_adj:.0f} but regime context reduced edge.")
    elif rsi_at_entry > 75:
        root_cause = (f"RSI at entry was {rsi_at_entry:.0f} — extended territory. "
                      f"Entry_timing_type={entry_timing} with elevated RSI increases mean-reversion risk.")
    elif delivery_pct > 0 and delivery_pct < 30:
        root_cause = (f"Low delivery percentage ({delivery_pct:.0f}%) at entry suggests "
                      f"speculative price action without institutional commitment. "
                      f"Institutional_score at entry: {signal_ctx.get('institutional_score', 'N/A')}.")
    elif lifecycle_e in ("EXTENDED", "OVEREXTENDED"):
        root_cause = f"Entry in {lifecycle_e} lifecycle — insufficient margin of safety for {strategy}."
    else:
        root_cause = f"Setup failed to follow through — likely false signal in {sector} sector."

    # ── What expected / what happened ────────────────────────────────────────
    what_expected = (f"{signal_sub} momentum continuation in {sector} "
                     f"from {lifecycle_e or 'UNKNOWN'} lifecycle "
                     f"(score_adj={score_adj:.0f}, grade={trade_grade})")
    if is_win:
        what_happened = (f"Trade reached target with {pnl_pct:+.1f}% P&L "
                         f"over {days_held} day(s). MFE={mfe:.1f}x.")
    else:
        what_happened = (f"Trade stopped out with {pnl_pct:+.1f}% P&L "
                         f"after {days_held} day(s). MFE reached {mfe:.1f}x.")

    what_failed = "Nothing — trade worked as planned" if is_win else root_cause

    # ── Corrective rule ───────────────────────────────────────────────────────
    if is_win and realised_vs_mfe is not None and realised_vs_mfe < MFE_TRAILING_FAILURE_RATIO:
        corrective_rule = (f"Rule: Once MFE exceeds 1.5x initial risk, tighten trailing stop "
                           f"to lock in at least {int(MFE_TRAILING_FAILURE_RATIO * 100)}% of excursion. "
                           f"Applies to {strategy} in {sector}.")
    elif is_win:
        corrective_rule = f"Rule: Continue applying {signal_sub} for {strategy} in {sector} — setup validated at grade {trade_grade}."
    elif trade_grade == "F":
        corrective_rule = (f"Rule: Hard-block entries where entry_timing={entry_timing} "
                           f"and regime={regime_entry}. Audit signal filter thresholds for "
                           f"{strategy} in {sector}.")
    elif trade_grade == "A":
        corrective_rule = (f"Rule: When PRIME_SETUP fails, check above_200dma_pct < 40% "
                           f"or fii_flag={fii_flag} as regime-level veto. "
                           f"PRIME_SETUP grade A should be blocked in weak breadth.")
    elif exit_warned and hs_degraded:
        corrective_rule = (f"Rule: Auto-exit or flag for human review when holding_score "
                           f"drops below {HOLDING_SCORE_EXIT_THRESHOLD} AND lifecycle shows "
                           f"EXIT/REDUCE. Trajectory showed warning for {days_held} days before stop hit.")
    elif exit_warned:
        corrective_rule = (f"Rule: Set automated exit trigger when msl_history lifecycle "
                           f"transitions to EXIT for 2+ consecutive days. "
                           f"Position in {sector} was held {days_held} days past the signal.")
    elif "STOP" in exit_reason and mfe > 1.5:
        corrective_rule = "Rule: Once MFE exceeds 1.5x risk, move stop to breakeven + 0.5R."
    elif "EVENT" in exit_reason:
        corrective_rule = "Rule: Check nifty_upcoming_events before entry — avoid within 2 days of results."
    elif regime_entry == "CAUTION":
        corrective_rule = (f"Rule: In CAUTION regime, require score_adjusted ≥ 75 "
                           f"(not {score_adj:.0f}) and signal_subtype PRIME_SETUP only "
                           f"for new {strategy} entries in {sector}.")
    elif rsi_at_entry > 75:
        corrective_rule = (f"Rule: For {strategy}, cap RSI-at-entry at 72 to avoid "
                           f"mean-reversion risk. Current breach: {rsi_at_entry:.0f}.")
    elif delivery_pct > 0 and delivery_pct < 30:
        corrective_rule = (f"Rule: Require delivery_pct ≥ 35% for {strategy} entry "
                           f"in {sector} to ensure institutional commitment.")
    elif lifecycle_e in ("EXTENDED", "OVEREXTENDED"):
        corrective_rule = f"Rule: Do not enter {strategy} when lifecycle_at_entry is EXTENDED or OVEREXTENDED."
    else:
        corrective_rule = (f"Rule: Require vol_ratio > 1.5 (was {vol_ratio:.1f}) and "
                           f"delivery_pct > 35% (was {delivery_pct:.0f}%) before "
                           f"{strategy} entry in {sector}.")

    return {
        "scenario_type":   scenario_type,
        "trigger_event":   trigger_event,
        "what_expected":   what_expected,
        "what_happened":   what_happened,
        "what_failed":     what_failed,
        "root_cause":      root_cause,
        "corrective_rule": corrective_rule,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE CALIBRATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_lesson_confidence(trade_grade: str, is_win: bool,
                               source: str, traj: dict) -> float:
    """
    Base confidence 1.0 adjusted by:
      Grade A loss  → 0.90 (clear system failure, strong learning signal)
      Grade F win   → 0.65 (won despite bad setup, don't over-correct)
      Grade F loss  → 0.85 (bad entry predictably failed)
      Traj warning  → +0.05 (system was right, we ignored it)
      AI source     → +0.05 for grade A/B (AI has richer context)
    Capped at 0.95 to allow human review to push to 1.0.
    """
    conf = 1.0

    if not is_win:
        if trade_grade == "A":
            conf = 0.90
        elif trade_grade == "F":
            conf = 0.85
        elif trade_grade == "D":
            conf = 0.80
    else:
        if trade_grade == "F":
            conf = 0.65
        elif trade_grade == "D":
            conf = 0.75

    if traj.get("trajectory_warning", "NONE") not in ("NONE", "NO_HISTORY"):
        conf = min(conf + 0.05, 0.95)

    if source.startswith("AI:") and trade_grade in ("A", "B"):
        conf = min(conf + 0.05, 0.95)

    return round(conf, 2)


# ─────────────────────────────────────────────────────────────────────────────
# v3: LESSON CONFIDENCE LOOP — ADDITION 1
# Find applicable existing lessons for a completed trade
# ─────────────────────────────────────────────────────────────────────────────

def _find_applicable_lessons(sb, trade: dict, signal_ctx: dict,
                              lesson_data: dict) -> list[dict]:
    """
    Find active lessons relevant to this trade outcome.
    Used to increment times_applied (and conditionally times_worked).

    Matching logic (ordered by specificity):
      1. Exact symbol match: lesson.linked_symbols contains this symbol
      2. Sector + signal_type match: lesson.impacted_sector matches AND
         lesson.scenario_context contains the signal_type
      3. Scenario type match: lesson.scenario_type matches the new lesson's
         scenario_type (same failure pattern, different stock)

    Returns list of lesson dicts with id, corrective_rule, scenario_type,
    times_applied, times_worked, confidence.
    """
    sym         = trade.get("symbol", "")
    sector      = trade.get("sector", "")
    signal_type = signal_ctx.get("signal_type", "")
    scenario    = lesson_data.get("scenario_type", "") if isinstance(lesson_data, dict) else ""

    applicable = []
    seen_ids   = set()

    # ── Match 1: exact symbol ──
    try:
        rows = fetch_all(lambda:
            sb.table("lessons")
              .select("id,corrective_rule,scenario_type,impacted_sector,"
                      "times_applied,times_worked,confidence,scenario_context,source")
              .eq("is_active", True)
              .contains("linked_symbols", [sym])
        )
        for r in rows:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                applicable.append(r)
    except Exception as e:
        logger.debug(f"Lesson exact symbol match failed: {e}")

    # ── Match 2: sector + signal_type context ──
    if sector:
        try:
            rows = fetch_all(lambda:
                sb.table("lessons")
                  .select("id,corrective_rule,scenario_type,impacted_sector,"
                          "times_applied,times_worked,confidence,scenario_context,source")
                  .eq("is_active", True)
                  .ilike("impacted_sector", f"%{sector}%")
            )
            for r in rows:
                if r["id"] not in seen_ids:
                    ctx = (r.get("scenario_context") or "").lower()
                    if signal_type and signal_type.lower()[:8] in ctx:
                        seen_ids.add(r["id"])
                        applicable.append(r)
        except Exception as e:
            logger.debug(f"Lesson sector match failed: {e}")

    # ── Match 3: same scenario_type (same failure pattern) ──
    if scenario:
        try:
            rows = (
                sb.table("lessons")
                  .select("id,corrective_rule,scenario_type,impacted_sector,"
                          "times_applied,times_worked,confidence,scenario_context,source")
                  .eq("is_active", True)
                  .eq("scenario_type", scenario)
                  .order("date", desc=True)
                  .limit(3)
                  .execute().data
            )
            for r in rows:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    applicable.append(r)
        except Exception as e:
            logger.debug(f"Lesson scenario_type match failed: {e}")

    return applicable[:8]  # cap to avoid over-updating


# ─────────────────────────────────────────────────────────────────────────────
# v3: LESSON CONFIDENCE LOOP — ADDITION 2
# Update lesson outcomes (times_applied, times_worked, confidence, retirement)
# ─────────────────────────────────────────────────────────────────────────────

def _update_lesson_outcomes(sb, applicable_lessons: list[dict],
                             trade: dict, signal_ctx: dict,
                             new_lesson_data: dict):
    """
    For each applicable lesson, increment times_applied and conditionally
    times_worked, then recompute confidence as times_worked / times_applied.

    times_worked logic:
      Loss-prevention rules (scenario_type starts with "Loss/"):
        - times_worked += 1 if the trade was a LOSS (rule correctly predicted
          the failure mode)
        - Also +1 for trajectory-based rules when the trajectory warning fired

      Win-enhancement rules (scenario_type starts with "Win/"):
        - times_worked += 1 if the trade was a WIN

      Exit/trailing rules (scenario_type contains "Exit" or "Stop"):
        - times_worked += 1 if MFE was realised at ≥ 50% (no trailing failure)

      System calibration rules (source=CALIBRATION):
        - Always increment times_applied only, never times_worked

    Retirement: sets is_active=False when times_applied ≥ 10 AND
    times_worked/times_applied < 0.25 (aligns with step 20 LESSON_RETIRE_WIN_RATE).
    Manual lessons (source=MANUAL) are never auto-retired.
    """
    is_win   = float(trade.get("pnl_pct") or 0) > 0
    mfe      = float(trade.get("max_favorable_excursion") or 0)
    pnl      = float(trade.get("pnl_pct") or 0)
    traj_w   = (new_lesson_data.get("trajectory_warning", "NONE")
                if isinstance(new_lesson_data, dict) else "NONE")

    updated_count = 0
    retired_count = 0

    for lesson in applicable_lessons:
        lid = lesson.get("id")
        if not lid:
            continue

        source        = (lesson.get("source") or "").upper()
        scenario_type = (lesson.get("scenario_type") or "").upper()

        # Skip calibration lessons
        if "CALIBRATION" in source:
            continue

        new_applied = (lesson.get("times_applied") or 0) + 1

        # Determine if this lesson "worked" for this trade
        lesson_worked = False

        if scenario_type.startswith("LOSS/"):
            lesson_worked = not is_win  # confirmed failure mode
            # Trajectory-based exit rule: correct if warning fired
            if "EXIT-SIGNAL" in scenario_type and traj_w not in ("NONE", "NO_HISTORY"):
                lesson_worked = True

        elif scenario_type.startswith("WIN/"):
            lesson_worked = is_win

        elif "EXIT" in scenario_type or "STOP" in scenario_type:
            # Exit management: worked if MFE was substantially realised
            if mfe > 0 and pnl > 0:
                realised_ratio = pnl / (mfe * 100)
                lesson_worked  = realised_ratio >= 0.5
            else:
                lesson_worked = False

        else:
            lesson_worked = is_win

        new_worked = (lesson.get("times_worked") or 0) + (1 if lesson_worked else 0)
        new_conf   = round(new_worked / new_applied, 3) if new_applied > 0 else 0.5

        # Retirement check
        _retire_min = cfg_int("lesson_retire_min_applications", 10)
        _retire_wr  = cfg_float("lesson_retire_win_rate", 0.25)
        should_retire = (
            new_applied >= _retire_min
            and new_conf < _retire_wr
            and "MANUAL" not in source
        )

        try:
            sb.table("lessons").update({
                "times_applied": new_applied,
                "times_worked":  new_worked,
                "confidence":    new_conf,
                "is_active":     not should_retire,
            }).eq("id", lid).execute()
            updated_count += 1

            if should_retire:
                retired_count += 1
                logger.info(
                    f"  Lesson {lid} auto-retired: "
                    f"conf={new_conf:.1%} after {new_applied} uses | "
                    f"type={lesson.get('scenario_type','?')}"
                )
        except Exception as e:
            logger.warning(f"Lesson outcome update failed for id={lid}: {e}")

    if updated_count > 0:
        logger.debug(
            f"  Lesson confidence loop: {updated_count} updated "
            f"({retired_count} retired) for {trade.get('symbol','?')}"
        )

    return {"updated": updated_count, "retired": retired_count}


# ─────────────────────────────────────────────────────────────────────────────
# v3: LESSON CONFIDENCE LOOP — ADDITION 3
# system_config migration helper (idempotent, run once during setup)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_lesson_loop_config(sb):
    """
    Ensure system_config has lesson loop configuration keys.
    Idempotent — safe to call on every startup.
    """
    existing = {r["key"] for r in sb.table("system_config").select("key").execute().data}
    for key, (value, desc) in LESSON_LOOP_CONFIG_DEFAULTS.items():
        if key not in existing:
            try:
                sb.table("system_config").insert({
                    "key": key, "value": value, "description": desc
                }).execute()
                logger.info(f"system_config: added {key}={value}")
            except Exception as e:
                logger.warning(f"system_config insert failed for {key}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# AI PROMPT v2
# ─────────────────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT_V2 = """You are an expert Indian swing trader reviewing a completed trade with full signal intelligence context. Analyze this trade and generate a structured lesson.

TRADE DETAILS:
Symbol: {symbol} ({company_name}) — {sector}
Strategy: {strategy}
Entry: {entry_date} @ ₹{entry_price} | Exit: {exit_date} @ ₹{exit_price}
Qty: {actual_qty} | Invested: ₹{invested_value:.0f}
P&L: ₹{realized_pnl:.0f} ({pnl_pct:+.1f}%)
Exit reason: {exit_reason}
Lifecycle at entry: {lifecycle_at_entry} | Expected R at entry: {expected_r}
High water mark: ₹{high_water_mark} | MFE: {mfe}x
Entry timing type: {entry_timing_type} | Reentry mode: {reentry_mode}

SIGNAL QUALITY AT ENTRY:
Signal subtype: {signal_subtype}
Score adjusted: {score_adjusted} | Trade grade: {trade_grade}
Struct edge: {struct_edge} | Risk score: {risk_score}
Momentum state: {momentum_state} | Velocity: {velocity_state}
Momentum phase: {momentum_phase} | Trend maturity: {trend_maturity}
Breakout readiness: {breakout_readiness} | BB squeeze: {bb_squeeze}
Holding score at entry: {holding_score} | Institutional score: {institutional_score}
RSI daily: {rsi_daily} | Vol ratio: {vol_ratio} | Delivery %: {delivery_pct}
VWAP alignment: {vwap_alignment} | Weekly structure: {weekly_structure}
PSAR dual confirmed: {psar_dual_confirmed} | MACD direction: {macd_direction}

MARKET CONTEXT AT ENTRY:
Regime: {regime} | VIX: {india_vix}
Nifty 5d chg: {nifty_5d_chg_pct}% | Above 200DMA: {above_200dma_pct}%
FII flag: {fii_flag} | FII net 20d: {fii_net_20d_ctx}
Sector rank at entry: {sector_rank_at_entry}
Industry state: {industry_state}

POSITION TRAJECTORY (msl_history entry→exit):
Days held: {days_held}
Lifecycle sequence: {lifecycle_sequence}
Velocity sequence: {velocity_sequence}
Holding score min during hold: {holding_score_min}
Trend maturity at exit: {trend_maturity_end}
Trajectory warning: {trajectory_warning}

UPCOMING EVENTS AT ENTRY:
{events_at_entry}

EXISTING LESSONS (same setup type in last 30 days — do NOT duplicate):
{existing_lessons_summary}

Generate a structured lesson. The rule must be highly SPECIFIC and actionable — reference exact fields, thresholds, and conditions from the signal context above. Output ONLY this JSON:
{{
  "scenario_type": "one of: Win/Trend-Follow, Win/Breakout, Win/Mean-Revert, Win/Suboptimal-Exit, Loss/Stop-Hunt, Loss/Event-Risk, Loss/False-Breakout, Loss/Overextended, Loss/Regime-Mismatch, Loss/System-Exit-Signal-Ignored, Loss/Prime-Setup-Failure, Loss/Invalid-Entry, Breakeven",
  "trigger_event": "what specifically triggered the exit (be precise)",
  "what_expected": "1 sentence: what the system expected at signal time",
  "what_happened": "1 sentence: what actually occurred",
  "what_failed": "specific failure point referencing signal fields, or 'Nothing — trade worked as planned'",
  "root_cause": "underlying reason using specific field values from the context above",
  "corrective_rule": "a rule starting with 'Rule:' that references specific field names and thresholds",
  "confidence_note": "1 sentence explaining your confidence in this lesson (grade {trade_grade} setup)"
}}"""


def _format_existing_lessons_summary(lessons: list[dict]) -> str:
    if not lessons:
        return "None — first occurrence of this pattern."
    return "; ".join(
        f"[{r.get('scenario_type', '?')}] {(r.get('corrective_rule') or '')[:80]}"
        for r in lessons[:3]
    )


# ─────────────────────────────────────────────────────────────────────────────
# AI PROVIDER CALL (unchanged from v1 — single responsibility)
# ─────────────────────────────────────────────────────────────────────────────

def _call_provider(provider_name: str, keys: dict, prompt: str) -> str:
    api_key = (keys or {}).get(provider_name, "")
    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider_name}'")

    if provider_name == "deepseek":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        # THINKING OFF BY DEFAULT — same fix, same reason, as ai_router.py's
        # own raw_completion(): deepseek-v4-flash is a reasoning model whose
        # max_tokens budgets reasoning AND output together, reasoning spent
        # first. This call site built its own client instead of routing
        # through ai_router and never got that fix — an 800-token budget
        # here can be consumed entirely by discarded reasoning before a
        # single character of the lesson text is written. Found 29-Aug-2026
        # auditing every AI call site's cost; nothing reads reasoning_content
        # from this call, only the returned string, so there is nothing to
        # lose by disabling it.
        extra: dict = {}
        if not cfg_bool("ai_thinking_enabled", False):
            extra["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = client.chat.completions.create(
            model="deepseek-v4-flash", max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "deepseek", "deepseek-v4-flash", resp)
        return resp.choices[0].message.content.strip()

    elif provider_name == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "claude", "claude-haiku-4-5-20251001", msg)
        return msg.content[0].text.strip()

    elif provider_name == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "openai", "gpt-4o-mini", resp)
        return resp.choices[0].message.content.strip()

    elif provider_name == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        resp = model.generate_content(prompt)
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "gemini", "gemini-2.5-flash", resp)
        return resp.text.strip()

    elif provider_name == "grok":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model="grok-4-latest", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "grok", "grok-4-latest", resp)
        return resp.choices[0].message.content.strip()

    elif provider_name == "copilot":
        from openai import AzureOpenAI
        from config import AZURE_ENDPOINT, AZURE_DEPLOYMENT
        client = AzureOpenAI(api_key=api_key, azure_endpoint=AZURE_ENDPOINT,
                             api_version="2024-02-01")
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        from ai.usage_tracker import log_usage
        log_usage("post_trade_analysis", "copilot", AZURE_DEPLOYMENT, resp)
        return resp.choices[0].message.content.strip()

    return ""


def _safe_parse_ai_json(raw: str) -> dict | None:
    json_match = re.search(r'\{.*\}', raw, re.DOTALL)
    if not json_match:
        return None
    json_str = json_match.group()
    json_str = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        try:
            json_str = re.sub(r',\s*}', '}', json_str)
            json_str = re.sub(r',\s*]', ']', json_str)
            return json.loads(json_str)
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TRADE ANALYSER
# ─────────────────────────────────────────────────────────────────────────────

def analyze_trade(trade: dict, sb,
                   provider_override: str | None = None) -> dict | None:
    """
    Full analysis pipeline for a single closed trade.

    1. Load signal_log context at signal_date (full 80-field row)
    2. Load msl_history trajectory entry → exit
    3. Grade the trade entry quality (A-F)
    4. Run rule-based lesson generation (v2: 20+ rules with signal context)
    5. Optionally enrich with AI (v2: richer prompt with all context)
    6. v3: Update existing lesson confidence via the lesson loop
    7. Return complete lesson dict ready for lessons table insert
    """
    sym         = trade.get("symbol", "")
    signal_date = str(trade.get("signal_date") or trade.get("entry_date", ""))
    entry_date  = str(trade.get("entry_date", ""))
    exit_date   = str(trade.get("exit_date", ""))
    pnl_pct     = float(trade.get("pnl_pct", 0) or 0)
    is_win      = pnl_pct > 0

    active_provider = provider_override if provider_override else cfg("ai_provider", "disabled").lower()

    # ── Step 1: pull signal context at entry ─────────────────────────────────
    signal_ctx = load_signal_context(sb, sym, signal_date)
    if not signal_ctx and signal_date != entry_date:
        signal_ctx = load_signal_context(sb, sym, entry_date)

    # ── Step 2: pull msl_history trajectory ──────────────────────────────────
    trajectory_raw = load_msl_trajectory(sb, sym, entry_date, exit_date)
    traj           = analyse_trajectory(trajectory_raw)

    # ── Step 3: grade the entry ───────────────────────────────────────────────
    trade_grade = grade_trade_entry(signal_ctx, trade)

    # ── Step 4: rule-based lesson ─────────────────────────────────────────────
    lesson_data = generate_rule_based_lesson(trade, signal_ctx, traj, trade_grade)
    source      = "RULE_BASED"

    # ── Step 5: AI enrichment (v2 prompt) ────────────────────────────────────
    if active_provider not in ("disabled", "ml"):
        # Load supporting context for AI prompt
        entry_ctx    = load_regime_at_entry(sb, entry_date)
        events_entry = load_events_at_entry(sb, sym, entry_date)
        existing_lsn = load_existing_lessons(
            sb,
            trade.get("strategy", ""),
            trade.get("sector", ""),
            lesson_data.get("scenario_type", ""),
        )
        existing_summary = _format_existing_lessons_summary(existing_lsn)

        prompt = ANALYSIS_PROMPT_V2.format(
            symbol                = sym,
            company_name          = trade.get("company_name", ""),
            sector                = trade.get("sector", ""),
            strategy              = trade.get("strategy", ""),
            entry_date            = entry_date,
            entry_price           = trade.get("entry_price", 0),
            exit_date             = exit_date,
            exit_price            = trade.get("exit_price", 0),
            actual_qty            = trade.get("actual_qty", 0),
            invested_value        = float(trade.get("invested_value", 0) or 0),
            realized_pnl          = float(trade.get("realized_pnl", 0) or 0),
            pnl_pct               = pnl_pct,
            exit_reason           = trade.get("exit_reason", "Unknown"),
            lifecycle_at_entry    = trade.get("lifecycle_at_entry", "Unknown"),
            expected_r            = trade.get("expected_r_at_entry", "N/A"),
            high_water_mark       = trade.get("high_water_mark", 0),
            mfe                   = trade.get("max_favorable_excursion", "N/A"),
            entry_timing_type     = signal_ctx.get("entry_timing_type", "N/A"),
            reentry_mode          = signal_ctx.get("reentry_mode", "N/A"),
            # Signal quality fields
            signal_subtype        = signal_ctx.get("signal_type", "N/A"),
            score_adjusted        = signal_ctx.get("score_adjusted", "N/A"),
            trade_grade           = trade_grade,
            struct_edge           = signal_ctx.get("struct_edge", "N/A"),
            risk_score            = signal_ctx.get("risk_score", "N/A"),
            momentum_state        = signal_ctx.get("momentum_state", "N/A"),
            velocity_state        = signal_ctx.get("velocity_state", "N/A"),
            momentum_phase        = signal_ctx.get("momentum_phase", "N/A"),
            trend_maturity        = signal_ctx.get("trend_maturity", "N/A"),
            breakout_readiness    = signal_ctx.get("breakout_readiness", "N/A"),
            bb_squeeze            = signal_ctx.get("bb_squeeze", "N/A"),
            holding_score         = signal_ctx.get("holding_score", "N/A"),
            institutional_score   = signal_ctx.get("institutional_score", "N/A"),
            rsi_daily             = signal_ctx.get("rsi_daily", "N/A"),
            vol_ratio             = signal_ctx.get("vol_ratio", "N/A"),
            delivery_pct          = signal_ctx.get("delivery_pct", "N/A"),
            vwap_alignment        = signal_ctx.get("vwap_alignment", "N/A"),
            weekly_structure      = signal_ctx.get("weekly_structure", "N/A"),
            psar_dual_confirmed   = signal_ctx.get("psar_dual_confirmed", "N/A"),
            macd_direction        = signal_ctx.get("macd_direction", "N/A"),
            # Market context at entry
            regime                = entry_ctx.get("regime", signal_ctx.get("regime", "N/A")),
            india_vix             = entry_ctx.get("india_vix", signal_ctx.get("india_vix", "N/A")),
            nifty_5d_chg_pct      = entry_ctx.get("nifty_5d_chg_pct", signal_ctx.get("nifty_5d_chg_pct", "N/A")),
            above_200dma_pct      = entry_ctx.get("above_200dma_pct", signal_ctx.get("above_200dma_pct", "N/A")),
            fii_flag              = signal_ctx.get("fii_flag", "N/A"),
            fii_net_20d_ctx       = signal_ctx.get("fii_net_20d_ctx", "N/A"),
            sector_rank_at_entry  = signal_ctx.get("sector_rank_at_entry", "N/A"),
            industry_state        = signal_ctx.get("industry_state", "N/A"),
            # Trajectory
            days_held             = traj.get("days_held", 0),
            lifecycle_sequence    = traj.get("lifecycle_sequence", "N/A"),
            velocity_sequence     = traj.get("velocity_sequence", "N/A"),
            holding_score_min     = traj.get("holding_score_min", "N/A"),
            trend_maturity_end    = traj.get("trend_maturity_end", "N/A"),
            trajectory_warning    = traj.get("trajectory_warning", "NONE"),
            events_at_entry       = events_entry,
            existing_lessons_summary = existing_summary,
        )

        try:
            raw = _call_provider(active_provider, AI_KEYS, prompt)
            if raw:
                parsed = _safe_parse_ai_json(raw)
                if parsed:
                    lesson_data = parsed
                    source = f"AI:{active_provider}"
        except Exception as e:
            logger.warning(f"AI enrichment skipped for {sym} — using rule-based: {e}")

    # ── Confidence calibration ────────────────────────────────────────────────
    confidence = compute_lesson_confidence(trade_grade, is_win, source, traj)

    # ── v3: Update existing lesson confidence via outcomes ────────────────────
    if cfg("lesson_loop_enabled", "true").lower() == "true":
        try:
            applicable = _find_applicable_lessons(sb, trade, signal_ctx, lesson_data)
            if applicable:
                _update_lesson_outcomes(sb, applicable, trade, signal_ctx, traj)
        except Exception as _le:
            logger.debug(f"Lesson loop non-fatal: {_le}")

    # ── Classify linked_event_type ────────────────────────────────────────────
    trigger = str(lesson_data.get("trigger_event", ""))
    if "result" in trigger.lower() or "earnings" in trigger.lower():
        linked_type = "RESULTS"
    elif "stop" in trigger.lower():
        linked_type = "STOP_LOSS"
    elif "regime" in trigger.lower():
        linked_type = "REGIME_CHANGE"
    elif "lifecycle" in trigger.lower() or "trajectory" in trigger.lower():
        linked_type = "MSL_LIFECYCLE_EXIT"
    else:
        linked_type = "TECHNICAL"

    # ── Scenario context (rich: includes grade and signal subtype) ─────────────
    scenario_ctx = (
        f"{sym} {trade.get('strategy')} grade={trade_grade} "
        f"subtype={signal_ctx.get('signal_type', 'UNKNOWN')} "
        f"| P&L: {pnl_pct:.1f}%"
    )

    return {
        "date":              exit_date or str(today_ist().isoformat()),
        "scenario_type":     lesson_data.get("scenario_type", ""),
        "trigger_event":     lesson_data.get("trigger_event", ""),
        "linked_event_type": linked_type,
        "impacted_sector":   trade.get("sector", ""),
        "scenario_context":  scenario_ctx,
        "what_expected":     lesson_data.get("what_expected", ""),
        "what_happened":     lesson_data.get("what_happened", ""),
        "what_failed":       lesson_data.get("what_failed", ""),
        "root_cause":        lesson_data.get("root_cause", ""),
        "corrective_rule":   lesson_data.get("corrective_rule", ""),
        "source":            source,
        "is_active":         True,
        "times_applied":     0,
        "times_worked":      0,
        "confidence":        confidence,
        "linked_symbols":    [sym],
        # Extra context stored in scenario_context for downstream filtering
        # Not a DB column — used only for lesson dedup logic above
        "_trade_grade":      trade_grade,          # ephemeral, stripped before insert
        "_signal_ctx":       signal_ctx,            # ephemeral, stripped before insert
        "_traj":             traj,                  # ephemeral, stripped before insert
    }


# ─────────────────────────────────────────────────────────────────────────────
# LESSONS DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def handle_lesson_dedup(sb, lesson: dict, existing: list[dict]) -> bool:
    """
    If an active lesson with the same corrective_rule core already exists
    (same strategy+sector+scenario_type within DEDUP_WINDOW_DAYS), increment
    times_applied instead of inserting a duplicate.

    Returns True if deduped (caller should skip insert), False if new.
    """
    if not existing:
        return False

    new_rule   = (lesson.get("corrective_rule") or "")[:60].lower()
    for ex in existing:
        ex_rule = (ex.get("corrective_rule") or "")[:60].lower()
        # Simple token overlap check — if 3+ words match it's likely a duplicate
        new_words = set(new_rule.split())
        ex_words  = set(ex_rule.split())
        overlap   = len(new_words & ex_words)
        if overlap >= 3:
            try:
                sb.table("lessons").update({
                    "times_applied": (ex.get("times_applied") or 0) + 1,
                    "confidence":    min(float(ex.get("confidence") or 1.0) + 0.02, 0.99),
                }).eq("id", ex["id"]).execute()
                logger.info(
                    f"Dedup: lesson id={ex['id']} times_applied++ "
                    f"(overlap={overlap} words with new corrective_rule)"
                )
            except Exception as e:
                logger.warning(f"Dedup update failed: {e}")
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL_LOG OUTCOME ENRICHMENT (v2: writes ai_note + execution_status)
# ─────────────────────────────────────────────────────────────────────────────

def enrich_signal_log(sb, trade: dict, lesson: dict):
    """
    SWING ONLY. signal_log is the evening pipeline's output.

    An intraday trade has no row here — it comes from intraday_setups, and its
    outcome is scored by intraday.outcomes.resolve_day. Writing one into
    signal_log does not fail; it silently lands on whatever SWING signal shares
    the symbol and date, and stamps a four-hour paper result onto a 1-3 week
    plan that had nothing to do with it.

    That happened. On 2026-08-01 the 2026-07-30 LALPATHLAB and ATHERENERG swing
    signals were marked LOSS from intraday paper trades, and ATHERENERG had two
    intraday closes that day so the second overwrote the first — the recorded
    outcome was not even the right intraday trade. signal_log.outcome is what
    the swing learning loop reads, so this poisons the evidence with results
    from a different framework and a different horizon.

    Same class as the exit policy being applied across frameworks and the health
    check counting intraday positions against swing signals. The two books stay
    separate here for the same reason they do everywhere else.
    """
    fw = (trade.get("framework") or "SWING").upper()
    if fw != "SWING":
        logger.debug(f"  {trade.get('symbol')}: {fw} trade — scored in "
                     f"intraday_setups, not signal_log")
        return None

    # Writes outcome, outcome_pnl_pct, execution_status and a one-line ai_note.
    sym         = trade.get("symbol", "")
    pnl         = float(trade.get("pnl_pct", 0) or 0)
    outcome     = "WIN" if pnl > 0 else "LOSS"
    signal_date = str(trade.get("signal_date") or trade.get("entry_date", ""))

    # Build concise ai_note (≤ 100 chars) from lesson
    root = (lesson.get("root_cause") or "")
    rule = (lesson.get("corrective_rule") or "")
    ai_note = f"[{lesson.get('scenario_type', '')}] {root[:70]}" if root else rule[:90]

    update_payload = {
        "outcome":          outcome,
        "outcome_pnl_pct":  pnl,
        "execution_status": "COMPLETED",
        "ai_note":          ai_note[:200],
    }

    def _write(payload: dict, date_value: str):
        return (sb.table("signal_log").update(payload)
                  .eq("symbol", sym).eq("date", date_value).execute())

    def _drop_unknown(payload: dict, err: str) -> dict | None:
        """
        Strip the one column PostgREST just rejected and hand back the rest.

        PostgREST fails the WHOLE update when any column is unknown, so a single
        missing column loses every field in the payload. On 2026-07-31 that meant
        "0/16 signal_log enriched" and no outcome, no pnl and no ai_note reached
        the table — because execution_status did not exist.

        Migration 029 adds the three missing columns. Until it is applied, and on
        any future schema drift, writing the fields that DO exist is strictly
        better than writing none: the learning loop reads outcome, and losing it
        to keep an all-or-nothing write is the wrong trade.
        """
        import re
        m = re.search(r"'([a-z_]+)' column", err or "")
        if not m or m.group(1) not in payload:
            return None
        col = m.group(1)
        logger.warning(f"  signal_log has no '{col}' column — apply migration 029. "
                       f"Writing the remaining fields rather than none.")
        return {k: v for k, v in payload.items() if k != col}

    try:
        try:
            response = _write(update_payload, signal_date)
        except Exception as e:
            reduced = update_payload
            # At most one attempt per column, so a schema missing all three
            # still resolves rather than looping.
            for _ in range(len(update_payload)):
                reduced = _drop_unknown(reduced, str(e))
                if not reduced:
                    raise
                try:
                    response = _write(reduced, signal_date)
                    update_payload = reduced
                    break
                except Exception as e2:
                    e = e2
            else:
                raise
        if response.data:
            logger.debug(f"signal_log enriched for {sym}@{signal_date}: {outcome}")
            return True
        else:
            # Try entry_date if signal_date had no match
            response2 = (sb.table("signal_log")
                           .update(update_payload)
                           .eq("symbol", sym)
                           .eq("date", str(trade.get("entry_date", "")))
                           .execute())
            return bool(response2.data)
    except Exception as e:
        logger.warning(f"signal_log enrichment failed for {sym}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — post_trade_analysis skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb     = get_supabase()
    cutoff = (today_ist() - timedelta(days=7)).isoformat()

    trades = (sb.table("closed_positions")
                .select("*")
                .gte("exit_date", cutoff)
                .execute().data)

    if not trades:
        logger.info("No recent closed trades to analyze")
        return {"status": "ok", "analyzed": 0}

    # ── Existing lesson dedup check (by scenario_context prefix) ─────────────
    existing_sc = fetch_all(lambda: sb.table("lessons")
                     .select("scenario_context")
                     .gte("date", cutoff))
    analyzed_prefixes = {(r.get("scenario_context") or "")[:20] for r in existing_sc}

    # ── AI budget gating (v1 logic retained) ─────────────────────────────────
    max_stocks    = cfg_int("ai_max_stocks_per_day", 20)
    daily_budget  = cfg_float("ai_daily_budget_inr", 200.0)
    today_str     = str(today_ist())
    todays_ai     = fetch_all(lambda: sb.table("lessons")
                       .select("id")
                       .like("source", "AI:%")
                       .gte("date", today_str))
    ai_count      = len(todays_ai)
    active_prov   = cfg("ai_provider", "disabled").lower()
    cost_per_call = PROVIDER_COST_INR.get(active_prov, 0.30)
    est_spend     = ai_count * cost_per_call
    provider_eff  = "disabled" if (est_spend >= daily_budget or ai_count >= max_stocks) else None

    # ── Process trades ────────────────────────────────────────────────────────
    analyzed_count      = 0
    signal_match_count  = 0
    dedup_count         = 0
    grade_dist          = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    ai_count_this_run   = 0

    for trade in trades:
        sym = trade.get("symbol", "")

        # Skip already-analyzed (by scenario_context prefix)
        ctx_prefix = f"{sym} {trade.get('strategy')}"[:20]
        if ctx_prefix in analyzed_prefixes:
            logger.debug(f"Skipping {sym} — lesson already exists this window")
            continue

        lesson = analyze_trade(trade, sb, provider_override=provider_eff)
        if not lesson:
            continue

        # Track grade (pop ephemeral keys before any writes)
        grade      = lesson.pop("_trade_grade", "C")
        signal_ctx = lesson.pop("_signal_ctx", {})
        traj       = lesson.pop("_traj", {})
        grade_dist[grade] = grade_dist.get(grade, 0) + 1

        # PERSIST THE GRADE — F-68 follow-up, 24-Aug-2026. grade_trade_entry()
        # has computed this since this file existed; it was used only to
        # word the lesson's prose and tallied into this run's own grade_dist
        # log line, then discarded — Stage E2's own quantify pass went
        # looking for whether the grade predicts forward outcome and found
        # the question unanswerable for exactly that reason. Migration 093.
        # SWING only, a deliberate narrowing of this loop's own scope (it
        # does not itself discriminate by framework) — see that migration's
        # own note for why.
        if (trade.get("framework") or "SWING").upper() == "SWING" and trade.get("id"):
            try:
                sb.table("closed_positions").update(
                    {"entry_grade": grade}).eq("id", trade["id"]).execute()
            except Exception as e:
                logger.debug(f"  {sym}: entry_grade write failed — {e}")

        # Dedup check
        existing_lsn = load_existing_lessons(
            sb,
            trade.get("strategy", ""),
            trade.get("sector", ""),
            lesson.get("scenario_type", ""),
        )
        if handle_lesson_dedup(sb, lesson, existing_lsn):
            dedup_count += 1
            if enrich_signal_log(sb, trade, lesson):
                signal_match_count += 1
            # v3: still update lesson confidence even on dedup
            if cfg("lesson_loop_enabled", "true").lower() == "true":
                try:
                    applicable = _find_applicable_lessons(sb, trade, signal_ctx, lesson)
                    if applicable:
                        _update_lesson_outcomes(sb, applicable, trade, signal_ctx, traj)
                except Exception as _le:
                    logger.debug(f"Lesson loop (dedup path) non-fatal: {_le}")
            continue

        # Insert new lesson
        sb.table("lessons").insert(lesson).execute()
        analyzed_count += 1
        if lesson.get("source", "").startswith("AI:"):
            ai_count_this_run += 1

        logger.info(
            f"  Lesson [{grade}] {sym}: {lesson.get('scenario_type')} | "
            f"{lesson.get('source')} | conf={lesson.get('confidence')}"
        )

        # Enrich signal_log (v2: outcome + ai_note + execution_status)
        if enrich_signal_log(sb, trade, lesson):
            signal_match_count += 1

    # Denominator is the SWING trades, not every trade. Reporting against all
    # of them read as "5/16 enriched" — a permanent-looking failure — when
    # eleven of the sixteen were intraday and have no signal_log row to reach
    # by construction. A ratio that cannot reach 1 teaches you to ignore it.
    eligible = len([t for t in trades
                    if (t.get("framework") or "SWING").upper() == "SWING"])
    skipped = len(trades) - eligible
    logger.success(
        f"Post-trade analysis v3 done: "
        f"{analyzed_count} new lessons | "
        f"{dedup_count} deduped (times_applied++) | "
        f"{signal_match_count}/{eligible} swing signal_log enriched"
        + (f" ({skipped} intraday scored in intraday_setups)" if skipped else "")
        + f" | AI calls this run: {ai_count_this_run}"
    )
    logger.info(f"  Trade grade distribution: {grade_dist}")

    return {
        "status":         "ok",
        "analyzed":       analyzed_count,
        "deduped":        dedup_count,
        "signal_matches": signal_match_count,
        "grade_dist":     grade_dist,
        "ai_calls":       ai_count_this_run,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-grades", action="store_true",
                    help="Populate entry_grade for existing closed SWING "
                         "trades that predate migration 093, then exit.")
    ap.add_argument("--since", default=None,
                    help="With --backfill-grades, only trades exiting on "
                         "or after this date (YYYY-MM-DD).")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.backfill_grades:
        backfill_entry_grades(get_supabase(), since=a.since, dry_run=a.dry_run)
    else:
        main()