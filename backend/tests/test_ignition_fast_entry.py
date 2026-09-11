"""
IGN's fast-entry path — intraday/event_core.py::_try_ign_fast_entry(),
08-Sep-2026. See that function's own docstring and event_core.py's module
docstring ("THE ONE DELIBERATE EXCEPTION") for the full reasoning.

WHY THE ENGINE IS FAKED, NOT REAL
------------------------------------
`_evaluate_one_intraday_candidate()`'s own correctness (shortability,
sizing, liquidity, depth, cost, structure, AI advice, conviction) is a
LIVE-STATE-heavy method (needs self._stock_row, self._news, self._advice,
self._pending_review, self._confidence_floor, self._overlay_intraday_mult,
open positions...) and is exercised by the full offline suite plus
tools.simulate against the real book (see docs/FINDINGS.md, 08-Sep-2026) —
re-testing its own gates here would either duplicate that coverage or
require reimplementing a second, parallel fake of the entire pipeline.

What THIS file tests is the WIRING around it: does _try_ign_fast_entry()
call the right things, in the right order, with the right arguments, under
the right conditions — the orchestration, not the decision logic each
called function already owns. `_FakeEngine` below stubs
_evaluate_one_intraday_candidate()/_score_proposals() to RETURN canned
results while RECORDING exactly what they were called with, so a test can
assert on the wiring without needing a real book behind it.
"""
from __future__ import annotations

from dataclasses import dataclass

from allocation.policies import TAKE, DECLINE
from intraday.strategies.base import Setup
from tests import cfg_ctx


@dataclass
class _FakeMC:
    state: str = "NEUTRAL"
    allow_longs: bool = True
    allow_shorts: bool = False
    size_multiplier: float = 1.0
    index_chg_pct: float | None = None
    vs_vwap_pct: float | None = None
    reason: str = "test"


def _fake_setup(direction="LONG") -> Setup:
    return Setup(symbol="IGNCO", strategy="IGN", direction=direction,
                entry=100.0, stop=98.0 if direction == "LONG" else 102.0,
                target=104.0 if direction == "LONG" else 96.0,
                confidence=0.6, rationale="r", invalidation="i")


class _FakeEngine:
    """Stubs every engine method _try_ign_fast_entry() calls, recording
    exactly what it was called with. `held`/`eval_result`/`score_verdict`
    are the three knobs each test sets to exercise a different branch.

    `bootstrap_used`/`open_result` fake the IGN bootstrap-override counter
    (migration 133, 08-Sep-2026) — default `bootstrap_used=10` reads as
    "the lifetime cap is already spent", matching cfg_int(
    "intraday_ign_exploration_trades", 10)'s own default, so every test
    written before the override existed keeps its original meaning (no
    override fires) unless a test explicitly opts in with a lower count."""
    def __init__(self, held=False, eval_result="default", score_verdict=None,
                bootstrap_used=None, open_result=True):
        self.held = held
        self._index_ctx = None
        self.eval_result = (
            {"setup": _fake_setup(), "qty": 10, "cost_pct": 0.1,
             "market": _FakeMC(), "phase": "PRIME", "cost_note": "ok"}
            if eval_result == "default" else eval_result)
        self.score_verdict = (
            score_verdict if score_verdict is not None
            else [{"verdict": TAKE, "pick_label": "TOP_PICK", "reason": "clears"}])
        self.eval_calls = []
        self.score_calls = []
        self.opened = []
        self.recorded = []
        self._bootstrap_used = 10 if bootstrap_used is None else bootstrap_used
        self.bootstrap_consumed = []
        self._open_result = open_result

    def _held_by_framework(self, sym, fw):
        return self.held

    def _evaluate_one_intraday_candidate(self, sym, ctx, best, mc, st,
                                         shorts_live, runway_refused):
        self.eval_calls.append({"sym": sym, "best": best, "shorts_live": shorts_live})
        return self.eval_result

    def _score_proposals(self, props):
        self.score_calls.append(props)
        return self.score_verdict

    def _maybe_open_paper(self, setup, qty, mc, phase="?", cost_pct=0.0,
                          pick_label=None, bootstrap_override_slot=None,
                          entry_path=None):
        self.opened.append({"setup": setup, "qty": qty, "pick_label": pick_label,
                            "bootstrap_override_slot": bootstrap_override_slot,
                            "entry_path": entry_path})
        return self._open_result

    def _record_setup(self, setup, phase, cost_pct, verdict, qty, mc_state=None):
        self.recorded.append(verdict)

    def _ign_bootstrap_used_count(self):
        return self._bootstrap_used

    def _consume_ign_bootstrap_slot(self):
        self._bootstrap_used += 1
        self.bootstrap_consumed.append(self._bootstrap_used)


