"""
TradeOS v7 — Signal Output Daily Snapshot
Step 20.5: Runs after ai_decision_engine (step 19), before send_alerts (step 20).

PURPOSE
  Writes one immutable row per symbol to signal_output_daily.
  This is the single source of truth for:
    - send_alerts (replaces the former 8-table join in load_data)
    - performance_tracker (outcome_* columns filled weekly post-hoc)
    - Brain Engine ML/AI training queries
    - Audit trail: what exactly was known at decision time

IMMUTABILITY CONTRACT
  Non-outcome columns are NEVER updated after the initial write.
  Only outcome_* columns are written post-hoc by performance_tracker.py.
  This ensures the feature record is frozen at decision time, making it
  a reliable ML training source.

PROVENANCE TRACKING
  data_sources  : JSONB — which source tables contributed to this row
  entry_zone_source : which table provided entry_zone_low/high
  ai_tier_source    : which source provided the ai_tier classification
  expected_r_source : which table provided expected_r

SOURCE TABLES (read once per run):
  signal_log            → signal classification, scores, MSL labels, technicals
  ai_context.__FINAL_PICKS__ → per-symbol tier/conviction/rationale from step 19
  ai_context (per-symbol)    → ai_note, ai_conviction from step 18
  master_shortlist      → fallback for entry_zone_low/high and expected_r
  market_regime         → regime context snapshot
  fii_dii_flow          → FII/DII context snapshot
"""
import sys, json
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from loguru import logger
from config import get_supabase, today_ist, IST, DRY_RUN, is_kill_switch_active


def resolve_last_trading_day(sb, reference_date: str, max_lookback: int = 10) -> str:
    """
    Re-used pattern from append_history.py — find last valid trading day
    that has signal_log data.
    """
    from datetime import datetime as _dt, timedelta as _td
    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}
    candidate    = _dt.strptime(reference_date, "%Y-%m-%d").date()

    for _ in range(max_lookback):
        date_str = str(candidate)
        dow      = candidate.weekday()

        if dow in (5, 6):
            candidate -= _td(days=1)
            continue
        if date_str in holidays:
            candidate -= _td(days=1)
            continue

        probe = (
            sb.table("signal_log")
              .select("date")
              .eq("date", date_str)
              .limit(1)
              .execute().data
        )
        if probe:
            if date_str != reference_date:
                logger.info(f"  Trading day resolved: {reference_date} → {date_str}")
            return date_str

        candidate -= _td(days=1)

    raise RuntimeError(
        f"No valid trading day with signal_log data found "
        f"within {max_lookback} days before {reference_date}."
    )


def _resolve_implied_rr(sl: dict, msl: dict) -> float | None:
    """
    Reward:risk available at the snapshot price.

    Prefers the value generate_signals already computed. Falls back to deriving
    it from whichever stop/target/price are available, so a row is never left
    with a NULL R:R that the alert layer would then have to render as a blank —
    the blank is what made the digest look like static data.
    """
    v = sl.get("implied_rr")
    if v is not None:
        return v
    stop   = sl.get("planned_stop")   or msl.get("planned_stop")
    target = sl.get("planned_target") or msl.get("planned_target")
    cp     = sl.get("current_price")  or msl.get("current_price")
    if not (stop and target and cp):
        return None
    try:
        stop_f, target_f, cp_f = float(stop), float(target), float(cp)
    except (TypeError, ValueError):
        return None
    risk = cp_f - stop_f
    if risk <= 0:
        return None
    return round((target_f - cp_f) / risk, 3)


