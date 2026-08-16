"""
The holdout runner. It refuses to start more often than it agrees to.

**IF THE HOLDOUT DISAGREES WITH VALIDATION, THE HOLDOUT WINS AND THE STRATEGY IS
NOT CONFIRMED. THERE IS NO RE-TUNING.** REPLAY_DESIGN §8 puts that sentence in
this docstring on purpose: the rule has to live where the person about to break
it is already looking.

A holdout is one look. Its entire value is that nothing was adjusted after
seeing it, and that value is destroyed silently — by a re-run "to check
something", by an uncommitted tweak to a threshold, by scoring it twice and
keeping the better number. None of those leave a mark. So the guarantees are
mechanical:

    R1  the working tree is clean
    R2  the frozen parameter file resolves to a COMMITTED git object,
        byte-identical to what is on disk
    R3  no holdout result already exists for that parameter SHA

R3 is the one that matters most and it is the cheapest to get wrong. It is
enforced by a FILE THAT ALREADY EXISTS — `results/holdout_<sha12>.json` — rather
than by a flag or a counter, because a file is the only kind of state that
survives the process that would like to ignore it.

Every refusal here has a test that demonstrates it REFUSING
(`tests/test_replay_harness.py`). Five checks in this project have been found
reporting green while the thing they watched was broken; a gate nobody has
watched block is not a gate.

    python -m tools.replay.holdout --label frozen --dry-run
    python -m tools.replay.holdout --label frozen
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import IST
from tools.replay import freeze as F

RESULTS_DIR = Path(__file__).resolve().parent / "results"
VALIDATION_LOG = F.PARAMS_DIR / "validation_log.jsonl"

# REPLAY_DESIGN §8, measured window. Stated here so a run cannot quietly widen
# it: the holdout is June and only June.
HOLDOUT_FROM, HOLDOUT_TO = "2026-06-01", "2026-06-30"


class Refused(RuntimeError):
    """A precondition failed. The run does not start."""


@dataclass
class Preflight:
    ok: bool
    refusals: list[str]
    params_sha: str = ""
    result_path: Path | None = None


def result_path_for(sha: str) -> Path:
    """The filename embeds the parameter SHA, so a second parameter set is
    visibly a different artefact and cannot overwrite the first."""
    return RESULTS_DIR / f"holdout_{sha[:12]}.json"


def _committed_blob_matches(rel_path: Path) -> tuple[bool, str]:
    """
    Is the file on disk byte-identical to the one committed at HEAD?

    Three separate failures, reported apart: outside the repository, not
    committed at all, and committed but since edited. The last is the dangerous
    one — the file LOOKS tracked, `git log` shows it, and its contents are
    whatever someone last saved.

    The first case RAISED rather than refusing until the R3 test drove a temp
    path through it: `Path.relative_to` throws on a path outside the tree, and a
    precondition that crashes is not a precondition — it is a traceback in the
    place a refusal was supposed to be.
    """
    try:
        rel = str(rel_path.relative_to(F.REPO)).replace("\\", "/")
    except ValueError:
        return False, f"{rel_path} is outside the repository at {F.REPO}"
    try:
        committed = F._git("rev-parse", f"HEAD:{rel}")
    except RuntimeError:
        return False, f"{rel} is not committed at HEAD"
    try:
        on_disk = F._git("hash-object", str(rel_path))
    except RuntimeError as e:
        return False, f"{rel} could not be hashed: {e}"
    if committed != on_disk:
        return False, (f"{rel} differs from its committed version "
                       f"(HEAD {committed[:12]}, disk {on_disk[:12]})")
    return True, ""


def preflight(label: str = "frozen") -> Preflight:
    """
    Every reason not to start, collected rather than short-circuited.

    Collected on purpose: stopping at the first refusal makes fixing them a
    sequence of three runs, and the third one is where somebody gets impatient.
    """
    refusals: list[str] = []

    # R1 — a dirty tree means the code SHAs in the frozen file describe HEAD
    # while something else is on disk, so the run cannot be identified at all.
    clean, dirt = F.tree_is_clean()
    if not clean:
        n = len(dirt.splitlines())
        first = "; ".join(dirt.splitlines()[:3])
        refusals.append(f"R1 working tree is dirty ({n} path(s)): {first}")

    # R2 — the parameters must be a committed object, and the same one.
    p = F.path_for(label)
    sha = ""
    if not p.exists():
        refusals.append(f"R2 no frozen parameters at {p}")
    else:
        ok, why = _committed_blob_matches(p)
        if not ok:
            refusals.append(f"R2 {why}")
        try:
            sha = F.load(label).sha
        except (ValueError, FileNotFoundError) as e:
            refusals.append(f"R2 {e}")

    # R3 — one look. Enforced by a file that already exists.
    rp = result_path_for(sha) if sha else None
    if rp is not None and rp.exists():
        try:
            prior = json.loads(rp.read_text(encoding="utf-8"))
            when = prior.get("ran_at", "?")
        except Exception:
            when = "?"
        refusals.append(
            f"R3 a holdout result already exists for params {sha[:12]} "
            f"({rp.name}, run {when}) — the holdout is ONE look, and this "
            f"would be the second")

    return Preflight(ok=not refusals, refusals=refusals,
                     params_sha=sha, result_path=rp)


def log_validation_look(label: str, note: str = "") -> None:
    """
    Append-only count of validation looks. `frozen.json` is the winner; this is
    the record of how many candidates it beat, so "let me just check one more
    variant" is visible afterwards rather than only remembered.
    """
    F.PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    with VALIDATION_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(IST).isoformat(),
                             "label": label, "note": note}) + "\n")


def validation_looks() -> int:
    if not VALIDATION_LOG.exists():
        return 0
    return sum(1 for line in VALIDATION_LOG.read_text(encoding="utf-8").splitlines()
               if line.strip())


def run(label: str = "frozen", dry_run: bool = False) -> int:
    """
    Score the holdout window under frozen parameters. Once.

    `--dry-run` runs the preflight and stops. It is how the refusals are meant
    to be inspected; it never writes a result file, so it cannot consume the one
    look it is checking for.
    """
    pre = preflight(label)
    print()
    print("=" * 74)
    print(f"HOLDOUT PREFLIGHT — window {HOLDOUT_FROM} .. {HOLDOUT_TO}")
    print("=" * 74)
    print(f"  params label        : {label}")
    print(f"  params sha          : {pre.params_sha[:12] or '(none)'}")
    print(f"  validation looks    : {validation_looks()}")
    print(f"  result would be     : "
          f"{pre.result_path.name if pre.result_path else '(unknown)'}")
    if pre.refusals:
        print(f"\n  REFUSED — {len(pre.refusals)} precondition(s) failed:")
        for r in pre.refusals:
            print(f"    [X] {r}")
        print("\n  The holdout did NOT run. None of these are overridable by a "
              "flag;\n  each one has to be fixed in the world, not in the "
              "invocation.")
        print("=" * 74)
        return 1

    print("\n  all preconditions met")
    if dry_run:
        print("  --dry-run: stopping before the run, no result written")
        print("=" * 74)
        return 0

    # The scoring pass itself is NOT implemented in this stage. Saying so is the
    # point: this stage was told to build the freeze and the refusals, and a
    # runner that silently produced a number from an unverified harness would
    # be the exact failure the verification gate exists to prevent. The harness
    # has not cleared that gate (79.7% against 85%), so there is nothing
    # legitimate for this branch to compute yet.
    print("\n  BLOCKED: the detection harness has not cleared its own "
          "verification\n  gate, so no window may be scored. See "
          "docs/FINDINGS.md, replay stage.")
    print("=" * 74)
    return 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="frozen")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run(args.label, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
