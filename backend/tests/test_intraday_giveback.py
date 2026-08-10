"""
The intraday give-back guard now fires — it shipped OFF and stayed OFF past
its own calibration trigger (10-Aug-2026).

WHAT THIS CATCHES
-----------------
`exit_policy.py`'s own comment named the trigger: "set intraday_giveback_pct
once ~20 intraday positions have closed carrying excursion data." Migration
059 lands 40 closed positions later — `tools/exit_audit.py` counted them —
and mirrors SWING's proven 50% (backed by a 70-trade study: 24 of 28 losing
swing trades had been >0.5% in profit first) as the calibration starting
point the comment asked for.

This test exists because a switch flipped in a migration and never observed
actually changing behaviour is exactly this project's most-repeated defect —
a config key that reads as live and does nothing. Demonstrated at the OLD
default (0.0) first: the guard must NOT fire there, on the identical
textbook giveback scenario.
"""

from __future__ import annotations

from datetime import datetime

from tests import cfg_ctx
from config import IST
from intraday.exit_policy import evaluate_intraday_exit, load_intraday_policy

# A fixed mid-session instant (11:00 IST). evaluate_intraday_exit's own rung 1
# checks the WALL CLOCK by default, and this suite otherwise runs at whatever
# time the test happens to execute — outside 09:30-15:00 IST every position
# resolves EXIT_SQUAREOFF/SESSION_END before the give-back rung is ever
# reached, which is exactly what made these tests order-dependent on the hour
# they were run at first.
_MID_SESSION = datetime(2026, 8, 10, 11, 0, 0, tzinfo=IST)


def _position(hwm, entry=100.0, stop=99.0, direction="LONG"):
    return {"entry_price": entry, "planned_stop": stop, "active_sl": stop,
            "planned_target": entry + (entry - stop) * 3, "direction": direction,
            "high_water_mark": hwm}


def test_old_default_never_fires_the_guard():
    """0.0 means OFF. A position that peaked at 2R and faded to 0.3R must NOT
    be exited by the give-back rung at the pre-migration default — this is
    the state exit_policy.py shipped in and the migration is replacing."""
    with cfg_ctx({"intraday_giveback_pct": "0.0", "intraday_giveback_min_r": "1.0"}):
        policy = load_intraday_policy()
        pos = _position(hwm=102.0)   # peaked 2R above entry (risk=1.0)
        result = evaluate_intraday_exit(pos, ltp=100.3, policy=policy, now=_MID_SESSION)
    assert result["action"] != "EXIT_GIVEBACK", (
        "the guard fired at the OFF default — the baseline this migration "
        "changes is not what this test thinks it is")


def test_calibrated_default_fires_on_a_textbook_giveback():
    """The identical scenario, at migration 059's new default. A position
    that peaked at 2R (>= giveback_min_r 1.0) and gave back 85% of that peak
    (kept 0.15 of 2.0, i.e. 15%, well under the 50% keep-floor) must exit."""
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        policy = load_intraday_policy()
        pos = _position(hwm=102.0)
        result = evaluate_intraday_exit(pos, ltp=100.3, policy=policy, now=_MID_SESSION)
    assert result["action"] == "EXIT_GIVEBACK", (
        f"the guard did not fire at the calibrated default — got "
        f"{result['action']}/{result.get('reason')}")


def test_a_peak_below_min_r_is_never_touched():
    """giveback_min_r=1.0 must still gate the rung — a position that only ever
    reached 0.6R and faded is an ordinary small move, not a give-back."""
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        policy = load_intraday_policy()
        pos = _position(hwm=100.6)   # peaked 0.6R, below the 1.0 floor
        result = evaluate_intraday_exit(pos, ltp=100.1, policy=policy, now=_MID_SESSION)
    assert result["action"] != "EXIT_GIVEBACK", (
        "the guard fired below its own min_r floor")


def test_keeping_most_of_the_move_does_not_fire():
    """A position that peaked at 2R and still holds 1.5R (kept 75%, above the
    50% floor) is a normal pullback, not a give-back — the guard must not be
    trigger-happy on ordinary noise."""
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        policy = load_intraday_policy()
        pos = _position(hwm=102.0)
        result = evaluate_intraday_exit(pos, ltp=101.5, policy=policy, now=_MID_SESSION)
    assert result["action"] != "EXIT_GIVEBACK", (
        f"the guard fired while 75% of the peak was still held — got "
        f"{result['action']}")


def test_a_short_giveback_is_symmetric():
    """high_water_mark for a short is its BEST (lowest) price. A short that
    fell 2R and gave most of it back must also trigger — the guard must not
    be a long-only rung."""
    with cfg_ctx({"intraday_giveback_pct": "50.0", "intraday_giveback_min_r": "1.0"}):
        policy = load_intraday_policy()
        pos = _position(hwm=98.0, entry=100.0, stop=101.0, direction="SHORT")
        result = evaluate_intraday_exit(pos, ltp=99.7, policy=policy, now=_MID_SESSION)
    assert result["action"] == "EXIT_GIVEBACK", (
        f"a short give-back did not fire — got {result['action']}")


TESTS = [
    ("old default never fires the guard",
     test_old_default_never_fires_the_guard),
    ("calibrated default fires on a textbook giveback",
     test_calibrated_default_fires_on_a_textbook_giveback),
    ("a peak below min_r is never touched",
     test_a_peak_below_min_r_is_never_touched),
    ("keeping most of the move does not fire",
     test_keeping_most_of_the_move_does_not_fire),
    ("a short giveback is symmetric",
     test_a_short_giveback_is_symmetric),
]
