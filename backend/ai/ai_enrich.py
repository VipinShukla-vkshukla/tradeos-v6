"""
TradeOS v6 — Phase 1: AI Signal Enrichment (FULLY PATCHED)

PATCHES APPLIED:
  Fix #3  (Strategic): EXIT+ADD signals loaded first, budget-exempt.
          BUY_CANDIDATEs fill remaining slots. adx field corrected.
          get_relevant_lessons() filters is_active=True.

  G6:  event_calendar (next 14 days, per-symbol) added to AI context.
  G13: sector_strength + industry_strength added to AI context.
  G17: market_regime full object (regime_score, breadth_pct, advance_decline)
       + global_cues (gift_nifty, gap_signal, sector_impacts, DOW/NQ change%)
       added to AI context. fii_flag now read DIRECTLY from fii_dii_flow
       (not from signal_log.fii_flag which was NULL before Fix #1).
  G18: open_positions portfolio context (total count, sector/strategy
       concentration, already_held flag) added to AI context.
"""
import sys
import time
from pathlib import Path
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg_int, today_ist, is_kill_switch_active, logger
from ai.ai_router import analyze


# ── Context fetchers ──────────────────────────────────────────────────────────

def get_relevant_lessons(symbol: str, sector: str, strategy: str, lifecycle: str, sb) -> list:
    """
    Fetch lessons relevant to this stock.
    PATCHED Fix #3: filters is_active=True — retired/stale lessons excluded.
    """
    rows = (sb.table("lessons")
              .select("scenario_type,root_cause,corrective_rule,what_failed")
              .eq("is_active", True)
              .ilike("impacted_sector", f"%{sector}%")
              .ilike("scenario_context", f"%{strategy}%")
              .order("date", desc=True).limit(2).execute().data)

    if len(rows) < 2:
        extra = (sb.table("lessons")
                   .select("scenario_type,root_cause,corrective_rule,what_failed")
                   .eq("is_active", True)
                   .ilike("scenario_type", f"%{lifecycle[:4]}%")
                   .order("date", desc=True).limit(2).execute().data)
        rows += extra

    return rows[:3]


def get_fii_context(sb, trade_date: str) -> dict:
    """
    G17: Read FII/DII data directly from fii_dii_flow.
    Falls back to NEUTRAL if table is empty (before Fix #1 is fully backfilled).
    """
    rows = (sb.table("fii_dii_flow")
              .select("fii_net,dii_net,fii_net_5d,fii_net_10d,fii_net_20d,fii_flag")
              .order("date", desc=True).limit(1).execute().data)
    if rows:
        r = rows[0]
        return {
            "fii_net":     r.get("fii_net"),
            "dii_net":     r.get("dii_net"),
            "fii_net_5d":  r.get("fii_net_5d"),
            "fii_net_20d": r.get("fii_net_20d"),
            "fii_flag":    r.get("fii_flag", "NEUTRAL"),
        }
    return {"fii_flag": "NEUTRAL", "note": "fii_dii_flow empty — backfill pending"}


def get_global_cues_context(sb) -> dict:
    """
    G17: Fetch today's EVENING global cues for AI context.
    Falls back to most recent EVENING row if today's not yet written.
    """
    today = today_ist().isoformat()

    rows = (sb.table("global_cues")
              .select("gift_nifty_chg_pct,gap_signal,"
                      "usd_inr,usd_inr_chg_pct,"
                      "brent_crude,brent_chg_pct,"
                      "gold_price,"
                      "us_dow_chg_pct,us_nasdaq_chg_pct,sp500_chg_pct,"
                      "sector_impacts")
              .eq("session", "EVENING")
              .eq("date", today)
              .limit(1).execute().data)

    if not rows:
        # Fall back to most recent EVENING row (e.g. if pipeline runs before cues written)
        rows = (sb.table("global_cues")
                  .select("gift_nifty_chg_pct,gap_signal,"
                          "usd_inr,usd_inr_chg_pct,"
                          "brent_crude,brent_chg_pct,"
                          "gold_price,"
                          "us_dow_chg_pct,us_nasdaq_chg_pct,sp500_chg_pct,"
                          "sector_impacts")
                  .eq("session", "EVENING")
                  .order("date", desc=True).limit(1).execute().data)

    if rows:
        r = rows[0]
        # Unpack sector_impacts JSONB so AI gets flat keys, not a nested dict string
        impacts = r.pop("sector_impacts", {}) or {}
        return {**r, **impacts}

    return {"note": "No global cues available"}


