"""
No undefined name reaches production silently again.

WHAT THIS CATCHES
------------------
Three real bugs were found in one afternoon (07-Aug-2026) by running
pyflakes over the whole backend, none of them found by reading code or by
seeing something fail — all three were swallowed by a broad `except
Exception` and never logged above DEBUG, or not logged at all:

  · intraday/engine.py::apply_live_quotes — `now` referenced, never assigned.
    Silently meant intraday_quote_mode had ZERO effect from the day it was
    turned on, three days of live sessions, discovered only via a live SQL
    trace showing the parity table it also should have written had zero rows.
  · swing/compute/compute_msl.py — `screener_rows`, a name with no producer
    anywhere in the file, referenced inside a "shadow mode" branch. Reachable
    via the code's own default AND a documented CLI flag; not triggered in
    production only because compute_msl_mode has read "full" since 11-Apr.
  · alerts/send_alerts.py — `datetime` (shadowed by a local `_dt` alias) and
    `IST` (never imported in that scope) referenced while building the "N
    days old" annotation on stale AI advisories — silently caught by a bare
    `except Exception: pass`, so the exact feature built to stop a stale
    advisory being read as current had never once fired.

Static analysis catches this WITHOUT needing to execute the crashing code
path — which is exactly why manual reading and even targeted tests kept
missing it: the branch has to actually run to be seen failing at runtime, and
a below-floor-probability branch (a retired mode, a rarely-hit combination of
switches) can sit broken for months. This is cheap (a few seconds, no DB, no
network) and belongs in the standing suite, not a one-off audit command.

SCOPE: undefined-name findings only (pyflakes' UndefinedName /
UndefinedLocal). Not unused imports, not redefinitions, not style — those are
real but are not the class of defect that has actually cost this project
money and silence; keeping the bar narrow keeps this check from becoming
noisy enough to ignore.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


class _Collector:
    """A pyflakes Reporter that keeps only undefined-name messages."""
    def __init__(self):
        self.undefined: list[str] = []
        self.errors: list[str] = []

    def flake(self, message) -> None:
        from pyflakes.messages import UndefinedName, UndefinedLocal
        if isinstance(message, (UndefinedName, UndefinedLocal)):
            self.undefined.append(str(message))

    def unexpectedError(self, filename, msg) -> None:
        self.errors.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text) -> None:
        self.errors.append(f"{filename}:{lineno}: {msg}")


def _scan() -> _Collector:
    import pyflakes.api
    collector = _Collector()
    # Exclude tests/ itself and any scratch/venv-style directories that might
    # be sitting alongside the real package on a given machine.
    targets = [
        str(p) for p in BACKEND.iterdir()
        if p.is_dir() and p.name not in ("tests", "__pycache__")
        and not p.name.startswith(".")
    ]
    pyflakes.api.checkRecursive(targets, collector)
    return collector


def test_no_undefined_names_anywhere_in_the_backend():
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        raise AssertionError(
            "pyflakes is not installed (see requirements.txt) — this check "
            "cannot run without it, and skipping silently is exactly the "
            "failure mode this module exists to prevent. pip install pyflakes")

    result = _scan()
    assert not result.undefined, (
        f"{len(result.undefined)} undefined-name reference(s) found — each "
        f"one is a guaranteed crash the first time its branch actually runs:\n  "
        + "\n  ".join(result.undefined))


# ── the 1000-row cap, caught at the source rather than in production ────────
#
# 15-Aug-2026. `intraday_setups` crossed 1000 rows PER SESSION on 12-Aug and
# every unpaged reader of it became silently wrong on the same day —
# `resolve_day` could only finish the first 1000 detections of a day,
# `unresolved_days` reported the cap as though it were a count, and
# `review_engines` judged every engine on 1000 of 8324 rows. Nothing raised,
# nothing warned, and the rows that came back were all genuine; only the absent
# ones carried the information.
#
# Fixing the six readers that existed does not stop the seventh being written.
# This does. A read on a table known to exceed the cap must page (via
# `config.fetch_all`, `.range()`), bound itself (`.limit()`, `.single()`,
# `count=`), or carry an explicit `paging-exempt:` marker stating why it is
# bounded in fact. The marker is deliberate friction: it makes an exemption a
# reviewed decision instead of an omission nobody noticed.

# Measured 15-Aug-2026 (`select count(*)`), not guessed:
#   stock_data_daily 55963 · chartink_raw_data 41496 · allocation_decisions
#   20873 · industry_strength 9382 · intraday_setups 8324 · master_shortlist
#   7212 · signal_log 4563 · sector_strength 2716 · signal_output_daily 2430
#   · lessons 1114
_LARGE_TABLES = {
    "stock_data_daily", "chartink_raw_data", "allocation_decisions",
    "industry_strength", "intraday_setups", "master_shortlist",
    "signal_log", "sector_strength", "signal_output_daily", "lessons",
}

# A single-day equality filter bounds a read only if that table's BUSIEST day
# fits under the cap. Measured the same way: the largest single day is 501 rows
# for stock_data_daily and chartink_raw_data, and at most 100 for the rest —
# except intraday_setups, whose 14-Aug session alone was 2289. So a day filter
# is sufficient evidence of boundedness everywhere but there.
_DAY_FILTER_IS_ENOUGH = _LARGE_TABLES - {"intraday_setups"}


def _unpaged_reads() -> tuple[list[str], int]:
    """Every unbounded read of a known-large table. Returns (violations, scanned)."""
    import re
    viol, scanned = [], 0
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or "db" in parts:
            continue
        # `swing/ingestion/ingest_sheets - Copy.py` is a Windows Explorer
        # duplicate that nothing imports — checked by grep across backend/,
        # .github/ and tradeos.cmd. Excluded rather than annotated, because
        # putting a considered paging exemption into dead code implies the code
        # is live. It should be deleted; that is the operator's call, not this
        # check's.
        if " - Copy" in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
                r'\.table\(\s*["\'](\w+)["\']\s*\)((?:.|\n){0,800}?)\.execute\(\)', text):
            table, chain = m.group(1), m.group(2)
            if table not in _LARGE_TABLES:
                continue
            if re.search(r'\.(insert|update|upsert|delete)\(', chain):
                continue
            scanned += 1
            if re.search(r'\.(limit|range|single|maybe_single)\(|count\s*=', chain):
                continue
            line = text[:m.start()].count("\n") + 1
            # A PAGED READ HAS NO .execute() OF ITS OWN — fetch_all supplies it.
            # Without this the regex runs past the end of the statement and
            # latches onto the NEXT .execute() in the file, so every read this
            # check successfully got fixed would keep being reported. A check
            # that cries wolf about work already done is one that gets muted.
            before = text[max(0, m.start() - 200):m.start()]
            if "fetch_all(" in before:
                continue
            # the marker may sit anywhere in the statement or just above it
            window = text[max(0, m.start() - 300):m.end()]
            if "paging-exempt:" in window:
                continue
            if table in _DAY_FILTER_IS_ENOUGH and re.search(
                    r'\.eq\(\s*["\'](date|trade_date)["\']', chain):
                continue
            rel = path.relative_to(BACKEND).as_posix()
            viol.append(f"{rel}:{line} reads {table} unpaged and unbounded")
    return viol, scanned


def test_no_unpaged_read_of_a_table_that_exceeds_the_row_cap():
    viol, scanned = _unpaged_reads()

    # A SCANNER THAT MATCHES NOTHING REPORTS NO VIOLATIONS. If the regex ever
    # stops matching the codebase's call style, this check would pass forever
    # while watching nothing — the exact shape of the five dead health checks
    # this project has already found. So assert it still sees the reads first.
    assert scanned > 40, (
        f"the scanner only matched {scanned} reads of large tables — it has "
        f"stopped recognising this codebase's query style, and a check that "
        f"cannot fail is not a check")

    assert not viol, (
        f"{len(viol)} read(s) of a table known to exceed PostgREST's 1000-row "
        f"cap are neither paged nor bounded. Each returns at most 1000 rows "
        f"with no error, no warning, and no way for the caller to tell. Use "
        f"config.fetch_all(), or add a `paging-exempt: <why>` comment if the "
        f"filter genuinely bounds it:\n  " + "\n  ".join(viol))


TESTS = [
    ("no undefined names anywhere in the backend",
     test_no_undefined_names_anywhere_in_the_backend),
    ("no unpaged read of a table that exceeds the row cap",
     test_no_unpaged_read_of_a_table_that_exceeds_the_row_cap),
]
