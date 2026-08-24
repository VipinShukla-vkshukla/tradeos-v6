"""
`review_swing_engine_lifecycle` — Track E, Stage E6, 24-Aug-2026. All 9
swing strategies have sat at `lifecycle=ACTIVE` since 25-Jul (MOM/RVS
re-touched 07-Aug) with nothing re-asking whether current evidence still
supports it — the "living engine lifecycle" piece of E6's own plan.
Mirrors `review_swing_family_maturity`'s own fake-SB test shape (same
file, same established pattern), extended with a `strategy_config` fake
since this function reads a strategy's CURRENT lifecycle before deciding
whether to propose a change.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self

    def order(self, col, *a, **k):
        """Paged reads sort on a unique key — see test_swing_family_
        maturity_review.py's own identical method for why this exists."""
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _Exec(self._rows[start:end + 1])

    def execute(self):
        return _Exec(self._rows)


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
    def __init__(self, signal_rows: list[dict], strategy_rows: list[dict]):
        self._signal_rows = signal_rows
        self._strategy_rows = strategy_rows
        self.proposals: list[dict] = []

    def table(self, name):
        if name == "signal_output_daily":
            return _FakeQuery(self._signal_rows)
        if name == "strategy_config":
            return _FakeQuery(self._strategy_rows)
        if name == "brain_proposals":
            return _FakeProposalsTable(self.proposals)
        raise AssertionError(f"unexpected table: {name}")


def _resolved(strategy: str, category: str, pct: float) -> dict:
    return {"strategy": strategy, "outcome_category": category,
            "outcome_return_pct": pct, "symbol": "X", "date": "2026-08-01"}


def _cfg_row(strategy: str, lifecycle: str = "ACTIVE") -> dict:
    return {"strategy": strategy, "lifecycle": lifecycle, "enabled": True}


def test_below_sample_floor_holds_not_retires():
    """RVS's own real shape, 24-Aug-2026: n=10, avg -0.97% — genuinely
    concerning, but nowhere near the 40-sample floor. Must hold, not
    manufacture a RETIRE from ten data points."""
    from tools.weekly_review import review_swing_engine_lifecycle
    rows = ([_resolved("RVS", "STOP", -3.0) for _ in range(6)]
           + [_resolved("RVS", "TARGET", 2.0) for _ in range(4)])
    with cfg_ctx({}):
        sb = _FakeSB(rows, [_cfg_row("RVS")])
        review_swing_engine_lifecycle(sb)
    assert not sb.proposals, (
        f"n=10 is below the 40-sample floor — must not propose a "
        f"lifecycle change on it, got {sb.proposals}")


def test_healthy_engine_at_full_sample_keeps_active():
    """CTL's own real shape: strong hit rate, strong avg, well above the
    floor — must stay keep/ACTIVE, no proposal."""
    from tools.weekly_review import review_swing_engine_lifecycle
    rows = ([_resolved("CTL", "TARGET", 3.0) for _ in range(50)]
           + [_resolved("CTL", "STOP", -2.0) for _ in range(14)])
    with cfg_ctx({}):
        sb = _FakeSB(rows, [_cfg_row("CTL")])
        review_swing_engine_lifecycle(sb)
    assert not sb.proposals, (
        f"78% hit, positive avg, n=64 — healthy, must not propose a "
        f"change, got {sb.proposals}")


def test_weak_engine_at_full_sample_proposes_shadow():
    """A strategy with real, sufficient, negative-expectancy evidence
    must propose SHADOW (or RETIRE, if bad enough) — the whole point of
    a LIVING lifecycle."""
    from tools.weekly_review import review_swing_engine_lifecycle
    rows = ([_resolved("TPO", "STOP", -3.0) for _ in range(30)]
           + [_resolved("TPO", "TARGET", 1.0) for _ in range(15)])
    with cfg_ctx({}):
        sb = _FakeSB(rows, [_cfg_row("TPO")])
        review_swing_engine_lifecycle(sb)
    assert sb.proposals, "negative avg at n=45 must propose a change"
    p = sb.proposals[0]
    assert p["proposal_type"] == "SWING_ENGINE_LIFECYCLE"
    assert p["target_key"] == "TPO"
    assert p["proposed_value"] in ("SHADOW", "RETIRE")


def test_combined_tags_are_measured_but_never_matched_to_a_lifecycle_row():
    """A combined tag ('MOM+SEC') is a real, distinct outcome the pipeline
    writes when more than one engine's trigger fired together — not a
    typo to merge into either single strategy, and not something
    strategy_config has a row for. Must not crash, and must not propose
    anything for a tag with no lifecycle to change."""
    from tools.weekly_review import review_swing_engine_lifecycle
    rows = [_resolved("MOM+SEC", "TARGET", 5.0) for _ in range(50)]
    with cfg_ctx({}):
        sb = _FakeSB(rows, [_cfg_row("MOM"), _cfg_row("SEC")])
        review_swing_engine_lifecycle(sb)
    assert not sb.proposals, (
        f"a combined tag has no strategy_config row to change — must not "
        f"propose anything for it, got {sb.proposals}")


def test_no_proposal_when_verdict_matches_current_lifecycle():
    """A strategy already sitting at SHADOW that still reads as SHADOW-
    worthy must not re-propose the same thing every run."""
    from tools.weekly_review import review_swing_engine_lifecycle
    rows = ([_resolved("TPO", "STOP", -3.0) for _ in range(30)]
           + [_resolved("TPO", "TARGET", 1.0) for _ in range(15)])
    with cfg_ctx({}):
        sb = _FakeSB(rows, [_cfg_row("TPO", lifecycle="SHADOW")])
        review_swing_engine_lifecycle(sb)
    assert not sb.proposals, (
        "verdict already matches the current lifecycle — nothing to "
        f"propose, got {sb.proposals}")


TESTS = [
    ("below sample floor holds, does not retire",
     test_below_sample_floor_holds_not_retires),
    ("healthy engine at full sample keeps ACTIVE",
     test_healthy_engine_at_full_sample_keeps_active),
    ("weak engine at full sample proposes a change",
     test_weak_engine_at_full_sample_proposes_shadow),
    ("combined tags are measured but never matched to a lifecycle row",
     test_combined_tags_are_measured_but_never_matched_to_a_lifecycle_row),
    ("no proposal when verdict already matches current lifecycle",
     test_no_proposal_when_verdict_matches_current_lifecycle),
]