def get_regime_context(sb, trade_date: str) -> dict:
    """
    G17: Full regime object for AI context.
    Selects only columns that exist after sql_market_regime_new_cols.sql is run.
    """
    cols = (
        "regime,regime_score,"
        "nifty_price,nifty_50dma,nifty_200dma,"
        "nifty_weekly_rsi,india_vix,avg_sector_breadth,"
        "nifty_1d_chg_pct,nifty_5d_chg_pct,nifty_20d_chg_pct,"
        "advance_decline_ratio,above_200dma_pct,"
        "predicted_regime,regime_confidence"
    )

    rows = (sb.table("market_regime")
              .select(cols)
              .eq("date", trade_date).execute().data)
    if not rows:
        rows = (sb.table("market_regime")
                  .select(cols)
                  .order("date", desc=True).limit(1).execute().data)
    return rows[0] if rows else {"regime": "UNKNOWN"}


def get_portfolio_context(sb, symbol: str, sector: str) -> dict:
    """G18: Open positions portfolio — concentration, already_held flag."""
    positions = (sb.table("open_positions")
                   .select("symbol,sector,strategy,invested_value,pnl_pct,lifecycle")
                   .execute().data)
    sectors_held    = {}
    strategies_held = {}
    for p in positions:
        sec   = p.get("sector",   "Unknown")
        strat = p.get("strategy", "Unknown")
        sectors_held[sec]      = sectors_held.get(sec, 0) + 1
        strategies_held[strat] = strategies_held.get(strat, 0) + 1

    already_held = any(p["symbol"] == symbol for p in positions)
    sector_count = sectors_held.get(sector, 0)

    return {
        "total_open":         len(positions),
        "already_held":       already_held,
        "sector_exposure":    sectors_held,
        "strategy_exposure":  strategies_held,
        "this_sector_count":  sector_count,
        "note": (
            f"Already holding {symbol} — consider HOLD not new entry."
            if already_held
            else f"{sector_count} positions already in {sector} sector."
        ),
    }


def get_sector_industry_context(sb, sector: str, industry: str, trade_date: str) -> dict:
    """G13: sector_strength + industry_strength context."""

    sec_cols = (
    "sector,rank,top4_flag,sector_state,"
    "avg_rsi_daily,avg_rsi_weekly,avg_rsi_monthly,"
    "avg_ret_6m,breadth_sma50,fii_flow_sector"
    )
    ind_cols = (
        "industry,rank,top5_flag,industry_state,"
        "avg_rsi_daily,avg_rsi_weekly,avg_rsi_monthly,"
        "avg_ret_6m,breadth_sma50"
    )

    sec_rows = (sb.table("sector_strength")
                  .select(sec_cols)
                  .eq("sector", sector)
                  .eq("date", trade_date).limit(1).execute().data)
    if not sec_rows:
        sec_rows = (sb.table("sector_strength")
                      .select(sec_cols)
                      .eq("sector", sector)
                      .order("date", desc=True).limit(1).execute().data)

    ind_rows = (sb.table("industry_strength")
                  .select(ind_cols)
                  .eq("industry", industry)
                  .eq("date", trade_date).limit(1).execute().data)
    if not ind_rows:
        ind_rows = (sb.table("industry_strength")
                      .select(ind_cols)
                      .eq("industry", industry)
                      .order("date", desc=True).limit(1).execute().data)

    return {
        "sector_context":   sec_rows[0] if sec_rows else {"note": f"No sector data for {sector}"},
        "industry_context": ind_rows[0] if ind_rows else {"note": f"No industry data for {industry}"},
    }


