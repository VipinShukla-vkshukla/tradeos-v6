"""
TradeOS v6 — Phase 1 Step: AI Signal Enrichment
Reads today's signals, enriches top N with AI conviction.
Uses whichever provider is configured in system_config.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, get_config_int, today_ist, check_kill_switch, logger
from ai.ai_router import analyze


def build_context(msl_row: dict, sd_row: dict, regime: dict, events: list, lessons: list) -> dict:
    """Merge all available data into context dict for AI provider."""
    return {
        **msl_row,
        **{k: v for k, v in sd_row.items() if v is not None},
        "regime":          regime.get("regime", "UNKNOWN"),
        "india_vix":       regime.get("india_vix"),
        "sector_rank":     None,  # enriched from sector_strength in full impl
        "upcoming_events": events,
        "relevant_lessons":lessons,
    }


def get_relevant_lessons(symbol: str, sector: str, sb) -> list:
    """Fetch lessons relevant to this stock's sector."""
    rows = sb.table("lessons").select("*").ilike("impacted_sector", f"%{sector}%").limit(3).execute().data
    return rows


def main():
    check_kill_switch()
    start = time.time()
    logger.info("=" * 60)
    logger.info("STEP 4: AI Signal Enrichment")
    logger.info("=" * 60)

    sb         = get_supabase()
    trade_date = today_ist().isoformat()
    max_stocks = get_config_int("ai_max_stocks_per_day", 20)

    # Load today's signals (BUY_CANDIDATE first, then EXIT)
    signals = (sb.table("signal_log")
               .select("*")
               .eq("date", trade_date)
               .in_("signal_type", ["BUY_CANDIDATE", "EXIT", "ADD"])
               .order("score", desc=True)
               .limit(max_stocks)
               .execute().data)

    if not signals:
        logger.info("No signals to enrich today")
        return {"enriched": 0}

    regime  = (sb.table("market_regime").select("*").eq("date", trade_date).execute().data or [{}])[0]
    events  = (sb.table("nifty_upcoming_events")
               .select("*")
               .gte("event_date", trade_date)
               .limit(50)
               .execute().data)

    enriched = 0
    for sig in signals:
        sym = sig["symbol"]
        try:
            # Load MSL row
            msl_rows = sb.table("master_shortlist").select("*").eq("date", trade_date).eq("symbol", sym).execute().data
            msl = msl_rows[0] if msl_rows else {}

            # Load stock data
            sd_rows = sb.table("stock_data_daily").select("*").eq("date", trade_date).eq("symbol", sym).execute().data
            sd = sd_rows[0] if sd_rows else {}

            # Relevant lessons
            sector  = sig.get("sector", "")
            lessons = get_relevant_lessons(sym, sector, sb)

            # Stock-specific events
            sym_events = [e for e in events if e.get("symbol") == sym]

            context = build_context(msl, sd, regime, sym_events, lessons)
            context["eap_action"] = sig.get("eap_action", "NO_CHANGE")

            result = analyze(context)
            if result is None:
                continue

            r = result.to_dict()

            # Update signal_log
            sb.table("signal_log").update({
                "ai_conviction": r["conviction"],
                "ai_note":       r["ai_note"],
                "ai_provider":   r["provider"],
            }).eq("id", sig["id"]).execute()

            # Upsert ai_context
            sb.table("ai_context").upsert({
                "date":             trade_date,
                "symbol":           sym,
                "conviction":       r["conviction"],
                "conviction_reason":r["conviction_reason"],
                "risks":            r["risks"],
                "catalyst":         r["catalyst"],
                "suggested_action": r["suggested_action"],
                "conflicts":        r["conflicts"],
                "ai_note":          r["ai_note"],
                "provider":         r["provider"],
                "fallback_used":    r["fallback_used"],
                "confidence":       r["confidence"],
            }, on_conflict="date,symbol").execute()

            # Update master_shortlist
            sb.table("master_shortlist").update({
                "ai_conviction":        r["conviction"],
                "ai_conviction_reason": r["conviction_reason"],
                "ai_risks":             r["risks"],
                "ai_suggested_action":  r["suggested_action"],
                "ai_note":              r["ai_note"],
                "ai_provider":          r["provider"],
                "ai_fallback_used":     r["fallback_used"],
            }).eq("date", trade_date).eq("symbol", sym).execute()

            enriched += 1
            logger.info(f"  {sym}: {r['conviction']} ({r['provider']}) — {r['conviction_reason'][:60]}")

        except Exception as e:
            logger.warning(f"  AI enrichment failed for {sym}: {e}")

    duration = time.time() - start
    logger.success(f"AI enrichment: {enriched}/{len(signals)} signals enriched in {duration:.1f}s")
    return {"enriched": enriched, "date": trade_date}


if __name__ == "__main__":
    main()
