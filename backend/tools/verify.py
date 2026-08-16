"""
Every logic check this project has, offline, in one run.

    python -m tools.verify           all of it
    python -m tools.verify --module direction_spine
    python -m tools.verify --quiet   one line per module, failures in full

HOW THIS DIFFERS FROM tools/health.py
--------------------------------------
`health` asks the RUNNING SYSTEM questions — is the broker reachable, does the
schema have this column, is a daemon holding the lease, is the database near
its ceiling. It needs credentials and it tells you whether today is safe.

`verify` asks the LOGIC questions — does a short mirror a long, does the
allocator score both directions, is the long path unchanged. It needs nothing
but the source tree, runs in about a second, and tells you whether a CHANGE is
safe. Run it after editing; run `health` before trading.

WHY IT EXISTS
-------------
Every check in `tests/` was previously written into a scratch directory, run
once, and thrown away when the session ended. The next session that touched the
same code rewrote it — and in between there was no regression net at all. Two
defects shipped straight through that gap, both of the same shape: a function
whose signature defaults `direction` to LONG, called without it.

The suite is deliberately dependency-free — no pytest, no fixtures framework,
no database — because a check that needs setting up is a check that gets
skipped, and this project has already paid for that lesson in `health`'s own
docstring.

EXIT CODE
---------
0 clean, 1 something failed. Suitable for a pre-commit hook or CI.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

#: Ordered by what it costs to be wrong about, not alphabetically. The long
#: path guards the book that holds real money and the only track record this
#: system owns — if it fails, nothing below it matters.
MODULES = [
    ("static analysis",     "tests.test_static_analysis"),
    ("long path unchanged", "tests.test_long_path_unchanged"),
    ("direction spine",     "tests.test_direction_spine"),
    ("regime vocabulary",   "tests.test_regime_vocabulary"),
    ("allocator direction", "tests.test_allocator_direction"),
    ("allocator priors",    "tests.test_allocator_priors"),
    ("swing engine attribution", "tests.test_swing_engine_attribution"),
    ("hurdle percentile",   "tests.test_hurdle_percentile"),
    ("apply live quotes",   "tests.test_apply_live_quotes"),
    ("track excursion pnl", "tests.test_track_excursion"),
    ("intraday heartbeat",  "tests.test_intraday_heartbeat"),
    ("short engine",        "tests.test_short_engine"),
    ("book isolation",      "tests.test_book_isolation"),
    ("swing reservation",   "tests.test_swing_reservation"),
    ("capital split",       "tests.test_capital_split"),
    ("paper capacity",      "tests.test_paper_capacity"),
    ("swing rank collision", "tests.test_swing_rank_collision"),
    ("hurdle dedup",         "tests.test_hurdle_dedup"),
    ("ai tier weight review", "tests.test_ai_tier_weight_review"),
    ("swing hold days",      "tests.test_swing_hold_days"),
    ("swing family maturity review", "tests.test_swing_family_maturity_review"),
    ("quote parity verdicts",  "tests.test_quote_parity"),
    ("allocator edge floor",   "tests.test_edge_floor"),
    ("gated intraday priors",  "tests.test_gated_priors"),
    ("setup dedup rehydration", "tests.test_setup_rehydration"),
    ("break confirmation",      "tests.test_break_confirmation"),
    ("close invalidation",      "tests.test_close_invalidation"),
    ("ai prompt size",          "tests.test_ai_prompt_size"),
    ("replay candidate dedup", "tests.test_replay_dedup"),
    ("floor exploration",      "tests.test_floor_exploration"),
    ("exit audit sanity",      "tests.test_exit_audit_sanity"),
    ("intraday giveback calibrated", "tests.test_intraday_giveback"),
    ("exit ladder replay",     "tests.test_exit_ladder_replay"),
    ("taken reconciliation",   "tests.test_taken_reconciliation"),
    ("paper entry verdicts",   "tests.test_paper_entry_verdicts"),
    ("benchmark snapshot",     "tests.test_benchmark"),
    ("screener weight",        "tests.test_screener_weight"),
    ("intraday universe live rerank", "tests.test_universe_rerank"),
    ("vwap reclaim refinement", "tests.test_vwap_reclaim"),
    ("bars built from ticks", "tests.test_bar_builder"),
    ("rr weight reduced on tercile evidence", "tests.test_rr_weight"),
    ("entry reserve", "tests.test_entry_reserve"),
    ("exit rules live vwap", "tests.test_exit_rules_live_vwap"),
    ("intraday short runway tighten", "tests.test_intraday_short_runway_tighten"),
    ("runway refusal summary", "tests.test_runway_refusal_summary"),
    ("runway requeue", "tests.test_runway_requeue"),
    ("gap down bounce engine", "tests.test_gap_down_bounce"),
    ("ingest asm gsm dedup and isolation", "tests.test_ingest_asm_gsm"),
    ("data quality resolved alert", "tests.test_data_quality_resolved_alert"),
    ("partial book quantity sync", "tests.test_partial_book_quantity_sync"),
    ("stale token alerting", "tests.test_token_alerting"),
    ("regime-aware engine fit (shipped inert)", "tests.test_regime_fit"),
    ("brain_proposals walk-forward backtest", "tests.test_proposal_backtest"),
    ("validate_config paper capacity", "tests.test_validate_config_paper_capacity"),
    ("expectancy ledger scores shorts", "tests.test_expectancy_ledger_shorts"),
<<<<<<< HEAD
    ("preflight host and lease guards", "tests.test_preflight_host_and_lease"),
    ("planner regime symmetry and cost floor (shipped inert)",
     "tests.test_planner_regime_and_cost"),
    ("outcome resolution gap (row cap + loud alert)",
     "tests.test_outcome_resolution_gap"),
=======
    ("single daemon startup lock", "tests.test_daemon_lock"),
>>>>>>> fix/single-daemon-lease
]


@dataclass
class Result:
    module: str
    name: str
    ok: bool
    detail: str = ""


def _run_one(fn) -> tuple[bool, str]:
    """A failure is a result, never a crash — one broken test must not hide the rest."""
    try:
        fn()
        return True, ""
    except AssertionError as e:
        return False, str(e) or "assertion failed with no message"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"


def run(only: str | None = None, quiet: bool = False) -> int:
    logger.info("=" * 74)
    logger.info("TradeOS verify — offline logic checks (no database, no broker)")
    logger.info("=" * 74)

    results: list[Result] = []
    for label, dotted in MODULES:
        if only and only not in dotted:
            continue
        try:
            mod = importlib.import_module(dotted)
        except Exception as e:
            results.append(Result(label, "import", False, f"{type(e).__name__}: {e}"))
            logger.error(f"  ✗  {label}: could not import — {e}")
            continue

        tests = getattr(mod, "TESTS", None)
        if not tests:
            # A module with no TESTS list is not "passing", it is not running.
            results.append(Result(label, "TESTS list", False,
                                  "module exposes no TESTS — nothing ran"))
            logger.error(f"  ✗  {label}: exposes no TESTS list, so nothing ran")
            continue

        failed_here = []
        for name, fn in tests:
            ok, detail = _run_one(fn)
            results.append(Result(label, name, ok, detail))
            if not ok:
                failed_here.append((name, detail))

        if failed_here:
            logger.error(f"  ✗  {label}  ({len(failed_here)}/{len(tests)} failed)")
            for name, detail in failed_here:
                logger.error(f"       {name}")
                for line in detail.strip().splitlines()[:6]:
                    logger.error(f"         {line}")
        elif not quiet:
            logger.success(f"  ✓  {label}  ({len(tests)} checks)")
        else:
            logger.info(f"  ok {label} ({len(tests)})")

    if not results:
        logger.error(f"  no modules matched --module {only!r}")
        return 1

    bad = [r for r in results if not r.ok]
    logger.info("")
    logger.info("─" * 74)
    if bad:
        logger.error(f"  {len(bad)} of {len(results)} checks FAILED.")
        logger.error("  Fix these before committing — every one of them was written "
                     "because the thing it checks has already broken once.")
        return 1
    logger.success(f"  all {len(results)} checks passed across "
                   f"{len({r.module for r in results})} modules")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline logic checks for TradeOS")
    ap.add_argument("--module", help="run only modules whose name contains this")
    ap.add_argument("--quiet", action="store_true",
                    help="one line per module; failures still print in full")
    a = ap.parse_args()
    return run(only=a.module, quiet=a.quiet)


if __name__ == "__main__":
    sys.exit(main())
