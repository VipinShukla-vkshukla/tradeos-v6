"""
Rung 6b of evaluate_intraday_exit(): an open SHORT's stop tightens as its own
cover deadline approaches, instead of riding full risk width into rung 1's
hard EXIT_SQUAREOFF and being closed at whatever price the market happens to
be at that instant — the forced, worst-possible-moment exit the entry-side
runway gate (can_short(), intraday/shortability.py) exists to avoid in the
first place. That gate only ever protected NEW entries; this closes the same
gap for a short already open.

Reuses intraday_short_min_runway_min — the SAME number can_short() gates
entries on — so the two sides cannot drift apart the way hurdle/edge did
before migration 044.

evaluate_intraday_exit() has no prior test coverage in this suite (checked —
zero references before this file), so these tests also serve as the first
direct exercise of that function, not just of the new rung.
"""

from __future__ import annotations

from datetime import datetime

from config import IST


def _policy(**overrides):
    base = {
        "use_setup_target":   True,
        "partial_book_r":     1.2,
        "partial_book_pct":   50.0,
        "move_to_breakeven":  True,
        "trail_r":            1.0,
        "trail_after_r":      1.5,
        "target_r":           2.0,
        "time_stop_min":      75,
        "time_stop_min_r":    0.3,
        "squareoff_buffer":   12,
        "check_invalidation": False,
        "must_exit_time":     "15:15",
        "breakeven_at_r":     1.2,
        "cost_buffer_pct":    0.21,
        "giveback_pct":       0.0,
        "short_runway_tighten_enabled":    True,
        "short_runway_min":                75,
        "short_runway_tighten_floor_pct":  40.0,
    }
    base.update(overrides)
    return base


def _short_pos(active_sl=105.0, planned_target=0):
    return {
        "symbol": "AEGISLOG", "direction": "SHORT",
        "entry_price": 100.0, "planned_stop": 105.0, "active_sl": active_sl,
        "planned_target": planned_target, "current_qty": 10,
        "partial_booked_qty": 0,
    }


# 14:25 IST -> minutes_to_cover_deadline() = 40 (15:05 deadline - 14:25),
# matching the real 14:24:30 log line this feature was built to address.
_NOW_40_LEFT = datetime(2026, 8, 11, 14, 25, tzinfo=IST)
# 13:00 IST -> 125 minutes left, well outside the 75-minute window.
_NOW_PLENTY_LEFT = datetime(2026, 8, 11, 13, 0, tzinfo=IST)


def test_tightens_a_profitable_short_as_the_deadline_nears():
    from intraday.exit_policy import evaluate_intraday_exit
    d = evaluate_intraday_exit(_short_pos(), ltp=98.0, policy=_policy(), now=_NOW_40_LEFT)
    assert d["action"] == "TRAIL_SL" and d["reason"] == "RUNWAY_TIGHTEN", d
    # frac = 0.40 + 0.60*(40/75) = 0.72; tightened = 98 - (-1*5*0.72) = 101.6
    assert d["new_sl"] == 101.6, d


def test_disabled_by_default_switch_does_nothing():
    from intraday.exit_policy import evaluate_intraday_exit
    d = evaluate_intraday_exit(_short_pos(), ltp=98.0,
                               policy=_policy(short_runway_tighten_enabled=False),
                               now=_NOW_40_LEFT)
    assert d["reason"] != "RUNWAY_TIGHTEN", d
    assert d["action"] == "HOLD", d


def test_never_fires_for_a_long_even_when_enabled():
    from intraday.exit_policy import evaluate_intraday_exit
    long_pos = {
        "symbol": "TCS", "direction": "LONG",
        "entry_price": 100.0, "planned_stop": 95.0, "active_sl": 95.0,
        "planned_target": 0, "current_qty": 10, "partial_booked_qty": 0,
    }
    d = evaluate_intraday_exit(long_pos, ltp=102.0, policy=_policy(), now=_NOW_40_LEFT)
    assert d["reason"] != "RUNWAY_TIGHTEN", d


def test_does_not_fire_with_plenty_of_runway_remaining():
    from intraday.exit_policy import evaluate_intraday_exit
    d = evaluate_intraday_exit(_short_pos(), ltp=98.0, policy=_policy(), now=_NOW_PLENTY_LEFT)
    assert d["reason"] != "RUNWAY_TIGHTEN", d
    assert d["action"] == "HOLD", d


def test_never_loosens_a_stop_already_tighter_than_the_runway_formula():
    """
    active_sl already at 99 -- tighter than the 101.6 the runway formula
    would propose. Must not loosen it back out to 101.6.
    """
    from intraday.exit_policy import evaluate_intraday_exit
    d = evaluate_intraday_exit(_short_pos(active_sl=99.0), ltp=98.0,
                               policy=_policy(), now=_NOW_40_LEFT)
    assert d["reason"] != "RUNWAY_TIGHTEN", d
    assert d["action"] == "HOLD", d


def test_floor_caps_how_tight_the_stop_can_get_right_before_the_deadline():
    """At ~1 minute left, frac should sit near the configured floor (0.40),
    not collapse to zero risk -- rung 1's EXIT_SQUAREOFF owns the zero case."""
    from intraday.exit_policy import evaluate_intraday_exit
    from datetime import timedelta
    now_1_left = datetime(2026, 8, 11, 15, 4, tzinfo=IST)  # 15:05 deadline - 1 min
    d = evaluate_intraday_exit(_short_pos(), ltp=98.0, policy=_policy(), now=now_1_left)
    assert d["reason"] == "RUNWAY_TIGHTEN", d
    # frac = 0.40 + 0.60*(1/75) ≈ 0.408; tightened ≈ 98 - (-1*5*0.408) ≈ 100.04
    assert 100.0 < d["new_sl"] < 100.1, d


TESTS = [
    ("tightens a profitable short as the deadline nears",
     test_tightens_a_profitable_short_as_the_deadline_nears),
    ("disabled by default switch does nothing",
     test_disabled_by_default_switch_does_nothing),
    ("never fires for a long even when enabled",
     test_never_fires_for_a_long_even_when_enabled),
    ("does not fire with plenty of runway remaining",
     test_does_not_fire_with_plenty_of_runway_remaining),
    ("never loosens a stop already tighter than the runway formula",
     test_never_loosens_a_stop_already_tighter_than_the_runway_formula),
    ("floor caps how tight the stop can get right before the deadline",
     test_floor_caps_how_tight_the_stop_can_get_right_before_the_deadline),
]
