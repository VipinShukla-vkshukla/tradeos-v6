"""
The asymmetric cross-book carve-out: INTRADAY may join a name SWING already
holds (same direction only); SWING never joins a name INTRADAY holds.

WHAT THIS CATCHES
------------------
`_intraday_may_join_swing_holding()` is the one place that decides whether an
INTRADAY setup stands down when `_other_framework_holding` finds SWING
already in the name. It sits UNDER `one_framework_per_symbol`, which stays
the master invariant and is untouched by this — the SWING-side call in
`evaluate_candidates` keeps refusing unconditionally. This module exercises
the helper directly, without needing a live engine or a database, so a
future edit that widens it to also let SHORTs through (reintroducing "two
exit ladders that contradict each other" against your own swing thesis)
fails a test instead of shipping quietly.
"""

from __future__ import annotations

from tests import cfg_ctx


def test_no_other_holder_is_always_joinable():
    from intraday.engine import _intraday_may_join_swing_holding
    assert _intraday_may_join_swing_holding(None, "LONG") is True


def test_long_joins_swing_holding_when_switch_on():
    from intraday.engine import _intraday_may_join_swing_holding
    with cfg_ctx({"intraday_allow_swing_held_symbols": "true"}):
        other = {"framework": "SWING", "current_qty": 5, "entry_price": 2500.0}
        assert _intraday_may_join_swing_holding(other, "LONG") is True


def test_long_refused_when_switch_off():
    from intraday.engine import _intraday_may_join_swing_holding
    with cfg_ctx({"intraday_allow_swing_held_symbols": "false"}):
        other = {"framework": "SWING", "current_qty": 5, "entry_price": 2500.0}
        assert _intraday_may_join_swing_holding(other, "LONG") is False


def test_short_never_joins_swing_holding_even_with_switch_on():
    """Shorting a name you're swing-long is a contradicted thesis, not a satellite."""
    from intraday.engine import _intraday_may_join_swing_holding
    with cfg_ctx({"intraday_allow_swing_held_symbols": "true"}):
        other = {"framework": "SWING", "current_qty": 5, "entry_price": 2500.0}
        assert _intraday_may_join_swing_holding(other, "SHORT") is False


def test_intraday_held_by_intraday_is_not_this_helpers_concern():
    """A name INTRADAY itself already holds is a different case — refused, not joined."""
    from intraday.engine import _intraday_may_join_swing_holding
    with cfg_ctx({"intraday_allow_swing_held_symbols": "true"}):
        other = {"framework": "INTRADAY", "current_qty": 5, "entry_price": 2500.0}
        assert _intraday_may_join_swing_holding(other, "LONG") is False


TESTS = [
    ("no other holder is always joinable",         test_no_other_holder_is_always_joinable),
    ("LONG joins SWING holding when switch on",     test_long_joins_swing_holding_when_switch_on),
    ("LONG refused when switch off",                test_long_refused_when_switch_off),
    ("SHORT never joins SWING holding",             test_short_never_joins_swing_holding_even_with_switch_on),
    ("INTRADAY-held name is not this helper's concern",
     test_intraday_held_by_intraday_is_not_this_helpers_concern),
]
