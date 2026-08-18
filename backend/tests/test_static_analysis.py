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
    #
    # `intraday_quote_parity` added 15-Aug-2026: 178,545 rows, by a wide margin the
    # largest table in this schema and absent from the list above, which was built
    # from the tables the previous session happened to be looking at. A "known
    # large tables" list is only as good as the census behind it.
    "intraday_quote_parity",
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


# ── fetch_all's sort key must exist on the table it sorts ───────────────────
#
# Converting a read to `config.fetch_all` fixes truncation and introduces a new
# way to be wrong: the function sorts by `id` unless told otherwise, and not
# every table HAS an `id`. `stock_data_daily` has 86 columns and none of them is
# `id`, so both price readers converted on 15-Aug raised
#
#     42703  column stock_data_daily.id does not exist
#
# on their FIRST page — the whole forward-price history for every swing outcome,
# unreadable, from the commit that was supposed to stop it being truncated.
#
# What makes this worth a standing check rather than a one-line fix is that the
# capability was already tested. `test_outcome_resolution_gap.py::
# test_fetch_all_lets_a_table_without_an_id_name_its_own_key` proves `order_by`
# is honoured, and its own docstring names a table with no `id` column. The
# parameter worked. Nobody checked the CALL SITES, which is this project's
# recurring shape: a function's correctness proves nothing about its callers.
#
# Probed live against the book, 16-Aug-2026, via `.order("id").range(0,0)` —
# the exact request fetch_all issues for page one:
#
#     lessons YES · allocation_decisions YES · intraday_setups YES ·
#     signal_log YES · stock_data_daily NO (42703)
#
# EXISTENCE IS ONLY HALF THE CLAIM. A sort key that exists but is not unique
# pages with no error at all and lets rows repeat and vanish across page
# boundaries — the failure mode the previous stage measured at 8324 rows / 5000
# distinct, and a strictly worse one than the 42703 above, because nothing
# raises. So each key below was also paged over the WHOLE table and its returned
# count checked against both the server-side count and its own distinct count:
#
#     lessons 1114 · signal_log 4563 · intraday_setups 8324 ·
#     allocation_decisions 20873 · stock_data_daily 55963
#     all three numbers equal for every row above.
#
# The map is keyed by TABLE, not by the bad ones, deliberately. An allowlist of
# known-broken tables goes stale in silence the day someone points fetch_all at
# a sixth table; requiring every table to carry a measured key means a new one
# fails here until somebody probes it. That is the friction, and it is the point.
_FETCH_ALL_SORT_KEY = {
    "lessons":              "id",
    "allocation_decisions": "id",
    "intraday_setups":      "id",
    "signal_log":           "id",
    # 55,963 rows and no `id` column. (symbol, date) is the table's natural key.
    "stock_data_daily":     "symbol,date",
    # Measured 2026-08-16 on the live table: 2,716 rows, NO `id` column — the
    # same trap as stock_data_daily. `sector` alone is NOT unique (25 distinct
    # across 1000 rows); (date, sector) probed unique.
    "sector_strength":      "date,sector",
    # Measured 2026-08-16: 51 rows, `id` present and unique. Note it has
    # start_date/end_date and NO `event_date`.
    "event_calendar":       "id",
    # Probed 2026-08-17 on the live table: 191,775 rows; `.order("id")` page
    # one returned 1000 rows, 1000 distinct, ids 1..1000. This table is why
    # both readers were paging in the first place — the unpaged health check
    # was silently truncating at 1000 of ~190,000, and quote_parity.report()'s
    # own unordered .range() loop was returning 38,559 day_high rows out of
    # the 38,683 that exist.
    "intraday_quote_parity": "id",
    # Probed 2026-08-18 on the live table: 2,511 rows, 2,511 distinct
    # (symbol, date), 257 distinct symbols. NO `id` column — CLAUDE.md names
    # this table specifically for that. `symbol` ALONE is emphatically not
    # unique (ten dates per name in the current window), which is the
    # non-unique-sort trap this map exists to catch: it pages without error
    # and silently repeats and drops rows.
    "signal_output_daily":  "symbol,date",
    # Probed 2026-08-18 on the live table: 162 rows, 162 distinct `id` —
    # unique, and it is the paged closed-book realised-R history five
    # readers (allocation/outcomes.py, allocation/swing_hold_days.py,
    # tools/benchmark.py, tools/exit_ladder_replay.py,
    # tools/expectancy_ledger.py) already page on the fetch_all default;
    # this only records the verification, it does not change any of them.
    "closed_positions":     "id",
}


