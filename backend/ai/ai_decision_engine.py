"""
TradeOS v7 — Step 19: AI Decision Engine
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

  2. NO SELF-RATED CONVICTION, 29-Aug-2026 — tier (TIER_1/2/3) and conviction
     (HIGH/MEDIUM/LOW) were REMOVED, not just demoted. Measured against real
     resolved outcomes (n=47-99/tier, n=30-44/conviction, stable across two
     separate two-week periods): TIER_1/HIGH-conviction picks UNDERPERFORMED
     TIER_3/LOW-conviction ones (E[R] -0.18 vs +0.37 by tier; -1.35 vs +3.28
     avg% by conviction) — the self-assessment was inversely predictive, not
     merely unhelpful (`rank_weight_tier`/`rank_weight_conviction` had already
     sat at 0 since 04-Aug-2026 for the weaker reason that it was simply
     unvalidated; this is the follow-up measurement that reason called for).
     Which candidates get operator attention is now decided deterministically
     downstream (the allocator's own measured edge — see control/
     candidate_monitor.py), not from the AI's opinion of quality. The AI's
     remaining job is narrative and factual: thesis, invalidation, catalyst,
     cross-candidate correlation — never a re-expression of "how good is this"
     under a different field name.

  3. STEP 15 GATE WORK IS RESPECTED — candidates already passed 11 hard gates.
     Step 19 does NOT re-pick from MSL or re-run technical gates. Its job is:
     portfolio-level reasoning, macro overlay, cross-stock correlation guards,
     and narrative on already-qualified setups.

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
  signal_log       — ai_conviction_reason (now pure narrative text — thesis/
                     entry_note/invalidation/catalyst, no tier or conviction
                     label prefixed), ai_suggested_action, ai_risks,
                     ai_catalyst, ai_note, ai_provider — written for ALL
                     ranked candidates. ai_conviction/ai_tier are no longer
                     written (left null going forward; historical rows keep
                     their old values, read-only, for the record).
                     ai_max_chase_pct, ai_zone_high_extended, ai_chase_rationale
                     — FIXED, deterministic (SWING_CHASE_PCT_FLAT), not an
                     AI judgment — see write_signal_enrichment. Quantified
                     29-Aug-2026: chase >2% above the mechanical zone turned
                     net negative on real resolved outcomes (n=17-24 per
                     band), so the ceiling is deliberately conservative, not
                     AI-widened per candidate the way it used to be.
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
from config import get_supabase, today_ist, is_kill_switch_active, cfg, cfg_float, cfg_int, get_trade_date, fetch_all

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

# FIXED, deterministic chase ceiling — 29-Aug-2026, replaces the old
# AI-proposed-then-clamped ai_max_chase_pct. Quantified against real resolved
# outcomes (signal_output_daily, n=511/23/17/7 across bands): 0% chase held
# +1.83% avg/68.5% win; 0-2% held positive (+1.10%/60.9%); 2-4% turned
# negative (-0.12%/41.2%); 4%+ was disastrous (-4.61%/14.3%, small n but
# consistent in direction with every other AI self-rated judgment measured
# this session). The AI no longer proposes a number to clamp — this constant
# IS the ceiling, applied uniformly, matching the last bin the data still
# supports. Config-overridable so a future re-measurement can move it
# without a code change; the number itself is not invented, it is where the
# real data stopped supporting a wider allowance.
SWING_CHASE_PCT_FLAT = 2.0


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
    "calibrated to this window — a setup that takes 6 weeks to play out is not a fit for us. "
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
    "assign it action SKIP — do not invent a price. "
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
              "eap_action,in_rule_engine,"
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
                "eap_action,in_rule_engine,near_miss_data,filter_reason"
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
              # `suggested` was dropped in migration 008. It was a Google Sheet
              # column, 100% NULL on every row since ingest_sheets was retired,
              # so selecting it contributed nothing — but PostgREST rejects the
              # WHOLE query for one unknown column, which took step 19 down
              # entirely the first evening after the migration ran.
              "stoch_context,persistent_phase,notes,"
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
    # PAGED — 1102 active lessons on 15-Aug-2026 against a 1000-row cap, so
    # 102 of them were invisible to the model and which 102 was up to the
    # planner. The comment below is right that Python sorts and limits; it
    # cannot sort what the server never sent.
    lesson_rows = (
        fetch_all(lambda: sb.table("lessons")
          .select("id,corrective_rule,scenario_type,impacted_sector,"
                  "times_applied,times_worked,confidence,source")
          .eq("is_active", True))
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
                # signal_log has NO outcome columns — it never has. The
                # outcome_* family lives on signal_output_daily
                # (outcome_entered, outcome_category, outcome_return_pct).
                # Selecting them from signal_log failed the whole query, so
                # this self-reflection block silently fed the model picks with
                # no outcomes attached: the LLM was asked to learn from its
                # past calls while being shown none of the results.
                out_rows = (
                    sb.table("signal_output_daily")
                      .select("symbol,outcome_category,outcome_return_pct,outcome_entered")
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
                        "outcome":  outcomes.get(p.get("symbol"), {}).get("outcome_category"),
                        "pnl_pct":  outcomes.get(p.get("symbol"), {}).get("outcome_return_pct"),
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

def build_prompt(ctx: dict, trade_date: str,
                 rank_only: list | None = None) -> str:
    """
    `rank_only` scopes the OUTPUT, never the INPUT.

    Every candidate stays in the prompt regardless, because sector exposure and
    correlation groups are cross-candidate judgements — asking about four names
    in isolation would produce four "standalone" verdicts and miss the very
    concentration this step exists to catch. Only the list the model must
    RETURN is narrowed, which is what bounds the response length.
    """
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
            "    1. Apply macro overlay and concentration guards as normal",
            "    2. Set upgraded_from_watch: true on all entries in ranked_candidates",
            "    3. Use near_miss_data.gap_atrs to judge how far extended — smaller gap = better,",
            "       but this is entry-timing context for entry_note/thesis, not a priority score",
            "    4. Assign SKIP if macro does not support entry at this distance",
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
            "    5. Assign action (ENTER_NOW/ENTER_ON_DIP/WAIT_FOR_TRIGGER/SKIP) based on",
            "       entry timing only — not a quality or conviction judgment",
            "    6. Suggest capital allocation % (soft guidance, human decides final amount)",
            "    7. Apply event awareness per EVENT GUIDANCE below (NOT a uniform cap)",
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
        "    - Present + ≤5d → halve suggested_allocation_pct and name the event",
        "      in risks, same spirit as the old rule.",
        "    - This is a genuine data limitation, not something to reason around —",
        "      don't infer a direction from the purpose text (e.g. 'Fund Raising' is",
        "      not inherently bad news; don't invent a bias that isn't in the data).",
        "",
        "  📅SectorEvent (sector-level, from event_calendar — DOES carry event_bias",
        "  and event_intensity, e.g. RBI MPC, monsoon, FII flow windows, festive season).",
        "  Use the bias, don't just react to presence:",
        "    - NEGATIVE + MEDIUM/HIGH intensity + ≤5d → halve suggested_allocation_pct",
        "      and name it in risks, same spirit as the old rule, correctly targeted",
        "      at bad news only.",
        "    - POSITIVE + MEDIUM/HIGH intensity → this is what event tracking is",
        "      FOR — cite it as a supporting catalyst in thesis/catalyst fields,",
        "      do NOT reduce sizing for it. A sector tailwind landing during your",
        "      1-3wk hold is a reason to note it positively, not caution.",
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

    # CHASE GUIDANCE removed as an AI-discretion prompt section, 29-Aug-2026.
    # Quantified against real resolved outcomes: candidates entered with zero
    # chase held +1.83% avg / 68.5% win (n=511); 0-2% chase held positive
    # (+1.10%/60.9%, n=23); 2-4% chase turned NEGATIVE (-0.12%/41.2%, n=17);
    # 4%+ chase was disastrous (-4.61%/14.3% win, n=7 — small, but directionally
    # consistent with every other AI self-rated judgment measured this session).
    # The AI is no longer asked for a chase number at all — chase_note below
    # is narrative only; the actual enforced ceiling is a fixed, deterministic
    # value computed in write_signal_enrichment(), not a model-proposed one.

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
            "  If you include any of these, add them to ranked_candidates normally.",
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

    if rank_only:
        lines += [
            "",
            "═══ SCOPE FOR THIS RESPONSE ═══",
            f"  Every candidate above is shown so you can judge sector exposure and",
            f"  correlation across the WHOLE field. But return ranked_candidates for",
            f"  ONLY these {len(rank_only)} symbols: {', '.join(rank_only)}",
            "  Omit all others from ranked_candidates. Still fill in",
            "  sector_exposure_warnings and correlation_groups using the full field.",
            "",
        ]

    lines.append(r"""
