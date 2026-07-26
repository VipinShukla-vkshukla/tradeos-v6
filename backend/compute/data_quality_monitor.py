"""
TradeOS v6 — Data Quality Monitor v4
======================================
Step 21 in run_pipeline.py — always last, always non-fatal.

WHAT CHANGED FROM v3 → v4:

ROOT CAUSE FIXES:
  1. Trade date now resolved via stock_data_daily probe (same fix as step 15).
     v3 used today_ist() directly, so at 1 AM on May 6 it checked May 6 tables
     that had no data yet — causing 14/17 "missing" false positives.

  2. C08 removed (ai_context.context_json column does not exist in production schema).

  3. C11 removed (__SHORTLIST__ concept was deprecated — generates false ERRORs).

  4. C13/C14 removed (ai_context.id and context_json columns do not exist).

  5. Telegram import fixed: send_message (correct) vs send_telegram_message (broken).

  6. C06 fixed: was querying master_shortlist.score (column removed), now uses
     final_score and also checks compute_msl ran with ≥70 enriched rows.

NEW CHECKS (aligned with 21-step Phase 2 pipeline):
  C08: __MARKET_INTEL__ validity — step 18 wrote for trade_date + conviction_reason parseable
  C09: __FINAL_PICKS__ validity  — step 19 wrote for trade_date + portfolio_guidance present
  C10: Candidate price coverage  — every TIER_1/TIER_2 symbol has non-null prices in MSL
       (catches the hallucination bug: null prices → AI invents numbers)
  C14: FII/regime/sizing consistency
  C15: Signal enrichment coverage — % of BUY candidates with ai_conviction set
  C16: Lesson freshness           — AI lessons written in last 2 days
  C17: Near-miss data coverage    — WATCH signals should carry near_miss_data
  C18: Entry zone validity        — zone_low < zone_high for all MSL candidates
  C19: Tier distribution sanity   — TIER_1 count between 1–10

CHECK CATALOGUE (19 checks):
  C01  chartink_row_count         — Nifty 500 count in band (450–510)
  C02  rsi_range                  — RSI 0–100 for all stocks in trade_date
  C03  vol_ratio_cap              — Auto-cap outliers at 50x (safe auto-correct)
  C04  delivery_pct_bounds        — Delivery % 0–100
  C05  signal_score_range         — signal_log scores in 0–120
  C06  msl_completeness           — compute_msl wrote ≥70 enriched rows; score jumps
  C07  pipeline_completeness      — All 21 steps wrote data for trade_date
  C08  market_intel_validity      — __MARKET_INTEL__ present + parseable for trade_date
  C09  final_picks_validity       — __FINAL_PICKS__ present + portfolio_guidance for trade_date
  C10  candidate_price_coverage   — TIER_1/TIER_2 candidates have prices (no hallucination risk)
  C11  regime_ml_vs_manual        — ML predicted vs manual regime diff ≤1 tier
  C12  positions_vs_regime_cap    — Open positions within regime max
  C13  fii_regime_consistency     — FII flag/regime/sizing alignment
  C14  signal_enrichment_coverage — % of BUY candidates with ai_conviction
  C15  lesson_freshness           — AI lessons written in last 2 days
  C16  near_miss_coverage         — WATCH signals carry near_miss_data
  C17  entry_zone_validity        — zone_low < zone_high for MSL candidates
  C18  tier_distribution          — TIER_1 count between 1–10
  C19  signal_date_alignment      — signal_log trade_date matches stock_data_daily trade_date
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    get_supabase, today_ist, IST,
    is_kill_switch_active, logger,
    get_trade_date,
)

# ── Thresholds ────────────────────────────────────────────────────────────────

CHARTINK_ROW_MIN   = 450
CHARTINK_ROW_MAX   = 510
VOL_RATIO_CAP      = 50.0
SCORE_MIN          = 0
SCORE_MAX          = 150
MSL_JUMP_WARN      = 20
MSL_MIN_ENRICHED   = 70     # compute_msl should write ≥70 rows with final_score
DELIVERY_MIN       = 0.0
DELIVERY_MAX       = 100.0
ENRICH_COVERAGE_WARN = 0.5  # warn if <50% of BUY signals have ai_conviction
LESSON_STALE_DAYS  = 2      # warn if no AI lessons written in 2 days
NEAR_MISS_MIN_PCT  = 0.5    # warn if <50% of WATCH signals lack near_miss_data
TIER1_MIN          = 1
TIER1_MAX          = 10

REGIME_MAX_POSITIONS = {
    "TRENDING": 8, "NEUTRAL": 7, "CAUTION": 5, "RISK OFF": 3,
}
REGIME_TIER = {"TRENDING": 0, "NEUTRAL": 1, "CAUTION": 2, "RISK OFF": 3}
REGIME_ML_TIER_ERROR = 2

SIZING_REGIME_COMPAT = {
    # (regime, sizing) pairs that are allowed
    ("TRENDING",  "FULL"),
    ("TRENDING",  "REDUCED_25PCT"),
    ("NEUTRAL",   "FULL"),
    ("NEUTRAL",   "REDUCED_25PCT"),
    ("NEUTRAL",   "HALF"),
    ("CAUTION",   "REDUCED_25PCT"),
    ("CAUTION",   "HALF"),
    ("CAUTION",   "MINIMAL"),
    ("RISK OFF",  "MINIMAL"),
    ("RISK OFF",  "HALF"),
}

BUY_SIGNAL_TYPES = {
    "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
    "REENTRY_SETUP", "STAGED_ENTRY", "MARKET_TOP_PICK",
}


# ── Result builder ────────────────────────────────────────────────────────────

def _result(check_name, ok, severity_if_fail, message, value="", affected=None):
    return {
        "check":    check_name,
        "ok":       ok,
        "severity": "OK" if ok else severity_if_fail,
        "message":  message,
        "value":    value,
        "affected": affected or [],
    }


# ── C01: Chartink Row Count ───────────────────────────────────────────────────

def c01_chartink_row_count(sb, td):
    cnt = sb.table("chartink_raw_data").select("id", count="exact").eq("date", td).execute().count or 0
    ok  = CHARTINK_ROW_MIN <= cnt <= CHARTINK_ROW_MAX
    sev = "ERROR" if cnt == 0 else "WARN"
    return _result("C01_chartink_row_count", ok, sev,
        f"chartink_raw_data: {cnt} rows for {td} (expected {CHARTINK_ROW_MIN}–{CHARTINK_ROW_MAX})",
        value=str(cnt))


# ── C02: RSI Range ───────────────────────────────────────────────────────────

def c02_rsi_range(sb, td):
    rows = sb.table("stock_data_daily").select("symbol,rsi_daily").eq("date", td).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("rsi_daily") is not None
            and not (0 <= float(r["rsi_daily"]) <= 100)]
    return _result("C02_rsi_range", len(bad) == 0, "WARN",
        f"{len(bad)} stocks with RSI outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C03: Vol Ratio Cap (auto-correct) ────────────────────────────────────────

def c03_vol_ratio_cap(sb, td):
    rows    = sb.table("stock_data_daily").select("symbol,vol_ratio").eq("date", td).execute().data
    outliers = [r["symbol"] for r in rows
                if r.get("vol_ratio") is not None and float(r["vol_ratio"]) > VOL_RATIO_CAP]
    capped  = 0
    for sym in outliers:
        try:
            sb.table("stock_data_daily").update({"vol_ratio": VOL_RATIO_CAP}) \
              .eq("date", td).eq("symbol", sym).execute()
            capped += 1
        except Exception:
            pass
    return _result("C03_vol_ratio_cap", len(outliers) == 0, "WARN",
        f"{len(outliers)} vol_ratio outliers auto-capped to {VOL_RATIO_CAP}x"
        + (f": {outliers[:5]}" if outliers else ""),
        value=str(capped), affected=outliers[:10])


# ── C04: Delivery Pct Bounds ─────────────────────────────────────────────────

def c04_delivery_pct_bounds(sb, td):
    rows = sb.table("stock_data_daily").select("symbol,delivery_pct").eq("date", td).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("delivery_pct") is not None
            and not (DELIVERY_MIN <= float(r["delivery_pct"]) <= DELIVERY_MAX)]
    return _result("C04_delivery_pct_bounds", len(bad) == 0, "WARN",
        f"{len(bad)} stocks with delivery_pct outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C05: Signal Score Range ───────────────────────────────────────────────────

def c05_signal_score_range(sb, td):
    sigs = sb.table("signal_log").select("symbol,score,score_adjusted").eq("date", td).execute().data
    bad  = [r["symbol"] for r in sigs
            if r.get("score") is not None
            and not (SCORE_MIN <= float(r["score"]) <= SCORE_MAX)]
    # Also flag implausibly high score_adjusted separately
    extreme = [r["symbol"] for r in sigs
               if r.get("score_adjusted") is not None
               and float(r["score_adjusted"]) > 180]  # true anomaly threshold
    msg = (
        f"{len(bad)} signals with score outside {SCORE_MIN}–{SCORE_MAX}"
        + (f": {bad[:5]}" if bad else " ✅")
        + (f" | {len(extreme)} extreme score_adjusted >180: {extreme}" if extreme else "")
    )
    return _result("C05_signal_score_range", len(bad) == 0 and len(extreme) == 0,
                   "WARN", msg, value=str(len(bad)), affected=bad[:10])


# ── C06: MSL Completeness + Score Jumps ──────────────────────────────────────

def c06_msl_completeness(sb, td):
    """
    Two sub-checks:
    1. compute_msl wrote ≥70 rows with non-null final_score for trade_date.
       (ingest_sheets writes ~31 rows; compute_msl should write 70-100+)
    2. Score jumps >20 pts vs msl_history yesterday — flags anomalies.
    """
    # Sub-check 1: row count + enrichment
    enriched_rows = (
        sb.table("master_shortlist")
          .select("symbol,final_score")
          .eq("date", td)
          .not_.is_("final_score", "null")
          .execute().data
    )
    enriched_count = len(enriched_rows)
    enriched_ok    = enriched_count >= MSL_MIN_ENRICHED

    if not enriched_ok:
        return _result("C06_msl_completeness", False, "ERROR",
            f"compute_msl only wrote {enriched_count} enriched rows for {td} "
            f"(expected ≥{MSL_MIN_ENRICHED}) — step 14 may have used wrong date",
            value=str(enriched_count))

    # Sub-check 2: score jumps (informational — WARN only)
    yesterday = str((datetime.strptime(td, "%Y-%m-%d") - timedelta(days=1)).date())
    now_map   = {r["symbol"]: float(r["final_score"]) for r in enriched_rows}
    try:
        yest_rows = (
            sb.table("msl_history")
              .select("symbol,final_score")
              .eq("date", yesterday)
              .not_.is_("final_score", "null")
              .execute().data
        )
        yest_map = {r["symbol"]: float(r["final_score"]) for r in yest_rows}
        jumps = sorted(
            [{"symbol": s, "delta": round(now_map[s] - yest_map[s], 1)}
             for s in now_map if s in yest_map
             and abs(now_map[s] - yest_map[s]) > MSL_JUMP_WARN],
            key=lambda j: abs(j["delta"]), reverse=True,
        )
    except Exception:
        jumps = []

    ok  = len(jumps) == 0
    msg = (
        f"compute_msl: {enriched_count} enriched rows ✅"
        + (f" | {len(jumps)} score jumps >{MSL_JUMP_WARN}pts: "
           f"{[(j['symbol'], j['delta']) for j in jumps[:3]]}" if jumps else "")
    )
    return _result("C06_msl_completeness", ok, "WARN", msg,
        value=str(enriched_count), affected=[j["symbol"] for j in jumps[:10]])


# ── C07: Pipeline Completeness ────────────────────────────────────────────────

def c07_pipeline_completeness(sb, td):
    today = str(today_ist())
    # For tables that write using today_ist() rather than trade_date,
    # accept either today or trade_date as valid
    date_window = list({td, today})  # e.g. ["2026-05-08", "2026-05-09"]

    steps = [
        # (table, date_col, step_label, severity, use_window)
        # use_window=True → accept td OR today (step writes with today_ist())
        ("market_news",           "news_date",      "01_market_news",        "WARN",  False),
        ("macro_indicators",      "indicator_date", "02_macro_indicators",   "WARN",  False),  # ← fixed column
        ("global_cues",           "date",           "03_global_cues",        "WARN",  False),
        # Step labels renumbered 2026-07 when sector_strength moved after
        # compute_indicators and the quality check was split into a pre-signal
        # gate and a post-alert audit. These are display labels only — the
        # check keys off the TABLE — but a stale label sends you looking for a
        # step that no longer exists ("21_quality_check missing" when the step
        # is now 27_quality_audit).
        ("chartink_raw_data",     "date",           "07_fetch_chartink",     "ERROR", False),
        ("stock_data_daily",      "date",           "08_ingest_bhavcopy",    "ERROR", False),
        ("master_shortlist",      "date",           "17_screen_stocks",      "ERROR", False),
        ("fii_dii_flow",          "date",           "09_fii_dii",            "WARN",  False),
        # 08 checked separately below — event_date is the future event's own
        # date (results/AGM/dividend), not an ingestion timestamp, and the
        # table is REPLACE-purged of past events every run. eq(td) was a
        # false-positive generator; use forward-looking existence instead.
        ("safety_lists",          "listed_date",    "11_asm_gsm",            "WARN",  False),
        ("stock_data_daily",      "date",           "12_compute_indicators", "ERROR", False),
        ("market_regime",         "date",           "14_compute_regime",     "WARN",  False),
        ("signal_log",            "date",           "20_signals",            "ERROR", False),
        ("msl_history",           "snapshot_date",           "21_history",            "WARN",  True),   # ← uses today_ist()
        ("ai_context",            "date",           "23_ai_market_intel",    "WARN",  False),
        ("ai_context",            "date",           "24_ai_decision_engine", "WARN",  False),
        ("data_anomalies",        "date",           "27_quality_audit",      "WARN",  True),   # ← self, writes today
    ]

    missing = []

    # 08_nse_events: event_date is a future event date, not an ingestion
    # timestamp, and the table is purged of past events on every run.
    # A healthy run leaves behind rows with event_date >= td.
    try:
        nse_cnt = (
            sb.table("nifty_upcoming_events")
              .select("*", count="exact")
              .gte("event_date", td)
              .limit(1)
              .execute().count or 0
        )
        if nse_cnt == 0:
            missing.append({"step": "08_nse_events", "table": "nifty_upcoming_events", "severity": "WARN"})
    except Exception as e:
        missing.append({"step": "08_nse_events", "table": "nifty_upcoming_events", "severity": "WARN",
                        "reason": str(e)[:80]})

    for table, date_col, label, sev, use_window in steps:
        try:
            cnt = 0
            if date_col is None:
                cnt = sb.table(table).select("*", count="exact").limit(1).execute().count or 0
            elif use_window:
                # Accept rows written with either trade_date or today_ist()
                for d in date_window:
                    cnt = sb.table(table).select("*", count="exact").eq(date_col, d).limit(1).execute().count or 0
                    if cnt > 0:
                        break
            else:
                cnt = sb.table(table).select("*", count="exact").eq(date_col, td).limit(1).execute().count or 0

            if cnt == 0:
                missing.append({"step": label, "table": table, "severity": sev})
        except Exception as e:
            missing.append({"step": label, "table": table, "severity": "WARN",
                            "reason": str(e)[:80]})

    has_error = any(m["severity"] == "ERROR" for m in missing)
    sev = "ERROR" if has_error else ("WARN" if missing else "OK")
    return _result("C07_pipeline_completeness", len(missing) == 0, sev,
        f"{len(missing)}/{len(steps)} steps missing data for {td}"
        + (f": {[m['step'] for m in missing]}" if missing else " ✅"),
        value=str(len(missing)), affected=[m["table"] for m in missing])


# ── C08: __MARKET_INTEL__ Validity ────────────────────────────────────────────

def c08_market_intel_validity(sb, td):
    """
    Step 18 must write __MARKET_INTEL__ to ai_context for trade_date.
    Validates: row exists + conviction_reason is parseable JSON with key fields.
    """
    rows = (
        sb.table("ai_context")
          .select("conviction_reason,suggested_action,ai_note")
          .eq("date", td)
          .eq("symbol", "__MARKET_INTEL__")
          .limit(1)
          .execute().data
    )
    if not rows:
        return _result("C08_market_intel_validity", False, "ERROR",
            f"No __MARKET_INTEL__ in ai_context for {td} — step 18 failed or used wrong date")

    row = rows[0]
    sizing = row.get("suggested_action") or "?"
    try:
        full = json.loads(row.get("conviction_reason") or "{}")
        has_fii      = bool(full.get("fii_outlook"))
        has_tone     = bool(full.get("market_tone"))
        has_sentiment = bool(full.get("candidate_sentiment"))
        fields_ok = has_fii and has_tone
    except Exception:
        fields_ok = False
        has_sentiment = False

    ok  = fields_ok
    msg = (
        f"__MARKET_INTEL__ ✅ sizing={sizing} "
        + ("fii_outlook ✅ " if fields_ok else "⚠️ missing fii_outlook/market_tone ")
        + (f"sentiment={has_sentiment}"  )
    )
    return _result("C08_market_intel_validity", ok, "WARN", msg, value=sizing)


# ── C09: __FINAL_PICKS__ Validity ─────────────────────────────────────────────

def c09_final_picks_validity(sb, td):
    """
    Step 19 must write __FINAL_PICKS__ to ai_context for trade_date.
    Validates: row exists + ranked_candidates present + portfolio_guidance present
    (in strategy_validation — v4 split payload fix).
    Missing portfolio_guidance = the truncation bug is still present in step 19.
    """
    rows = (
        sb.table("ai_context")
          .select("conviction_reason,strategy_validation,ai_note,suggested_action")
          .eq("date", td)
          .eq("symbol", "__FINAL_PICKS__")
          .limit(1)
          .execute().data
    )
    if not rows:
        return _result("C09_final_picks_validity", False, "ERROR",
            f"No __FINAL_PICKS__ in ai_context for {td} — step 19 failed or used wrong date")

    row = rows[0]
    try:
        candidates_data = json.loads(row.get("conviction_reason") or "{}")
        ranked = candidates_data.get("ranked_candidates") or []
    except Exception:
        ranked = []

    guidance_ok = False
    tier1_count = 0
    try:
        if row.get("strategy_validation"):
            gd = json.loads(row["strategy_validation"])
            guidance_ok = bool(gd.get("portfolio_guidance"))
        tier1_count = sum(1 for r in ranked if r.get("tier") == "TIER_1")
    except Exception:
        pass

    issues = []
    if not ranked:        issues.append("ranked_candidates empty")
    if not guidance_ok:   issues.append("portfolio_guidance missing (truncation bug in step 19?)")

    ok  = not issues
    msg = (
        f"__FINAL_PICKS__ {'✅' if ok else '⚠️'} "
        f"candidates:{len(ranked)} TIER_1:{tier1_count} "
        f"guidance:{'✅' if guidance_ok else '❌'}"
        + (f" | Issues: {issues}" if issues else "")
    )
    return _result("C09_final_picks_validity", ok, "WARN", msg,
        value=f"{len(ranked)} ranked, {tier1_count} T1")


# ── C10: Candidate Price Coverage ─────────────────────────────────────────────

def c10_candidate_price_coverage(sb, td):
    """
    CRITICAL: Verify TIER_1 and TIER_2 candidates have non-null prices in MSL.
    Null prices cause the AI to hallucinate entry levels (e.g. WELCORP ₹4050 bug).

    Root cause of that bug: step 19's master_shortlist JOIN found no data because
    the wrong date was resolved (May 6 31-row MSL vs May 5 93-row MSL).
    After the date fix this should always pass. If it fails again, investigate step 15.
    """
    # Get ranked candidates from __FINAL_PICKS__
    rows = (
        sb.table("ai_context")
          .select("conviction_reason,strategy_validation")
          .eq("date", td).eq("symbol", "__FINAL_PICKS__")
          .limit(1).execute().data
    )
    if not rows:
        return _result("C10_candidate_price_coverage", True, "WARN",
            "No __FINAL_PICKS__ — cannot check price coverage (see C09)")

    try:
        data = json.loads(rows[0].get("conviction_reason") or "{}")
        ranked = data.get("ranked_candidates") or []
    except Exception:
        ranked = []

    watchlist = [r["symbol"] for r in ranked
                 if r.get("tier") in ("TIER_1", "TIER_2") and r.get("symbol")]
    if not watchlist:
        return _result("C10_candidate_price_coverage", True, "OK",
            "No TIER_1/TIER_2 candidates to check prices for")

    # Check master_shortlist for these symbols on trade_date
    msl_rows = (
        sb.table("master_shortlist")
          .select("symbol,current_price,entry_zone_low,entry_zone_high")
          .eq("date", td)
          .in_("symbol", watchlist)
          .execute().data
    )
    msl_map = {r["symbol"]: r for r in msl_rows}

    missing_price = []
    missing_zone  = []
    for sym in watchlist:
        m = msl_map.get(sym, {})
        if not m or m.get("current_price") is None:
            missing_price.append(sym)
        if not m or m.get("entry_zone_low") is None:
            missing_zone.append(sym)

    null_price_risk = missing_price  # these could cause AI hallucination
    ok = len(null_price_risk) == 0
    msg = (
        f"{len(watchlist)} TIER_1/2 candidates checked | "
        + (f"✅ all have prices" if ok else
           f"⚠️ {len(null_price_risk)} missing current_price → AI hallucination risk: {null_price_risk}")
        + (f" | {len(missing_zone)} missing entry_zone" if missing_zone else "")
    )
    sev = "ERROR" if len(null_price_risk) >= 2 else "WARN"
    return _result("C10_candidate_price_coverage", ok, sev, msg,
        value=f"{len(watchlist) - len(null_price_risk)}/{len(watchlist)}",
        affected=null_price_risk)


# ── C11: Regime ML vs Manual ─────────────────────────────────────────────────

def c11_regime_ml_vs_manual(sb, td):
    row = (
        sb.table("market_regime")
          .select("regime,predicted_regime,regime_confidence")
          .eq("date", td).limit(1).execute().data
    )
    if not row:
        return _result("C11_regime_ml_vs_manual", True, "WARN",
            f"No market_regime row for {td} — compute_regime may not have run")

    manual    = (row[0].get("regime")           or "").replace("_", " ").upper().strip()
    predicted = (row[0].get("predicted_regime") or "").replace("_", " ").upper().strip()
    conf      = row[0].get("regime_confidence")

    if not predicted:
        return _result("C11_regime_ml_vs_manual", True, "WARN",
            "predicted_regime not set — ml_regime_classifier not yet active")

    mt   = REGIME_TIER.get(manual, -1)
    pt   = REGIME_TIER.get(predicted, -1)
    if mt < 0 or pt < 0:
        return _result("C11_regime_ml_vs_manual", True, "WARN",
            f"Unknown regime label: manual='{manual}' predicted='{predicted}'")

    diff = abs(mt - pt)
    ok   = diff <= 1
    sev  = "ERROR" if diff >= REGIME_ML_TIER_ERROR else "WARN"
    conf_str = f" conf={float(conf):.0%}" if conf else ""
    return _result("C11_regime_ml_vs_manual", ok, sev,
        f"Regime manual={manual} vs ML={predicted}{conf_str} tier_diff={diff}"
        + (" ✅" if ok else " ⚠️ investigate"),
        value=f"diff={diff}")


# ── C12: Positions vs Regime Cap ─────────────────────────────────────────────

def c12_positions_vs_regime_cap(sb, td):
    regime_row = (
        sb.table("market_regime")
          .select("regime").order("date", desc=True).limit(1).execute().data
    )
    regime  = (regime_row[0].get("regime") or "NEUTRAL").replace("_"," ").upper().strip() if regime_row else "NEUTRAL"
    max_pos = REGIME_MAX_POSITIONS.get(regime, 7)
    current = sb.table("open_positions").select("symbol", count="exact").eq("status","ACTIVE").execute().count or 0
    ok      = current <= max_pos
    return _result("C12_positions_vs_regime_cap", ok, "WARN",
        f"Open positions: {current}/{max_pos} for {regime} regime"
        + (" — new entries blocked" if not ok else " ✅"),
        value=f"{current}/{max_pos}")


# ── C13: FII / Regime / Sizing Consistency ───────────────────────────────────

def c13_fii_regime_sizing_consistency(sb, td):
    """
    Check that position_sizing in __FINAL_PICKS__ is compatible with
    the current regime. E.g. FULL sizing in RISK OFF is a mismatch.
    """
    regime_row = (
        sb.table("market_regime")
          .select("regime").eq("date", td).limit(1).execute().data
    )
    if not regime_row:
        regime_row = sb.table("market_regime").select("regime").order("date",desc=True).limit(1).execute().data
    regime = (regime_row[0].get("regime") or "NEUTRAL").replace("_"," ").upper().strip() if regime_row else "NEUTRAL"

    fp_row = (
        sb.table("ai_context")
          .select("suggested_action")
          .eq("date", td).eq("symbol", "__FINAL_PICKS__")
          .limit(1).execute().data
    )
    sizing = (fp_row[0].get("suggested_action") or "").upper() if fp_row else "UNKNOWN"

    fii_row = sb.table("fii_dii_flow").select("fii_flag").order("date",desc=True).limit(1).execute().data
    fii_flag = (fii_row[0].get("fii_flag") or "NEUTRAL").upper() if fii_row else "NEUTRAL"

    ok = (regime, sizing) in SIZING_REGIME_COMPAT or sizing == "UNKNOWN"
    msg = (
        f"Regime={regime} | FII={fii_flag} | Sizing={sizing}"
        + (" ✅ consistent" if ok else f" ⚠️ {sizing} sizing unusual for {regime} regime")
    )
    return _result("C13_fii_regime_sizing_consistency", ok, "WARN", msg,
        value=f"regime={regime},sizing={sizing},fii={fii_flag}")


# ── C14: Signal Enrichment Coverage ──────────────────────────────────────────

def c14_signal_enrichment_coverage(sb, td):
    """
    After step 19 runs, all BUY candidates should have ai_conviction set.
    Low coverage = step 19 failed or ran with wrong date.
    """
    buys = (
        sb.table("signal_log")
          .select("symbol,ai_conviction")
          .eq("date", td)
          .in_("signal_type", list(BUY_SIGNAL_TYPES))
          .execute().data
    )
    if not buys:
        return _result("C14_signal_enrichment_coverage", True, "WARN",
            f"No BUY signals for {td} — step 15 may have used wrong date (check C07)")

    enriched = [r for r in buys if r.get("ai_conviction")]
    pct      = len(enriched) / len(buys) if buys else 0
    ok       = pct >= ENRICH_COVERAGE_WARN
    unenriched = [r["symbol"] for r in buys if not r.get("ai_conviction")]
    return _result("C14_signal_enrichment_coverage", ok, "WARN",
        f"{len(enriched)}/{len(buys)} BUY signals enriched ({pct:.0%})"
        + (f" | Not enriched: {unenriched[:5]}" if unenriched else " ✅"),
        value=f"{pct:.0%}", affected=unenriched[:10])


# ── C15: Lesson Freshness ─────────────────────────────────────────────────────

def c15_lesson_freshness(sb, td):
    """
    Steps 18 and 19 write new lessons each run.
    If no AI lessons in the last 2 days, something is wrong with the AI pipeline.
    """
    cutoff = str((datetime.strptime(td, "%Y-%m-%d") - timedelta(days=LESSON_STALE_DAYS)).date())
    rows = (
        sb.table("lessons")
          .select("date,source")
          .gte("date", cutoff)
          .like("source", "AI:%")
          .execute().data
    )
    ai_lessons = len(rows)
    ok = ai_lessons > 0
    return _result("C15_lesson_freshness", ok, "WARN",
        f"{ai_lessons} AI lessons written in last {LESSON_STALE_DAYS} days"
        + (" ✅" if ok else f" ⚠️ — steps 18/19 may not have run or AI failed"),
        value=str(ai_lessons))


# ── C16: Near-Miss Coverage ───────────────────────────────────────────────────
NEAR_MISS_MIN_COUNT = 3
NEAR_MISS_MIN_COUNT = 3   # at least 3 near-misses should exist on any active day

def c16_near_miss_coverage(sb, td):
    """
    Near-miss data should exist on every active trading day for at least a few
    WATCH signals. We no longer require 50% of ALL WATCH signals to carry it —
    most WATCH signals are routine, not near-misses.
    """
    watch = (
        sb.table("signal_log")
          .select("symbol,near_miss_data,filter_reason")
          .eq("date", td)
          .eq("signal_type", "WATCH")
          .execute().data
    )
    if not watch:
        return _result("C16_near_miss_coverage", True, "OK",
            f"No WATCH signals for {td}")

    with_data    = [r for r in watch if r.get("near_miss_data")]
    without_data = [r["symbol"] for r in watch if not r.get("near_miss_data")]

    # Only fail if zero near-misses exist — not based on % of all WATCH signals
    ok = len(with_data) >= NEAR_MISS_MIN_COUNT
    pct = len(with_data) / len(watch) if watch else 0

    return _result("C16_near_miss_coverage", ok, "WARN",
        f"{len(with_data)}/{len(watch)} WATCH signals have near_miss_data ({pct:.0%}) "
        f"— expected ≥{NEAR_MISS_MIN_COUNT} near-misses minimum"
        + (" ✅" if ok else " ⚠️ step 15 may not be tagging near-misses"),
        value=f"{len(with_data)} near-misses", affected=[] )  # don't list all 71 non-NM stocks


# ── C17: Entry Zone Validity ──────────────────────────────────────────────────

def c17_entry_zone_validity(sb, td):
    """
    For all MSL candidates with non-null zones, verify zone_low < zone_high.
    Inverted zones cause broken R:R calculations in step 15.
    """
    rows = (
        sb.table("master_shortlist")
          .select("symbol,entry_zone_low,entry_zone_high")
          .eq("date", td)
          .not_.is_("entry_zone_low", "null")
          .not_.is_("entry_zone_high", "null")
          .execute().data
    )
    bad = [
        r["symbol"] for r in rows
        if float(r["entry_zone_low"] or 0) >= float(r["entry_zone_high"] or 0)
    ]
    return _result("C17_entry_zone_validity", len(bad) == 0, "WARN",
        f"{len(bad)} MSL rows with inverted entry zone (zone_low ≥ zone_high)"
        + (f": {bad[:5]}" if bad else " ✅"),
        value=str(len(bad)), affected=bad[:10])


# ── C18: Tier Distribution ───────────────────────────────────────────────────

def c18_tier_distribution(sb, td):
    """
    Tier distribution sanity: TIER_1 count should be between 1 and 10.
    0 TIER_1 = step 19 overly conservative or failed.
    >10 TIER_1 = step 19 not applying sector concentration guards properly.
    """
    rows = (
        sb.table("ai_context")
          .select("conviction_reason")
          .eq("date", td).eq("symbol", "__FINAL_PICKS__")
          .limit(1).execute().data
    )
    if not rows:
        return _result("C18_tier_distribution", True, "WARN",
            "No __FINAL_PICKS__ — cannot check tier distribution (see C09)")
    try:
        data   = json.loads(rows[0].get("conviction_reason") or "{}")
        ranked = data.get("ranked_candidates") or []
    except Exception:
        ranked = []

    tier1 = sum(1 for r in ranked if r.get("tier") == "TIER_1")
    tier2 = sum(1 for r in ranked if r.get("tier") == "TIER_2")
    tier3 = sum(1 for r in ranked if r.get("tier") == "TIER_3")

    ok  = TIER1_MIN <= tier1 <= TIER1_MAX if ranked else True
    msg = (
        f"TIER_1:{tier1} TIER_2:{tier2} TIER_3:{tier3} total:{len(ranked)}"
        + (" ✅" if ok else
           f" ⚠️ TIER_1={tier1} outside expected {TIER1_MIN}–{TIER1_MAX}")
    )
    return _result("C18_tier_distribution", ok, "WARN", msg,
        value=f"T1:{tier1} T2:{tier2} T3:{tier3}")


# ── C19: Signal Date Alignment ────────────────────────────────────────────────

def c19_signal_date_alignment(sb, td):
    """
    Verify signal_log was written for the same trade_date as stock_data_daily.
    Mismatch = step 15 used wrong date (the master_shortlist probe bug).
    """
    sig_row = (
        sb.table("signal_log")
          .select("date")
          .order("date", desc=True)
          .limit(1)
          .execute().data
    )
    if not sig_row:
        return _result("C19_signal_date_alignment", True, "WARN",
            "No signal_log rows — step 15 may not have run")

    sig_date = str(sig_row[0].get("date", ""))[:10]
    ok       = sig_date == td
    msg      = (
        f"signal_log latest date: {sig_date} | stock_data_daily date: {td}"
        + (" ✅ aligned" if ok else
           f" ⚠️ MISMATCH — step 15 wrote to {sig_date} instead of {td}. "
           f"Likely master_shortlist probe bug — check resolve_last_trading_day()")
    )
    return _result("C19_signal_date_alignment", ok,
        "ERROR" if not ok else "OK", msg,
        value=f"sig={sig_date} sdd={td}")


# ── C20: Trade Date Integrity ─────────────────────────────────────────────────

def c20_trade_date_integrity(sb, td):
    """
    Validates that the resolved trade_date is correct for the current run time.
    Detects: bhavcopy missing post-6pm, stale date on weekday, future date.

    Scenarios:
      Weekday post-6pm  → td must == today        (FAIL if not)
      Weekday pre-6pm   → td == yesterday          (OK — expected)
      Weekend/Holiday   → td == last trading day   (OK — expected)
    """
    import pytz
    now_ist      = datetime.now(IST)
    calendar_today = str(today_ist())
    bhavcopy_eta = now_ist.replace(hour=18, minute=0, second=0, microsecond=0)
    is_weekday   = now_ist.weekday() < 5
    bhavcopy_written = (td == calendar_today)

    # Check holiday list
    try:
        holiday_rows = sb.table("nse_holidays").select("date").execute().data
        holidays     = {r["date"] for r in holiday_rows}
        is_holiday   = calendar_today in holidays
    except Exception:
        holidays   = set()
        is_holiday = False

    is_trading_day = is_weekday and not is_holiday

    # Determine expected state and status
    if not is_trading_day:
        status = "OK"
        note   = f"Non-trading day (weekend/holiday) — previous trading day {td} expected"

    elif now_ist < bhavcopy_eta:
        status = "OK"
        note   = (
            f"Pre-bhavcopy window ({now_ist.strftime('%H:%M')} IST) — "
            f"previous date {td} expected, today's bhavcopy not yet written"
        )
    else:
        if bhavcopy_written:
            status = "OK"
            note   = f"Post-6pm ✅ bhavcopy written on time for {td}"
        else:
            status = "ERROR"
            note   = (
                f"Post-6pm ❌ bhavcopy NOT written — "
                f"trade_date={td} but calendar={calendar_today}. "
                f"Step 05 (ingest_bhavcopy) likely failed."
            )

    ok = status == "OK"
    log_fn = logger.info if ok else logger.error
    log_fn(
        f"  C20_trade_date | td={td} | calendar={calendar_today} | "
        f"bhavcopy={'✅' if bhavcopy_written else '⚠️'} | "
        f"trading_day={is_trading_day} | run_time={now_ist.strftime('%H:%M')} IST | "
        f"status={status}"
    )

    return _result(
        "C20_trade_date_integrity", ok, status,
        note,
        value=f"td={td} cal={calendar_today} bhavcopy={'yes' if bhavcopy_written else 'no'}"
    )

# ── Telegram Alert ────────────────────────────────────────────────────────────

def _send_error_alert(errors: list, warnings: list):
    """
    Fire Telegram for ERRORs (immediate) and a summary of WARNs.
    Uses send_message from send_alerts (correct function name).
    """
    try:
        from alerts.send_alerts import send_message
    except ImportError:
        logger.warning("Quality: cannot import send_message from send_alerts — skipping Telegram")
        return

    lines = ["🔴 <b>Pipeline Quality — Review Before Open</b>", ""]
    if errors:
        lines.append(f"<b>{len(errors)} ERROR(S):</b>")
        for e in errors:
            lines.append(f"❌ <b>{e['check']}</b>: {e['message'][:120]}")
    if warnings:
        lines.append(f"\n<b>{len(warnings)} WARN(S):</b>")
        for w in warnings[:5]:   # cap to avoid message overflow
            lines.append(f"⚠️ {w['check']}: {w['message'][:100]}")
        if len(warnings) > 5:
            lines.append(f"  … +{len(warnings)-5} more warnings")

    try:
        send_message("\n".join(lines))
    except Exception as ex:
        logger.warning(f"Quality Telegram alert failed: {ex}")


# ── Dedup guard ───────────────────────────────────────────────────────────────

def _already_logged_today(sb, td):
    try:
        cnt = sb.table("data_anomalies").select("check_name", count="exact").eq("date", td).execute().count or 0
        return cnt > 0
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# CHECK PHASES
# ─────────────────────────────────────────────────────────────────────────────
#
# The 20 checks answer two different questions at two different times, and
# running them all at the very end of the pipeline (as step 23, AFTER alerts)
# meant an input-data failure was reported only after you had already been told
# what to buy. Splitting them:
#
#   INPUT  — validates the raw + computed data that signals are about to be
#            derived from. Runs BEFORE generate_signals. An ERROR here means
#            any signal produced would be built on bad data, so the pipeline
#            stops rather than emitting a recommendation.
#
#   OUTPUT — validates what the pipeline produced (signal scores, AI tiering,
#            entry zones, price coverage, alignment). Cannot run before the
#            artefacts exist, so it stays at the end and is advisory.
#
INPUT_CHECKS = {
    "C01": ("chartink row count",     c01_chartink_row_count),
    "C02": ("RSI range",              c02_rsi_range),
    "C03": ("vol_ratio cap",          c03_vol_ratio_cap),
    "C04": ("delivery_pct bounds",    c04_delivery_pct_bounds),
    "C06": ("MSL completeness",       c06_msl_completeness),
    "C20": ("trade date integrity",   c20_trade_date_integrity),
}

OUTPUT_CHECKS = {
    "C05": ("signal score range",     c05_signal_score_range),
    "C07": ("pipeline completeness",  c07_pipeline_completeness),
    "C08": ("market intel validity",  c08_market_intel_validity),
    "C09": ("final picks validity",   c09_final_picks_validity),
    "C10": ("candidate price cover",  c10_candidate_price_coverage),
    "C11": ("regime ML vs manual",    c11_regime_ml_vs_manual),
    "C12": ("positions vs cap",       c12_positions_vs_regime_cap),
    "C13": ("FII/regime sizing",      c13_fii_regime_sizing_consistency),
    "C14": ("signal enrichment",      c14_signal_enrichment_coverage),
    "C15": ("lesson freshness",       c15_lesson_freshness),
    "C16": ("near-miss coverage",     c16_near_miss_coverage),
    "C17": ("entry zone validity",    c17_entry_zone_validity),
    "C18": ("tier distribution",      c18_tier_distribution),
    "C19": ("signal date alignment",  c19_signal_date_alignment),
}


def main(phase: str = "all"):
    """
    phase="input"  → pre-signal gate. Raises on ERROR so run_pipeline halts.
    phase="output" → post-alert audit. Advisory, never raises.
    phase="all"    → every check, advisory (original behaviour, for manual runs).
    """
    if is_kill_switch_active():
        logger.warning("Kill switch active — data_quality_monitor skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb = get_supabase()

    # ── Resolve trade date (same probe as generate_signals — stock_data_daily) ──
    td    = get_trade_date(sb, mode="auto", caller=f"quality_check[{phase}]")
    today = str(today_ist())
    stale = td != today

    if phase == "input":
        selected = INPUT_CHECKS
    elif phase == "output":
        selected = OUTPUT_CHECKS
    else:
        selected = {**INPUT_CHECKS, **OUTPUT_CHECKS}

    logger.info(
        f"data_quality_monitor v5 [{phase}]: {len(selected)} checks | trade_date={td}"
        + (f" (calendar={today} — bhavcopy not yet written)" if stale else " — bhavcopy ✅")
    )

    all_checks = [
        (label, (lambda f=fn: f(sb, td)))
        for label, (_desc, fn) in sorted(selected.items())
    ]

    results = []
    for label, fn in all_checks:
        try:
            r = fn()
            results.append(r)
            icon = {"OK": "✅", "WARN": "⚠️", "ERROR": "🔴"}.get(r["severity"], "?")
            logger.info(f"  {icon} {r['check']}: {r['message'][:120]}")
        except Exception as e:
            logger.warning(f"  ⚠️ {label} threw exception: {e}")
            results.append(_result(f"{label}_exception", False, "WARN",
                f"Check threw exception: {str(e)[:100]}"))

    ok_n   = sum(1 for r in results if r["severity"] == "OK")
    warn_n = sum(1 for r in results if r["severity"] == "WARN")
    err_n  = sum(1 for r in results if r["severity"] == "ERROR")

    # Write to data_anomalies (skip if already logged for this trade_date)
    anomalies = [r for r in results if r["severity"] in ("WARN", "ERROR")]
    if anomalies and not _already_logged_today(sb, td):
        try:
            sb.table("data_anomalies").insert([{
                "date":       td,
                "check_name": a["check"],
                "severity":   a["severity"],
                "value":      a.get("value", ""),
                "message":    a["message"],
                "affected":   str(a.get("affected", [])),
                "created_at": datetime.now(IST).isoformat(),
            } for a in anomalies]).execute()
        except Exception as e:
            logger.warning(f"data_anomalies insert failed: {e}")

    # Telegram: fire on any ERROR; include WARN summary
    errors   = [r for r in results if r["severity"] == "ERROR"]
    warnings = [r for r in results if r["severity"] == "WARN"]
    if errors:
        _send_error_alert(errors, warnings)

    logger.success(
        f"Quality v5 [{phase}]: ✅{ok_n} OK | ⚠️{warn_n} WARN | 🔴{err_n} ERROR | "
        f"{len(selected)} checks | trade_date={td}"
    )

    summary = {
        "date":    td,
        "phase":   phase,
        "ok":      ok_n,
        "warn":    warn_n,
        "error":   err_n,
        "results": results,
    }

    # ── Input phase is a GATE, not a report ───────────────────────────────────
    # Everything downstream (screener, MSL, signals, AI tiering, alerts) derives
    # from the data these checks validate. Emitting a buy recommendation off
    # data we have just proven bad is strictly worse than emitting nothing, so
    # raise and let run_pipeline halt. Override with quality_gate_enabled=false
    # in system_config if a check ever becomes noisy enough to block a good run.
    if phase == "input" and err_n > 0:
        from config import cfg_bool
        failed = ", ".join(f"{r['check']}({r['message'][:60]})" for r in errors)
        if cfg_bool("quality_gate_enabled", True):
            raise RuntimeError(
                f"Input data quality gate FAILED for {td}: {err_n} ERROR check(s) — {failed}. "
                f"Pipeline halted before signal generation. "
                f"Set quality_gate_enabled=false in system_config to downgrade to a warning."
            )
        logger.error(f"Input quality gate would have blocked ({failed}) — disabled by config, continuing")

    return summary


def main_input():
    """Pre-signal gate. Wired as a fatal step in run_pipeline."""
    return main(phase="input")


def main_output():
    """Post-alert audit. Wired as a non-fatal step in run_pipeline."""
    return main(phase="output")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v6 — data quality monitor")
    ap.add_argument("--phase", choices=["input", "output", "all"], default="all")
    args = ap.parse_args()
    result = main(phase=args.phase)
    icons = {"OK": "✅", "WARN": "⚠️", "ERROR": "🔴"}
    for r in result.get("results", []):
        print(f"{icons.get(r['severity'], '?')} {r['check']}: {r['message']}")