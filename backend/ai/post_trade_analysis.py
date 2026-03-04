"""
TradeOS v6 — Phase 1: Post-Trade Analysis
Triggered when a new row appears in closed_positions.
Auto-generates a structured lesson entry using AI (or ML fallback).

Data retention: APPEND only to lessons table
"""
import sys
import json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, check_kill_switch, logger
sys.path.insert(0, str(Path(__file__).parent))
from ai_router import AIRouter

ANALYSIS_PROMPT = """You are an expert Indian swing trader reviewing a completed trade. Analyze this trade and generate a structured lesson.

TRADE DETAILS:
Symbol: {symbol} ({company_name}) — {sector}
Strategy: {strategy}
Entry: {entry_date} @ ₹{entry_price} | Exit: {exit_date} @ ₹{exit_price}
Qty: {actual_qty} | Invested: ₹{invested_value:.0f}
P&L: ₹{realized_pnl:.0f} ({pnl_pct:.1f}%)
Exit reason: {exit_reason}
Lifecycle at entry: {lifecycle_at_entry}
Expected R at entry: {expected_r}
High water mark: ₹{high_water_mark}
Max favorable excursion: {mfe}x

MARKET CONTEXT AT ENTRY:
{entry_context}

UPCOMING EVENTS AT ENTRY (if any):
{events_at_entry}

Generate a structured lesson. Output ONLY this JSON:
{{
  "scenario_type": "one of: Win/Trend-Follow, Win/Breakout, Win/Mean-Revert, Loss/Stop-Hunt, Loss/Event-Risk, Loss/False-Breakout, Loss/Overextended, Loss/Sector-Rotation, Breakeven",
  "trigger_event": "what specifically triggered the exit",
  "what_expected": "1 sentence what you expected when you entered",
  "what_happened": "1 sentence what actually happened",
  "what_failed": "specific failure point (or 'Nothing — trade worked as planned')",
  "root_cause": "underlying reason for outcome",
  "corrective_rule": "specific actionable rule for future (start with 'Rule:')"
}}"""

def get_entry_context(sb, symbol: str, entry_date: str) -> str:
    """Get market context around the entry date."""
    try:
        regime = sb.table("regime_history").select("regime,nifty_price,india_vix") \
            .eq("date", entry_date).execute().data
        if regime:
            r = regime[0]
            return f"Regime: {r.get('regime')} | Nifty: {r.get('nifty_price')} | VIX: {r.get('india_vix')}"
        return "Context not available"
    except Exception:
        return "Context not available"

def get_events_at_entry(sb, symbol: str, entry_date: str) -> str:
    """Get events that were active around entry."""
    try:
        events = sb.table("nifty_upcoming_events").select("purpose,event_date") \
            .eq("symbol", symbol).execute().data
        if events:
            return ", ".join(f"{e['purpose']} on {e['event_date']}" for e in events[:3])
        return "None recorded"
    except Exception:
        return "None recorded"

def analyze_trade(trade: dict, sb) -> dict | None:
    """Run AI analysis on a single closed trade."""
    router = AIRouter()

    entry_ctx    = get_entry_context(sb, trade.get("symbol", ""), str(trade.get("entry_date", "")))
    events_entry = get_events_at_entry(sb, trade.get("symbol", ""), str(trade.get("entry_date", "")))

    prompt = ANALYSIS_PROMPT.format(
        symbol          = trade.get("symbol", ""),
        company_name    = trade.get("company_name", ""),
        sector          = trade.get("sector", ""),
        strategy        = trade.get("strategy", ""),
        entry_date      = trade.get("entry_date", ""),
        entry_price     = trade.get("entry_price", 0),
        exit_date       = trade.get("exit_date", ""),
        exit_price      = trade.get("exit_price", 0),
        actual_qty      = trade.get("actual_qty", 0),
        invested_value  = float(trade.get("invested_value", 0) or 0),
        realized_pnl    = float(trade.get("realized_pnl", 0) or 0),
        pnl_pct         = float(trade.get("pnl_pct", 0) or 0) * 100,
        exit_reason     = trade.get("exit_reason", "Unknown"),
        lifecycle_at_entry = trade.get("lifecycle_at_entry", "Unknown"),
        expected_r      = trade.get("expected_r_at_entry", "N/A"),
        high_water_mark = trade.get("high_water_mark", 0),
        mfe             = trade.get("max_favorable_excursion", "N/A"),
        entry_context   = entry_ctx,
        events_at_entry = events_entry,
    )

    if not router.is_available():
        logger.warning("No AI available — skipping post-trade analysis")
        return None

    try:
        raw = router.raw_completion(prompt, max_tokens=600)
        import re
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON in response")
        lesson_data = json.loads(json_match.group())

        linked_type = "RESULTS" if "result" in str(lesson_data.get("trigger_event", "")).lower() \
                      else "STOP_LOSS" if "stop" in str(lesson_data.get("trigger_event", "")).lower() \
                      else "TECHNICAL"

        return {
            "date":              str(trade.get("exit_date", today_ist().isoformat())),
            "scenario_type":     lesson_data.get("scenario_type", ""),
            "trigger_event":     lesson_data.get("trigger_event", ""),
            "linked_event_type": linked_type,
            "impacted_sector":   trade.get("sector", ""),
            "scenario_context":  f"{trade.get('symbol')} {trade.get('strategy')} | P&L: {float(trade.get('pnl_pct',0) or 0)*100:.1f}%",
            "what_expected":     lesson_data.get("what_expected", ""),
            "what_happened":     lesson_data.get("what_happened", ""),
            "what_failed":       lesson_data.get("what_failed", ""),
            "root_cause":        lesson_data.get("root_cause", ""),
            "corrective_rule":   lesson_data.get("corrective_rule", ""),
            "source":            "AI",
        }
    except Exception as e:
        logger.warning(f"Post-trade AI failed for {trade.get('symbol')}: {e}")
        return None

def main():
    check_kill_switch()
    logger.info("Post-trade analysis starting")
    sb = get_supabase()

    # Find recently closed trades without AI lessons (last 7 days)
    from datetime import timedelta
    cutoff = (today_ist() - timedelta(days=7)).isoformat()
    trades = sb.table("closed_positions").select("*") \
        .gte("exit_date", cutoff).execute().data

    if not trades:
        logger.info("No recent closed trades to analyze")
        return {"status": "ok", "analyzed": 0}

    # Get already-analyzed symbols to avoid duplicates
    existing = sb.table("lessons").select("scenario_context") \
        .eq("source", "AI").gte("date", cutoff).execute().data
    analyzed = {r.get("scenario_context", "")[:10] for r in existing}

    analyzed_count = 0
    for trade in trades:
        sym = trade.get("symbol", "")
        if sym[:10] in analyzed:
            continue

        lesson = analyze_trade(trade, sb)
        if lesson:
            # APPEND: insert new lesson
            sb.table("lessons").insert(lesson).execute()
            analyzed_count += 1
            logger.info(f"Lesson generated for {sym}: {lesson.get('scenario_type')}")

    logger.success(f"Post-trade analysis done: {analyzed_count} lessons generated")
    return {"status": "ok", "analyzed": analyzed_count}

if __name__ == "__main__":
    main()
