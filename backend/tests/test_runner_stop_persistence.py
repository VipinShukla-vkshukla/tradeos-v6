"""
The runner-stop persistence gap: manage_open_positions() (the once-a-day
batch, control/position_lifecycle.py) already persists a RUN decision's
widened stop to active_sl. The live 15s daemon (intraday/engine.py)
forwarded runner TELEMETRY (runner_evidence/runner_verdict/runner_since_r,
F-46, 21-Aug-2026) but never the stop itself — a position converted to a
runner mid-session had its broker-side GTT still pinned to the pre-runner
stop until the next day's batch caught up, for as long as the market
stayed open and the position was carrying its largest open profit of the
trade.

IntradayEngine._runner_stop_update() is the pure decision both the live
loop and this test exercise directly, mirroring position_lifecycle.py's
own `elif act == "RUN":` branch so the two paths cannot drift apart again.
"""

from __future__ import annotations


def test_run_with_new_sl_persists_active_sl():
    from intraday.engine import IntradayEngine
    d = {"action": "RUN", "new_sl": 542.10, "reason": "TREND_INTACT"}
    upd = IntradayEngine._runner_stop_update(d)
    assert upd == {"active_sl": 542.10, "trail_activated": True}


def test_run_with_new_sl_none_persists_nothing():
    # exit_rules.py's own never-loosen guard already decided the runner's
    # stop isn't moving this cycle — new_sl is None, not omitted.
    from intraday.engine import IntradayEngine
    d = {"action": "RUN", "new_sl": None, "reason": "TREND_INTACT"}
    assert IntradayEngine._runner_stop_update(d) is None


def test_run_with_missing_new_sl_key_persists_nothing():
    from intraday.engine import IntradayEngine
    d = {"action": "RUN", "reason": "TREND_INTACT"}
    assert IntradayEngine._runner_stop_update(d) is None


def test_trail_sl_action_not_handled_here():
    # TRAIL_SL has its own, separate persistence block in act_on_positions
    # — this function must stay silent for it, not double-write.
    from intraday.engine import IntradayEngine
    d = {"action": "TRAIL_SL", "new_sl": 542.10, "reason": "TRAIL_UPDATE"}
    assert IntradayEngine._runner_stop_update(d) is None


def test_hold_action_persists_nothing():
    from intraday.engine import IntradayEngine
    d = {"action": "HOLD"}
    assert IntradayEngine._runner_stop_update(d) is None


TESTS = [
    ("RUN with new_sl -> active_sl + trail_activated persisted",
     test_run_with_new_sl_persists_active_sl),
    ("RUN with new_sl=None (never-loosen guard) -> nothing persisted",
     test_run_with_new_sl_none_persists_nothing),
    ("RUN with new_sl key missing -> nothing persisted",
     test_run_with_missing_new_sl_key_persists_nothing),
    ("TRAIL_SL is not RUN's job -> nothing persisted here",
     test_trail_sl_action_not_handled_here),
    ("HOLD -> nothing persisted",
     test_hold_action_persists_nothing),
]
