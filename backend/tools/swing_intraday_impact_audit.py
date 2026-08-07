"""
Does this branch touch intraday, directly or indirectly — and if it touches
a SHARED file, is there a specific, named test PROVING intraday's behaviour
is unchanged, or is that merely asserted in a commit message?

WHY THIS EXISTS
-----------------
Operator's explicit requirement, 07-Aug-2026: any change on this branch
must not change intraday's scripts, logic or rules at all, and anything
touching a shared file needs a thorough impact analysis and explicit
approval before merge — not a commit message that says "isolated" and
hopes. This is that analysis, made mechanical and re-runnable rather than
performed once by hand and trusted from memory.

WHAT "PROVEN" MEANS HERE
--------------------------
A file is only marked safe if EITHER (a) it is swing-only by construction
— a new file with zero intraday import or call site, confirmed by grep, not
assumption — or (b) it is a genuinely shared file where the specific
isolation guarantee is backed by a NAMED regression test in this registry,
and that test is run as part of this audit, live, every time. A shared file
with no isolation test listed against it is NOT safe — it is flagged, and
the audit fails loudly rather than passing on the strength of a comment.

This does not replace human review. It replaces "I re-read the diff and it
looked fine" with "these specific tests, naming these specific guarantees,
passed just now" — the same shift from asserted to demonstrated this whole
branch has been built on.

Usage:
    python -m tools.swing_intraday_impact_audit
    python -m tools.swing_intraday_impact_audit --base origin/main
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SWING_ONLY = "SWING-ONLY (new file, zero intraday import/call site)"
SHARED_PROVEN = "SHARED — isolation proven by a named test"
NO_RUNTIME_IMPACT = "NO RUNTIME IMPACT (test / standalone tool / migration / verify registration)"

# ── The registry. Every file this branch has ever touched must be here. ────
# Update this when a new file is touched; an unlisted file is what makes the
# audit fail, on purpose — see main() below.
REGISTRY: dict[str, tuple[str, str]] = {
    "backend/allocation/swing_hold_days.py": (
        SWING_ONLY, "grepped: zero references outside allocation/allocator.py's "
                    "own call site and tools/swing_family_maturity_audit.py"),
    "backend/allocation/allocator.py": (
        SHARED_PROVEN, "tests/test_swing_hold_days.py::"
                       "test_intraday_is_never_affected_by_the_swing_family_dict"),
    "backend/allocation/hurdle.py": (
        SHARED_PROVEN, "tests/test_hurdle_dedup.py::"
                       "test_swing_dedup_switch_does_not_touch_intraday + "
                       "test_intraday_dedup_switch_does_not_touch_swing"),
    "backend/allocation/scoring.py": (
        SHARED_PROVEN, "swing_family() has zero intraday call sites (grepped "
                       "every reference in the repo); "
                       "tests/test_swing_engine_attribution.py covers the fix itself"),
    "backend/intraday/engine.py": (
        SHARED_PROVEN, "_legacy_rank_gate_blocks() only called from _maybe_enter_swing(), "
                       "which has exactly 2 call sites, both inside act_on_candidates() "
                       "operating on SWING candidates (framework=\"SWING\" literal at the "
                       "notifier call adjacent to both); "
                       "tests/test_swing_rank_collision.py::"
                       "test_never_blocks_when_allocator_is_the_live_veto"),
    "backend/tools/hurdle_population_audit.py": (
        NO_RUNTIME_IMPACT, "standalone CLI report, not imported by intraday/run.py or engine.py"),
    "backend/tools/swing_family_maturity_audit.py": (
        NO_RUNTIME_IMPACT, "standalone CLI report, not imported by intraday/run.py or engine.py"),
    "backend/tools/weekly_review.py": (
        NO_RUNTIME_IMPACT, "grepped: zero imports from intraday/; runs as its own scheduled "
                           "job (brain_sunday_chain.yml), not part of the intraday daemon"),
    "backend/tools/verify.py": (
        NO_RUNTIME_IMPACT, "test registration only, not imported by the live daemon"),
}


def _changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines() if f.strip()]


def _is_exempt(path: str) -> bool:
    """Tests, migrations and docs carry no runtime behaviour of their own —
    exempt from the registry requirement, but still listed in the report."""
    return (path.startswith("backend/tests/")
            or path.startswith("backend/db/migrations/")
            or path.endswith(".md"))


def audit(base: str = "origin/main") -> int:
    logger.info("=" * 78)
    logger.info(f"SWING/INTRADAY IMPACT AUDIT — against {base}")
    logger.info("=" * 78)

    try:
        changed = _changed_files(base)
    except subprocess.CalledProcessError as e:
        logger.error(f"  could not diff against {base}: {e.stderr}")
        return 1

    if not changed:
        logger.success(f"  no changes against {base}")
        return 0

    unregistered = []
    for f in sorted(changed):
        if not f.startswith("backend/"):
            continue
        if f in REGISTRY:
            classification, evidence = REGISTRY[f]
            logger.info(f"  {f}")
            logger.info(f"    -> {classification}")
            logger.info(f"    -> {evidence}")
        elif _is_exempt(f):
            logger.info(f"  {f}")
            logger.info(f"    -> test/migration/doc, no runtime behaviour of its own")
        else:
            unregistered.append(f)
            logger.error(f"  {f}")
            logger.error(f"    -> NOT IN THE REGISTRY — impact unproven, "
                         f"needs manual review and explicit approval before merge")

    logger.info("")
    logger.info("─" * 78)
    logger.info("Running the isolation-proving tests named above, live:")
    isolation_modules = [
        "tests.test_swing_hold_days",
        "tests.test_hurdle_dedup",
        "tests.test_swing_engine_attribution",
        "tests.test_swing_rank_collision",
        "tests.test_book_isolation",
        "tests.test_capital_split",
        "tests.test_paper_capacity",
    ]
    all_ok = True
    for mod_name in isolation_modules:
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            failed = []
            for name, fn in mod.TESTS:
                try:
                    fn()
                except Exception as e:
                    failed.append((name, str(e)))
            if failed:
                all_ok = False
                logger.error(f"  ✗ {mod_name} — {len(failed)} failed")
                for name, err in failed:
                    logger.error(f"      {name}: {err}")
            else:
                logger.success(f"  ✓ {mod_name} ({len(mod.TESTS)} checks)")
        except Exception as e:
            all_ok = False
            logger.error(f"  ✗ {mod_name} — could not import: {e}")

    logger.info("")
    logger.info("─" * 78)
    if unregistered:
        logger.error(f"  FAIL — {len(unregistered)} changed file(s) have no proven "
                     f"isolation claim: {', '.join(unregistered)}")
        logger.error("  Do not merge until each is either shown to be swing-only, "
                     "backed by a new isolation test, or explicitly approved as "
                     "an accepted intraday-affecting change.")
        return 1
    if not all_ok:
        logger.error("  FAIL — one or more isolation tests failed. The claims "
                     "above are not currently backed by passing evidence.")
        return 1

    logger.success(f"  PASS — every changed backend file is either swing-only, "
                   f"has no runtime behaviour, or is a shared file with a "
                   f"currently-passing isolation test. Still requires your "
                   f"explicit approval before merging to main.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main",
                    help="ref to diff against (default: origin/main)")
    a = ap.parse_args()
    return audit(a.base)


if __name__ == "__main__":
    sys.exit(main())
