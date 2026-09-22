"""
intraday/notifier.py — Action.push and Action.restate_on_change.

WHY push EXISTS (08-Sep-2026)
------------------------------
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

WHY restate_on_change EXISTS (09-Sep-2026, JSWSTEEL)
------------------------------------------------------
A swing "still chaseable" alert recomputes its live R:R, gap-to-zone and
derived risk/invested amounts every cycle from a ticking price. None of
that arithmetic changes the actual recommendation until the KIND itself
changes — but `_material()`'s integer-rounding was tuned for prices, not a
ratio hovering in one narrow band, so consecutive cycles kept reading as
"genuinely different" and restating on `intraday_restate_minutes` (5 live)
instead of the long rearm window. Confirmed live: JSWSTEEL alerted 35+
times between 10:45 and 15:28 IST on one unbroken "CHASE OK". `Action.
restate_on_change=False` (default True) makes the "different material"
branch behave exactly like "same material" — governed by the long rearm
window regardless of what the numbers say.

WHY rearm EXISTS (23-Sep-2026, AUROPHARMA)
--------------------------------------------
restate_on_change=False closed the every-5-minutes case but left the
ordinary 45-minute rearm window running underneath it — a candidate that
spends a whole session "still approaching" now interrupts on a slower
timer instead of a fast one, but still on a timer, with no new information
at each tick. Confirmed live: AUROPHARMA's ENTRY_APPROACHING fired nine
times in one session, exactly 45 minutes apart, for a buy limit it never
durably crossed; its ENTRY ("BUY — in zone") alert fired twice, 84 minutes
apart, and never once resulted in a position. `Action.rearm=False`
(default True) means: once sent, this (symbol, kind) never sends again
today, full stop — no rearm at all, identical headline or not. A genuine
change still interrupts immediately, because it is a DIFFERENT kind with
no prior entry in `_last`.
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


def test_restate_on_change_false_holds_a_differently_worded_repeat():
    """THE BUG, reproduced and closed. Two sends for the same (symbol, kind)
    with genuinely different-looking headlines (R:R/qty/risk drifting from a
    ticking price) — default behaviour (restate_on_change=True, the
    exit-alert case) would restate on intraday_restate_minutes; with it
    False, the second send must be held exactly like an unchanged repeat."""
    from intraday.notifier import Action
    sb = _FakeSB()
    # Headlines chosen so _material() rounds them to DIFFERENT text (0/3 vs
    # 0/2 below) — restate_minutes=0 means the old "different material"
    # branch would allow an immediate resend; rearm_minutes=999 means the
    # "treat as unchanged" branch this field forces will not. The two
    # settings are what make this test actually discriminate the fix from
    # its absence, rather than both paths agreeing by the accident of a
    # zero-second gap between sends.
    with cfg_ctx({"intraday_restate_minutes": 0, "intraday_rearm_minutes": 999}), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        notifier = _notifier(sb)
        notifier.send(Action(symbol="JSWSTEEL", kind="ENTRY",
                             headline="JSWSTEEL: CHASE OK — 0.2% above zone, R:R still 2.59",
                             restate_on_change=False))
        sent_again = notifier.send(Action(symbol="JSWSTEEL", kind="ENTRY",
                                          headline="JSWSTEEL: CHASE OK — 0.5% above zone, R:R still 2.20",
                                          restate_on_change=False))
    assert deliver_mock.call_count == 1, (
        f"expected exactly 1 delivery — restate_on_change=False must ignore "
        f"the differently-rounded headline, got {deliver_mock.call_count}")
    assert sent_again is False


def test_restate_on_change_true_still_restates_a_genuine_difference():
    """The default must not regress the exit-alert case this project
    already depends on — a materially different headline (a stop that
    actually moved) still restates faster than the full rearm window."""
    from intraday.notifier import Action
    sb = _FakeSB()
    # restate_minutes=0 isolates "is a genuine difference even ELIGIBLE to
    # restate" from "has enough wall-clock time passed" — the two sends in
    # this test are back-to-back, and a nonzero throttle would fail this for
    # a reason unrelated to what it is testing.
    with cfg_ctx({"intraday_restate_minutes": 0}), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        notifier = _notifier(sb)
        notifier.send(Action(symbol="SBIN", kind="TRAIL_SL",
                             headline="Trail SL 780.00 -> 802.00"))
        sent_again = notifier.send(Action(symbol="SBIN", kind="TRAIL_SL",
                                          headline="Trail SL 802.00 -> 815.00"))
    assert deliver_mock.call_count == 2, "a genuinely different trail level must still restate"
    assert sent_again is True


def test_rearm_false_never_resends_even_after_the_rearm_window_passes():
    """THE BUG, reproduced and closed. AUROPHARMA-shaped case: an IDENTICAL
    headline (nothing rounds differently — restate_on_change is not even
    in play here), but sent again after the ordinary rearm window would
    have elapsed. With rearm=False it must stay silent regardless."""
    from intraday.notifier import Action
    sb = _FakeSB()
    with cfg_ctx({"intraday_rearm_minutes": 0}), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        notifier = _notifier(sb)
        notifier.send(Action(symbol="AUROPHARMA", kind="ENTRY_APPROACHING",
                             headline="#2 today · 0.9% above the buy limit ₹1690.29",
                             rearm=False))
        sent_again = notifier.send(Action(symbol="AUROPHARMA", kind="ENTRY_APPROACHING",
                                          headline="#2 today · 0.9% above the buy limit ₹1690.29",
                                          rearm=False))
    assert deliver_mock.call_count == 1, (
        f"expected exactly 1 delivery for the whole day, got {deliver_mock.call_count} — "
        f"rearm=False must not rearm even once the window has elapsed")
    assert sent_again is False


def test_rearm_true_default_still_rearms_after_the_window():
    """Must not regress exit alerts — a persisting condition (a stop still
    breached) legitimately needs to keep reminding the operator."""
    from intraday.notifier import Action
    sb = _FakeSB()
    with cfg_ctx({"intraday_rearm_minutes": 0}), \
         patch("intraday.notifier.Notifier._deliver", return_value=True) as deliver_mock:
        notifier = _notifier(sb)
        notifier.send(Action(symbol="SBIN", kind="EXIT_STOP",
                             headline="Stop breached @ 795.00", urgency="CRITICAL"))
        sent_again = notifier.send(Action(symbol="SBIN", kind="EXIT_STOP",
                                          headline="Stop breached @ 795.00", urgency="CRITICAL"))
    assert deliver_mock.call_count == 2, "an unresolved stop breach must still rearm and repeat"
    assert sent_again is True


TESTS = [
    ("push=True delivers and returns the delivery result",
     test_push_true_delivers_and_returns_the_delivery_result),
    ("push=False skips delivery but still records and dedupes",
     test_push_false_skips_delivery_but_still_records_and_dedupes),
    ("push=False is still gated by material change",
     test_push_false_still_gated_by_material_change),
    ("restate_on_change=False holds a differently-worded repeat",
     test_restate_on_change_false_holds_a_differently_worded_repeat),
    ("restate_on_change=True (default) still restates a genuine difference",
     test_restate_on_change_true_still_restates_a_genuine_difference),
    ("rearm=False never resends even after the rearm window passes",
     test_rearm_false_never_resends_even_after_the_rearm_window_passes),
    ("rearm=True (default) still rearms after the window",
     test_rearm_true_default_still_rearms_after_the_window),
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
