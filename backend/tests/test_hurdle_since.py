"""
`alloc_hurdle_since` — a hard floor date for the BAR's own arrival population,
mirroring `priors_intraday_since`'s contract exactly — 20-Aug-2026.

WHY THIS EXISTS
-----------------
`scoring.intraday_priors()` already has a floor date so a per-ENGINE prior
cannot silently average across F-33's stop-clamping fix. `hurdle._empirical_base`
builds the BAR from a different table (`allocation_decisions.edge`) but that
column is COMPUTED using whatever engine prior was in force at write time — so
the bar's own population inherits the identical contamination and, until this
change, had no floor at all: only a 90-day ROLLING window
(`alloc_hurdle_lookback_days`). Every fix that shipped in the last two weeks
(F-33 on 18-Aug, F-39/F-40 on 20-Aug) sits inside that rolling window, so the
75th-percentile bar was being drawn from a population mixing pre- and
post-fix eras without anything saying so.

THIS FILE CHECKS THE PURE COMPUTATION, NOT THE FILTER'S EFFECT. The fakes
below (same shape as test_hurdle_dedup.py's) do not apply a WHERE clause —
they capture the exact `since` value handed to `.gte("trade_date", ...)` so
the max(rolling, floor) arithmetic can be asserted directly, which is what
actually matters: whether the query gets a later `since` than the rolling
window alone. PostgREST doing the actual filtering is not this function's
job to prove twice.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows, capture):
        self._rows = rows
        self._capture = capture

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

    def gte(self, col, val):
        if col == "trade_date":
            self._capture.append(val)
        return self

    def is_(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def order(self, col, *a, **k):
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows):
        self.data = rows

    def execute(self):
        return self


class _FakeSB:
    """Ignores WHERE clauses entirely, same as test_hurdle_dedup.py's fake —
    this exercises the since-arithmetic, not PostgREST's own filtering."""
    def __init__(self, rows: list[dict], capture: list[str]):
        self._rows = rows
        self._capture = capture

    def table(self, name):
        return _FakeQuery(self._rows, self._capture)


def _row(symbol, trade_date, edge, bucket="STRONG"):
    return {"symbol": symbol, "edge": edge, "framework": "INTRADAY",
            "regime_bucket": bucket, "trade_date": trade_date}


def _population(n=50):
    """Enough SETTLED (yesterday-dated) rows to clear a low floor without
    tripping the pooled-fallback re-fetch — keeps this file about the since
    arithmetic, not the settled_n/pooling mechanism test_hurdle_percentile.py
    and the 10-Aug finding already cover."""
    return [_row(f"SYM{i}", "2020-01-01", 0.01 * i) for i in range(n)]


def test_unset_by_default_leaves_the_rolling_window_untouched():
    """No config = today's exact behaviour: since is the rolling window
    alone. This is the regression guard — shipping this switch must not move
    the bar for anyone who has not armed it."""
    from allocation.hurdle import _empirical_base
    from config import today_ist
    from datetime import timedelta
    with cfg_ctx({"alloc_hurdle_min_sample": "10", "alloc_hurdle_lookback_days": "90"}):
        expected = (today_ist() - timedelta(days=90)).isoformat()
        capture: list[str] = []
        sb = _FakeSB(_population(), capture)
        _empirical_base("STRONG", "INTRADAY", sb)
        assert capture, "gte('trade_date', ...) was never called"
        assert all(v == expected for v in capture), (
            f"since drifted from the rolling window with the switch unset: "
            f"expected {expected}, got {set(capture)}")


def test_floor_date_later_than_the_rolling_window_wins():
    """The whole point: a floor date INSIDE the 90-day window must push
    `since` forward to it, past whatever the rolling window alone would
    have used — exactly priors_intraday_since's contract."""
    from allocation.hurdle import _empirical_base
    with cfg_ctx({"alloc_hurdle_min_sample": "10", "alloc_hurdle_lookback_days": "90",
                  "alloc_hurdle_since": "2026-08-20"}):
        capture: list[str] = []
        sb = _FakeSB(_population(), capture)
        _empirical_base("STRONG", "INTRADAY", sb)
        assert capture and all(v == "2026-08-20" for v in capture), (
            f"floor date did not win: got {set(capture)}, expected 2026-08-20")


def test_floor_date_earlier_than_the_rolling_window_does_not_loosen_it():
    """max(), not a blind override — a floor date OLDER than the rolling
    window must never widen the population back out. Guards against a
    future refactor swapping max() for a plain assignment."""
    from allocation.hurdle import _empirical_base
    from config import today_ist
    from datetime import timedelta
    with cfg_ctx({"alloc_hurdle_min_sample": "10", "alloc_hurdle_lookback_days": "90",
                  "alloc_hurdle_since": "1900-01-01"}):
        expected = (today_ist() - timedelta(days=90)).isoformat()
        capture: list[str] = []
        sb = _FakeSB(_population(), capture)
        _empirical_base("STRONG", "INTRADAY", sb)
        assert capture and all(v == expected for v in capture), (
            f"an ancient floor date widened the window: got {set(capture)}, "
            f"expected the rolling window {expected}")


def test_blank_floor_date_is_treated_as_unset():
    """system_config ships this key as '' (see migration) — an empty string
    must behave exactly like the key being absent, not like an empty date
    that happens to compare as the smallest possible string."""
    from allocation.hurdle import _empirical_base
    from config import today_ist
    from datetime import timedelta
    with cfg_ctx({"alloc_hurdle_min_sample": "10", "alloc_hurdle_lookback_days": "90",
                  "alloc_hurdle_since": ""}):
        expected = (today_ist() - timedelta(days=90)).isoformat()
        capture: list[str] = []
        sb = _FakeSB(_population(), capture)
        _empirical_base("STRONG", "INTRADAY", sb)
        assert capture and all(v == expected for v in capture)


TESTS = [
    ("unset by default leaves the rolling window untouched",
     test_unset_by_default_leaves_the_rolling_window_untouched),
    ("a floor date later than the rolling window wins",
     test_floor_date_later_than_the_rolling_window_wins),
    ("a floor date earlier than the rolling window does not loosen it",
     test_floor_date_earlier_than_the_rolling_window_does_not_loosen_it),
    ("a blank floor date is treated as unset",
     test_blank_floor_date_is_treated_as_unset),
]
