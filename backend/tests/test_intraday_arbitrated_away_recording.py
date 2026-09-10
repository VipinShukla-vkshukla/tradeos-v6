"""
ARBITRATED_AWAY recording (docs/FINDINGS.md, 10-Sep-2026) — an ACTIVE engine
that loses arbitration to a rival now gets its own row, the same way a
SHADOW-lifecycle engine already did. Without this, RNG (11 lifetime taken
trades) and PBK (5) can never accumulate the evidence that would let them
start winning: `expected_r_for()` correctly never lets an unmeasured engine
outrank a measured one (test_engine_fairness_and_bands.py already pins that
as intended behaviour) — but the LOSER of that comparison was simply
discarded, with no row, no outcome, no way to ever stop being unmeasured.

Tests `_record_setup()` directly, the same real function the live loop
calls — not a reimplementation.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, sink: list):
        self._sink = sink

    def insert(self, payload: dict):
        self._sink.append(payload)
        return self

    def execute(self):
        return self


class _FakeSB:
    def __init__(self, sink: list):
        self._sink = sink

    def table(self, name):
        assert name == "intraday_setups"
        return _FakeQuery(self._sink)


class _Setup:
    def __init__(self, symbol="RELIANCE", strategy="PBK", confidence=0.60):
        self.symbol = symbol
        self.strategy = strategy
        self.confidence = confidence
        self.rr = 2.0
        self.entry, self.stop, self.target = 100.0, 99.0, 102.0
        self.risk_pct, self.reward_pct = 1.0, 2.0
        self.direction = "LONG"
        self.rationale = "test fixture"
        self.invalidation = "test"
        self.meta = {"lifecycle": "ACTIVE", "sub_engine": strategy, "family": strategy}


def _engine(sink: list):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = _FakeSB(sink)
    eng._recorded = {}
    return eng


def test_record_setup_accepts_the_new_verdict_cleanly():
    """No hardcoded verdict allowlist -- ARBITRATED_AWAY flows through to
    cost_verdict exactly like any other verdict string."""
    sink: list = []
    eng = _engine(sink)
    s = _Setup()
    with cfg_ctx({}):
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
    assert len(sink) == 1
    row = sink[0]
    assert row["cost_verdict"] == "ARBITRATED_AWAY"
    assert row["strategy"] == "PBK"  # family, falls back to strategy when unset
    assert row["meta"]["qty"] == 0


def test_arbitrated_away_never_written_as_a_traded_qty():
    sink: list = []
    eng = _engine(sink)
    s = _Setup()
    with cfg_ctx({}):
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
    assert sink[0]["meta"]["qty"] == 0


def test_dedup_still_applies_to_arbitrated_away_same_as_every_other_verdict():
    """The SAME 15s-repeat guard that stops SHADOW/BLOCKED_* from spamming
    duplicate rows must also bound ARBITRATED_AWAY -- an engine losing
    arbitration every cycle for an hour must not write an hour of rows."""
    sink: list = []
    eng = _engine(sink)
    s = _Setup()
    with cfg_ctx({}):
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
    assert len(sink) == 1, "the same setup, same verdict, must dedup like any other"


def test_a_verdict_change_still_writes_a_fresh_row():
    """If the same setup later WINS arbitration (or gets blocked on merit),
    that is a real change and must be recorded, not swallowed by dedup."""
    sink: list = []
    eng = _engine(sink)
    s = _Setup()
    with cfg_ctx({}):
        eng._record_setup(s, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
        eng._record_setup(s, "OPEN", 0.0, "BLOCKED_STRUCTURE", 0)
    assert len(sink) == 2
    assert [r["cost_verdict"] for r in sink] == ["ARBITRATED_AWAY", "BLOCKED_STRUCTURE"]


def test_shadow_and_arbitrated_away_are_never_the_same_verdict():
    """A SHADOW-lifecycle engine and an ACTIVE engine that lost arbitration
    are different facts (retired vs. eligible-but-beaten) -- must stay
    distinct verdicts so a reader can never conflate the two populations."""
    sink: list = []
    eng = _engine(sink)
    shadow = _Setup(strategy="GDB")
    shadow.meta["lifecycle"] = "SHADOW"
    active = _Setup(strategy="PBK")
    with cfg_ctx({}):
        eng._record_setup(shadow, "OPEN", 0.0, "SHADOW", 0)
        eng._record_setup(active, "OPEN", 0.0, "ARBITRATED_AWAY", 0)
    verdicts = {r["strategy"]: r["cost_verdict"] for r in sink}
    assert verdicts["GDB"] == "SHADOW"
    assert verdicts["PBK"] == "ARBITRATED_AWAY"


TESTS = [
    ("_record_setup accepts ARBITRATED_AWAY cleanly", test_record_setup_accepts_the_new_verdict_cleanly),
    ("ARBITRATED_AWAY never written as a traded qty", test_arbitrated_away_never_written_as_a_traded_qty),
    ("dedup still applies to ARBITRATED_AWAY", test_dedup_still_applies_to_arbitrated_away_same_as_every_other_verdict),
    ("a verdict change still writes a fresh row", test_a_verdict_change_still_writes_a_fresh_row),
    ("SHADOW and ARBITRATED_AWAY are never the same verdict", test_shadow_and_arbitrated_away_are_never_the_same_verdict),
]
