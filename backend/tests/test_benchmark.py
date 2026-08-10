"""
The benchmark snapshot/compare tool, pinned against known arithmetic
(10-Aug-2026).

WHAT THIS CATCHES
-----------------
The operator asked directly how to gain confidence that a code change "is not
breaking anything or introducing any bug" by comparing TradeOS's actual
behaviour before and after, book by book. `tools/benchmark.py` answers this
with a snapshot/compare pair built entirely from ground-truth columns
(`closed_positions.r_multiple`, `.realized_pnl`) rather than any
model-derived figure — this project spent an entire session today discovering
how easily a derived number can disagree with the real ledger, and this tool
deliberately never repeats that mistake by staying on the ledger side of it.

These tests pin the arithmetic (win rate, profit factor, avg R) against
hand-computed values, and confirm snapshot -> JSON -> compare round-trips
correctly.
"""

from __future__ import annotations

import json


class _Q:
    def __init__(self, rows):
        self.r = rows
        self._eq = {}

    def select(self, *a, **k): return self
    def gte(self, *a, **k): return self

    def eq(self, col, val):
        q = _Q(self.r)
        q._eq = {**self._eq, col: val}
        return q

    @property
    def not_(self): return self

    def is_(self, *a, **k): return self

    def _filtered(self):
        rows = self.r
        for c, v in self._eq.items():
            rows = [r for r in rows if r.get(c) == v]
        return rows

    def range(self, a, b):
        return _E(self._filtered()[a:b + 1])

    def execute(self):
        return _E(self._filtered())


class _E:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, closed=None, open_pos=None, setups=None):
        self.closed = closed or []
        self.open_pos = open_pos or []
        self.setups = setups or []

    def table(self, name):
        if name == "closed_positions":
            return _Q(self.closed)
        if name == "open_positions":
            return _Q(self.open_pos)
        if name == "intraday_setups":
            return _Q(self.setups)
        return _Q([])


def _closed(r_mult, pnl, date="2026-08-05", fw="INTRADAY"):
    return {"r_multiple": r_mult, "realized_pnl": pnl, "charges": 10.0,
            "entry_date": date, "framework": fw}


def test_win_rate_and_profit_factor_match_hand_computation():
    """2 winners (+1.0R/+200, +0.5R/+100), 1 loser (-0.6R/-90).
    win_rate = 2/3 = 66.7%. PF = gross_win/gross_loss = 300/90 = 3.33."""
    from tools.benchmark import _closed_trade_stats
    rows = [_closed(1.0, 200.0), _closed(0.5, 100.0), _closed(-0.6, -90.0)]
    sb = _SB(closed=rows)
    stats = _closed_trade_stats(sb, "INTRADAY", days=10)
    assert stats["n"] == 3
    assert stats["wins"] == 2 and stats["losses"] == 1
    assert abs(stats["win_rate_pct"] - 66.7) < 0.1
    assert abs(stats["profit_factor"] - 3.33) < 0.01
    assert abs(stats["avg_r"] - (1.0 + 0.5 - 0.6) / 3) < 1e-9


def test_no_losses_gives_no_profit_factor_not_a_crash():
    """A division by zero (gross_loss=0) must report None, not raise or
    silently print inf/nan — the same 'a check that cannot fail is not a
    check' lesson applied to a metric that cannot be wrong by construction."""
    from tools.benchmark import _closed_trade_stats
    rows = [_closed(1.0, 200.0)]
    stats = _closed_trade_stats(_SB(closed=rows), "INTRADAY", days=10)
    assert stats["profit_factor"] is None


def test_empty_window_reports_zero_not_none_counts():
    from tools.benchmark import _closed_trade_stats
    stats = _closed_trade_stats(_SB(closed=[]), "INTRADAY", days=10)
    assert stats["n"] == 0
    assert stats["avg_r"] is None
    assert stats["win_rate_pct"] is None


def test_framework_filter_is_respected():
    """A SWING row must never leak into the INTRADAY snapshot or vice versa —
    the two books' real track records must never be blended."""
    from tools.benchmark import _closed_trade_stats
    rows = [_closed(1.0, 200.0, fw="INTRADAY"), _closed(-5.0, -900.0, fw="SWING")]
    sb = _SB(closed=rows)
    intraday = _closed_trade_stats(sb, "INTRADAY", days=10)
    swing = _closed_trade_stats(sb, "SWING", days=10)
    assert intraday["n"] == 1 and intraday["avg_r"] == 1.0
    assert swing["n"] == 1 and swing["avg_r"] == -5.0


def test_snapshot_round_trips_through_json():
    """The whole point is A/B comparison across two separate runs, possibly
    days apart -- the snapshot must serialise and deserialise losslessly."""
    from tools.benchmark import snapshot
    sb = _SB(closed=[_closed(0.5, 100.0)],
            open_pos=[{"symbol": "X", "unrealized_pnl": 10.0, "status": "ACTIVE",
                      "framework": "SWING"}])
    data = snapshot(days=10, label="test", sb=sb)
    restored = json.loads(json.dumps(data))
    assert restored == data
    assert restored["intraday"]["closed"]["n"] == 1
    assert restored["swing"]["open"]["open_count"] == 1


def test_compare_runs_end_to_end_on_real_files(tmp_path=None):
    """Smoke test through the actual CLI-facing compare() function, using
    real files on disk -- the code path the operator actually runs."""
    import tempfile
    from tools.benchmark import snapshot, compare

    sb1 = _SB(closed=[_closed(0.5, 100.0)])
    sb2 = _SB(closed=[_closed(0.5, 100.0), _closed(1.2, 250.0)])
    d1, d2 = snapshot(10, "a", sb=sb1), snapshot(10, "b", sb=sb2)

    with tempfile.TemporaryDirectory() as td:
        import os
        p1, p2 = os.path.join(td, "a.json"), os.path.join(td, "b.json")
        with open(p1, "w") as f:
            json.dump(d1, f)
        with open(p2, "w") as f:
            json.dump(d2, f)
        code = compare(p1, p2)
    assert code == 0


TESTS = [
    ("win rate and profit factor match hand computation",
     test_win_rate_and_profit_factor_match_hand_computation),
    ("no losses gives no profit factor, not a crash",
     test_no_losses_gives_no_profit_factor_not_a_crash),
    ("empty window reports zero, not None counts",
     test_empty_window_reports_zero_not_none_counts),
    ("framework filter is respected",
     test_framework_filter_is_respected),
    ("snapshot round-trips through JSON",
     test_snapshot_round_trips_through_json),
    ("compare runs end to end on real files",
     test_compare_runs_end_to_end_on_real_files),
]
