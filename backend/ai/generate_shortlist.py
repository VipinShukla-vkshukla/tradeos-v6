"""
TradeOS v6 — Phase 1: Auto-Generate Shortlist (AI)
Replaces the manual SHORTLISTED_12 process.
Uses configured AI provider to select and rank TOP 12 stocks from MSL.

Reads:
    master_shortlist  — top candidates by score (trade_allowed=True only)
    market_regime     — today's regime for context
    event_calendar    — active events affecting sectors/timing
    open_positions    — held symbols/sectors (AI avoids adding more of same)

Writes:
    master_shortlist  — ai_shortlist_rank, ai_shortlist_reason per symbol
    ai_context        — full shortlist JSON as symbol=__SHORTLIST__ reference row

Wire in run_pipeline.py after ai_enrich, Phase 1+:
    def step_shortlist():
        from ai.generate_shortlist import main as fn; return fn()
"""
import sys
import os
import json
import re
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

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
    regime     = sb.table("market_regime").select("*").eq("date", today_str).execute().data
    regime_row = regime[0] if regime else {}
    regime_summary = (
        f"Regime: {regime_row.get('regime', 'UNKNOWN')} | "
        f"Nifty: {regime_row.get('nifty_price', 'N/A')} | "
        f"VIX: {regime_row.get('india_vix', 'N/A')} | "
        f"Breadth: {regime_row.get('avg_sector_breadth', 'N/A')}%"
    )

    # MSL — top 25 by score, trade_allowed only
    msl = sb.table("master_shortlist").select(
        "symbol,company_name,sector,strategy_source,final_score,lifecycle,"
        "entry_zone_low,entry_zone_high,current_price,trade_allowed,"
        "entry_ready,position_state,upcoming_news,event_bias"
    ).eq("date", today_str).order("final_score", desc=True).limit(25).execute().data

    msl_lines = []
    for r in msl:
        if not r.get("trade_allowed"):
            continue
        msl_lines.append(
            f"{r['symbol']} | {r.get('sector', '')} | Score:{r.get('final_score', 0):.1f} | "
            f"Lifecycle:{r.get('lifecycle', '')} | State:{r.get('position_state', '')} | "
            f"Zone:{r.get('entry_zone_low', 0)}-{r.get('entry_zone_high', 0)} | "
            f"Price:{r.get('current_price', 0)} | News:{r.get('upcoming_news', 'None')}"
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
    positions  = sb.table("open_positions").select("symbol,sector,strategy,pnl_pct").execute().data
    pos_lines  = [f"{p['symbol']} ({p.get('sector', '')})" for p in positions]

    return {
        "regime_summary": regime_summary,
        "msl_data":       "\n".join(msl_lines) or "No shortlist data",
        "events_data":    "\n".join(event_lines) or "No active events",
        "positions_data": ", ".join(pos_lines) or "None",
    }


def write_to_master_shortlist(sb, shortlist: list[dict], today: str):
    """
    Write ai_shortlist_rank and ai_shortlist_reason back to master_shortlist.
    This is the primary write target per the future state design.
    """
    written = 0
    for item in shortlist:
        symbol = item.get("symbol")
        rank   = item.get("rank")
        reason = item.get("reason", "")
        action = item.get("action", "")
        if not symbol:
            continue
        try:
            sb.table("master_shortlist").update({
                "ai_shortlist_rank":   rank,
                "ai_shortlist_reason": f"[{action}] {reason}",
            }).eq("date", today).eq("symbol", symbol).execute()
            written += 1
        except Exception as e:
            logger.warning(f"master_shortlist update failed for {symbol}: {e}")
    logger.info(f"master_shortlist: wrote ai_shortlist_rank/reason for {written} symbols")
    return written


def write_to_ai_context(sb, shortlist: list[dict], provider_name: str, today: str):
    """
    Save full shortlist JSON to ai_context as a reference/backup row.
    Uses symbol=__SHORTLIST__ as the key.
    """
    try:
        sb.table("ai_context").upsert({
            "date":       today,
            "symbol":     "__SHORTLIST__",
            "ai_note":    json.dumps(shortlist),
            "provider":   provider_name,
            "conviction": "GENERATED",
        }, on_conflict="date,symbol").execute()
    except Exception as e:
        logger.warning(f"ai_context __SHORTLIST__ write failed (non-fatal): {e}")


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — generate_shortlist skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Auto-Shortlist generation starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb    = get_supabase()
    today = today_ist()

    # Import module-level functions from patched ai_router (no AIRouter class)
    try:
        from ai.ai_router import raw_completion, is_ai_available
    except ImportError:
        from ai_router import raw_completion, is_ai_available

    if not is_ai_available():
        logger.warning("No AI provider available for shortlist generation")
        return {"status": "no_ai"}

    # Build context
    ctx    = build_context(sb, today)
    prompt = SHORTLIST_PROMPT.format(**ctx)

    try:
        raw = raw_completion(prompt, max_tokens=2000)

        # Parse JSON array from response
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON array in AI response: {raw[:200]}")
        shortlist = json.loads(json_match.group())

        if not isinstance(shortlist, list) or len(shortlist) == 0:
            raise ValueError("AI returned empty or invalid shortlist")

        provider_name = cfg("ai_provider", "ml")
        today_str     = today.isoformat()

        if DRY_RUN:
            logger.info(f"[DRY RUN] Would write {len(shortlist)} shortlist ranks to master_shortlist")
            for item in shortlist[:3]:
                logger.info(f"  #{item['rank']} {item['symbol']}: {item['action']} — {item['reason'][:60]}")
            return {"status": "dry_run", "shortlist_count": len(shortlist)}

        # Primary write: master_shortlist ranks
        written = write_to_master_shortlist(sb, shortlist, today_str)

        # Secondary write: ai_context reference row
        write_to_ai_context(sb, shortlist, provider_name, today_str)

        logger.success(f"Shortlist generated: {len(shortlist)} stocks via {provider_name}, {written} written to master_shortlist")
        return {
            "status":          "ok",
            "shortlist_count": len(shortlist),
            "written":         written,
            "provider":        provider_name,
        }

    except Exception as e:
        logger.error(f"Shortlist generation failed: {e}")
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    result = main()
    if result.get("status") == "ok":
        # Print via reading back from ai_context for display
        print(f"Generated {result['shortlist_count']} stocks, "
              f"wrote {result['written']} to master_shortlist via {result['provider']}")
    else:
        print(result)
