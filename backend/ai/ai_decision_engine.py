"""
TradeOS v6 — Step 19: AI Decision Engine
=========================================
Pipeline position: after step 18 (market_intel), final AI step of the day.

PURPOSE:
  One AI call. Full portfolio-aware ranking of all gate-passed candidates
  against today's complete market context. Produces the final actionable
  output a trader opens their terminal to see.

DESIGN PRINCIPLES:
  1. SINGLE BATCH CALL — all candidates evaluated simultaneously.
     Old Phase 1 + Phase 3 made 20+ per-stock calls with no cross-stock
     awareness. This makes 1 call and explicitly asks for group-move analysis,
     sector concentration guards, and portfolio overlap checks.

  2. RANK ALL, TIER ALL — no arbitrary top-12 cutoff.
     TIER_1 = act now | TIER_2 = watch for trigger | TIER_3 = monitor only
     On a strong day you may get 10 TIER_1s. On a weak day, 2.
     The human decides their own capacity; the AI shows true quality distribution.

  3. STEP 15 GATE WORK IS RESPECTED — candidates already passed 11 hard gates.
     Step 19 does NOT re-pick from MSL or re-run technical gates. Its job is:
     portfolio-level reasoning, macro overlay, cross-stock correlation guards,
     and conviction scoring on already-qualified setups.

  4. STEP 18 OUTPUT IS THE MARKET OVERLAY — sentiment_modifiers already applied
     to signal_log.score_adjusted by step 18. Step 19 reads the final scores
     and the full __MARKET_INTEL__ context as its market lens.

  5. SELF-IMPROVING — reads historical __FINAL_PICKS__ to compare prior
     selections against outcomes. Generates new rules when cross-stock
     patterns are detected. Lesson confidence scores drive rule weighting.

INPUTS:
  signal_log             — gate-passed candidates with score_adjusted
                           (already updated by step 18 sentiment_modifier)
  master_shortlist JOIN  — entry zones, current_price, dist_entry_pct,
                           convergence_pts, engines_count, fundamental_quality,
                           market_cap (capital sizing context)
  ai_context.__MARKET_INTEL__ — step 18 output: sizing, FII outlook,
                                 sector biases, regulatory alerts
  open_positions         — concentration guard: how many positions per sector/strategy
  lessons                — ordered by live confidence (times_worked/times_applied)
  sector_strength        — full ranking for sector cap enforcement
  macro_indicators       — recent macro context
  ai_context.__FINAL_PICKS__ (last 5d) — historical echo: what we picked vs outcome

OUTPUTS:
  signal_log       — ai_conviction, ai_confidence, ai_conviction_reason,
                     ai_suggested_action, ai_risks, ai_catalyst, ai_note,
                     ai_provider — written for ALL ranked candidates
                     ai_max_chase_pct, ai_zone_high_extended, ai_chase_rationale
                     — conviction-aware extension above compute_msl's mechanical
                     entry_zone_high (see _resolve_chase_pct). Lets a live
                     intraday check (entry_readiness.py) tell a genuinely
                     "too extended, skip" situation apart from "still above
                     the mechanical zone but AI's full-context conviction
                     supports chasing it" — which compute_msl alone cannot
                     know, since it never sees conviction/macro/FII context.
  master_shortlist — same AI fields synced
  ai_context       — symbol=__FINAL_PICKS__, full ranked JSON
  lessons          — new rules from cross-stock pattern detection

SELF-IMPROVEMENT LOOP:
  - Historical __FINAL_PICKS__ fed back into prompt
  - AI compares its prior picks against today's position/outcome data
  - Patterns that repeatedly work become high-confidence lessons
  - Step 17 (post_trade) should increment lessons.times_worked/times_applied
    when signals are acted on and outcomes are known
"""

import os
import re
import sys
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg, cfg_float, cfg_int, get_trade_date

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# Ceiling for ai_max_chase_pct (% above compute_msl's entry_zone_high that AI
# conviction may justify chasing into). Deliberately matches the 8% boundary
# compute_msl.compute_entry_timing_type() already uses to separate CHASING
# from EXTENDED — AI decides how much of that existing headroom today's
# conviction/momentum/macro context justifies; it never invents new headroom
# beyond what compute_msl itself still calls "chaseable".
MAX_CHASE_PCT_CEILING = 8.0


# ── Trading-day resolution ─────────────────────────────────────────────────

def get_last_trading_date(sb, reference_date: date | None = None) -> str:
    if reference_date is None:
        reference_date = today_ist()

    window_start = reference_date - timedelta(days=10)
    try:
        holiday_rows = (
            sb.table("nse_holidays")
              .select("date")
              .gte("date", str(window_start))
              .lte("date", str(reference_date))
              .execute().data
        )
        holiday_set = {r["date"] for r in (holiday_rows or [])}
    except Exception as exc:
        logger.warning(f"Could not fetch nse_holidays: {exc}")
        holiday_set = set()

    candidate = reference_date
    for _ in range(10):
        if candidate.weekday() >= 5 or str(candidate) in holiday_set:
            candidate -= timedelta(days=1)
            continue

        # ── KEY FIX: verify signal_log actually has data for this date ──
        # A valid trading day at 00:55 may not have pipeline data yet.
        # Roll back until we find a date where step 15 has already run.
        try:
            probe = (
                sb.table("signal_log")
                  .select("date")
                  .eq("date", str(candidate))
                  .limit(1)
                  .execute().data
            )
            if probe:
                if str(candidate) != str(reference_date):
                    logger.info(
                        f"No signal_log data for {reference_date} yet "
                        f"(pipeline not run) — using last available: {candidate}"
                    )
                return str(candidate)
        except Exception as exc:
            logger.warning(f"signal_log probe failed for {candidate}: {exc}")
            return str(candidate)   # fallback: trust the date if probe errors

        candidate -= timedelta(days=1)

    logger.error("Could not find a trading date with signal_log data in last 10 days")
    return str(reference_date)

