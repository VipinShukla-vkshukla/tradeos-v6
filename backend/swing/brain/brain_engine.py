"""
TradeOS v6 — Brain Engine v2: Orchestrator
============================================
Full holistic pipeline:
  script_scanner (+ behavioral profiler + consistency checker)
  → data_aggregator → quant_analyzer → llm_synthesizer
  → backtester (quant AND llm proposals, same mechanism)
  → change_manager (per-type auto-apply policy) → performance_tracker → Telegram

Modes:
  full   — everything including LLM, script scanning/profiling, weekly digest (weekly)
  quant  — quant + backtest + proposals, no LLM, no scan (mid-week)
  scan   — script scan + profiling + consistency check only (no trading-data analysis)
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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import get_supabase, cfg, cfg_int, is_kill_switch_active

from swing.brain.data_aggregator   import build_analysis_dataset
from swing.brain.quant_analyzer    import run_analysis
from swing.brain.backtester_and_change_manager import (
    run_backtests,
    save_proposals, process_auto_approvals,
    get_pending_proposals, send_telegram_digest,
)
from swing.brain.performance_tracker import run_performance_tracking


def _make_run_id() -> str:
    return f"brain_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def _merge_proposals(quant_validated: list, llm_proposals: list, max_total: int) -> list:
    """
    Normalize every finding type into the unified schema (target_key/current_value/
    proposed_value) BEFORE dedup. Without this, any finding that doesn't carry a
    "key" field (ENGINE_WEIGHT, ENGINE_PERFORMANCE, INSIGHT — i.e. everything except
    THRESHOLD_CHANGE) has no target_key, fails the "if not k: continue" check below,
    and is silently dropped — never saved, never visible, never reviewable.
    """
    seen, merged = set(), []
    for p in quant_validated + llm_proposals:
        p = dict(p)  # never mutate the caller's dict
        ptype = p.get("type", "")

        if "key" in p and "target_key" not in p:
            p["target_key"]     = p.pop("key")
            p["current_value"]  = str(p.pop("current", ""))
            p["proposed_value"] = str(p.pop("proposed", ""))

        elif "target_key" not in p:
            if ptype == "ENGINE_WEIGHT" and p.get("regime") and p.get("engine"):
                p["target_key"]     = f"{p['regime']}:{p['engine']}"
                p.setdefault("current_value", str(p.get("baseline", "")))
                p.setdefault("proposed_value", p.get("direction", ""))
            elif ptype == "ENGINE_PERFORMANCE" and p.get("engine"):
                p["target_key"]      = f"engine_performance:{p['engine']}"
                p.setdefault("current_value", str(p.get("win_rate", "")))
                p.setdefault("proposed_value", p.get("direction", ""))
            elif ptype == "INSIGHT":
                basis = (p.get("suggested_rule")
                         or "-".join(p.get("fields", []))
                         or str(p.get("evidence_summary", ""))[:60])
                p["target_key"] = f"insight:{basis}"[:120] if basis else None
                p.setdefault("current_value", "")
                p.setdefault("proposed_value", "")

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
    scan_proposals       = []
    if mode in ("full", "scan"):
        logger.info("Step 1/6 — Scanning scripts (hardcode + behavioral profiling "
                    "+ cross-script consistency)...")
        try:
            from swing.brain.script_scanner import run_scan
            script_scan_results, report, scan_proposals = run_scan()
            logger.info(report)
        except Exception as e:
            logger.warning(f"Script scan failed (non-fatal): {e}")
            errors.append({"step": "script_scanner", "error": str(e)})

    if mode == "scan":
        scan_proposals = sorted(
            scan_proposals,
            key=lambda x: (int(x.get("priority", 5)), -float(x.get("confidence", 0)))
        )[:max_proposals]
        saved_ids = save_proposals(scan_proposals, run_id) if scan_proposals else []
        pending   = get_pending_proposals(get_supabase())
        send_telegram_digest(pending, run_id, auto_applied=0)
        return {"run_id": run_id, "status": "SCAN_ONLY",
                "scripts_scanned": len(script_scan_results),
                "new_proposals": len(saved_ids)}

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
        from swing.brain.dynamic_registry import DynamicRegistry
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

    # ── LLM synthesis (full mode only) — moved BEFORE backtesting ─────────
    logger.info("Step 4/6 — LLM synthesis (full data, all fields)..."
                 if mode == "full" else "Step 4/6 — LLM synthesis skipped (mode=quant)")
    narrative, llm_proposals = {}, []
    if mode == "full":
        try:
            from swing.brain.llm_synthesizer import synthesize
            llm_proposals, narrative = synthesize(
                quant_findings, dataset,
                script_scan_results=script_scan_results,
                max_proposals=8,
                registry=registry,          # ← LLM sees discovered engines/fields
            )
        except Exception as e:
            logger.warning(f"LLM synthesis failed (non-fatal): {e}")
            errors.append({"step": "llm_synthesizer", "error": str(e), "fatal": False})

    # ── Backtest — quant findings AND LLM proposals, same mechanism ───────
    # LLM-sourced THRESHOLD_CHANGE/ENGINE_WEIGHT proposals used to get a fake
    # "always passes" stamp and could never be evidence-checked. Now they're
    # reshaped (via the registry's threshold_map) into the exact same raw
    # finding format quant proposals use, and run through the identical
    # backtester — auto-apply can only ever fire on real, measured evidence.
    logger.info("Step 5/6 — Backtesting all proposals (quant + LLM)...")
    quant_raw = (quant_findings.get("threshold_proposals", [])
                 + quant_findings.get("score_component_findings", [])
                 + quant_findings.get("engine_performance", [])
                 + quant_findings.get("improvement_insights", []))

    llm_backtestable, llm_passthrough = [], []
    if llm_proposals:
        try:
            from swing.brain.llm_synthesizer import prepare_for_backtest
            llm_backtestable, llm_passthrough = prepare_for_backtest(llm_proposals, registry)
        except Exception as e:
            logger.warning(f"  LLM backtest prep failed, treating LLM proposals as review-only: {e}")
            llm_passthrough = llm_proposals

    try:
        import pandas as pd
        validated = run_backtests(
            dataset.get("signals", pd.DataFrame()), quant_raw + llm_backtestable
        )
    except Exception as e:
        logger.error(f"Backtesting failed: {e}")
        errors.append({"step": "backtester", "error": str(e)})
        validated = []

    # ── Merge and save ───────────────────────────────────────────────────
    # scan_proposals (CODE_SUGGESTION/CONSISTENCY_CONFLICT from Step 1) were
    # being computed and then silently discarded here — _merge_proposals only
    # ever saw quant+LLM findings. They already carry a target_key (set by
    # script_profiler.py/consistency_checker.py directly), so they pass
    # through _merge_proposals's dedup untouched, same as llm_passthrough.
    #
    # Capped independently before merging: a heavy profiling week (e.g. the
    # first run after deployment, scanning everything at once) can easily
    # produce more scan findings alone than max_proposals — without its own
    # cap here, _merge_proposals's single combined slice at the end would let
    # whichever category is sorted first crowd out the other entirely.
    scan_proposals = sorted(
        scan_proposals,
        key=lambda x: (int(x.get("priority", 5)), -float(x.get("confidence", 0)))
    )[:max_proposals]
    all_proposals = _merge_proposals(
        validated, llm_passthrough + scan_proposals, max_total=max_proposals * 2
    )

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

    # ── Weekly performance digest — only on the full weekly cycle ─────────
    # This replaces the separate brain_sunday_chain.yml workflow. That workflow
    # fired on every completion of brain_scheduler.yml (up to 9x/week, not just
    # after the Sunday run), and the weekly grain it reported on was never
    # actually computed (its only caller never runs on a Sunday). Computing
    # and sending it here means it happens exactly once, exactly when this
    # job itself runs.
    if mode == "full":
        try:
            from swing.brain.performance_tracker import send_weekly_telegram_summary
            run_performance_tracking(for_date=date.today())
            send_weekly_telegram_summary(for_date=date.today())
        except Exception as e:
            logger.warning(f"Weekly performance digest failed (non-fatal): {e}")
            errors.append({"step": "weekly_digest", "error": str(e), "fatal": False})

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
