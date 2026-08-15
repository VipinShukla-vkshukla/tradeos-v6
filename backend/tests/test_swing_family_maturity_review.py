"""
`review_swing_family_maturity` — 07-Aug-2026, operator's request after
seeing Point 5 (position replacement) is blocked on evidence: "yes please
build" the automated re-check, same pattern as review_ai_tier_weight.

07-Aug-2026, the day this was built: CONTINUATION was the closest family
at 561 entered / 125 resolved (22%), nowhere near a 60% bar. This is that
check wired into the weekly review so nobody has to pull the SQL by hand
again to find out when a family finally clears it.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeSignalQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

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
    def __init__(self, sink: list):
        self._sink = sink

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

    def execute(self):
        return _Exec([])

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


def _signal(strategy: str, resolved: bool):
    return {"strategy": strategy, "outcome_entered": True,
            "outcome_category": "TARGET" if resolved else "OPEN"}


def _population(strategy: str, entered: int, resolved: int):
    rows = [_signal(strategy, True) for _ in range(resolved)]
    rows += [_signal(strategy, False) for _ in range(entered - resolved)]
    return rows


def test_no_proposal_at_todays_actual_maturity():
    """The real state, 07-Aug-2026: CONTINUATION 561/125 (22%) -- well short."""
    from tools.weekly_review import review_swing_family_maturity
    with cfg_ctx():
        sb = _FakeSB(_population("CTL", 561, 125))
        review_swing_family_maturity(sb)
        assert not sb.proposals, (
            "22% resolved must not clear a 60% default bar")


def test_proposes_once_a_family_clears_both_bars():
    from tools.weekly_review import review_swing_family_maturity
    with cfg_ctx({"swing_maturity_min_resolved": "30",
                  "swing_maturity_min_resolved_pct": "60"}):
        sb = _FakeSB(_population("CTL", 100, 65))   # 65%, n=65 resolved
        review_swing_family_maturity(sb)
        assert sb.proposals, "65% resolved with n=65 must clear a 30/60% bar"
        p = sb.proposals[0]
        assert p["proposal_type"] == "SWING_FAMILY_MATURE"
        assert p["target_key"] == "position_replacement/CONTINUATION"


def test_no_proposal_when_pct_clears_but_count_is_too_thin():
    from tools.weekly_review import review_swing_family_maturity
    with cfg_ctx({"swing_maturity_min_resolved": "30",
                  "swing_maturity_min_resolved_pct": "60"}):
        # 100% resolved, but only 4 signals ever entered -- not a real sample.
        sb = _FakeSB(_population("RVS", 4, 4))
        review_swing_family_maturity(sb)
        assert not sb.proposals, (
            "a 100% resolved rate on only 4 signals must not propose -- "
            "the count floor exists for exactly this case")


def test_no_proposal_when_count_clears_but_pct_is_too_low():
    from tools.weekly_review import review_swing_family_maturity
    with cfg_ctx({"swing_maturity_min_resolved": "30",
                  "swing_maturity_min_resolved_pct": "60"}):
        sb = _FakeSB(_population("MOM", 500, 40))   # n=40 resolved, but only 8%
        review_swing_family_maturity(sb)
        assert not sb.proposals, (
            "40 resolved clears the count floor, but 8% is nowhere near "
            "60% -- most of the family is still unresolved and must not "
            "be called mature")


TESTS = [
    ("no proposal at today's actual maturity",
     test_no_proposal_at_todays_actual_maturity),
    ("proposes once a family clears both bars",
     test_proposes_once_a_family_clears_both_bars),
    ("no proposal when pct clears but count is too thin",
     test_no_proposal_when_pct_clears_but_count_is_too_thin),
    ("no proposal when count clears but pct is too low",
     test_no_proposal_when_count_clears_but_pct_is_too_low),
]