═══ OUTPUT FORMAT ═══
Return ONLY this exact JSON. Cover ALL candidates above — none should be missing.

TIER AND CONVICTION ARE GONE, 29-Aug-2026 — DO NOT INVENT REPLACEMENTS.
Measured against real resolved outcomes: candidates you marked TIER_1/HIGH
conviction underperformed your own TIER_3/LOW-conviction picks (E[R] -0.18
vs +0.37, n=47/99, holding across two separate two-week periods) — your
self-rated confidence was inversely predictive, not just unhelpful. Do not
add a new field that re-expresses "how good do you think this is" under any
other name (a 1-10 score, a star rating, "priority", etc.) — the finding was
about the JUDGMENT, not the label. Your job below is narrative and factual
observation only: why the setup exists, what breaks it, what to watch for.
Which candidates actually get attention is decided deterministically
elsewhere, from real measured edge, not from your assessment of quality.

action: ENTER_NOW | ENTER_ON_DIP | WAIT_FOR_TRIGGER | SKIP — a factual read of
  entry timing (is price actually in a reasonable zone right now), not a
  conviction call.
expected_holding_days: your estimate of how many trading sessions to target exit (e.g. 5, 10, 15)
suggested_allocation_pct: % of available capital (0 if SKIP) — soft, informational
  context for the human reader only; real position sizing is computed
  deterministically elsewhere and does not read this field.

