"""
Phase 2b of the swing framework evolution blueprint, 26-Aug-2026.

control/candidate_monitor.py runs on a GitHub Actions cron, entirely outside
the daemon (intraday/engine.py) and its lease, evaluating the identical
"is this candidate buyable now" question with a FLAT min_rr_to_enter bar
that never scales for regime, unlike the daemon's regime_min_rr(). BLUEJET,
26-Aug: never once reached the daemon's own allocator scoring (zero rows in
allocation_decisions, ever) because it never cleared the daemon's
regime-scaled bar — yet this monitor alerted it repeatedly as BUY_NOW on its
looser flat one, all morning, for a trade this monitor has no way to place
anyway (it is alert-only).

`_daemon_lease_healthy()` and `_maybe_send_candidate_alerts()` close that
gap: while the daemon's lease is healthy, this monitor stays silent — the
daemon already covers it, better. Only a real daemon outage (lease stale or
absent) makes it fire, clearly labeled as a degraded fallback.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _view(held_by_other, holder="tradeos-vcn", hostname="tradeos-vcn", detail="ok"):
    return SimpleNamespace(holder=holder, hostname=hostname,
                           held_by_other=held_by_other, detail=detail)


def test_healthy_lease_reads_as_daemon_alive():
    from control.candidate_monitor import _daemon_lease_healthy
    assert _daemon_lease_healthy(_view(held_by_other=True)) is True


def test_no_lease_reads_as_daemon_down():
    from control.candidate_monitor import _daemon_lease_healthy
    assert _daemon_lease_healthy(_view(held_by_other=False)) is False


def test_unreadable_lease_reads_as_unhealthy_not_permissive():
    """A DB blip must not read as permission for this monitor to go quiet
    — the same 'over-watching is recoverable, silently watching nothing is
    not' rule this project applies elsewhere. intraday/lease.py's own
    observe() already returns held_by_other=False on its error path; this
    just confirms the boolean is read the safe direction, not inverted."""
    from control.candidate_monitor import _daemon_lease_healthy
    unreadable = _view(held_by_other=False, holder="", hostname="",
                       detail="lease unreadable: connection reset")
    assert _daemon_lease_healthy(unreadable) is False


def test_no_alerts_never_reaches_the_lease_check():
    """An empty alert list has nothing to gate — must not even call
    observe(), let alone send anything."""
    from control.candidate_monitor import _maybe_send_candidate_alerts
    with patch("intraday.lease.observe") as observe_mock, \
         patch("control.candidate_monitor._send") as send_mock:
        sent = _maybe_send_candidate_alerts(sb=None, alerts=[], source="kite")
    assert sent is False
    observe_mock.assert_not_called()
    send_mock.assert_not_called()


def test_healthy_daemon_suppresses_the_alert():
    """RKFORGE/BLUEJET-shaped case with the daemon up: this monitor must
    not duplicate what the daemon already sent."""
    from control.candidate_monitor import _maybe_send_candidate_alerts
    with patch("intraday.lease.observe", return_value=_view(held_by_other=True)), \
         patch("control.candidate_monitor._send") as send_mock:
        sent = _maybe_send_candidate_alerts(sb=None, alerts=[{"symbol": "BLUEJET"}],
                                            source="kite")
    assert sent is False
    send_mock.assert_not_called()


def test_daemon_down_fires_the_degraded_fallback():
    """The actual outage case this exists for: the daemon is not covering
    it, so the operator must not be left blind."""
    from control.candidate_monitor import _maybe_send_candidate_alerts
    with patch("intraday.lease.observe", return_value=_view(held_by_other=False)), \
         patch("control.candidate_monitor._send") as send_mock:
        sent = _maybe_send_candidate_alerts(sb=None, alerts=[{"symbol": "BLUEJET"}],
                                            source="kite")
    assert sent is True
    send_mock.assert_called_once()
    args, kwargs = send_mock.call_args
    assert kwargs.get("degraded") is True or (len(args) >= 3 and args[2] is True), (
        "the fallback message must be visibly labeled degraded, not "
        "presented as the normal live alert")


TESTS = [
    ("healthy lease reads as daemon alive", test_healthy_lease_reads_as_daemon_alive),
    ("no lease reads as daemon down", test_no_lease_reads_as_daemon_down),
    ("unreadable lease reads as unhealthy, not permissive",
     test_unreadable_lease_reads_as_unhealthy_not_permissive),
    ("no alerts never reaches the lease check", test_no_alerts_never_reaches_the_lease_check),
    ("healthy daemon suppresses the alert", test_healthy_daemon_suppresses_the_alert),
    ("daemon down fires the degraded fallback", test_daemon_down_fires_the_degraded_fallback),
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
