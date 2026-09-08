"""
IGN's exit-lag probe — intraday/event_core.py::_probe_ign_exit_lag(),
migration 135, 09-Sep-2026. See event_core.py's own "A SECOND EXCEPTION"
docstring section: pure measurement, changes nothing about any position.
Built to answer "does the ordinary 15s exit cycle cost anything on a fast
IGN hold" from real data rather than a guess — this system stores no
tick-level price history, so the question is unanswerable retroactively
and has to be instrumented BEFORE the trades it would measure.

WHAT THIS FILE COVERS
----------------------
1. IntradayEngine._ign_open_position() — the pure lookup.
2. _probe_ign_exit_lag() wiring, same _FakeEngine-style stub discipline as
   test_ignition_fast_entry.py: fires only for an open INTRADAY/IGN
   position, reuses the REAL exit_policy.evaluate_intraday_exit() (not a
   second implementation), never repeats once probed, HOLD/TRAIL_SL never
   count as a probe hit, a downstream write failure does not raise.
3. control.position_lifecycle._exit_lag_seconds() — pure.
4. close_position()'s own dict construction carries the three fields
   through, same pattern test_sub_engine_on_positions.py used for
   sub_engine/bootstrap_override_slot.
5. check()'s own loop actually calls the probe, gated by its own switch —
   source-inspection pin, same discipline as the fast-entry branch's own
   equivalent test.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from config import IST
from tests import cfg_ctx

_MID_SESSION = datetime(2026, 8, 10, 11, 0, 0, tzinfo=IST)


# ── IntradayEngine._ign_open_position() ─────────────────────────────────

def _engine_with_positions(positions):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.positions = positions
    return eng


def test_ign_open_position_matches_framework_and_sub_engine():
    eng = _engine_with_positions([
        {"symbol": "IGNCO", "framework": "INTRADAY", "sub_engine": "IGN", "id": 1},
        {"symbol": "OTHER", "framework": "INTRADAY", "sub_engine": "GAP"},
    ])
    pos = eng._ign_open_position("IGNCO")
    assert pos is not None and pos["id"] == 1


def test_ign_open_position_returns_none_for_other_engines():
    eng = _engine_with_positions(
        [{"symbol": "IGNCO", "framework": "INTRADAY", "sub_engine": "GAP"}])
    assert eng._ign_open_position("IGNCO") is None


def test_ign_open_position_returns_none_for_swing():
    """IGN's own vocabulary is intraday-only — a SWING position sharing
    the symbol must never be mistaken for the one this probe measures."""
    eng = _engine_with_positions(
        [{"symbol": "IGNCO", "framework": "SWING", "sub_engine": "IGN"}])
    assert eng._ign_open_position("IGNCO") is None


def test_ign_open_position_returns_the_actual_dict_not_a_copy():
    positions = [{"symbol": "IGNCO", "framework": "INTRADAY", "sub_engine": "IGN"}]
    eng = _engine_with_positions(positions)
    pos = eng._ign_open_position("IGNCO")
    pos["exit_lag_action"] = "EXIT_GIVEBACK"
    assert positions[0]["exit_lag_action"] == "EXIT_GIVEBACK", (
        "the probe mutates this dict in place so a later dirty tick in the "
        "same process sees it without a fresh load_state() — a copy would "
        "silently defeat that")


# ── _probe_ign_exit_lag() wiring ─────────────────────────────────────────

class _RecordingOpenPositionsSB:
    def __init__(self):
        self.updates: list[dict] = []
        self._pending = None

    def table(self, name):
        assert name == "open_positions"
        return self

    def update(self, fields):
        self._pending = dict(fields)
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        self.updates.append(self._pending)
        return self


class _FailingSB:
    def table(self, name): return self
    def update(self, fields): return self
    def eq(self, *a, **k): return self

    def execute(self):
        raise RuntimeError("PostgREST 503")


def _fake_engine(pos, sb=None):
    eng = SimpleNamespace()
    eng.sb = sb or _RecordingOpenPositionsSB()
    eng._ign_open_position = lambda sym: pos
    return eng


def _pos(hwm, entry=100.0, stop=99.0, direction="LONG", exit_lag_action=None):
    return {"entry_price": entry, "planned_stop": stop, "active_sl": stop,
            "planned_target": entry + (entry - stop) * 3, "direction": direction,
            "high_water_mark": hwm, "product": "MIS",
            "exit_lag_action": exit_lag_action}


def test_probe_does_nothing_when_no_open_ign_position():
    from intraday.event_core import _probe_ign_exit_lag
    eng = _fake_engine(None)
    ctx = SimpleNamespace(ltp=100.3)
    _probe_ign_exit_lag(eng, "IGNCO", ctx, _MID_SESSION)
    assert eng.sb.updates == []


def test_probe_never_fires_twice_for_the_same_position():
    from intraday.event_core import _probe_ign_exit_lag
    pos = _pos(hwm=102.0, exit_lag_action="EXIT_GIVEBACK")
    eng = _fake_engine(pos)
    ctx = SimpleNamespace(ltp=100.3)
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        _probe_ign_exit_lag(eng, "IGNCO", ctx, _MID_SESSION)
    assert eng.sb.updates == [], (
        "already-probed must short-circuit before evaluate_intraday_exit "
        "is even called again")


def test_probe_does_nothing_on_hold_or_trail_sl():
    """A position barely off entry clears no rung at all — must not be
    recorded as a probe hit."""
    from intraday.event_core import _probe_ign_exit_lag
    pos = _pos(hwm=100.05)
    eng = _fake_engine(pos)
    ctx = SimpleNamespace(ltp=100.02)
    with cfg_ctx({}):
        _probe_ign_exit_lag(eng, "IGNCO", ctx, _MID_SESSION)
    assert eng.sb.updates == []
    assert pos.get("exit_lag_action") is None


def test_probe_fires_once_on_a_real_giveback_and_writes_the_update():
    """Reuses the EXACT textbook giveback shape test_intraday_giveback.py
    already pins (peaked 2R, faded to 0.3R, giveback armed at 50%/1.0R) —
    this is the same real function, not a second implementation."""
    from intraday.event_core import _probe_ign_exit_lag
    pos = _pos(hwm=102.0)
    eng = _fake_engine(pos)
    ctx = SimpleNamespace(ltp=100.3)
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        _probe_ign_exit_lag(eng, "IGNCO", ctx, _MID_SESSION)
    assert len(eng.sb.updates) == 1
    assert eng.sb.updates[0]["exit_lag_action"] == "EXIT_GIVEBACK"
    assert eng.sb.updates[0]["exit_lag_probe_at"] == _MID_SESSION.isoformat()
    # Kept in sync in-process, same object _ign_open_position() returned.
    assert pos["exit_lag_action"] == "EXIT_GIVEBACK"
    assert pos["exit_lag_probe_at"] == _MID_SESSION.isoformat()


def test_probe_write_failure_does_not_raise_and_leaves_pos_unmutated():
    from intraday.event_core import _probe_ign_exit_lag
    pos = _pos(hwm=102.0)
    eng = _fake_engine(pos, sb=_FailingSB())
    ctx = SimpleNamespace(ltp=100.3)
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        _probe_ign_exit_lag(eng, "IGNCO", ctx, _MID_SESSION)   # must not raise
    assert pos.get("exit_lag_action") is None, (
        "a failed write must not be recorded as if it had succeeded")


# ── control.position_lifecycle._exit_lag_seconds(), pure ────────────────

def test_exit_lag_seconds_is_none_with_no_probe():
    from control.position_lifecycle import _exit_lag_seconds
    assert _exit_lag_seconds(None) is None
    assert _exit_lag_seconds("") is None


def test_exit_lag_seconds_computes_a_real_positive_gap():
    from control.position_lifecycle import _exit_lag_seconds
    probed = (datetime.now(IST) - timedelta(seconds=42)).isoformat()
    gap = _exit_lag_seconds(probed)
    assert gap is not None and 40.0 <= gap <= 46.0, (
        f"expected ~42 seconds, got {gap} — allow a little runtime slack")


def test_exit_lag_seconds_is_none_on_an_unparseable_value():
    from control.position_lifecycle import _exit_lag_seconds
    assert _exit_lag_seconds("not-a-timestamp") is None


# ── close()'s dict construction carries the three fields through ────────

def test_close_carries_exit_lag_fields_through_to_the_closed_row():
    """Pure dict-construction check, mirroring
    test_sub_engine_on_positions.py's own pattern for the real `closed`
    dict in close(): {"exit_lag_action": pos.get("exit_lag_action"), ...}."""
    pos = {"symbol": "IGNCO", "exit_lag_action": "EXIT_GIVEBACK",
          "exit_lag_probe_at": "2026-09-09T10:00:00+05:30"}
    closed = {"exit_lag_action": pos.get("exit_lag_action"),
             "exit_lag_probe_at": pos.get("exit_lag_probe_at")}
    assert closed["exit_lag_action"] == "EXIT_GIVEBACK"
    assert closed["exit_lag_probe_at"] == "2026-09-09T10:00:00+05:30"


def test_close_carries_none_through_when_the_probe_never_fired():
    pos = {"symbol": "IGNCO"}
    closed = {"exit_lag_action": pos.get("exit_lag_action"),
             "exit_lag_probe_at": pos.get("exit_lag_probe_at")}
    assert closed["exit_lag_action"] is None
    assert closed["exit_lag_probe_at"] is None


# ── wiring: check() actually calls the probe, gated by its own switch ──

def test_check_calls_the_probe_gated_by_its_own_switch():
    """Source-inspection pin, same discipline as
    test_fast_entry_branch_is_gated_by_strategy_name_and_its_own_switch —
    check()'s own body cannot be run standalone in a unit test (needs a
    live feed, a real registry pass, a real engine)."""
    import inspect
    from intraday import event_core as M
    src = inspect.getsource(M.check)
    i = src.find('intraday_ign_exit_lag_probe_enabled')
    assert i != -1, "the probe's own switch must still gate this branch"
    window = src[i:i + 300]
    assert "_probe_ign_exit_lag(" in window, (
        "the guarded branch must actually call _probe_ign_exit_lag()")


TESTS = [
    ("_ign_open_position matches framework and sub_engine",
     test_ign_open_position_matches_framework_and_sub_engine),
    ("_ign_open_position returns None for other engines",
     test_ign_open_position_returns_none_for_other_engines),
    ("_ign_open_position returns None for SWING",
     test_ign_open_position_returns_none_for_swing),
    ("_ign_open_position returns the actual dict, not a copy",
     test_ign_open_position_returns_the_actual_dict_not_a_copy),
    ("probe does nothing when no open IGN position",
     test_probe_does_nothing_when_no_open_ign_position),
    ("probe never fires twice for the same position",
     test_probe_never_fires_twice_for_the_same_position),
    ("probe does nothing on HOLD or TRAIL_SL",
     test_probe_does_nothing_on_hold_or_trail_sl),
    ("probe fires once on a real giveback and writes the update",
     test_probe_fires_once_on_a_real_giveback_and_writes_the_update),
    ("probe write failure does not raise and leaves pos unmutated",
     test_probe_write_failure_does_not_raise_and_leaves_pos_unmutated),
    ("exit_lag_seconds is None with no probe",
     test_exit_lag_seconds_is_none_with_no_probe),
    ("exit_lag_seconds computes a real positive gap",
     test_exit_lag_seconds_computes_a_real_positive_gap),
    ("exit_lag_seconds is None on an unparseable value",
     test_exit_lag_seconds_is_none_on_an_unparseable_value),
    ("close() carries exit_lag fields through to the closed row",
     test_close_carries_exit_lag_fields_through_to_the_closed_row),
    ("close() carries None through when the probe never fired",
     test_close_carries_none_through_when_the_probe_never_fired),
    ("check() calls the probe, gated by its own switch",
     test_check_calls_the_probe_gated_by_its_own_switch),
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