def _fetch_all_sites() -> tuple[list[str], int]:
    """
    Every production `fetch_all(...)` call, the table it reads and the sort key
    it will use. Returns (violations, scanned).

    Parsed with `ast`, not a regex, for two reasons the regex version of the
    sibling check learned the hard way: a `fetch_all` argument may be a NAMED
    builder rather than a lambda (`hurdle.py` and `scoring.py` both pass one),
    and a text window wide enough to catch those reliably latches onto whatever
    statement follows.
    """
    import ast

    def _table_of(node, funcs: dict, depth: int = 3) -> str | None:
        """
        The table name inside a lambda body or a named builder function.

        Follows one builder into another, because `hurdle.py` pages with a
        `build()` that returns `base_query()` — the `.table()` call is a
        function away, and a single-level walk silently resolved nothing there
        and skipped the site rather than checking it.
        """
        if depth <= 0 or node is None:
            return None
        if isinstance(node, ast.Name):
            return _table_of(funcs.get(node.id), funcs, depth - 1)
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "table"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)):
                return sub.args[0].value
        # no .table() of its own — chase the local helpers it calls
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id in funcs):
                found = _table_of(funcs[sub.func.id], funcs, depth - 1)
                if found:
                    return found
        return None

    viol, scanned = [], 0
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or "db" in parts:
            continue
        if " - Copy" in path.name or path.name == "config.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        # Builders are defined at any nesting depth — hurdle's `build` is nested
        # two deep inside the function that pages with it.
        funcs = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        rel = path.relative_to(BACKEND).as_posix()

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name != "fetch_all":
                continue
            table = _table_of(node.args[0], funcs)
            if table is None:
                continue
            scanned += 1

            key = "id"          # config.fetch_all's default
            for kw in node.keywords:
                if kw.arg == "order_by" and isinstance(kw.value, ast.Constant):
                    key = kw.value.value

            want = _FETCH_ALL_SORT_KEY.get(table)
            if want is None:
                viol.append(
                    f"{rel}:{node.lineno} pages {table}, whose sort key has "
                    f"never been measured — probe `.order(\"id\")` against it "
                    f"and record the result in _FETCH_ALL_SORT_KEY")
            elif key != want:
                viol.append(
                    f"{rel}:{node.lineno} pages {table} sorted on '{key}', "
                    f"but that table's verified unique key is '{want}'"
                    + (" — `id` does not exist there and PostgREST raises "
                       "42703 on page one" if key == "id" else ""))
    return viol, scanned


def test_fetch_all_sorts_on_a_key_that_exists_on_the_table_it_reads():
    viol, scanned = _fetch_all_sites()

    # A PARSER THAT RESOLVES NOTHING REPORTS NOTHING. If `fetch_all` is ever
    # renamed, re-exported, or wrapped, this walk would quietly match zero calls
    # and pass forever while watching an empty set — the shape of the five dead
    # health checks this project has already found. 16 production sites exist
    # today; the floor is set below that so ordinary deletions do not trip it,
    # and high enough that losing the call style does.
    assert scanned >= 12, (
        f"only {scanned} fetch_all call sites resolved to a table — the parser "
        f"has stopped recognising how this codebase pages, and a check that "
        f"cannot fail is not a check")

    assert not viol, (
        f"{len(viol)} paged read(s) sort on a column that is not that table's "
        f"verified unique key. A missing column raises 42703 on the first page; "
        f"a non-unique one is worse — it pages without error and lets rows "
        f"repeat and vanish across page boundaries:\n  " + "\n  ".join(viol))



# ── THE SECOND DEFECT FAMILY: PAGED, BUT SORTED ON NOTHING ──────────────
#
# The check above treats `.range(` as evidence of boundedness, which is exactly
# right for the truncation it hunts and exactly wrong for this one. A
# hand-rolled `.range()` pager with no ORDER BY reads the whole table and still
# gets the wrong rows: PostgreSQL guarantees no row order without an ORDER BY,
# so successive LIMIT/OFFSET windows over one filter may repeat rows and skip
# others. The TOTAL comes back right, which is why nothing raises and why every
# count-based sanity check passes.
#
# Measured on the live book 15-Aug-2026, three trials of each reader:
#   allocator_replay._fetch_setups   8324 rows / 5000 distinct   (3324 lost)
#   control_room._load_setups        7864 rows / 5000 distinct   (2864 lost)
# Both on trial 1, both clean on trials 2 and 3. That is the property this
# check exists to stop anyone relying on.
#
# F-23 enumerated this family by grepping `table("intraday_setups")` and so saw
# only one table's worth. The same idiom was live on five more, including
# `swing_priors()` — the LIVE swing book's prior — and the PEAD engine's
# results-day bars.


