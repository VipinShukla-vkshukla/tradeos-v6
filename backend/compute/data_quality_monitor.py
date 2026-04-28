"""
TradeOS v6 — Phase 2: Data Quality Monitor (EVOLVED v3)
========================================================
Validates every pipeline output at the end of each evening run.
Wire: Step 18 in run_pipeline.py — always last, always non-fatal.

This script does NOT block the pipeline. It observes, auto-corrects where safe,
logs anomalies to data_anomalies, and fires a Telegram condensed alert when
something needs human attention before the next session opens.

REGIME MODEL (5-STATE — aligned with compute_regime v2.0):
  TRENDING   → All cylinders firing. Strong bull market.   Tier 0
  RISK ON    → Positive with some mixed signals.           Tier 1
  NEUTRAL    → Neither bull nor bear. Selective.           Tier 2
  RECOVERING → Bouncing from bear — not yet neutral.       Tier 3
  RISK OFF   → Bear conditions. Capital preservation.      Tier 4

PIPELINE ORDER VALIDATED (Phase 2):
  01 market_news → 02 macro_indicators → 03 global_cues → 04 fetch_chartink
  05 ingest_bhavcopy → 06 ingest_sheets → 07 compute_indicators → 08 signals
  09 history → 10 fii_dii → 11 nse_events → 12 asm_gsm → 13 post_trade
  14 generate_shortlist → 15 market_intel → 16 ai_enrich → 17 alerts → 18 quality_check

Check catalogue (17 checks):
  ── Data Integrity ──
  C01  chartink_row_count        — Nifty 500 row count within expected band
  C02  rsi_range                 — RSI values in 0-100 for all stocks
  C03  vol_ratio_cap             — Auto-cap outliers at 50x (safe auto-correct)
  C04  delivery_pct_bounds       — Delivery % in 0-100 for all stocks
  C05  signal_score_range        — signal_log scores in 0-120
  C06  msl_score_jumps           — Score changes >20 pts vs yesterday flagged

  ── Pipeline Completeness ──
  C07  pipeline_completeness     — All 18 steps wrote data today (new order)
  C08  ai_context_completeness   — ai_enrich ran with G6/G13/G17/G18 + market_intel keys

  ── Regime & Positions ──
  C09  regime_ml_vs_manual       — ML predicted_regime vs manual regime diff
  C10  positions_vs_regime_cap   — Open position count within regime max limit

  ── AI Intelligence Chain ──
  C11  shortlist_validity        — __SHORTLIST__ in ai_context has ranked candidates
                                   with correct last_trading_day date
  C12  market_intel_validity     — __MARKET_INTEL__ has all 5 JSON keys, valid sizing
                                   enum, top_3 is populated array
  C13  date_alignment            — lessons/signal_log/ai_context all written to
                                   last_trading_day, not today (date bug check)
  C14  ai_enrich_intel_consumed  — ai_enrich injected market_intel key into ai_context
                                   (confirms new context injection is flowing)
  C15  fii_regime_consistency    — FII flag and market sizing must not contradict
                                   regime (e.g. RISK OFF + FULL sizing = ERROR)

  ── Lesson Quality ──
  C16  lesson_quality            — AI:market_intel lessons have valid corrective_rule
                                   starting with "Rule:" prefix

  ── Signal Coverage ──
  C17  signal_enrich_coverage    — Ratio of signal_log rows enriched by ai_enrich
                                   vs total signals (< 50% = WARN, < 25% = ERROR)

Severity levels:
  OK    — check passed cleanly
  WARN  — anomaly detected, pipeline continues, logged
  ERROR — requires human review before next session, Telegram alert fired

Tables read:
  market_news, global_cues, chartink_raw_data, stock_data_daily,
  master_shortlist, signal_log, msl_history, fii_dii_flow, event_calendar,
  ai_context, market_regime, open_positions, lessons, data_anomalies

Tables written:
  data_anomalies   — one row per check that is WARN or ERROR
  stock_data_daily — auto-corrects vol_ratio outliers (C03 only)
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import (
    get_supabase, today_ist, IST,
    is_kill_switch_active, logger,
)

# ── Thresholds ────────────────────────────────────────────────────────────────

CHARTINK_ROW_MIN    = 450
CHARTINK_ROW_MAX    = 510
VOL_RATIO_CAP       = 50.0
SCORE_MIN           = 0
SCORE_MAX           = 120
MSL_JUMP_WARN       = 20
DELIVERY_MIN        = 0.0
DELIVERY_MAX        = 100.0
ENRICH_WARN_RATIO   = 0.50   # below 50% enrichment → WARN
ENRICH_ERROR_RATIO  = 0.25   # below 25% enrichment → ERROR

VALID_SIZING = {"FULL", "REDUCED_25PCT", "HALF", "MINIMAL"}

# G6/G13/G17/G18 patch fields + new market_intel key
AI_CONTEXT_FIELDS = [
    "market_regime",    # G17
    "global_cues",      # G17
    "portfolio",        # G18
    "upcoming_events",  # G6
    "sector_context",   # G13
    "market_intel",     # NEW — market_intel engine context injection
]

REGIME_MAX_POSITIONS = {
    "TRENDING":   8,   # full risk — all cylinders firing
    "RISK ON":    7,   # positive trend, slightly reduced
    "NEUTRAL":    6,   # selective positioning
    "RECOVERING": 4,   # confirmed bounce but not yet neutral — still cautious
    "RISK OFF":   3,   # capital preservation
}

# Tier ordering: lower = more bullish. Used for ML divergence + sizing checks.
REGIME_TIER = {
    "TRENDING":   0,
    "RISK ON":    1,
    "NEUTRAL":    2,
    "RECOVERING": 3,
    "RISK OFF":   4,
}
REGIME_ML_TIER_ERROR = 2   # tier gap ≥ 2 = ERROR (e.g. TRENDING vs NEUTRAL)

# Sizing → minimum regime tier where that sizing is appropriate.
# Contradiction = regime_tier > (sizing_min_tier + 1), i.e. taking more risk than regime allows.
SIZING_MIN_REGIME_TIER = {
    "FULL":          0,   # only appropriate in TRENDING
    "REDUCED_25PCT": 1,   # TRENDING or RISK ON
    "HALF":          2,   # NEUTRAL or better
    "MINIMAL":       3,   # RECOVERING or worse
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _result(check_name, ok, severity_if_fail, message, value="", affected=None):
    return {
        "check":    check_name,
        "ok":       ok,
        "severity": "OK" if ok else severity_if_fail,
        "message":  message,
        "value":    value,
        "affected": affected or [],
    }


def get_last_trading_day(sb) -> str:
    """Mirror of market_intelligence_engine.get_last_trading_day — avoids import."""
    try:
        holiday_rows = sb.table("nse_holidays").select("date").execute().data
        holidays = {r["date"][:10] for r in holiday_rows}
    except Exception:
        holidays = set()

    candidate = today_ist()
    for _ in range(10):
        candidate -= timedelta(days=1)
        if candidate.weekday() >= 5:
            continue
        if str(candidate) in holidays:
            continue
        return str(candidate)
    return str(today_ist() - timedelta(days=1))


# ── C01: Chartink Row Count ───────────────────────────────────────────────────

def c01_chartink_row_count(sb, today):
    cnt = sb.table("chartink_raw_data").select("id", count="exact").eq("date", today).execute().count or 0
    ok  = CHARTINK_ROW_MIN <= cnt <= CHARTINK_ROW_MAX
    sev = "ERROR" if cnt == 0 else "WARN"
    return _result("C01_chartink_row_count", ok, sev,
        f"chartink_raw_data: {cnt} rows (expected {CHARTINK_ROW_MIN}–{CHARTINK_ROW_MAX})",
        value=str(cnt))


# ── C02: RSI Range ────────────────────────────────────────────────────────────

def c02_rsi_range(sb, today):
    rows = sb.table("stock_data_daily").select("symbol,rsi_daily").eq("date", today).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("rsi_daily") is not None and not (0 <= float(r["rsi_daily"]) <= 100)]
    sev  = "ERROR" if len(bad) > 10 else "WARN"
    return _result("C02_rsi_range", len(bad) == 0, sev,
        f"{len(bad)} stocks with RSI outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C03: vol_ratio Cap (auto-correct) ────────────────────────────────────────

def c03_vol_ratio_cap(sb, today):
    outliers  = sb.table("stock_data_daily").select("symbol,vol_ratio").eq("date", today) \
                  .gt("vol_ratio", VOL_RATIO_CAP).execute().data
    corrected = []
    for r in outliers:
        try:
            sb.table("stock_data_daily").update({"vol_ratio": VOL_RATIO_CAP}) \
                .eq("date", today).eq("symbol", r["symbol"]).execute()
            corrected.append(r["symbol"])
        except Exception as e:
            logger.warning(f"vol_ratio cap write failed for {r['symbol']}: {e}")

    msg = (f"{len(corrected)} vol_ratio outliers auto-capped at {VOL_RATIO_CAP}x"
           if corrected else "All vol_ratio values within cap")
    return _result("C03_vol_ratio_cap", True, "WARN", msg,
                   value=str(len(corrected)), affected=corrected)


# ── C04: Delivery % Bounds ────────────────────────────────────────────────────

def c04_delivery_pct_bounds(sb, today):
    rows = sb.table("stock_data_daily").select("symbol,delivery_pct").eq("date", today).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("delivery_pct") is not None
            and not (DELIVERY_MIN <= float(r["delivery_pct"]) <= DELIVERY_MAX)]
    return _result("C04_delivery_pct_bounds", len(bad) == 0, "WARN",
        f"{len(bad)} stocks with delivery_pct outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C05: Signal Score Range ───────────────────────────────────────────────────

def c05_signal_score_range(sb, today):
    sigs = sb.table("signal_log").select("symbol,score").eq("date", today).execute().data
    bad  = [r["symbol"] for r in sigs
            if r.get("score") is not None
            and not (SCORE_MIN <= float(r["score"]) <= SCORE_MAX)]
    return _result("C05_signal_score_range", len(bad) == 0, "WARN",
        f"{len(bad)} signals with score outside {SCORE_MIN}–{SCORE_MAX}" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C06: MSL Score Jumps ──────────────────────────────────────────────────────

def c06_msl_score_jumps(sb, today):
    yesterday = str(today_ist() - timedelta(days=1))
    now_map   = {r["symbol"]: float(r.get("score") or 0)
                 for r in sb.table("master_shortlist").select("symbol,score").execute().data}
    yest_map  = {r["symbol"]: float(r.get("score") or 0)
                 for r in sb.table("msl_history").select("symbol,score").eq("date", yesterday).execute().data}
    jumps = sorted(
        [{"symbol": s, "delta": round(now_map[s] - yest_map[s], 1)}
         for s in now_map if s in yest_map and abs(now_map[s] - yest_map[s]) > MSL_JUMP_WARN],
        key=lambda j: abs(j["delta"]), reverse=True
    )
    return _result("C06_msl_score_jumps", len(jumps) == 0, "WARN",
        f"{len(jumps)} stocks with score change >{MSL_JUMP_WARN}pts vs yesterday"
        + (f": {[(j['symbol'], j['delta']) for j in jumps[:3]]}" if jumps else ""),
        value=str(len(jumps)), affected=[j["symbol"] for j in jumps[:10]])


# ── C07: Pipeline Completeness (updated to new 18-step order) ────────────────

def c07_pipeline_completeness(sb, today, last_trading_day):
    """
    Validates all 18 pipeline steps wrote expected data.
    Steps 14-16 use ai_context sentinel rows — not date-filtered tables.
    """
    # (label, check_type, table_or_symbol, filter_by_today, severity)
    # check_type: "table" = row count, "ai_sentinel" = ai_context symbol lookup
    steps = [
        ("01_market_news",      "table",       "market_news",          True,  "WARN"),
        ("02_macro_indicators", "table",       "macro_indicators",     True,  "WARN"),
        ("03_global_cues",      "table",       "global_cues",          True,  "WARN"),
        ("04_fetch_chartink",   "table",       "chartink_raw_data",    True,  "ERROR"),
        ("05_ingest_bhavcopy",  "table",       "stock_data_daily",     True,  "WARN"),
        ("06_ingest_sheets",    "table",       "master_shortlist",     False, "ERROR"),
        ("07_compute_indicators","table",      "stock_data_daily",     True,  "WARN"),
        ("08_signals",          "table",       "signal_log",           True,  "ERROR"),
        ("09_history",          "table",       "msl_history",          True,  "WARN"),
        ("10_fii_dii",          "table",       "fii_dii_flow",         True,  "WARN"),
        ("11_nse_events",       "table",       "event_calendar",       False, "WARN"),
        ("12_asm_gsm",          "table",       "asm_gsm_data",         False, "WARN"),
        ("13_post_trade",       "table",       "trade_log",            False, "WARN"),
        ("14_generate_shortlist","ai_sentinel","__SHORTLIST__",        False, "ERROR"),
        ("15_market_intel",     "ai_sentinel", "__MARKET_INTEL__",     False, "ERROR"),
        ("16_ai_enrich",        "ai_enrich",   None,                   False, "WARN"),
        ("17_alerts",           "table",       "alert_log",            True,  "WARN"),
    ]

    missing = []
    for label, check_type, target, date_filter, sev in steps:
        try:
            if check_type == "table":
                q   = sb.table(target).select("id", count="exact")
                cnt = (q.eq("date", today) if date_filter else q).execute().count or 0
                if cnt == 0:
                    missing.append({"step": label, "target": target, "severity": sev})

            elif check_type == "ai_sentinel":
                # Check ai_context has the sentinel row for last_trading_day
                rows = sb.table("ai_context").select("symbol") \
                         .eq("symbol", target) \
                         .eq("date", last_trading_day) \
                         .limit(1).execute().data
                if not rows:
                    missing.append({"step": label, "target": f"ai_context[{target}]", "severity": sev})

            elif check_type == "ai_enrich":
                # Check at least 1 non-sentinel ai_context row exists for last_trading_day
                rows = sb.table("ai_context").select("symbol", count="exact") \
                         .eq("date", last_trading_day) \
                         .not_.in_("symbol", ["__SHORTLIST__", "__MARKET_INTEL__"]) \
                         .execute().count or 0
                if rows == 0:
                    missing.append({"step": label, "target": "ai_context (stock rows)", "severity": sev})

        except Exception as e:
            missing.append({"step": label, "target": str(target), "severity": "WARN", "reason": str(e)})

    has_error = any(m["severity"] == "ERROR" for m in missing)
    sev = "ERROR" if has_error else "WARN"
    return _result("C07_pipeline_completeness", len(missing) == 0, sev,
        f"{len(missing)}/17 steps missing data"
        + (f": {[m['step'] for m in missing]}" if missing else " — all steps complete"),
        value=str(len(missing)), affected=[m["target"] for m in missing])


# ── C08: AI Context Completeness (updated — includes market_intel key) ────────

def c08_ai_context_completeness(sb, today, last_trading_day):
    """
    Samples 5 stock-level ai_context rows and checks all patch fields are present,
    including the new market_intel key injected after C14 fix.
    """
    rows = sb.table("ai_context").select("symbol,context_json") \
             .eq("date", last_trading_day) \
             .not_.in_("symbol", ["__SHORTLIST__", "__MARKET_INTEL__"]) \
             .limit(5).execute().data

    if not rows:
        return _result("C08_ai_context_completeness", False, "WARN",
            f"No stock ai_context rows for {last_trading_day} — ai_enrich may not have run")

    missing_by_symbol = {}
    for row in rows:
        ctx = row.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        absent = [f for f in AI_CONTEXT_FIELDS if f not in ctx or ctx[f] is None]
        if absent:
            missing_by_symbol[row["symbol"]] = absent

    ok  = len(missing_by_symbol) == 0
    msg = ("AI context complete — all G6/G13/G17/G18 + market_intel keys present"
           if ok else
           f"{len(missing_by_symbol)} symbols missing context fields: "
           + str({s: f for s, f in list(missing_by_symbol.items())[:3]}))
    return _result("C08_ai_context_completeness", ok, "WARN", msg,
        value=str(len(missing_by_symbol)), affected=list(missing_by_symbol.keys()))


# ── C09: Regime ML vs Manual ──────────────────────────────────────────────────

def c09_regime_ml_vs_manual(sb, today):
    row = sb.table("market_regime").select("regime,predicted_regime,regime_confidence") \
            .eq("date", today).limit(1).execute().data

    if not row:
        return _result("C09_regime_ml_vs_manual", True, "WARN",
            "No market_regime row today — ingest_sheets may not have run")

    manual    = (row[0].get("regime")           or "").replace("_", " ").upper().strip()
    predicted = (row[0].get("predicted_regime") or "").replace("_", " ").upper().strip()
    conf      = row[0].get("regime_confidence")

    if not predicted:
        return _result("C09_regime_ml_vs_manual", True, "WARN",
            "predicted_regime not set — ml_regime_classifier not yet deployed")

    mt = REGIME_TIER.get(manual, -1)
    pt = REGIME_TIER.get(predicted, -1)
    if mt < 0 or pt < 0:
        # Flag unknown labels — could be a stale 3-state label (CAUTION) still in DB
        unknown = []
        if mt < 0: unknown.append(f"manual='{manual}'")
        if pt < 0: unknown.append(f"predicted='{predicted}'")
        return _result("C09_regime_ml_vs_manual", False, "WARN",
            f"Unknown regime label(s): {', '.join(unknown)} — "
            f"expected one of: TRENDING/RISK ON/NEUTRAL/RECOVERING/RISK OFF")

    diff     = abs(mt - pt)
    ok       = diff <= 1
    sev      = "ERROR" if diff >= REGIME_ML_TIER_ERROR else "WARN"
    conf_str = f" conf={float(conf):.0%}" if conf else ""
    return _result("C09_regime_ml_vs_manual", ok, sev,
        f"Regime manual={manual} vs ML={predicted}{conf_str} tier_diff={diff}"
        + (" ✅" if ok else " — investigate disagreement"),
        value=f"diff={diff}")


# ── C10: Open Positions vs Regime Cap ────────────────────────────────────────

def c10_positions_vs_regime_cap(sb, today):
    regime_row = sb.table("market_regime").select("regime").order("date", desc=True).limit(1).execute().data
    regime     = (regime_row[0].get("regime") or "NEUTRAL").replace("_", " ").upper().strip() if regime_row else "NEUTRAL"
    max_pos    = REGIME_MAX_POSITIONS.get(regime, 6)
    current    = sb.table("open_positions").select("symbol", count="exact").execute().count or 0

    ok = current <= max_pos
    return _result("C10_positions_vs_regime_cap", ok, "WARN",
        f"Open positions: {current}/{max_pos} for {regime} regime"
        + (" — new entries will be blocked by risk_manager" if not ok else ""),
        value=f"{current}/{max_pos}")


# ── C11: __SHORTLIST__ Validity ───────────────────────────────────────────────

def c11_shortlist_validity(sb, last_trading_day):
    """
    Confirms generate_shortlist wrote a valid __SHORTLIST__ sentinel:
    - Row exists for last_trading_day
    - ai_note parses as JSON array
    - At least 3 candidates with symbol + rank fields
    - All candidates have last_trading_day date
    """
    rows = sb.table("ai_context").select("ai_note,date") \
             .eq("symbol", "__SHORTLIST__") \
             .eq("date", last_trading_day) \
             .limit(1).execute().data

    if not rows:
        return _result("C11_shortlist_validity", False, "ERROR",
            f"No __SHORTLIST__ in ai_context for {last_trading_day} — generate_shortlist failed or wrote wrong date")

    try:
        shortlist = json.loads(rows[0].get("ai_note") or "[]")
    except Exception:
        return _result("C11_shortlist_validity", False, "ERROR",
            "__SHORTLIST__ ai_note is not valid JSON")

    if not isinstance(shortlist, list) or len(shortlist) < 3:
        return _result("C11_shortlist_validity", False, "WARN",
            f"__SHORTLIST__ has only {len(shortlist)} candidates (expected ≥3)",
            value=str(len(shortlist)))

    # Check required fields
    malformed = [x.get("symbol", "?") for x in shortlist if not x.get("symbol") or x.get("rank") is None]
    symbols   = [x.get("symbol") for x in shortlist if x.get("symbol")]

    ok = len(malformed) == 0
    return _result("C11_shortlist_validity", ok, "WARN",
        f"__SHORTLIST__ valid: {len(shortlist)} candidates, symbols={symbols[:5]}"
        if ok else
        f"__SHORTLIST__ has {len(malformed)} malformed entries missing symbol/rank: {malformed[:3]}",
        value=str(len(shortlist)), affected=malformed)


# ── C12: __MARKET_INTEL__ Structural Validity ─────────────────────────────────

def c12_market_intel_validity(sb, last_trading_day):
    """
    Confirms market_intel engine wrote a valid __MARKET_INTEL__ sentinel:
    - Row exists for last_trading_day
    - conviction_reason parses as JSON with all 5 required keys
    - position_sizing_guidance is a valid enum
    - top_3_candidates is a non-empty array
    - fii_outlook.5session_bias is present
    """
    REQUIRED_KEYS = [
        "market_tone", "macro_sector_impacts", "regulatory_alerts",
        "fii_outlook", "top_3_candidates",
    ]

    rows = sb.table("ai_context").select("conviction_reason,suggested_action,ai_note") \
             .eq("symbol", "__MARKET_INTEL__") \
             .eq("date", last_trading_day) \
             .limit(1).execute().data

    if not rows:
        return _result("C12_market_intel_validity", False, "ERROR",
            f"No __MARKET_INTEL__ in ai_context for {last_trading_day} — market_intel engine failed or wrong date")

    r = rows[0]
    sizing = (r.get("suggested_action") or "").upper().strip()

    try:
        full = json.loads(r.get("conviction_reason") or "{}")
    except Exception:
        return _result("C12_market_intel_validity", False, "ERROR",
            "__MARKET_INTEL__ conviction_reason is not valid JSON")

    issues = []

    # Check all 5 required keys
    missing_keys = [k for k in REQUIRED_KEYS if k not in full]
    if missing_keys:
        issues.append(f"missing keys: {missing_keys}")

    # Check sizing enum
    if sizing not in VALID_SIZING:
        issues.append(f"invalid sizing='{sizing}' (expected one of {VALID_SIZING})")

    # Check top_3_candidates populated
    top3 = full.get("top_3_candidates") or []
    if not isinstance(top3, list) or len(top3) == 0:
        issues.append("top_3_candidates is empty")

    # Check fii_outlook has 5session_bias
    fii_outlook = full.get("fii_outlook") or {}
    if not fii_outlook.get("5session_bias"):
        issues.append("fii_outlook missing 5session_bias")

    ok  = len(issues) == 0
    top_symbols = [c.get("symbol") for c in top3 if c.get("symbol")]
    return _result("C12_market_intel_validity", ok, "ERROR" if missing_keys else "WARN",
        f"__MARKET_INTEL__ valid: sizing={sizing}, top_picks={top_symbols}"
        if ok else
        f"__MARKET_INTEL__ issues — {'; '.join(issues)}",
        value=sizing, affected=issues)


# ── C13: Date Alignment ───────────────────────────────────────────────────────

def c13_date_alignment(sb, today, last_trading_day):
    """
    Catches the date bug: lessons/signal_log/ai_context written to today
    instead of last_trading_day.

    Expected: AI-generated rows use last_trading_day.
    Bug: rows written with today_str (e.g. 2026-03-30 instead of 2026-03-27).
    """
    if today == last_trading_day:
        return _result("C13_date_alignment", True, "WARN",
            "today == last_trading_day — date alignment check not applicable (trading day)")

    issues = []

    # Check lessons — source=AI:market_intel should be on last_trading_day
    wrong_lessons = sb.table("lessons").select("id", count="exact") \
                      .eq("source", "AI:market_intel") \
                      .eq("date", today).execute().count or 0
    if wrong_lessons > 0:
        issues.append(f"{wrong_lessons} AI:market_intel lessons written to {today} (should be {last_trading_day})")

    # Check signal_log MARKET_TOP_PICK on today (should be last_trading_day)
    wrong_signals = sb.table("signal_log").select("id", count="exact") \
                      .eq("signal_type", "MARKET_TOP_PICK") \
                      .eq("date", today).execute().count or 0
    if wrong_signals > 0:
        issues.append(f"{wrong_signals} MARKET_TOP_PICK signals written to {today} (should be {last_trading_day})")

    # Check ai_context __MARKET_INTEL__ on today instead of last_trading_day
    wrong_intel = sb.table("ai_context").select("id", count="exact") \
                    .eq("symbol", "__MARKET_INTEL__") \
                    .eq("date", today).execute().count or 0
    if wrong_intel > 0:
        issues.append(f"__MARKET_INTEL__ written to {today} (should be {last_trading_day})")

    ok = len(issues) == 0
    return _result("C13_date_alignment", ok, "ERROR",
        "All AI rows aligned to last_trading_day" if ok else
        f"Date misalignment detected — {'; '.join(issues)}",
        value=last_trading_day, affected=issues)


# ── C14: ai_enrich Market Intel Consumed ─────────────────────────────────────

def c14_ai_enrich_intel_consumed(sb, last_trading_day):
    """
    Confirms the market_intel context injection is flowing through to ai_enrich.
    Samples 3 stock-level ai_context rows and checks 'market_intel' key exists
    in context_json — this key is only present if the new get_market_intel_context()
    function was called and __MARKET_INTEL__ was successfully read.
    """
    rows = sb.table("ai_context").select("symbol,context_json") \
             .eq("date", last_trading_day) \
             .not_.in_("symbol", ["__SHORTLIST__", "__MARKET_INTEL__"]) \
             .limit(3).execute().data

    if not rows:
        return _result("C14_ai_enrich_intel_consumed", False, "WARN",
            f"No stock ai_context rows for {last_trading_day} — cannot verify market_intel injection")

    injected     = []
    not_injected = []

    for row in rows:
        ctx = row.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}

        sym = row.get("symbol", "?")
        if "market_intel" in ctx and ctx["market_intel"]:
            injected.append(sym)
        else:
            not_injected.append(sym)

    ok = len(not_injected) == 0
    return _result("C14_ai_enrich_intel_consumed", ok, "WARN",
        f"market_intel key present in all sampled ai_context rows: {injected}"
        if ok else
        f"market_intel key MISSING in {len(not_injected)} sampled rows: {not_injected} "
        f"— ai_enrich not consuming __MARKET_INTEL__ (check pipeline order + get_market_intel_context)",
        value=f"{len(injected)}/{len(rows)}", affected=not_injected)


# ── C15: FII / Regime / Sizing Consistency ────────────────────────────────────

def c15_fii_regime_consistency(sb, today, last_trading_day):
    """
    Cross-checks three signals that must not contradict each other:
      market_regime.regime
      __MARKET_INTEL__.suggested_action (position sizing)
      fii_dii_flow.fii_flag

    Contradiction examples:
      RISK OFF regime + FULL sizing       → ERROR (taking full risk in crisis)
      TRENDING regime + MINIMAL sizing    → WARN  (overly conservative)
      FII SELL flag + FULL sizing         → WARN  (ignoring institutional flow)
    """
    issues   = []
    severity = "OK"

    # Regime
    regime_row = sb.table("market_regime").select("regime") \
                   .order("date", desc=True).limit(1).execute().data
    regime = (regime_row[0].get("regime") or "NEUTRAL").replace("_", " ").upper().strip() \
             if regime_row else "NEUTRAL"
    regime_tier = REGIME_TIER.get(regime, 1)

    # Sizing from __MARKET_INTEL__
    intel_row = sb.table("ai_context").select("suggested_action") \
                  .eq("symbol", "__MARKET_INTEL__") \
                  .eq("date", last_trading_day).limit(1).execute().data
    sizing = (intel_row[0].get("suggested_action") or "").upper().strip() if intel_row else ""

    # FII flag
    fii_row = sb.table("fii_dii_flow").select("fii_flag") \
                .order("date", desc=True).limit(1).execute().data
    fii_flag = (fii_row[0].get("fii_flag") or "NEUTRAL").upper().strip() if fii_row else "NEUTRAL"

    # Check 1: sizing vs regime
    if sizing and regime:
        min_tier_for_sizing = SIZING_MIN_REGIME_TIER.get(sizing, 0)
        if regime_tier > min_tier_for_sizing + 1:
            # e.g. regime=RISK OFF (tier 3), sizing=FULL (min_tier=0) → gap of 3
            issues.append(f"sizing={sizing} too aggressive for regime={regime}")
            severity = "ERROR"
        elif regime_tier < min_tier_for_sizing - 1:
            # e.g. regime=TRENDING (tier 0), sizing=MINIMAL (min_tier=2) → too conservative
            issues.append(f"sizing={sizing} too conservative for regime={regime}")
            severity = max(severity, "WARN") if severity != "ERROR" else "ERROR"

    # Check 2: FII selling + aggressive sizing
    if fii_flag == "SELL" and sizing in ("FULL", "REDUCED_25PCT"):
        issues.append(f"FII flag=SELL but sizing={sizing} — ignoring institutional flow")
        if severity != "ERROR":
            severity = "WARN"

    # Check 3: FII buying + overly defensive sizing in a bullish regime
    if fii_flag == "BUY" and sizing == "MINIMAL" and regime in ("TRENDING", "RISK ON"):
        issues.append(f"FII flag=BUY + regime={regime} but sizing=MINIMAL — overly defensive")
        if severity != "ERROR":
            severity = "WARN"

    # Check 4: RECOVERING regime — warn if sizing is FULL (too aggressive for a bounce)
    if regime == "RECOVERING" and sizing == "FULL":
        issues.append(f"regime=RECOVERING but sizing=FULL — bounce not yet confirmed as trend")
        if severity != "ERROR":
            severity = "WARN"

    ok = len(issues) == 0
    return _result("C15_fii_regime_consistency", ok, severity,
        f"FII/regime/sizing consistent: regime={regime}, sizing={sizing}, fii={fii_flag}"
        if ok else
        f"Consistency issues — {'; '.join(issues)} [regime={regime}, sizing={sizing}, fii={fii_flag}]",
        value=f"{regime}|{sizing}|{fii_flag}", affected=issues)


# ── C16: Lesson Quality ───────────────────────────────────────────────────────

def c16_lesson_quality(sb, last_trading_day):
    """
    Checks AI:market_intel lessons written for last_trading_day:
    - corrective_rule must start with "Rule:"
    - scenario_type must be non-empty
    - is_active must be True
    Malformed lessons silently fail to propagate — this catches them early.
    """
    rows = sb.table("lessons").select("corrective_rule,scenario_type,is_active") \
             .eq("source", "AI:market_intel") \
             .eq("date", last_trading_day).execute().data

    if not rows:
        return _result("C16_lesson_quality", False, "WARN",
            f"No AI:market_intel lessons for {last_trading_day} — market_intel engine wrote 0 lessons")

    malformed = []
    for r in rows:
        rule = r.get("corrective_rule") or ""
        stype = r.get("scenario_type") or ""
        active = r.get("is_active")
        if not rule.startswith("Rule:"):
            malformed.append(f"missing 'Rule:' prefix: '{rule[:60]}'")
        if not stype:
            malformed.append("empty scenario_type")
        if not active:
            malformed.append(f"is_active=False on new lesson")

    ok = len(malformed) == 0
    return _result("C16_lesson_quality", ok, "WARN",
        f"All {len(rows)} AI:market_intel lessons well-formed"
        if ok else
        f"{len(malformed)} lesson quality issues: {malformed[:3]}",
        value=str(len(rows)), affected=malformed[:5])


# ── C17: Signal Enrichment Coverage ──────────────────────────────────────────

def c17_signal_enrich_coverage(sb, last_trading_day):
    """
    Checks what fraction of signal_log rows were enriched by ai_enrich.
    ai_conviction is set by ai_enrich — NULL means not enriched.
    Low coverage = ai_enrich crashed mid-run or budget was exhausted.
    """
    total = sb.table("signal_log").select("id", count="exact") \
              .eq("date", last_trading_day).execute().count or 0

    if total == 0:
        return _result("C17_signal_enrich_coverage", False, "WARN",
            f"No signal_log rows for {last_trading_day} — signals step may not have run")

    enriched = sb.table("signal_log").select("id", count="exact") \
                 .eq("date", last_trading_day) \
                 .not_.is_("ai_conviction", "null").execute().count or 0

    ratio = enriched / total
    ok    = ratio >= ENRICH_WARN_RATIO
    sev   = "ERROR" if ratio < ENRICH_ERROR_RATIO else "WARN"

    return _result("C17_signal_enrich_coverage", ok, sev,
        f"ai_enrich coverage: {enriched}/{total} signals enriched ({ratio:.0%})"
        + (" ✅" if ok else f" — below {ENRICH_WARN_RATIO:.0%} threshold"),
        value=f"{enriched}/{total}")


# ── Dedup Guard ───────────────────────────────────────────────────────────────

def _already_logged_today(sb, today):
    try:
        cnt = sb.table("data_anomalies").select("id", count="exact").eq("date", today).execute().count or 0
        return cnt > 0
    except Exception:
        return False


# ── Telegram Alert (condensed format) ────────────────────────────────────────

def _send_condensed_alert(results, today, last_trading_day):
    """
    Sends one condensed Telegram message with:
    - Summary line (OK/WARN/ERROR counts)
    - Only failing checks listed with short reason
    - Last trading day context
    """
    try:
        from alerts.send_alerts import send_telegram_message

        ok_n   = sum(1 for r in results if r["severity"] == "OK")
        warn_n = sum(1 for r in results if r["severity"] == "WARN")
        err_n  = sum(1 for r in results if r["severity"] == "ERROR")

        lines = [
            f"📊 <b>TradeOS Quality — {today}</b> (LTD: {last_trading_day})",
            f"✅ {ok_n} OK  ⚠️ {warn_n} WARN  🔴 {err_n} ERROR",
        ]

        # Only list failing checks
        failures = [r for r in results if r["severity"] in ("ERROR", "WARN")]
        if failures:
            lines.append("")
            for r in failures:
                icon = "🔴" if r["severity"] == "ERROR" else "⚠️"
                # Truncate message to keep it concise
                msg = r["message"][:120]
                lines.append(f"{icon} <b>{r['check']}</b>: {msg}")
        else:
            lines.append("✨ All checks passed — pipeline healthy")

        send_telegram_message("\n".join(lines))
    except Exception as ex:
        logger.warning(f"Quality Telegram alert failed: {ex}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — data_quality_monitor skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb               = get_supabase()
    today            = str(today_ist())
    last_trading_day = get_last_trading_day(sb)

    logger.info(f"data_quality_monitor v3: 17 checks | 5-state regime | today={today} | LTD={last_trading_day}")

    all_checks = [
        # ── Data Integrity ──────────────────────────────────────────────────
        ("C01", lambda: c01_chartink_row_count(sb, today)),
        ("C02", lambda: c02_rsi_range(sb, today)),
        ("C03", lambda: c03_vol_ratio_cap(sb, today)),
        ("C04", lambda: c04_delivery_pct_bounds(sb, today)),
        ("C05", lambda: c05_signal_score_range(sb, today)),
        ("C06", lambda: c06_msl_score_jumps(sb, today)),
        # ── Pipeline Completeness ───────────────────────────────────────────
        ("C07", lambda: c07_pipeline_completeness(sb, today, last_trading_day)),
        ("C08", lambda: c08_ai_context_completeness(sb, today, last_trading_day)),
        # ── Regime & Positions ──────────────────────────────────────────────
        ("C09", lambda: c09_regime_ml_vs_manual(sb, today)),
        ("C10", lambda: c10_positions_vs_regime_cap(sb, today)),
        # ── AI Intelligence Chain ───────────────────────────────────────────
        ("C11", lambda: c11_shortlist_validity(sb, last_trading_day)),
        ("C12", lambda: c12_market_intel_validity(sb, last_trading_day)),
        ("C13", lambda: c13_date_alignment(sb, today, last_trading_day)),
        ("C14", lambda: c14_ai_enrich_intel_consumed(sb, last_trading_day)),
        ("C15", lambda: c15_fii_regime_consistency(sb, today, last_trading_day)),
        # ── Lesson Quality ──────────────────────────────────────────────────
        ("C16", lambda: c16_lesson_quality(sb, last_trading_day)),
        # ── Signal Coverage ─────────────────────────────────────────────────
        ("C17", lambda: c17_signal_enrich_coverage(sb, last_trading_day)),
    ]

    results = []
    for label, fn in all_checks:
        try:
            r = fn()
            results.append(r)
            icon = {"OK": "✅", "WARN": "⚠️", "ERROR": "🔴"}.get(r["severity"], "?")
            logger.info(f"  {icon} {r['check']}: {r['message'][:100]}")
        except Exception as e:
            logger.warning(f"{label} threw exception: {e}")
            results.append(_result(f"{label}_exception", False, "WARN",
                f"Check threw exception: {e}"))

    ok_n   = sum(1 for r in results if r["severity"] == "OK")
    warn_n = sum(1 for r in results if r["severity"] == "WARN")
    err_n  = sum(1 for r in results if r["severity"] == "ERROR")

    # Write anomalies (dedup guard — skip if already ran today)
    anomalies = [r for r in results if r["severity"] in ("WARN", "ERROR")]
    if anomalies and not _already_logged_today(sb, today):
        try:
            sb.table("data_anomalies").insert([{
                "date":       today,
                "check_name": a["check"],
                "severity":   a["severity"],
                "value":      a.get("value", ""),
                "message":    a["message"],
                "affected":   str(a.get("affected", [])),
                "created_at": datetime.now(IST).isoformat(),
            } for a in anomalies]).execute()
        except Exception as e:
            logger.warning(f"data_anomalies insert failed: {e}")

    # Telegram — always send condensed summary (errors + warns)
    _send_condensed_alert(results, today, last_trading_day)

    auto_cap = next((r for r in results if r["check"] == "C03_vol_ratio_cap"), {})
    logger.success(
        f"Quality v3: ✅{ok_n} OK | ⚠️{warn_n} WARN | 🔴{err_n} ERROR | "
        f"17 checks | LTD={last_trading_day}"
        + (f" | 🔧{auto_cap.get('value','0')} vol_ratio capped"
           if auto_cap.get("value", "0") != "0" else "")
    )

    return {
        "date":            today,
        "last_trading_day": last_trading_day,
        "ok":              ok_n,
        "warn":            warn_n,
        "error":           err_n,
        "results":         results,
    }


if __name__ == "__main__":
    result = main()
    icons  = {"OK": "✅", "WARN": "⚠️", "ERROR": "🔴"}
    print(f"\n{'='*60}")
    print(f"TradeOS Quality Monitor v3 — {result.get('date')}")
    print(f"Last Trading Day: {result.get('last_trading_day')}")
    print(f"{'='*60}")
    for r in result.get("results", []):
        print(f"{icons.get(r['severity'], '?')} {r['check']}: {r['message']}")
    print(f"{'='*60}")
    print(f"Summary: ✅{result['ok']} OK | ⚠️{result['warn']} WARN | 🔴{result['error']} ERROR")