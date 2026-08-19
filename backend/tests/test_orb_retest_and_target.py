"""
ORB's two completed gaps: the RETEST arm of "retest OR strength", and the
measured-move target — both 19-Aug-2026.

WHAT THESE PIN
---------------
`_retest_and_held()` is pure and gets the thorough coverage. `evaluate()`
itself gets two end-to-end fixtures — one proving a weak break really is
rescued by a genuine retest, one proving the target really does widen to the
range's own height — because the wiring between the pure function and the
engine is exactly where this repository has repeatedly shipped a builder and
a consumer that quietly disagreed (F-33 §4, the SDN confidence key; this
session's own family/sub_engine keying). Proving the pure function is correct
does not prove `evaluate()` calls it correctly.

Every check here was demonstrated FAILING against a one-line removal of the
behaviour it pins before being trusted to pass.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests import cfg_ctx                                          # noqa: E402
from config import IST                                             # noqa: E402
from intraday.strategies.base import Bar, SymbolContext             # noqa: E402
from intraday.strategies.orb import OpeningRangeBreakout, _retest_and_held  # noqa: E402
from intraday.session import PRIME                                  # noqa: E402


# ── _retest_and_held(), pure ────────────────────────────────────────────────

def _bar(minutes_after_open: float, high, low, close, t0=None):
    t0 = t0 or datetime(2026, 8, 19, 9, 15, tzinfo=IST)
    return Bar(ts=t0 + timedelta(minutes=minutes_after_open),
              open=(high + low) / 2, high=high, low=low, close=close, volume=1.0)


def test_probe_then_retest_then_hold_confirms():
    bars = [
        _bar(16, high=102.5, low=101.9, close=102.3),   # probes above 102
        _bar(21, high=102.10, low=102.03, close=102.05),  # retests, holds
    ]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is True


def test_no_probe_above_level_never_confirms():
    """Price must have actually traded above the level at least once — a
    retest of a level never broken is not a retest."""
    bars = [_bar(16, high=101.9, low=101.5, close=101.8)]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is False


def test_probe_with_no_retest_never_confirms():
    """Probed above and never came back — nothing has been RE-tested."""
    bars = [_bar(16, high=102.5, low=102.2, close=102.4)]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is False


def test_retest_that_fails_to_hold_is_rejected():
    """Came back to the level, then closed BELOW it — reclaimed, not held."""
    bars = [
        _bar(16, high=102.5, low=101.9, close=102.3),
        _bar(21, high=102.05, low=101.95, close=101.90),   # closed under 102
    ]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is False


def test_a_later_bar_breaking_back_below_invalidates_a_prior_good_retest():
    """
    ONE FAILURE VOIDS THE WHOLE SEQUENCE. A level "held" once and then lost
    two bars later is not held — this is what separates a real retest from a
    lucky snapshot at the moment this function happens to be called.
    """
    bars = [
        _bar(16, high=102.5, low=101.9, close=102.3),
        _bar(21, high=102.10, low=102.03, close=102.05),   # good retest
        _bar(26, high=102.00, low=101.80, close=101.85),   # lost it
    ]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is False


def test_retest_outside_tolerance_does_not_count_as_a_retest():
    """Came back near the level but not close enough to call it a genuine
    test of it — a pullback that stops well above the level proves nothing
    about whether the level itself would hold."""
    bars = [
        _bar(16, high=102.5, low=101.9, close=102.3),
        _bar(21, high=103.0, low=102.80, close=102.90),   # never got near 102
    ]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is False


def test_empty_bars_is_unconfirmed_not_an_error():
    assert _retest_and_held([], level=102.0, tolerance_pct=0.15) is False


def test_multiple_retests_all_holding_still_confirms():
    bars = [
        _bar(16, high=102.5, low=101.9, close=102.3),
        _bar(21, high=102.10, low=102.02, close=102.05),
        _bar(26, high=102.20, low=102.01, close=102.15),   # second retest, holds
    ]
    assert _retest_and_held(bars, level=102.0, tolerance_pct=0.15) is True


# ── evaluate(), end to end ───────────────────────────────────────────────────

def _weak_break_ctx_with_retest():
    """
    A break at 0.049% of a 0.89%-wide range — well under the strength
    threshold (~0.12%, the invalidation-buffer floor) — with a genuine
    retest-and-hold behind it. Levels chosen so risk_pct lands at ~1.03%,
    inside both intraday_min_risk_pct (0.6, armed this session) and
    orb_max_risk_pct (1.20 default), so risk_from_structure does not itself
    refuse the setup for an unrelated reason.
    """
    t0 = datetime(2026, 8, 19, 9, 15, tzinfo=IST)
    bars = [
        _bar(0, high=101.9, low=101.0, close=101.5, t0=t0),   # the opening range
        _bar(16, high=102.10, low=101.95, close=102.0, t0=t0),  # probes above 101.9
        _bar(21, high=101.98, low=101.92, close=101.95, t0=t0),  # retests, holds
    ]
    return SymbolContext(symbol="TEST", ltp=101.95, bars=bars)


def test_a_weak_break_is_refused_with_retest_confirmation_off():
    ctx = _weak_break_ctx_with_retest()
    with cfg_ctx({"orb_retest_confirmation_enabled": "false",
                  "intraday_min_risk_pct": "0.0"}):
        assert OpeningRangeBreakout().evaluate(ctx, PRIME) is None


def test_the_same_weak_break_is_rescued_by_a_genuine_retest():
    ctx = _weak_break_ctx_with_retest()
    with cfg_ctx({"orb_retest_confirmation_enabled": "true",
                  "intraday_min_risk_pct": "0.0"}):
        setup = OpeningRangeBreakout().evaluate(ctx, PRIME)
    assert setup is not None, "a genuinely retested break must be taken"
    assert setup.meta.get("retest_confirmed") is True


def test_a_weak_break_with_no_retest_is_still_refused_even_when_enabled():
    """THE SAFETY PROPERTY. Turning the arm on rescues a WEAK-BUT-RETESTED
    break; it must not turn into "take every weak break"."""
    t0 = datetime(2026, 8, 19, 9, 15, tzinfo=IST)
    bars = [
        _bar(0, high=101.9, low=101.0, close=101.5, t0=t0),
        _bar(16, high=101.98, low=101.94, close=101.96, t0=t0),  # never probes >101.9 meaningfully then away
    ]
    ctx = SymbolContext(symbol="TEST", ltp=101.95, bars=bars)
    with cfg_ctx({"orb_retest_confirmation_enabled": "true",
                  "intraday_min_risk_pct": "0.0"}):
        assert OpeningRangeBreakout().evaluate(ctx, PRIME) is None


def _wide_range_ctx():
    """
    A strong break (0.29% of a 2.475%-wide range — well clear of both the
    ~0.25% strength floor and the 0.60% chase cap, so retest logic is not in
    play) with the stop anchored at the range low, per ORB's own structural-
    stop rule.

    THE FIXTURE HAD TO ACCOUNT FOR A REAL STRUCTURAL FACT, FOUND BY RUNNING
    IT. `risk = entry - stop` and `stop` sits at the range low, so
    `risk = (entry - range_high) + range_height >= range_height` always — a
    2R target is therefore close to `entry + 2*range_height` and structurally
    tends to beat the plain measured-move target (`entry + range_height`).
    `orb_target_r=0.3` is set low here specifically so the fixture can
    demonstrate the WIDER branch at all; at the code's own default (2.0) the
    measured-move target rarely wins given this engine's stop placement, and
    that is a property of the stop rule, not a bug in `max()`.
    """
    t0 = datetime(2026, 8, 19, 9, 15, tzinfo=IST)
    bars = [
        _bar(0, high=103.5, low=101.0, close=102.0, t0=t0),   # 2.5-wide range
        _bar(16, high=103.9, low=103.6, close=103.85, t0=t0),  # strong, unchased break
    ]
    return SymbolContext(symbol="TEST", ltp=103.8, bars=bars, atr_pct_daily=5.0)


def test_measured_move_widens_the_target_when_the_range_is_wide():
    ctx = _wide_range_ctx()
    with cfg_ctx({"orb_measured_move_target_enabled": "true",
                  "intraday_min_risk_pct": "0.0", "orb_max_risk_pct": "3.0",
                  "orb_target_r": "0.3"}):
        setup = OpeningRangeBreakout().evaluate(ctx, PRIME)
    assert setup is not None
    by_r = setup.entry + (setup.entry - setup.stop) * 0.3
    measured = setup.entry + (103.5 - 101.0)
    assert measured > by_r, "fixture must actually exercise the wider branch"
    assert abs(setup.target - measured) < 0.01, (setup.target, measured, by_r)
    assert setup.meta.get("measured_move_used") is True


def test_measured_move_off_restores_the_flat_multiple_exactly():
    ctx = _wide_range_ctx()
    with cfg_ctx({"orb_measured_move_target_enabled": "false",
                  "intraday_min_risk_pct": "0.0", "orb_max_risk_pct": "3.0",
                  "orb_target_r": "0.3"}):
        setup = OpeningRangeBreakout().evaluate(ctx, PRIME)
    assert setup is not None
    by_r = setup.entry + (setup.entry - setup.stop) * 0.3
    assert abs(setup.target - by_r) < 0.01, (setup.target, by_r)
    assert setup.meta.get("measured_move_used") is False


def test_measured_move_does_not_win_at_the_default_target_r():
    """
    THE HONEST COUNTERPART. Documents the structural fact the fixture above
    had to work around: at the code's OWN shipped default (orb_target_r=2.0),
    the flat multiple wins against this same wide range, because ORB's own
    stop-at-range-low rule makes risk >= range_height. max() is doing its
    job correctly; it simply has little to bite on until a trade's risk is
    small relative to the range that produced it.
    """
    ctx = _wide_range_ctx()
    with cfg_ctx({"orb_measured_move_target_enabled": "true",
                  "intraday_min_risk_pct": "0.0", "orb_max_risk_pct": "3.0"}):
        setup = OpeningRangeBreakout().evaluate(ctx, PRIME)
    assert setup is not None
    by_r = setup.entry + (setup.entry - setup.stop) * 2.0
    assert abs(setup.target - by_r) < 0.01, (setup.target, by_r)
    assert setup.meta.get("measured_move_used") is False


TESTS = [
    ("probe then retest then hold confirms",
     test_probe_then_retest_then_hold_confirms),
    ("no probe above level never confirms",
     test_no_probe_above_level_never_confirms),
    ("probe with no retest never confirms",
     test_probe_with_no_retest_never_confirms),
    ("a retest that fails to hold is rejected",
     test_retest_that_fails_to_hold_is_rejected),
    ("a later break-back voids a prior good retest",
     test_a_later_bar_breaking_back_below_invalidates_a_prior_good_retest),
    ("a retest outside tolerance does not count",
     test_retest_outside_tolerance_does_not_count_as_a_retest),
    ("empty bars is unconfirmed, not an error",
     test_empty_bars_is_unconfirmed_not_an_error),
    ("multiple holding retests still confirm",
     test_multiple_retests_all_holding_still_confirms),
    ("a weak break is refused with retest confirmation off",
     test_a_weak_break_is_refused_with_retest_confirmation_off),
    ("the same weak break is rescued by a genuine retest",
     test_the_same_weak_break_is_rescued_by_a_genuine_retest),
    ("a weak break with no retest is still refused even when enabled",
     test_a_weak_break_with_no_retest_is_still_refused_even_when_enabled),
    ("measured move widens the target when the range is wide",
     test_measured_move_widens_the_target_when_the_range_is_wide),
    ("measured move off restores the flat multiple exactly",
     test_measured_move_off_restores_the_flat_multiple_exactly),
    ("measured move does not win at the default target_r",
     test_measured_move_does_not_win_at_the_default_target_r),
]
