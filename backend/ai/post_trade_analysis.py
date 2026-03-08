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
import re
from config import cfg, AI_KEYS, cfg_float, cfg_int


sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger
sys.path.insert(0, str(Path(__file__).parent))
from ai_router import analyze as ai_analyze # main AI provider

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

def _call_provider(provider_name: str, ai_keys: dict, prompt: str) -> str:
    """Call the configured AI provider directly with the lesson prompt.
    Returns raw text response or empty string on failure."""

    if provider_name == "deepseek":
        from openai import OpenAI
        client = OpenAI(api_key=ai_keys["deepseek"], base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=ai_keys["claude"])
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()

    elif provider_name == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=ai_keys["openai"])
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=ai_keys["gemini"])
        model = genai.GenerativeModel("gemini-2.5-flash")
        resp = model.generate_content(prompt)
        return resp.text.strip()

    elif provider_name == "grok":
        from openai import OpenAI
        client = OpenAI(api_key=ai_keys["grok"], base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model="grok-4-latest", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "copilot":
        from openai import AzureOpenAI
        from config import AZURE_ENDPOINT, AZURE_DEPLOYMENT
        client = AzureOpenAI(
            api_key=ai_keys["copilot"],
            azure_endpoint=AZURE_ENDPOINT,
            api_version="2024-02-01"
        )
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    return ""

def analyze_trade(trade: dict, sb, provider_override: str = None) -> dict | None:
    """Run analysis on a single closed trade. Rule-based first, AI enriches if available."""  # ← MOVE HERE
    from config import cfg, AI_KEYS  # ← DELETE this, already imported at top
    active_provider = provider_override if provider_override else cfg("ai_provider", "disabled").lower()

    # Always generate rule-based first — guaranteed to succeed
    lesson_data = generate_rule_based_lesson(trade)
    source = "RULE_BASED"

    # Build context
    entry_ctx    = get_entry_context(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))
    events_entry = get_events_at_entry(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))

    # Build the prompt here — not inside context dict
    prompt = ANALYSIS_PROMPT.format(
        symbol             = trade.get("symbol", ""),
        company_name       = trade.get("company_name", ""),
        sector             = trade.get("sector", ""),
        strategy           = trade.get("strategy", ""),
        entry_date         = trade.get("entry_date", ""),
        entry_price        = trade.get("entry_price", 0),
        exit_date          = trade.get("exit_date", ""),
        exit_price         = trade.get("exit_price", 0),
        actual_qty         = trade.get("actual_qty", 0),
        invested_value     = float(trade.get("invested_value", 0) or 0),
        realized_pnl       = float(trade.get("realized_pnl", 0) or 0),
        pnl_pct            = float(trade.get("pnl_pct", 0) or 0),
        exit_reason        = trade.get("exit_reason", "Unknown"),
        lifecycle_at_entry = trade.get("lifecycle_at_entry", "Unknown"),
        expected_r         = trade.get("expected_r_at_entry", "N/A"),
        high_water_mark    = trade.get("high_water_mark", 0),
        mfe                = trade.get("max_favorable_excursion", "N/A"),
        entry_context      = entry_ctx,
        events_at_entry    = events_entry,
    )
 
    # Attempt AI enrichment — non-fatal if anything fails
    if active_provider not in ("disabled", "ml"):
        try:
            raw = _call_provider(active_provider, AI_KEYS, prompt)
            if raw:
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    json_str = json_match.group()
                    # Clean common JSON breaking characters from LLM responses
                    json_str = json_str.replace('\n', ' ').replace('\r', '')
                    # Remove control characters
                    json_str = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
                    try:
                        lesson_data = json.loads(json_str)
                        source = f"AI:{active_provider}"
                    except json.JSONDecodeError:
                        # Try with json5/relaxed parsing as last resort
                        import ast
                        logger.debug(f"Standard JSON parse failed for {trade.get('symbol')} — trying cleanup")
                        # Fix trailing commas and other common LLM JSON issues
                        json_str = re.sub(r',\s*}', '}', json_str)
                        json_str = re.sub(r',\s*]', ']', json_str)
                        lesson_data = json.loads(json_str)
                        source = f"AI:{active_provider}"
                    logger.debug(f"AI lesson generated for {trade.get('symbol')} via {active_provider}")
        except Exception as e:
            logger.warning(f"AI enrichment skipped for {trade.get('symbol')} — using rule-based: {e}")

    # Build final lesson record
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

    # Cost controls
    max_stocks = cfg_int("ai_max_stocks_per_day", 20)
    daily_budget = cfg_float("ai_daily_budget_inr", 200.0)

    # Approximate cost per call in INR
    PROVIDER_COST_INR = {
        "deepseek": 0.15,
        "claude":   0.40,
        "openai":   0.30,
        "gemini":   0.08,
        "grok":     0.50,
        "copilot":  0.60,
    }

    # Count how many AI lessons already generated today
    today_str = str(today_ist())
    todays_ai = sb.table("lessons") \
        .select("id") \
        .like("source", "AI:%") \
        .gte("date", today_str) \
        .execute().data
    ai_count_today = len(todays_ai)

    active_provider = cfg("ai_provider", "disabled").lower()
    cost_per_call = PROVIDER_COST_INR.get(active_provider, 0.30)
    estimated_spend_today = ai_count_today * cost_per_call

    if estimated_spend_today >= daily_budget:
        logger.warning(
            f"Estimated daily AI spend ₹{estimated_spend_today:.1f} >= "
            f"budget ₹{daily_budget:.0f} — using rule-based only"
        )
        provider_override = "disabled"
    elif ai_count_today >= max_stocks:
        logger.warning(
            f"AI daily limit reached ({ai_count_today}/{max_stocks}) — using rule-based only"
        )
        provider_override = "disabled"
    else:
        logger.info(
            f"AI usage today: {ai_count_today} calls | "
            f"~₹{estimated_spend_today:.1f} spent | budget ₹{daily_budget:.0f}"
        )
        provider_override = None

    analyzed_count = 0
    signal_match_count = 0
    for trade in trades:
        sym = trade.get("symbol", "")
        if sym[:10] in analyzed:
            continue

        lesson = analyze_trade(trade, sb, provider_override=provider_override)
        if lesson:
            # APPEND: insert new lesson
            sb.table("lessons").insert(lesson).execute()
            analyzed_count += 1
            logger.info(f"Lesson generated for {sym}: {lesson.get('scenario_type')} | source: {lesson.get('source')}")

            # Write outcome back to signal_log for ML training
            try:
                pnl = float(trade.get("pnl_pct", 0) or 0)
                outcome = "WIN" if pnl > 0 else "LOSS"
                entry_date = str(trade.get("entry_date", ""))
                response = sb.table("signal_log") \
                    .update({
                        "outcome":         outcome,
                        "outcome_pnl_pct": pnl,       # ← G2 FIX
                    }) \
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
    f"{signal_match_count}/{len(trades)} trades matched to signal_log | "
    f"AI spend today: ~₹{estimated_spend_today + (analyzed_count * cost_per_call):.1f} / ₹{daily_budget:.0f} budget"
    )
    return {"status": "ok", "analyzed": analyzed_count, "signal_matches": signal_match_count}

if __name__ == "__main__":
    main()
