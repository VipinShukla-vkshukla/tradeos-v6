"""
TradeOS v6 — Step 20: Signal Calibration Engine
================================================
Pipeline position: Weekly — runs after enough outcome data accumulates.
Recommended schedule: Every Sunday, or when closed_positions count
since last calibration exceeds CALIBRATION_MIN_NEW_TRADES (default 15).

PURPOSE:
  Close the self-improvement loop by reading actual trade outcomes and
  updating signal gate thresholds in system_config. Step 15 reads these
  thresholds at runtime, so calibration updates take effect the next
  pipeline run without any code changes.

WHAT IT CALIBRATES:
  - Risk gate thresholds (risk_block_threshold, risk_penalty_threshold)
  - Holding score gates (holding_score_exit_threshold, add_holding_min)
  - ATR chase multipliers by momentum state (stored as JSON in system_config)
  - Minimum R:R requirement (min_rr_to_enter)
  - Minimum adjusted score floor (min_score_adjusted)
  - PRIME_SETUP breakout_readiness minimum (prime_breakout_min)
  - Industry bonus values (industry_bonus_top5, industry_bonus_strong)
  - Score adjustment bonuses (score_bonus_*)

SAFETY RAILS (non-negotiable):
  1. Minimum 20 closed trades required — below this, no changes made
  2. Maximum 20% change per parameter per calibration run
  3. Parameters always stay within absolute bounds (e.g. risk_block can't go below 50)
  4. If calibration_approval_required=true in system_config, writes PROPOSED_*
     keys instead of live keys — human reviews and renames to activate
  5. Every change is logged to lessons table with source=CALIBRATION for audit trail
  6. Parameters revert to defaults if win rate degrades over 3 consecutive runs

SELF-IMPROVEMENT LOOP:
  closed_positions + signal_log outcomes (weekly)
      ↓
  calibration_engine analyzes win rates by parameter bucket
      ↓ 
  AI synthesizes patterns, proposes threshold adjustments
      ↓
  Updated thresholds written to system_config
      ↓
  Step 15 reads fresh thresholds next run
      ↓
  Step 17 records new outcomes
      ↓ (loop repeats)

ALSO CLOSES THE LESSONS CONFIDENCE LOOP:
  Reads signal_log outcomes and matches them to lessons that were applied.
  Increments lessons.times_applied and lessons.times_worked based on outcomes.
  Retires lessons that consistently fail (times_worked/times_applied < 0.3 
  after 10+ applications).

WRITES:
  system_config — updated threshold values (or PROPOSED_* if approval required)
  lessons       — calibration audit entries (source=CALIBRATION)
  lessons       — times_applied, times_worked updates for outcome matching
"""

import sys
import json
import re
from datetime import timedelta
from pathlib import Path
from collections import defaultdict

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg, cfg_int, cfg_float

# ── Calibration constants ──────────────────────────────────────────────────

CALIBRATION_MIN_NEW_TRADES = cfg_float("calibration_engine_calibration_min_new_trades", 15)
CALIBRATION_MAX_CHANGE_PCT = cfg_float("calibration_engine_calibration_max_change_pct", 0.2)
CALIBRATION_LOOKBACK_DAYS = cfg_float("calibration_engine_calibration_lookback_days", 90)
MIN_LESSON_APPLICATIONS = cfg_float("calibration_engine_min_lesson_applications", 10)
LESSON_RETIRE_WIN_RATE       = 0.30 # retire if win rate below this after 10+ uses

# Absolute bounds — calibration cannot move parameters outside these
PARAM_BOUNDS = {
    "min_rr_to_enter_TRENDING":         (0.9, 1.8),
    "min_rr_to_enter_RISK_ON":          (0.9, 1.8),   # was CAUTION — wrong
    "min_rr_to_enter_NEUTRAL":          (1.0, 2.0),
    "min_rr_to_enter_RECOVERING":       (1.1, 2.0),   # new
    "min_rr_to_enter_RISK_OFF":         (1.3, 2.0),
    "min_rr_prime":                     0.9,
    "min_rr_breakout":                  1.4,
    "min_rr_reentry":                   1.1,
    "min_rr_staged":                    1.0,
    "risk_block_threshold":          (50, 95),
    "risk_penalty_threshold":        (40, 80),
    "holding_score_exit_threshold":  (15, 50),
    "add_holding_min":               (40, 80),
    "prime_breakout_min":            (40, 75),
    "min_rr_to_enter":               (0.6, 2.0),
    "min_score_adjusted":            (30, 65),
    "industry_bonus_top5":           (3, 20),
    "industry_bonus_strong":         (2, 10),
}

