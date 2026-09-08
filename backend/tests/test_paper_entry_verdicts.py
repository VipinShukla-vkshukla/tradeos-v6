"""
Every silent refusal in _maybe_open_paper now writes a verdict row
(10-Aug-2026).

WHAT THIS CATCHES
-----------------
`tools/taken_reconciliation.py` measured 27 of 60 unmatched TAKEN detections
across 10 real sessions with NEITHER a real position NOR an ALLOCATOR_DECLINED
row — a setup that passed the cost gate AND the allocator's own veto, then
vanished. `_maybe_open_paper` was the one place left in the whole gate chain
that could refuse a setup with no trace: a concurrency cap, a daily budget, a
paper-broker capacity refusal, an already-held race, a failed fill, or a bare
exception all returned silently or logged to a channel `intraday_setups`
cannot see. Every other gate in this file (BLOCKED_SHORTABILITY,
BLOCKED_STRUCTURE, VETOED_AI, ALLOCATOR_DECLINED, ...) already followed the
"a refusal that leaves no row is a rule nobody can price" rule; this closes
the one gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests import cfg_ctx


@dataclass
class _FakeSetup:
    symbol: str = "PAYTM"
    strategy: str = "GAP"
    direction: str = "LONG"
    entry: float = 100.0
    stop: float = 99.0
    target: float = 103.0
    confidence: float = 0.7
    rr: float = 2.0
    rationale: str = ""
    invalidation: str = ""
    risk_pct: float = 1.0
    reward_pct: float = 3.0
    meta: dict = field(default_factory=dict)


class _RecordingSB:
    """
    Captures every _record_setup insert without touching a real database.

    Briefly grew select/eq/order/limit on 11-Aug-2026 for a gap-based
    pacing gate (IntradayEngine._last_intraday_entry_at()) that queried
    intraday_setups directly and, against this fake's original insert-only
    shape, hit an AttributeError its fail-strict except clause turned into
    "just now" — blocking every entry in this file for a reason none of
    these tests were about. That gate was replaced the same day by
    order_manager.entry_reserved(), which reads _entries_today() (stubbed
    below to a plain lambda, never touching sb) instead of querying this
    table directly — so the read-side methods are gone again; nothing in
    this file exercises them anymore.
    """
    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "intraday_setups"
        return self

    def insert(self, row):
        self.inserted.append(row)
        return self

    def execute(self):
        return self


def _engine(sb=None, positions=None):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = sb or _RecordingSB()
    eng.positions = positions or []
    eng._recorded = {}
    eng._entries_today = lambda: 0
    eng._held_by_framework = lambda sym, fw: False
    return eng


def _verdicts(sb) -> list[str]:
    return [r["cost_verdict"] for r in sb.inserted]


def test_auto_entry_off_is_recorded():
    sb = _RecordingSB()
    eng = _engine(sb)
    with cfg_ctx({"intraday_auto_entry": "false"}):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_AUTO_ENTRY_OFF"]
    assert ret is False, "a blocked entry must return False, not None"


def test_concurrency_cap_is_recorded():
    sb = _RecordingSB()
    positions = [{"framework": "INTRADAY"}] * 10
    eng = _engine(sb, positions=positions)
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "10"}):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_CONCURRENCY"]
    assert ret is False


def test_daily_budget_is_recorded():
    sb = _RecordingSB()
    eng = _engine(sb)
    eng._entries_today = lambda: 10
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "10"}):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_DAILY_BUDGET"]
    assert ret is False


def test_paper_capacity_refusal_is_recorded():
    sb = _RecordingSB()
    eng = _engine(sb)
    from unittest.mock import patch
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity", return_value=(False, "book full", 0)):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_PAPER_CAPACITY"]
    assert ret is False


def test_already_held_race_is_recorded():
    sb = _RecordingSB()
    eng = _engine(sb)
    eng._held_by_framework = lambda sym, fw: True
    from unittest.mock import patch
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity", return_value=(True, "", 5)):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_ALREADY_HELD"]
    assert ret is False


def test_a_failed_fill_is_recorded():
    sb = _RecordingSB()
    eng = _engine(sb)
    from unittest.mock import patch
    from execution.paper_broker import PaperFill
    bad_fill = PaperFill(ok=False, order_id=None, fill_price=None,
                         quantity=0, charges=0.0, message="no liquidity")
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity", return_value=(True, "", 5)), \
         patch("execution.paper_broker.simulate_fill", return_value=bad_fill), \
         patch("execution.paper_broker.product_for", return_value="MIS"):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_FILL_FAILED"]
    assert ret is False


def test_position_write_failure_is_recorded():
    """08-Sep-2026 fix, alongside the IGN bootstrap-override build: until
    now, paper_broker.open_position()'s own bool return was never even
    read here — a write failure this deep was indistinguishable from a
    clean success both to intraday_setups AND to this function's own
    caller. See _maybe_open_paper()'s own docstring."""
    sb = _RecordingSB()
    eng = _engine(sb)
    from unittest.mock import patch
    from execution.paper_broker import PaperFill
    good_fill = PaperFill(ok=True, order_id="x", fill_price=100.0,
                          quantity=10, charges=1.5, message="")
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity", return_value=(True, "", 5)), \
         patch("execution.paper_broker.simulate_fill", return_value=good_fill), \
         patch("execution.paper_broker.product_for", return_value="MIS"), \
         patch("execution.paper_broker.open_position", return_value=False):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_POSITION_WRITE_FAILED"]
    assert ret is False