def main(force: bool = False):
    if is_kill_switch_active():
        return {}

    logger.info("STEP [20.5]: Signal Output Daily Snapshot")
    sb    = get_supabase()
    today = str(today_ist())

    try:
        today = resolve_last_trading_day(sb, today)
    except RuntimeError as exc:
        logger.error(str(exc))
        return {"status": "no_data"}

    # ── Idempotency ───────────────────────────────────────────────────────────
    existing = (
        sb.table("signal_output_daily")
          .select("symbol")
          .eq("date", today)
          .limit(1)
          .execute().data
    )
    if existing and not force:
        logger.info(f"signal_output_daily already snapshotted for {today} — skipping")
        return {"skipped": True}
    if existing and force:
        # Immutability is the default for a reason — this table is the
        # point-in-time record of what the system believed, and silently
        # rewriting it destroys that. But an unconditional guard also makes the
        # snapshot unrepairable: on 2026-07-27 step 19 crashed, so the snapshot
        # was taken with no AI tiers at all, and no amount of fixing step 19
        # afterwards could ever get them in. Explicit, logged, opt-in only.
        logger.warning(
            f"--force: OVERWRITING the existing snapshot for {today}. The "
            f"previous point-in-time record for this date is lost. Only correct "
            f"when an upstream step failed during the original run."
        )

    # ── Source 1: signal_log ──────────────────────────────────────────────────
    sig_rows = (
        sb.table("signal_log")
          .select("*")
          .eq("date", today)
          .execute().data
    )
    if not sig_rows:
        logger.warning(f"No signal_log rows for {today} — snapshot skipped")
        return {"skipped": True}
    sig_map = {r["symbol"]: r for r in sig_rows}
    logger.info(f"  signal_log: {len(sig_rows)} rows")

    # ── Source 2: ai_context.__FINAL_PICKS__ (step 19 ranked candidates) ─────
    ai_picks: dict[str, dict] = {}
    try:
        fp_row = (
            sb.table("ai_context")
              .select("conviction_reason")
              .eq("date", today)
              .eq("symbol", "__FINAL_PICKS__")
              .limit(1)
              .execute().data
        )
        if fp_row and fp_row[0].get("conviction_reason"):
            raw = json.loads(fp_row[0]["conviction_reason"])
            for c in raw.get("ranked_candidates", []):
                sym = c.get("symbol")
                if sym:
                    ai_picks[sym] = c
        logger.info(f"  __FINAL_PICKS__: {len(ai_picks)} ranked candidates")
    except Exception as e:
        logger.warning(f"  __FINAL_PICKS__ load failed (non-fatal): {e}")

    # ── Source 3: per-symbol ai_context rows (step 18 sentiment/conviction) ──
    sym_ai_map: dict[str, dict] = {}
    try:
        ai_rows = (
            sb.table("ai_context")
              .select("symbol,conviction,suggested_action,ai_note,conviction_reason")
              .eq("date", today)
              .neq("symbol", "__FINAL_PICKS__")
              .neq("symbol", "__MARKET_INTEL__")
              .execute().data
        )
        sym_ai_map = {r["symbol"]: r for r in ai_rows}
        logger.info(f"  per-symbol ai_context: {len(sym_ai_map)} rows")
    except Exception as e:
        logger.warning(f"  per-symbol ai_context load failed (non-fatal): {e}")

    # ── Source 4: master_shortlist (fallback for entry_zone, expected_r) ─────
    msl_map: dict[str, dict] = {}
    try:
        msl_rows = (
            sb.table("master_shortlist")
            .select("symbol,entry_zone_low,entry_zone_high,expected_r,"
                    "dist_entry_pct,days_to_trigger_est,composite_score,"
                    "current_price,entry_timing_type,price_location,"
                    "zone_hot_adjusted,ai_max_chase_pct,ai_chase_rationale,"
                    "ai_zone_high_extended,"
                    # Trade plan — the fallback source when signal_log predates
                    # migration 002. Without these in the SELECT the fallback
                    # silently resolves to None and the snapshot stays NULL.
                    "planned_stop,planned_target,planned_risk_pct,planned_entry,planned_stop_source")
            .eq("date", today)
            .execute().data
        )
        msl_map = {r["symbol"]: r for r in msl_rows}
        logger.info(f"  master_shortlist: {len(msl_map)} rows (fallback source)")
    except Exception as e:
        logger.warning(f"  master_shortlist load failed (non-fatal): {e}")

    # ── Source 5: market_regime context ──────────────────────────────────────
    regime_ctx: dict = {}
    try:
        rr = (
            sb.table("market_regime")
              .select(
                  "regime,predicted_regime,regime_confidence,nifty_price,"
                  "india_vix,avg_sector_breadth,nifty_1d_chg_pct,"
                  "above_200dma_pct,advance_decline_ratio,nifty_pcr"
              )
              .eq("date", today)
              .limit(1)
              .execute().data
        )
        regime_ctx = rr[0] if rr else {}
        if not regime_ctx:
            # fallback to most recent
            rr2 = (
                sb.table("market_regime")
                  .select(
                      "regime,predicted_regime,regime_confidence,nifty_price,"
                      "india_vix,avg_sector_breadth,nifty_1d_chg_pct,"
                      "above_200dma_pct,advance_decline_ratio,nifty_pcr"
                  )
                  .order("date", desc=True).limit(1).execute().data
            )
            regime_ctx = rr2[0] if rr2 else {}
    except Exception as e:
        logger.warning(f"  market_regime load failed (non-fatal): {e}")

