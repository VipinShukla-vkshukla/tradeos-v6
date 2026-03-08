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
from config import get_supabase, today_ist, is_kill_switch_active, logger
sys.path.insert(0, str(Path(__file__).parent))
from ai_router import analyze as analyze_trade # main AI provider

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

def generate_rule_based_lesson(trade: dict) -> dict:
    """
    Generate a structured lesson from trade data without LLM.
    Uses deterministic rules on outcome, exit_reason, RSI, strategy etc.
    Produces the same schema as AI-generated lessons.
    """
    pnl_pct    = float(trade.get("pnl_pct", 0) or 0)
    exit_reason = str(trade.get("exit_reason", "")).upper()
    strategy   = trade.get("strategy", "")
    sector     = trade.get("sector", "")
    symbol     = trade.get("symbol", "")
    lifecycle  = trade.get("lifecycle_at_entry", "")
    mfe        = float(trade.get("max_favorable_excursion", 0) or 0)
    hwm        = float(trade.get("high_water_mark", 0) or 0)
    entry_price = float(trade.get("entry_price", 0) or 0)

    is_win = pnl_pct > 0

    # ── Scenario type ─────────────────────────────────────────
    if is_win:
        if strategy == "CTL":
            scenario_type = "Win/Trend-Follow"
        elif strategy == "SBS":
            scenario_type = "Win/Breakout"
        elif strategy == "TPO":
            scenario_type = "Win/Mean-Revert"
        else:
            scenario_type = "Win/Trend-Follow"
    else:
        if "STOP" in exit_reason:
            scenario_type = "Loss/Stop-Hunt" if mfe > 1.5 else "Loss/False-Breakout"
        elif "EVENT" in exit_reason or "RESULT" in exit_reason:
            scenario_type = "Loss/Event-Risk"
        elif "SECTOR" in exit_reason or "ROTATION" in exit_reason:
            scenario_type = "Loss/Sector-Rotation"
        elif lifecycle in ("EXTENDED", "OVEREXTENDED"):
            scenario_type = "Loss/Overextended"
        else:
            scenario_type = "Loss/False-Breakout"

    # ── Root cause ────────────────────────────────────────────
    if is_win:
        root_cause = f"{strategy} setup played out as expected in {sector} sector"
    elif "STOP" in exit_reason and mfe > 1.5:
        root_cause = f"Trade moved favorably (MFE {mfe:.1f}x) but reversed — trailing stop not tightened"
    elif "EVENT" in exit_reason:
        root_cause = f"Event risk at entry not adequately priced — exit triggered by event volatility"
    elif lifecycle in ("EXTENDED", "OVEREXTENDED"):
        root_cause = f"Entry in {lifecycle} lifecycle — insufficient margin of safety"
    else:
        root_cause = f"Setup failed to follow through — likely false signal in {sector}"

    # ── Corrective rule ───────────────────────────────────────
    if is_win and mfe > 2.0 and pnl_pct < mfe * 0.5:
        corrective_rule = "Rule: Trail stop more aggressively once trade exceeds 1.5x initial target"
    elif is_win:
        corrective_rule = f"Rule: Continue applying {strategy} in {sector} — setup validated"
    elif "STOP" in exit_reason and mfe > 1.5:
        corrective_rule = "Rule: Once MFE exceeds 1.5x risk, move stop to breakeven + 0.5R"
    elif "EVENT" in exit_reason:
        corrective_rule = "Rule: Check nifty_upcoming_events before entry — avoid within 2 days of results"
    elif lifecycle in ("EXTENDED", "OVEREXTENDED"):
        corrective_rule = f"Rule: Do not enter {strategy} when lifecycle_at_entry is EXTENDED or higher"
    else:
        corrective_rule = f"Rule: Require volume confirmation (vol_ratio > 1.5) before {strategy} entry in {sector}"

    # ── What expected / happened ──────────────────────────────
    what_expected = f"{strategy} momentum continuation in {sector} sector from {lifecycle} lifecycle"
    what_happened = f"Trade {'reached target' if is_win else 'stopped out'} with {pnl_pct:+.1f}% P&L"
    what_failed   = "Nothing — trade worked as planned" if is_win else root_cause

    return {
        "scenario_type":   scenario_type,
        "trigger_event":   exit_reason or "Unknown",
        "what_expected":   what_expected,
        "what_happened":   what_happened,
        "what_failed":     what_failed,
        "root_cause":      root_cause,
        "corrective_rule": corrective_rule,
    }

