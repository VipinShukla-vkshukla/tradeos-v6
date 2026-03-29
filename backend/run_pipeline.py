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

    # ── Step definitions ─────────────────────────────────────────────────
    # All defined here so --step can reference any of them by name.
    # Lazy imports keep startup fast — each module only loads if the step runs.

    def step_global_cues_evening():
        from ingestion.ingest_global_cues import main as fn; return fn("EVENING")

    def step_fetch_chartink():
        from ingestion.fetch_chartink import main as fn; return fn()

    def step_ingest_bhavcopy():
        # Script exists in codebase. Wired here as Step 1.1 from deployment README.
        from ingestion.ingest_bhavcopy import main as fn; return fn()

    def step_ingest():
        from ingestion.ingest_sheets import main as fn; return fn()

    def step_signals():
        from signals.generate_signals import main as fn; return fn()

    def step_fii_dii():
        from ingestion.ingest_fii_dii import main as fn; return fn()

    def step_nse_events():
        from ingestion.ingest_nse_events import main as fn; return fn()

    def step_history():
        from history.append_history import main as fn; return fn()

    def step_alerts():
        from alerts.send_alerts import main as fn; return fn()

    def step_post_trade():
        from ai.post_trade_analysis import main as fn; return fn()

    def step_ai_enrich():
        from ai.ai_enrich import main as fn; return fn()

    def step_generate_shortlist():
        from ai.generate_shortlist import main as fn; return fn()

    # ── Phase 2 step stubs — defined here, only activated when phase >= 2 ─
    # These functions are never called in Phase 0/1; they just need to exist
    # so --step CLI can reference them by name during Phase 2 testing.
    def step_market_news():
        from ingestion.ingest_market_news import main as fn; return fn()

    def step_macro_indicators():
        # SG5: domestic macro data (CPI/WPI/GDP/IIP) + US 10-yr/silver backup
        from ingestion.ingest_macro_indicators import main as fn; return fn()

    def step_compute_indicators():
        from compute.compute_indicators import main as fn; return fn()

    def step_asm_gsm():
        from ingestion.ingest_asm_gsm import main as fn; return fn()

    def step_market_intel():
        from ai.market_intelligence_engine import main as fn; return fn()

    def step_quality_check():
        from compute.data_quality_monitor import main as fn; return fn()

    # ── Phase 0 core steps (must-run for signals/history) ─────────────────
    # Numbers are stable across all phases — same step = same number always.
    steps_p0 = [
        ("01_global_cues",      step_global_cues_evening, False),  # non-fatal: fixes change % bug, enable asap
        ("02_fetch_chartink",   step_fetch_chartink,      True),
        ("03_ingest_bhavcopy",  step_ingest_bhavcopy,     False),  # non-fatal: signals can run without it
        ("04_ingest_sheets",    step_ingest,              True),
        ("05_signals",          step_signals,             True),
        ("06_history",          step_history,             False),
    ]

    # ── Phase 1 extras ────────────────────────────────────────────────────
    steps_p1 = [
        ("07_fii_dii",            step_fii_dii,            False),
        ("08_nse_events",         step_nse_events,         False),
        ("09_post_trade",         step_post_trade,         False),
        ("10_ai_enrich",          step_ai_enrich,          False),
        ("11_generate_shortlist", step_generate_shortlist, False),  # AI top-12 ranking after enrich
        ("12_alerts",             step_alerts,             False),
    ]

    # ── Phase 2 — full sequence replacing phases 0+1 ──────────────────────
    # Two new ingestion steps prepended (market_news, macro_indicators).
    # compute_indicators inserted after ingest_sheets — it reads sheet values
    # as safe fallback, computes its own, reconciles field-by-field, and
    # overwrites where confident. Sheet values persist where compute diverges.
    # asm_gsm inserted after nse_events (needs events data to be fresh).
    # market_intel inserted after shortlist, before alerts.
    # quality_check always runs last, never fatal.
    #
    # Phase 0 step numbers shift by +2 to accommodate the two new leading steps.
    # Phase 1 step numbers shift by +3 to also accommodate compute_indicators.
    if phase >= 2:
        all_steps = [
            ("01_market_news",        step_market_news,        False),  # scrape news first
            ("02_macro_indicators",   step_macro_indicators,   False),  # structured macro data (CPI/WPI/GDP)
            ("03_global_cues",        step_global_cues_evening,False),  # fixes change % calculation
            ("04_fetch_chartink",     step_fetch_chartink,     True),
            ("05_ingest_bhavcopy",    step_ingest_bhavcopy,    False),
            ("06_ingest_sheets",      step_ingest,             True),   # writes sheet values first
            ("07_compute_indicators", step_compute_indicators, False),  # reads sheet → computes → reconciles
            ("08_signals",            step_signals,            True),
            ("09_history",            step_history,            False),
            ("10_fii_dii",            step_fii_dii,            False),
            ("11_nse_events",         step_nse_events,         False),
            ("12_asm_gsm",            step_asm_gsm,            False),  # safety lists, needs fresh events
            ("13_post_trade",         step_post_trade,         False),
            ("14_generate_shortlist", step_generate_shortlist, False),
            ("15_market_intel",       step_market_intel,       False),
            ("16_ai_enrich",          step_ai_enrich,          False), 
            ("17_alerts",             step_alerts,             False),
            ("18_quality_check",      step_quality_check,      False),  # always last, never fatal
        ]
    else:
        all_steps = steps_p0 + (steps_p1 if phase >= 1 else [])

    if args.step:
        # Single step mode — match by name after the number prefix
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