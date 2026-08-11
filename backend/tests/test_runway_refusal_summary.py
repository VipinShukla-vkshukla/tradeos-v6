"""
Six shorts refused in a row for runway ("N min to the cover deadline") read
like a dead market from the console — the observation this feature was built
from. The refusal is about the clock, not the tape: can_short()'s cover-
deadline check says nothing about whether longs are firing in the same cycle.
_runway_refusal_summary() is evaluate_intraday_setups()'s pure tail —
extracted specifically so this is testable without mocking session_state(),
market_context, the cost model, ai_advisor and market_structure just to
exercise one summary line.
"""

from __future__ import annotations

from intraday.strategies.base import Setup


def _setup(symbol: str, direction: str):
    return Setup(symbol, "PDL", direction, 100.0,
                102.0 if direction == "SHORT" else 98.0,
                96.0 if direction == "SHORT" else 104.0,
                0.7, "rationale", "invalidation")


def _fired(symbol: str, direction: str):
    return {"setup": _setup(symbol, direction), "qty": 5, "cost_pct": 0.1}


def test_no_refusals_says_nothing():
    from intraday.engine import _runway_refusal_summary
    assert _runway_refusal_summary([], []) is None
    assert _runway_refusal_summary([], [_fired("TCS", "LONG")]) is None


def test_refusals_with_longs_firing_points_at_them():
    from intraday.engine import _runway_refusal_summary
    msg = _runway_refusal_summary(
        ["AEGISLOG", "CEMPRO", "CHOLAFIN"],
        [_fired("TCS", "LONG"), _fired("INFY", "LONG"), _fired("WIPRO", "SHORT")])
    assert "3 short(s) refused on runway" in msg
    assert "AEGISLOG" in msg and "CEMPRO" in msg and "CHOLAFIN" in msg
    # Only the LONG setups count toward the pointer — a short that happened
    # to clear its OWN runway check is not "unaffected by the cover deadline".
    assert "2 long setup(s) fired" in msg


def test_refusals_with_nothing_else_firing_says_so_honestly():
    """Must not imply a long alternative exists when none fired — a false
    'go look at the longs instead' is worse than no comparison at all."""
    from intraday.engine import _runway_refusal_summary
    msg = _runway_refusal_summary(["SONACOMS"], [])
    assert "1 short(s) refused on runway" in msg
    assert "no long setups fired" in msg
    assert "long setup(s) fired in the same cycle" not in msg


def test_shorts_that_fired_are_not_counted_as_the_long_alternative():
    from intraday.engine import _runway_refusal_summary
    msg = _runway_refusal_summary(["ENRIN"], [_fired("DATAPATTNS", "SHORT")])
    assert "no long setups fired" in msg


TESTS = [
    ("no refusals says nothing", test_no_refusals_says_nothing),
    ("refusals with longs firing points at them",
     test_refusals_with_longs_firing_points_at_them),
    ("refusals with nothing else firing says so honestly",
     test_refusals_with_nothing_else_firing_says_so_honestly),
    ("shorts that fired are not counted as the long alternative",
     test_shorts_that_fired_are_not_counted_as_the_long_alternative),
]
