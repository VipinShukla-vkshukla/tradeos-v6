"""
control/position_lifecycle.py::send_action_alerts — material-change gate,
2026-09-08.

THE GAP THIS CLOSES
--------------------
pipeline_intraday.yml's cron calls `position_lifecycle --manage-only
--require-live` every 30 minutes, independently of whether intraday/
engine.py's daemon is running. The daemon evaluates the SAME swing exits on
live ticks and alerts through Notifier, which only speaks on a genuine
transition (see intraday/notifier.py's own module docstring). This cron used
to build its own "Position Actions Required" digest and send it unconditionally
on every run with any SELLABLE action pending — with swing_auto_exit off by
default (execution/gates.py), a position sitting in EXIT_STOP or BOOK_PARTIAL
waiting for the operator to act manually re-announced the IDENTICAL
instruction every half hour for as long as it stayed unactioned. That is the
"alert for every single action instead of material changes" complaint.

Two independent fixes, mirroring patterns this codebase already uses
elsewhere:
  1. Same "second actor" lease check control/candidate_monitor.py already
     applies to entry alerts (test_candidate_monitor_lease_fallback.py) — while
     the daemon's lease is healthy, this cron stays silent; the daemon already
     covers it, on better prices, with its own gate.
  2. When the daemon IS down (the real fallback case), route through the same
     Notifier the daemon uses instead of a bespoke digest, and rehydrate its
     `_last` dedup map from `intraday_alerts` — the table both already write
     to — so a repeat cron run inherits the SAME material-change/rearm
     comparison a long-lived process gets for free, instead of starting cold
     every 30 minutes and treating an unchanged instruction as brand new. Same
     idea as intraday/engine.py's own `_rehydrate_recorded()`, applied to this
     map.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from tests import cfg_ctx
from config import IST
import datetime as _dt


def _view(held_by_other, holder="tradeos-vcn", hostname="tradeos-vcn", detail="ok"):
    return SimpleNamespace(holder=holder, hostname=hostname,
                           held_by_other=held_by_other, detail=detail)


class _FakeAlertsTable:
    """Enough of the query builder for `intraday_alerts`' two real uses here:
    the rehydration read (.select().in_().order().limit().execute()) and the
    Notifier dashboard write (.insert().execute()). Rows are a list shared
    across every `.table()` call on the same fake sb, exactly like a real
    table persists across separate PostgREST requests."""

    def __init__(self, store: list):
        self._store = store
        self._filters: dict = {}

    def select(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self._filters[col] = set(vals)
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, row):
        self._store.append(dict(row))
        return self

    def execute(self):
        allowed = self._filters.get("symbol")
        rows = [r for r in self._store if allowed is None or r.get("symbol") in allowed]
        rows = sorted(rows, key=lambda r: r["ts"], reverse=True)
        return SimpleNamespace(data=rows)


class _FakeSB:
    def __init__(self):
        self.alerts: list = []

    def table(self, name):
        assert name == "intraday_alerts", f"unexpected table access: {name}"
        return _FakeAlertsTable(self.alerts)


def _action(symbol="SBIN", action="EXIT_STOP", detail="Stop breached @ 795.00",
           reason="EXIT_STOP", ltp=795.0, r=-1.0):
    return {"symbol": symbol, "action": action, "reason": reason,
            "detail": detail, "ltp": ltp, "r": r}


def test_no_urgent_actions_never_reaches_the_lease_check():
    """A HOLD-only actions list has nothing worth alerting on — must not even
    call observe(), matching candidate_monitor's identical discipline."""
    from control.position_lifecycle import send_action_alerts
    with cfg_ctx(), \
         patch("intraday.lease.observe") as observe_mock:
        send_action_alerts([{"symbol": "SBIN", "action": "HOLD",
                             "reason": "x", "detail": "x", "ltp": 100.0, "r": 0.0}],
                           sb=_FakeSB())
    observe_mock.assert_not_called()


def test_healthy_daemon_suppresses_the_cron_alert():
    """The exact repeat-noise case this closes: daemon up, cron also runs —
    the cron must not duplicate what the daemon already sent live."""
    from control.position_lifecycle import send_action_alerts
    sb = _FakeSB()
    with cfg_ctx(), \
         patch("intraday.lease.observe", return_value=_view(held_by_other=True)), \
         patch("intraday.notifier.Notifier._deliver") as deliver_mock:
        send_action_alerts([_action()], sb=sb)
    deliver_mock.assert_not_called()
    assert sb.alerts == [], "dashboard row written even though the daemon already covers this"


def test_daemon_down_fires_through_the_notifier():
    """The real outage case: daemon down, so the operator must not go blind —
    the cron fires, through the shared Notifier (dashboard row included)."""
    from control.position_lifecycle import send_action_alerts
    sb = _FakeSB()
    with cfg_ctx(), \
         patch("intraday.lease.observe", return_value=_view(held_by_other=False)), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        send_action_alerts([_action()], sb=sb)
    deliver_mock.assert_called_once()
    assert len(sb.alerts) == 1, "the sent action must land on the dashboard rail"
    assert sb.alerts[0]["symbol"] == "SBIN"
    assert sb.alerts[0]["kind"] == "EXIT_STOP"


