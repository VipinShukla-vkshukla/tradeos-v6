"""
The hurdle's percentile-space bar (06-Aug-2026).

WHAT THIS CATCHES
------------------
The old bar multiplied a base R number (the 75th percentile of
`allocation_decisions.edge`) by a time/scarcity factor. 05-Aug-2026 already
had to special-case a NEGATIVE base, because scaling it by >1 makes an
already-loose bar LOOSER — backwards from the intent. That special case was a
symptom: an R number's sign and scale are not stable across sessions, so
"multiply it by 1.6" was never a meaningful operation on it. The fix maps
time/scarcity pressure to WHICH PERCENTILE of today's arrivals must be
cleared instead, which needs no sign branch at all — this module proves that
property directly, on an arrival population that is entirely negative (the
actual shape `allocation_decisions.edge` has for INTRADAY right now, from
before Break 1 was fixed).
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows):
        self.data = rows

    def execute(self):
        return self


class _FakeSB:
    """Ignores filters entirely — this module tests the percentile mechanism,
    not the query's WHERE clauses, which are exercised for real by health.py
    against the live database."""
    def __init__(self, edges: list[float]):
        self._rows = [{"edge": e, "framework": "INTRADAY", "regime_bucket": "STRONG",
                       "trade_date": "2026-08-06"} for e in edges]

    def table(self, name):
        return _FakeQuery(self._rows)


def _positive_spread_edges(n=60):
    return [-2.0 + 4.0 * i / (n - 1) for i in range(n)]


def _all_negative_edges(n=60):
    """The actual shape of INTRADAY's allocation_decisions.edge today: every
    value below zero (below-floor priors resolving to a NEUTRAL, cost-only
    edge, pre-Break-1-fix)."""
    return [-2.0 + 1.0 * i / (n - 1) for i in range(n)]


def test_bar_rises_with_more_time_left():
    from allocation.hurdle import hurdle
    with cfg_ctx({"alloc_hurdle_min_sample": "30"}):
        sb = _FakeSB(_positive_spread_edges())
        bar_early, _ = hurdle("STRONG", 4, 355, "INTRADAY", sb, max_slots=4)
        bar_late,  _ = hurdle("STRONG", 4, 20,  "INTRADAY", sb, max_slots=4)
        assert bar_early >= bar_late, (
            f"more time left produced a LOWER bar ({bar_early} < {bar_late}) — "
            f"a slot spent at 09:20 should be judged more harshly than one at 14:45")


def test_bar_rises_as_slots_run_out():
    from allocation.hurdle import hurdle
    with cfg_ctx({"alloc_hurdle_min_sample": "30"}):
        sb = _FakeSB(_positive_spread_edges())
        bar_full, _ = hurdle("STRONG", 4, 200, "INTRADAY", sb, max_slots=4)
        bar_last, _ = hurdle("STRONG", 1, 200, "INTRADAY", sb, max_slots=4)
        assert bar_last >= bar_full, (
            f"the last slot ({bar_last}) was judged no more harshly than a full "
            f"book ({bar_full}) — scarcity must raise the bar")


def test_negative_arrival_population_does_not_invert():
    """
    On an all-negative population, more time left must STILL raise the bar
    (be MORE selective, i.e. a higher percentile) rather than making it more
    negative and therefore looser — the exact inversion 05-Aug's fix needed a
    sign branch to prevent. The percentile-space bar has no sign branch and
    does not need one: this is what proves that.
    """
    from allocation.hurdle import hurdle
    with cfg_ctx({"alloc_hurdle_min_sample": "30"}):
        sb = _FakeSB(_all_negative_edges())
        bar_more_time, in_more = hurdle("STRONG", 4, 355, "INTRADAY", sb, max_slots=4)
        bar_less_time, in_less = hurdle("STRONG", 4, 20,  "INTRADAY", sb, max_slots=4)
        assert bar_more_time >= bar_less_time, (
            f"on an all-negative arrival population, more time left produced a "
            f"LOWER (looser) bar ({bar_more_time} < {bar_less_time}) — patience "
            f"must never lower the bar")
        assert in_more["effective_percentile"] >= in_less["effective_percentile"]


def test_effective_percentile_never_drops_below_baseline():
    from allocation.hurdle import _effective_percentile
    assert _effective_percentile(0.75, 1.0, 1.0, 1.6, 1.5) == 0.75


def test_effective_percentile_caps_at_95():
    from allocation.hurdle import _effective_percentile
    assert _effective_percentile(0.75, 5.0, 5.0, 1.6, 1.5) == 0.95


def test_cold_start_ignores_time_and_scarcity():
    """A cold start (-inf) stays -inf regardless of pressure — unaffected, not
    scaled, exactly as before this change."""
    from allocation.hurdle import hurdle

    class _EmptySB:
        def table(self, name):
            return _FakeQuery([])

    with cfg_ctx({"alloc_hurdle_min_sample": "30", "alloc_hurdle_cold_start": ""}):
        bar, inputs = hurdle("STRONG", 4, 355, "INTRADAY", _EmptySB(), max_slots=4)
        assert bar == float("-inf")
        assert inputs["effective_percentile"] is None


TESTS = [
    ("bar rises with more time left",
     test_bar_rises_with_more_time_left),
    ("bar rises as slots run out",
     test_bar_rises_as_slots_run_out),
    ("negative arrival population does not invert",
     test_negative_arrival_population_does_not_invert),
    ("effective percentile never drops below baseline",
     test_effective_percentile_never_drops_below_baseline),
    ("effective percentile caps at 0.95",
     test_effective_percentile_caps_at_95),
    ("cold start ignores time and scarcity",
     test_cold_start_ignores_time_and_scarcity),
]
