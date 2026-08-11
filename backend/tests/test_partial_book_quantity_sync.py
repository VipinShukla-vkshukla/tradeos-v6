"""
A partial book leaving actual_qty/kite_qty stale forever (11-Aug-2026).

WHAT THIS CATCHES
------------------
PPLPHARMA booked 5 of 11 shares on 10-Aug. The write that recorded it
(IntradayEngine._auto_exit, BOOK_PARTIAL branch) touched current_qty alone
(6, correctly) and left actual_qty/kite_qty at the pre-partial 11 — and
they STAYED there, confirmed against the live broker holding a full day
later (broker: 6 real shares; DB: actual_qty=11, kite_qty=11). The reason
it never self-corrected: control.position_lifecycle's reconcile "already
matches" fast path compared ONLY current_qty against the broker, found
agreement (current_qty was already right), and declared
reconcile_status=MATCHED without ever looking at the other two fields —
and the frontend's own mismatch banner is gated on
`reconcile_status != 'MATCHED'`, so a false MATCHED did not just miss the
drift, it actively hid a real one on the same row.

These tests pin: BOOK_PARTIAL's write now sets actual_qty/kite_qty
alongside current_qty, on BOTH the PAPER and LIVE paths; reconcile's fast
path now re-syncs actual_qty/kite_qty from the broker even when
current_qty already agrees with it (proven with the EXACT PPLPHARMA
numbers: current_qty=6 already matches a broker holding of 6, while
actual_qty/kite_qty sit at the stale 11); and tools.health's new
qty_fields check flags the drifted shape and clears once fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import patch

from tests import cfg_ctx


# ── _auto_exit()'s BOOK_PARTIAL branch ──────────────────────────────────────

def _position(current_qty=11, actual_qty=11, framework="SWING", mode="LIVE",
              product="CNC", symbol="PPLPHARMA", partial_booked_qty=0):
    return {"symbol": symbol, "framework": framework, "mode": mode,
            "product": product, "current_qty": current_qty,
            "actual_qty": actual_qty, "kite_qty": actual_qty,
            "partial_booked_qty": partial_booked_qty,
            "planned_stop": 190.0, "direction": "LONG"}


def _decision(action="BOOK_PARTIAL", book_qty=5, new_sl=None):
    d = {"action": action, "book_qty": book_qty,
        "reason": "target reached", "detail": "1.13R >= target"}
    if new_sl is not None:
        d["new_sl"] = new_sl
    return d


def _mk_engine():
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.captured_updates = []
    eng._update_position = lambda p, upd: eng.captured_updates.append(upd)
    eng.load_state = lambda: None
    eng.sb = None
    eng.notifier = None
    return eng


def test_live_book_partial_syncs_actual_and_kite_qty():
    """The exact PPLPHARMA shape: 11 shares, book 5, 6 left."""
    from execution.order_manager import OrderResult
    eng = _mk_engine()
    pos = _position(current_qty=11, actual_qty=11)
    with patch("execution.order_manager.place",
              return_value=OrderResult(ok=True, order_id="1", message="ok")), \
         patch("execution.gates.is_paper", return_value=False), \
         cfg_ctx({}):
        eng._auto_exit(pos, _decision(book_qty=5), ltp=214.60)
    assert len(eng.captured_updates) == 1
    upd = eng.captured_updates[0]
    assert upd["current_qty"] == 6
    assert upd["actual_qty"] == 6, (
        f"got {upd.get('actual_qty')} — actual_qty must shrink WITH "
        f"current_qty, not be left at the pre-partial value")
    assert upd["kite_qty"] == 6, f"got {upd.get('kite_qty')}"


def test_paper_book_partial_syncs_actual_and_kite_qty():
    """No broker exists to correct a PAPER position's mirror fields
    afterward (reconcile skips PAPER rows) — this write is the ONLY chance
    to get it right, so the paper path needs the identical fix."""
    eng = _mk_engine()
    pos = _position(current_qty=20, actual_qty=20, mode="PAPER", framework="INTRADAY")
    with patch("execution.gates.is_paper", return_value=True), cfg_ctx({}):
        eng._auto_exit(pos, _decision(book_qty=8), ltp=100.0)
    assert len(eng.captured_updates) == 1
    upd = eng.captured_updates[0]
    assert upd["current_qty"] == 12
    assert upd["actual_qty"] == 12
    assert upd["kite_qty"] == 12


def test_full_exit_is_unaffected_by_this_change():
    """EXIT_* actions route through close_position/current_qty=0, not the
    partial branch — must not be touched by this fix."""
    from execution.order_manager import OrderResult
    eng = _mk_engine()
    pos = _position(current_qty=9, actual_qty=9)
    with patch("execution.order_manager.place",
              return_value=OrderResult(ok=True, order_id="1", message="ok")), \
         patch("execution.gates.is_paper", return_value=False), \
         cfg_ctx({}):
        eng._auto_exit(pos, _decision(action="EXIT_GIVEBACK", book_qty=0), ltp=312.5)
    assert len(eng.captured_updates) == 1
    upd = eng.captured_updates[0]
    assert upd["current_qty"] == 0
    assert upd["status"] == "CLOSING"
    assert "actual_qty" not in upd, (
        "a full exit does not need actual_qty/kite_qty touched here — "
        "close_position/reconcile own that path")


# ── _mirror_qty_drift() — the real function, not a reimplementation ────────

def test_mirror_qty_drift_catches_the_exact_pplpharma_shape():
    """current_qty (6) already equals the broker (6) — the fast path this
    function guards is only reachable in exactly this state. Before the
    fix, reconcile_with_broker() stopped looking the moment current_qty
    agreed and wrote nothing else; this is the function that now looks
    further, called DIRECTLY (not reimplemented) so a regression here is
    caught by the same code path production runs."""
    from control.position_lifecycle import _mirror_qty_drift
    pos = {"current_qty": 6, "actual_qty": 11, "kite_qty": 11}
    drift = _mirror_qty_drift(pos, br_qty=6)
    assert drift == {"actual_qty": 6, "kite_qty": 6}


def test_mirror_qty_drift_is_empty_when_everything_already_agrees():
    from control.position_lifecycle import _mirror_qty_drift
    pos = {"current_qty": 6, "actual_qty": 6, "kite_qty": 6}
    assert _mirror_qty_drift(pos, br_qty=6) == {}


def test_mirror_qty_drift_catches_only_the_field_that_actually_drifted():
    from control.position_lifecycle import _mirror_qty_drift
    pos = {"current_qty": 6, "actual_qty": 6, "kite_qty": 11}
    assert _mirror_qty_drift(pos, br_qty=6) == {"kite_qty": 6}


def test_mirror_qty_drift_treats_a_missing_field_as_zero_not_a_crash():
    from control.position_lifecycle import _mirror_qty_drift
    pos = {"current_qty": 6}   # actual_qty/kite_qty absent entirely
    assert _mirror_qty_drift(pos, br_qty=6) == {"actual_qty": 6, "kite_qty": 6}


# ── tools.health's new check ─────────────────────────────────────────────────

class _HealthFakeQuery:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self):
        self.data = self._rows
        return self


class _HealthFakeSB:
    def __init__(self, rows): self._rows = rows
    def table(self, name):
        assert name == "open_positions"
        return _HealthFakeQuery(self._rows)


def test_health_check_flags_the_drifted_shape():
    from tools.health import check_quantity_fields
    rows = [{"symbol": "PPLPHARMA", "framework": "SWING", "mode": "LIVE",
            "status": "ACTIVE", "current_qty": 6, "actual_qty": 11,
            "kite_qty": 11, "reconcile_status": "MATCHED"}]
    with patch("config.get_supabase", return_value=_HealthFakeSB(rows)):
        ok, why = check_quantity_fields()
    assert ok is False
    assert "PPLPHARMA" in why and "current_qty=6" in why and "actual_qty=11" in why


def test_health_check_passes_once_fields_agree():
    from tools.health import check_quantity_fields
    rows = [{"symbol": "PPLPHARMA", "framework": "SWING", "mode": "LIVE",
            "status": "ACTIVE", "current_qty": 6, "actual_qty": 6,
            "kite_qty": 6, "reconcile_status": "MATCHED"}]
    with patch("config.get_supabase", return_value=_HealthFakeSB(rows)):
        ok, why = check_quantity_fields()
    assert ok is True


def test_health_check_handles_no_open_positions():
    from tools.health import check_quantity_fields
    with patch("config.get_supabase", return_value=_HealthFakeSB([])):
        ok, why = check_quantity_fields()
    assert ok is True


TESTS = [
    ("LIVE book-partial syncs actual_qty/kite_qty",
     test_live_book_partial_syncs_actual_and_kite_qty),
    ("PAPER book-partial syncs actual_qty/kite_qty",
     test_paper_book_partial_syncs_actual_and_kite_qty),
    ("a full exit is unaffected by this change",
     test_full_exit_is_unaffected_by_this_change),
    ("mirror_qty_drift catches the exact PPLPHARMA shape",
     test_mirror_qty_drift_catches_the_exact_pplpharma_shape),
    ("mirror_qty_drift is empty when everything already agrees",
     test_mirror_qty_drift_is_empty_when_everything_already_agrees),
    ("mirror_qty_drift catches only the field that actually drifted",
     test_mirror_qty_drift_catches_only_the_field_that_actually_drifted),
    ("mirror_qty_drift treats a missing field as zero, not a crash",
     test_mirror_qty_drift_treats_a_missing_field_as_zero_not_a_crash),
    ("health check flags the drifted shape", test_health_check_flags_the_drifted_shape),
    ("health check passes once fields agree", test_health_check_passes_once_fields_agree),
    ("health check handles no open positions", test_health_check_handles_no_open_positions),
]
