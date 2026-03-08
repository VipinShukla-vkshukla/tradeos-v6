"""
TradeOS v6 — Phase 1 Step: AI Signal Enrichment
Reads today's signals, enriches top N with AI conviction.
Uses whichever provider is configured in system_config.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg_int, today_ist, is_kill_switch_active, logger
from ai.ai_router import analyze



def get_relevant_lessons(symbol: str, sector: str, strategy: str, lifecycle: str, sb) -> list:
    # First: same sector + same strategy (most specific)
    rows = sb.table("lessons").select("scenario_type,root_cause,corrective_rule,what_failed") \
        .ilike("impacted_sector", f"%{sector}%") \
        .ilike("scenario_context", f"%{strategy}%") \
        .order("date", desc=True).limit(2).execute().data

    # Supplement with same lifecycle scenario if under 2 results
    if len(rows) < 2:
        extra = sb.table("lessons").select("scenario_type,root_cause,corrective_rule,what_failed") \
            .ilike("scenario_type", f"%{lifecycle[:4]}%") \
            .order("date", desc=True).limit(2).execute().data
        rows += extra

    return rows[:3]

#def get_relevant_lessons(symbol: str, sector: str, sb) -> list:
#    """Fetch lessons relevant to this stock's sector."""
#    rows = sb.table("lessons").select("*").ilike("impacted_sector", f"%{sector}%").limit(3).execute().data
#   return rows


def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — ai_enrich skipped")
        return {"enriched": 0, "reason": "kill_switch"}
    start = time.time()
    logger.info("=" * 60)
    logger.info("STEP 4: AI Signal Enrichment")
    logger.info("=" * 60)

    sb         = get_supabase()
    trade_date = today_ist().isoformat()
    max_stocks = cfg_int("ai_max_stocks_per_day", 20)

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
            lessons = get_relevant_lessons(
                sym,
                sector,
                sig.get("strategy", ""),
                msl.get("lifecycle", ""),
                sb
            )

            # Stock-specific events
            sym_events = [e for e in events if e.get("symbol") == sym]

            stock_data = {
                "symbol":        sym,
                "sector":        sector,
                "final_score":   float(sig.get("score") or 0),
                "current_price": float(msl.get("current_price") or 0),
                "rsi_daily":     float(sd.get("rsi_daily") or 0),
                "rsi_weekly":    float(sd.get("rsi_weekly") or 0),
                "adx":           float(sd.get("adx_14") or 0),
                "vol_ratio":     float(sd.get("vol_ratio") or 0),
                "delivery_pct":  float(sd.get("delivery_pct") or 0),
                "atr_pct":       float(sd.get("atr_pct") or 0),
                "ret_6m":        float(sd.get("ret_6m") or 0),
                "lifecycle":     msl.get("lifecycle") or "",
                "eap_action":    sig.get("eap_action") or "NO_CHANGE",
            }
            
            ai_context = {
                "regime":           regime.get("regime", "UNKNOWN"),
                "fii_flag":         sig.get("fii_flag", "NEUTRAL"),
                "active_events":    [e.get("purpose") for e in sym_events],
                "relevant_lessons": [
                    f"{l.get('scenario_type','')} — {l.get('root_cause','')} | {l.get('corrective_rule','')}"
                    for l in lessons
                ],
            }
            
            
            result = analyze(stock_data, ai_context)
            if result is None or result.conviction == "UNKNOWN":
                logger.debug(f"No AI conviction for {sym} — skipping enrichment")
                continue

            r = result.to_dict()

            # Update signal_log
            sb.table("signal_log").update({
                "ai_conviction":          r["ai_conviction"],
                "ai_note":                r["ai_note"],
                "ai_provider":            r["ai_provider"],
                "ai_strategy_validation": r["ai_strategy_validation"],  # ← G4 FIX
            }).eq("id", sig["id"]).execute()

            # Upsert ai_context
            # ai_context upsert — fix keys
            sb.table("ai_context").upsert({
                "date":              trade_date,
                "symbol":            sym,
                "conviction":        r["ai_conviction"],
                "conviction_reason": r["ai_conviction_reason"],
                "risks":             r["ai_risks"],
                "catalyst":          r["ai_catalyst"],
                "suggested_action":  r["ai_suggested_action"],
                "conflicts":         r["ai_conflicts"],
                "ai_note":           r["ai_note"],
                "provider":          r["ai_provider"],
                "fallback_used":     r["ai_fallback_used"],
                "confidence":        r["ai_confidence"],
            }, on_conflict="date,symbol").execute()

            # master_shortlist update — fix keys
            sb.table("master_shortlist").update({
                "ai_conviction":        r["ai_conviction"],
                "ai_conviction_reason": r["ai_conviction_reason"],
                "ai_risks":             r["ai_risks"],
                "ai_suggested_action":  r["ai_suggested_action"],
                "ai_note":              r["ai_note"],
                "ai_provider":          r["ai_provider"],
                "ai_fallback_used":     r["ai_fallback_used"],
            }).eq("date", trade_date).eq("symbol", sym).execute()

            enriched += 1
            logger.info(f"  {sym}: {r['ai_conviction']} ({r['ai_provider']}) — {r['ai_conviction_reason'][:60]}")

        except Exception as e:
            logger.warning(f"  AI enrichment failed for {sym}: {e}")

    duration = time.time() - start
    logger.success(f"AI enrichment: {enriched}/{len(signals)} signals enriched in {duration:.1f}s")
    return {"enriched": enriched, "date": trade_date}


if __name__ == "__main__":
    main()
