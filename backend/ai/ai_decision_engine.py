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
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg, cfg_int, get_trade_date

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")


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
              "low_30d,consol_range"
          )
          .eq("date", trade_date)
          .in_("symbol", symbols)
          .execute().data
    )
    msl_map = {r["symbol"]: r for r in msl_rows}


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
        merged["low_30d"]            = msl.get("low_30d")
        merged["consol_range"]       = msl.get("consol_range")
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
        }

    # ── Open positions — sector/strategy concentration ──
    pos_rows = (
        sb.table("open_positions")
          .select("symbol,sector,strategy,invested_value,pnl_pct,active_sl,lifecycle")
          .eq("status", "ACTIVE").execute().data
    )
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
        sector_rows = (
            sb.table("sector_strength")
              .select("sector,rank,top4_flag,sector_state,avg_rsi_daily,fii_flow_sector")
              .order("date", desc=True).limit(20).execute().data
        )

    # ── Lessons — sorted by live confidence ──
    lesson_rows = (
        sb.table("lessons")
          .select("id,corrective_rule,scenario_type,impacted_sector,"
                  "times_applied,times_worked,confidence,source")
          .eq("is_active", True)
          .order("times_applied", desc=True).limit(12).execute().data
    )
    for l in lesson_rows:
        l["live_confidence"] = _lesson_confidence(l)
    lesson_rows.sort(key=lambda x: x["live_confidence"], reverse=True)

    # ── Macro indicators — recent ──
    macro_rows = (
        sb.table("macro_indicators")
          .select("indicator_date,indicator_name,indicator_value,previous_value,change_bps")
          .order("indicator_date", desc=True).limit(8).execute().data
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
        for p in pos:
            lines.append(
                f"  {p.get('symbol','?')} | {p.get('sector','?')} | "
                f"P&L:{float(p.get('pnl_pct') or 0):+.1f}% | SL:{p.get('active_sl','?')} | "
                f"Lifecycle:{p.get('lifecycle','?')}"
            )
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
            "",
            _COLUMNS,
        ]
    for c in ctx["candidates"]:
        # Line 1: identity, price, position sizing context
        lines.append(
            f"  {c.get('symbol','?'):<12} | {c.get('sector','?')[:16]:<16} | "
            f"{c.get('signal_type','?'):<15} | Score:{float(c.get('score_adjusted',0) or 0):.1f} | "
            f"CMP:₹{c.get('current_price','?')} "
            f"Zone:₹{c.get('entry_zone_low','?')}-{c.get('entry_zone_high','?')} "
            f"Dist:{c.get('dist_entry_pct','?')}% | "
            f"DaysTrig:{c.get('days_to_trigger_est','?')} | "
            f"{c.get('lifecycle','?')}/{c.get('trend_maturity','?')} | "
            f"HoldSc:{c.get('holding_score','?')} ExpR:{c.get('expected_r_msl','?')}x "
            f"Valid:{c.get('validity_score','?')} | "
            f"Eng:{c.get('engines_count','?')} Conv:{c.get('convergence_pts','?')} | "
            f"SRk:{c.get('sector_rank_at_entry','?')} FII:{c.get('fii_flag','?')} | "
            f"MCap:₹{c.get('market_cap','?')}Cr Qual:{c.get('fundamental_quality','?')}"
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
            f"RkVel:{c.get('rank_vel_3d','?')}"
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
  ]
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

    # Build id lookup from candidates for signal_log update
    id_map = {c["symbol"]: c.get("id") for c in candidates}

    written = 0
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
            }).eq("date", trade_date).eq("symbol", sym).execute()
        except Exception as e:
            logger.warning(f"master_shortlist update failed for {sym}: {e}")

        written += 1

    logger.info(f"signal_log + master_shortlist: {written} candidates enriched")
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
                f"TIER_1:{len(tier1)} TIER_2:{len(tier2)} total:{candidate_count} | "
                f"{guidance.get('new_positions_guidance', '')[:150]} | "
                f"{guidance.get('capital_deployment_narrative', '')[:150]}"
            )[:500],
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

    # Summary log
    logger.success(
        f"Step 19 done in {elapsed:.1f}s | "
        f"TIER_1:{len(tier1)} TIER_2:{len(tier2)} TIER_3:{len(tier3)} | "
        f"Enriched:{enriched} | New rules:{new_rules} | "
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