def analyze_trade(trade: dict, sb) -> dict | None:
    """Run analysis on a single closed trade. Rule-based is always generated first,
    AI enriches it if available."""

    # Always generate rule-based first — guaranteed to succeed regardless of data quality
    lesson_data = generate_rule_based_lesson(trade)
    source = "RULE_BASED"

    # Attempt AI enrichment on top — non-fatal if anything fails
    try:
        entry_ctx    = get_entry_context(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))
        events_entry = get_events_at_entry(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))

        stock_data = {
            "symbol":      trade.get("symbol", ""),
            "sector":      trade.get("sector", ""),
            "strategy":    trade.get("strategy", ""),
            "pnl_pct":     float(trade.get("pnl_pct", 0) or 0),
            "exit_reason": trade.get("exit_reason", ""),
            "lifecycle":   trade.get("lifecycle_at_entry", ""),
        }
        context = {
            "regime":            entry_ctx,
            "active_events":     events_entry,
            "relevant_lessons":  [],
        }

        result = ai_analyze(stock_data, context)

        if result is not None and result.conviction != "UNKNOWN":
            import re
            json_match = re.search(r'\{.*\}', result.conviction_reason or "", re.DOTALL)
            if json_match:
                lesson_data = json.loads(json_match.group())
                source = f"AI:{result.provider}"

    except Exception as e:
        logger.debug(f"AI enrichment skipped for {trade.get('symbol')} — using rule-based: {e}")

    # Build lesson record from whatever lesson_data we have
    trigger = str(lesson_data.get("trigger_event", ""))
    linked_type = "RESULTS"   if "result" in trigger.lower() \
             else "STOP_LOSS" if "stop"   in trigger.lower() \
             else "TECHNICAL"

    return {
        "date":              str(trade.get("exit_date") or today_ist().isoformat()),
        "scenario_type":     lesson_data.get("scenario_type", ""),
        "trigger_event":     lesson_data.get("trigger_event", ""),
        "linked_event_type": linked_type,
        "impacted_sector":   trade.get("sector", ""),
        "scenario_context":  f"{trade.get('symbol')} {trade.get('strategy')} | P&L: {float(trade.get('pnl_pct', 0) or 0):.1f}%",
        "what_expected":     lesson_data.get("what_expected", ""),
        "what_happened":     lesson_data.get("what_happened", ""),
        "what_failed":       lesson_data.get("what_failed", ""),
        "root_cause":        lesson_data.get("root_cause", ""),
        "corrective_rule":   lesson_data.get("corrective_rule", ""),
        "source":            source,
    }

def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — post_trade_analysis skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb = get_supabase()

    # Find recently closed trades without AI lessons (last 7 days)
    from datetime import timedelta
    cutoff = (today_ist() - timedelta(days=7)).isoformat()
    #cutoff = (today_ist() - timedelta(days=365)).isoformat()  # back-fill up to 1 year on first run
    trades = sb.table("closed_positions").select("*") \
        .gte("exit_date", cutoff).execute().data

    if not trades:
        logger.info("No recent closed trades to analyze")
        return {"status": "ok", "analyzed": 0}

    # Get already-analyzed symbols to avoid duplicates
    existing = sb.table("lessons").select("scenario_context") \
    .gte("date", cutoff).execute().data   # no source filter — catches AI and RULE_BASED
    analyzed = {r.get("scenario_context", "")[:10] for r in existing}

    analyzed_count = 0
    signal_match_count = 0
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

            # Write outcome back to signal_log for ML training
            try:
                pnl = float(trade.get("pnl_pct", 0) or 0)
                outcome = "WIN" if pnl > 0 else "LOSS"
                entry_date = str(trade.get("entry_date", ""))
                response =sb.table("signal_log") \
                    .update({"outcome": outcome}) \
                    .eq("symbol", sym) \
                    .eq("date", entry_date) \
                    .execute()
                if response.data:  # update returned rows = match found
                    signal_match_count += 1
                logger.debug(f"Outcome written for {sym}: {outcome}")
            except Exception as e:
                logger.warning(f"Could not write outcome for {sym}: {e}")

    logger.success(
        f"Post-trade analysis done: {analyzed_count} lessons generated | "
        f"{signal_match_count}/{len(trades)} trades matched to signal_log"
    )
    return {"status": "ok", "analyzed": analyzed_count, "signal_matches": signal_match_count}

if __name__ == "__main__":
    main()
