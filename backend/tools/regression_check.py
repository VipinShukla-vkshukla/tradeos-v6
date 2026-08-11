"""
Run every backtest this project has, in one command, after every change.

    python -m tools.regression_check
    python -m tools.regression_check --days 15

WHY THIS EXISTS
---------------
10-Aug-2026, the operator asked directly: "after every change we should run
this and compare if the outcome is as per our expectations... not sure if we
are doing it already." The honest answer at the time was no — the allocator
fix had `tools.allocator_replay`, but the exit-ladder fix (the give-back
guard, migration 059) shipped with no equivalent, and there was no single
command tying either to `tools.verify`. Three separate manual steps is how a
step quietly stops being run.

WHAT THIS RUNS, IN ORDER, AND WHY THAT ORDER
----------------------------------------------
  1. tools.verify           offline logic — no database. If this fails,
                             nothing below it is trustworthy, because the
                             replay tools import the same code being changed.
  2. tools.allocator_replay entry-side backtest — real detections, real
                             resolved outcomes, no path-dependency. High
                             confidence.
  3. tools.exit_ladder_replay exit-side ESTIMATE — summary excursion data
                             only, no bar-by-bar path. Read as a ceiling, not
                             a forecast; see that tool's own docstring.

Steps 2 and 3 need database credentials and are SKIPPED, not failed, when
those are not configured — this command must be safe to run from a change
that touches no trading logic at all, and a missing credential is not the
same claim as a regression.

WHAT THIS DOES NOT DO
----------------------
It does not touch `system_config`, place an order, or write anywhere. It does
not replace `tools.health`, which asks whether TODAY is safe to trade — this
asks whether a CHANGE is, using history. Run health separately before trading.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# WINDOWS CONSOLE ENCODING — 11-Aug-2026, this tool's own responsibility,
# not inherited for free. config.py carries the identical line (with the
# identical comment) because every OTHER tool imports config for its actual
# values and picks up the fix as a side effect. This script needs none of
# config's values — it only shells out to other tools via subprocess — so
# it never imported config and never got the fix: print(out) below crashed
# with UnicodeEncodeError on the rupee sign the FIRST TIME captured output
# from a child process (which DOES import config, and prints ₹ safely on
# its own stdout) got relayed through this one's un-reconfigured stdout.
# Explicit here rather than `import config` purely for the side effect,
# which would make this fix invisible to the next person reading this file.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp1252 can't print ₹/✓/—

from loguru import logger


def _run(label: str, args: list[str], db_dependent: bool) -> tuple[bool, bool]:
    """Returns (ran, ok). ran=False means skipped (e.g. no DB creds), not failed.

    `db_dependent` decides whether "no database credentials" is read as a skip
    at all. `config.py` prints its "SUPABASE_URL and SUPABASE_SERVICE_KEY must
    be set" WARNING on every import, as a side effect, whether or not the
    running tool ever calls get_supabase() — tools.verify does not need a
    database and still triggers that line. Text-sniffing for it without this
    flag marked verify "skipped" even on a session where it had just run 171
    checks clean, which is worse than not detecting a real skip: it hides a
    result that actually happened.
    """
    logger.info("")
    logger.info("=" * 74)
    logger.info(f"  {label}")
    logger.info("=" * 74)
    # encoding="utf-8" IS NOT OPTIONAL ON WINDOWS — 11-Aug-2026. text=True
    # alone decodes the child process's stdout/stderr using
    # locale.getpreferredencoding(), which on Windows is a codepage
    # (cp1252 here), not UTF-8. Every tool this wraps prints through
    # loguru, and loguru's own box-drawing rules (=, —) plus the rupee
    # sign and check/cross marks this codebase uses everywhere include
    # bytes cp1252 has no mapping for — confirmed: byte 0x90 crashed the
    # decode. That crash happens inside subprocess's OWN internal reader
    # thread, not in a place a try/except here could catch, and it leaves
    # proc.stdout/proc.stderr as None rather than raising cleanly — so the
    # actual failure surfaced two frames later as `None + str`, which reads
    # nothing like an encoding problem. errors="replace" is the second half:
    # even UTF-8 can meet a genuinely undecodable byte from a dependency's
    # own output, and this tool's job is reporting pass/fail, not being a
    # byte-perfect terminal — a replacement character beats a crash that
    # discards the result of everything already run.
    proc = subprocess.run([sys.executable, "-m", *args],
                          cwd=str(Path(__file__).parent.parent),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out)
    if db_dependent and proc.returncode != 0 and \
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set" in out:
        logger.warning(f"  {label}: SKIPPED — no database credentials in this environment")
        return False, True
    return True, proc.returncode == 0


def main(days: int) -> int:
    results = []

    ran, ok = _run("1/3 — tools.verify (offline logic)", ["tools.verify"],
                   db_dependent=False)
    results.append(("verify", ran, ok))
    if not ok:
        logger.error("  verify failed — stopping here. The replay tools import the "
                     "same code; their output cannot be trusted until this is green.")
        return 1

    ran, ok = _run(f"2/3 — tools.allocator_replay --days {days} (entry-side, high confidence)",
                   ["tools.allocator_replay", "--days", str(days)], db_dependent=True)
    results.append(("allocator_replay", ran, ok))

    ran, ok = _run(f"3/3 — tools.exit_ladder_replay --days {days} (exit-side, ESTIMATE)",
                   ["tools.exit_ladder_replay", "--days", str(days)], db_dependent=True)
    results.append(("exit_ladder_replay", ran, ok))

    logger.info("")
    logger.info("=" * 74)
    logger.info("  SUMMARY")
    logger.info("=" * 74)
    any_failed = False
    for name, ran, ok in results:
        if not ran:
            logger.info(f"  {name:<22} skipped (no DB)")
        elif ok:
            logger.success(f"  {name:<22} ran clean")
        else:
            logger.error(f"  {name:<22} FAILED — read the output above")
            any_failed = True

    if any_failed:
        logger.error("  At least one check failed. Do not merge on this state.")
        return 1
    logger.success("  All available checks ran clean. Read the two replay "
                   "reports above before deciding to merge — a clean run "
                   "means the tools executed without error, not that the "
                   "numbers are good; that judgement is still yours.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run verify + both replay backtests in sequence")
    ap.add_argument("--days", type=int, default=10)
    sys.exit(main(ap.parse_args().days))
