"""
TradeOS v6 — Phase 2: Data Quality Monitor
===========================================
Validates every pipeline output at the end of each evening run.
Wire: Step 99 in run_pipeline.py — always last, always non-fatal.

This script does NOT block the pipeline. It observes, auto-corrects where safe,
logs anomalies to data_anomalies, and fires a Telegram ERROR alert when
something needs human attention before the next session opens.

Check catalogue (10 checks):
  C01  chartink_row_count      — Nifty 500 row count within expected band
  C02  rsi_range               — RSI values in 0-100 for all stocks
  C03  vol_ratio_cap           — Auto-cap outliers at 50x (safe auto-correct)
  C04  delivery_pct_bounds     — Delivery % in 0-100 for all stocks
  C05  signal_score_range      — signal_log scores in 0-120
  C06  msl_score_jumps         — Score changes >20 pts vs yesterday flagged
  C07  pipeline_completeness   — Key tables have today's rows (did steps run?)
  C08  ai_context_completeness — AI enrichment ran with full context (G17/G18)
  C09  regime_ml_vs_manual     — ML predicted_regime vs manual regime diff (P2+)
  C10  positions_vs_regime_cap — Open position count within regime max limit

Severity levels:
  OK    — check passed cleanly
  WARN  — anomaly detected, pipeline continues, log it
  ERROR — requires human review before next session, Telegram alert fired

Tables read:
  chartink_raw_data, stock_data_daily, signal_log, master_shortlist,
  msl_history, ai_context, market_regime, open_positions, system_config

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

CHARTINK_ROW_MIN  = 450
CHARTINK_ROW_MAX  = 510
VOL_RATIO_CAP     = 50.0
SCORE_MIN         = 0
SCORE_MAX         = 120
MSL_JUMP_WARN     = 20
DELIVERY_MIN      = 0.0
DELIVERY_MAX      = 100.0

# Fields that must exist in ai_context.context_json after all patches applied
AI_CONTEXT_FIELDS = [
    "market_regime",    # G17 — Patch 07
    "global_cues",      # G17 — Patch 07
    "portfolio",        # G18 — Patch 08
    "upcoming_events",  # G6  — Patch 05
    "sector_context",   # G13 — Patch 06
]

# Must match risk_manager.py MAX_POSITIONS
REGIME_MAX_POSITIONS = {
    "TRENDING": 8,
    "NEUTRAL":  6,
    "CAUTION":  4,
    "RISK OFF": 3,
}

REGIME_TIER = {"TRENDING": 0, "NEUTRAL": 1, "CAUTION": 2, "RISK OFF": 3}
REGIME_ML_TIER_ERROR = 2   # tier diff at which disagreement becomes ERROR


# ── Result Builder ────────────────────────────────────────────────────────────

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

def c01_chartink_row_count(sb, today):
    cnt = sb.table("chartink_raw_data").select("id", count="exact").eq("date", today).execute().count or 0
    ok  = CHARTINK_ROW_MIN <= cnt <= CHARTINK_ROW_MAX
    sev = "ERROR" if cnt == 0 else "WARN"
    return _result("C01_chartink_row_count", ok, sev,
        f"chartink_raw_data: {cnt} rows today (expected {CHARTINK_ROW_MIN}–{CHARTINK_ROW_MAX})",
        value=str(cnt))


# ── C02: RSI Range ────────────────────────────────────────────────────────────

def c02_rsi_range(sb, today):
    rows = sb.table("stock_data_daily").select("symbol, rsi_daily").eq("date", today).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("rsi_daily") is not None and not (0 <= float(r["rsi_daily"]) <= 100)]
    sev  = "ERROR" if len(bad) > 10 else "WARN"
    return _result("C02_rsi_range", len(bad) == 0, sev,
        f"{len(bad)} stocks with RSI outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C03: vol_ratio Cap (auto-correct) ────────────────────────────────────────

def c03_vol_ratio_cap(sb, today):
    """Only check that writes back — safe because >50x is always a data artefact."""
    outliers  = sb.table("stock_data_daily").select("symbol, vol_ratio").eq("date", today).gt("vol_ratio", VOL_RATIO_CAP).execute().data
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
    rows = sb.table("stock_data_daily").select("symbol, delivery_pct").eq("date", today).execute().data
    bad  = [r["symbol"] for r in rows
            if r.get("delivery_pct") is not None
            and not (DELIVERY_MIN <= float(r["delivery_pct"]) <= DELIVERY_MAX)]
    return _result("C04_delivery_pct_bounds", len(bad) == 0, "WARN",
        f"{len(bad)} stocks with delivery_pct outside 0–100" + (f": {bad[:5]}" if bad else ""),
        value=str(len(bad)), affected=bad[:10])


# ── C05: Signal Score Range ───────────────────────────────────────────────────

def c05_signal_score_range(sb, today):
    sigs = sb.table("signal_log").select("symbol, score").eq("date", today).execute().data
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
                 for r in sb.table("master_shortlist").select("symbol, score").execute().data}
    yest_map  = {r["symbol"]: float(r.get("score") or 0)
                 for r in sb.table("msl_history").select("symbol, score").eq("date", yesterday).execute().data}
    jumps = sorted(
        [{"symbol": s, "delta": round(now_map[s] - yest_map[s], 1)}
         for s in now_map if s in yest_map and abs(now_map[s] - yest_map[s]) > MSL_JUMP_WARN],
        key=lambda j: abs(j["delta"]), reverse=True
    )
    return _result("C06_msl_score_jumps", len(jumps) == 0, "WARN",
        f"{len(jumps)} stocks with score change >{MSL_JUMP_WARN}pts vs yesterday"
        + (f": {[(j['symbol'], j['delta']) for j in jumps[:3]]}" if jumps else ""),
        value=str(len(jumps)), affected=[j["symbol"] for j in jumps[:10]])


# ── C07: Pipeline Completeness ────────────────────────────────────────────────

def c07_pipeline_completeness(sb, today):
    """Verify each pipeline step actually wrote data today."""
    steps = [
        # (table, filter_by_today, step_label, severity_if_empty)
        ("chartink_raw_data", True,  "Step 01 fetch_chartink",     "ERROR"),
        ("stock_data_daily",  True,  "Step 02 ingest_bhavcopy",    "WARN"),
        ("master_shortlist",  False, "Step 03 ingest_sheets",      "ERROR"),
        ("signal_log",        True,  "Step 04 generate_signals",   "ERROR"),
        ("msl_history",       True,  "Step 05 append_history",     "WARN"),
        ("fii_dii_flow",      True,  "Step 06 ingest_fii_dii",     "WARN"),
        ("event_calendar",    False, "Step 07 ingest_nse_events",  "WARN"),
    ]
    missing = []
    for table, date_filter, label, sev in steps:
        try:
            q   = sb.table(table).select("id", count="exact")
            cnt = (q.eq("date", today) if date_filter else q).execute().count or 0
            if cnt == 0:
                missing.append({"step": label, "table": table, "severity": sev})
        except Exception as e:
            missing.append({"step": label, "table": table, "severity": "WARN", "reason": str(e)})

    has_error = any(m["severity"] == "ERROR" for m in missing)
    sev = "ERROR" if has_error else "WARN"
    return _result("C07_pipeline_completeness", len(missing) == 0, sev,
        f"{len(missing)} pipeline steps have no data for {today}"
        + (f": {[m['step'] for m in missing]}" if missing else ""),
        value=str(len(missing)), affected=[m["table"] for m in missing])


# ── C08: AI Context Completeness ─────────────────────────────────────────────

def c08_ai_context_completeness(sb, today):
    """
    Checks if ai_enrich ran AND whether the G6/G13/G17/G18 patch fields
    are present in context_json. Missing fields = patches not yet applied.
    Samples 5 rows only — enough to detect systemic gaps without a heavy query.
    """
    rows = sb.table("ai_context").select("symbol, context_json").eq("date", today).limit(5).execute().data

    if not rows:
        return _result("C08_ai_context_completeness", False, "WARN",
            f"No ai_context rows for {today} — ai_enrich may not have run or no BUY_CANDIDATEs")

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
    msg = ("AI context complete — all G6/G13/G17/G18 patches active"
           if ok else
           f"{len(missing_by_symbol)} symbols missing context fields (patches pending): "
           + str({s: f for s, f in list(missing_by_symbol.items())[:3]}))
    return _result("C08_ai_context_completeness", ok, "WARN", msg,
        value=str(len(missing_by_symbol)), affected=list(missing_by_symbol.keys()))


# ── C09: Regime ML vs Manual ──────────────────────────────────────────────────

def c09_regime_ml_vs_manual(sb, today):
    """
    Cross-checks ML predicted_regime vs manual regime.
    ml_regime_classifier.py already flags disagreements when it runs —
    this acts as an independent pipeline-level cross-check.
    Skips cleanly if ml_regime_classifier not yet deployed (Phase 2 gate).
    """
    row = sb.table("market_regime").select("regime, predicted_regime, regime_confidence") \
        .eq("date", today).limit(1).execute().data

    if not row:
        return _result("C09_regime_ml_vs_manual", True, "WARN",
            "No market_regime row today — ingest_sheets may not have run")

    manual    = (row[0].get("regime")           or "").replace("_", " ").upper().strip()
    predicted = (row[0].get("predicted_regime") or "").replace("_", " ").upper().strip()
    conf      = row[0].get("regime_confidence")

    if not predicted:
        return _result("C09_regime_ml_vs_manual", True, "WARN",
            "predicted_regime not set — ml_regime_classifier not yet deployed (Phase 2 gate)")

    mt = REGIME_TIER.get(manual, -1)
    pt = REGIME_TIER.get(predicted, -1)
    if mt < 0 or pt < 0:
        return _result("C09_regime_ml_vs_manual", True, "WARN",
            f"Unknown regime label: manual='{manual}' predicted='{predicted}'")

    diff = abs(mt - pt)
    ok   = diff <= 1
    sev  = "ERROR" if diff >= REGIME_ML_TIER_ERROR else "WARN"
    conf_str = f" conf={float(conf):.0%}" if conf else ""
    return _result("C09_regime_ml_vs_manual", ok, sev,
        f"Regime manual={manual} vs ML={predicted}{conf_str} tier_diff={diff}"
        + (" ✅" if ok else " ⚠️ investigate"),
        value=f"diff={diff}")


# ── C10: Open Positions vs Regime Cap ────────────────────────────────────────

def c10_positions_vs_regime_cap(sb, today):
    """
    Warn if open positions exceed regime maximum. Not an ERROR because existing
    positions are not a pipeline problem — but signals risk_manager will block
    new entries and you should be aware.
    """
    regime_row = sb.table("market_regime").select("regime").order("date", desc=True).limit(1).execute().data
    regime     = (regime_row[0].get("regime") or "NEUTRAL").replace("_", " ").upper().strip() if regime_row else "NEUTRAL"
    max_pos    = REGIME_MAX_POSITIONS.get(regime, 6)
    current    = sb.table("open_positions").select("symbol", count="exact").execute().count or 0

    ok  = current <= max_pos
    return _result("C10_positions_vs_regime_cap", ok, "WARN",
        f"Open positions: {current}/{max_pos} for {regime} regime"
        + (" — new entries blocked by risk_manager" if not ok else ""),
        value=f"{current}/{max_pos}")


# ── Dedup Guard ───────────────────────────────────────────────────────────────

def _already_logged_today(sb, today):
    try:
        cnt = sb.table("data_anomalies").select("id", count="exact").eq("date", today).execute().count or 0
        return cnt > 0
    except Exception:
        return False


# ── Telegram Alert ────────────────────────────────────────────────────────────

def _send_error_alert(errors):
    try:
        from alerts.send_alerts import send_telegram_message
        lines = ["🔴 <b>Pipeline Quality ERRORs — Review Before Open</b>"]
        for e in errors:
            lines.append(f"❌ <b>{e['check']}</b>: {e['message']}")
        send_telegram_message("\n".join(lines))
    except Exception as ex:
        logger.warning(f"Quality ERROR Telegram alert failed: {ex}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — data_quality_monitor skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb    = get_supabase()
    today = str(today_ist())
    logger.info(f"data_quality_monitor: 10 checks for {today}")

    all_checks = [
        ("C01", lambda: c01_chartink_row_count(sb, today)),
        ("C02", lambda: c02_rsi_range(sb, today)),
        ("C03", lambda: c03_vol_ratio_cap(sb, today)),
        ("C04", lambda: c04_delivery_pct_bounds(sb, today)),
        ("C05", lambda: c05_signal_score_range(sb, today)),
        ("C06", lambda: c06_msl_score_jumps(sb, today)),
        ("C07", lambda: c07_pipeline_completeness(sb, today)),
        ("C08", lambda: c08_ai_context_completeness(sb, today)),
        ("C09", lambda: c09_regime_ml_vs_manual(sb, today)),
        ("C10", lambda: c10_positions_vs_regime_cap(sb, today)),
    ]

    results = []
    for label, fn in all_checks:
        try:
            results.append(fn())
        except Exception as e:
            logger.warning(f"{label} threw exception: {e}")
            results.append(_result(f"{label}_exception", False, "WARN",
                f"Check threw exception: {e}"))

    ok_n   = sum(1 for r in results if r["severity"] == "OK")
    warn_n = sum(1 for r in results if r["severity"] == "WARN")
    err_n  = sum(1 for r in results if r["severity"] == "ERROR")

    # Write anomalies — skip if pipeline reran same day (dedup guard)
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

    # Telegram only for ERRORs
    errors = [r for r in results if r["severity"] == "ERROR"]
    if errors:
        _send_error_alert(errors)

    auto_cap = next((r for r in results if r["check"] == "C03_vol_ratio_cap"), {})
    logger.info(
        f"Quality: ✅{ok_n} OK | ⚠️{warn_n} WARN | 🔴{err_n} ERROR"
        + (f" | 🔧{auto_cap.get('value','0')} vol_ratio capped" if auto_cap.get("value", "0") != "0" else "")
    )
    return {"date": today, "ok": ok_n, "warn": warn_n, "error": err_n, "results": results}


if __name__ == "__main__":
    result = main()
    icons  = {"OK": "✅", "WARN": "⚠️", "ERROR": "🔴"}
    for r in result.get("results", []):
        print(f"{icons.get(r['severity'], '?')} {r['check']}: {r['message']}")