def get_event_calendar_context(sb, symbol: str, sector: str, trade_date: str) -> list:
    """
    G6: event_calendar (next 14 days).
    Checks sector-level and global events.
    NOTE: no symbol column in event_calendar — matching is sector/global only.
    """
    lookahead = (today_ist() + timedelta(days=14)).isoformat()

    rows = (sb.table("event_calendar")
              .select("event_type,event_name,event_category,"
                      "start_date,end_date,affected_sectors,notes")
              .eq("is_active", True)
              .gte("start_date", trade_date)
              .lte("start_date", lookahead)
              .order("start_date")
              .limit(10).execute().data)

    relevant = []
    for ev in rows:
        aff_sec = str(ev.get("affected_sectors", "")).lower()
        is_global = ev.get("event_category") == "GLOBAL"
        is_sector_match = sector and sector.lower() in aff_sec

        if is_global or is_sector_match:
            label = f"{ev.get('event_type', '')} — {ev.get('event_name', '')} on {ev.get('start_date', '')}"
            if ev.get("notes"):
                label += f": {ev['notes']}"
            relevant.append(label)

    return relevant if relevant else ["No relevant events in next 14 days"]


# ── Main ──────────────────────────────────────────────────────────────────────

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

    # ── Fix #3: Load EXIT/ADD signals first — always, no budget limit ─────────
    exit_signals = (sb.table("signal_log")
                    .select("*")
                    .eq("date", trade_date)
                    .in_("signal_type", ["EXIT", "ADD"])
                    .order("score", desc=True)
                    .execute().data)

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

    signals = exit_signals + buy_signals

    logger.info(
        f"Signals loaded: {len(exit_signals)} EXIT/ADD (budget-exempt) + "
        f"{len(buy_signals)} BUY_CANDIDATE (from {budget_remaining} budget slots)"
    )

    if not signals:
        logger.info("No signals to enrich today")
        return {"enriched": 0}

    # ── Shared context (fetched once, reused per-stock) ───────────────────────
    regime_ctx    = get_regime_context(sb, trade_date)
    fii_ctx       = get_fii_context(sb, trade_date)
    global_cues   = get_global_cues_context(sb)

    # nifty_upcoming_events (kept as-is for backward compat — used for quick sym lookup)
    upcoming_events = (sb.table("nifty_upcoming_events")
                         .select("*")
                         .gte("event_date", trade_date)
                         .limit(50)
                         .execute().data)

    enriched = 0
    for sig in signals:
        sym = sig["symbol"]
        try:
            msl_rows = (sb.table("master_shortlist")
                          .select("*").eq("date", trade_date).eq("symbol", sym)
                          .execute().data)
            msl = msl_rows[0] if msl_rows else {}

            sd_rows = (sb.table("stock_data_daily")
                         .select("*").eq("date", trade_date).eq("symbol", sym)
                         .execute().data)
            sd = sd_rows[0] if sd_rows else {}

            sector   = sig.get("sector", "") or msl.get("sector", "")
            industry = msl.get("industry", "") or ""
            lessons  = get_relevant_lessons(
                sym, sector, sig.get("strategy", ""), msl.get("lifecycle", ""), sb
            )

            # ── Per-stock context fetches ──────────────────────────────────────
            # G6: event_calendar (14-day lookahead, per-symbol + sector)
            event_cal_ctx = get_event_calendar_context(sb, sym, sector, trade_date)

            # G13: sector + industry strength
            sec_ind_ctx = get_sector_industry_context(sb, sector, industry, trade_date)

            # G18: portfolio concentration
            portfolio_ctx = get_portfolio_context(sb, sym, sector)

            # nifty_upcoming_events (quick per-symbol event lookup)
            sym_upcoming = [e.get("purpose") for e in upcoming_events if e.get("symbol") == sym]

            # ── Stock data dict (technical indicators for AI) ──────────────────
            stock_data = {
                "symbol":        sym,
                "sector":        sector,
                "industry":      industry,
                "signal_type":   sig.get("signal_type", ""),
                "final_score":   float(sig.get("score") or 0),
                "current_price": float(msl.get("current_price") or 0),
                "rsi_daily":     float(sd.get("rsi_daily") or 0),
                "rsi_weekly":    float(sd.get("rsi_weekly") or 0),
                "adx":           float(sd.get("adx") or 0),     # Fix #3: was adx_14
                "vol_ratio":     float(sd.get("vol_ratio") or 0),
                "delivery_pct":  float(sd.get("delivery_pct") or 0),
                "atr_pct":       float(sd.get("atr_pct") or 0),
                "ret_6m":        float(sd.get("ret_6m") or 0),
                "lifecycle":     msl.get("lifecycle") or "",
                "eap_action":    sig.get("eap_action") or "NO_CHANGE",
            }

            # ── AI context dict (everything the AI reasons over) ───────────────
            ai_context = {
                # Core regime (G17: full object, not just string)
                "market_regime": {
                    "regime":                regime_ctx.get("regime", "UNKNOWN"),
                    "regime_score":          regime_ctx.get("regime_score"),          # 0.0-1.0 strength
                    "nifty_1d_chg_pct":      regime_ctx.get("nifty_1d_chg_pct"),     # today's move
                    "nifty_5d_chg_pct":      regime_ctx.get("nifty_5d_chg_pct"),     # weekly momentum
                    "nifty_20d_chg_pct":     regime_ctx.get("nifty_20d_chg_pct"),    # monthly momentum
                    "india_vix":             regime_ctx.get("india_vix"),
                    "breadth_pct":           regime_ctx.get("avg_sector_breadth"),
                    "advance_decline_ratio": regime_ctx.get("advance_decline_ratio"),
                    "above_200dma_pct":      regime_ctx.get("above_200dma_pct"),
                    "predicted_regime":      regime_ctx.get("predicted_regime"),
                    "regime_confidence":     regime_ctx.get("regime_confidence"),
                },
                # FII/DII (G17: direct from fii_dii_flow, not signal_log.fii_flag)
                "fii_dii": fii_ctx,

                # Global cues (G17)
                "global_cues": global_cues,

                # Events (G6: event_calendar 14-day + nifty_upcoming)
                "upcoming_events":    event_cal_ctx,
                "nse_upcoming":       sym_upcoming,  # backward compat field

                # Sector + industry (G13)
                "sector_context":   sec_ind_ctx["sector_context"],
                "industry_context": sec_ind_ctx["industry_context"],

                # Portfolio concentration (G18)
                "portfolio": portfolio_ctx,

                # Lessons (Fix #3: is_active=True only)
                "relevant_lessons": [
                    f"{l.get('scenario_type', '')} — {l.get('root_cause', '')} "
                    f"| {l.get('corrective_rule', '')}"
                    for l in lessons
                ],
            }

            result = analyze(stock_data, ai_context)
            if result is None or result.conviction == "UNKNOWN":
                logger.debug(f"No AI conviction for {sym} — skipping enrichment")
                continue

            r = result.to_dict()

            # ── Writes ────────────────────────────────────────────────────────
            sb.table("signal_log").update({
                "ai_conviction":          r["ai_conviction"],
                "ai_note":                r["ai_note"],
                "ai_provider":            r["ai_provider"],
                "ai_strategy_validation": r.get("ai_strategy_validation"),  # G4
                "ai_suggested_action":    r.get("ai_suggested_action"),
                "ai_confidence":          r.get("ai_confidence"),
                "ai_conviction_reason":   r.get("ai_conviction_reason"),
            }).eq("id", sig["id"]).execute()

            sb.table("ai_context").upsert({
                "date":              trade_date,
                "symbol":            sym,
                "conviction":        r["ai_conviction"],
                "conviction_reason": r["ai_conviction_reason"],
                "risks":             r["ai_risks"],
                "catalyst":          r["ai_catalyst"],
                "suggested_action":  r["ai_suggested_action"],
                "strategy_validation":  r.get("ai_strategy_validation"),
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
            logger.info(
                f"  {sym} [{sig.get('signal_type')}]: {r['ai_conviction']} "
                f"({r['ai_provider']}) — {r['ai_conviction_reason'][:60]}"
            )

        except Exception as e:
            logger.warning(f"  AI enrichment failed for {sym}: {e}")

    duration = time.time() - start
    logger.success(
        f"AI enrichment: {enriched}/{len(signals)} enriched in {duration:.1f}s "
        f"[{len(exit_signals)} EXIT/ADD guaranteed + {len(buy_signals)} buys]"
    )
    return {
        "enriched":       enriched,
        "date":           trade_date,
        "exit_enriched":  len(exit_signals),
        "buy_enriched":   len(buy_signals),
    }


if __name__ == "__main__":
    main()