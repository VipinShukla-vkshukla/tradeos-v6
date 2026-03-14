"""
TradeOS v6 — Phase 1: AI Signal Enrichment (PATCHED)

PATCH APPLIED (Strategic Fix #3):
  - EXIT and ADD signals for OPEN POSITIONS are now always loaded
    regardless of the ai_max_stocks_per_day budget limit.
    Previously: all signals ordered by score DESC then limited to max_stocks —
    EXIT signals could be silently dropped if BUY_CANDIDATE signals filled
    the budget first. A position could miss its AI-enriched exit analysis.
  - New load strategy:
      1. Load ALL EXIT + ADD signals for open positions (no limit — you only
         ever have max 20 open positions so this is always bounded).
      2. Fill remaining budget with BUY_CANDIDATE signals ordered by score DESC.
  - Also fixed: ai_enrich reads sd.get("adx") not sd.get("adx_14")
    (stock_data_daily stores the field as 'adx' after RENAME_MAP is applied
    in compute_indicators.py — see RENAME_MAP in deployment README).
  - Also fixed: get_relevant_lessons now filters is_active=True only
    (stale/retired lessons are excluded from AI context).
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg_int, today_ist, is_kill_switch_active, logger
from ai.ai_router import analyze


def get_relevant_lessons(symbol: str, sector: str, strategy: str, lifecycle: str, sb) -> list:
    """Fetch lessons relevant to this stock. PATCHED: filters is_active=True only."""
    rows = sb.table("lessons").select("scenario_type,root_cause,corrective_rule,what_failed") \
        .eq("is_active", True) \
        .ilike("impacted_sector", f"%{sector}%") \
        .ilike("scenario_context", f"%{strategy}%") \
        .order("date", desc=True).limit(2).execute().data

    if len(rows) < 2:
        extra = sb.table("lessons").select("scenario_type,root_cause,corrective_rule,what_failed") \
            .eq("is_active", True) \
            .ilike("scenario_type", f"%{lifecycle[:4]}%") \
            .order("date", desc=True).limit(2).execute().data
        rows += extra

    return rows[:3]


def main():
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — ai_enrich skipped")
        return {"enriched": 0, "reason": "kill_switch"}

    start      = time.time()
    sb         = get_supabase()
    trade_date = today_ist().isoformat()
    max_stocks = cfg_int("ai_max_stocks_per_day", 20)

    logger.info("=" * 60)
    logger.info("STEP: AI Signal Enrichment")
    logger.info("=" * 60)

    # ── PATCH: Load EXIT/ADD signals first (always, budget-exempt) ────────────
    # These are for stocks already in open_positions — their exit analysis
    # must never be dropped by budget. Open positions are capped at ~20 anyway.
    exit_signals = (sb.table("signal_log")
                    .select("*")
                    .eq("date", trade_date)
                    .in_("signal_type", ["EXIT", "ADD"])
                    .order("score", desc=True)
                    .execute().data)

    # Load BUY_CANDIDATE signals — fill remaining budget after EXIT allocation
    budget_remaining = max(0, max_stocks - len(exit_signals))
    buy_signals      = []
    if budget_remaining > 0:
        buy_signals = (sb.table("signal_log")
                       .select("*")
                       .eq("date", trade_date)
                       .eq("signal_type", "BUY_CANDIDATE")
                       .order("score", desc=True)
                       .limit(budget_remaining)
                       .execute().data)

    # EXIT signals always first in processing order
    signals = exit_signals + buy_signals

    logger.info(
        f"Signals loaded: {len(exit_signals)} EXIT/ADD (budget-exempt) + "
        f"{len(buy_signals)} BUY_CANDIDATE (from {budget_remaining} budget slots)"
    )

    if not signals:
        logger.info("No signals to enrich today")
        return {"enriched": 0}

    regime = (sb.table("market_regime").select("*").eq("date", trade_date).execute().data or [{}])[0]
    events = (sb.table("nifty_upcoming_events")
               .select("*")
               .gte("event_date", trade_date)
               .limit(50)
               .execute().data)

    enriched = 0
    for sig in signals:
        sym = sig["symbol"]
        try:
            msl_rows = sb.table("master_shortlist").select("*").eq("date", trade_date).eq("symbol", sym).execute().data
            msl      = msl_rows[0] if msl_rows else {}

            sd_rows = sb.table("stock_data_daily").select("*").eq("date", trade_date).eq("symbol", sym).execute().data
            sd      = sd_rows[0] if sd_rows else {}

            sector  = sig.get("sector", "")
            lessons = get_relevant_lessons(sym, sector, sig.get("strategy", ""), msl.get("lifecycle", ""), sb)

            sym_events = [e for e in events if e.get("symbol") == sym]

            stock_data = {
                "symbol":        sym,
                "sector":        sector,
                "signal_type":   sig.get("signal_type", ""),    # pass to AI for context
                "final_score":   float(sig.get("score") or 0),
                "current_price": float(msl.get("current_price") or 0),
                "rsi_daily":     float(sd.get("rsi_daily") or 0),
                "rsi_weekly":    float(sd.get("rsi_weekly") or 0),
                "adx":           float(sd.get("adx") or 0),     # FIXED: was adx_14
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

            sb.table("signal_log").update({
                "ai_conviction":          r["ai_conviction"],
                "ai_note":                r["ai_note"],
                "ai_provider":            r["ai_provider"],
                "ai_strategy_validation": r["ai_strategy_validation"],
            }).eq("id", sig["id"]).execute()

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
            logger.info(f"  {sym} [{sig.get('signal_type')}]: {r['ai_conviction']} "
                        f"({r['ai_provider']}) — {r['ai_conviction_reason'][:60]}")

        except Exception as e:
            logger.warning(f"  AI enrichment failed for {sym}: {e}")

    duration = time.time() - start
    logger.success(
        f"AI enrichment: {enriched}/{len(signals)} enriched in {duration:.1f}s "
        f"[{len(exit_signals)} EXIT/ADD guaranteed + {len(buy_signals)} buys]"
    )
    return {"enriched": enriched, "date": trade_date,
            "exit_enriched": sum(1 for s in exit_signals if s["symbol"] in [sig["symbol"] for sig in signals[:enriched]])}


if __name__ == "__main__":
    main()
