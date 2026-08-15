"""
`review_ai_tier_weight` — 07-Aug-2026, operator's own request: "ensure it
gets picked at the right time in future," after agreeing rank_weight_tier
and rank_weight_conviction should stay at 0 for now (both demoted 04-Aug,
sample too thin: TIER_1 n=26, TIER_2 n=57, TIER_3 n=133 the day this was
built — TIER_1 alone still below the 30-sample floor).

This is that check, wired into the weekly review so the question is
re-asked automatically every week rather than depending on someone
remembering to ask again. It never sets the weight itself — only raises a
brain_proposals row once the evidence clears the same bar Prior already
uses everywhere else in this codebase, and even then proposes "ready for
calibration," not a specific number, for the same reason the original
demotion refused to guess one.
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
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
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

    def table(self, name):
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


def test_no_proposal_while_any_tier_is_below_the_floor():
    """The real state the day this was built: TIER_1 n=26 (below 30)."""
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx():
        sb = _FakeSB(_population(26, 57, 133, 0.30, 0.10, -0.05))
        review_ai_tier_weight(sb)
        assert not sb.proposals, (
            "must not propose while TIER_1 (n=26) is still below the "
            "30-sample floor, even though TIER_2/TIER_3 have cleared it")


def test_proposes_once_all_tiers_clear_the_floor_with_real_separation():
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx({"ai_tier_separation_min": "0.05"}):
        sb = _FakeSB(_population(35, 35, 35, 0.30, 0.10, -0.05))
        review_ai_tier_weight(sb)
        assert sb.proposals, (
            "all three tiers cleared the floor with a monotonic, "
            ">=0.05R separation — must raise a proposal")
        p = sb.proposals[0]
        assert p["proposal_type"] == "AI_TIER_WEIGHT_READY"
        assert p["target_key"] == "rank_weight_tier"
        assert "0.0" in p["current_value"]


def test_no_proposal_when_separation_is_not_monotonic():
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx({"ai_tier_separation_min": "0.05"}):
        # TIER_2 beats TIER_1 -- not the expected ordering, no signal to trust.
        sb = _FakeSB(_population(35, 35, 35, 0.10, 0.30, -0.05))
        review_ai_tier_weight(sb)
        assert not sb.proposals, (
            "TIER_1 < TIER_2 is not a monotonic separation and must not "
            "propose turning the weight on")


def test_no_proposal_when_gap_is_too_small_to_matter():
    from tools.weekly_review import review_ai_tier_weight
    with cfg_ctx({"ai_tier_separation_min": "0.20"}):
        # Monotonic, but TIER_1 - TIER_3 = 0.03R, below the configured 0.20 floor.
        sb = _FakeSB(_population(35, 35, 35, 0.05, 0.03, 0.02))
        review_ai_tier_weight(sb)
        assert not sb.proposals, (
            "a monotonic but tiny (0.03R) separation below the configured "
            "minimum must not propose turning the weight on")


TESTS = [
    ("no proposal while any tier is below the floor",
     test_no_proposal_while_any_tier_is_below_the_floor),
    ("proposes once all tiers clear the floor with real separation",
     test_proposes_once_all_tiers_clear_the_floor_with_real_separation),
    ("no proposal when separation is not monotonic",
     test_no_proposal_when_separation_is_not_monotonic),
    ("no proposal when gap is too small to matter",
     test_no_proposal_when_gap_is_too_small_to_matter),
]
