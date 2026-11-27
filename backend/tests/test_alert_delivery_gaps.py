"""
Alert delivery gaps — rejected orders and a silent daemon.

Two holes this pins, both found in the alerting audit:

1. order_manager.place() pushed a CRITICAL alert on a successful fill but
   only LOGGED a rejection. An account-wide block is the worst case — it
   disables exits too — and was visible only in a log line after the fact.

2. The intraday daemon writes a heartbeat every 15 minutes but cannot detect
   its own death, and the dashboard tile that reads it is passive. The watcher
   runs OUTSIDE the daemon, so it can actually page when the writer stops.
   Its rearm is backed by intraday_alerts because a short-lived CLI process
   cannot use Notifier's in-memory dedup across invocations.

All of it is verified with fakes: no database, no broker, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from config import IST
from tests import cfg_ctx


class _CaptureNotifier:
    def __init__(self, raises: bool = False):
        self.calls: list = []
        self.raises = raises

    def send(self, action, force=False):
        if self.raises:
            raise RuntimeError("telegram is down")
        self.calls.append((action, force))
        return True


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *a, **k):
        return self

    def eq(self, key, value):
        self._rows = [r for r in self._rows if str(r.get(key)) == str(value)]
        return self

    def gte(self, key, value):
        self._rows = [r for r in self._rows if str(r.get(key, "")) >= str(value)]
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _WatchSB:
    def __init__(self, heartbeat_ts=None, alerts=None, holidays=()):
        self.heartbeat = [{"id": 1, "ts": heartbeat_ts}] if heartbeat_ts else []
        self.alerts = list(alerts or [])
        self.holidays = [{"date": d} for d in holidays]

    def table(self, name):
        if name == "intraday_heartbeat":
            return _Query(self.heartbeat)
        if name == "intraday_alerts":
            return _Query(self.alerts)
        if name == "nse_holidays":
            return _Query(self.holidays)
        raise AssertionError(f"unexpected table {name}")


NOW = datetime(2026, 11, 27, 10, 0, tzinfo=IST)   # a Friday, mid-session


def _rejection(error: str, notifier):
    from execution import order_manager as om
    req = om.OrderRequest("SBIN", "SELL", 10, "MARKET", reason="EXIT_STOP")
    with patch.object(om, "preflight", return_value=om.OrderResult(True, None, "ok")), \
         patch("execution.gates.is_paper", return_value=False), \
         patch.object(om, "_log"), \
         patch("kite.kite_client.get_kite", side_effect=RuntimeError(error)):
        return om.place(req, sb=object(), notifier=notifier, framework="SWING")


def test_order_rejection_pushes_a_critical_alert():
    n = _CaptureNotifier()
    res = _rejection("BROKER_ERROR: order rejected by broker", n)
    assert res.ok is False
    assert len(n.calls) == 1, "a rejected order must reach the same channel as a filled one"
    action, force = n.calls[0]
    assert action.kind == "ORDER_REJECTED"
    assert action.urgency == "CRITICAL"
    assert action.framework == "SWING"
    assert force is False, "repeated rejections must stay governed by the rearm gate"


def test_account_wide_block_is_named_as_such():
    n = _CaptureNotifier()
    _rejection("No IPs configured for your API key", n)
    action, _ = n.calls[0]
    assert action.kind == "ORDER_BLOCKED_ACCOUNT"
    assert "ACCOUNT-WIDE BLOCK" in action.detail
    assert "INCLUDING EXITS" in action.detail


def test_alert_failure_never_breaks_rejection_handling():
    res = _rejection("BROKER_ERROR: rejected", _CaptureNotifier(raises=True))
    assert res.ok is False, "the caller must still receive the broker failure"


# ── heartbeat watch ──────────────────────────────────────────────────────────

def _watch(sb, now=NOW):
    from intraday import heartbeat_watch
    n = _CaptureNotifier()
    with cfg_ctx({"intraday_heartbeat_stale_minutes": 20,
                  "intraday_heartbeat_rearm_minutes": 120}):
        result = heartbeat_watch.check(sb=sb, notifier=n, now=now)
    return result, n


def test_fresh_heartbeat_is_silent():
    sb = _WatchSB(heartbeat_ts=(NOW - timedelta(minutes=5)).isoformat())
    result, n = _watch(sb)
    assert n.calls == []
    assert result["reason"] == "heartbeat_fresh"


def test_stale_heartbeat_alerts_once():
    sb = _WatchSB(heartbeat_ts=(NOW - timedelta(minutes=45)).isoformat())
    result, n = _watch(sb)
    assert result["alerted"] is True
    assert len(n.calls) == 1
    action, force = n.calls[0]
    assert action.kind == "HEARTBEAT_STALE"
    assert action.urgency == "CRITICAL"
    assert action.framework == "INTRADAY"
    assert force is True, "cross-process rearm is handled by the DB check"


def test_recent_alert_suppresses_repeat():
    sb = _WatchSB(
        heartbeat_ts=(NOW - timedelta(minutes=45)).isoformat(),
        alerts=[{"kind": "HEARTBEAT_STALE",
                 "ts": (NOW - timedelta(minutes=10)).isoformat()}],
    )
    result, n = _watch(sb)
    assert n.calls == []
    assert result["reason"] == "recently_alerted"


def test_nse_holiday_is_silent():
    sb = _WatchSB(heartbeat_ts=(NOW - timedelta(minutes=45)).isoformat(),
                  holidays=["2026-11-27"])
    result, n = _watch(sb)
    assert n.calls == []
    assert result["reason"] == "nse_holiday"


def test_outside_market_hours_is_silent():
    sb = _WatchSB(heartbeat_ts=(NOW - timedelta(minutes=45)).isoformat())
    result, n = _watch(sb, now=datetime(2026, 11, 27, 8, 0, tzinfo=IST))
    assert n.calls == []
    assert result["reason"] == "outside_market_hours"


def test_no_heartbeat_row_is_not_manufactured_into_a_page():
    sb = _WatchSB()
    result, n = _watch(sb)
    assert n.calls == []
    assert result["reason"] == "no_heartbeat_row"


# ── target monitor now uses the shared broker session ────────────────────────

def test_target_monitor_uses_shared_kite_session():
    from control import position_target_monitor as ptm
    kite = SimpleNamespace(ltp=lambda instruments: {"NSE:SBIN": {"last_price": 845.5}})
    with patch("kite.kite_client.get_kite", return_value=kite) as get_kite:
        prices = ptm.get_live_prices(["SBIN"])
    assert get_kite.call_count == 1
    assert prices == {"SBIN": 845.5}


TESTS = [
    ("rejected order pushes a CRITICAL alert", test_order_rejection_pushes_a_critical_alert),
    ("account-wide block is named as such", test_account_wide_block_is_named_as_such),
    ("alert failure never breaks rejection handling", test_alert_failure_never_breaks_rejection_handling),
    ("fresh heartbeat is silent", test_fresh_heartbeat_is_silent),
    ("stale heartbeat alerts once", test_stale_heartbeat_alerts_once),
    ("recent alert suppresses repeat", test_recent_alert_suppresses_repeat),
    ("NSE holiday is silent", test_nse_holiday_is_silent),
    ("outside market hours is silent", test_outside_market_hours_is_silent),
    ("no heartbeat row is not manufactured into a page", test_no_heartbeat_row_is_not_manufactured_into_a_page),
    ("target monitor uses the shared Kite session", test_target_monitor_uses_shared_kite_session),
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