_SYSTEM_PROMPT = (
    "You are a senior Indian equity portfolio manager with 20 years of NSE 500 swing trading experience. "
    "TARGET HORIZON: 1–3 weeks (5–15 trading sessions). Every ranking and allocation decision must be "
    "calibrated to this window — a setup that takes 6 weeks to play out is NOT TIER_1 for us. "
    "You think in terms of capital deployment, risk-adjusted returns, and portfolio construction — "
    "not just individual stock setups. "
    "You are reading a fully enriched, gate-filtered candidate list. Every stock here already passed "
    "11 technical gates. Your job is NOT to re-screen them. Your job is to apply portfolio-level thinking: "
    "sector concentration, FII alignment, macro tailwinds, correlation clusters, and capital efficiency. "
    "Prefer candidates with: low days_to_trigger_est (≤3 sessions), strong rs_vs_nifty, "
    "positive ret_6m momentum, and high holding_score — these signal a stock primed to move in 1–3 weeks. "
    "When you see a group of 4 similar stocks, you cap them to the best 1-2. "
    "When FII is selling a sector, you drop those candidates regardless of their technical score. "
    "You generate new rules when you detect patterns that should be remembered. "
    "Output ONLY valid JSON — no preamble, no markdown."
    "PRICE RULE: Every price you write in thesis, entry_note, and invalidation "
    "MUST come from the CMP and Zone values shown in the candidate data. "
    "Never use prices from training memory. If CMP or Zone is missing for a candidate, "
    "assign it TIER_3/SKIP — do not invent a price. "
    "Output ONLY valid JSON — no preamble, no markdown."
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _lesson_confidence(lesson: dict) -> float:
    applied = lesson.get("times_applied") or 0
    worked  = lesson.get("times_worked")  or 0
    if applied < 3:
        return round(float(lesson.get("confidence") or 0.5), 2)
    return round(worked / applied, 2)


# ── Data loading ───────────────────────────────────────────────────────────

def load_context(sb, trade_date: str) -> dict:
    """
    Load all inputs for the decision engine.
    Candidates come from signal_log (already sentiment-adjusted by step 18).
    """

    # ── Gate-passed candidates with step 18 sentiment applied ──
    sig_rows = (
        sb.table("signal_log")
          .select(
              "id,symbol,company_name,sector,industry,strategy,"
              "signal_type,signal_subtype,position_state,"
              "score,score_adjusted,"
              # Momentum & structure
              "momentum_state,momentum_phase,velocity_state,trend_maturity,"
              "lifecycle,struct_edge,entry_timing_type,reentry_mode,"
              # Sub-scores
              "holding_score,momentum_score,institutional_score,"
              "breakout_readiness,risk_score,"
              # Technical indicators
              "rsi_daily,rsi_weekly,adx,vol_ratio,atr_pct,ret_6m,"
              "rs_vs_nifty,above_sma50,delivery_pct,"
              "bb_squeeze,bb_context,vwap_alignment,macd_direction,"
              "weekly_structure,psar_dual_confirmed,ha_signal,st_cushion_pct,"
              # Context fields
              "validity_score,expected_r_msl,days_to_trigger_est,"
              "fii_flag,sector_rank_at_entry,industry_rank,industry_top5,"
              "eap_action,in_rule_engine,in_scanner,"
              "india_vix,nifty_5d_chg_pct,fii_net_20d_ctx,"
              "filter_reason,ai_note"
          )
          .eq("date", trade_date)
          .in_("signal_type", [
              "PRIME_SETUP", "BREAKOUT_SETUP", "REENTRY_SETUP",
              "BUY_CANDIDATE", "STAGED_ENTRY","MARKET_TOP_PICK",
          ])
          .order("score_adjusted", desc=True)
          .execute().data
    )
    
    promoted_from_watch = False
    if not sig_rows:
        logger.warning(
            f"No gate-passed buy signals for {trade_date} — "
            f"falling back to near-miss WATCH signals"
        )
        sig_rows = (
            sb.table("signal_log")
            .select(
                "id,symbol,company_name,sector,industry,strategy,"
                "signal_type,signal_subtype,position_state,"
                "score,score_adjusted,"
                "momentum_state,momentum_phase,velocity_state,trend_maturity,"
                "lifecycle,struct_edge,entry_timing_type,reentry_mode,"
                "holding_score,momentum_score,institutional_score,"
                "breakout_readiness,risk_score,"
                "rsi_daily,rsi_weekly,adx,vol_ratio,atr_pct,ret_6m,"
                "rs_vs_nifty,above_sma50,delivery_pct,"
                "bb_squeeze,bb_context,vwap_alignment,macd_direction,"
                "weekly_structure,psar_dual_confirmed,ha_signal,st_cushion_pct,"
                "validity_score,expected_r_msl,days_to_trigger_est,"
                "fii_flag,sector_rank_at_entry,industry_rank,industry_top5,"
                "eap_action,in_rule_engine,in_scanner,near_miss_data,filter_reason"
            )
            .eq("date", trade_date)
            .eq("signal_type", "WATCH")
            .not_.is_("near_miss_data", "null")
            .order("score_adjusted", desc=True)
            .limit(20).execute().data
        )
        promoted_from_watch = True
        if len(sig_rows) == 20:
            logger.warning(
                "WATCH fallback hit limit of 20 — there may be more near-miss "
                "signals not being ranked. Consider raising the limit."
            )
        if not sig_rows:
            logger.warning("No near-miss WATCH signals either — step 19 has nothing to rank")
            return {}

    # ── JOIN with master_shortlist for fields not in signal_log ──
    symbols = [r["symbol"] for r in sig_rows]
    msl_rows = (
        sb.table("master_shortlist")
          .select(
              "symbol,current_price,entry_zone_low,entry_zone_high,"
              "dist_entry_pct,convergence_pts,engines_count,"
              "fundamental_quality,market_cap,st_cushion_pct,"
              "bb_width_pct,bb_position_pct,ma_alignment_score,"
              "stoch_context,persistent_phase,suggested,notes,"
              "days_in_list,rank_vel_3d,score_vel_5d,"
              "dist_sma50,ret_3m,ret_12m,low_52w,pct_change,"
              "low_30d,consol_range,zone_hot_adjusted"
          )
          .eq("date", trade_date)
          .in_("symbol", symbols)
          .execute().data
    )
    msl_map = {r["symbol"]: r for r in msl_rows}

    # ── Upcoming corporate events for signal candidates ──
    # NOTE: this table (nifty_upcoming_events) carries NO polarity — purpose is
    # things like "Financial Results" or "Fund Raising", with no signal for
    # whether the outcome is likely good or bad. That's a genuine data
    # limitation, not a bug — earnings can beat or miss, we don't know which
    # in advance — so keeping this one blanket-cautious below is correct.
    try:
        event_rows = (
            sb.table("nifty_upcoming_events")
              .select("symbol,purpose,days_to_event")
              .in_("symbol", symbols)
              .gte("days_to_event", 0)
              .lte("days_to_event", 10)
              .order("days_to_event")
              .execute().data
        )
        event_map = {r["symbol"]: r for r in event_rows}
    except Exception as e:
        logger.warning(f"nifty_upcoming_events load failed (non-fatal): {e}")
        event_map = {}

    # ── Sector-level event_calendar — 2026-07 addition ──────────────────────
    # This is the ONE event source that actually carries directional signal
    # (event_bias, event_intensity) plus qualitative strategy_impact — but
    # until now it only ever reached step 18 (market_intelligence_engine) as
    # a flat, un-joined list. Step 19 — the engine that actually assigns
    # tier/action/allocation per stock — had zero visibility into it, which
    # is why the old CAP rule below could only ever be "cap on ANY nearby
    # event" (uniformly defensive) rather than "cap on nearby BAD news,
    # treat nearby GOOD news as a tailwind" (what event tracking is for).
    # Joined by sector match, same 10-day forward window as nifty_upcoming_events
    # above for consistency. is_active=True mirrors market_intelligence_engine's
    # own event_calendar query.
    try:
        cal_cutoff = str(date.fromisoformat(trade_date) + timedelta(days=10))
        cal_rows = (
            sb.table("event_calendar")
              .select("event_name,event_type,affected_sectors,event_bias,"
                      "event_intensity,strategy_impact,start_date,end_date")
              .eq("is_active", True)
              .lte("start_date", cal_cutoff)
              .order("start_date")
              .execute().data
        )
    except Exception as e:
        logger.warning(f"event_calendar load failed (non-fatal): {e}")
        cal_rows = []

    def _sector_event_for(sector: str) -> dict | None:
        """Nearest active event_calendar row whose affected_sectors list
        contains this sector, within the forward window above. Deliberately
        NO lower bound on start_date (matches market_intelligence_engine's
        own event_calendar query) — event_calendar rows are start/end DATE
        RANGES, not single-point dates, so a long-running event that started
        weeks ago (Monsoon, AGM Season, an INR depreciation window) is often
        MORE relevant than one that hasn't started yet, not less. Defensively
        re-checks end_date >= trade_date in case is_active is stale.
        Simple case-insensitive membership match against the comma-separated
        affected_sectors string — no fuzzy matching, so sector names must
        match your event_calendar rows' own convention (e.g. 'healthcare',
        'i.t', 'power & utilities') to be picked up."""
        if not sector:
            return None
        sector_l = sector.strip().lower()
        matches = [
            r for r in cal_rows
            if sector_l in [s.strip().lower() for s in (r.get("affected_sectors") or "").split(",")]
            and (not r.get("end_date") or r["end_date"] >= trade_date)
        ]
        return matches[0] if matches else None  # already ordered by start_date

    candidates = []
    for sig in sig_rows:
        sym = sig["symbol"]
        msl = msl_map.get(sym, {})
        merged = {**sig}
        merged["current_price"]      = msl.get("current_price")
        merged["entry_zone_low"]     = msl.get("entry_zone_low")
        merged["entry_zone_high"]    = msl.get("entry_zone_high")
        merged["dist_entry_pct"]     = msl.get("dist_entry_pct")
        merged["convergence_pts"]    = msl.get("convergence_pts")
        merged["engines_count"]      = msl.get("engines_count")
        merged["fundamental_quality"]= msl.get("fundamental_quality")
        merged["market_cap"]         = msl.get("market_cap")
        merged["bb_width_pct"]       = msl.get("bb_width_pct")
        merged["ma_alignment_score"] = msl.get("ma_alignment_score")
        merged["days_in_list"]       = msl.get("days_in_list")
        merged["rank_vel_3d"]        = msl.get("rank_vel_3d")
        merged["score_vel_5d"]       = msl.get("score_vel_5d")
        merged["dist_sma50"]         = msl.get("dist_sma50")
        merged["ret_3m"]             = msl.get("ret_3m")
        merged["ret_12m"]            = msl.get("ret_12m")
        merged["low_52w"]            = msl.get("low_52w")
        merged["pct_change"]         = msl.get("pct_change")
        merged["low_30d"]           = msl.get("low_30d")
        merged["consol_range"]      = msl.get("consol_range")
        merged["zone_hot_adjusted"] = msl.get("zone_hot_adjusted")
        ev = event_map.get(sym)
        merged["upcoming_event_type"] = ev["purpose"]       if ev else None
        merged["upcoming_event_days"] = ev["days_to_event"] if ev else None
        sec_ev = _sector_event_for(sig.get("sector"))
        if sec_ev:
            merged["sector_event_name"]      = sec_ev.get("event_name")
            merged["sector_event_bias"]      = sec_ev.get("event_bias")
            merged["sector_event_intensity"] = sec_ev.get("event_intensity")
            merged["sector_event_days"]      = (
                date.fromisoformat(sec_ev["start_date"]) - date.fromisoformat(trade_date)
            ).days
        else:
            merged["sector_event_name"]      = None
            merged["sector_event_bias"]      = None
            merged["sector_event_intensity"] = None
            merged["sector_event_days"]      = None
        candidates.append(merged)

    # ── Market intel from step 18 ──
    intel_rows = (
        sb.table("ai_context")
          .select("conviction,conviction_reason,catalyst,suggested_action,ai_note,risks")
          .eq("date", trade_date)
          .eq("symbol", "__MARKET_INTEL__")
          .limit(1).execute().data
    )
    market_intel = {}
    if intel_rows:
        r = intel_rows[0]
        try:
            full = json.loads(r.get("conviction_reason") or "{}")
        except Exception:
            full = {}
        market_intel = {
            "position_sizing":       r.get("suggested_action"),
            "summary":               r.get("ai_note", "")[:300],
            "setup_types_favoured":  full.get("market_tone", {}).get("setup_types_favoured", []),
            "setup_types_avoid":     full.get("market_tone", {}).get("setup_types_to_avoid", []),
            "fii_5session_bias":     full.get("fii_outlook", {}).get("5session_bias"),
            "fii_favoured_sectors":  full.get("fii_outlook", {}).get("favoured_sectors", []),
            "fii_exit_sectors":      full.get("fii_outlook", {}).get("exit_sectors", []),
            "fii_confidence":        full.get("fii_outlook", {}).get("confidence"),
            "fii_key_signal":        full.get("fii_outlook", {}).get("key_signal_to_watch"),
            "macro_impacts":         full.get("macro_sector_impacts", []),
            "regulatory_alerts":     full.get("regulatory_alerts", []),
            "echo_comparison":       full.get("market_tone", {}).get("echo_comparison", ""),
            "dii_signal":            full.get("dii_outlook", {}).get("5session_signal"),
            "dii_absorbing":         full.get("dii_outlook", {}).get("absorbing_fii_selling"),
        }

    # ── Open positions — sector/strategy concentration ──
    pos_rows = (
        sb.table("open_positions")
          .select("symbol,sector,strategy,invested_value,pnl_pct,active_sl,lifecycle")
          .eq("status", "ACTIVE").execute().data
    )

    # 2026-07: regulatory_alerts cross-reference. Previously a flat,
    # un-joined list at the top of the prompt (from market_news via step 18) —
    # confirmed via post_trade_analysis.py that nothing else checks this
    # against live positions either; that file only ever processes already-
    # CLOSED trades (closed_positions, filtered by exit_date) for retrospective
    # lesson generation, never currently-open ones. So this was a genuine gap,
    # not something duplicated elsewhere. affected_symbols checked first
    # (more specific), affected_sectors as fallback.
    def _regulatory_alert_for(symbol: str, sector: str) -> dict | None:
        for alert in (market_intel.get("regulatory_alerts") or []):
            if symbol and symbol in (alert.get("affected_symbols") or []):
                return alert
            if sector and sector in (alert.get("affected_sectors") or []):
                return alert
        return None

    # Candidates were built above, before market_intel (this section) loaded —
    # second pass to inject the regulatory_alerts cross-reference now that
    # it's available. Loop cost is negligible (candidate counts are small).
    for c in candidates:
        alert = _regulatory_alert_for(c.get("symbol"), c.get("sector"))
        c["regulatory_alert_action"] = alert.get("action")    if alert else None
        c["regulatory_alert_note"]   = alert.get("news_item") if alert else None

    # nifty_upcoming_events for held positions too — event_map above is only
    # ever queried against candidate symbols (new entries). Small supplementary
    # query for position symbols rather than restructuring the existing one.
    pos_symbols = [p["symbol"] for p in pos_rows]
    if pos_symbols:
        try:
            pos_event_rows = (
                sb.table("nifty_upcoming_events")
                  .select("symbol,purpose,days_to_event")
                  .in_("symbol", pos_symbols)
                  .gte("days_to_event", 0)
                  .lte("days_to_event", 10)
                  .execute().data
            )
            pos_event_map = {r["symbol"]: r for r in pos_event_rows}
        except Exception as e:
            logger.warning(f"nifty_upcoming_events (positions) load failed (non-fatal): {e}")
            pos_event_map = {}
    else:
        pos_event_map = {}

    # 2026-07: same sector-event join as candidates above. This section was
    # previously concentration-context only (new-entry sector caps) with zero
    # event awareness for stock you're actually holding — a position sitting
    # through a HIGH-intensity NEGATIVE sector event 2 days out was invisible
    # here. _sector_event_for is defined below with the candidates join;
    # applied here too so held positions get the same visibility.
    for p in pos_rows:
        sec_ev = _sector_event_for(p.get("sector"))
        if sec_ev:
            p["sector_event_name"]      = sec_ev.get("event_name")
            p["sector_event_bias"]      = sec_ev.get("event_bias")
            p["sector_event_intensity"] = sec_ev.get("event_intensity")
            p["sector_event_days"]      = (
                date.fromisoformat(sec_ev["start_date"]) - date.fromisoformat(trade_date)
            ).days
        else:
            p["sector_event_name"] = p["sector_event_bias"] = None
            p["sector_event_intensity"] = p["sector_event_days"] = None
        ev = pos_event_map.get(p.get("symbol"))
        p["upcoming_event_type"] = ev["purpose"]       if ev else None
        p["upcoming_event_days"] = ev["days_to_event"] if ev else None
        alert = _regulatory_alert_for(p.get("symbol"), p.get("sector"))
        p["regulatory_alert_action"] = alert.get("action")    if alert else None
        p["regulatory_alert_note"]   = alert.get("news_item") if alert else None
    sector_counts   = {}
    strategy_counts = {}
    for p in pos_rows:
        sec  = p.get("sector", "Unknown")
        strat = p.get("strategy", "Unknown")
        sector_counts[sec]    = sector_counts.get(sec, 0) + 1
        strategy_counts[strat] = strategy_counts.get(strat, 0) + 1

    # ── Full sector ranking for concentration cap ──
    sector_rows = (
        sb.table("sector_strength")
          .select("sector,rank,top4_flag,sector_state,avg_rsi_daily,fii_flow_sector")
          .eq("date", trade_date).order("rank").execute().data
    )
    if not sector_rows:
        latest = (
            sb.table("sector_strength")
              .select("date")
              .order("date", desc=True)
              .limit(1).execute().data
        )
        if latest:
            sector_rows = (
                sb.table("sector_strength")
                  .select("sector,rank,top4_flag,sector_state,avg_rsi_daily,fii_flow_sector")
                  .eq("date", latest[0]["date"])
                  .order("rank").execute().data
            )

    # ── Lessons — sorted by live confidence ──
    lesson_rows = (
        sb.table("lessons")
          .select("id,corrective_rule,scenario_type,impacted_sector,"
                  "times_applied,times_worked,confidence,source")
          .eq("is_active", True)
          .execute().data          # ← no limit, no order — Python will do both
    )
    for l in lesson_rows:
        l["live_confidence"] = _lesson_confidence(l)
    lesson_rows.sort(key=lambda x: x["live_confidence"], reverse=True)
    lesson_rows = lesson_rows[:12]

    # ── Macro indicators — recent ──
    macro_cutoff = str(date.today() - timedelta(days=10))
    macro_rows = (
        sb.table("macro_indicators")
          .select("indicator_date,indicator_name,indicator_value,previous_value,change_bps")
          .gte("indicator_date", macro_cutoff)
          .order("indicator_date", desc=True).execute().data
    )

    # ── Historical echoes: last 5 __FINAL_PICKS__ with outcome context ──
    # Lets AI see what it picked previously and whether those trades worked
    echo_rows = (
        sb.table("ai_context")
          .select("date,conviction_reason,ai_note")
          .eq("symbol", "__FINAL_PICKS__")
          .order("date", desc=True).limit(5).execute().data
    )
    echoes = []
    for row in echo_rows:
        try:
            full = json.loads(row.get("conviction_reason") or "{}")
            picks = full.get("ranked_candidates") or []
            # Get outcomes for those symbols from signal_log
            pick_symbols = [p["symbol"] for p in picks[:5] if p.get("symbol")]
            outcomes = {}
            if pick_symbols:
                out_rows = (
                    sb.table("signal_log")
                      .select("symbol,outcome,outcome_pnl_pct")
                      .eq("date", row["date"])
                      .in_("symbol", pick_symbols)
                      .execute().data
                )
                outcomes = {o["symbol"]: o for o in out_rows}

            echoes.append({
                "date":  row["date"],
                "note":  row.get("ai_note", "")[:150],
                "top_picks": [
                    {
                        "symbol":   p.get("symbol"),
                        "tier":     p.get("tier"),
                        "action":   p.get("action"),
                        "outcome":  outcomes.get(p.get("symbol"), {}).get("outcome"),
                        "pnl_pct":  outcomes.get(p.get("symbol"), {}).get("outcome_pnl_pct"),
                    }
                    for p in picks[:5]
                ],
            })
        except Exception:
            echoes.append({"date": row["date"], "note": row.get("ai_note", "")[:150]})

    # WATCH signals that step 18 flagged as near-miss upgrades
    watch_upgrades = (
        sb.table("signal_log")
          .select("symbol,sector,score_adjusted,near_miss_data,ai_note,filter_reason,"
                  "momentum_state,lifecycle,fii_flag,sector_rank_at_entry")
          .eq("date", trade_date)
          .eq("signal_type", "WATCH")
          .like("ai_note", "%NEAR_MISS_UPGRADE%")
          .execute().data
    )
    
    return {
        "candidates":      candidates,
        "promoted_from_watch": promoted_from_watch,
        "market_intel":    market_intel,
        "positions":       pos_rows,
        "sector_counts":   sector_counts,
        "strategy_counts": strategy_counts,
        "sectors":         sector_rows,
        "lessons":         lesson_rows,
        "macro":           macro_rows,
        "echoes":          echoes,
        "watch_upgrades":  watch_upgrades,
    }


# ── Prompt builder ─────────────────────────────────────────────────────────

def build_prompt(ctx: dict, trade_date: str) -> str:
    mi  = ctx["market_intel"]
    pos = ctx["positions"]

    lines = [
        f"DATE: {trade_date}",
        "",
        "═══ MARKET OVERLAY (from Step 18 Market Intelligence) ═══",
        f"  Sizing guidance: {mi.get('position_sizing','?')}",
        f"  Summary: {mi.get('summary','?')}",
        f"  Echo comparison: {mi.get('echo_comparison','N/A')[:200]}",
        f"  Setup types FAVOURED today: {mi.get('setup_types_favoured',[])}",
        f"  Setup types to AVOID today: {mi.get('setup_types_avoid',[])}",
        "",
        "  FII Outlook (5-session):",
        f"    Bias: {mi.get('fii_5session_bias','?')} | Confidence: {mi.get('fii_confidence','?')}",
        f"    Buying into: {mi.get('fii_favoured_sectors',[])}",
        f"    Selling out of: {mi.get('fii_exit_sectors',[])}",
        f"    Key signal to watch: {mi.get('fii_key_signal','?')}",
        f"    DII 5-session: {mi.get('dii_signal','?')} | Absorbing FII selling: {mi.get('dii_absorbing','?')}",
    ]

    if mi.get("macro_impacts"):
        lines.append("  Macro sector impacts:")
        for imp in (mi.get("macro_impacts") or [])[:4]:
            lines.append(
                f"    {imp.get('driver','?')} | Tailwind:{imp.get('tailwind_sectors',[])} | "
                f"Headwind:{imp.get('headwind_sectors',[])} | {imp.get('magnitude','?')} | "
                f"{imp.get('sessions','?')} sessions"
            )

    if mi.get("regulatory_alerts"):
        lines.append("  Regulatory alerts:")
        for alert in (mi.get("regulatory_alerts") or [])[:5]:
            if alert.get("action") not in ("NO_ACTION", None):
                lines.append(
                    f"    [{alert.get('urgency','?')}] {alert.get('action','?')} — "
                    f"{alert.get('news_item','?')[:100]} | "
                    f"Symbols:{alert.get('affected_symbols',[])} | "
                    f"Sectors:{alert.get('affected_sectors',[])} "
                )

    if ctx["macro"]:
        lines += ["", "═══ MACRO INDICATORS ═══"]
        for m in ctx["macro"]:
            delta = f" Δ{float(m.get('change_bps',0) or 0):+.0f}bps" if m.get("change_bps") else ""
            lines.append(
                f"  {m.get('indicator_date','?')} {m.get('indicator_name','?')}: "
                f"{m.get('indicator_value','?')} (prev:{m.get('previous_value','?')}){delta}"
            )

    lines += ["", "═══ OPEN POSITIONS (concentration context) ═══"]
    lines.append(f"  Total open: {len(pos)}")
    if pos:
        lines.append(f"  Sector concentration: {ctx['sector_counts']}")
        lines.append(f"  Strategy concentration: {ctx['strategy_counts']}")
        # 2026-07: sector_event_* / upcoming_event_* / regulatory_alert_* all
        # added for visibility — this module has no exit-action output field
        # (ranked_candidates is new-entries only), so this is informational
        # context for your thesis/risk narrative and for weighing concentration
        # guards, not an automated trim/exit signal. See conversation notes:
        # confirmed via post_trade_analysis.py that no other module checks
        # any of these three sources against currently-open positions either.
        for p in pos:
            _pse_days = p.get("sector_event_days")
            _pse_str  = ""
            if _pse_days is not None:
                _pse_when = f"in {_pse_days}d" if _pse_days >= 0 else f"ONGOING ({-_pse_days}d in)"
                _pse_str = (
                    f" | 📅{p.get('sector_event_bias','?')}/{p.get('sector_event_intensity','?')} "
                    f"\"{p.get('sector_event_name','?')}\" {_pse_when}"
                )
            _pev_days = p.get("upcoming_event_days")
            _pev_str  = (
                f" | ⚠️{p.get('upcoming_event_type','?')} in {_pev_days}d"
                if _pev_days is not None else ""
            )
            _pra_action = p.get("regulatory_alert_action")
            _pra_str = (
                f" | 📰{_pra_action}:\"{(p.get('regulatory_alert_note') or '')[:50]}\""
                if _pra_action and _pra_action != "NO_ACTION" else ""
            )
            lines.append(
                f"  {p.get('symbol','?')} | {p.get('sector','?')} | "
                f"P&L:{float(p.get('pnl_pct') or 0):+.1f}% | SL:{p.get('active_sl','?')} | "
                f"Lifecycle:{p.get('lifecycle','?')}{_pse_str}{_pev_str}{_pra_str}"
            )
        lines.append("  → Second task: see POSITION ACTIONS GUIDANCE below")
    else:
        lines.append("  No open positions — full capital available")

    lines += ["", "═══ SECTOR STRENGTH RANKING ═══"]
    for s in ctx["sectors"]:
        top      = "★" if s.get("top4_flag") else " "
        fii_flow = f"FII:₹{float(s.get('fii_flow_sector',0) or 0):+,.0f}Cr" if s.get("fii_flow_sector") else ""
        lines.append(
            f"  {top} #{str(s.get('rank','?')):>2} {s.get('sector','?'):<32} "
            f"{s.get('sector_state','?'):<12} RSI-D:{s.get('avg_rsi_daily','?')} {fii_flow}"
        )

    lines += ["", "═══ ACTIVE LESSONS (confidence-weighted) ═══"]
    for l in ctx["lessons"]:
        conf    = l.get("live_confidence", 0)
        applied = l.get("times_applied") or 0
        lines.append(
            f"  [{l.get('scenario_type','?')}] CONF:{conf:.0%} ({applied} uses) "
            f"| {l.get('corrective_rule','')[:170]}"
        )

    if ctx["echoes"]:
        lines += ["", "═══ YOUR PRIOR PICKS — COMPARE TO OUTCOMES ═══"]
        lines.append("  Use this to detect patterns: what tier of pick worked? which sectors failed?")
        for e in ctx["echoes"]:
            lines.append(f"  {e.get('date','?')}: {e.get('note','')[:120]}")
            for p in (e.get("top_picks") or []):
                outcome_str = (
                    f"→ {p.get('outcome','pending')} {float(p.get('pnl_pct') or 0):+.1f}%"
                    if p.get("outcome") else "→ outcome pending"
                )
                lines.append(
                    f"    {p.get('symbol','?')} [{p.get('tier','?')}] {p.get('action','?')} {outcome_str}"
                )

    _COLUMNS = (
        "  Line 1 — Identity + Price + Position:\n"
        "    Symbol | Sector | SignalType | Score | CMP | Zone(entry) | "
        "Dist% | DaysTrig | Lifecycle/Maturity | HoldScore | ExpR | Engines | "
        "SectorRk | FIIFlag | MCap | Quality\n"
        "  Line 2 — Technicals + Momentum (all from today's Supabase data):\n"
        "    Momentum(state/phase/velocity) | StructEdge | EntryTiming | "
        "RSI-D | RSI-W | ADX | RSvNifty | Ret6M | Vol | Del% | "
        "BrkReadiness | MomScore | InstScore | ST% | "
        "MACD | BB | WeeklyStr | PSAR | ScoreVel | RkVel"
    )

    if ctx.get("promoted_from_watch"):
        lines += [
            "",
            "═══ NEAR-MISS WATCH SIGNALS — PROMOTED FOR RANKING (no gate-passed candidates today) ═══",
            "  Market is likely in a broad uptrend — these stocks passed scoring but were blocked",
            "  by ATR chase distance. near_miss_data shows exactly how far above zone each is.",
            "  TARGET HORIZON: 1–3 weeks. Evaluate whether today's FII/macro justifies entry.",
            "  Your task:",
            "    1. For each: check if HOT+EXPANSION+FII_BUYING+SectorTop4 → flag TIER_1",
            "    2. Apply macro overlay and concentration guards as normal",
            "    3. Set upgraded_from_watch: true on all entries in ranked_candidates",
            "    4. Use near_miss_data.gap_atrs to judge how far extended — smaller gap = better",
            "    5. Assign SKIP if macro does not support chasing at this distance",
            "    6. Set max_chase_pct per CHASE GUIDANCE below",
            "",
            _COLUMNS,
        ]
    else:
        lines += [
            "",
            "═══ GATE-PASSED CANDIDATES — YOUR RANKING TASK ═══",
            "  IMPORTANT: These stocks already passed 11 hard technical gates (step 15).",
            "  Do NOT re-evaluate their technical merits — step 15 already did that.",
            "  TARGET HORIZON: 1–3 weeks. Favour low DaysTrig (≤3), strong RSvNifty, high HoldScore.",
            "  Your task:",
            "    1. Apply the market overlay above (FII, macro, regulatory alerts)",
            "    2. Detect correlation clusters (cap clustered sectors/styles)",
            "    3. Apply concentration guards against open positions",
            "    4. Apply lessons in confidence order",
            "    5. Assign TIER_1/2/3 based on setup quality + macro alignment + 1-3wk readiness",
            "    6. Suggest capital allocation % (soft guidance, human decides final amount)",
            "    7. Rank within each tier by conviction",
            "    8. Apply event awareness per EVENT GUIDANCE below (NOT a uniform cap)",
            "    9. Set max_chase_pct per CHASE GUIDANCE below",
            "",
            _COLUMNS,
        ]

    lines += [
        "",
        "═══ EVENT GUIDANCE (2026-07 — replaces uniform EventRisk cap) ═══",
        "  Two different event flags appear on each candidate line above — treat",
        "  them differently, they carry different amounts of information:",
        "",
        "  ⚠️EventRisk (company-specific, from nifty_upcoming_events — e.g. earnings,",
        "  board meetings, fund-raising). This carries NO polarity — we cannot know",
        "  in advance whether results beat or miss. Keep this one blanket-cautious:",
        "    - Present + ≤5d → cap TIER_2 or mandate half-size, same as before.",
        "    - This is a genuine data limitation, not something to reason around —",
        "      don't infer a direction from the purpose text (e.g. 'Fund Raising' is",
        "      not inherently bad news; don't invent a bias that isn't in the data).",
        "",
        "  📅SectorEvent (sector-level, from event_calendar — DOES carry event_bias",
        "  and event_intensity, e.g. RBI MPC, monsoon, FII flow windows, festive season).",
        "  Use the bias, don't just react to presence:",
        "    - NEGATIVE + MEDIUM/HIGH intensity + ≤5d → cap TIER_2 or half-size,",
        "      same spirit as the old rule, now correctly targeted at bad news only.",
        "    - POSITIVE + MEDIUM/HIGH intensity → this is what event tracking is",
        "      FOR — cite it as a supporting catalyst in thesis/catalyst fields,",
        "      do NOT cap. A sector tailwind landing during your 1-3wk hold is a",
        "      reason for conviction, not caution.",
        "    - MIXED or LOW intensity, either polarity → contextual awareness only,",
        "      no automatic cap or boost — mention in thesis only if it changes the",
        "      picture, most of the time it won't.",
        "    - Both flags present on the same stock → weigh independently; a",
        "      NEGATIVE sector event does not need company-specific EventRisk to",
        "      justify caution, and a clean SectorEvent does not offset a genuine",
        "      near-term earnings date.",
        "",
        "  📰RegAlert (news-derived, from market_news via step 18 — action is",
        "  WATCH/EXIT/AVOID_NEW_ENTRY/NO_ACTION, NO_ACTION items are already filtered",
        "  out of what you see):",
        "    - On a CANDIDATE: AVOID_NEW_ENTRY → treat as a hard SKIP regardless of",
        "      how good the technicals look — step 18 already decided this specific",
        "      news makes a new entry unwise. WATCH → contextual caution, not a cap.",
        "    - On an OPEN POSITION: use the position_actions output below — this used",
        "      to be narrative-only with nothing downstream able to act on it; it now",
        "      has a real output field, see POSITION ACTIONS GUIDANCE.",
    ]

    lines += [
        "",
        "═══ POSITION ACTIONS GUIDANCE (position_actions field, 2026-07) ═══",
        "  This is a SECOND, SEPARATE task from ranking candidates above — review",
        "  each OPEN POSITION for whether today's context (SectorEvent/EventRisk/",
        "  RegAlert flags shown on it, or a clear conviction reversal vs. its",
        "  original thesis) warrants action. This is NOT the same thing as the",
        "  existing action_required/exit_signal/event_risk fields you may see",
        "  elsewhere in this system (those come from a different, mechanical/",
        "  price-based process) — this is your own independent news/event-aware",
        "  judgment, kept as a clearly separate field so it can never silently",
        "  overwrite or conflict with that other process.",
        "  Rules:",
        "    - SPARSE OUTPUT ONLY. Omit any position with nothing worth flagging —",
        "      do NOT emit a NO_ACTION entry for every comfortable position, that's",
        "      pure noise. An empty position_actions list on a quiet day is correct.",
        "    - Only include a position when you can name the SPECIFIC flag or",
        "      reversal driving it — 'reason' must reference something concrete",
        "      that's actually visible above (a named SectorEvent, EventRisk,",
        "      RegAlert, or a stated change from the position's own thesis).",
        "    - EXIT / TRIM → confidence should reflect how directly the event maps",
        "      to this specific stock (symbol-matched RegAlert > sector-matched >",
        "      general macro). Vague unease is not enough for EXIT.",
        "    - TIGHTEN_SL → use when the case for the position hasn't broken down",
        "      but near-term risk has clearly increased (e.g. a NEGATIVE sector",
        "      event landing mid-hold) — a smaller ask than EXIT/TRIM.",
        "    - This is currently advisory only — written to open_positions for a",
        "      human to review, not something that executes automatically.",
    ]

    lines += [
        "",
        "═══ CHASE GUIDANCE (max_chase_pct field) ═══",
        "  compute_msl's Zone(entry) above is a MECHANICAL zone — ATR/anchor-based,",
        "  zero awareness of conviction, momentum quality, or today's FII/sector tailwind.",
        "  A live intraday check re-uses this same zone hours later with no further judgment,",
        "  so a stock that gaps or rallies a few % above Zone-high gets flatly treated as",
        "  'missed' even when everything that actually drives conviction still supports it.",
        "  max_chase_pct is how you fix that: the % ABOVE Zone-high (0 to 8, matching",
        "  compute_msl's own CHASING/EXTENDED boundary — you decide how much of that existing",
        "  headroom today's setup earns, never invent more) that this specific stock's setup",
        "  justifies still entering at, for a 1-3wk swing with a correspondingly tighter stop.",
        "    - Only meaningful for action=ENTER_NOW/ENTER_ON_DIP. SKIP/WAIT_FOR_TRIGGER → 0.",
        "    - Entry:EXTENDED or Entry:WAIT in the data above → 0 (compute_msl already says",
        "      this setup is too far gone; do not override that mechanical read).",
        "    - Entry:CHASING/OPTIMAL/REENTRY + HIGH conviction + ACCELERATING/EXPANSION + ",
        "      struct_edge YES + supportive FII/sector tailwind → can justify the upper end (5-8).",
        "    - Weak/MEDIUM conviction or only some signals aligned → keep it small (1-3) or 0.",
        "    - HotAdj:Y means compute_msl's own zone math already detected 3+ acceleration",
        "      signals yesterday and pulled the zone toward that close — treat as one",
        "      supporting data point for chasing today, never sufficient on its own.",
        "    - chase_rationale: one short phrase naming the specific signals that justify it",
        "      (or say so plainly when the answer is 0 — e.g. 'EXTENDED per compute_msl').",
    ]

    for c in ctx["candidates"]:
        # Line 1: identity, price, position sizing context
        _ev_days = c.get("upcoming_event_days")
        _ev_str  = (
            f" | ⚠️EventRisk:{c.get('upcoming_event_type','?')} in {_ev_days}d"
            if _ev_days is not None and _ev_days <= 7 else ""
        )
        # SectorEvent — unlike EventRisk above, this one carries bias + intensity
        # (from event_calendar), so it's shown whenever present, not just ≤7d —
        # a POSITIVE event further out can still be worth citing as a tailwind.
        # days can be negative (event already started, still active/ongoing —
        # e.g. day 12 of a 3-month Monsoon window) — shown as ONGOING, not "-12d".
        _se_days = c.get("sector_event_days")
        _se_str  = ""
        if _se_days is not None:
            _se_when = f"in {_se_days}d" if _se_days >= 0 else f"ONGOING ({-_se_days}d in)"
            _se_str = (
                f" | 📅SectorEvent:{c.get('sector_event_bias','?')}/"
                f"{c.get('sector_event_intensity','?')} \"{c.get('sector_event_name','?')}\" {_se_when}"
            )
        # RegAlert — news-derived (market_news → step 18 regulatory_alerts),
        # now cross-referenced per-candidate instead of sitting as a flat,
        # easy-to-miss list at the top of the prompt. action is one of
        # WATCH | EXIT | AVOID_NEW_ENTRY | NO_ACTION — see EVENT GUIDANCE.
        _ra_action = c.get("regulatory_alert_action")
        _ra_str = (
            f" | 📰RegAlert:{_ra_action} \"{(c.get('regulatory_alert_note') or '')[:60]}\""
            if _ra_action and _ra_action != "NO_ACTION" else ""
        )
        lines.append(
            f"  {c.get('symbol','?'):<12} | {c.get('sector','?')[:16]:<16} | "
            f"{c.get('signal_type','?'):<15} | Score:{float(c.get('score_adjusted',0) or 0):.1f} | "
            f"CMP:₹{c.get('current_price','?')} "
            f"Zone:₹{c.get('entry_zone_low','?')}-{c.get('entry_zone_high','?')} "
            f"Dist:{c.get('dist_entry_pct','?')}% | "
            f"DaysTrig:{c.get('days_to_trigger_est','?')} | "
            f"{c.get('lifecycle','?')}/{c.get('trend_maturity','?')} | "
            f"HoldSc:{c.get('holding_score','?')} Risk:{c.get('risk_score','?')} "
            f"ExpR:{c.get('expected_r_msl','?')}x "
            f"Valid:{c.get('validity_score','?')} | "
            f"Eng:{c.get('engines_count','?')} Conv:{c.get('convergence_pts','?')} | "
            f"SRk:{c.get('sector_rank_at_entry','?')} FII:{c.get('fii_flag','?')} | "
            f"MCap:₹{c.get('market_cap','?')}Cr Qual:{c.get('fundamental_quality','?')}"
            f"{_ev_str}{_se_str}{_ra_str}"
        )
        # Line 2: full technical picture
        _low52 = c.get('low_52w')
        _cmp   = c.get('current_price')
        _dist_52w_low = (
            round((float(_cmp) - float(_low52)) / float(_low52) * 100, 1)
            if _low52 and _cmp and float(_low52) > 0 else "?"
        )
        lines.append(
            f"    → Mom:{c.get('momentum_state','?')}/{c.get('momentum_phase','?')}"
            f"/{c.get('velocity_state','?')} "
            f"Struct:{c.get('struct_edge','?')} Entry:{c.get('entry_timing_type','?')} | "
            f"RSI-D:{c.get('rsi_daily','?')} RSI-W:{c.get('rsi_weekly','?')} "
            f"ADX:{c.get('adx','?')} RSvN:{c.get('rs_vs_nifty','?')} "
            f"Ret3M:{c.get('ret_3m','?')}% Ret6M:{c.get('ret_6m','?')}% "
            f"Ret12M:{c.get('ret_12m','?')}% "
            f"Vol:{c.get('vol_ratio','?')}x Del:{c.get('delivery_pct','?')}% | "
            f"BrkRdy:{c.get('breakout_readiness','?')} "
            f"MomSc:{c.get('momentum_score','?')} "
            f"InstSc:{c.get('institutional_score','?')} "
            f"ST:{c.get('st_cushion_pct','?')}% | "
            f"MACD:{c.get('macd_direction','?')} BB:{c.get('bb_context','?')} "
            f"WkStr:{c.get('weekly_structure','?')} PSAR:{c.get('psar_dual_confirmed','?')} | "
            f"Dist50:{c.get('dist_sma50','?')}% From52wL:{_dist_52w_low}% "
            f"Consol:{c.get('consol_range','?')}% "
            f"DaysOL:{c.get('days_in_list','?')} ScVel:{c.get('score_vel_5d','?')} "
            f"RkVel:{c.get('rank_vel_3d','?')} "
            f"HotAdj:{'Y' if c.get('zone_hot_adjusted') else 'N'}"
        )

    # ── NEAR-MISS UPGRADES — must be OUTSIDE the raw string ──
    if ctx.get("watch_upgrades"):
        lines += [
            "",
            "═══ NEAR-MISS UPGRADES (step 18 flagged — decide whether to include in ranking) ═══",
            "  If you include any of these, add them to ranked_candidates with appropriate tier.",
            "  Set upgraded_from_watch: true on those entries.",
        ]
        for w in ctx["watch_upgrades"]:
            nm = {}
            try:
                nm = json.loads(w.get("near_miss_data") or "{}")
            except Exception:
                pass
            note = (w.get("ai_note") or "").replace("[NEAR_MISS_UPGRADE:", "").split("]")
            lines.append(
                f"  {w.get('symbol','?')} | {w.get('sector','?')} | "
                f"Score:{float(w.get('score_adjusted',0) or 0):.1f} | "
                f"Original block: {nm.get('blocked_by','?')} | "
                f"Step18 flag: {note[0] if note else '?'} | "
                f"Reason: {note[1].strip()[:120] if len(note)>1 else '?'}"
            )

    lines.append(r"""
═══ OUTPUT FORMAT ═══
Return ONLY this exact JSON. Rank ALL candidates above — none should be missing.

conviction: HIGH | MEDIUM | LOW
tier: TIER_1 (act now, 1–3 week setup) | TIER_2 (watch for trigger) | TIER_3 (monitor)
action: ENTER_NOW | ENTER_ON_DIP | WAIT_FOR_TRIGGER | SKIP
expected_holding_days: your estimate of how many trading sessions to target exit (e.g. 5, 10, 15)
suggested_allocation_pct: % of available capital (0 if SKIP, e.g. 5.0 for 5%)
  Guidelines: TIER_1 high-conviction → 6-8% | TIER_1 medium → 4-5% | TIER_2 → 2-3% | TIER_3/SKIP → 0%
  Total allocations should not exceed 100% minus existing positions
max_chase_pct: 0-8, see CHASE GUIDANCE above (0 unless action is ENTER_NOW/ENTER_ON_DIP)

{
  "ranked_candidates": [
    {
      "rank": 1,
      "symbol": "NSE_SYMBOL",
      "tier": "TIER_1",
      "upgraded_from_watch": false,
      "conviction": "HIGH",
      "confidence": 0.85,
      "action": "ENTER_NOW",
      "expected_holding_days": 10,
      "suggested_allocation_pct": 6.0,
      "thesis": "2 sentences — why this stock in these exact macro conditions within 1-3 weeks",
      "entry_note": "specific price level or volume condition to watch for",
      "invalidation": "one precise condition that kills this setup",
      "max_chase_pct": 4.0,
      "chase_rationale": "one short phrase — which signals justify chasing this far, or why 0",
      "risks": ["risk1", "risk2"],
      "catalyst": "specific macro/FII/sector tailwind driving this",
      "lessons_applied": ["which lesson rules were used in this decision"],
      "correlation_group": "e.g. PSU Banks or null if standalone"
    }
  ],
  "sector_exposure_warnings": [
    {
      "sector": "sector name",
      "candidate_count": 4,
      "already_held": 1,
      "recommendation": "specific reason to cap — e.g. FII selling + RSI extended",
      "allow_count": 1
    }
  ],
  "correlation_groups": [
    {
      "group_label": "e.g. Mid-cap IT",
      "symbols": ["SYM1", "SYM2"],
      "shared_risk": "what makes them move together",
      "recommendation": "take best 1 only — SYM1 has better FII alignment"
    }
  ],
  "position_actions": [
    {
      "symbol": "must be one of the OPEN POSITIONS symbols above — never invent one",
      "recommended_action": "HOLD | TRIM | EXIT | TIGHTEN_SL | NO_ACTION",
      "reason": "1 sentence — must cite the specific SectorEvent/EventRisk/RegAlert flag or",
      "confidence": 0.75,
      "urgency": "IMMEDIATE | THIS_WEEK | LOW"
    }
  ],
  "portfolio_guidance": {
    "position_sizing_override": "FULL | REDUCED_25PCT | HALF | MINIMAL",
    "agrees_with_step18": true,
    "override_reason": "null if agrees, else explain why you differ from step 18 sizing",
    "new_positions_guidance": "e.g. Max 3 new entries today, TIER_1 only given CAUTION regime",
    "capital_deployment_narrative": "2 sentences on how to deploy capital today given all context",
    "sectors_to_overweight": ["sector names"],
    "sectors_to_underweight": ["sector names"]
  },
  "self_improvement_notes": [
    {
      "pattern_observed": "specific cross-stock or temporal pattern detected today",
      "suggested_rule": "Rule: actionable instruction starting with Rule:",
      "applies_to_sectors": ["sector1"],
      "confidence": "LOW"
    }
  ],
  "summary": "3-4 sentence condensed summary of today's action: how many picks, sizing stance, top 2 names and why, key risk to watch. Write as if briefing a trader in 30 seconds."
}""")

    return "\n".join(lines)


# ── AI call ────────────────────────────────────────────────────────────────

def call_ai(prompt: str) -> dict | None:
    try:
        from ai.ai_router import is_ai_available, raw_completion
    except ImportError:
        from ai_router import is_ai_available, raw_completion

    if not is_ai_available():
        logger.warning("No AI provider — step 19 skipped")
        return None

    provider = cfg("ai_provider", "disabled").lower()
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"

    logger.info(f"Step 19 AI call via provider: {provider}")
    try:
        full_text = raw_completion(full_prompt, max_tokens=12000)
    except Exception as e:
        logger.warning(f"AI call failed: {e}")
        return None

    if not full_text:
        return None

    try:
        m = re.search(r"\{[\s\S]+\}", full_text)
        return json.loads(m.group()) if m else None
    except Exception as e:
        logger.warning(f"JSON parse failed: {e}")
        return None


# ── Writes ─────────────────────────────────────────────────────────────────

def write_signal_enrichment(sb, result: dict, candidates: list[dict],
                            trade_date: str, provider: str) -> int:
    """
    Write AI conviction fields to signal_log and master_shortlist for ALL ranked candidates.
    Also writes portfolio_guidance as a note on the TIER_1 records.
    """
    ranked = result.get("ranked_candidates") or []
    if not ranked:
        return 0

    guidance   = result.get("portfolio_guidance") or {}
    guidance_note = (
        f"Sizing:{guidance.get('position_sizing_override','?')} | "
        f"{guidance.get('capital_deployment_narrative','')[:150]}"
    )

    # Build id + entry_zone_high lookups from candidates for signal_log update.
    # zone_high is compute_msl's mechanical ceiling — needed here so the AI's
    # max_chase_pct (a judgment %) can be turned into ai_zone_high_extended
    # (a deterministic absolute price) the same way GTT levels are computed
    # in entry_readiness.py: AI reasons in %, Python does the arithmetic.
    id_map        = {c["symbol"]: c.get("id") for c in candidates}
    zone_high_map = {c["symbol"]: c.get("entry_zone_high") for c in candidates}
    # 2026-07: sector-event context per symbol, for audit — records what
    # event_calendar signal (if any) was actually visible at decision time.
    sector_event_map = {
        c["symbol"]: {
            "sector_event_bias":      c.get("sector_event_bias"),
            "sector_event_intensity": c.get("sector_event_intensity"),
            "sector_event_name":      c.get("sector_event_name"),
        }
        for c in candidates
    }
    # 2026-07: regulatory_alert context per symbol, for audit — was reaching
    # the prompt but never persisted, unlike every other new field added this
    # round. Records what market_news-derived alert (if any) was visible.
    regulatory_alert_map = {
        c["symbol"]: {
            "regulatory_alert_action": c.get("regulatory_alert_action"),
            "regulatory_alert_note":   c.get("regulatory_alert_note"),
        }
        for c in candidates
    }

    written  = 0
    chase_clamped = 0
    for item in ranked:
        sym = item.get("symbol")
        if not sym:
            continue

        tier        = item.get("tier", "TIER_3")
        conviction  = item.get("conviction", "LOW")
        confidence  = float(item.get("confidence") or 0)
        action      = item.get("action", "SKIP")
        allocation  = float(item.get("suggested_allocation_pct") or 0)
        thesis      = item.get("thesis", "")
        entry_note  = item.get("entry_note", "")
        invalidation = item.get("invalidation", "")
        risks        = item.get("risks") or []
        catalyst     = item.get("catalyst", "")
        corr_group   = item.get("correlation_group") or ""
        lessons_used = item.get("lessons_applied") or []

        # ── max_chase_pct: clamp defensively — never trust raw AI output for
        # a value that will directly widen a live trading zone. Force to 0
        # for any action that isn't an actual entry, regardless of what the
        # AI returned (prompt asks for this too, but the write path is the
        # actual safety boundary, same philosophy as _validate_ai_prices).
        raw_chase_pct = float(item.get("max_chase_pct") or 0)
        if action not in ("ENTER_NOW", "ENTER_ON_DIP"):
            max_chase_pct = 0.0
        else:
            max_chase_pct = min(max(raw_chase_pct, 0.0), MAX_CHASE_PCT_CEILING)
        if max_chase_pct != raw_chase_pct:
            chase_clamped += 1
        chase_rationale = (item.get("chase_rationale") or "")[:200]

        zone_high = zone_high_map.get(sym)
        ai_zone_high_extended = (
            round(float(zone_high) * (1 + max_chase_pct / 100), 2)
            if zone_high and max_chase_pct > 0 else None
        )

        conviction_reason = (
            f"[{tier}] {thesis} | Entry: {entry_note} | "
            f"Invalidation: {invalidation} | "
            f"Alloc: {allocation:.1f}% | Corr: {corr_group}"
        )[:800]

        ai_note = (
            f"[{tier}/{action}] {thesis[:100]} | "
            + (f"Lessons: {', '.join(lessons_used[:2])}" if lessons_used else "")
            + (f" | {guidance_note}" if tier == "TIER_1" else "")
        )[:500]

        signal_update = {
            "ai_conviction":        conviction,
            "ai_confidence":        confidence,
            "ai_conviction_reason": conviction_reason,
            "ai_suggested_action":  action,
            "ai_risks":             risks,
            "ai_catalyst":          catalyst,
            "ai_note":              ai_note,
            "ai_provider":          provider,
            "ai_max_chase_pct":     max_chase_pct,
            "ai_chase_rationale":   chase_rationale,
            "ai_zone_high_extended": ai_zone_high_extended,
            **sector_event_map.get(sym, {}),
            **regulatory_alert_map.get(sym, {}),
        }

        try:
            sig_id = id_map.get(sym)
            if sig_id:
                sb.table("signal_log").update(signal_update).eq("id", sig_id).execute()
            else:
                sb.table("signal_log").update(signal_update).eq("date", trade_date).eq("symbol", sym).execute()
        except Exception as e:
            logger.warning(f"signal_log update failed for {sym}: {e}")

        try:
            sb.table("master_shortlist").update({
                "ai_conviction":        conviction,
                "ai_conviction_reason": conviction_reason,
                "ai_risks":             risks,
                "ai_suggested_action":  action,
                "ai_note":              ai_note[:300],
                "ai_provider":          provider,
                "ai_shortlist_rank":    item.get("rank"),
                "ai_shortlist_reason":  f"[{tier}/{action}] {thesis[:150]}",
                "ai_max_chase_pct":     max_chase_pct,
                "ai_chase_rationale":   chase_rationale,
                "ai_zone_high_extended": ai_zone_high_extended,
            }).eq("date", trade_date).eq("symbol", sym).execute()
        except Exception as e:
            logger.warning(f"master_shortlist update failed for {sym}: {e}")


        written += 1

    logger.info(
        f"signal_log + master_shortlist: {written} candidates enriched"
        + (f" | ⚠️ {chase_clamped} max_chase_pct values clamped (out-of-range or non-entry action)"
           if chase_clamped else "")
    )
    return written


_VALID_POSITION_ACTIONS = {"HOLD", "TRIM", "EXIT", "TIGHTEN_SL", "NO_ACTION"}
_VALID_URGENCY          = {"IMMEDIATE", "THIS_WEEK", "LOW"}


def write_position_actions(sb, result: dict, positions: list[dict], trade_date: str) -> int:
    """
    2026-07 addition — mitigates the "AI can see event risk on a held
    position but has no way to act on it" gap. Persists the AI's
    position_actions output (advisory, news/event-aware judgment on
    currently-held positions) to NEW columns on open_positions:
    ai_recommended_action, ai_action_reason, ai_action_confidence,
    ai_action_urgency, ai_action_updated_at.

    Deliberately separate, ai_-prefixed columns — NEVER touches the
    pre-existing action_required / exit_signal / event_risk fields already
    on open_positions, which some other, unseen process populates. Writing
    to genuinely new columns means this can never silently override or
    collide with whatever already owns those. If you know what populates
    those three fields, worth checking whether this should eventually feed
    into that process instead of running alongside it.

    Defensive validation, same philosophy as max_chase_pct clamping above —
    never trust raw AI output for something this consequential without
    checking it against ground truth first:
      - symbol must match a real currently-open position — hallucinated or
        stale symbols are dropped and logged, never written anywhere.
      - recommended_action must be one of the 5 allowed values — anything
        else silently becomes NO_ACTION (fail safe, not fail loud-and-wrong).
      - confidence clamped to [0, 1]. urgency defaults to LOW if invalid.

    Advisory only — nothing downstream currently executes on these columns
    automatically. A human (or a future automation) reads them.
    """
    actions = result.get("position_actions") or []
    if not actions:
        return 0

    valid_symbols = {p["symbol"] for p in positions if p.get("symbol")}
    written = 0
    dropped = 0
    now_iso = datetime.now().isoformat()

    for item in actions:
        sym = item.get("symbol")
        if not sym or sym not in valid_symbols:
            logger.warning(
                f"position_actions: dropping entry for {sym!r} — not a "
                f"currently-open position, never writing this"
            )
            dropped += 1
            continue

        action = item.get("recommended_action")
        if action not in _VALID_POSITION_ACTIONS:
            logger.warning(
                f"position_actions: {sym} had invalid recommended_action "
                f"{action!r} — defaulting to NO_ACTION"
            )
            action = "NO_ACTION"

        confidence = min(max(float(item.get("confidence") or 0), 0.0), 1.0)
        reason     = (item.get("reason") or "")[:500]
        urgency    = item.get("urgency") if item.get("urgency") in _VALID_URGENCY else "LOW"

        try:
            sb.table("open_positions").update({
                "ai_recommended_action": action,
                "ai_action_reason":      reason,
                "ai_action_confidence":  confidence,
                "ai_action_urgency":     urgency,
                "ai_action_updated_at":  now_iso,
            }).eq("symbol", sym).eq("status", "ACTIVE").execute()
            written += 1
        except Exception as e:
            logger.warning(f"open_positions ai_action update failed for {sym}: {e}")

    logger.info(
        f"open_positions: {written} position_actions written"
        + (f" | ⚠️ {dropped} dropped (invalid/hallucinated symbol)" if dropped else "")
    )
    return written


def write_final_picks(sb, result: dict, trade_date: str, provider: str,
                      candidate_count: int):
    """
    Store ranked JSON in ai_context.__FINAL_PICKS__.
 
    SPLIT STORAGE (v4 fix):
      conviction_reason   ← ranked_candidates only  (heavy, up to 7800 chars)
      strategy_validation ← portfolio_guidance + warnings + correlations
                            (light, up to 3500 chars — always fits)
    """
    from loguru import logger
    import json
 
    guidance = result.get("portfolio_guidance") or {}
    ranked   = result.get("ranked_candidates") or []
    tier1    = [r for r in ranked if r.get("tier") == "TIER_1"]
    tier2    = [r for r in ranked if r.get("tier") == "TIER_2"]
 
    # ── Payload split ──────────────────────────────────────────────────────
    # conviction_reason: ranked_candidates only — the heavy list
    candidates_payload = {"ranked_candidates": ranked}
 
    # strategy_validation: all guidance/meta — compact, always < 3500 chars
    guidance_payload = {
        "portfolio_guidance":        result.get("portfolio_guidance", {}),
        "sector_exposure_warnings":  result.get("sector_exposure_warnings", []),
        "correlation_groups":        result.get("correlation_groups", []),
        "self_improvement_notes":    result.get("self_improvement_notes", []),
    }
 
    try:
        sb.table("ai_context").upsert({
            "date":               trade_date,
            "symbol":             "__FINAL_PICKS__",
            "conviction":         guidance.get("position_sizing_override", "REDUCED_25PCT"),
 
            # ranked_candidates — main read target in send_alerts
            "conviction_reason":  json.dumps(candidates_payload,
                                             ensure_ascii=False),
 
            # portfolio_guidance + warnings + correlations — now never truncated
            "strategy_validation": json.dumps(guidance_payload,
                                              ensure_ascii=False),
 
            "risks":              [w.get("recommendation", "")
                                   for w in (result.get("sector_exposure_warnings") or [])[:3]],
            "catalyst":           "; ".join(guidance.get("sectors_to_overweight") or []),
            "suggested_action":   guidance.get("position_sizing_override", "REDUCED_25PCT"),
            "provider":           provider,
            "ai_note": (
                result.get("summary") or (
                    f"TIER_1:{len(tier1)} TIER_2:{len(tier2)} total:{candidate_count} | "
                    f"{guidance.get('new_positions_guidance', '')[:200]} | "
                    f"{guidance.get('capital_deployment_narrative', '')[:300]}"
                )
            )[:2000],
            "fallback_used":  False,
            "confidence":     0.9,
        }, on_conflict="date,symbol").execute()
 
        logger.info(
            f"ai_context __FINAL_PICKS__ written (split payload) | "
            f"candidates:{len(candidates_payload['ranked_candidates'])} | "
            f"guidance_keys:{list(guidance_payload.keys())}"
        )
    except Exception as e:
        logger.warning(f"__FINAL_PICKS__ write failed: {e}")


def write_self_improvement_lessons(sb, result: dict, trade_date: str) -> int:
    """
    Write new rules from cross-stock pattern detection to lessons table.
    These start with LOW confidence and earn trust through post_trade outcomes.
    """
    notes = result.get("self_improvement_notes") or []
    written = 0
    for note in notes[:3]:
        rule = note.get("suggested_rule") or ""
        if not rule or not rule.strip():
            continue
        try:
            sb.table("lessons").insert({
                "date":              trade_date,
                "scenario_type":     note.get("pattern_observed", "Cross-stock pattern")[:80],
                "trigger_event":     "ai_decision_engine",
                "linked_event_type": "DECISION_ENGINE",
                "impacted_sector":   ", ".join(note.get("applies_to_sectors") or [])[:200],
                "scenario_context":  note.get("pattern_observed", ""),
                "what_expected":     "Pattern-based forward-looking rule",
                "what_happened":     note.get("pattern_observed", ""),
                "what_failed":       "N/A — proactive detection",
                "root_cause":        note.get("pattern_observed", ""),
                "corrective_rule":   rule,
                "source":            "AI:decision_engine",
                "is_active":         True,
                "times_applied":     0,
                "times_worked":      0,
                "confidence":        0.3,  # starts LOW — cross-stock patterns need validation
            }).execute()
            written += 1
        except Exception as e:
            logger.warning(f"Self-improvement lesson write failed: {e}")
    return written

def _validate_ai_prices(result: dict, candidates: list[dict]) -> None:
    """
    Cross-check prices in AI output against master_shortlist ground truth.
    Logs WARNING for any price >25% off DB value. Does NOT modify result.
    This catches hallucinations caused by null price input (date bug recurrence).
    """
    import re
    price_re = re.compile(r'₹([\d,]+(?:\.\d+)?)')
    db_map   = {c["symbol"]: float(c.get("current_price") or 0) for c in candidates}

    issues = []
    for item in (result.get("ranked_candidates") or []):
        sym    = item.get("symbol", "?")
        db_cmp = db_map.get(sym, 0)
        tier   = item.get("tier", "TIER_3")
        if not db_cmp:
            if tier in ("TIER_1", "TIER_2"):
                issues.append(f"{sym}({tier}): no DB price — output unverifiable")
            continue
        for field in ("entry_note", "invalidation", "thesis"):
            for raw in price_re.findall(item.get(field) or ""):
                try:
                    p       = float(raw.replace(",", ""))
                    pct_off = abs(p - db_cmp) / db_cmp * 100
                    if pct_off > 25:
                        issues.append(
                            f"{sym}.{field}: ₹{p:,.0f} vs DB ₹{db_cmp:,.0f} "
                            f"({pct_off:.0f}% off)"
                        )
                except ValueError:
                    pass

    if issues:
        logger.warning(f"AI price validation — {len(issues)} suspect values:")
        for iss in issues[:10]:
            logger.warning(f"  ⚠️  {iss}")
    else:
        logger.info(
            f"AI price validation ✅ — all prices within 25% of DB CMP "
            f"({len(db_map)} candidates checked)"
        )

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch — step 19 skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info("=" * 60)
    logger.info(f"STEP 19: AI Decision Engine {'[DRY RUN]' if DRY_RUN else ''}")
    logger.info("=" * 60)

    sb         = get_supabase()
    trade_date = get_trade_date(sb)

    if trade_date != str(today_ist()):
        logger.info(f"Non-trading day detected — rolling back to last trading date: {trade_date}")

    # Load all context
    logger.info("Loading context...")
    ctx = load_context(sb, trade_date)

    if not ctx or not ctx.get("candidates"):
        logger.warning("No candidates available — step 19 skipped")
        return {"status": "no_candidates"}

    candidates = ctx["candidates"]

    null_price = [c["symbol"] for c in candidates if c.get("current_price") is None]
    if null_price:
        logger.warning(
            f"⚠️  {len(null_price)} candidates have null current_price — "
            f"AI hallucination risk: {null_price}. "
            f"Verify master_shortlist has compute_msl data for {trade_date}"
        )

    if not ctx.get("market_intel"):
        logger.warning(
            "No __MARKET_INTEL__ found — step 18 may not have run. "
            "Proceeding without market overlay."
        )

    logger.info(
        f"  {len(candidates)} candidates | {len(ctx['positions'])} open positions | "
        f"{len(ctx['lessons'])} lessons | {len(ctx['echoes'])} echoes | "
        f"Sizing from step 18: {ctx.get('market_intel', {}).get('position_sizing','?')}"
    )

    # Build prompt
    prompt = build_prompt(ctx, trade_date)

    if DRY_RUN:
        logger.info(f"[DRY RUN] Prompt: {len(prompt)} chars")
        logger.info(f"[DRY RUN] Candidates: {[c.get('symbol') for c in candidates]}")
        return {"status": "dry_run", "prompt_chars": len(prompt),
                "candidates": len(candidates)}

    # Single AI call
    logger.info(f"Calling AI (single batch, {len(candidates)} candidates)...")
    t0     = time.time()
    result = call_ai(prompt)
    elapsed = time.time() - t0

    if not result:
        logger.warning("AI call returned nothing — step 19 non-fatal exit")
        return {"status": "ai_failed"}

    logger.info(f"AI responded in {elapsed:.1f}s")
    _validate_ai_prices(result, candidates)

    provider = cfg("ai_provider", "unknown")
    ranked   = result.get("ranked_candidates") or []
    tier1    = [r for r in ranked if r.get("tier") == "TIER_1"]
    tier2    = [r for r in ranked if r.get("tier") == "TIER_2"]
    tier3    = [r for r in ranked if r.get("tier") == "TIER_3"]
    guidance = result.get("portfolio_guidance") or {}

    # Writes
    enriched  = write_signal_enrichment(sb, result, candidates, trade_date, provider)
    write_final_picks(sb, result, trade_date, provider, len(candidates))
    new_rules = write_self_improvement_lessons(sb, result, trade_date)
    pos_actions_written = write_position_actions(sb, result, ctx["positions"], trade_date)

    # Summary log
    logger.success(
        f"Step 19 done in {elapsed:.1f}s | "
        f"TIER_1:{len(tier1)} TIER_2:{len(tier2)} TIER_3:{len(tier3)} | "
        f"Enriched:{enriched} | New rules:{new_rules} | "
        f"PosActions:{pos_actions_written} | "
        f"Sizing:{guidance.get('position_sizing_override','?')}"
    )
    logger.info(f"  Guidance: {guidance.get('new_positions_guidance','')}")
    logger.info(f"  Overweight: {guidance.get('sectors_to_overweight',[])} | "
                f"Underweight: {guidance.get('sectors_to_underweight',[])}")

    if tier1:
        t1_str = " | ".join(
            f"{r['symbol']}({r.get('conviction','?')},{r.get('suggested_allocation_pct',0):.0f}%)"
            for r in tier1
        )
        logger.info(f"  ★ TIER_1: {t1_str}")

    if result.get("sector_exposure_warnings"):
        for w in result["sector_exposure_warnings"]:
            logger.info(
                f"  ⚠ {w.get('sector','?')}: {w.get('candidate_count','?')} candidates, "
                f"allow {w.get('allow_count','?')} — {w.get('recommendation','')}"
            )

    if result.get("correlation_groups"):
        for g in result["correlation_groups"]:
            logger.info(f"  ⚡ Cluster [{g.get('group_label','?')}]: "
                        f"{g.get('symbols',[])} — {g.get('recommendation','')}")

    return {
        "status":     "ok",
        "tier1":      len(tier1),
        "tier2":      len(tier2),
        "tier3":      len(tier3),
        "enriched":   enriched,
        "new_rules":  new_rules,
        "sizing":     guidance.get("position_sizing_override"),
        "top_picks":  [r["symbol"] for r in tier1[:5]],
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TradeOS v6 — Step 19: AI Decision Engine")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())