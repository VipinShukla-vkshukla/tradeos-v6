"""
TradeOS v6 — Brain Engine v2: Orchestrator
============================================
Full holistic pipeline:
  script_scanner → data_aggregator → quant_analyzer → backtester
  → llm_synthesizer → change_manager → performance_tracker → Telegram

Modes:
  full   — everything including LLM and script scanning (weekly)
  quant  — quant + backtest + proposals, no LLM (mid-week)
  scan   — script scan only (reports hardcoded values, no proposals)
  dry    — full analysis, no writes
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg, cfg_int, cfg_float, is_kill_switch_active

from brain.data_aggregator   import build_analysis_dataset
from brain.quant_analyzer    import run_analysis
from brain.backtester_and_change_manager import (
    run_backtests,
    save_proposals, process_auto_approvals,
    get_pending_proposals, send_telegram_digest,
)
from brain.performance_tracker import run_performance_tracking


def _make_run_id() -> str:
    return f"brain_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def _merge_proposals(quant_validated: list, llm_proposals: list, max_total: int) -> list:
    seen, merged = set(), []
    for p in quant_validated + llm_proposals:
        # Normalize quant field names → unified schema
        if "key" in p and "target_key" not in p:
            p = dict(p)
            p["target_key"]    = p.pop("key")
            p["current_value"] = str(p.pop("current", ""))
            p["proposed_value"]= str(p.pop("proposed", ""))
        k = p.get("target_key")
        if not k or k in seen:
            continue
        seen.add(k)
        merged.append(p)
    merged.sort(key=lambda x: (int(x.get("priority", 5)), -float(x.get("confidence", 0))))
    return merged[:max_total]


def run(mode: str = "full", dry_run: bool = False,
        period_days: Optional[int] = None) -> dict:

    start    = time.time()
    run_id   = _make_run_id()
    errors   = []

    logger.info(f"🧠 Brain v2 starting — {run_id} | mode={mode} | dry={dry_run}")

    if is_kill_switch_active():
        return {"run_id": run_id, "status": "ABORTED", "reason": "kill_switch"}

    if cfg("brain_enabled", "true").lower() != "true":
        return {"run_id": run_id, "status": "ABORTED", "reason": "brain_disabled"}

    period_days   = period_days or cfg_int("brain_period_days", 90)
    max_proposals = cfg_int("brain_max_proposals_per_run", 15)

    # ── Script scan (full mode only) ─────────────────────────────────────
    script_scan_results = []
    if mode in ("full", "scan"):
        logger.info("Step 1/6 — Scanning scripts for hardcoded values...")
        try:
            from brain.script_scanner import ScriptScanner
            scanner = ScriptScanner()
            script_scan_results = scanner.scan_all()
            report  = scanner.generate_scan_report(script_scan_results)
            logger.info(report)
        except Exception as e:
            logger.warning(f"Script scan failed (non-fatal): {e}")
            errors.append({"step": "script_scanner", "error": str(e)})

    if mode == "scan":
        return {"run_id": run_id, "status": "SCAN_ONLY",
                "scripts_scanned": len(script_scan_results)}

    # ── Data loading ─────────────────────────────────────────────────────
    logger.info("Step 2/6 — Loading full dataset (SELECT * everywhere)...")
    try:
        dataset = build_analysis_dataset(days=period_days)
    except Exception as e:
        logger.error(f"Data loading failed: {e}")
        return {"run_id": run_id, "status": "FAILED",
                "errors": [{"step": "data_aggregator", "error": str(e)}]}
 
    if dataset.get("signals_analyzed", 0) == 0:
        logger.warning("No signals in lookback window")
        return {"run_id": run_id, "status": "NO_DATA"}
 
    # ── Dynamic Registry — build ONCE, pass through entire run ───────────
    # Resolves engines, tables, categorical fields, threshold map from live DB.
    # Any new engine/table/column is auto-included from this point forward.
    registry = None
    try:
        from brain.dynamic_registry import DynamicRegistry
        registry = DynamicRegistry(dataset)
        logger.info(f"  Registry: {registry.summary()}")
        # Stash registry stats in dataset for LLM to see
        dataset["registry_summary"] = {
            "engines":            registry.engines,
            "regime_names":       registry.regime_names,
            "tables_discovered":  registry.tables,
            "categorical_fields": registry.categorical_fields,
            "threshold_mappings": len(registry.threshold_map),
        }
    except Exception as e:
        logger.warning(f"  DynamicRegistry failed ({e}) — analyses will use fallbacks")
        errors.append({"step": "dynamic_registry", "error": str(e), "fatal": False})
 
    # ── Quant analysis ───────────────────────────────────────────────────
    logger.info("Step 3/6 — Quantitative analysis (all fields)...")
    try:
        quant_findings = run_analysis(dataset, registry=registry)   # ← pass registry
    except Exception as e:
        logger.error(f"Quant analysis failed: {e}")
        errors.append({"step": "quant_analyzer", "error": str(e)})
        quant_findings = {}

    # ── Backtest ─────────────────────────────────────────────────────────
    logger.info("Step 4/6 — Backtesting quant proposals...")
    quant_raw = (quant_findings.get("threshold_proposals", [])
                 + quant_findings.get("engine_performance", [])
                 + quant_findings.get("improvement_insights", []))
    try:
        import pandas as pd
        quant_validated = run_backtests(
            dataset.get("signals", pd.DataFrame()), quant_raw
        )
    except Exception as e:
        logger.error(f"Backtesting failed: {e}")
        errors.append({"step": "backtester", "error": str(e)})
        quant_validated = []

    # ── LLM synthesis (full mode only) ───────────────────────────────────
    narrative, llm_proposals = {}, []
    if mode == "full":
        logger.info("Step 5/6 — LLM synthesis (full data, all fields)...")
        try:
            from brain.llm_synthesizer import synthesize
            llm_proposals, narrative = synthesize(
                quant_findings, dataset,
                script_scan_results=script_scan_results,
                max_proposals=8,
                registry=registry,          # ← LLM sees discovered engines/fields
            )
        except Exception as e:
            logger.warning(f"LLM synthesis failed (non-fatal): {e}")
            errors.append({"step": "llm_synthesizer", "error": str(e), "fatal": False})
    else:
        logger.info("Step 5/6 — LLM synthesis skipped (mode=quant)")

    # ── Merge and save ───────────────────────────────────────────────────
    all_proposals = _merge_proposals(quant_validated, llm_proposals, max_proposals)

    logger.info(f"\n{'═'*60}")
    logger.info(f"BRAIN SUMMARY: {len(all_proposals)} proposals")
    logger.info(f"  Signals: {dataset['signals_analyzed']} ({dataset['coverage_pct']:.0f}% outcomes)")
    for p in all_proposals:
        bt = p.get("backtest_result") or {}
        if isinstance(bt, str):
            try: bt = json.loads(bt)
            except: bt = {}
        wr_s = f"+{bt['wr_delta']:.1f}pp" if isinstance(bt.get("wr_delta"),(int,float)) else ""
        logger.info(f"  [{p['type']}] {p.get('target_key','?')[:45]} "
                    f"conf={p.get('confidence',0):.0%} {wr_s}")
    logger.info(f"{'═'*60}\n")

    if dry_run or mode == "dry":
        return {
            "run_id": run_id, "status": "DRY_RUN",
            "proposals": len(all_proposals),
            "elapsed_sec": round(time.time()-start, 1),
        }

    # ── Save + auto-apply ─────────────────────────────────────────────────
    logger.info("Step 6/6 — Saving proposals, processing approvals...")
    sb           = get_supabase()
    save_proposals(all_proposals, run_id)
    auto_applied = process_auto_approvals(dataset.get("config", {}))
    pending      = get_pending_proposals(sb)
    send_telegram_digest(pending, run_id, auto_applied=auto_applied)

    # ── Log run ───────────────────────────────────────────────────────────
    elapsed = time.time() - start
    try:
        sb.table("brain_analysis_log").insert({
            "run_id":               run_id,
            "run_date":             str(date.today()),
            "period_days":          period_days,
            "run_mode":             mode,
            "signals_analyzed":     dataset["signals_analyzed"],
            "signals_with_outcomes":dataset["signals_with_outcomes"],
            "coverage_pct":         dataset["coverage_pct"],
            "tables_loaded":        json.dumps(dataset.get("tables_loaded",{})),
            "scripts_analyzed":     json.dumps([
                {"path": r.get("script_path"), "coverage": r.get("brain_coverage")}
                for r in script_scan_results
            ]),
            "quant_findings":       json.dumps(quant_findings, default=str)[:8000],
            "llm_insights":         json.dumps(narrative, default=str)[:5000] if narrative else None,
            "proposals_generated":  len(all_proposals),
            "proposals_auto_applied": auto_applied,
            "execution_time_sec":   round(elapsed, 1),
            "errors":               json.dumps(errors) if errors else None,
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to log brain run: {e}")

    logger.success(
        f"🧠 Brain v2 complete in {elapsed:.0f}s — "
        f"{len(all_proposals)} proposals, {auto_applied} auto-applied, "
        f"{len(pending)} pending review"
    )

    return {
        "run_id":         run_id,
        "status":         "OK",
        "proposals":      len(all_proposals),
        "auto_applied":   auto_applied,
        "pending_review": len(pending),
        "elapsed_sec":    round(elapsed, 1),
        "errors":         errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TradeOS Brain Engine v2")
    parser.add_argument("--mode", choices=["full","quant","scan","dry"], default="full")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args   = parser.parse_args()
    mode   = "dry" if args.dry_run else args.mode
    result = run(mode=mode, dry_run=(mode=="dry"), period_days=args.days)
    print(json.dumps(result, indent=2, default=str))
