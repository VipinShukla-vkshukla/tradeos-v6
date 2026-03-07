"""
TradeOS v6 — Pipeline Orchestrator
Phase 0: 3 steps (ingest → signals → history)
Phase 1+: more steps added as components are enabled
"""
import sys, time, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from loguru import logger
from config import today_ist, is_kill_switch_active, cfg, cfg_bool


def run_step(name: str, fn, fatal: bool = True) -> dict:
    logger.info(f"\n{'─'*60}")
    logger.info(f"  {name}")
    logger.info(f"{'─'*60}")
    t0 = time.time()
    try:
        result = fn()
        elapsed = time.time() - t0
        logger.info(f"✓ {name} done in {elapsed:.1f}s")
        return {"ok": True, "result": result, "elapsed": elapsed}
    except Exception as e:
        elapsed = time.time() - t0
        logger.error(f"✗ {name} FAILED in {elapsed:.1f}s: {e}")
        if fatal:
            raise
        return {"ok": False, "error": str(e), "elapsed": elapsed}


def print_summary(results: dict, total: float):
    ok  = sum(1 for r in results.values() if r["ok"])
    err = sum(1 for r in results.values() if not r["ok"])
    logger.info(f"\n{'═'*60}")
    logger.info(f"  TradeOS v6 Pipeline — {today_ist()} IST")
    logger.info(f"{'═'*60}")
    for name, r in results.items():
        status = "✅ OK" if r["ok"] else "❌ FAIL"
        logger.info(f"  {status}  {name:<30} {r['elapsed']:.1f}s")
    logger.info(f"{'─'*60}")
    logger.info(f"  Total: {total:.1f}s | ✅ {ok} OK | ❌ {err} Failed")
    logger.info(f"{'═'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="TradeOS v6 Pipeline")
    parser.add_argument("--step", help="Run single step only")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if is_kill_switch_active():
        logger.critical("⛔ KILL SWITCH ACTIVE — pipeline aborted")
        sys.exit(1)

    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"

    phase = int(cfg("autonomy_phase", "0"))
    logger.info(f"{'═'*60}")
    logger.info(f"  TradeOS v6 Pipeline | Phase {phase}")
    logger.info(f"{'═'*60}")
# ── Step definitions ────────────────────────────────────────────────────
    # All defined here so --step can reference any of them by name.
    # Lazy imports keep startup fast — each module only loads if the step runs.

    def step_fetch_chartink():
        from ingestion.fetch_chartink import main as fn; return fn()

    def step_ingest():
        from ingestion.ingest_sheets import main as fn; return fn()

    def step_signals():
        from signals.generate_signals import main as fn; return fn()

    def step_history():
        from history.append_history import main as fn; return fn()

    def step_alerts():
        from alerts.send_alerts import main as fn; return fn()
    
    def step_post_trade():
        from ai.post_trade_analysis import main as fn; return fn()

    # Phase 0 steps (always run)
    steps_p0 = [
        ("01_fetch_chartink", step_fetch_chartink, True),
        ("02_ingest_sheets",  step_ingest,  True),
        ("03_signals",        step_signals, True),
        ("04_history",        step_history, False),
    ]

    # Phase 1 extras
    steps_p1 = [
        #("05_post_trade_analysis",  step_post_trade, False),
        #("06_ai_enrich",            step_ai_enrich,  False),
        ("07_alerts",               step_alerts,  False),
        
    ]

    all_steps = steps_p0 + (steps_p1 if phase >= 1 else [])

    if args.step:
        # Single step mode
        step_map = {s[0].split("_", 1)[1]: s for s in all_steps}
        if args.step not in step_map:
            logger.error(f"Unknown step: {args.step}. Available: {list(step_map.keys())}")
            sys.exit(1)
        name, fn, fatal = step_map[args.step]
        t0 = time.time()
        r  = run_step(name, fn, fatal)
        print_summary({name: r}, time.time() - t0)
        sys.exit(0 if r["ok"] else 1)

    # Full pipeline
    results = {}
    t_start = time.time()
    for name, fn, fatal in all_steps:
        r = run_step(name, fn, fatal)
        results[name] = r
        if not r["ok"] and fatal:
            logger.error(f"Fatal failure at {name} — stopping pipeline")
            break

    print_summary(results, time.time() - t_start)
    all_ok = all(r["ok"] for r in results.values())
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
