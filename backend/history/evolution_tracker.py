"""
TradeOS v6 — Phase 4: Evolution Tracker
Runs every Sunday. Analyzes last 30 closed trades and proposes rule improvements.
Also retrains the ML model with latest data.

Schedule: Sunday 9 AM IST via .github/workflows/evolution_weekly.yml
"""
import sys
import json
import pickle
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, MODELS, check_kill_switch, logger

EVOLUTION_PROMPT = """You are a quantitative trading strategy analyst reviewing the performance of a systematic trading system.

RECENT PERFORMANCE (last 30 closed trades):
{performance_summary}

CURRENT STRATEGY PARAMETERS:
{strategy_params}

LESSONS LEARNED (from trade journal):
{lessons_summary}

SIGNAL LOG ANALYSIS (signals fired vs outcomes):
{signal_analysis}

Your task: Propose specific, evidence-based parameter improvements.

For each proposal:
1. State which parameter to change
2. Current value → proposed value
3. Evidence: how many trades this would have improved/filtered
4. Estimated impact on win rate

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
    """Compare signals fired vs their outcomes."""
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
        if s.get("outcome") == "WIN":
            by_type[key]["win"] += 1
        elif s.get("outcome") == "LOSS":
            by_type[key]["loss"] += 1

    lines = []
    for k, v in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
        wr = v["win"] / v["total"] * 100 if v["total"] else 0
        lines.append(f"  {k}: {v['total']} | Win:{wr:.0f}%")
    return "\n".join(lines)

def retrain_ml_model(sb) -> dict:
    """Retrain the ML conviction model with latest data."""
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
    """Use AI to generate rule change proposals."""
    from ai.ai_router import AIRouter
    router = AIRouter()
    if not router.is_available():
        logger.info("No AI available for evolution proposals")
        return []

    perf_summary = get_performance_summary(sb)
    signal_analysis = get_signal_analysis(sb)

    strategy_params = {}
    try:
        configs = sb.table("strategy_config").select("strategy,params,enabled").execute().data
        strategy_params = {r["strategy"]: r["params"] for r in configs}
    except Exception:
        pass

    lessons = []
    try:
        lesson_rows = sb.table("lessons").select("corrective_rule,scenario_type") \
            .order("date", desc=True).limit(10).execute().data
        lessons = [r.get("corrective_rule", "") for r in lesson_rows if r.get("corrective_rule")]
    except Exception:
        pass

    prompt = EVOLUTION_PROMPT.format(
        performance_summary = perf_summary,
        strategy_params     = json.dumps(strategy_params, indent=2),
        lessons_summary     = "\n".join(lessons[:5]) or "No lessons recorded yet",
        signal_analysis     = signal_analysis,
    )

    try:
        import re
        raw = router.raw_completion(prompt, max_tokens=1500)
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            return []
        proposals = json.loads(json_match.group())
        return proposals
    except Exception as e:
        logger.warning(f"Proposal generation failed: {e}")
        return []

def main():
    check_kill_switch()
    logger.info("Evolution Tracker starting (Sunday run)")
    sb = get_supabase()
    today = today_ist()

    # Retrain ML model
    ml_result = retrain_ml_model(sb)
    logger.info(f"ML retrain: {ml_result}")

    # Generate proposals
    proposals = generate_proposals(sb)
    if proposals:
        for p in proposals:
            p["week_of"] = today.isoformat()
            p["status"]  = "PENDING"
        sb.table("evolution_proposals").insert(proposals).execute()
        logger.success(f"Evolution proposals generated: {len(proposals)}")

        # Send via Telegram
        try:
            from control.telegram_bot import send_message
            msg = f"<b>🧠 Weekly Evolution Proposals ({len(proposals)})</b>\n"
            for p in proposals:
                msg += f"\n• {p.get('strategy')} {p.get('param_name')}: {p.get('current_value')} → {p.get('proposed_value')}\n"
                msg += f"  <i>{p.get('evidence', '')[:100]}</i>\n"
            msg += "\nReview in Evolution tab and approve/reject."
            send_message(msg)
        except Exception:
            pass
    else:
        logger.info("No proposals generated this week")

    return {"ml": ml_result, "proposals": len(proposals)}

if __name__ == "__main__":
    main()