def test_a_confirmed_open_returns_true():
    """The other half of the write-failure fix: a REAL success must read
    back as True, not just avoid reading as a refusal — this is the exact
    signal act_on_setups()/_try_ign_fast_entry() gate an IGN bootstrap-
    override slot's consumption on (migration 133)."""
    sb = _RecordingSB()
    eng = _engine(sb)
    eng.load_state = lambda: None   # real load_state() needs a live sb; not under test here
    from unittest.mock import patch
    from execution.paper_broker import PaperFill
    good_fill = PaperFill(ok=True, order_id="x", fill_price=100.0,
                          quantity=10, charges=1.5, message="")
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity", return_value=(True, "", 5)), \
         patch("execution.paper_broker.simulate_fill", return_value=good_fill), \
         patch("execution.paper_broker.product_for", return_value="MIS"), \
         patch("execution.paper_broker.open_position", return_value=True):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == [], "a confirmed open must not also record a BLOCKED_* verdict"
    assert ret is True


def test_an_exception_is_recorded_not_only_logged():
    """The worst of the six silent paths pre-fix: an exception anywhere in the
    fill/open_position block was caught and logged, with NOTHING written to
    intraday_setups — a real bug could eat opportunities with zero trace in
    the database."""
    sb = _RecordingSB()
    eng = _engine(sb)
    from unittest.mock import patch
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity",
               side_effect=RuntimeError("PostgREST 503")):
        ret = eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert _verdicts(sb) == ["BLOCKED_EXCEPTION"]
    assert ret is False


def test_the_exception_detail_lands_in_meta_not_the_verdict_column():
    """cost_verdict must stay an exact-match-able fixed string — every reader
    (taken_reconciliation, engine_scorecard, the gated prior) filters on it
    verbatim. The exception text belongs in meta, same as ai_note/event_note."""
    sb = _RecordingSB()
    eng = _engine(sb)
    from unittest.mock import patch
    with cfg_ctx({"intraday_auto_entry": "true", "intraday_max_concurrent": "50",
                  "intraday_max_new_per_day": "50"}), \
         patch("execution.gates.is_paper", return_value=True), \
         patch("execution.paper_broker.capacity",
               side_effect=RuntimeError("PostgREST 503")):
        eng._maybe_open_paper(_FakeSetup(), 10, None, phase="PRIME")
    assert sb.inserted[0]["cost_verdict"] == "BLOCKED_EXCEPTION"
    # meta is a native dict, not a JSON string — 20-Aug-2026 fix
    # (intraday/engine.py::_record_setup no longer json.dumps()s it before
    # handing it to the client, which was storing a JSON string inside the
    # jsonb column instead of a JSON object; this test's own json.loads()
    # was written against that bug and passed BECAUSE of it).
    meta = sb.inserted[0]["meta"]
    assert isinstance(meta, dict), f"meta is a {type(meta).__name__}, not a dict"
    assert "PostgREST 503" in meta.get("exception", "")


TESTS = [
    ("auto-entry off is recorded",
     test_auto_entry_off_is_recorded),
    ("concurrency cap is recorded",
     test_concurrency_cap_is_recorded),
    ("daily budget is recorded",
     test_daily_budget_is_recorded),
    ("paper capacity refusal is recorded",
     test_paper_capacity_refusal_is_recorded),
    ("already-held race is recorded",
     test_already_held_race_is_recorded),
    ("a failed fill is recorded",
     test_a_failed_fill_is_recorded),
    ("a position write failure is recorded",
     test_position_write_failure_is_recorded),
    ("a confirmed open returns True",
     test_a_confirmed_open_returns_true),
    ("an exception is recorded, not only logged",
     test_an_exception_is_recorded_not_only_logged),
    ("the exception detail lands in meta, not the verdict column",
     test_the_exception_detail_lands_in_meta_not_the_verdict_column),
]
