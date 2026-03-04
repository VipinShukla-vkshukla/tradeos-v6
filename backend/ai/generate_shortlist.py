"""
TradeOS v6 — Phase 1: Auto-Generate Shortlist (AI)
Replaces the manual SHORTLISTED_12 process (README items 5-7).
Uses configured AI provider to select top 12 stocks from MSL.
"""
import sys
import json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, check_kill_switch, get_config, logger
sys.path.insert(0, str(Path(__file__).parent))
from ai_router import AIRouter

SHORTLIST_PROMPT = """You are an expert Indian swing trader. Based on the following data, select the TOP 12 stocks for swing trading this week.

CURRENT CONDITIONS:
{regime_summary}

MASTER SHORTLIST (top candidates by score):
{msl_data}

ACTIVE EVENTS (that could impact timing):
{events_data}

OPEN POSITIONS (already held — avoid adding more of same sector unless strong reason):
{positions_data}

SELECTION CRITERIA for TOP 12:
1. Strong trend + momentum (lifecycle = HOT or BUILDING, not EXTENDED)
2. Entry zone reachable within 1-2% of current price
3. Clean sector (top 4 sectors, no event risk in next 5 days)
4. Not already in open positions
5. Trade allowed = YES, entry ready = YES

OUTPUT FORMAT: Return ONLY a JSON array of exactly 12 objects:
[
  {{
    "rank": 1,
    "symbol": "SBIN",
    "reason": "2-line explanation of why this stock now",
    "risk": "main risk factor",
    "action": "BUY_ON_DIP / BUY_NOW / WATCH_FOR_TRIGGER"
  }},
  ...
]
No preamble, no markdown, just the JSON array."""

def build_context(sb, today: date) -> dict:
    today_str = today.isoformat()

    # Regime
    regime = sb.table("market_regime").select("*").eq("date", today_str).execute().data
    regime_row = regime[0] if regime else {}
    regime_summary = (
        f"Regime: {regime_row.get('regime', 'UNKNOWN')} | "
        f"Nifty: {regime_row.get('nifty_price', 'N/A')} | "
        f"VIX: {regime_row.get('india_vix', 'N/A')} | "
        f"Breadth: {regime_row.get('avg_sector_breadth', 'N/A')}%"
    )

    # MSL — top 25 by score
    msl = sb.table("master_shortlist").select(
        "symbol,company_name,sector,strategy_source,final_score,lifecycle,"
        "entry_zone_low,entry_zone_high,current_price,trade_allowed,"
        "entry_ready,position_state,upcoming_news,event_bias"
    ).eq("date", today_str).order("final_score", desc=True).limit(25).execute().data

    msl_lines = []
    for r in msl:
        if not r.get("trade_allowed"): continue
        msl_lines.append(
            f"{r['symbol']} | {r.get('sector','')} | Score:{r.get('final_score',0):.1f} | "
            f"Lifecycle:{r.get('lifecycle','')} | State:{r.get('position_state','')} | "
            f"Zone:{r.get('entry_zone_low',0)}-{r.get('entry_zone_high',0)} | "
            f"Price:{r.get('current_price',0)} | News:{r.get('upcoming_news','None')}"
        )

    # Events (active)
    events = sb.table("event_calendar").select(
        "event_name,event_type,affected_sectors,event_bias,event_intensity,is_active"
    ).eq("is_active", True).execute().data
    event_lines = [
        f"{e['event_name']} | {e['event_type']} | Sectors:{e['affected_sectors']} | Bias:{e['event_bias']}"
        for e in events[:10]
    ]

    # Open positions
    positions = sb.table("open_positions").select("symbol,sector,strategy,pnl_pct").execute().data
    pos_lines = [f"{p['symbol']} ({p.get('sector','')})" for p in positions]

    return {
        "regime_summary": regime_summary,
        "msl_data":       "\n".join(msl_lines) or "No shortlist data",
        "events_data":    "\n".join(event_lines) or "No active events",
        "positions_data": ", ".join(pos_lines) or "None",
    }

def main():
    check_kill_switch()
    logger.info("Auto-Shortlist generation starting")
    sb = get_supabase()
    today = today_ist()

    # Build context
    ctx = build_context(sb, today)
    prompt = SHORTLIST_PROMPT.format(**ctx)

    # Call AI via router
    router = AIRouter()
    if not router.is_available():
        logger.warning("No AI provider available for shortlist generation")
        return {"status": "no_ai"}

    try:
        raw = router.raw_completion(prompt, max_tokens=2000)
        # Parse JSON from response
        import re
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON array in response")
        shortlist = json.loads(json_match.group())

        # Store result
        result = {
            "date":      today.isoformat(),
            "shortlist": shortlist,
            "provider":  router.active_provider_name(),
            "regime":    ctx["regime_summary"],
        }
        # Save to ai_context table keyed by date+__SHORTLIST__
        sb.table("ai_context").upsert({
            "date":       today.isoformat(),
            "symbol":     "__SHORTLIST__",
            "ai_note":    json.dumps(shortlist),
            "provider":   router.active_provider_name(),
            "conviction": "GENERATED",
        }, on_conflict="date,symbol").execute()

        logger.success(f"Shortlist generated: {len(shortlist)} stocks via {router.active_provider_name()}")
        return result
    except Exception as e:
        logger.error(f"Shortlist generation failed: {e}")
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    result = main()
    if result.get("shortlist"):
        for item in result["shortlist"]:
            print(f"{item['rank']:2}. {item['symbol']:<15} {item['action']:<20} {item['reason'][:60]}")
