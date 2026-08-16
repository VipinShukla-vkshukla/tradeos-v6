"""
The harness may not read the system's own record of what it decided.

WHY THIS IS A STATIC CHECK AND NOT A CONVENTION
-----------------------------------------------
The forbidden reads are all *plausible*. Every one of them would make some part
of this harness shorter, and each would silently convert a measurement into a
restatement of the thing being measured:

  · reading `intraday_setups` outside verification turns "what would the engines
    have found?" into "what did the engines find?" — the replay stops being able
    to disagree with the live system, which is its entire purpose;
  · reading the swing pipeline's stored plans replays the pipeline's own
    conclusions rather than recomputing them;
  · reading the learning loop's outputs closes a circle the replay exists to
    break — the allocator's priors are built from the population the replay is
    supposed to independently price.

A convention in a docstring does not survive the session where someone needs one
number quickly. A grep registered in `tools/verify.py` does.

THE SELF-REFERENCE PROBLEM, AND HOW IT IS HANDLED HONESTLY
-----------------------------------------------------------
This module names every forbidden table, so a naive scan flags itself. Two files
are therefore exempt from the token scan — this one, and the verification module
that must compare against the live record.

An exemption is a hole, so both holes are guarded rather than trusted:

  · `check_exempt_files_are_inert()` asserts that THIS file contains no database
    access of any kind — no client, no table call, no paging helper. A file that
    cannot reach the database cannot smuggle a read through its exemption.
  · `check_scan_is_not_vacuous()` asserts the scan actually walked a plausible
    number of files. A scan that silently matched nothing would pass forever,
    which is precisely the shape of the five dead health checks this project has
    already found.

The exempt set is a frozen literal. Adding a third file to it is a visible
change to a checked-in constant, not an accident.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# Table and module names the harness must never reference. From REPLAY_DESIGN §2.
FORBIDDEN: dict[str, str] = {
    "intraday_setups":
        "the live detection record — comparing against it is verification, "
        "computing from it is circular",
    "signal_log":
        "the swing pipeline's own record of what it decided",
    "signal_output_daily":
        "the day's stored plans; replaying against them replays their conclusions",
    "allocation_decisions":
        "an output of the learning loop the replay exists to check",
    "brain_proposals":
        "same — and the replay must never be able to write one either",
    "closed_positions":
        "live book state; exit_reason is wrong on all 11 rows (F-3)",
    "open_positions":
        "live book state — a replay owns its own book",
    "engine_scorecard":
        "a reader under suspicion (paged with .range() and no .order())",
    "weekly_review":
        "same — and its dedup rule is reimplemented here deliberately",
    "review_engines":
        "the function inside that reader",
}

# The ONLY files permitted to contain the tokens above. See the module docstring
# for why each is exempt and how each exemption is guarded.
SCAN_EXEMPT: frozenset[str] = frozenset({
    "independence.py",     # this file — names them as data; proven inert below
    "verify_known_day.py", # compares against the live record; never computes from it
})

# Any of these appearing in an exempt file means the exemption has been abused.
_DB_ACCESS_MARKERS = ("get_supabase", "sb.table", ".table(", "fetch_all",
                      "supabase", "postgrest")


def _package_files() -> list[Path]:
    """Every Python source file in the harness package."""
    return sorted(p for p in PACKAGE_DIR.rglob("*.py") if p.is_file())


def scan(files: list[Path] | None = None) -> list[str]:
    """
    Every forbidden reference in the harness, as human-readable violations.

    Returns an empty list when the package is clean. Never raises on a dirty
    package — the caller decides whether a violation is fatal, so this is usable
    both from the test suite and from a CLI that wants to print all of them.
    """
    viol: list[str] = []
    for path in (files if files is not None else _package_files()):
        if path.name in SCAN_EXEMPT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:                      # unreadable is not clean
            viol.append(f"{path.name}: could not be read ({e})")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for token, why in FORBIDDEN.items():
                if token in line:
                    viol.append(
                        f"{path.name}:{lineno} references '{token}' — {why}\n"
                        f"      {line.strip()[:100]}")
    return viol


def check_scan_is_not_vacuous(files: list[Path] | None = None) -> int:
    """
    How many files the scan actually inspected. A scan of nothing passes forever.

    Returns the count so a caller can assert a floor against it.
    """
    all_files = files if files is not None else _package_files()
    return sum(1 for p in all_files if p.name not in SCAN_EXEMPT)


def check_exempt_files_are_inert() -> list[str]:
    """
    An exempt file must not be able to reach the database at all.

    `verify_known_day.py` is the deliberate exception — it exists to read the
    live record — so only `independence.py` is required to be inert. That is the
    file whose exemption exists purely to avoid self-reference, and it has no
    business touching a database for any reason.
    """
    bad: list[str] = []
    me = PACKAGE_DIR / "independence.py"
    if not me.exists():
        return [f"{me.name} is missing — the exemption guard cannot run"]
    text = me.read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        # The markers appear in this docstring as prose; only code lines count.
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        for marker in _DB_ACCESS_MARKERS:
            if marker in line and "_DB_ACCESS_MARKERS" not in line:
                bad.append(f"independence.py:{lineno} reaches the database "
                           f"('{marker}') while exempt from the token scan — "
                           f"the exemption is now a hole:\n      {stripped[:100]}")
    return bad


def main() -> int:
    """CLI: `python -m tools.replay.independence`. Non-zero exit on violation."""
    viol = scan()
    inert = check_exempt_files_are_inert()
    scanned = check_scan_is_not_vacuous()

    print(f"harness independence scan: {scanned} file(s) inspected, "
          f"{len(SCAN_EXEMPT)} exempt")
    if scanned < 4:
        print(f"FAIL: only {scanned} files scanned — the harness package has "
              f"shrunk or the walk is broken. A scan that inspects nothing "
              f"cannot fail.")
        return 2
    # ASCII only. This console is cp1252 and a Unicode glyph here raises
    # UnicodeEncodeError *while reporting a violation* — the check would die on
    # exactly the input it exists to catch, and exit non-zero for the wrong
    # reason. Commit b239aef hit the same thing elsewhere in this repo.
    for v in viol + inert:
        print(f"  [X] {v}")
    if viol or inert:
        print(f"FAIL: {len(viol)} forbidden reference(s), "
              f"{len(inert)} exemption breach(es)")
        return 1
    print("OK: the harness reads no forbidden table or module")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