{
  "ranked_candidates": [
    {
      "symbol": "NSE_SYMBOL",
      "upgraded_from_watch": false,
      "action": "ENTER_NOW",
      "expected_holding_days": 10,
      "suggested_allocation_pct": 6.0,
      "thesis": "2 sentences — why this stock in these exact macro conditions within 1-3 weeks",
      "entry_note": "specific price level or volume condition to watch for",
      "invalidation": "one precise condition that kills this setup",
      "chase_note": "one short phrase on entry timing/extension — informational only, the actual chase ceiling is fixed and does not read this",
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
    "new_positions_guidance": "e.g. Max 3 new entries today given CAUTION regime",
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
        # Previously: return None, log "step 19 skipped", produce nothing.
        # A day with no AI provider therefore generated signals, scored and
        # ranked them, and then emitted no tiering at all — because the only
        # component that could tier them was unavailable and had no substitute.
        # models/ml_conviction.pkl existed the whole time and was unreachable,
        # since its only entry point (ai_router.analyze) is called by nothing.
        logger.warning("No AI provider — falling back to the ML conviction model")
        return None

    provider = cfg("ai_provider", "disabled").lower()
    full_prompt = f"{_SYSTEM_PROMPT}\n\n{prompt}"

    logger.info(f"Step 19 AI call via provider: {provider}")
    # 21 fully-reasoned candidates plus position_actions, portfolio_guidance,
    # self_improvement_notes, and summary can exceed 12k tokens on a busy
    # day (confirmed truncation at rank 16/21 in production). 20000 gives
    # real headroom; if it's still truncated, retry once at 28000 before
    # giving up rather than failing the whole step.
    token_budgets = [20000, 28000]
    full_text = None
    for attempt, max_tokens in enumerate(token_budgets, start=1):
        try:
            full_text = raw_completion(full_prompt, max_tokens=max_tokens,
                                       call_site="ai_decision_engine", framework="SWING")
        except Exception as e:
            logger.warning(f"AI call failed: {e}")
            return None

        if not full_text:
            return None

        if _looks_truncated(full_text):
            if attempt < len(token_budgets):
                logger.warning(
                    f"Response truncated at max_tokens={max_tokens} "
                    f"({len(full_text)} chars) — retrying at {token_budgets[attempt]}"
                )
                continue
            logger.warning(
                f"Response still truncated at max_tokens={max_tokens} after retry — "
                f"candidate count may need batching instead of a single call"
            )
        break

    return _parse_ai_json(full_text)


# ── Cache: reuse yesterday's verdict for a materially unchanged candidate ──
#
# Found 29-Aug-2026, auditing AI cost: a candidate persisting across evenings
# gets a full, fresh AI analysis every single evening, even when nothing
# about its plan has actually changed. Quantified before writing this: over
# 21 days, 212 candidates repeated on consecutive days, but ZERO were
# byte-identical (avg entry-zone drift ~2%, since ATR-based zones recompute
# from fresh price data every evening even for a persisting setup) — so an
# exact-match cache would silently never fire. Within a 2% tolerance, 116 of
# 212 (55%) qualify; within 1%, 73 (34%).
#
# SCOPED TO CANDIDATE RANKING ONLY. `position_actions` (which feeds the live
# TIGHTEN_SL action on open positions — Track E, F-70) is a structurally
# separate part of both the prompt and the AI's JSON response, keyed off
# `ctx["positions"]`, never touched by anything below — an open position
# always gets a fresh assessment every evening, regardless of whether its
# entry candidate (if any) was ever cached.
def _cache_eligible(today: dict, prior: dict, tolerance_pct: float) -> bool:
    """
    Pure. True if `prior` (yesterday's plan for this symbol — strategy,
    entry_zone_low/high) is close enough to `today`'s that reusing
    yesterday's full AI verdict is a reasonable substitute for a fresh one.

    Same strategy label required — a plan that changed which engine(s)
    produced it is a different plan, not a continuation of the old one,
    regardless of how close the price levels happen to sit.
    """
    if not today.get("strategy") or today.get("strategy") != prior.get("strategy"):
        return False
    for field in ("entry_zone_low", "entry_zone_high"):
        a, b = today.get(field), prior.get(field)
        if a is None or b is None:
            return False
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            return False
        if b == 0 or abs(a - b) / abs(b) > tolerance_pct / 100.0:
            return False
    return True


def find_reusable_candidates(sb, candidates: list[dict], trade_date: str,
                             tolerance_pct: float = 1.0
                             ) -> tuple[dict[str, dict], list[str]]:
    """
    For each of today's candidates, checks whether yesterday's plan for the
    same symbol was materially unchanged (`_cache_eligible`) and already
    carries a genuine — never itself reused, no chaining — AI verdict worth
    reusing. Returns (reused_by_symbol, symbols_still_needing_fresh_analysis).

    The full prior verdict comes from `ai_context.__FINAL_PICKS__`
    (`write_final_picks`'s own `conviction_reason` JSON blob), the one place
    the complete `ranked_candidates` entry survives — `signal_log`'s own
    written columns (`write_signal_enrichment`) collapse tier/thesis/
    entry_note/invalidation/allocation into one truncated, human-readable
    string that is not reliably parseable back into its parts.

    Best-effort throughout: any failure (no prior trading date resolvable,
    missing prior-day data, a malformed JSON blob) returns "nothing
    reusable" rather than raising — caching is a cost optimisation, never
    something that may cost a candidate its analysis.
    """
    symbols = [c.get("symbol") for c in candidates if c.get("symbol")]
    if not symbols:
        return {}, []

    try:
        ref = date.fromisoformat(trade_date) - timedelta(days=1)
        prior_date = get_last_trading_date(sb, ref)
    except Exception as e:
        logger.warning(f"  cache: could not resolve prior trading date ({e}) — skipping cache")
        return {}, symbols
    if not prior_date or prior_date == trade_date:
        return {}, symbols

    try:
        prior_msl = {r["symbol"]: r for r in
                    sb.table("master_shortlist")
                      .select("symbol,entry_zone_low,entry_zone_high")
                      .eq("date", prior_date).in_("symbol", symbols)
                      .execute().data or []}
        prior_sig = {r["symbol"]: r for r in
                    sb.table("signal_log").select("symbol,strategy")
                      .eq("date", prior_date).in_("symbol", symbols)
                      .execute().data or []}
        picks_rows = (sb.table("ai_context").select("conviction_reason")
                        .eq("date", prior_date).eq("symbol", "__FINAL_PICKS__")
                        .execute().data or [])
        prior_ranked: dict[str, dict] = {}
        if picks_rows:
            payload = json.loads(picks_rows[0].get("conviction_reason") or "{}")
            prior_ranked = {r.get("symbol"): r
                           for r in (payload.get("ranked_candidates") or [])
                           if r.get("symbol")}
    except Exception as e:
        logger.warning(f"  cache: prior-day data unavailable ({e}) — skipping cache")
        return {}, symbols

    reused: dict[str, dict] = {}
    to_rank: list[str] = []
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        prior_zone  = prior_msl.get(sym)
        prior_entry = prior_ranked.get(sym)
        prior_full  = {**(prior_zone or {}), "strategy": prior_sig.get(sym, {}).get("strategy")}
        if (prior_zone and prior_entry is not None
                and not prior_entry.get("_reused_from")
                and _cache_eligible(c, prior_full, tolerance_pct)):
            reused[sym] = {**prior_entry, "_reused_from": prior_date}
        else:
            to_rank.append(sym)

    if reused:
        logger.info(f"  cache: {len(reused)} of {len(symbols)} candidate(s) reused "
                   f"from {prior_date}'s materially unchanged analysis "
                   f"(tolerance {tolerance_pct}%)")
    return reused, to_rank


def call_ai_batched(ctx: dict, trade_date: str, batch_size: int = 0,
                    suffix: str = "", symbols_to_rank: list[str] | None = None) -> dict | None:
    """
    `symbols_to_rank`, when given, narrows which symbols this call must
    RETURN a verdict for — same mechanism `rank_only` already uses to split
    across batches, reused here so a caller (e.g. find_reusable_candidates'
    cache) can exclude specific symbols from needing a fresh AI verdict at
    all, without touching `ctx["candidates"]` — every batch still sees
    every candidate for sector/correlation judgement, cache-excluded ones
    included. `None` (the default) reproduces the exact old behaviour:
    every symbol in `ctx["candidates"]` is ranked.

    Rank the field in batches small enough to come back whole.

    WHY THIS EXISTS
    ---------------
    On 2026-07-31 twelve candidates in one call truncated at 20,000 max_tokens,
    truncated again on retry at 28,000, failed to parse at line 301, and the
    step exited with nothing. The ML fallback then produced nothing either, so
    signal_output_daily got zero ranked candidates, the alert went out with
    T1:0 T2:0, and quality checks C08/C09/C18 all fired. One over-long response
    took out the whole evening's tiering.

    Raising max_tokens does not fix it. The provider accepts the larger number
    and stops generating anyway — attempt one returned 4,321 characters against
    a 20,000-token budget. Output length is the constraint, so the request has
    to be smaller, which is what the previous code's own warning said.

    ROOT CAUSE, FOUND LATER (01-Aug-2026, ai_router.py) — 12 candidates should
    never have been near a 20,000-token ceiling for output this terse.
    deepseek-v4-flash is a reasoning model: `max_tokens` budgets hidden
    reasoning AND the actual output from ONE allowance, reasoning spent first,
    and that reasoning is discarded — nothing here reads it. The 07-31 incident
    predates that fix by one day; `ai_thinking_enabled` has been OFF ever since,
    so the failure mode this docstring describes has not recurred. `batch_size`
    was never revisited after the root cause was actually fixed elsewhere —
    raised 29-Aug-2026 (`ai_decision_batch_size` 5→15, `system_config`) once
    real daily volume (8–45 candidates/day, median ~20–24, measured over 20
    sessions) showed batch_size=5 meant 2–9 full-context resends on a typical
    evening for a truncation risk that no longer applies at the size that
    caused it. The escalating budget and per-batch failure handling below stay
    exactly as they were — this is a size change, not a removal of the safety
    net.

    WHAT IS PRESERVED
    -----------------
    Every batch sees EVERY candidate; only the list it must return is narrowed.
    Sector exposure and correlation are cross-candidate judgements and would be
    destroyed by splitting the input — four names looked at alone are four
    standalone verdicts. So the input is whole and the output is partitioned.

    WHAT IS LOST
    ------------
    The model no longer assigns one global rank order in a single pass; ranks
    are renumbered on merge by (tier, confidence). For a decision whose output
    is tiers and actions this is close to free, and it is unambiguously better
    than the current behaviour of returning nothing at all.

    A batch that fails does not fail the step. Its candidates are simply absent
    from the ranking, which is logged, and the remaining batches still land.
    """
    cands = ctx.get("candidates") or []
    if not cands:
        return None

    batch_size = batch_size or cfg_int("ai_decision_batch_size", 15)
    symbols = [c.get("symbol") for c in cands if c.get("symbol")]
    # to_rank is the OUTPUT obligation; `symbols`/`cands`/ctx["candidates"]
    # stay the full field throughout — build_prompt below always receives
    # the whole ctx, never a trimmed one.
    to_rank = symbols if symbols_to_rank is None else [
        s for s in symbols if s in set(symbols_to_rank)]
    if not to_rank:
        # Every candidate was excluded (e.g. fully cache-covered) — nothing
        # for the AI to do this call. A distinct, valid outcome from "the
        # AI call failed": returns a real, empty-but-truthy result rather
        # than None, so the caller's `if not result:` ML-fallback branch
        # does not fire over having nothing left to ask.
        return {"ranked_candidates": []}
    # rank_only only when to_rank is a STRICT subset — passing the full
    # symbol list would add build_prompt's "SCOPE FOR THIS RESPONSE" text
    # for no reason, a needless behaviour change on a day nothing is cached.
    only = to_rank if len(to_rank) < len(symbols) else None

    # One call is cheaper and keeps a single global rank order, so it stays the
    # path for a field small enough to answer in one response.
    if len(to_rank) <= batch_size:
        return call_ai(build_prompt(ctx, trade_date, rank_only=only) + suffix)

    batches = [to_rank[i:i + batch_size] for i in range(0, len(to_rank), batch_size)]
    logger.info(f"  {len(to_rank)} candidates exceeds the {batch_size}-per-call "
                f"budget — splitting into {len(batches)} batches, each seeing "
                f"the full field")

    merged: dict = {}
    ranked: list = []
    seen: set = set()
    failed: list = []

    for i, group in enumerate(batches, start=1):
        logger.info(f"  batch {i}/{len(batches)}: {', '.join(group)}")
        part = call_ai(build_prompt(ctx, trade_date, rank_only=group) + suffix)
        if not part:
            failed.append(group)
            logger.warning(f"  batch {i} returned nothing — {len(group)} "
                           f"candidate(s) will be missing from the ranking")
            continue

        for row in (part.get("ranked_candidates") or []):
            sym = row.get("symbol")
            # A batch asked for four names can still volunteer others. Keep the
            # first verdict per symbol so a later batch cannot silently restate
            # an earlier one with different conviction.
            if not sym or sym in seen:
                continue
            seen.add(sym)
            ranked.append(row)

        # Cross-candidate and portfolio sections: first non-empty wins, because
        # every batch saw the whole field and they are answering the same
        # question. Merging them would double-count the same warning.
        for key in ("sector_exposure_warnings", "correlation_groups",
                    "position_actions", "portfolio_guidance",
                    "self_improvement_notes", "summary"):
            if key not in merged and part.get(key):
                merged[key] = part[key]

    if not ranked:
        logger.warning("  every batch failed — nothing to rank")
        return None

    # No re-sort, no rank assignment, 29-Aug-2026 — both used to order by
    # tier then confidence, the two fields this session's own measurement
    # showed were inversely predictive. Priority among candidates is decided
    # deterministically downstream (the allocator's own measured edge, see
    # control/candidate_monitor.py), not by an AI-native ordering here.
    merged["ranked_candidates"] = ranked
    if failed:
        missing = [s for g in failed for s in g]
        merged["batch_failures"] = missing
        logger.warning(f"  ranked {len(ranked)}/{len(symbols)} — missing: "
                       f"{', '.join(missing)}")
    else:
        logger.success(f"  ranked {len(ranked)}/{len(symbols)} across "
                       f"{len(batches)} batches")
    return merged


def _looks_truncated(full_text: str) -> bool:
    """Cheap pre-parse check: does the response end without closing its
    outermost JSON structure? Used to decide whether a retry at a higher
    token budget is worth it before spending time on full JSON repair."""
    # Trailing code fences and sign-off prose are not truncation. Judging on
    # the raw last character made every ```-wrapped response look cut off, so
    # a complete answer was thrown away and re-requested at a higher budget —
    # both batches on 2026-08-01 parsed fine on the FIRST response and still
    # paid for a second one. The retry is for genuinely incomplete JSON.
    tail = full_text.rstrip().rstrip("`").rstrip()
    return not tail or tail[-1] not in "}]"


def _parse_ai_json(full_text: str) -> dict | None:
    """
    Parse the AI's JSON response defensively.

    LLMs routinely emit multi-sentence string fields (reason/summary/etc.)
    with a literal newline instead of an escaped "\\n". Strict JSON forbids
    raw control characters inside strings, which is exactly what produces
    errors like "Expecting ',' delimiter: line N column M" — the parser
    hits the line break, the string token ends early, and the following
    text looks like a stray token instead of a comma-separated value.

    Strategy, cheapest first:
      1. json.loads(strict=False) — allows raw control chars in strings.
         This alone fixes the vast majority of these failures.
      2. If that still fails, escape stray control chars that appear
         *inside* string literals (tracked via a quote/escape scan) and
         retry with strict=False.
      3. On total failure, dump the raw text to disk for debugging and
         log the first parse error (not the last, which is usually noise
         from the repair attempt) so root cause is easy to find.
    """
    m = re.search(r"\{[\s\S]+\}", full_text)
    if m:
        candidate = m.group()
    else:
        # No matching closing brace at all is itself a truncation signal —
        # fall through to the first '{' so _log_parse_failure's truncation
        # heuristic gets a chance to run instead of just giving up here.
        first_brace = full_text.find("{")
        if first_brace == -1:
            logger.warning("AI response contained no JSON object")
            return None
        candidate = full_text[first_brace:]

    first_error = None
    result = None

    # Attempt 1: direct parse, tolerant of raw control chars in strings
    try:
        result = json.loads(candidate, strict=False)
    except Exception as e:
        first_error = e

    # Attempt 2: escape any raw control characters found inside string
    # literals (outside strings they're just whitespace and left alone).
    if result is None:
        try:
            repaired = _escape_control_chars_in_strings(candidate)
            result = json.loads(repaired, strict=False)
        except Exception:
            pass

    # Attempt 3: hand off to json_repair if it's installed. It's a
    # purpose-built library for exactly this problem (unescaped quotes,
    # trailing commas, missing brackets, etc.) and covers cases our
    # hand-rolled repair above doesn't. Soft dependency — skipped cleanly
    # if not installed.
    if result is None:
        try:
            from json_repair import repair_json
            parsed = repair_json(candidate, return_objects=True)
            if isinstance(parsed, dict) and parsed:
                result = parsed
        except ImportError:
            pass
        except Exception:
            pass

    if result is None:
        _log_parse_failure(full_text, candidate, first_error)
        return None

    return _sanitize_ranked_candidates(result)


def _sanitize_ranked_candidates(result: dict) -> dict:
    """
    Drop any ranked_candidates entry that isn't an object.

    On 2026-08-25, batch 3/6 came back with a well-formed JSON object whose
    ranked_candidates list held bare strings instead of {symbol, tier, ...}
    objects. json.loads succeeded — this is a SHAPE problem, not a syntax
    one — so nothing upstream caught it, and the first row.get("symbol") in
    call_ai_batched's merge loop crashed with 'str' object has no attribute
    'get' and took out the whole step, exactly the failure mode that
    function's own docstring says a single bad batch must not cause.
    Malformed entries are logged and dropped, same treatment as a batch
    that returns nothing.
    """
    ranked = result.get("ranked_candidates")
    if isinstance(ranked, list):
        clean = [r for r in ranked if isinstance(r, dict)]
        dropped = len(ranked) - len(clean)
        if dropped:
            logger.warning(
                f"AI returned {dropped} non-object ranked_candidates "
                f"entr{'y' if dropped == 1 else 'ies'} (expected "
                f"{{symbol, tier, ...}}) — dropped: "
                f"{[r for r in ranked if not isinstance(r, dict)]}"
            )
            result["ranked_candidates"] = clean
    return result


def _log_parse_failure(full_text: str, candidate: str, error: Exception) -> None:
    """Log everything needed to diagnose a JSON parse failure without
    having to go find a dumped file: the error, the exact text around it,
    and a heuristic check for whether this looks like max_tokens truncation."""
    logger.warning(f"JSON parse failed: {error}")

    pos = getattr(error, "pos", None)
    if pos is not None:
        start = max(0, pos - 120)
        end = min(len(candidate), pos + 60)
        snippet = candidate[start:end].replace("\n", "\\n")
        logger.warning(f"Text around failure point: ...{snippet}...")

    tail = full_text.rstrip()
    if not tail or tail[-1] not in "}]":
        logger.warning(
            f"Response does NOT end with a closing brace/bracket "
            f"(ends with: {tail[-60:]!r}) — likely truncated by max_tokens, consider raising it"
        )
    else:
        logger.warning(
            "Response ends with a proper closing brace — not a truncation signature, "
            "likely a formatting slip mid-response instead"
        )

    import tempfile
    try:
        script_dir = Path(__file__).parent
    except NameError:
        script_dir = Path.cwd()
    for d in (Path(tempfile.gettempdir()), script_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
            dump_path = d / "step19_ai_response_failed.json"
            dump_path.write_text(full_text, encoding="utf-8")
            logger.warning(f"Raw AI response saved to {dump_path} for inspection")
            break
        except Exception:
            continue
    else:
        logger.warning("Could not save raw AI response to disk (all candidate paths failed)")


def _escape_control_chars_in_strings(s: str) -> str:
    """Escape raw \\n, \\r, \\t that appear inside JSON string literals."""
    out = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            if ch == "\t":
                out.append("\\t")
                continue
            out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
    return "".join(out)


# ── Writes ─────────────────────────────────────────────────────────────────

def write_signal_enrichment(sb, result: dict, candidates: list[dict],
                            trade_date: str, provider: str) -> int:
    """
    Write AI narrative fields to signal_log and master_shortlist for ALL
    ranked candidates.

    NO TIER, NO CONVICTION — 29-Aug-2026. ai_tier/ai_conviction are no
    longer written (see the module docstring for the measurement that
    removed them). ai_conviction_reason is now pure narrative text
    (thesis/entry/invalidation/allocation/correlation), with no tier label
    prefixed onto it. Chase allowance is a fixed constant
    (SWING_CHASE_PCT_FLAT), not a clamped AI number — the AI is no longer
    asked to propose one at all.
    """
    ranked = result.get("ranked_candidates") or []
    if not ranked:
        return 0

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
    for item in ranked:
        sym = item.get("symbol")
        if not sym:
            continue

        action      = item.get("action", "SKIP")
        allocation  = float(item.get("suggested_allocation_pct") or 0)
        thesis      = item.get("thesis", "")
        entry_note  = item.get("entry_note", "")
        invalidation = item.get("invalidation", "")
        risks        = item.get("risks") or []
        catalyst     = item.get("catalyst", "")
        corr_group   = item.get("correlation_group") or ""
        lessons_used = item.get("lessons_applied") or []
        chase_note   = (item.get("chase_note") or "")[:200]

        # FIXED chase ceiling, not an AI number — see SWING_CHASE_PCT_FLAT's
        # own comment for the measurement behind this. Still 0 for anything
        # that isn't an actual entry, same asymmetry the old clamp had.
        max_chase_pct = SWING_CHASE_PCT_FLAT if action in ("ENTER_NOW", "ENTER_ON_DIP") else 0.0

        zone_high = zone_high_map.get(sym)
        ai_zone_high_extended = (
            round(float(zone_high) * (1 + max_chase_pct / 100), 2)
            if zone_high and max_chase_pct > 0 else None
        )

        conviction_reason = (
            f"{thesis} | Entry: {entry_note} | "
            f"Invalidation: {invalidation} | "
            f"Alloc: {allocation:.1f}% | Corr: {corr_group}"
        )[:800]

        ai_note = (
            f"[{action}] {thesis[:100]} | "
            + (f"Lessons: {', '.join(lessons_used[:2])}" if lessons_used else "")
        )[:500]

        signal_update = {
            "ai_conviction_reason": conviction_reason,
            "ai_suggested_action":  action,
            "ai_risks":             risks,
            "ai_catalyst":          catalyst,
            "ai_note":              ai_note,
            "ai_provider":          provider,
            "ai_max_chase_pct":     max_chase_pct,
            "ai_chase_rationale":   chase_note,
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
                "ai_conviction_reason": conviction_reason,
                "ai_risks":             risks,
                "ai_suggested_action":  action,
                "ai_note":              ai_note[:300],
                "ai_provider":          provider,
                "ai_shortlist_reason":  f"[{action}] {thesis[:150]}",
                "ai_max_chase_pct":     max_chase_pct,
                "ai_chase_rationale":   chase_note,
                "ai_zone_high_extended": ai_zone_high_extended,
            }).eq("date", trade_date).eq("symbol", sym).execute()
        except Exception as e:
            logger.warning(f"master_shortlist update failed for {sym}: {e}")

        written += 1

    logger.info(f"signal_log + master_shortlist: {written} candidates enriched")
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
    entries  = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]

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
                    f"entries:{len(entries)} total:{candidate_count} | "
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
        action = item.get("action", "SKIP")
        if not db_cmp:
            if action != "SKIP":
                issues.append(f"{sym}({action}): no DB price — output unverifiable")
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

    # ── ML AS SUPPORT, NOT JUST BACKUP ───────────────────────────────────────
    # Score every candidate with the trained conviction model and hand the
    # result to the LLM. Without this the LLM reasons purely from the narrative
    # in front of it, while a model fitted on realised outcomes sits unused on
    # disk. Where the two disagree, the disagreement is itself information —
    # the prompt block asks the model to explain it rather than defer to it.
    #
    # The block states the model's own trustworthiness. Right now only one
    # closed trade is signal-attributed, so it is presented as a weak prior;
    # a confident-looking 0.73 must not imply evidence that does not exist.
    ml_suffix = ""
    try:
        from ai.ml_support import ml_win_probability, is_ml_trustworthy, format_for_prompt
        ml_scores = ml_win_probability(candidates)
        if ml_scores:
            ok, why = is_ml_trustworthy(sb)
            # Held separately as well as appended: batching rebuilds the prompt
            # per batch from ctx, and appending only to this copy would have
            # silently dropped the ML second opinion from every batched run —
            # the model back on disk unused, which is the exact failure the
            # block was written to end.
            ml_suffix = format_for_prompt(ml_scores, ok, why)
            prompt += ml_suffix
            logger.info(f"  ML second opinion attached ({'usable' if ok else 'weak prior'}: {why})")
    except Exception as e:
        logger.debug(f"  ML second opinion unavailable: {e}")

    if DRY_RUN:
        logger.info(f"[DRY RUN] Prompt: {len(prompt)} chars")
        logger.info(f"[DRY RUN] Candidates: {[c.get('symbol') for c in candidates]}")
        return {"status": "dry_run", "prompt_chars": len(prompt),
                "candidates": len(candidates)}

    # Cache: reuse yesterday's verdict for a materially unchanged candidate
    # — see find_reusable_candidates' own docstring. Skipped entirely on
    # the WATCH-fallback path (no gate-passed candidates today): that
    # population is already a degraded, less-certain one, and is not worth
    # the added risk of also reusing a prior verdict on top of it.
    reused: dict[str, dict] = {}
    if not ctx.get("promoted_from_watch"):
        try:
            reused, symbols_to_rank = find_reusable_candidates(sb, candidates, trade_date)
        except Exception as e:
            logger.warning(f"  cache lookup failed ({e}) — analysing every candidate fresh")
            reused, symbols_to_rank = {}, [c.get("symbol") for c in candidates if c.get("symbol")]
    else:
        symbols_to_rank = [c.get("symbol") for c in candidates if c.get("symbol")]

    # Batched when the field is too large to answer in one response. Small
    # fields still go in a single call — see call_ai_batched.
    if symbols_to_rank:
        logger.info(f"Calling AI ({len(symbols_to_rank)} of {len(candidates)} "
                   f"candidates" + (f", {len(reused)} reused" if reused else "") + ")...")
        t0     = time.time()
        result = call_ai_batched(ctx, trade_date, suffix=ml_suffix,
                                 symbols_to_rank=symbols_to_rank)
        elapsed = time.time() - t0
    else:
        logger.info(f"  all {len(reused)} candidate(s) reused — no AI call needed today")
        result, elapsed = {"ranked_candidates": []}, 0.0

    if not result:
        # ML FALLBACK. Rather than exiting with nothing, rank on the trained
        # conviction model. Tiering degrades from reasoned to scored — no news,
        # event or correlation context — which is a large drop in nuance and a
        # small one in usefulness compared with emitting no tiering at all.
        # The output carries source='ml_fallback' so nothing downstream, and no
        # later post-mortem, mistakes it for an LLM decision. Operates on
        # every candidate, ignoring any cache hits — a rare degraded path
        # where correctness matters more than avoiding one redundant score.
        logger.warning("AI call returned nothing — trying the ML conviction model")
        try:
            from ai.ml_support import rank_by_ml
            result = rank_by_ml(candidates)
            reused = {}
        except Exception as e:
            logger.warning(f"  ML fallback unavailable: {e}")
            result = None
        if not result:
            logger.warning("AI unavailable and ML fallback produced nothing — step 19 non-fatal exit")
            return {"status": "ai_failed"}

    logger.info(f"AI responded in {elapsed:.1f}s")

    # Merge reused entries in BEFORE validation, so a reused verdict gets
    # exactly the same price/chase-pct safety checks a fresh one does —
    # today's price can still have moved within tolerance since yesterday.
    if reused:
        result = {**result,
                 "ranked_candidates": (result.get("ranked_candidates") or [])
                                       + list(reused.values())}
    _validate_ai_prices(result, candidates)

    provider = cfg("ai_provider", "unknown")
    ranked   = result.get("ranked_candidates") or []
    entries  = [r for r in ranked if r.get("action") in ("ENTER_NOW", "ENTER_ON_DIP")]
    waiting  = [r for r in ranked if r.get("action") == "WAIT_FOR_TRIGGER"]
    guidance = result.get("portfolio_guidance") or {}

    # Writes
    enriched  = write_signal_enrichment(sb, result, candidates, trade_date, provider)
    write_final_picks(sb, result, trade_date, provider, len(candidates))
    new_rules = write_self_improvement_lessons(sb, result, trade_date)
    pos_actions_written = write_position_actions(sb, result, ctx["positions"], trade_date)

    # Summary log
    logger.success(
        f"Step 19 done in {elapsed:.1f}s | "
        f"Entries:{len(entries)} Waiting:{len(waiting)} Total:{len(ranked)} | "
        f"Enriched:{enriched} | New rules:{new_rules} | "
        f"PosActions:{pos_actions_written} | "
        f"Sizing:{guidance.get('position_sizing_override','?')}"
    )
    logger.info(f"  Guidance: {guidance.get('new_positions_guidance','')}")
    logger.info(f"  Overweight: {guidance.get('sectors_to_overweight',[])} | "
                f"Underweight: {guidance.get('sectors_to_underweight',[])}")

    if entries:
        e_str = " | ".join(
            f"{r['symbol']}({r.get('action','?')},{r.get('suggested_allocation_pct',0):.0f}%)"
            for r in entries
        )
        logger.info(f"  ★ Entries: {e_str}")

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
        "entries":    len(entries),
        "waiting":    len(waiting),
        "total":      len(ranked),
        "enriched":   enriched,
        "new_rules":  new_rules,
        "sizing":     guidance.get("position_sizing_override"),
        "top_picks":  [r["symbol"] for r in entries[:5]],
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TradeOS v7 — Step 19: AI Decision Engine")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())