def _patched_session_and_market(monkeypatch_ns: dict, phase="PRIME", can_enter=True,
                                mc=None):
    """Manual monkeypatch (save/restore), matching this codebase's own
    plain-Python fake style rather than a mock framework. Patches the
    SOURCE modules (intraday.session, intraday.market_context), not
    event_core's own namespace — _try_ign_fast_entry() re-imports them
    fresh (`from intraday.session import session_state`) on every call, so
    patching the home module is what actually takes effect."""
    import intraday.session as session_mod
    import intraday.market_context as mkt_mod
    from intraday.session import SessionState

    monkeypatch_ns["orig_session_state"] = session_mod.session_state
    monkeypatch_ns["orig_from_context"] = mkt_mod.from_context

    fixed_state = SessionState(phase=phase, minutes_since_open=60,
                               minutes_to_squareoff=120, can_enter=can_enter, reason="")
    session_mod.session_state = lambda now=None: fixed_state
    mkt_mod.from_context = lambda index_ctx: (mc if mc is not None else _FakeMC())


def _restore(monkeypatch_ns: dict):
    import intraday.session as session_mod
    import intraday.market_context as mkt_mod
    session_mod.session_state = monkeypatch_ns["orig_session_state"]
    mkt_mod.from_context = monkeypatch_ns["orig_from_context"]


def test_take_verdict_opens_a_paper_position():
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine()
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert len(eng.opened) == 1, "a TAKE verdict must call _maybe_open_paper exactly once"
    assert eng.opened[0]["pick_label"] == "TOP_PICK", (
        "pick_label must come from the local verdict, not engine._verdicts")
    assert eng.opened[0]["entry_path"] == "fast_organic", (
        "a genuine single-candidate TAKE (never touches allocation_decisions) "
        "must be tagged 'fast_organic' so it is not confused with an ordinary "
        "competitive-pass approval")
    assert not eng.recorded, "a TAKE must not also record an ALLOCATOR_DECLINED row"


def test_decline_verdict_does_not_open_and_records_allocator_declined():
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(score_verdict=[{"verdict": DECLINE, "reason": "edge below bar"}])
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.opened == [], "a DECLINE verdict must never open a position"
    assert eng.recorded == ["ALLOCATOR_DECLINED"], (
        "a DECLINE must still be recorded — a refusal that leaves no row is "
        "a rule nobody can price, same standard as the ordinary path")


def test_already_held_symbol_short_circuits_before_any_scoring():
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(held=True)
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.eval_calls == [], "already-held must return before _evaluate_one_intraday_candidate"
    assert eng.opened == [] and eng.recorded == []


def test_candidate_refused_upstream_stops_the_fast_path():
    """_evaluate_one_intraday_candidate() returning None (any of ITS OWN
    gates -- shortability, re-entry, cross-framework, event, structure, AI,
    conviction, sizing, liquidity, depth, cost) must stop the fast path
    before the allocator is ever consulted."""
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(eval_result=None)
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.score_calls == [], "a None result must never reach _score_proposals"
    assert eng.opened == []


