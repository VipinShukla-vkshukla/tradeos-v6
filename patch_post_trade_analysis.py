"""
TradeOS v6 — Phase 1: Post-Trade Analysis
Triggered when a new row appears in closed_positions.
Auto-generates a structured lesson entry using AI (or ML fallback).

Data retention: APPEND only to lessons table

PATCH APPLIED (Strategic Fix #2):
  - REMOVED: 'from ai_router import analyze as ai_analyze' — this was an
    unused import that caused circular import → maximum recursion depth exceeded.
    post_trade_analysis calls providers directly via _call_provider(),
    not through ai_router. The import was leftover from an earlier design.
  - REMOVED: duplicate 'from config import cfg, AI_KEYS' inside analyze_trade()
    (it was already imported at module level on line 1).
  - ADDED: guard against AI_KEYS being None/missing key so NoneType errors
    don't silently swallow the whole trade analysis.
"""
import sys
import json
import re
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger
from config import cfg, cfg_float, cfg_int

# ── AI_KEYS: load once at module level ────────────────────────────────────────
# Defensive load: if AI_KEYS not in config, build from env vars
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

# ── NO import of ai_router here — was causing circular import recursion ────────


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
    """
    pnl_pct     = float(trade.get("pnl_pct", 0) or 0)
    exit_reason = str(trade.get("exit_reason", "")).upper()
    strategy    = trade.get("strategy", "")
    sector      = trade.get("sector", "")
    lifecycle   = trade.get("lifecycle_at_entry", "")
    mfe         = float(trade.get("max_favorable_excursion", 0) or 0)
    is_win      = pnl_pct > 0

    if is_win:
        scenario_type = {"CTL": "Win/Trend-Follow", "SBS": "Win/Breakout", "TPO": "Win/Mean-Revert"}.get(strategy, "Win/Trend-Follow")
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

    if is_win:
        root_cause = f"{strategy} setup played out as expected in {sector} sector"
    elif "STOP" in exit_reason and mfe > 1.5:
        root_cause = f"Trade moved favorably (MFE {mfe:.1f}x) but reversed — trailing stop not tightened"
    elif "EVENT" in exit_reason:
        root_cause = "Event risk at entry not adequately priced — exit triggered by event volatility"
    elif lifecycle in ("EXTENDED", "OVEREXTENDED"):
        root_cause = f"Entry in {lifecycle} lifecycle — insufficient margin of safety"
    else:
        root_cause = f"Setup failed to follow through — likely false signal in {sector}"

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

    return {
        "scenario_type":   scenario_type,
        "trigger_event":   exit_reason or "Unknown",
        "what_expected":   f"{strategy} momentum continuation in {sector} from {lifecycle} lifecycle",
        "what_happened":   f"Trade {'reached target' if is_win else 'stopped out'} with {pnl_pct:+.1f}% P&L",
        "what_failed":     "Nothing — trade worked as planned" if is_win else root_cause,
        "root_cause":      root_cause,
        "corrective_rule": corrective_rule,
    }


def _call_provider(provider_name: str, keys: dict, prompt: str) -> str:
    """Call AI provider directly. Returns raw text or empty string on failure.
    FIXED: guards against missing/empty API key before attempting call.
    """
    api_key = (keys or {}).get(provider_name, "")
    if not api_key:
        raise ValueError(f"No API key configured for provider '{provider_name}'")

    if provider_name == "deepseek":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model="deepseek-chat", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()

    elif provider_name == "openai":
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.5-flash")
        return model.generate_content(prompt).text.strip()

    elif provider_name == "grok":
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model="grok-4-latest", max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    elif provider_name == "copilot":
        from openai import AzureOpenAI
        from config import AZURE_ENDPOINT, AZURE_DEPLOYMENT
        client = AzureOpenAI(api_key=api_key, azure_endpoint=AZURE_ENDPOINT, api_version="2024-02-01")
        resp = client.chat.completions.create(
            model=AZURE_DEPLOYMENT, max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content.strip()

    return ""


def analyze_trade(trade: dict, sb, provider_override: str = None) -> dict | None:
    """Run analysis on a single closed trade. Rule-based first, AI enriches if available.
    FIXED: no longer imports AI_KEYS inside this function (was duplicate + caused issues).
    """
    # Use module-level AI_KEYS — do NOT re-import here
    active_provider = provider_override if provider_override else cfg("ai_provider", "disabled").lower()

    lesson_data = generate_rule_based_lesson(trade)
    source      = "RULE_BASED"

    entry_ctx    = get_entry_context(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))
    events_entry = get_events_at_entry(sb, trade.get("symbol", ""), str(trade.get("entry_date", "") or ""))

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

    if active_provider not in ("disabled", "ml"):
        try:
            raw = _call_provider(active_provider, AI_KEYS, prompt)
            if raw:
                json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                if json_match:
                    json_str = json_match.group()
                    json_str = json_str.replace('\n', ' ').replace('\r', '')
                    json_str = re.sub(r'[\x00-\x1f\x7f]', ' ', json_str)
                    try:
                        lesson_data = json.loads(json_str)
                        source = f"AI:{active_provider}"
                    except json.JSONDecodeError:
                        json_str = re.sub(r',\s*}', '}', json_str)
                        json_str = re.sub(r',\s*]', ']', json_str)
                        lesson_data = json.loads(json_str)
                        source = f"AI:{active_provider}"
        except Exception as e:
            logger.warning(f"AI enrichment skipped for {trade.get('symbol')} — using rule-based: {e}")

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
        "is_active":         True,
        "times_applied":     0,
        "times_worked":      0,
        "confidence":        1.0,
    }


def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — post_trade_analysis skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb = get_supabase()
    from datetime import timedelta
    cutoff = (today_ist() - timedelta(days=7)).isoformat()

    trades = sb.table("closed_positions").select("*").gte("exit_date", cutoff).execute().data
    if not trades:
        logger.info("No recent closed trades to analyze")
        return {"status": "ok", "analyzed": 0}

    existing = sb.table("lessons").select("scenario_context").gte("date", cutoff).execute().data
    analyzed = {r.get("scenario_context", "")[:10] for r in existing}

    max_stocks   = cfg_int("ai_max_stocks_per_day", 20)
    daily_budget = cfg_float("ai_daily_budget_inr", 200.0)

    PROVIDER_COST_INR = {
        "deepseek": 0.15, "claude": 0.40, "openai": 0.30,
        "gemini": 0.08, "grok": 0.50, "copilot": 0.60,
    }

    today_str       = str(today_ist())
    todays_ai       = sb.table("lessons").select("id").like("source", "AI:%").gte("date", today_str).execute().data
    ai_count_today  = len(todays_ai)
    active_provider = cfg("ai_provider", "disabled").lower()
    cost_per_call   = PROVIDER_COST_INR.get(active_provider, 0.30)
    estimated_spend = ai_count_today * cost_per_call

    if estimated_spend >= daily_budget or ai_count_today >= max_stocks:
        provider_override = "disabled"
    else:
        provider_override = None

    analyzed_count = 0
    signal_match_count = 0
    for trade in trades:
        sym = trade.get("symbol", "")
        if sym[:10] in analyzed:
            continue

        lesson = analyze_trade(trade, sb, provider_override=provider_override)
        if lesson:
            sb.table("lessons").insert(lesson).execute()
            analyzed_count += 1
            logger.info(f"Lesson generated for {sym}: {lesson.get('scenario_type')} | {lesson.get('source')}")

            try:
                pnl        = float(trade.get("pnl_pct", 0) or 0)
                outcome    = "WIN" if pnl > 0 else "LOSS"
                entry_date = str(trade.get("entry_date", ""))
                response   = sb.table("signal_log") \
                    .update({"outcome": outcome, "outcome_pnl_pct": pnl}) \
                    .eq("symbol", sym).eq("date", entry_date).execute()
                if response.data:
                    signal_match_count += 1
            except Exception as e:
                logger.warning(f"Could not write outcome for {sym}: {e}")

    logger.success(
        f"Post-trade analysis done: {analyzed_count} lessons | "
        f"{signal_match_count}/{len(trades)} matched to signal_log"
    )
    return {"status": "ok", "analyzed": analyzed_count, "signal_matches": signal_match_count}


if __name__ == "__main__":
    main()