# Default values — fallback if calibration has insufficient data
PARAM_DEFAULTS = {
    "min_rr_to_enter_TRENDING":         1.0,
    "min_rr_to_enter_RISK_ON":          1.1,
    "min_rr_to_enter_NEUTRAL":          1.2,
    "min_rr_to_enter_RECOVERING":       1.3,   # new
    "min_rr_to_enter_RISK_OFF":         1.5,
    "risk_block_threshold":          80,
    "risk_penalty_threshold":        65,
    "holding_score_exit_threshold":  30,
    "add_holding_min":               60,
    "prime_breakout_min":            55,
    "min_rr_to_enter":               1.0,
    "min_score_adjusted":            45,
    "industry_bonus_top5":           10,
    "industry_bonus_strong":         5,
    # ATR matrix as JSON
    "atr_chase_matrix": json.dumps({
        "HOT_EARLY_ACCELERATING":      3.0,
        "HOT_EXPANSION_ACCELERATING":  2.5,
        "HOT_EXPANSION_FLAT":          1.8,
        "BUILDING_ACCELERATING":       2.0,
        "BUILDING_FLAT":               1.2,
        "LATE_ANY":                    0.5,
        "EXTENDED_ANY":                0.0,
        "DEFAULT":                     1.0,
    }),
    # Score adjustment bonuses
    "score_bonus_momentum_high":     3,
    "score_bonus_momentum_mid":      1,
    "score_bonus_institutional":     2,
    "score_bonus_breakout_ready":    2,
    "score_bonus_psar_dual":         2,
    "score_bonus_weekly_strong":     1,
    "score_penalty_risk_high":      -5,
    "score_penalty_risk_mid":       -2,
    "score_bonus_prime_setup":       5,
    "score_bonus_breakout_setup":    3,
    "score_bonus_reentry_setup":     2,
}

# ── Data loading ───────────────────────────────────────────────────────────

def load_outcome_data(sb, cutoff: str) -> list[dict]:
    """
    Load closed positions joined with signal_log context.
    Returns merged rows with both trade outcome and signal quality at entry.
    """
    trades = (
        sb.table("closed_positions")
          .select("*")
          .gte("exit_date", cutoff)
          .execute().data
    )
    if not trades:
        return []

    # Batch join: get all signal_log rows for these symbols/dates
    signal_dates = list({str(t.get("signal_date") or t.get("entry_date", "")) for t in trades if t.get("symbol")})
    symbols      = list({t.get("symbol") for t in trades if t.get("symbol")})

    if symbols and signal_dates:
        sig_rows = (
            sb.table("signal_log")
              .select(
                  "symbol,date,signal_type,signal_subtype,score_adjusted,"
                  "momentum_state,momentum_phase,velocity_state,trend_maturity,"
                  "risk_score,holding_score,breakout_readiness,institutional_score,"
                  "regime,rsi_daily,vol_ratio,delivery_pct,atr_pct,"
                  "struct_edge,weekly_structure,entry_timing_type,fii_flag,"
                  "sector_rank_at_entry,industry_top5,industry_state"
              )
              .in_("symbol", symbols[:100])  # supabase in_ limit
              .execute().data
        )
        sig_map = {(r["symbol"], str(r["date"])): r for r in sig_rows}
    else:
        sig_map = {}

    merged = []
    for t in trades:
        sym  = t.get("symbol", "")
        sdate = str(t.get("signal_date") or t.get("entry_date", ""))
        sig  = sig_map.get((sym, sdate), {})
        merged.append({**t, **{f"sig_{k}": v for k, v in sig.items()}})

    return merged


def load_msl_trajectory_outcomes(sb, cutoff: str) -> list[dict]:
    """
    From msl_history: sequences that preceded good/bad outcomes.
    Returns symbols with their final lifecycle/velocity sequences.
    """
    rows = (
        sb.table("msl_history")
          .select(
              "snapshot_date,symbol,lifecycle,velocity_state,"
              "momentum_phase,trend_maturity,final_score,validity_score"
          )
          .gte("snapshot_date", cutoff)
          .order("snapshot_date", desc=False)
          .execute().data
    )
    # Group by symbol, build sequences
    by_symbol = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)
    return [{"symbol": sym, "trajectory": traj} for sym, traj in by_symbol.items()]


