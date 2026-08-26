"""
tools/archive_allocation_decisions.py — 27-Aug-2026.

archive-then-delete, same contract as migration 016 (stock_data_daily), but
the archive target is a local file, not another Postgres table -- so this
is a two-step, application-level process rather than one PL/pgSQL function
in a transaction: export, verify the file round-trips, only then delete.
Never deletes on an export it can't verify.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name
        self._lt = None
        self._deleting = False

    def select(self, *_a, **_k):
        return self

    def delete(self):
        self._deleting = True
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, off, end):
        rows = self._matching()
        page = rows[off:end + 1]
        return SimpleNamespace(data=[dict(r) for r in page], _executed=True,
                                execute=lambda: SimpleNamespace(data=[dict(r) for r in page]))

    def _matching(self):
        rows = self._store.get(self._name, [])
        if self._lt:
            col, val = self._lt
            rows = [r for r in rows if r.get(col) is not None and r[col] < val]
        return rows

    def execute(self):
        if self._deleting:
            col, val = self._lt
            kept = [r for r in self._store.get(self._name, [])
                     if not (r.get(col) is not None and r[col] < val)]
            self._store[self._name] = kept
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[dict(r) for r in self._matching()])


class FakeSB:
    def __init__(self, rows: list[dict]):
        self._store = {"allocation_decisions": list(rows)}

    def table(self, name):
        return _FakeTable(self._store, name)


def _rows(n: int, trade_date: str) -> list[dict]:
    return [{"id": i, "trade_date": trade_date, "symbol": f"SYM{i}",
              "edge": 0.01 * i, "verdict": "DECLINE"} for i in range(n)]


def test_archives_nothing_and_deletes_nothing_when_no_rows_are_old_enough():
    from tools.archive_allocation_decisions import archive_and_prune
    sb = FakeSB(_rows(3, "2026-08-26"))
    result = archive_and_prune(keep_days=60, sb=sb)
    assert result["archived"] == 0
    assert len(sb._store["allocation_decisions"]) == 3


def test_archives_and_deletes_only_rows_older_than_the_cutoff():
    from tools.archive_allocation_decisions import archive_and_prune
    old = _rows(5, "2026-05-01")
    recent = _rows(2, "2026-08-26")
    for r in recent:
        r["id"] += 1000
    sb = FakeSB(old + recent)

    tmp = Path(tempfile.mkdtemp())
    import tools.archive_allocation_decisions as mod
    orig_dir = mod.ARCHIVE_DIR
    mod.ARCHIVE_DIR = tmp
    try:
        result = archive_and_prune(keep_days=60, sb=sb)
    finally:
        mod.ARCHIVE_DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)

    assert result["archived"] == 5
    remaining = sb._store["allocation_decisions"]
    assert len(remaining) == 2
    assert {r["trade_date"] for r in remaining} == {"2026-08-26"}


def test_dry_run_reports_the_count_but_deletes_nothing():
    from tools.archive_allocation_decisions import archive_and_prune
    sb = FakeSB(_rows(5, "2026-05-01"))
    result = archive_and_prune(keep_days=60, sb=sb, dry_run=True)
    assert result["archived"] == 5
    assert result["path"] is None
    assert len(sb._store["allocation_decisions"]) == 5, "dry run must not delete"


def test_a_failed_round_trip_verify_leaves_supabase_untouched():
    """The core safety property: if the file this writes can't be read back
    with the same row count and id set, nothing gets deleted."""
    from tools.archive_allocation_decisions import archive_and_prune
    import tools.archive_allocation_decisions as mod

    sb = FakeSB(_rows(4, "2026-05-01"))
    tmp = Path(tempfile.mkdtemp())
    orig_dir, orig_export = mod.ARCHIVE_DIR, mod._export_and_verify
    mod.ARCHIVE_DIR = tmp

    def _boom(rows, cutoff):
        raise RuntimeError("archive verify failed: simulated corruption")
    mod._export_and_verify = _boom
    try:
        raised = False
        try:
            archive_and_prune(keep_days=60, sb=sb)
        except RuntimeError:
            raised = True
        assert raised, "a failed verify must raise, not silently continue"
    finally:
        mod.ARCHIVE_DIR = orig_dir
        mod._export_and_verify = orig_export
        shutil.rmtree(tmp, ignore_errors=True)

    assert len(sb._store["allocation_decisions"]) == 4, "delete must not run after a failed verify"


TESTS = [
    ("archives nothing when no rows are old enough",
     test_archives_nothing_and_deletes_nothing_when_no_rows_are_old_enough),
    ("archives and deletes only rows older than the cutoff",
     test_archives_and_deletes_only_rows_older_than_the_cutoff),
    ("dry run reports the count but deletes nothing",
     test_dry_run_reports_the_count_but_deletes_nothing),
    ("a failed round-trip verify leaves Supabase untouched",
     test_a_failed_round_trip_verify_leaves_supabase_untouched),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name} — {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