def test_shorts_live_is_computed_from_config_and_market_context_and_passed_through():
    """Confirms the fast path computes shorts_live the SAME way the
    ordinary path does (intraday_allow_shorts AND mc.allow_shorts) and
    passes it to _evaluate_one_intraday_candidate() — that method's own
    internal shortability.can_short() call is what actually protects a
    SHORT best, but only if shorts_live reaches it correctly."""
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns, mc=_FakeMC(allow_shorts=True))
    try:
        with cfg_ctx({"intraday_allow_shorts": "true"}):
            eng = _FakeEngine()
            _try_ign_fast_entry(eng, "IGNCO", ctx=object(),
                                best=_fake_setup("SHORT"), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.eval_calls[0]["shorts_live"] is True

    ns2 = {}
    _patched_session_and_market(ns2, mc=_FakeMC(allow_shorts=False))
    try:
        with cfg_ctx({"intraday_allow_shorts": "true"}):
            eng2 = _FakeEngine()
            _try_ign_fast_entry(eng2, "IGNCO", ctx=object(),
                                best=_fake_setup("SHORT"), phase="PRIME")
    finally:
        _restore(ns2)
    assert eng2.eval_calls[0]["shorts_live"] is False, (
        "market context not confirming weakness must read shorts_live=False "
        "even with the config switch on — the same AND the ordinary path applies")


# ── the bounded bootstrap override (migration 133), same wiring on the fast path

def test_decline_with_bootstrap_slot_available_opens_as_override():
    """An IGN decline with lifetime slots still free (migration 133) must
    proceed instead of stopping — the same override act_on_setups() applies
    on the ordinary 15s loop, reused here via the same two engine methods."""
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(score_verdict=[{"verdict": DECLINE, "reason": "edge below bar"}],
                          bootstrap_used=3)
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.recorded == ["ALLOCATOR_DECLINED"], (
        "the allocator's real verdict must still be recorded first, even "
        "when the override goes on to act anyway")
    assert len(eng.opened) == 1, "a decline with slots free must still open, overridden"
    assert eng.opened[0]["bootstrap_override_slot"] == 4, "next slot after 3 used is 4"
    assert eng.opened[0]["entry_path"] == "fast_bootstrap", (
        "the fast path's own override branch must be tagged 'fast_bootstrap', "
        "distinct from the ordinary loop's 'bootstrap'")
    assert eng.bootstrap_consumed == [4], "a confirmed open must consume exactly one slot"


def test_decline_with_no_bootstrap_slots_left_stays_declined():
    """The cap is a hard lifetime stop, not a suggestion — once spent, an
    IGN decline behaves exactly like the pre-override path."""
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(score_verdict=[{"verdict": DECLINE, "reason": "edge below bar"}],
                          bootstrap_used=10)
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert eng.opened == []
    assert eng.bootstrap_consumed == []


def test_bootstrap_override_downstream_failure_does_not_consume_a_slot():
    """Constraint 6: only a CONFIRMED write may spend a lifetime slot — a
    downstream _maybe_open_paper failure (fill failure, concurrency cap,
    paper capacity, ...) must not."""
    from intraday.event_core import _try_ign_fast_entry
    ns = {}
    _patched_session_and_market(ns)
    try:
        eng = _FakeEngine(score_verdict=[{"verdict": DECLINE, "reason": "edge below bar"}],
                          bootstrap_used=0, open_result=False)
        _try_ign_fast_entry(eng, "IGNCO", ctx=object(), best=_fake_setup(), phase="PRIME")
    finally:
        _restore(ns)
    assert len(eng.opened) == 1, "the attempt must still happen"
    assert eng.bootstrap_consumed == [], (
        "a downstream _maybe_open_paper failure must not cost a lifetime slot")


# ── wiring: the branch is gated by BOTH the strategy name and its own switch

def test_fast_entry_branch_is_gated_by_strategy_name_and_its_own_switch():
    """Source-inspection pin, same discipline as test_today_totals_cache.py's
    own cycle()-reset test: check()'s own body cannot be run standalone in a
    unit test (needs a live feed, a real registry pass, a real engine) — so
    this pins the two conditions that must both be true before
    _try_ign_fast_entry() is ever called, directly against the source."""
    import inspect
    from intraday import event_core as M
    src = inspect.getsource(M.check)
    guard = src[src.find('best.strategy == "IGN"'):]
    assert 'best.strategy == "IGN"' in src, (
        "the fast-entry branch must be scoped to IGN by name")
    assert "intraday_ign_fast_entry_enabled" in guard[:200], (
        "the strategy-name check and the switch check must guard the same branch")
    assert "_try_ign_fast_entry(" in guard[:400], (
        "the guarded branch must actually call _try_ign_fast_entry()")


TESTS = [
    ("TAKE verdict opens a paper position", test_take_verdict_opens_a_paper_position),
    ("DECLINE verdict does not open, records ALLOCATOR_DECLINED",
     test_decline_verdict_does_not_open_and_records_allocator_declined),
    ("already-held symbol short-circuits before any scoring",
     test_already_held_symbol_short_circuits_before_any_scoring),
    ("candidate refused upstream stops the fast path",
     test_candidate_refused_upstream_stops_the_fast_path),
    ("shorts_live computed from config + market context, passed through",
     test_shorts_live_is_computed_from_config_and_market_context_and_passed_through),
    ("decline with a bootstrap slot available opens as override",
     test_decline_with_bootstrap_slot_available_opens_as_override),
    ("decline with no bootstrap slots left stays declined",
     test_decline_with_no_bootstrap_slots_left_stays_declined),
    ("bootstrap override downstream failure does not consume a slot",
     test_bootstrap_override_downstream_failure_does_not_consume_a_slot),
    ("fast-entry branch gated by strategy name and its own switch",
     test_fast_entry_branch_is_gated_by_strategy_name_and_its_own_switch),
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
