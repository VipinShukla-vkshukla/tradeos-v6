"""
TradeOS v6 — Phase 4: Evolution Tracker
Runs every Sunday. Analyzes last 90 days closed trades and proposes rule improvements.

Schedule: Sunday 9 AM IST via .github/workflows/evolution_weekly.yml

PATCHES APPLIED (Strategic Fix #5):
  - Added retire_stale_lessons(): lessons applied 5+ times with <30% success
    rate are set is_active=False. Runs before proposal generation.
  - Added week_of field to proposals (was written by script but missing from
    schema — migration_strategic_fixes.sql adds the column).
  - Added confidence field to proposals (from AI response).
  - Impact tracking: proposal now records applied_at on approval path.
"""
import sys
import json
import pickle
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, MODELS, is_kill_switch_active, logger

EVOLUTION_PROMPT = """You are a quantitative trading strategy analyst reviewing the performance of a systematic trading system.

RECENT PERFORMANCE (last 90 closed trades):
{performance_summary}

CURRENT STRATEGY PARAMETERS:
{strategy_params}

LESSONS LEARNED (from trade journal — active lessons only):
{lessons_summary}

SIGNAL LOG ANALYSIS (signals fired vs outcomes):
{signal_analysis}

AI PROVIDER PERFORMANCE (last 14 days):
{provider_performance}

Your task: Propose specific, evidence-based parameter improvements.

For each proposal:
1. State which parameter to change
2. Current value → proposed value
3. Evidence: how many trades this would have improved/filtered
4. Estimated impact on win rate
5. Confidence score (0.0–1.0)

OUTPUT: JSON array of proposals (max 5):
[
  {{
    "strategy": "CTL",
    "param_name": "min_weekly_rsi",
    "current_value": "58",
    "proposed_value": "62",
    "evidence": "3 losing trades had weekly RSI 58-61 at entry. Filtering these would improve win rate from 57% to 63%",
    "confidence": 0.72
  }}
]
Only propose changes with strong evidence. No preamble, just JSON."""


def get_performance_summary(sb) -> str:
    cutoff = (today_ist() - timedelta(days=90)).isoformat()
    trades = sb.table("closed_positions").select("*").gte("exit_date", cutoff).execute().data
    if not trades:
        return "No closed trades in last 90 days"

    wins  = [t for t in trades if float(t.get("pnl_pct") or 0) > 0]
    total = len(trades)
    win_r = len(wins) / total * 100 if total else 0
    avg_pnl = sum(float(t.get("pnl_pct") or 0) for t in trades) / total if total else 0
    by_strategy = {}
    for t in trades:
        s = t.get("strategy", "Unknown")
        by_strategy.setdefault(s, {"wins": 0, "total": 0})
        by_strategy[s]["total"] += 1
        if float(t.get("pnl_pct") or 0) > 0:
            by_strategy[s]["wins"] += 1

    lines = [f"Total: {total} | Win Rate: {win_r:.1f}% | Avg P&L: {avg_pnl*100:.2f}%"]
    for s, d in by_strategy.items():
        wr = d["wins"] / d["total"] * 100 if d["total"] else 0
        lines.append(f"  {s}: {d['total']} trades, {wr:.0f}% win rate")
    return "\n".join(lines)


def get_signal_analysis(sb) -> str:
    cutoff = (today_ist() - timedelta(days=60)).isoformat()
    signals = sb.table("signal_log").select("strategy,signal_type,score,regime,outcome") \
        .gte("date", cutoff).not_.is_("outcome", "null").execute().data
    if not signals:
        return "No signal outcomes recorded yet"

    by_type = {}
    for s in signals:
        key = f"{s.get('strategy','?')}/{s.get('signal_type','?')}"
        by_type.setdefault(key, {"win": 0, "loss": 0, "total": 0})
        by_type[key]["total"] += 1
        if s.get("outcome") == "WIN":   by_type[key]["win"]  += 1
        elif s.get("outcome") == "LOSS": by_type[key]["loss"] += 1

    lines = []
    for k, v in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
        wr = v["win"] / v["total"] * 100 if v["total"] else 0
        lines.append(f"  {k}: {v['total']} | Win:{wr:.0f}%")
    return "\n".join(lines)


def retire_stale_lessons(sb) -> int:
    """
    NEW — Strategic Fix #5.
    Set is_active=False for lessons that have been applied 5+ times
    and have a success rate below 30%. Prevents the AI from learning
    from rules that demonstrably don't work.

    Requires: lessons.is_active, lessons.times_applied, lessons.times_worked
    (added by migration_strategic_fixes.sql)
    """
    try:
        rows = sb.table("lessons") \
            .select("id, times_applied, times_worked") \
            .eq("is_active", True) \
            .gte("times_applied", 5) \
            .execute().data

        retire_ids = [
            r["id"] for r in rows
            if r.get("times_applied", 0) > 0 and
               (r.get("times_worked") or 0) / r["times_applied"] < 0.30
        ]

        if retire_ids:
            sb.table("lessons") \
                .update({"is_active": False}) \
                .in_("id", retire_ids) \
                .execute()
            logger.info(f"Retired {len(retire_ids)} stale lessons (applied ≥5×, win rate <30%)")
        else:
            logger.info("No stale lessons to retire this week")

        return len(retire_ids)

    except Exception as e:
        logger.warning(f"Lessons retirement failed (is migration applied?): {e}")
        return 0