# ── Source 6: fii_dii_flow ────────────────────────────────────────────────
    fii_ctx: dict = {}
    try:
        fr = (
            sb.table("fii_dii_flow")
              .select("fii_net,fii_net_5d,fii_net_20d,fii_flag,dii_net,dii_flag")
              .order("date", desc=True)
              .limit(1)
              .execute().data
        )
        fii_ctx = fr[0] if fr else {}
    except Exception as e:
        logger.warning(f"  fii_dii_flow load failed (non-fatal): {e}")

    # ── Source 7: sector_strength fallback ───────────────────────────────────
    # Used when signal_log.sector_rank_at_entry is NULL because sector_strength
    # had no data for today when generate_signals ran.
    sector_rank_map: dict[str, int] = {}
    try:
        sec_rows = sb.table("sector_strength").select("sector,rank").eq("date", today).execute().data
        if not sec_rows:
            _ls = sb.table("sector_strength").select("date").order("date", desc=True).limit(1).execute().data
            if _ls:
                sec_rows = sb.table("sector_strength").select("sector,rank").eq("date", _ls[0]["date"]).execute().data
        sector_rank_map = {r["sector"]: r["rank"] for r in sec_rows if r.get("rank")}
        logger.info(f"  sector_rank_map: {len(sector_rank_map)} sectors")
    except Exception as e:
        logger.warning(f"  sector_rank fallback load failed (non-fatal): {e}")

    # ── Source 8: industry_strength fallback ─────────────────────────────────
    # industry_state removed — rank only is sufficient alongside sector_rank.
    industry_rank_map: dict[str, int] = {}
    try:
        ind_rows = sb.table("industry_strength").select("industry,rank").eq("date", today).execute().data
        if not ind_rows:
            _li = sb.table("industry_strength").select("date").order("date", desc=True).limit(1).execute().data
            if _li:
                ind_rows = sb.table("industry_strength").select("industry,rank").eq("date", _li[0]["date"]).execute().data
        industry_rank_map = {r["industry"]: r["rank"] for r in ind_rows if r.get("rank")}
        logger.info(f"  industry_rank_map: {len(industry_rank_map)} industries")
    except Exception as e:
        logger.warning(f"  industry_rank fallback load failed (non-fatal): {e}")

    # ── Build output rows ─────────────────────────────────────────────────────
    output_rows = []
    tier_counts: dict[str, int] = {}

    for sym, sl in sig_map.items():
        msl      = msl_map.get(sym, {})
        ai_sym   = sym_ai_map.get(sym, {})
        ai_pick  = ai_picks.get(sym, {})

        # ── Score layers ──────────────────────────────────────────────────────
        score_pre  = float(sl.get("score")          or 0)
        score_post = float(sl.get("score_adjusted") or score_pre)
        ai_delta   = round(score_post - score_pre, 2)

        # ── AI tier resolution (with provenance) ──────────────────────────────
        # tier: from __FINAL_PICKS__ ranked_candidates — field is "tier"
        ai_tier_fp  = ai_pick.get("tier")
        # fallback: infer from signal_type if tier not in picks
        ai_tier_sym = None   # per-symbol ai_context doesn't carry tier
        # Infer from signal_type when neither AI source has a tier
        sig_type = sl.get("signal_type", "")
        ai_tier_inferred = (
            "TIER_1"       if sig_type == "PRIME_SETUP" else
            "TIER_2"       if sig_type in ("BREAKOUT_SETUP", "REENTRY_SETUP",
                                           "MOMENTUM_CONTINUATION") else
            "TIER_3"       if sig_type in ("BUY_CANDIDATE", "STAGED_ENTRY") else
            "WATCH_CLOSELY" if sig_type == "WATCH" else
            None
        )
        ai_tier        = ai_tier_fp or ai_tier_sym or ai_tier_inferred
        ai_tier_source = ("final_picks" if ai_tier_fp
                          else "per_symbol" if ai_tier_sym
                          else "inferred"  if ai_tier_inferred
                          else None)

        tier_counts[ai_tier or "untiered"] = tier_counts.get(ai_tier or "untiered", 0) + 1

        # ── Entry zone resolution (with provenance) ───────────────────────────
        ez_low_sl   = sl.get("entry_zone_low")
        ez_low_msl  = msl.get("entry_zone_low")
        ez_high_sl  = sl.get("entry_zone_high")
        ez_high_msl = msl.get("entry_zone_high")

        entry_zone_low   = ez_low_sl  or ez_low_msl
        entry_zone_high  = ez_high_sl or ez_high_msl
        entry_zone_source = ("signal_log"      if ez_low_sl
                             else "master_shortlist" if ez_low_msl
                             else None)

        # ── expected_r resolution (with provenance) ───────────────────────────
        er_sl   = sl.get("expected_r_msl")
        er_msl  = msl.get("expected_r")
        expected_r        = er_sl or er_msl
        expected_r_source = ("signal_log"      if er_sl
                             else "master_shortlist" if er_msl
                             else None)

        # ── AI conviction reason — guard size ────────────────────────────────
        # ai_conviction_reason: signal_log already has this written by step 18/19
        # (format: "[TIER_X] thesis | Entry: ... | Invalidation: ... | Alloc: ...")
        # ai_pick.get("thesis") is the cleaner version from __FINAL_PICKS__ if needed
        ai_conv_reason = (sl.get("ai_conviction_reason")
                          or ai_pick.get("thesis")
                          or ai_pick.get("rationale"))
        if isinstance(ai_conv_reason, str) and len(ai_conv_reason) > 5000:
            ai_conv_reason = ai_conv_reason[:5000]

        # ai_risks: field name in ranked_candidates is "risks" (list), not "key_risks"
        ai_risks_raw = ai_pick.get("risks") or ai_pick.get("key_risks")
        if isinstance(ai_risks_raw, list):
            ai_risks = "; ".join(str(x) for x in ai_risks_raw)
        elif isinstance(ai_risks_raw, str):
            ai_risks = ai_risks_raw
        else:
            ai_risks = sl.get("ai_risks")
        if isinstance(ai_risks, str) and len(ai_risks) > 2000:
            ai_risks = ai_risks[:2000]

        # ── Provenance record ─────────────────────────────────────────────────
        data_sources = {
            "signal_log":              True,
            "ai_context_per_symbol":   sym in sym_ai_map,
            "ai_context_final_picks":  sym in ai_picks,
            "master_shortlist":        sym in msl_map,
            "market_regime":           bool(regime_ctx),
            "fii_dii_flow":            bool(fii_ctx),
            "written_at":              datetime.now(IST).isoformat(),
            "pipeline_date":           today,
            
        }
        # ── Sector / industry rank with fallback ──────────────────────────────
        _sector  = sl.get("sector") or ""
        _ind     = sl.get("industry") or ""

        # ── Assemble row ──────────────────────────────────────────────────────
        row = {
            # Identity
            "date":           today,
            "symbol":         sym,
            "company_name":   sl.get("company_name"),
            "sector":         sl.get("sector"),
            "industry":       sl.get("industry"),
            "strategy":       sl.get("strategy"),

            # Signal classification
            "signal_type":    sl.get("signal_type"),
            "signal_subtype": sl.get("signal_subtype"),
            "position_state": sl.get("position_state"),
            "filter_reason":  sl.get("filter_reason"),
            "near_miss_data": (json.loads(sl["near_miss_data"])
                               if isinstance(sl.get("near_miss_data"), str)
                               else sl.get("near_miss_data")),
            "eap_action":         sl.get("eap_action"),
            # Pre-results event context (enrichment, not blocking)
            "pre_results_flag":    sl.get("pre_results_flag", False),
            "upcoming_event_type": sl.get("upcoming_event_type"),
            "upcoming_event_days": sl.get("upcoming_event_days"),
            

            # Scores
            "score":           score_pre,
            "score_adjusted":  score_post,
            "final_score":     sl.get("score"),         # same as score for non-adjusted
            "screener_score":  msl.get("composite_score"),
            "ai_sentiment_delta": ai_delta,

            # AI enrichment
            "ai_tier":               ai_tier,
            # Read ai_conviction, ai_note, ai_suggested_action directly from signal_log
            # — step 18/19 already writes these back to signal_log
            "ai_conviction":         sl.get("ai_conviction") or ai_pick.get("conviction"),
            "ai_conviction_reason":  ai_conv_reason,
            "ai_note":               sl.get("ai_note"),
            # "action" in ranked_candidates = "ENTER_NOW", "SKIP", "WAIT_FOR_TRIGGER" etc.
            "ai_suggested_action":   (sl.get("ai_suggested_action")
                                      or ai_pick.get("action")
                                      or ai_pick.get("suggested_action")),
            "ai_risks":              ai_risks,
            # Chase-zone fields (step 19 CHASE GUIDANCE) — signal_log primary,
            # master_shortlist fallback. Explicit is-not-None check (not "or")
            # because 0.0 is the common, legitimate "no chase approved" value
            # for ai_max_chase_pct and must not be treated as missing.
            "ai_max_chase_pct":      (sl.get("ai_max_chase_pct")
                                       if sl.get("ai_max_chase_pct") is not None
                                       else msl.get("ai_max_chase_pct")),
            "ai_chase_rationale":    sl.get("ai_chase_rationale") or msl.get("ai_chase_rationale"),
            "ai_zone_high_extended": (sl.get("ai_zone_high_extended")
                                       if sl.get("ai_zone_high_extended") is not None
                                       else msl.get("ai_zone_high_extended")),
            # 2026-07: event_calendar sector-join audit trail (ai_decision_engine
            # only writes these to signal_log, no master_shortlist fallback exists)
            "sector_event_name":      sl.get("sector_event_name"),
            "sector_event_bias":      sl.get("sector_event_bias"),
            "sector_event_intensity": sl.get("sector_event_intensity"),
            "regulatory_alert_action": sl.get("regulatory_alert_action"),
            "regulatory_alert_note":   sl.get("regulatory_alert_note"),

            # Entry parameters
            "entry_zone_low":     entry_zone_low,
            "entry_zone_high":    entry_zone_high,
            "zone_hot_adjusted":  (sl.get("zone_hot_adjusted")
                                    if sl.get("zone_hot_adjusted") is not None
                                    else msl.get("zone_hot_adjusted")),
            "current_price":      sl.get("current_price")    or msl.get("current_price"),
            "entry_timing_type":  sl.get("entry_timing_type") or msl.get("entry_timing_type"),
            "price_location":     sl.get("price_location")   or msl.get("price_location"),
            "dist_entry_pct":     sl.get("dist_entry_pct") or msl.get("dist_entry_pct"),
            "expected_r":         expected_r,
            # ── Trade plan (migration 002) ────────────────────────────────
            # THE CHAIN USED TO BREAK HERE. compute_msl writes planned_stop /
            # planned_target to master_shortlist and generate_signals copies
            # them onto signal_log, but this snapshot never carried them
            # forward — so all 38 rows of signal_output_daily had them NULL.
            # send_alerts reads ONLY signal_output_daily, which is why the
            # digest could show a candidate but never its stop, target or R:R,
            # and therefore never an actionable decision point.
            #
            # signal_log first, master_shortlist as fallback for rows written
            # before the columns existed.
            "planned_stop":     sl.get("planned_stop")     or msl.get("planned_stop"),
            "planned_target":   sl.get("planned_target")   or msl.get("planned_target"),
            "planned_risk_pct": sl.get("planned_risk_pct") or msl.get("planned_risk_pct"),
            # Carries the REASON a plan is absent (e.g. risk_too_wide_8.2pct)
            # so the alert can say "stop too wide to size" instead of the
            # much less useful "missing stop, target".
            "planned_stop_source": sl.get("planned_stop_source") or msl.get("planned_stop_source"),
            # implied_rr is reward:risk AT THE SNAPSHOT PRICE, so it is the
            # number that decides whether the trade is still worth taking when
            # you read the alert. Recomputed here when absent rather than left
            # NULL, because a stale ratio is worse than a fresh one.
            "implied_rr":       _resolve_implied_rr(sl, msl),
            "validity_score":     sl.get("validity_score"),
            "days_to_trigger_est": (sl.get("days_to_trigger_est")
                                    or msl.get("days_to_trigger_est")),

            # MSL intelligence labels
            "momentum_state":    sl.get("momentum_state"),
            "momentum_phase":    sl.get("momentum_phase"),
            "velocity_state":    sl.get("velocity_state"),
            "trend_maturity":    sl.get("trend_maturity"),
            "lifecycle":         sl.get("lifecycle"),
            "struct_edge":       sl.get("struct_edge"),
            "breakout_readiness": sl.get("breakout_readiness"),
            "holding_score":     sl.get("holding_score"),
            "risk_score":        sl.get("risk_score"),
            "momentum_score":    sl.get("momentum_score"),
            "institutional_score": sl.get("institutional_score"),
            "persistent_phase":  sl.get("persistent_phase"),
            "reentry_mode":      sl.get("reentry_mode"),

            # Key technicals
            "rsi_daily":          sl.get("rsi_daily"),
            "rsi_weekly":         sl.get("rsi_weekly"),
            "rsi_monthly":        sl.get("rsi_monthly"),
            "adx":                sl.get("adx"),
            "vol_ratio":          sl.get("vol_ratio"),
            "delivery_pct":       sl.get("delivery_pct"),
            "atr_pct":            sl.get("atr_pct"),
            "dist_sma50":         sl.get("dist_sma50"),
            "ret_6m":             sl.get("ret_6m"),
            "rs_vs_nifty":        sl.get("rs_vs_nifty"),
            "bb_context":         sl.get("bb_context"),
            "bb_squeeze":         sl.get("bb_squeeze"),
            "vwap_alignment":     sl.get("vwap_alignment"),
            "volume_trend":       sl.get("volume_trend"),
            "weekly_structure":   sl.get("weekly_structure"),
            "macd_direction":     sl.get("macd_direction"),
            "psar_dual_confirmed": sl.get("psar_dual_confirmed"),
            "ha_signal":          sl.get("ha_signal"),
            "stoch_context":      sl.get("stoch_context"),
            "ma_alignment_score": sl.get("ma_alignment_score"),

            # Market context snapshot
            "regime":               sl.get("active_regime") or regime_ctx.get("regime"),
            "predicted_regime":     regime_ctx.get("predicted_regime"),
            "regime_confidence":    regime_ctx.get("regime_confidence"),
            "nifty_price":          regime_ctx.get("nifty_price"),
            "india_vix":            sl.get("india_vix") or regime_ctx.get("india_vix"),
            "avg_sector_breadth":   regime_ctx.get("avg_sector_breadth"),
            "nifty_1d_chg_pct":     regime_ctx.get("nifty_1d_chg_pct"),
            "nifty_5d_chg_pct":     sl.get("nifty_5d_chg_pct") or regime_ctx.get("nifty_5d_chg_pct"),
            "above_200dma_pct":     sl.get("above_200dma_pct") or regime_ctx.get("above_200dma_pct"),
            "advance_decline_ratio": regime_ctx.get("advance_decline_ratio"),
            "nifty_pcr":            regime_ctx.get("nifty_pcr"),
            "fii_flag":             sl.get("fii_flag") or fii_ctx.get("fii_flag"),
            "fii_net":              fii_ctx.get("fii_net"),
            "fii_net_5d":           fii_ctx.get("fii_net_5d"),
            "fii_net_20d":          sl.get("fii_net_20d_ctx") or fii_ctx.get("fii_net_20d"),
            "dii_net":              fii_ctx.get("dii_net"),
            "dii_flag":             fii_ctx.get("dii_flag"),
            "sector_rank_at_entry": sl.get("sector_rank_at_entry") or sector_rank_map.get(_sector),
            "industry_rank":        sl.get("industry_rank") or industry_rank_map.get(_ind),
            "asm_flag":             sl.get("asm_flag"),

            # Provenance
            "data_sources":      data_sources,
            "entry_zone_source": entry_zone_source,
            "ai_tier_source":    ai_tier_source,
            "expected_r_source": expected_r_source,

            # Outcome columns — NULL at write time
            "outcome_entered":       None,
            "outcome_entry_price":   None,
            "outcome_exit_price":    None,
            "outcome_hold_days":     None,
            "outcome_return_pct":    None,
            "outcome_category":      None,
            "outcome_exit_reason":   None,
            "outcome_notes":         None,
        }
        output_rows.append(row)

    if not output_rows:
        logger.warning("No rows to write — snapshot skipped")
        return {"skipped": True}

    # ── Schema check BEFORE the write ────────────────────────────────────────
    # One key in this payload that is not a real column fails the upsert with
    # PGRST 42703, and step 20.5 is fatal — the evening pipeline stops before
    # alerts are built. The raw error names a single column and gives no hint
    # that a migration is missing, so check up front and say exactly what to do.
    #
    # This costs one SELECT ... LIMIT 1 per run. planned_stop_source shipped in
    # the payload before its column existed and would have taken the pipeline
    # down on the next run.
    try:
        probe = (sb.table("signal_output_daily").select("*").limit(1)
                   .execute().data or [{}])
        if probe and probe[0]:
            unknown = sorted(set(output_rows[0]) - set(probe[0]))
            if unknown:
                raise SystemExit(
                    f"\n  signal_output_daily is missing {len(unknown)} column(s) "
                    f"the snapshot writes: {', '.join(unknown)}\n"
                    f"  Apply the pending migration in backend/db/migrations/ "
                    f"before running the pipeline.\n"
                )
    except SystemExit:
        raise
    except Exception as e:
        # A probe failure must not itself break the run.
        logger.debug(f"  schema probe skipped: {e}")

    # ── Write ─────────────────────────────────────────────────────────────────
    if not DRY_RUN:
        written = 0
        for i in range(0, len(output_rows), 100):
            sb.table("signal_output_daily").upsert(
                output_rows[i:i+100], on_conflict="date,symbol"
            ).execute()
            written += len(output_rows[i:i+100])
        logger.info(f"✓ signal_output_daily: {written} rows written for {today}")
    else:
        logger.info(f"[DRY RUN] Would write {len(output_rows)} rows to signal_output_daily")

    # ── Diagnostics ───────────────────────────────────────────────────────────
    ez_from_sl  = sum(1 for r in output_rows if r["entry_zone_source"] == "signal_log")
    ez_from_msl = sum(1 for r in output_rows if r["entry_zone_source"] == "master_shortlist")
    ez_missing  = sum(1 for r in output_rows if r["entry_zone_source"] is None)
    tier_str    = " | ".join(f"{t}:{c}" for t, c in sorted(tier_counts.items()))

    logger.info(f"  Tier distribution: {tier_str}")
    logger.info(
        f"  Entry zone sources: signal_log={ez_from_sl} "
        f"master_shortlist={ez_from_msl} missing={ez_missing}"
    )
    if ez_missing > 0:
        missing_syms = [r["symbol"] for r in output_rows
                        if r["entry_zone_source"] is None][:8]
        logger.warning(
            f"  {ez_missing} symbols have no entry_zone — "
            f"compute_msl may have missed them: {missing_syms}"
        )

    return {
        "snapshotted":        len(output_rows),
        "date":               today,
        "tier_distribution":  tier_counts,
        "ez_missing":         ez_missing,
    }


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="TradeOS v7 — step 20.5 snapshot")
    _ap.add_argument("--force", action="store_true",
                     help="overwrite an existing snapshot for today (use only to "
                          "repair a run where an upstream step failed)")
    main(force=_ap.parse_args().force)