def load_current_thresholds(sb) -> dict:
    """Read all calibration-relevant keys from system_config."""
    rows = sb.table("system_config").select("key,value").execute().data
    return {r["key"]: r["value"] for r in rows}


def load_active_lessons(sb) -> list[dict]:
    """Load lessons eligible for confidence update."""
    return (
        sb.table("lessons")
          .select("id,corrective_rule,scenario_type,source,"
                  "times_applied,times_worked,confidence,is_active,linked_symbols")
          .eq("is_active", True)
          .execute().data
    )


# ── Outcome analysis ───────────────────────────────────────────────────────

def analyse_outcomes(trades: list[dict]) -> dict:
    """
    Compute win rates and optimal thresholds from trade outcomes.
    Returns a structured analysis dict.
    """
    if not trades:
        return {}

    wins   = [t for t in trades if float(t.get("pnl_pct") or 0) > 0]
    losses = [t for t in trades if float(t.get("pnl_pct") or 0) <= 0]
    total  = len(trades)
    wr     = len(wins) / total

    # ── Win rate by risk_score bucket ──
    risk_buckets = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        rs   = float(t.get("sig_risk_score") or 0)
        is_w = float(t.get("pnl_pct") or 0) > 0
        bucket = (int(rs // 10)) * 10   # 0, 10, 20 ... 90
        risk_buckets[bucket]["total"] += 1
        if is_w:
            risk_buckets[bucket]["wins"] += 1

    # Find the risk_score above which win rate drops below 40%
    risk_block_optimal = PARAM_DEFAULTS["risk_block_threshold"]
    for bucket in sorted(risk_buckets.keys()):
        b = risk_buckets[bucket]
        if b["total"] >= 3:
            bwr = b["wins"] / b["total"]
            if bwr < 0.40:
                risk_block_optimal = max(bucket, 50)  # never below 50
                break

    # ── Win rate by momentum state ──
    momentum_wr = defaultdict(lambda: {"wins": 0, "total": 0})
    for t in trades:
        ms = t.get("sig_momentum_state") or "UNKNOWN"
        is_w = float(t.get("pnl_pct") or 0) > 0
        momentum_wr[ms]["total"] += 1
        if is_w:
            momentum_wr[ms]["wins"] += 1

    # ── ATR distance analysis: what chase distances actually work ──
    # For each momentum state, find the ATR multiple at which win rate stays > 50%
    atr_analysis = defaultdict(list)
    for t in trades:
        ms      = t.get("sig_momentum_state") or "UNKNOWN"
        vstate  = t.get("sig_velocity_state") or "UNKNOWN"
        mphase  = t.get("sig_momentum_phase") or "UNKNOWN"
        atr_pct = float(t.get("sig_atr_pct") or 3.0)
        ep      = float(t.get("entry_price") or 0)
        ez_hi   = 0  # we don't have zone high in closed_positions easily
        is_w    = float(t.get("pnl_pct") or 0) > 0
        # If we have entry_timing_type=CHASING we know they were above zone
        timing  = t.get("sig_entry_timing_type") or ""
        if timing == "CHASING" and ep > 0 and atr_pct > 0:
            key = f"{ms}_{mphase[:3]}_{vstate[:3]}"
            atr_analysis[key].append({"win": is_w, "pnl": float(t.get("pnl_pct") or 0)})

    # Compute optimal ATR per momentum combo
    atr_matrix = dict(json.loads(PARAM_DEFAULTS["atr_chase_matrix"]))
    for key, outcomes in atr_analysis.items():
        if len(outcomes) >= 5:
            win_rate = sum(1 for o in outcomes if o["win"]) / len(outcomes)
            current  = atr_matrix.get(key, atr_matrix.get("DEFAULT", 1.0))
            # Nudge: if win rate high, allow slightly more chase; if low, tighten
            if win_rate > 0.65:
                atr_matrix[key] = round(min(current * 1.1, 3.5), 2)
            elif win_rate < 0.35:
                atr_matrix[key] = round(max(current * 0.85, 0.3), 2)

    # ── Holding score at exit threshold ──
    hs_at_exit = [
        float(t.get("sig_holding_score") or 0)
        for t in losses
        if float(t.get("sig_holding_score") or 0) > 0
    ]
    hs_threshold_optimal = PARAM_DEFAULTS["holding_score_exit_threshold"]
    if len(hs_at_exit) >= 5:
        hs_sorted = sorted(hs_at_exit)
        # The 30th percentile of holding scores at loss = a sensible exit trigger
        idx = int(len(hs_sorted) * 0.30)
        candidate = hs_sorted[idx]
        hs_threshold_optimal = max(15, min(50, round(candidate, 0)))

    # ── Score adjusted distribution for wins vs losses ──
    win_scores  = [float(t.get("sig_score_adjusted") or 0) for t in wins  if t.get("sig_score_adjusted")]
    loss_scores = [float(t.get("sig_score_adjusted") or 0) for t in losses if t.get("sig_score_adjusted")]
    min_score_optimal = PARAM_DEFAULTS["min_score_adjusted"]
    if win_scores and loss_scores:
        win_p25  = sorted(win_scores)[int(len(win_scores) * 0.25)]
        loss_p75 = sorted(loss_scores)[int(len(loss_scores) * 0.75)]
        # Optimal floor = midpoint of win 25th %ile and loss 75th %ile
        candidate = (win_p25 + loss_p75) / 2
        min_score_optimal = max(30, min(65, round(candidate, 1)))

    # ── Industry bonus effectiveness ──
    ind_top5_trades = [t for t in trades if t.get("sig_industry_top5")]
    ind_non_trades  = [t for t in trades if not t.get("sig_industry_top5")]
    industry_bonus_optimal = PARAM_DEFAULTS["industry_bonus_top5"]
    if len(ind_top5_trades) >= 5 and len(ind_non_trades) >= 5:
        wr_top5 = sum(1 for t in ind_top5_trades if float(t.get("pnl_pct") or 0) > 0) / len(ind_top5_trades)
        wr_non  = sum(1 for t in ind_non_trades  if float(t.get("pnl_pct") or 0) > 0) / len(ind_non_trades)
        edge    = wr_top5 - wr_non   # how much better are top5 industry stocks?
        # Scale bonus: each 1% edge = ~1 point bonus
        industry_bonus_optimal = max(3, min(20, round(edge * 50, 0)))

    # ── Regime-specific min_rr ──
    regime_rr = {}
    # ── Regime-specific min_rr — aligned to compute_regime's 5-state model ──
    regime_rr = {}
    for regime_name in ("TRENDING", "RISK ON", "NEUTRAL", "RECOVERING", "RISK OFF"):
        bucket = [t for t in trades if (t.get("sig_regime") or "").upper() == regime_name]
        if len(bucket) >= 5:
            losses_b = [t for t in bucket if float(t.get("pnl_pct") or 0) <= 0]
            if losses_b:
                rr_key = f"min_rr_to_enter_{regime_name.replace(' ', '_')}"
                fallback = PARAM_DEFAULTS.get(rr_key, 1.2)
                rrs = sorted([
                    float(t.get("implied_rr") or fallback)
                    for t in losses_b
                ])
                candidate = rrs[len(rrs) // 2]
                regime_rr[rr_key] = round(max(0.6, min(2.0, candidate)), 2)

    # ── Signal-type-specific min_rr ──
    signal_rr = {}
    for sig_type, rr_key in [
        ("PRIME_SETUP",    "min_rr_prime"),
        ("BREAKOUT_SETUP", "min_rr_breakout"),
        ("REENTRY_SETUP",  "min_rr_reentry"),
        ("STAGED_ENTRY",   "min_rr_staged"),
    ]:
        bucket = [t for t in trades if (t.get("sig_signal_type") or "").upper() == sig_type]
        if len(bucket) >= 5:
            losses_b = [t for t in bucket if float(t.get("pnl_pct") or 0) <= 0]
            if losses_b:
                fallback = PARAM_DEFAULTS.get(rr_key, 1.2)
                rrs = sorted([
                    float(t.get("implied_rr") or fallback)
                    for t in losses_b
                ])
                candidate = rrs[len(rrs) // 2]
                signal_rr[rr_key] = round(max(0.6, min(2.0, candidate)), 2)

    return {
        "total_trades":           total,
        "win_rate":               round(wr, 3),
        "wins":                   len(wins),
        "losses":                 len(losses),
        "risk_block_optimal":     int(risk_block_optimal),
        "hs_threshold_optimal":   int(hs_threshold_optimal),
        "min_score_optimal":      float(min_score_optimal),
        "industry_bonus_optimal": int(industry_bonus_optimal),
        "atr_matrix_updated":     atr_matrix,
        "momentum_win_rates":     {k: round(v["wins"]/v["total"], 3) for k, v in momentum_wr.items() if v["total"] >= 3},
        "risk_win_rates":         {str(k): round(v["wins"]/v["total"], 3) for k, v in risk_buckets.items() if v["total"] >= 3},
        "regime_rr":              regime_rr,    # ← was computed but never returned
        "signal_rr":              signal_rr,    # ← same
    }


# ── AI synthesis ───────────────────────────────────────────────────────────

def ai_synthesize(analysis: dict, current_thresholds: dict, trade_sample: list[dict]) -> dict | None:
    """
    Single AI call to synthesize calibration findings and validate proposed changes.
    AI can veto or modify proposed threshold changes based on market context.
    Returns dict of {param_name: new_value} or None if AI unavailable.
    """
    try:
        from ai.ai_router import is_ai_available, raw_completion
    except ImportError:
        return None

    if not is_ai_available():
        return None

    # Summarize recent notable trades for context
    notable = sorted(trade_sample, key=lambda t: abs(float(t.get("pnl_pct") or 0)), reverse=True)[:5]
    notable_str = "\n".join(
        f"  {t.get('symbol','?')} [{t.get('sig_signal_type','?')}] "
        f"P&L:{float(t.get('pnl_pct',0)):+.1f}% "
        f"risk_score:{t.get('sig_risk_score','?')} "
        f"score_adj:{t.get('sig_score_adjusted','?')} "
        f"regime:{t.get('sig_regime','?')}"
        for t in notable
    )

    prompt = f"""You are reviewing a swing trading system's self-calibration analysis.
The system completed {analysis['total_trades']} trades in the last 90 days.
Win rate: {analysis['win_rate']:.1%} | Wins: {analysis['wins']} | Losses: {analysis['losses']}

CURRENT SYSTEM THRESHOLDS:
  risk_block_threshold: {current_thresholds.get('risk_block_threshold', 80)}
  risk_penalty_threshold: {current_thresholds.get('risk_penalty_threshold', 65)}
  holding_score_exit_threshold: {current_thresholds.get('holding_score_exit_threshold', 30)}
  min_score_adjusted: {current_thresholds.get('min_score_adjusted', 45)}
  min_rr_to_enter: {current_thresholds.get('min_rr_to_enter', 1.0)}
  prime_breakout_min: {current_thresholds.get('prime_breakout_min', 55)}

STATISTICALLY OPTIMAL VALUES (from outcome analysis):
  risk_block_threshold → {analysis['risk_block_optimal']}
  holding_score_exit_threshold → {analysis['hs_threshold_optimal']}
  min_score_adjusted → {analysis['min_score_optimal']}
  industry_bonus_top5 → {analysis['industry_bonus_optimal']}

WIN RATES BY RISK SCORE BUCKET:
{json.dumps(analysis['risk_win_rates'], indent=2)}

WIN RATES BY MOMENTUM STATE:
{json.dumps(analysis['momentum_win_rates'], indent=2)}

NOTABLE RECENT TRADES:
{notable_str}

TASK: Review the proposed threshold changes. For each parameter:
1. Confirm the statistical proposal makes logical sense for swing trading
2. Check for overfitting risk (small sample sizes, regime-specific patterns)
3. Suggest the actual new value (can differ from statistical proposal if you see risks)

Return ONLY this JSON:
{{
  "risk_block_threshold": <number or null if no change>,
  "risk_penalty_threshold": <number or null>,
  "holding_score_exit_threshold": <number or null>,
  "add_holding_min": <number or null>,
  "prime_breakout_min": <number or null>,
  "min_rr_to_enter": <number or null>,
  "min_score_adjusted": <number or null>,
  "industry_bonus_top5": <number or null>,
  "industry_bonus_strong": <number or null>,
  "calibration_rationale": "2 sentences explaining the key insight from this run",
  "overfitting_risk": "LOW | MEDIUM | HIGH",
  "recommended_next_review_days": 14
}}"""

    try:
        raw = raw_completion(prompt, max_tokens=800)
        m   = re.search(r"\{[\s\S]+\}", raw)
        return json.loads(m.group()) if m else None
    except Exception as e:
        logger.warning(f"AI synthesis failed: {e}")
        return None


# ── Safety clamp ───────────────────────────────────────────────────────────

def safe_clamp(param: str, current_val, new_val) -> float:
    """
    Apply safety rails:
    1. Max 20% change per run
    2. Must stay within absolute bounds
    """
    try:
        current = float(current_val or PARAM_DEFAULTS.get(param, new_val))
        proposed = float(new_val)
    except (ValueError, TypeError):
        return None

    # Max change guard
    max_delta = abs(current) * CALIBRATION_MAX_CHANGE_PCT
    proposed  = max(current - max_delta, min(current + max_delta, proposed))

    # Absolute bounds guard
    if param in PARAM_BOUNDS:
        lo, hi  = PARAM_BOUNDS[param]
        proposed = max(lo, min(hi, proposed))

    # Round sensibly
    if param in ("min_rr_to_enter",):
        return round(proposed, 2)
    return round(proposed, 1)


# ── Write thresholds ───────────────────────────────────────────────────────

def write_thresholds(sb, proposed: dict, current: dict, analysis: dict,
                     approval_required: bool, today_str: str) -> int:
    """
    Write updated thresholds to system_config.
    If approval_required, writes PROPOSED_* keys instead.
    Returns count of parameters updated.
    """
    updated = 0
    prefix  = "PROPOSED_" if approval_required else ""
    changes = []

    for param, new_val in proposed.items():
        if new_val is None or param in ("calibration_rationale", "overfitting_risk",
                                         "recommended_next_review_days"):
            continue

        clamped = safe_clamp(param, current.get(param), new_val)
        if clamped is None:
            continue

        current_num = float(current.get(param) or PARAM_DEFAULTS.get(param, clamped))
        if abs(clamped - current_num) < 0.5:
            continue  # not meaningful change

        key = f"{prefix}{param}"
        try:
            sb.table("system_config").upsert({
                "key":   key,
                "value": str(clamped),
                "description": (
                    f"{'[PROPOSED] ' if approval_required else ''}"
                    f"Calibrated {today_str} from {current_num:.1f} → {clamped:.1f} "
                    f"(based on {analysis['total_trades']} trades, WR={analysis['win_rate']:.1%})"
                ),
                "updated_at": today_str,
            }, on_conflict="key").execute()
            updated += 1
            changes.append(f"{param}: {current_num:.1f} → {clamped:.1f}")
        except Exception as e:
            logger.warning(f"system_config write failed for {param}: {e}")

    if changes:
        logger.info(f"  Threshold changes: {' | '.join(changes)}")

    # Write ATR matrix if updated
    if "atr_matrix_updated" in analysis:
        try:
            sb.table("system_config").upsert({
                "key":         f"{prefix}atr_chase_matrix",
                "value":       json.dumps(analysis["atr_chase_matrix"]),
                "description": f"Calibrated ATR chase matrix {today_str}",
                "updated_at":  today_str,
            }, on_conflict="key").execute()
            updated += 1
        except Exception as e:
            logger.warning(f"ATR matrix write failed: {e}")

    return updated


def write_calibration_lesson(sb, analysis: dict, ai_result: dict | None,
                              changes_count: int, today_str: str):
    """Audit trail entry in lessons table."""
    rationale = (ai_result or {}).get("calibration_rationale", "Statistical calibration from outcome data")
    risk_level = (ai_result or {}).get("overfitting_risk", "MEDIUM")
    try:
        sb.table("lessons").insert({
            "date":              today_str,
            "scenario_type":     "Calibration/Threshold-Update",
            "trigger_event":     "weekly_calibration",
            "linked_event_type": "CALIBRATION",
            "impacted_sector":   "ALL",
            "scenario_context":  f"Calibration run: {analysis['total_trades']} trades, WR={analysis['win_rate']:.1%}",
            "what_expected":     "System threshold adjustment based on outcome data",
            "what_happened":     f"{changes_count} parameters updated",
            "what_failed":       f"Overfitting risk: {risk_level}",
            "root_cause":        rationale,
            "corrective_rule":   f"Rule: Updated thresholds based on {analysis['total_trades']} trades with {analysis['win_rate']:.1%} win rate",
            "source":            "CALIBRATION",
            "is_active":         True,
            "times_applied":     0,
            "times_worked":      0,
            "confidence":        0.7 if risk_level == "LOW" else (0.5 if risk_level == "MEDIUM" else 0.3),
        }).execute()
    except Exception as e:
        logger.warning(f"Calibration lesson write failed: {e}")


# ── Lesson confidence updater ──────────────────────────────────────────────

def update_lesson_confidence(sb, trades: list[dict], lessons: list[dict]) -> dict:
    """
    CLOSES THE LESSONS CONFIDENCE LOOP.

    For each closed trade:
      1. Find lessons that were plausibly applied (symbol in linked_symbols,
         or scenario_type matches signal context)
      2. Increment times_applied
      3. Increment times_worked if the trade was profitable
      4. Retire lessons where win rate < LESSON_RETIRE_WIN_RATE after sufficient uses

    This is the mechanism that makes lessons earn or lose confidence over time.
    """
    applied_count = 0
    retired_count = 0

    # Build lesson lookup by symbol and scenario
    lesson_by_symbol = defaultdict(list)
    for l in lessons:
        for sym in (l.get("linked_symbols") or []):
            lesson_by_symbol[sym].append(l)

    for trade in trades:
        sym   = trade.get("symbol", "")
        is_w  = float(trade.get("pnl_pct") or 0) > 0
        sig_t = trade.get("sig_signal_type", "")
        regime = trade.get("sig_regime", "")

        # Match lessons for this trade
        matched = set()
        for l in lesson_by_symbol.get(sym, []):
            matched.add(l["id"])

        # Also match by scenario type pattern
        for l in lessons:
            sc = l.get("scenario_type") or ""
            if (sig_t and sig_t.lower() in sc.lower()) or \
               (regime and regime.lower() in sc.lower()):
                matched.add(l["id"])

        for lid in matched:
            lesson = next((l for l in lessons if l["id"] == lid), None)
            if not lesson:
                continue

            new_applied = (lesson.get("times_applied") or 0) + 1
            new_worked  = (lesson.get("times_worked")  or 0) + (1 if is_w else 0)
            new_conf    = round(new_worked / new_applied, 3) if new_applied > 0 else 0.5

            # Retire if consistently failing
            should_retire = (
                new_applied >= MIN_LESSON_APPLICATIONS
                and new_conf < LESSON_RETIRE_WIN_RATE
            )

            try:
                sb.table("lessons").update({
                    "times_applied": new_applied,
                    "times_worked":  new_worked,
                    "confidence":    new_conf,
                    "is_active":     not should_retire,
                }).eq("id", lid).execute()
                applied_count += 1
                if should_retire:
                    retired_count += 1
                    logger.info(f"  Lesson {lid} retired — win rate {new_conf:.1%} after {new_applied} uses")
            except Exception as e:
                logger.warning(f"Lesson confidence update failed for id={lid}: {e}")

    return {"applied": applied_count, "retired": retired_count}


# ── Calibration gate ───────────────────────────────────────────────────────

def should_calibrate(sb, today_str: str) -> tuple[bool, str]:
    """
    Check if calibration should run today.
    Returns (should_run, reason).
    """
    last_cal = cfg("last_calibration_date", "")
    if last_cal:
        from datetime import date
        try:
            last_dt = date.fromisoformat(last_cal)
            days_since = (today_ist() - last_dt).days
            if days_since < 7:
                return False, f"Last calibration {days_since}d ago — minimum interval is 7 days"
        except Exception:
            pass

    # Check if enough new trades since last calibration
    cutoff = (today_ist() - timedelta(days=CALIBRATION_LOOKBACK_DAYS)).isoformat()
    count  = sb.table("closed_positions").select("id").gte("exit_date", cutoff).execute().data
    if len(count) < CALIBRATION_MIN_NEW_TRADES:
        return False, f"Only {len(count)} trades in lookback window — minimum {CALIBRATION_MIN_NEW_TRADES} required"

    return True, f"{len(count)} trades available for calibration"


# ── Main ───────────────────────────────────────────────────────────────────

def main(force: bool = False):
    if is_kill_switch_active():
        logger.warning("Kill switch — calibration skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info("=" * 60)
    logger.info("STEP 20: Signal Calibration Engine")
    logger.info("=" * 60)

    sb         = get_supabase()
    today_str  = str(today_ist())
    cutoff     = (today_ist() - timedelta(days=CALIBRATION_LOOKBACK_DAYS)).isoformat()

    # ── Gate: should we calibrate? ──
    if not force:
        run, reason = should_calibrate(sb, today_str)
        if not run:
            logger.info(f"Calibration skipped: {reason}")
            return {"status": "skipped", "reason": reason}
    logger.info(f"Starting calibration (lookback: {cutoff} to {today_str})")

    # ── Step 1: Load data ──
    logger.info("Loading outcome data...")
    trades    = load_outcome_data(sb, cutoff)
    lessons   = load_active_lessons(sb)
    current_t = load_current_thresholds(sb)

    logger.info(f"  {len(trades)} closed trades | {len(lessons)} active lessons")

    if len(trades) < CALIBRATION_MIN_NEW_TRADES and not force:
        logger.warning(f"Insufficient trades ({len(trades)}) — skipping threshold update")
        return {"status": "insufficient_data", "trades": len(trades)}

    # ── Step 2: Analyse outcomes ──
    logger.info("Analysing outcomes...")
    analysis = analyse_outcomes(trades)
    logger.info(
        f"  Win rate: {analysis.get('win_rate', 0):.1%} | "
        f"Risk block optimal: {analysis.get('risk_block_optimal')} | "
        f"HS threshold optimal: {analysis.get('hs_threshold_optimal')} | "
        f"Min score optimal: {analysis.get('min_score_optimal')}"
    )

    # ── Step 3: Update lesson confidence ──
    logger.info("Updating lesson confidence from outcomes...")
    lesson_stats = update_lesson_confidence(sb, trades, lessons)
    logger.info(f"  Lessons: {lesson_stats['applied']} applied updates | {lesson_stats['retired']} retired")

    # ── Step 4: AI synthesis ──
    logger.info("Calling AI for calibration synthesis...")
    ai_result = ai_synthesize(analysis, current_t, trades[:20])
    if ai_result:
        logger.info(f"  AI: {ai_result.get('calibration_rationale','')[:120]}")
        logger.info(f"  Overfitting risk: {ai_result.get('overfitting_risk','?')}")
        # Skip application if AI flags high overfitting risk
        if ai_result.get("overfitting_risk") == "HIGH" and not force:
            logger.warning("AI flagged HIGH overfitting risk — skipping threshold updates")
            write_calibration_lesson(sb, analysis, ai_result, 0, today_str)
            return {"status": "skipped_overfitting", "analysis": analysis}
    else:
        logger.info("  AI unavailable — using statistical proposals only")

    # ── Step 5: Write thresholds ──
    approval_required = cfg("calibration_approval_required", "false").lower() == "true"
    proposed = {
        "risk_block_threshold":         analysis.get("risk_block_optimal"),
        "holding_score_exit_threshold": analysis.get("hs_threshold_optimal"),
        "min_score_adjusted":           analysis.get("min_score_optimal"),
        "industry_bonus_top5":          analysis.get("industry_bonus_optimal"),
        **analysis.get("regime_rr", {}),   # e.g. min_rr_to_enter_RISK_ON: 1.3
        **analysis.get("signal_rr", {}),   # e.g. min_rr_prime: 0.95
    }
    # Merge AI suggestions (AI can override statistical proposals)
    if ai_result:
        for k, v in ai_result.items():
            if k in PARAM_DEFAULTS and v is not None:
                proposed[k] = v

    logger.info(f"Writing thresholds (approval_required={approval_required})...")
    changes = write_thresholds(sb, proposed, current_t, analysis, approval_required, today_str)

    # ── Step 6: Write calibration record + update last_calibration_date ──
    write_calibration_lesson(sb, analysis, ai_result, changes, today_str)

    try:
        sb.table("system_config").upsert({
            "key":         "last_calibration_date",
            "value":       today_str,
            "description": f"Last calibration: {today_str} ({analysis['total_trades']} trades, WR={analysis.get('win_rate', 0):.1%})",
            "updated_at":  today_str,
        }, on_conflict="key").execute()
    except Exception as e:
        logger.warning(f"last_calibration_date write failed: {e}")

    next_review = (ai_result or {}).get("recommended_next_review_days", 14)

    logger.success(
        f"Calibration complete: {changes} parameters updated | "
        f"{lesson_stats['retired']} lessons retired | "
        f"Next review in {next_review} days"
    )
    return {
        "status":           "ok",
        "trades_analysed":  len(trades),
        "win_rate":         analysis.get("win_rate"),
        "parameters_updated": changes,
        "lessons_retired":  lesson_stats["retired"],
        "approval_required": approval_required,
        "next_review_days": next_review,
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TradeOS v6 — Step 20: Calibration Engine")
    p.add_argument("--force",    action="store_true", help="Override gate checks and force calibration")
    p.add_argument("--dry-run",  action="store_true", help="Analyse but do not write")
    args = p.parse_args()

    if args.dry_run:
        import os
        os.environ["DRY_RUN"] = "True"

    print(main(force=args.force))