def retrain_ml_model(sb) -> dict:
    try:
        from ai.providers.ml_provider import MLProvider
        ml = MLProvider()
        result = ml.retrain(sb)
        logger.info(f"ML model retrained: {result}")
        return result
    except Exception as e:
        logger.warning(f"ML retrain failed: {e}")
        return {"status": "error", "error": str(e)}


def generate_proposals(sb) -> list[dict]:
    """
    Use AI to generate rule change proposals.
    G8:  Uses is_ai_available() / raw_completion() — AIRouter class removed in v6.
    G15: Feeds ai_model_performance (14-day provider accuracy) into AI prompt.
    """
    from ai.ai_router import raw_completion, is_ai_available

    if not is_ai_available():
        logger.info("No AI provider configured — evolution proposals skipped")
        return []

    perf_summary    = get_performance_summary(sb)
    signal_analysis = get_signal_analysis(sb)

    strategy_params = {}
    try:
        configs = sb.table("strategy_config").select("strategy,params,enabled").execute().data
        strategy_params = {r["strategy"]: r["params"] for r in configs}
    except Exception:
        pass

    # ── G15: ai_model_performance — 14-day provider accuracy summary ──────────
    provider_summary = "No provider performance data available"
    try:
        perf_rows = (sb.table("ai_model_performance")
                       .select("provider,accuracy,calls_today,cost_today,date")
                       .order("date", desc=True).limit(14).execute().data)
        if perf_rows:
            by_provider: dict = {}
            for r in perf_rows:
                p = r.get("provider", "unknown")
                by_provider.setdefault(p, []).append(r)
            lines = []
            for prov, rows in by_provider.items():
                avg_acc = sum(float(r.get("accuracy") or 0) for r in rows) / len(rows)
                lines.append(f"  {prov}: avg_accuracy={avg_acc:.1%} over {len(rows)} days")
            provider_summary = "\n".join(lines)
    except Exception as e:
        logger.debug(f"ai_model_performance fetch skipped: {e}")

    strategy_params = {}
    try:
        configs = sb.table("strategy_config").select("strategy,params,enabled").execute().data
        strategy_params = {r["strategy"]: r["params"] for r in configs}
    except Exception:
        pass

    # PATCH: fetch only ACTIVE lessons (is_active=True) for context
    lessons = []
    try:
        lesson_rows = sb.table("lessons") \
            .select("corrective_rule,scenario_type,times_applied,times_worked") \
            .eq("is_active", True) \
            .order("date", desc=True) \
            .limit(10).execute().data
        lessons = [
            f"{r.get('corrective_rule','')} [applied {r.get('times_applied',0)}×, "
            f"worked {r.get('times_worked',0)}×]"
            for r in lesson_rows if r.get("corrective_rule")
        ]
    except Exception:
        pass

    prompt = EVOLUTION_PROMPT.format(
        performance_summary  = perf_summary,
        strategy_params      = json.dumps(strategy_params, indent=2),
        lessons_summary      = "\n".join(lessons[:5]) or "No active lessons yet",
        signal_analysis      = signal_analysis,
        provider_performance = provider_summary,    # G15
    )

    try:
        import re
        raw = raw_completion(prompt, max_tokens=1500)
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            return []
        return json.loads(json_match.group())
    except Exception as e:
        logger.warning(f"Proposal generation failed: {e}")
        return []


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — evolution_tracker skipped")
        return {"status": "skipped", "reason": "kill_switch"}
    logger.info("Evolution Tracker starting (Sunday run)")
    sb    = get_supabase()
    today = today_ist()

    # Step 1: Retire stale lessons before generating new proposals
    retired = retire_stale_lessons(sb)

    # Step 2: Retrain ML conviction model
    ml_result = retrain_ml_model(sb)
    logger.info(f"ML retrain: {ml_result}")

    # Step 3: Generate parameter change proposals
    proposals = generate_proposals(sb)
    if proposals:
        for p in proposals:
            p["week_of"]       = today.isoformat()    # PATCH: was missing, now written
            p["proposed_date"] = today.isoformat()
            p["status"]        = "PENDING"
            # confidence is already in proposal dict from AI response
        sb.table("evolution_proposals").insert(proposals).execute()
        logger.success(f"Evolution proposals generated: {len(proposals)}")

        try:
            from control.telegram_bot import send_message
            msg = f"<b>🧠 Weekly Evolution Proposals ({len(proposals)})</b>\n"
            if retired:
                msg += f"<i>📚 {retired} stale lesson(s) retired this week</i>\n"
            for p in proposals:
                msg += f"\n• {p.get('strategy')} {p.get('param_name')}: {p.get('current_value')} → {p.get('proposed_value')}\n"
                msg += f"  <i>{p.get('evidence', '')[:100]}</i>\n"
                msg += f"  Confidence: {p.get('confidence', '?')}\n"
            msg += "\nReview in Evolution tab and approve/reject."
            send_message(msg)
        except Exception:
            pass
    else:
        logger.info("No proposals generated this week")

    return {"ml": ml_result, "proposals": len(proposals), "lessons_retired": retired}


if __name__ == "__main__":
    main()