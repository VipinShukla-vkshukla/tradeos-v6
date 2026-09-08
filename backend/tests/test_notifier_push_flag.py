"""
intraday/notifier.py — Action.push, 08-Sep-2026.

WHY THIS EXISTS
----------------
The operator asked twice not to be interrupted by anything short of a real
change to a position — buy, sell, exit, partial, stop hit, trail update.
"Allocator declined OIL" and "allocator is holding PETRONET's slot for
BEML" are both true and both worth recording, but neither is a trade event:
nothing was bought, sold, or changed. `Action.push` (default True) lets a
caller keep an alert in the same material-change-gated pipeline — same
dedup, same dashboard row, same audit trail — while opting it out of the
one channel meant for interruptions.

This exercises `Notifier.send()` directly: `push=True` still calls
`_deliver()` (the network hop); `push=False` skips it but still writes the
dashboard row and still updates the dedup memory, so a later `push=True`
alert for the same (symbol, kind) is compared against it exactly as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from tests import cfg_ctx


class _FakeAlertsTable:
    def __init__(self, store: list):
        self._store = store

    def insert(self, row):
        self._store.append(dict(row))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeSB:
    def __init__(self):
        self.alerts: list = []

    def table(self, name):
        assert name == "intraday_alerts"
        return _FakeAlertsTable(self.alerts)


def _notifier(sb):
    from intraday.notifier import Notifier
    return Notifier(sb)


def test_push_true_delivers_and_returns_the_delivery_result():
    from intraday.notifier import Action
    sb = _FakeSB()
    with cfg_ctx(), patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        ok = _notifier(sb).send(Action(symbol="OIL", kind="ENTRY", headline="BUY — in zone"))
    deliver_mock.assert_called_once()
    assert ok is True
    assert len(sb.alerts) == 1, "dashboard row must still be written"


def test_push_false_skips_delivery_but_still_records_and_dedupes():
    from intraday.notifier import Action
    sb = _FakeSB()
    with cfg_ctx(), patch("intraday.notifier.Notifier._deliver") as deliver_mock:
        notifier = _notifier(sb)
        ok = notifier.send(Action(symbol="OIL", kind="ENTRY_DECLINED",
                                  headline="Allocator declined — edge -0.005 below the bar 0.015",
                                  urgency="INFO", push=False))
    deliver_mock.assert_not_called()
    assert ok is True, "a deliberately unpushed alert is not a delivery failure"
    assert len(sb.alerts) == 1, "must still land on the dashboard's audit trail"
    assert "OIL:ENTRY_DECLINED" in notifier._last, "must still update the dedup memory"


def test_push_false_still_gated_by_material_change():
    """A repeat of the SAME unpushed alert must not write a second dashboard
    row any more than a pushed one would — the gate is orthogonal to push."""
    from intraday.notifier import Action
    sb = _FakeSB()
    with cfg_ctx(), patch("intraday.notifier.Notifier._deliver"):
        notifier = _notifier(sb)
        notifier.send(Action(symbol="OIL", kind="ENTRY_DECLINED",
                             headline="Allocator declined — edge -0.005 below the bar 0.015",
                             urgency="INFO", push=False))
        sent_again = notifier.send(Action(symbol="OIL", kind="ENTRY_DECLINED",
                                          headline="Allocator declined — edge -0.005 below the bar 0.015",
                                          urgency="INFO", push=False))
    assert sent_again is False, "an unchanged DECLINE must still be held by the rearm gate"
    assert len(sb.alerts) == 1


TESTS = [
    ("push=True delivers and returns the delivery result",
     test_push_true_delivers_and_returns_the_delivery_result),
    ("push=False skips delivery but still records and dedupes",
     test_push_false_skips_delivery_but_still_records_and_dedupes),
    ("push=False is still gated by material change",
     test_push_false_still_gated_by_material_change),
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