def test_repeat_cron_run_does_not_restate_an_unchanged_instruction():
    """THE BUG, reproduced and closed. Two separate `send_action_alerts` calls
    — a fresh process each time, exactly like the GitHub Actions cron — for
    the SAME still-pending EXIT_STOP. Before this fix there was no persisted
    memory at all, so the second call re-sent the identical alert. Now the
    second call rehydrates from the first call's own `intraday_alerts` row
    and the material-change gate holds it back."""
    from control.position_lifecycle import send_action_alerts
    sb = _FakeSB()
    with cfg_ctx(), \
         patch("intraday.lease.observe", return_value=_view(held_by_other=False)), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        send_action_alerts([_action()], sb=sb)     # cron run #1
        send_action_alerts([_action()], sb=sb)     # cron run #2, seconds later
    assert deliver_mock.call_count == 1, (
        f"expected exactly 1 delivery across two identical runs, got "
        f"{deliver_mock.call_count} — the same unchanged EXIT_STOP was "
        f"restated instead of held by the material-change gate")
    assert len(sb.alerts) == 1, "a second, redundant dashboard row was written"


def test_a_genuinely_different_action_still_alerts_on_the_second_run():
    """The gate must not swallow real news. A different symbol (or a
    materially different instruction) must never be treated as a repeat."""
    from control.position_lifecycle import send_action_alerts
    sb = _FakeSB()
    with cfg_ctx(), \
         patch("intraday.lease.observe", return_value=_view(held_by_other=False)), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        send_action_alerts([_action(symbol="SBIN")], sb=sb)
        send_action_alerts([_action(symbol="INFY", detail="Stop breached @ 1500.00",
                                    ltp=1500.0)], sb=sb)
    assert deliver_mock.call_count == 2, (
        "a second position's genuinely distinct exit was suppressed alongside "
        "the first — the gate is keyed on symbol+kind, not firing blind")


def test_trail_sl_alerts_even_though_it_places_no_order():
    """TRAIL_SL/RUN are not in SELLABLE (manage_open_positions() persists
    them unconditionally, with no order to place), but the operator
    explicitly asked to hear about a trail update — this must still reach
    the Notifier, at NORMAL urgency, not be dropped as if it were HOLD."""
    from control.position_lifecycle import send_action_alerts
    sb = _FakeSB()
    trail = _action(action="TRAIL_SL", detail="Trail SL 780.00 -> 802.00",
                    reason="TRAIL", ltp=830.0, r=1.4)
    with cfg_ctx(), \
         patch("intraday.lease.observe", return_value=_view(held_by_other=False)), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        send_action_alerts([trail], sb=sb)
    deliver_mock.assert_called_once()
    assert sb.alerts[0]["kind"] == "TRAIL_SL"
    assert sb.alerts[0]["urgency"] == "NORMAL", "a stop trail is not an EXIT — must not read CRITICAL"


def test_rehydrate_seeds_from_the_most_recent_row_per_key():
    """Two prior rows for the same (symbol, kind) at different times — the
    rehydration must keep the LATEST, not the first one read, since
    `.order(desc=True)` means newest-first and only the newest is real."""
    from control.position_lifecycle import _rehydrate_notifier_memory
    from intraday.notifier import Notifier
    now = _dt.datetime.now(IST)
    sb = _FakeSB()
    sb.alerts.extend([
        {"symbol": "SBIN", "kind": "EXIT_STOP", "headline": "old headline",
         "ts": (now - timedelta(minutes=40)).isoformat()},
        {"symbol": "SBIN", "kind": "EXIT_STOP", "headline": "new headline",
         "ts": (now - timedelta(minutes=1)).isoformat()},
    ])
    notifier = Notifier(sb)
    _rehydrate_notifier_memory(notifier, sb, ["SBIN"])
    headline, ts = notifier._last["SBIN:EXIT_STOP"]
    assert headline == "new headline", (
        f"rehydration kept the stale row ({headline!r}) instead of the latest one")


TESTS = [
    ("no urgent actions never reaches the lease check",
     test_no_urgent_actions_never_reaches_the_lease_check),
    ("healthy daemon suppresses the cron alert",
     test_healthy_daemon_suppresses_the_cron_alert),
    ("daemon down fires through the shared Notifier",
     test_daemon_down_fires_through_the_notifier),
    ("repeat cron run does not restate an unchanged instruction",
     test_repeat_cron_run_does_not_restate_an_unchanged_instruction),
    ("a genuinely different action still alerts on the second run",
     test_a_genuinely_different_action_still_alerts_on_the_second_run),
    ("trail SL alerts even though it places no order",
     test_trail_sl_alerts_even_though_it_places_no_order),
    ("rehydrate seeds from the most recent row per key",
     test_rehydrate_seeds_from_the_most_recent_row_per_key),
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
