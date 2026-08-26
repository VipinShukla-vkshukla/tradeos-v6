"""
Daily Summary Dashboard, swing framework evolution blueprint, 26-Aug-2026.

tools/snapshot_book_value.py — sleeve + all-time realized P&L + live
unrealized P&L, per book, so the operator can see whether the swing sleeve
(paper, ₹300,000) or the intraday sleeve is actually up or down. Pure
computation tested here; the write path (upsert to book_value_snapshots)
is exercised via a fake SB.
"""

from __future__ import annotations

from types import SimpleNamespace


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name
        self._filters: list[tuple[str, object]] = []

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def upsert(self, rows, on_conflict=None):
        self._store.setdefault(self._name, []).extend(rows)
        return self

    def execute(self):
        rows = self._store.get(self._name, [])
        matched = [r for r in rows if all(r.get(c) == v for c, v in self._filters)]
        return SimpleNamespace(data=[dict(r) for r in matched])


class FakeSB:
    def __init__(self, tables: dict):
        self._store = {k: list(v) for k, v in tables.items()}

    def table(self, name):
        return _FakeTable(self._store, name)


def test_compute_book_value_sums_sleeve_realized_and_unrealized():
    from tools.snapshot_book_value import compute_book_value
    sb = FakeSB({
        "closed_positions": [
            {"framework": "SWING", "realized_pnl": 500.0},
            {"framework": "SWING", "realized_pnl": -120.0},
            {"framework": "INTRADAY", "realized_pnl": 9999.0},   # must not leak in
        ],
        "open_positions": [
            {"framework": "SWING", "status": "ACTIVE", "unrealized_pnl": 250.0},
            {"framework": "SWING", "status": "PENDING_FILL", "unrealized_pnl": 5000.0},  # not ACTIVE, excluded
            {"framework": "INTRADAY", "status": "ACTIVE", "unrealized_pnl": -9999.0},
        ],
    })
    import tools.snapshot_book_value as sbv
    orig = sbv.capital_for
    sbv.capital_for = lambda fw: 300000.0 if fw == "SWING" else 100000.0
    try:
        result = compute_book_value(sb, "SWING")
    finally:
        sbv.capital_for = orig

    assert result["sleeve"] == 300000.0
    assert result["realized_pnl_cum"] == 380.0        # 500 - 120, INTRADAY excluded
    assert result["unrealized_pnl"] == 250.0            # PENDING_FILL excluded
    assert result["book_value"] == 300000.0 + 380.0 + 250.0


def test_unrealized_falls_back_to_current_minus_invested_when_column_is_null():
    from tools.snapshot_book_value import compute_book_value
    sb = FakeSB({
        "closed_positions": [],
        "open_positions": [
            {"framework": "SWING", "status": "ACTIVE", "unrealized_pnl": None,
             "current_value": 21000.0, "invested_value": 19440.0},
        ],
    })
    import tools.snapshot_book_value as sbv
    orig = sbv.capital_for
    sbv.capital_for = lambda fw: 300000.0
    try:
        result = compute_book_value(sb, "SWING")
    finally:
        sbv.capital_for = orig
    assert result["unrealized_pnl"] == 1560.0   # 21000 - 19440


def test_snapshot_writes_one_row_per_framework():
    from tools import snapshot_book_value as sbv
    sb = FakeSB({"closed_positions": [], "open_positions": []})
    orig = sbv.capital_for
    sbv.capital_for = lambda fw: 300000.0 if fw == "SWING" else 100000.0
    try:
        rows = sbv.snapshot(sb)
    finally:
        sbv.capital_for = orig

    assert len(rows) == 2
    fws = {r["framework"] for r in rows}
    assert fws == {"SWING", "INTRADAY"}
    assert sb._store["book_value_snapshots"], "must actually write, not just return"


def test_a_failed_framework_computation_does_not_block_the_other():
    """SWING and INTRADAY must be independent — a bad read for one book
    must not silently also drop the other's snapshot for the day."""
    from tools import snapshot_book_value as sbv

    class _BoomOnIntraday(FakeSB):
        def table(self, name):
            return super().table(name)

    sb = FakeSB({"closed_positions": [], "open_positions": []})
    orig = sbv.capital_for
    calls = []
    def fake_capital_for(fw):
        calls.append(fw)
        if fw == "INTRADAY":
            raise RuntimeError("boom")
        return 300000.0
    sbv.capital_for = fake_capital_for
    try:
        rows = sbv.snapshot(sb)
    finally:
        sbv.capital_for = orig

    assert len(rows) == 1 and rows[0]["framework"] == "SWING"


TESTS = [
    ("compute_book_value sums sleeve/realized/unrealized, per framework",
     test_compute_book_value_sums_sleeve_realized_and_unrealized),
    ("unrealized falls back to current-minus-invested when the column is null",
     test_unrealized_falls_back_to_current_minus_invested_when_column_is_null),
    ("snapshot writes one row per framework", test_snapshot_writes_one_row_per_framework),
    ("a failed framework computation does not block the other",
     test_a_failed_framework_computation_does_not_block_the_other),
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
