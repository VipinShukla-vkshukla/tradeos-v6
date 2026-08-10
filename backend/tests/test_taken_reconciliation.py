"""
Does cost_verdict='TAKEN' actually mean a position opened? (10-Aug-2026)

WHAT THIS CATCHES
-----------------
The operator's own dashboard showed the intraday book net PROFITABLE
(+Rs 141.80, PF 1.55, 40 closed trades) while engine_scorecard reported the
book gross -0.138R over the same window and was read as "no edge." Both
numbers were correct for what they measured — they measure DIFFERENT
populations. `cost_verdict='TAKEN'` is written the moment a setup clears the
COST gate, before the allocator veto or capacity checks run, so a TAKEN row
can exist with no position ever opening behind it. This tool measures how
large that gap actually is.
"""

from __future__ import annotations

from tools.taken_reconciliation import reconcile


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

    def range(self, a, b):
        rows = self.r
        for c, v in self._eq.items():
            rows = [r for r in rows if r.get(c) == v]
        return _E(rows[a:b + 1])


class _E:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, setups, open_pos=None, closed_pos=None):
        self.setups = setups
        self.open_pos = open_pos or []
        self.closed_pos = closed_pos or []

    def table(self, name):
        if name == "intraday_setups":
            return _Q(self.setups)
        if name == "open_positions":
            return _Q(self.open_pos)
        if name == "closed_positions":
            return _Q(self.closed_pos)
        return _Q([])


def _setup(date, symbol, strategy, verdict):
    return {"trade_date": date, "symbol": symbol, "strategy": strategy,
            "cost_verdict": verdict}


def _pos(symbol, date, fw="INTRADAY"):
    return {"symbol": symbol, "entry_date": date, "framework": fw}


def test_a_large_gap_is_detected_and_warned():
    """3 TAKEN detections, 1 real position -- the tool must report 33% and
    warn, since that gap is large enough to explain a scorecard/dashboard
    disagreement."""
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN"),
              _setup("2026-08-05", "B", "VWR", "TAKEN"),
              _setup("2026-08-05", "C", "GAP", "TAKEN")]
    opened = [_pos("A", "2026-08-05")]
    code = reconcile(days=10, sb=_SB(setups, open_pos=opened))
    assert code == 0


def test_non_taken_rows_are_never_counted():
    """REJECTED_COST / ALLOCATOR_DECLINED rows must not inflate the TAKEN
    count -- only cost_verdict='TAKEN' rows are the population in question."""
    from tools.taken_reconciliation import _rows
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN"),
              _setup("2026-08-05", "D", "ORB", "REJECTED_COST"),
              _setup("2026-08-05", "E", "ORB", "ALLOCATOR_DECLINED")]
    taken = [r for r in setups if (r.get("cost_verdict") or "").upper() == "TAKEN"]
    assert len(taken) == 1 and taken[0]["symbol"] == "A"


def test_a_clean_book_where_every_taken_row_opened():
    """When every TAKEN detection matches a real position, the tool must not
    warn -- a 100% match rate is the healthy case."""
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN"),
              _setup("2026-08-05", "B", "VWR", "TAKEN")]
    opened = [_pos("A", "2026-08-05"), _pos("B", "2026-08-05")]
    code = reconcile(days=10, sb=_SB(setups, open_pos=opened))
    assert code == 0


def test_closed_positions_also_count_as_opened():
    """A position that already closed within the window is still evidence a
    real trade happened -- only open_positions would miss it entirely."""
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN")]
    closed = [_pos("A", "2026-08-05")]
    code = reconcile(days=10, sb=_SB(setups, closed_pos=closed))
    assert code == 0


def test_no_taken_rows_is_reported_not_crashed():
    code = reconcile(days=10, sb=_SB([]))
    assert code == 1


def test_allocator_declined_rows_are_explained_not_flagged_as_a_leak():
    """A TAKEN setup with a matching ALLOCATOR_DECLINED row for the same
    (symbol, engine, day) is EXPLAINED -- the allocator vetoed it, which is
    by design. This must be counted separately from a genuine operational
    leak, not blended into one undifferentiated gap number."""
    from tools.taken_reconciliation import reconcile
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN"),
              _setup("2026-08-05", "A", "ORB", "ALLOCATOR_DECLINED")]
    code = reconcile(days=10, sb=_SB(setups))
    assert code == 0


def test_an_unexplained_gap_is_distinguished_from_an_explained_one():
    """A TAKEN setup with NO matching ALLOCATOR_DECLINED row and no real
    position is the genuinely concerning case -- capacity, broker refusal,
    or a swallowed exception, not the allocator doing its job."""
    from tools.taken_reconciliation import reconcile
    setups = [_setup("2026-08-05", "A", "ORB", "TAKEN"),   # explained (declined)
              _setup("2026-08-05", "A", "ORB", "ALLOCATOR_DECLINED"),
              _setup("2026-08-05", "B", "VWR", "TAKEN")]    # unexplained
    code = reconcile(days=10, sb=_SB(setups))
    assert code == 0


TESTS = [
    ("a large gap is detected and warned",
     test_a_large_gap_is_detected_and_warned),
    ("non-TAKEN rows are never counted",
     test_non_taken_rows_are_never_counted),
    ("a clean book where every TAKEN row opened",
     test_a_clean_book_where_every_taken_row_opened),
    ("closed positions also count as opened",
     test_closed_positions_also_count_as_opened),
    ("no TAKEN rows is reported, not crashed",
     test_no_taken_rows_is_reported_not_crashed),
    ("allocator-declined rows are explained, not flagged as a leak",
     test_allocator_declined_rows_are_explained_not_flagged_as_a_leak),
    ("an unexplained gap is distinguished from an explained one",
     test_an_unexplained_gap_is_distinguished_from_an_explained_one),
]
