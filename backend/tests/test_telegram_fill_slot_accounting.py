"""
Phase 2a of the swing framework evolution blueprint, 26-Aug-2026.

control/execution_engine.py::place_order() — the function that runs when the
operator approves a swing signal via a Telegram button — used to call
kite.place_order() directly: no preflight() (no per-order cap, no daily
count/notional cap, no combined-account guard, no broker-cash check, no
duplicate-order window), and no swing_auto_entry/swing_live_auto_entry check
— the two-switch live-money gate every OTHER swing entry in this system
respects before spending real money. It also wrote a malformed
open_positions row missing product/framework/mode, a real corruption risk
given the table is keyed on (symbol, product) since migration 028.

Rebuilt to route through execution.order_manager.place() (so preflight()'s
caps/checks apply) and control.position_lifecycle._upsert_position() (so the
position row matches the shape every other swing entry writes) — the same
two functions intraday/engine.py::_maybe_enter_swing already uses. These
tests exercise the two-switch gate offline (pure logic, no DB/broker calls
reached) and, with the I/O boundary mocked, confirm a fully-armed approval
actually reaches and uses those two shared functions rather than a private
reimplementation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests import cfg_ctx


def _sizing(qty=10, stop=95.0, invested=1000.0):
    return SimpleNamespace(quantity=qty, stop_loss=stop, invested_value=invested)


def test_blocked_when_swing_auto_entry_is_off():
    """Neither switch armed — must block before any risk/sizing/order call."""
    from control.execution_engine import place_order
    with cfg_ctx({"master_kill_switch": "false", "swing_auto_entry": "false",
                  "swing_live_auto_entry": "false"}):
        with patch("control.risk_manager.check_portfolio_risk") as risk_mock:
            result = place_order({"symbol": "X", "entry_price": 100.0})
    assert result["success"] is False
    assert "swing_auto_entry" in result["error"]
    risk_mock.assert_not_called(), (
        "must block BEFORE reaching the risk/sizing/order path, not after")


def test_blocked_when_only_first_switch_is_on():
    """swing_auto_entry alone must not be enough — this is the exact
    two-switch discipline the daemon's own _maybe_enter_swing already
    enforces; a human clicking Approve is not exempt from it."""
    from control.execution_engine import place_order
    with cfg_ctx({"master_kill_switch": "false", "swing_auto_entry": "true",
                  "swing_live_auto_entry": "false"}):
        with patch("control.risk_manager.check_portfolio_risk") as risk_mock:
            result = place_order({"symbol": "X", "entry_price": 100.0})
    assert result["success"] is False
    assert "swing_live_auto_entry" in result["error"]
    risk_mock.assert_not_called()


def test_armed_approval_routes_through_order_manager_and_upsert_position():
    """Both switches on, everything downstream mocked at the I/O boundary
    (matching this project's own precedent — Stage E7's own tests fake only
    the Supabase client and Kite session, never the decision functions
    themselves). Confirms place_order() actually calls
    execution.order_manager.place() and control.position_lifecycle.
    _upsert_position() — the same two functions the daemon's own
    _maybe_enter_swing uses — with framework=SWING/product=CNC, rather than
    a second, private order-placement implementation."""
    from control.execution_engine import place_order

    fake_result = SimpleNamespace(ok=True, order_id="ORD-123", message="ok")

    with cfg_ctx({"master_kill_switch": "false", "swing_auto_entry": "true",
                  "swing_live_auto_entry": "true", "autonomy_phase": "4"}), \
         patch("control.execution_engine.check_portfolio_risk",
               return_value={"eligible": True, "reason": ""}), \
         patch("control.execution_engine.calculate_position_size",
               return_value=_sizing()), \
         patch("control.execution_engine.get_supabase") as sb_mock, \
         patch("execution.order_manager.place", return_value=fake_result) as place_mock, \
         patch("control.position_lifecycle._upsert_position") as upsert_mock:
        sb_mock.return_value.table.return_value.update.return_value.eq.return_value.eq.return_value.execute.return_value = None
        result = place_order({"symbol": "X", "entry_price": 100.0,
                              "strategy": "CTL", "sector": "IT", "id": 42,
                              "date": "2026-08-26"},
                             approved_by="TELEGRAM")

    assert result["success"] is True
    assert result["order_id"] == "ORD-123"

    place_mock.assert_called_once()
    req = place_mock.call_args.args[0]
    assert req.symbol == "X" and req.side == "BUY" and req.quantity == 10
    assert place_mock.call_args.kwargs.get("framework") == "SWING"

    upsert_mock.assert_called_once()
    row = upsert_mock.call_args.args[1]
    assert row["framework"] == "SWING" and row["product"] == "CNC"
    assert row["status"] == "PENDING_FILL", (
        "res.ok only means Kite accepted the order, not that it filled — "
        "must not mark ACTIVE before confirmation, same rule "
        "_maybe_enter_swing follows")
    assert row["entry_order_id"] == "ORD-123"


def test_order_manager_decline_is_surfaced_not_swallowed():
    """preflight() refusing (a cap, a duplicate window, insufficient cash)
    must come back as a clear failure, not a silent no-op or a raised
    exception the Telegram handler has to guess about."""
    from control.execution_engine import place_order

    declined = SimpleNamespace(ok=False, order_id=None,
                               message="₹50,000 committed today; this order "
                                       "would breach the daily notional cap")

    with cfg_ctx({"master_kill_switch": "false", "swing_auto_entry": "true",
                  "swing_live_auto_entry": "true", "autonomy_phase": "4"}), \
         patch("control.execution_engine.check_portfolio_risk",
               return_value={"eligible": True, "reason": ""}), \
         patch("control.execution_engine.calculate_position_size",
               return_value=_sizing()), \
         patch("control.execution_engine.get_supabase"), \
         patch("execution.order_manager.place", return_value=declined), \
         patch("control.position_lifecycle._upsert_position") as upsert_mock:
        result = place_order({"symbol": "X", "entry_price": 100.0})

    assert result["success"] is False
    assert "daily notional cap" in result["error"]
    upsert_mock.assert_not_called(), "a declined order must never write a position row"


TESTS = [
    ("blocked when swing_auto_entry is off", test_blocked_when_swing_auto_entry_is_off),
    ("blocked when only the first switch is on", test_blocked_when_only_first_switch_is_on),
    ("armed approval routes through order_manager + _upsert_position",
     test_armed_approval_routes_through_order_manager_and_upsert_position),
    ("order_manager decline is surfaced, not swallowed",
     test_order_manager_decline_is_surfaced_not_swallowed),
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
