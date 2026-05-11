"""
TradeOS v6 — Pipeline Orchestrator
Phase 0: foundation (ingest → signals → history)
Phase 1: + AI enrichment, FII, events, post-trade, alerts
Phase 2: + compute indicators, ASM, market news, macro, market intel, quality check

STEP ORDER FIX (01-Apr-2026 audit):
  Phase 2 previously ran fii_dii (10), nse_events (11), asm_gsm (12) AFTER signals (08).
  generate_signals.py reads fii_flag, event_calendar, safety_lists at runtime.
  Running ingestion after signals means signals always saw yesterday's data.
  Fixed: fii_dii, nse_events, asm_gsm now run BEFORE compute_indicators and signals.
  Also added: regime_predict step (P2-D) between compute_indicators and signals.
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
        result  = fn()
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
        logger.info(f"  {status}  {name:<35} {r['elapsed']:.1f}s")
    logger.info(f"{'─'*60}")
    logger.info(f"  Total: {total:.1f}s | ✅ {ok} OK | ❌ {err} Failed")
    logger.info(f"{'═'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="TradeOS v6 Pipeline")
    parser.add_argument("--step",      help="Run single step only")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--calibrate", action="store_true", help="Run weekly calibration only")
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

    # ── Step definitions ──────────────────────────────────────────────────────
    def step_global_cues_evening():
        from ingestion.ingest_global_cues import main as fn; return fn("EVENING")

    def step_fetch_chartink():
        from ingestion.fetch_chartink import main as fn; return fn()

    def step_ingest_bhavcopy():
        from ingestion.ingest_bhavcopy import main as fn; return fn()

    def step_ingest_sheets():
        from ingestion.ingest_sheets import main as fn; return fn()

    def step_fii_dii():
        from ingestion.ingest_fii_dii import main as fn; return fn()

    def step_nse_events():
        from ingestion.ingest_nse_events import main as fn; return fn()

    def step_signals():
        from signals.generate_signals import main as fn; return fn()

    def step_history():
        from history.append_history import main as fn; return fn()

    def step_post_trade():
        from ai.post_trade_analysis import main as fn; return fn()

    def step_ai_decision_engine():
        from ai.ai_decision_engine import main as fn; return fn()

    def step_alerts():
        from alerts.send_alerts import main as fn; return fn()

    # Phase 2 steps
    def step_market_news():
        from ingestion.ingest_market_news import main as fn; return fn()

    def step_macro_indicators():
        from ingestion.ingest_macro_indicators import main as fn; return fn()

    def step_asm_gsm():
        from ingestion.ingest_asm_gsm import main as fn; return fn()

    def step_compute_indicators():
        from compute.compute_indicators import main as fn; return fn()

    def step_regime_predict():
        """
        P2-D: ML regime prediction. Runs AFTER compute_indicators (needs fresh
        sector breadth data) and BEFORE signals (_resolve_regime reads predicted_regime).
        Non-fatal: if model absent or autonomy_phase < 2, returns silently.
        Manual regime from ingest_sheets remains in control until model is trained.
        """
        import subprocess
        result = subprocess.run(
            [sys.executable, "ai/providers/ml_regime_classifier.py", "--predict"],
            capture_output=True, text=True, cwd=str(Path(__file__).parent)
        )
        if result.stdout:
            logger.debug(f"regime_predict: {result.stdout[-300:]}")
        if result.returncode != 0:
            logger.warning(f"regime_predict non-fatal error: {result.stderr[-300:]}")

    def step_ai_market_intel():
        from ai.market_intelligence_engine import main as fn; return fn()

    def step_quality_check():
        from compute.data_quality_monitor import main as fn; return fn()
    
    def step_performance_tracking():
        """Compute and store daily/weekly/monthly performance metrics."""
        from brain.performance_tracker import run_performance_tracking
        import datetime
        result = run_performance_tracking(for_date=datetime.date.today())
        logger.info(f"Performance tracking: {result}")
    
    def step_compute_regime():
        from compute.compute_regime import main as fn; return fn()
    
    def step_screen_stocks():
        from signals.screen_stocks import main as fn; return fn()
    
    def step_compute_msl():
        from compute.compute_msl import main as fn; return fn()
    
    def step_calibration():
        from calibration.calibration_engine import main as fn; return fn()
    
    if args.calibrate:
        result = step_calibration()
        print(f"Calibration: {result}")
        sys.exit(0)

    # ── Phase 0 steps ─────────────────────────────────────────────────────────
    steps_p0 = [
        ("01_global_cues",     step_global_cues_evening, False),
        ("02_fetch_chartink",  step_fetch_chartink,      True),
        ("03_ingest_bhavcopy", step_ingest_bhavcopy,     False),
        ("04_ingest_sheets",   step_ingest_sheets,       True),
        ("05_signals",         step_signals,             True),
        ("06_history",         step_history,             False),
    ]

    # ── Phase 1 extras ────────────────────────────────────────────────────────
    steps_p1 = [
        ("07_fii_dii",            step_fii_dii,            False),
        ("08_nse_events",         step_nse_events,         False),
        ("09_post_trade",         step_post_trade,         False),
        ("10_ai_decision_engine", step_ai_decision_engine, False),
        ("11_alerts",             step_alerts,             False),
    ]

    # ── Phase 2 — corrected full sequence ─────────────────────────────────────
    #
    # KEY ORDERING RULES (generate_signals reads these at runtime):
    #   fii_dii     → must run BEFORE signals  (reads fii_flag from fii_dii_flow)
    #   nse_events  → must run BEFORE signals  (reads event_calendar)
    #   asm_gsm     → must run BEFORE signals  (reads safety_lists for ASM/FO_BAN)
    #   compute_ind → must run BEFORE signals  (populates stock_data_daily computed cols)
    #   regime_pred → must run AFTER compute   (needs sector breadth from sector_strength)
    #                 must run BEFORE signals   (_resolve_regime reads predicted_regime)
    #
    # STEP ORDER FIX: fii_dii/nse_events/asm_gsm moved from steps 10/11/12
    # (after signals) to steps 06/07/08 (before compute_indicators and signals).
    if phase >= 2:
        all_steps = [
            # ── Data ingestion (all sources before any computation) ──
            ("01_market_news",         step_market_news,         False),  # scrape news
            ("02_macro_indicators",    step_macro_indicators,    False),  # CPI/WPI/GDP
            ("03_global_cues",         step_global_cues_evening, False),  # gift nifty, crude
            ("04_fetch_chartink",      step_fetch_chartink,      True),   # 500 stocks raw
            ("05_ingest_bhavcopy",     step_ingest_bhavcopy,     False),  # OHLCV + delivery
            ("06_ingest_sheets",       step_ingest_sheets,       True),   # regime, MSL, sectors
            # FIX: these three now run BEFORE signals (were after in original Phase 2)
            ("07_fii_dii",             step_fii_dii,             False),  # fii_net_5d/20d, fii_flag
            ("08_nse_events",          step_nse_events,          False),  # event_calendar
            ("09_asm_gsm",             step_asm_gsm,             False),  # safety_lists ASM/FO_BAN
            ("10_compute_indicators",  step_compute_indicators,  False),  # chartink_raw_data (current + 370d history), stock_data_daily (sheet baseline for reconcile), nifty_total_market (index flags), nifty_upcoming_events (next 14d), master_shortlist (in_master_shortlist flag), market_regime (nifty price for rs_vs_nifty), system_config (field trust levels)
            ("11_compute_regime",      step_compute_regime,      False),  # auto calculation of market regime a substitute of sheet regime input, runs after compute_indicators to use breadth data, regime_predict is non-fatal ML enhancement on top of this
            ("12_regime_predict",      step_regime_predict,      False),  # ML predicted_regime (P2-D)
            # ── Computation (after all raw data is fresh) ──
            ("13_screen_stocks",       step_screen_stocks,       False),  # 9 engines scan 500 stocks → all or top 30 based on condition → msl_computed (shadow) or master_shortlist (hybrid/full)
            ("14_compute_msl",          step_compute_msl,          False),  # 15 intelligence functions enrich screener symbols → holding_score, bb_context, vwap_alignment, final_score
            # ── Signal generation (all inputs now fresh) ──
            ("15_signals",             step_signals,             True),   # PRIME/STAGED/BREAKOUT/REENTRY
            ("16_history",             step_history,             False),  # msl_history + regime_history
            # ── Analysis and AI ──
            ("17_post_trade",          step_post_trade,          False),  # outcomes → lessons
            ("18_ai_market_intel",     step_ai_market_intel,     False),  # news synthesis
            ("19_ai_decision_engine",  step_ai_decision_engine,  False),  # conviction per signal
            # ── Output ──
            ("20_alerts",              step_alerts,              False),  # Telegram digest
            ("21_quality_check",       step_quality_check,       False), # data quality monitor with alerts
            ("22_performance_tracking", step_performance_tracking, False), # daily/weekly/monthly performance metrics
        ]
    else:
        all_steps = steps_p0 + (steps_p1 if phase >= 1 else [])

    if args.step:
        step_map = {s[0].split("_", 1)[1]: s for s in all_steps}
        if args.step not in step_map:
            logger.error(f"Unknown step: {args.step}. Available: {list(step_map.keys())}")
            sys.exit(1)
        name, fn, fatal = step_map[args.step]
        t0 = time.time()
        r  = run_step(name, fn, fatal)
        print_summary({name: r}, time.time() - t0)
        sys.exit(0 if r["ok"] else 1)

    results = {}
    t_start = time.time()
    for name, fn, fatal in all_steps:
        r = run_step(name, fn, fatal)
        results[name] = r
        if not r["ok"] and fatal:
            logger.error(f"Fatal failure at {name} — stopping pipeline")
            break

    print_summary(results, time.time() - t_start)
    import datetime as _dt
    if _dt.date.today().weekday() == 6:  # Sunday
        try:
            from brain.scheduler_yaml import trigger_brain_async
            trigger_brain_async(mode="full")
            logger.info("Brain engine triggered async (Sunday full run)")
        except ImportError:
            logger.debug("Brain scheduler not yet deployed — skipping")
    sys.exit(0 if all(r["ok"] for r in results.values()) else 1)


if __name__ == "__main__":
    main()

#Get-ChildItem -Recurse -Filter "*.py" | Select-String "ctl_enabled|sbs_enabled|tpo_enabled|eap_enabled"#