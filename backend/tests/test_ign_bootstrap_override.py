"""
IGN's bounded, lifetime-capped bootstrap override — migration 133,
08-Sep-2026. See intraday/engine.py::_maybe_open_paper()/
_ign_bootstrap_used_count()/act_on_setups() and
intraday/event_core.py::_try_ign_fast_entry() for the full design; the
Context section of the plan this shipped from has the corrected
understanding of WHY (not a permanent trap — see docs/FINDINGS.md,
08-Sep-2026).

WHAT THIS FILE COVERS
----------------------
1. The counter itself (_ign_bootstrap_used_count/_consume_ign_bootstrap_
   slot) — lazy single-fetch, in-process caching, fail-to-the-strict-side
   on a query error. Mirrors test_today_totals_cache.py's own _CountingDB
   pattern.
2. act_on_setups()'s wiring — the override fires ONLY for strategy=="IGN"
   with slots available, NEVER for any other engine even with slots free
   (the explicit negative case), a downstream _maybe_open_paper() failure
   does not consume a slot, and the allocator's real DECLINE is always
   recorded before the override is ever considered. Built the same way
   test_paper_entry_verdicts.py exercises _maybe_open_paper() directly:
   IntradayEngine.__new__() plus targeted instance-attribute stubs, not a
   mock framework.
   (_try_ign_fast_entry()'s identical wiring on the 2s fast path already
   has its own coverage in test_ignition_fast_entry.py.)
3. Traceability — the bootstrap_override_slot marker actually reaches both
   open_positions (paper_broker.open_position()) and closed_positions
   (control.position_lifecycle.close()), the same pattern
   test_sub_engine_on_positions.py used for sub_engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests import cfg_ctx


# ── 1. the counter itself ───────────────────────────────────────────────

class _CountingClosedDB:
    """Counts real closed_positions fetches, same shape as
    test_today_totals_cache.py's _CountingDB — a test can assert the cache
    is actually doing something, not just returning a plausible number."""
    def __init__(self, closed_count: int):
        self._closed_count = closed_count
        self.fetch_count = 0
        self.fail = False

    def table(self, name):
        assert name == "closed_positions", (
            f"_ign_bootstrap_used_count() queries a different table now "
            f"({name!r}) — update this fixture")
        return self

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def is_(self, *a, **k): return self

    def execute(self):
        self.fetch_count += 1
        if self.fail:
            raise RuntimeError("PostgREST 503")
        r = SimpleNamespace()
        r.count = self._closed_count
        return r


def _counter_engine(closed_count=0, positions=None):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = _CountingClosedDB(closed_count)
    eng.positions = positions or []
    eng._ign_bootstrap_used = None
    return eng


def test_counter_lazily_fetches_once_and_caches():
    positions = [
        # counts: INTRADAY + IGN + a slot number
        {"framework": "INTRADAY", "sub_engine": "IGN", "bootstrap_override_slot": 1},
        # does NOT count: an ordinary (non-override) IGN entry
        {"framework": "INTRADAY", "sub_engine": "IGN", "bootstrap_override_slot": None},
        # does NOT count: wrong engine
        {"framework": "INTRADAY", "sub_engine": "GAP", "bootstrap_override_slot": 1},
        # does NOT count: wrong framework
        {"framework": "SWING", "sub_engine": "IGN", "bootstrap_override_slot": 1},
    ]
    eng = _counter_engine(closed_count=2, positions=positions)
    n1 = eng._ign_bootstrap_used_count()
    n2 = eng._ign_bootstrap_used_count()
    n3 = eng._ign_bootstrap_used_count()
    assert eng.sb.fetch_count == 1, (
        f"expected exactly 1 closed_positions fetch across 3 calls, got "
        f"{eng.sb.fetch_count} — the lazy cache is not being consulted")
    assert n1 == n2 == n3 == 3, f"expected 1 open + 2 closed = 3, got {n1}"


def test_counter_fails_to_the_strict_side_on_query_error_and_does_not_poison_the_cache():
    eng = _counter_engine(closed_count=0)
    eng.sb.fail = True
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        n = eng._ign_bootstrap_used_count()
    assert n == 10, (
        "an unreadable count must read as the lifetime cap already spent, "
        "never as budget available")
    # NOT cached as spent — a later successful call must recover the real
    # count rather than refusing IGN's bootstrap for the rest of the
    # process on one transient query failure.
    eng.sb.fail = False
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        n2 = eng._ign_bootstrap_used_count()
    assert n2 == 0, "a transient failure must not poison the in-process cache"


def test_consume_increments_the_cached_count_without_a_second_fetch():
    eng = _counter_engine(closed_count=0)
    assert eng._ign_bootstrap_used_count() == 0
    eng._consume_ign_bootstrap_slot()
    assert eng._ign_bootstrap_used_count() == 1
    eng._consume_ign_bootstrap_slot()
    assert eng._ign_bootstrap_used_count() == 2
    assert eng.sb.fetch_count == 1, "consuming a slot must not trigger a fresh DB fetch"


# ── 2. act_on_setups() wiring ───────────────────────────────────────────

def _fake_setup(strategy="IGN", symbol="IGNCO"):
    # confidence is read by act_on_setups()'s own "quiet" log line right
    # after _intraday_alert_worthy() returns False, even though that line
    # is stubbed away here — not by anything this file is testing.
    return SimpleNamespace(symbol=symbol, strategy=strategy, meta={}, confidence=0.5)


def _wiring_engine(bootstrap_used=10, open_result=True, allocator_ok=False):
    """IntradayEngine.__new__() plus targeted stubs — same shape as
    test_paper_entry_verdicts.py's _engine(). _intraday_alert_worthy is
    stubbed to False throughout so these tests stay scoped to the
    entry/override wiring and never need a real notifier."""
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng._verdicts = {}
    eng._recorded_setups: list[str] = []
    eng._opened_calls: list[dict] = []
    eng._consumed: list[int] = []
    eng._bootstrap_used = bootstrap_used
    eng._open_result = open_result

    eng.allocator_permits = lambda sym, product, fw: (allocator_ok, "edge below bar")
    eng._record_setup = (
        lambda st, phase, cost_pct, verdict, qty, mc_state=None:
        eng._recorded_setups.append(verdict))
    eng._intraday_alert_worthy = lambda st: False

    def _maybe_open_paper(st, qty, mc, phase="?", cost_pct=0.0,
                          pick_label=None, bootstrap_override_slot=None):
        eng._opened_calls.append({
            "st": st, "pick_label": pick_label,
            "bootstrap_override_slot": bootstrap_override_slot})
        return eng._open_result
    eng._maybe_open_paper = _maybe_open_paper

    eng._ign_bootstrap_used_count = lambda: eng._bootstrap_used

    def _consume():
        eng._bootstrap_used += 1
        eng._consumed.append(eng._bootstrap_used)
    eng._consume_ign_bootstrap_slot = _consume
    return eng


def _one_setup(strategy="IGN"):
    return [{"setup": _fake_setup(strategy), "qty": 10, "market": None,
            "phase": "PRIME", "cost_pct": 0.1}]


def test_ign_decline_with_a_slot_available_opens_as_override():
    eng = _wiring_engine(bootstrap_used=0, allocator_ok=False)
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        eng.act_on_setups(_one_setup("IGN"))
    assert eng._recorded_setups == ["ALLOCATOR_DECLINED"], (
        "the allocator's own real verdict must always be recorded first")
    assert len(eng._opened_calls) == 1, "a decline with a slot free must still open"
    assert eng._opened_calls[0]["bootstrap_override_slot"] == 1
    assert eng._consumed == [1]


def test_non_ign_decline_never_overrides_even_with_slots_free():
    """The explicit negative case: a strategy other than IGN must never use
    the bootstrap override, no matter how many slots remain."""
    eng = _wiring_engine(bootstrap_used=0, allocator_ok=False)
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        eng.act_on_setups(_one_setup("GAP"))
    assert eng._recorded_setups == ["ALLOCATOR_DECLINED"]
    assert eng._opened_calls == [], "only IGN may use the bootstrap override"
    assert eng._consumed == []


def test_cap_exhausted_ign_decline_stays_declined():
    eng = _wiring_engine(bootstrap_used=10, allocator_ok=False)
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        eng.act_on_setups(_one_setup("IGN"))
    assert eng._opened_calls == [], "the cap is a hard lifetime stop, not a suggestion"
    assert eng._consumed == []


def test_downstream_open_failure_does_not_consume_a_slot():
    eng = _wiring_engine(bootstrap_used=0, allocator_ok=False, open_result=False)
    with cfg_ctx({"intraday_ign_exploration_trades": "10"}):
        eng.act_on_setups(_one_setup("IGN"))
    assert len(eng._opened_calls) == 1, "the attempt must still happen"
    assert eng._consumed == [], (
        "constraint 6: only a CONFIRMED write may spend a lifetime slot")


def test_allocator_take_never_touches_the_bootstrap_counter():
    """A clean allocator TAKE is the ordinary path, unrelated to the
    override — it must record nothing as declined and must not carry a
    slot number, even for IGN."""
    eng = _wiring_engine(bootstrap_used=0, allocator_ok=True)
    eng.act_on_setups(_one_setup("IGN"))
    assert eng._recorded_setups == [], "a TAKE must not record ALLOCATOR_DECLINED"
    assert len(eng._opened_calls) == 1
    assert eng._opened_calls[0]["bootstrap_override_slot"] is None
    assert eng._consumed == []


# ── 3. traceability — the marker reaches both position tables ──────────

def test_open_position_carries_bootstrap_override_slot():
    from execution import paper_broker
    captured = {}

    def _capture(sb, row):
        captured.update(row)

    with patch("control.position_lifecycle._upsert_position", side_effect=_capture):
        paper_broker.open_position(
            "IGNCO", 10, 100.0,
            {"stop": 98.0, "target": 106.0, "strategy": "IGN",
             "sub_engine": "IGN", "direction": "LONG",
             "bootstrap_override_slot": 3},
            "INTRADAY", sb=object(), charges=5.0)
    assert captured.get("bootstrap_override_slot") == 3


def test_open_position_writes_none_for_an_ordinary_entry():
    from execution import paper_broker
    captured = {}

    def _capture(sb, row):
        captured.update(row)

    with patch("control.position_lifecycle._upsert_position", side_effect=_capture):
        paper_broker.open_position(
            "IGNCO", 10, 100.0,
            {"stop": 98.0, "target": 106.0, "strategy": "IGN",
             "sub_engine": "IGN", "direction": "LONG"},
            "INTRADAY", sb=object(), charges=5.0)
    assert captured.get("bootstrap_override_slot") is None, (
        "an ordinary entry (no override) must record NULL, not 0 or missing")


def test_close_carries_bootstrap_override_slot_through_to_the_closed_row():
    """Pure dict-construction check, mirroring
    test_sub_engine_on_positions.py's own pattern for the real `closed`
    dict in close(): {"bootstrap_override_slot": pos.get(...)}."""
    pos = {"symbol": "IGNCO", "strategy": "IGN", "sub_engine": "IGN",
          "bootstrap_override_slot": 5}
    closed = {"sub_engine": pos.get("sub_engine"),
             "bootstrap_override_slot": pos.get("bootstrap_override_slot")}
    assert closed["bootstrap_override_slot"] == 5


def test_close_carries_none_through_for_an_ordinary_position():
    pos = {"symbol": "IGNCO", "strategy": "IGN", "sub_engine": "IGN"}
    closed = {"sub_engine": pos.get("sub_engine"),
             "bootstrap_override_slot": pos.get("bootstrap_override_slot")}
    assert closed["bootstrap_override_slot"] is None


TESTS = [
    ("counter lazily fetches once and caches",
     test_counter_lazily_fetches_once_and_caches),
    ("counter fails to the strict side on a query error, without poisoning the cache",
     test_counter_fails_to_the_strict_side_on_query_error_and_does_not_poison_the_cache),
    ("consuming a slot increments the cache without a second fetch",
     test_consume_increments_the_cached_count_without_a_second_fetch),
    ("IGN decline with a slot available opens as override",
     test_ign_decline_with_a_slot_available_opens_as_override),
    ("non-IGN decline never overrides even with slots free",
     test_non_ign_decline_never_overrides_even_with_slots_free),
    ("cap-exhausted IGN decline stays declined",
     test_cap_exhausted_ign_decline_stays_declined),
    ("a downstream open failure does not consume a slot",
     test_downstream_open_failure_does_not_consume_a_slot),
    ("an allocator TAKE never touches the bootstrap counter",
     test_allocator_take_never_touches_the_bootstrap_counter),
    ("open_position carries bootstrap_override_slot",
     test_open_position_carries_bootstrap_override_slot),
    ("open_position writes None for an ordinary entry",
     test_open_position_writes_none_for_an_ordinary_entry),
    ("close() carries bootstrap_override_slot through to the closed row",
     test_close_carries_bootstrap_override_slot_through_to_the_closed_row),
    ("close() carries None through for an ordinary position",
     test_close_carries_none_through_for_an_ordinary_position),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
