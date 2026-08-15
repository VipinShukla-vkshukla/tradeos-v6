"""
TradeOS v7 — Pipeline Orchestrator
Phase 0: foundation (ingest → signals → history)
Phase 1: + AI enrichment, FII, events, post-trade, alerts
Phase 2: + compute indicators, ASM, market news, macro, market intel, quality gate

STEP ORDER FIX (01-Apr-2026 audit):
  Phase 2 previously ran fii_dii (10), nse_events (11), asm_gsm (12) AFTER signals (08).
  generate_signals.py reads fii_flag, event_calendar, safety_lists at runtime.
  Running ingestion after signals means signals always saw yesterday's data.
  Fixed: fii_dii, nse_events, asm_gsm now run BEFORE compute_indicators and signals.
  Also added: regime_predict step (P2-D) between compute_indicators and signals.

STEP ORDER FIX (26-Jul-2026 audit):
  sector_strength was computed at step 06, four steps BEFORE compute_indicators
  wrote the columns it aggregates. It therefore matched zero rows and wrote
  nothing — every day, silently, because the v1 SQL function returned void and
  the caller logged success unconditionally. sector_strength went stale on
  2026-07-02 and was still stale on 2026-07-24 while stock_data_daily was
  current. Since all nine screener engines and generate_signals gate on
  sector_rank, and generate_signals has no fallback, the practical effect was:
  CTL/SBS/TPO produced zero candidates daily, and recommendations came from
  sector ranks three weeks out of date. Moved to step 11 and made fatal.

  Also: quality_check ran LAST (after alerts). Split into a pre-signal input
  gate (fatal) and a post-alert output audit (advisory).

  See migrations/001_sector_strength_v2.sql.
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
    logger.info(f"  TradeOS v7 Pipeline — {today_ist()} IST")
    logger.info(f"{'═'*60}")
    for name, r in results.items():
        status = "✅ OK" if r["ok"] else "❌ FAIL"
        logger.info(f"  {status}  {name:<35} {r['elapsed']:.1f}s")
    logger.info(f"{'─'*60}")
    logger.info(f"  Total: {total:.1f}s | ✅ {ok} OK | ❌ {err} Failed")
    logger.info(f"{'═'*60}\n")


def main():
    parser = argparse.ArgumentParser(description="TradeOS v7 Pipeline")
    parser.add_argument("--step",      help="Run single step only")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--date", help="Trading date (YYYY-MM-DD)")
    args = parser.parse_args()
    if is_kill_switch_active():
        logger.critical("⛔ KILL SWITCH ACTIVE — pipeline aborted")
        sys.exit(1)

    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"

    phase = int(cfg("autonomy_phase", "0"))
    logger.info(f"{'═'*60}")
    logger.info(f"  TradeOS v7 Pipeline | Phase {phase}")
    logger.info(f"{'═'*60}")

    # ── Step definitions ──────────────────────────────────────────────────────
    def step_global_cues_evening():
        from swing.ingestion.ingest_global_cues import main as fn; return fn("EVENING")

    def step_fetch_chartink():
        from swing.ingestion.fetch_chartink import main as fn; return fn()

    def step_ingest_bhavcopy():
        from swing.ingestion.ingest_bhavcopy import main as fn; return fn()

    def step_calendar_prep():
        """NSE holidays + event_calendar rollover. No data dependencies — runs early."""
        from swing.ingestion.ingest_sector_holiday_fixevents import main as fn; return fn()

    def step_sector_strength():
        """
        sector_strength + industry_strength.

        MUST run after compute_indicators. Was previously bundled into
        step_calendar_prep and run early, which silently produced zero rows
        every day (see ingest_sector_holiday_fixevents.step_compute_sector_strength).
        """
        from swing.ingestion.ingest_sector_holiday_fixevents import main_strength as fn; return fn()

    def step_fii_dii():
        from swing.ingestion.ingest_fii_dii import main as fn; return fn()

    def step_nse_events():
        from swing.ingestion.ingest_nse_events import main as fn; return fn()

    def step_signals():
        from swing.signals.generate_signals import main as fn; return fn()

    def step_history():
        from swing.history.append_history import main as fn; return fn()

    def step_post_trade():
        from ai.post_trade_analysis import main as fn; return fn()

    def step_ai_decision_engine():
        from ai.ai_decision_engine import main as fn; return fn()

    def step_alerts():
        from alerts.send_alerts import main as fn; return fn()

    # Phase 2 steps
    def step_market_news():
        from swing.ingestion.ingest_market_news import main as fn; return fn()

    def step_macro_indicators():
        from swing.ingestion.ingest_macro_indicators import main as fn; return fn()

    def step_asm_gsm():
        from swing.ingestion.ingest_asm_gsm import main as fn; return fn()

    def step_compute_indicators():
        from swing.compute.compute_indicators import main as fn; return fn()

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
    
    def step_signal_snapshot():
        from swing.signals.final_snapshot import main as fn; return fn()

    def step_position_reconcile():
        """
        Kite holdings -> open_positions. Runs EARLY because screen_stocks reads
        open_syms and compute_msl sets in_position from this table — a stale
        open_positions means the screener re-recommends stock already held and
        compute_msl computes entry logic for positions instead of holding logic.
        Reconcile only; exit management needs EOD prices and runs later.
        """
        from control.position_lifecycle import main as fn
        return fn(reconcile=True, manage=False)

    def step_position_manage():
        """
        Apply the exit policy at EOD. Runs after compute_indicators (needs
        closing prices as the fallback when the Kite session has lapsed) and
        before post_trade, so a position closed today feeds tonight's lessons.
        """
        from control.position_lifecycle import main as fn
        # alert=False: send_alerts renders these same actions inside the
        # digest's OPEN POSITIONS block, so alerting here too sent the
        # identical BOOK_PARTIAL twice in one run, worded differently.
        return fn(reconcile=False, manage=True, alert=False)

    def step_telegram_journal():
        """Drain Telegram entry/exit confirmations into open_positions."""
        from control.telegram_position_journal import main as fn; return fn()

    def step_quality_gate():
        """
        Input-data checks (C01-C04, C06, C20). FATAL by design: runs before
        signals so a data failure stops the pipeline instead of producing a
        recommendation off data we already know is bad.
        """
        from swing.compute.data_quality_monitor import main_input as fn; return fn()

    def step_quality_audit():
        """Output checks (C05, C07-C19). Advisory, runs after alerts."""
        from swing.compute.data_quality_monitor import main_output as fn; return fn()
    
    def step_compute_regime():
        from swing.compute.compute_regime import main as fn; return fn()
    
    def step_screen_stocks():
        from swing.signals.screen_stocks import main as fn; return fn()
    
    def step_compute_msl():
        from swing.compute.compute_msl import main as fn; return fn()

    def step_results_calendar():
        """
        Give the results calendar a memory.

        nifty_upcoming_events is forward-only: measured 04-Aug-2026 it held one
        day of past and two weeks of future, and dates that pass stop being
        there. The post-earnings drift engine asks "what reported N days ago",
        which that table structurally cannot answer.

        Runs after events ingestion and before screening, so today's screen sees
        a calendar that remembers. Non-fatal: a missed capture costs one day of
        PEAD history, an aborted pipeline costs a day of signals.
        """
        from swing.signals.engines_stage8 import capture_results_calendar
        return {"recorded": capture_results_calendar()}

    def step_resolve_outcomes():
        """
        Score every plan the system wrote, traded or not.

        THE SWING BOOK'S UNBIASED DENOMINATOR. The intraday daemon has resolved
        every detection it ever made — 595 of 595 on 04-Aug-2026. The swing side
        had the same seven outcome columns on signal_output_daily, written as
        NULL by final_snapshot, and **nothing had ever filled a single one**:
        0 resolved of 1,711. ai_decision_engine reads outcome_category and
        outcome_return_pct to tell the model how past signals fared, and had
        therefore always read NULL.

        Every prior the allocator is meant to rank on comes from this table. A
        prior built from executed trades inherits the old policy's selection and
        is applied to a region that policy never sampled, which is why the
        architecture calls full-field priors the single most important thing in
        the plan.

        Runs after the snapshot so today's plans exist, and before roll-off so
        it still has the price history it reads. Non-fatal: a scoring gap costs
        a day of learning, an aborted pipeline costs a day of signals.
        """
        from swing.signals.outcomes import main as fn
        return fn()

    def step_resolve_intraday_outcomes():
        """
        Score every intraday detection — on a SCHEDULE, not on a daemon exit.

        THE ASYMMETRY THIS CLOSES — 15-Aug-2026. The two steps around this one
        score the swing book and the allocator from the pipeline, on a clock.
        The intraday book had no equivalent: `outcomes.resolve_day` was reached
        only from `intraday/run.py`'s `finally` block, and only when that daemon
        held the lease. A day was therefore scored if and only if the daemon
        started, acquired the lease, and exited cleanly THAT day. Nothing ever
        scheduled it.

        Combined with `unresolved_days` excluding today — correct on its own
        terms, since today's setups are legitimately unresolved until the close
        — a session could not repair its own remainder, so the earliest repair
        was the NEXT trading day's daemon exit. On a Friday that lands after
        the Sunday weekly review has already consumed the hole. 14-Aug-2026 was
        a Friday: 2289 detections, 1000 scored, 1289 left, and the review that
        would have used them was 40 hours away with no trading day in between.

        `backfill` is idempotent and cheap — it resolves nothing when nothing
        is outstanding, and one symbol fetch per symbol when something is.
        Non-fatal, matching its siblings: a scoring gap costs a day of
        learning, an aborted pipeline costs a day of signals. But it ALERTS on
        anything it could not finish, because "could not reach the broker" and
        "there was nothing to do" both used to return zero.
        """
        from intraday import outcomes
        res = outcomes.backfill()
        if not res.get("complete", True):
            outcomes.alert_unscored()
        return res

    def step_score_allocator():
        """
        Score every allocation verdict — taken, deferred and declined.

        WITHOUT THIS THE PROMOTION GATE CANNOT MOVE. tools/allocator_report
        counts a disagreement only once it carries an outcome_r; migration 041
        created that column and nothing wrote it. The allocator could have
        shadowed for a year and the scorecard would have read zero scored
        disagreements the whole time — the same silent hole as the 1,711 plan
        outcomes that sat NULL.

        Runs after the plan resolver so both populations are scored in one pass,
        and before roll-off so it still has the price history it walks.
        Non-fatal: a scoring gap costs a day of promotion evidence, an aborted
        pipeline costs a day of signals.
        """
        from allocation.outcomes import main as fn
        return fn()

    def step_storage_rolloff():
        """
        Archive history out of stock_data_daily so the database never reaches
        the ceiling that would stop it accepting writes.

        WHY THIS IS THE LAST STEP, AND WHY IT IS NON-FATAL.
        It deletes rows the same pipeline just wrote indicators onto, so it can
        only run once everything that reads them is done. And it is maintenance:
        a roll-off that fails costs a night of headroom, while a roll-off that
        aborts the pipeline costs a day of signals. Failure here must never be
        the reason there are no plans tomorrow.

        WHY IT IS SAFE. archive_stock_data() is archive-then-delete in one
        transaction and is re-runnable, so a failure leaves the rows in
        stock_data_daily rather than nowhere. Migration 016 wrote it in
        Feb-2026 and nothing has ever called it; this is the call.

        It is a no-op until the table holds more than keep_days trading dates —
        99 on 04-Aug-2026 against a 250-day window — and reports that plainly
        instead of logging a success it did not earn.
        """
        from config import get_supabase, cfg_bool, cfg_int
        if not cfg_bool("storage_rolloff_enabled", True):
            logger.info("  roll-off disabled (storage_rolloff_enabled=false) — skipped")
            return {"skipped": True}

        keep = cfg_int("storage_rolloff_keep_days", 250)
        rows = get_supabase().rpc("archive_stock_data", {"keep_days": keep}).execute().data
        r    = (rows or [{}])[0]
        archived, deleted, cutoff = r.get("archived", 0), r.get("deleted", 0), r.get("cutoff")

        if cutoff is None:
            logger.info(f"  fewer than {keep} trading dates on hand — nothing to roll off yet")
        else:
            logger.info(f"  archived {archived} rows, deleted {deleted}, cutoff {cutoff} "
                        f"(keeping {keep} trading days)")

        # ── Ingestion staging ────────────────────────────────────────────────
        # raw_prices and chartink_raw_data are 33% of the database and 45% of
        # its growth, and every reader in the repository queries them one day
        # deep. Every field they feed is already persisted on stock_data_daily —
        # verified 04-Aug-2026 — and both sources are re-fetchable, so this is a
        # delete with no archive half. See migration 032.
        staging = {}
        if cfg_bool("storage_staging_rolloff_enabled", False):
            keep_s = cfg_int("storage_staging_keep_days", 120)
            try:
                for row in (get_supabase().rpc("rolloff_staging",
                                               {"keep_days": keep_s}).execute().data or []):
                    staging[row["table_name"]] = row["deleted"]
                    logger.info(f"  {row['table_name']}: deleted {row['deleted']} row(s) "
                                f"before {row['cutoff']}")
            except Exception as e:
                # Non-fatal within a non-fatal step: the archive above already
                # succeeded and must not be reported as failed because a second,
                # independent prune could not run.
                logger.warning(f"  staging roll-off skipped: {e}")

        return {"archived": archived, "deleted": deleted, "cutoff": cutoff,
                "keep_days": keep, "staging": staging}


    # ── Phase 0 steps ─────────────────────────────────────────────────────────
    steps_p0 = [
        ("01_calendar_prep",   step_calendar_prep,       True),
        ("02_global_cues",     step_global_cues_evening, False),
        ("03_fetch_chartink",  step_fetch_chartink,      True),
        ("04_ingest_bhavcopy", step_ingest_bhavcopy,     False),
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

    # ── Phase 2 — dependency-ordered sequence ─────────────────────────────────
    #
    # DEPENDENCY GRAPH (what actually reads what, verified against the code)
    #
    #   calendar_prep   → nse_holidays. EVERY step that resolves a trading day
    #                     reads it. Must be first. Also rolls event_calendar.
    #   fetch_chartink  → chartink_raw_data. The raw universe.
    #   ingest_bhavcopy → stock_data_daily.value_cr / delivery_pct / delivery_qty
    #   compute_ind     → stock_data_daily everything else (sector, market_cap,
    #                     rsi_*, adx, ret_*, rs_vs_nifty, above_sma50, atr_*)
    #   sector_strength → reads the columns compute_indicators writes.
    #                     ***MUST RUN AFTER compute_indicators***
    #   compute_regime  → reads sector_strength.breadth_sma50
    #   screen_stocks   → reads sector_strength.rank (all 9 engines gate on it)
    #   generate_signals→ reads sector_strength.rank, fii_dii_flow, event_calendar,
    #                     safety_lists, master_shortlist/msl_computed
    #
    # ══ WHAT CHANGED AND WHY (2026-07 audit) ══════════════════════════════════
    #
    # 1. sector_strength MOVED from step 06 → step 11 (after compute_indicators).
    #
    #    This was the single most damaging ordering bug in the pipeline. The old
    #    step 06 called fn_compute_sector_industry_strength, which aggregates
    #    stock_data_daily on `sector IS NOT NULL AND coalesce(market_cap,0) > 0`.
    #    At step 06 the only writer of today's rows is ingest_bhavcopy (step 05),
    #    which populates only value_cr/delivery_pct/delivery_qty — sector and
    #    market_cap are still NULL. The aggregate matched ZERO rows, the v1
    #    function returned void, and the caller logged success anyway.
    #
    #    Observed result: sector_strength was last written 2026-07-02 while
    #    stock_data_daily was current to 2026-07-24 — 22 days stale. Downstream:
    #      • generate_signals reads sector_strength with NO fallback → empty dict
    #        → CTL/SBS/TPO returned zero candidates every single day
    #        (in_rule_engine was False for 38/38 signals on 2026-07-24)
    #      • screen_stocks fell back to `.order(date desc).limit(50)`, mixing
    #        rows from three different dates
    #      • compute_regime read a frozen breadth (avg_sector_breadth was
    #        identically 59.5 for 10 consecutive days)
    #    It is now FATAL — stale sector ranks silently corrupt every signal.
    #
    # 2. quality_check SPLIT into a pre-signal GATE and a post-alert AUDIT.
    #    It previously ran last (step 23), after alerts had already been sent,
    #    so a bad-input day was reported only after you had been told what to buy.
    #
    # 3. compute_indicators / screen_stocks / compute_msl are now FATAL.
    #    Each is a hard input to signals. Letting them fail non-fatally meant the
    #    pipeline carried on and generated signals from the previous day's
    #    shortlist while logging a warning nobody reads.
    if phase >= 2:
        all_steps = [
            # ── Calendar first: everything resolves trading days against this ──
            ("01_calendar_prep",       step_calendar_prep,       True),
            # ── Position truth BEFORE selection: screen_stocks reads open_syms
            #    and compute_msl sets in_position from open_positions ──
            ("02_telegram_journal",    step_telegram_journal,    False),
            ("03_position_reconcile",  step_position_reconcile,  False),
            # ── Independent external feeds (no downstream hard dependency) ──
            ("04_market_news",         step_market_news,         False),
            ("05_macro_indicators",    step_macro_indicators,    False),
            ("06_global_cues",         step_global_cues_evening, False),
            # ── Raw universe ──
            ("07_fetch_chartink",      step_fetch_chartink,      True),
            ("08_ingest_bhavcopy",     step_ingest_bhavcopy,     True),   # delivery/value: IAD + institutional_score depend on it
            # ── Context feeds consumed by signals ──
            ("09_fii_dii",             step_fii_dii,             False),
            ("10_nse_events",          step_nse_events,          False),
            ("11_asm_gsm",             step_asm_gsm,             False),  # safety_lists ASM/FO_BAN
            # ── Derived stock data ──
            ("12_compute_indicators",  step_compute_indicators,  True),   # writes ~70 cols on stock_data_daily
            ("13_sector_strength",     step_sector_strength,     True),   # MOVED: needs step 12's output
            ("14_compute_regime",      step_compute_regime,      False),  # reads sector_strength breadth
            ("15_regime_predict",      step_regime_predict,      False),  # ML predicted_regime overlay (P2-D)
            # ── Exit policy on held positions (needs EOD closes from step 12) ──
            ("16_position_manage",     step_position_manage,     False),
            ("16b_results_calendar",   step_results_calendar,    False),  # PEAD needs a past, not a future
            # ── Selection + enrichment ──
            ("17_screen_stocks",       step_screen_stocks,       True),   # 9 engines → master_shortlist / msl_computed
            ("18_compute_msl",         step_compute_msl,         True),   # intelligence fields, entry zones, final_score
            # ── GATE: validate inputs before committing to a recommendation ──
            ("19_quality_gate",        step_quality_gate,        True),
            # ── Signal generation ──
            ("20_signals",             step_signals,             True),   # PRIME/BREAKOUT/REENTRY/STAGED/MOM_CONT
            ("21_history",             step_history,             False),  # msl_history + regime_history
            # ── Analysis and AI ──
            ("22_post_trade",          step_post_trade,          False),  # closed trades → lessons
            ("23_ai_market_intel",     step_ai_market_intel,     False),  # news synthesis
            ("24_ai_decision_engine",  step_ai_decision_engine,  False),  # tiering + conviction per signal
            # ── Output ──
            ("25_signal_snapshot",     step_signal_snapshot,     False),  # immutable signal_output_daily freeze
            ("26_alerts",              step_alerts,              False),  # Telegram digest
            ("27_quality_audit",       step_quality_audit,       False),  # output-side checks, advisory
            ("28_resolve_outcomes",    step_resolve_outcomes,    False),  # full-field priors: score every plan
            ("28a_resolve_intraday",   step_resolve_intraday_outcomes, False),  # intraday evidence: no longer daemon-only
            ("28b_score_allocator",    step_score_allocator,     False),  # promotion evidence: score every verdict
            ("29_storage_rolloff",     step_storage_rolloff,     False),  # LAST: archive history, never fatal
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
    sys.exit(0 if all(r["ok"] for r in results.values()) else 1)


if __name__ == "__main__":
    main()

#Get-ChildItem -Recurse -Filter "*.py" | Select-String "ctl_enabled|sbs_enabled|tpo_enabled|eap_enabled"#