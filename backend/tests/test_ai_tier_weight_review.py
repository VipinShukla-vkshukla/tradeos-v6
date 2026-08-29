"""
`review_ai_tier_weight` — RETIRED 29-Aug-2026.

Originally built 07-Aug-2026, operator's own request: "ensure it gets
picked at the right time in future," after agreeing rank_weight_tier and
rank_weight_conviction should stay at 0 for now (sample too thin: TIER_1
n=26 the day this was built, below the 30-sample floor). It was wired
into the weekly review so the question — "is the evidence strong enough
to promote the weight off zero yet" — was re-asked automatically every
week rather than depending on someone remembering to ask.

That question is now closed, not merely re-askable: run for real on
29-Aug-2026 with n=47/58/99 per tier, it found TIER_1 UNDERPERFORMING
TIER_2/TIER_3 (E[R] -0.180 vs +0.322 vs +0.372) — the promotion condition
this test file used to verify (monotonic separation, TIER_1 best) is the
OPPOSITE of what real data showed. ai_tier/ai_conviction were then removed
from the evening AI's output entirely (ai/ai_decision_engine.py's own
module docstring has the full measurement), so `review_ai_tier_weight`
now returns immediately and never proposes, regardless of what synthetic
data would once have cleared its own bar.

This module used to verify the PROMOTION logic worked correctly across
four scenarios (below floor / clears with separation / non-monotonic /
gap too small). Rewritten to verify the RETIREMENT instead — the same
favorable-data scenario that used to require a proposal must now produce
none, proving the early return is unconditional, not merely coincidental
with today's real data.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeSignalQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
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
        return _Exec(self._rows[start:end + 1])


class _Exec:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self


class _FakeProposalsTable:
    """Records every insert so a test can assert whether one happened."""
    def __init__(self, sink: list):
        self._sink = sink

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

    def execute(self):
        return _Exec([])   # never an existing PENDING proposal in these tests

    def insert(self, row):
        self._sink.append(row)
        return self

    def update(self, row):
        return self


class _FakeSB:
    def __init__(self, signal_rows: list[dict]):
        self._signal_rows = signal_rows
        self.proposals: list[dict] = []
        self.tables_touched: list[str] = []

    def table(self, name):
        self.tables_touched.append(name)
        if name == "signal_output_daily":
            return _FakeSignalQuery(self._signal_rows)
        if name == "brain_proposals":
            return _FakeProposalsTable(self.proposals)
        raise AssertionError(f"unexpected table: {name}")


def _plan(tier: str, entry: float, stop: float, ret_pct: float):
    return {"ai_tier": tier, "outcome_entered": True,
            "entry_zone_high": entry, "planned_stop": stop,
            "outcome_return_pct": ret_pct}


def _population(n1: int, n2: int, n3: int, r1: float, r2: float, r3: float):
    """n plans per tier, each realising an R of roughly r{tier} (entry=100,
    risk=10% -> R = ret_pct / 10)."""
    rows = []
    for _ in range(n1):
        rows.append(_plan("TIER_1", 100.0, 90.0, r1 * 10))
    for _ in range(n2):
        rows.append(_plan("TIER_2", 100.0, 90.0, r2 * 10))
    for _ in range(n3):
        rows.append(_plan("TIER_3", 100.0, 90.0, r3 * 10))
    return rows


def test_never_proposes_even_with_the_exact_data_that_used_to_qualify():
    """The scenario this file's own history used to require a proposal
    for (all three tiers clear the 30-sample floor with a monotonic,
    >=0.05R separation) — proving the retirement is unconditional, not
    just coincidentally correct against today's real data."""
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx({"ai_tier_separation_min": "0.05"}):
        sb = _FakeSB(_population(35, 35, 35, 0.30, 0.10, -0.05))
        review_ai_tier_weight(sb)
        assert not sb.proposals, (
            "review_ai_tier_weight proposed a promotion — the retirement "
            "early-return is not actually unconditional")


def test_never_touches_the_database_at_all():
    """The retired function should not even query signal_output_daily —
    the early return is the very first thing that runs."""
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx():
        sb = _FakeSB(_population(35, 35, 35, 0.30, 0.10, -0.05))
        review_ai_tier_weight(sb)
        assert not sb.tables_touched, (
            f"retired function still touched tables: {sb.tables_touched}")


TESTS = [
    ("never proposes, even with the exact data that used to qualify",
     test_never_proposes_even_with_the_exact_data_that_used_to_qualify),
    ("never touches the database at all", test_never_touches_the_database_at_all),
]