def _scan_unsorted(label: str, text: str) -> tuple[list[str], int]:
    """The detector itself, over ONE source text. Split out from the file walk
    so the test can feed it a known-bad sample and prove it still bites."""
    import re
    viol, scanned = [], 0
    # NOTE the table name is captured LOOSELY — `([^)\s]*?)` — because
    # `engine_scorecard._fetch` and `taken_reconciliation._rows` pass it as a
    # PARAMETER, `sb.table(table)`. The unpaged check above matches
    # `.table("literal")` only, so those two were invisible to it AND to
    # F-23's census, and one of them measured a live 3324-row loss.
    for m in re.finditer(
            r'\.table\(\s*([^)\s]*?)\s*\)((?:.|\n){0,900}?)\.execute\(\)', text):
        table, chain = m.group(1).strip("\"'"), m.group(2)
        if re.search(r'\.(insert|update|upsert|delete)\(', chain):
            continue
        if ".range(" not in chain:
            continue
        scanned += 1
        if ".order(" in chain:
            continue
        line = text[:m.start()].count("\n") + 1
        window = text[max(0, m.start() - 400):m.end()]
        if "sort-exempt:" in window:
            continue
        viol.append(f"{label}:{line} pages {table} with no sort key")
    return viol, scanned


def _unsorted_pagers() -> tuple[list[str], int]:
    """Every `.range()` pager with no `.order()`. Returns (violations, scanned)."""
    viol, scanned = [], 0
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.parts
        if "tests" in parts or "__pycache__" in parts or "db" in parts:
            continue
        if " - Copy" in path.name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        v, s = _scan_unsorted(path.relative_to(BACKEND).as_posix(), text)
        viol += v
        scanned += s
    return viol, scanned


# A POSITIVE CONTROL, not a row count. The check above this one guards against
# a vacuous pass with `scanned > 40`, which works because reads of large tables
# are plentiful and stay plentiful. That reasoning does NOT transfer here: this
# check's own fixes REMOVE `.range()` sites, so the population it counts shrinks
# every time it succeeds — the scan went 19 -> 10 in the session that added it,
# and a floor set from the "before" number would have failed on the "after".
# A threshold that the fix itself erodes is a worse guard than no threshold.
# So prove the detector bites on a sample that cannot change instead.
_KNOWN_BAD = '''
def _bad(sb):
    rows, off = [], 0
    while True:
        page = (sb.table("intraday_setups").select("id")
                  .range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000
    return rows
'''

_KNOWN_GOOD = '''
def _good(sb):
    rows, off = [], 0
    while True:
        page = (sb.table("intraday_setups").select("id")
                  .order("id").range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000
    return rows
'''


def test_no_range_pager_without_a_sort_key():
    # 1 — the detector fires on the defect it exists for
    bad, bad_scanned = _scan_unsorted("<control>", _KNOWN_BAD)
    assert bad_scanned == 1 and len(bad) == 1, (
        f"the detector no longer recognises an unsorted .range() pager "
        f"(scanned={bad_scanned}, flagged={len(bad)}) — it has stopped "
        f"matching this codebase's paging style, and a check that cannot fail "
        f"is not a check")

    # 2 — and does NOT fire on the same pager once sorted, so a green result
    #     means "sorted", not "unparsed"
    good, good_scanned = _scan_unsorted("<control>", _KNOWN_GOOD)
    assert good_scanned == 1 and not good, (
        f"the detector flags a correctly-sorted pager (scanned={good_scanned}, "
        f"flagged={len(good)}) — it would cry wolf about work already done")

    # 3 — the real tree
    viol, _ = _unsorted_pagers()
    assert not viol, (
        f"{len(viol)} paged read(s) sort on nothing. LIMIT/OFFSET without "
        f"ORDER BY may return the right COUNT built from the wrong rows — "
        f"measured on this book at 8324 rows / 5000 distinct. Use "
        f"config.fetch_all(), or add a `sort-exempt: <why>` comment:\n  "
        + "\n  ".join(viol))


TESTS = [
    ("no undefined names anywhere in the backend",
     test_no_undefined_names_anywhere_in_the_backend),
    ("no unpaged read of a table that exceeds the row cap",
     test_no_unpaged_read_of_a_table_that_exceeds_the_row_cap),
    ("fetch_all sorts on a key that exists on the table it reads",
     test_fetch_all_sorts_on_a_key_that_exists_on_the_table_it_reads),
    ("no .range() pager without a sort key",
     test_no_range_pager_without_a_sort_key),
]
