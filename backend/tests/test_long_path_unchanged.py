"""
Adding SHORT must be invisible to LONG.

This is the most important module here and the one to run first. The swing book
holds real money and the intraday long engines have the only track record this
system owns; a shorting change that quietly alters either is worse than one that
crashes, because it would be discovered in the P&L rather than in a traceback.

Every direction-aware signature is called the way EXISTING callers call it —
with no direction argument at all — and compared against the pre-change formula
recomputed independently here, not against a remembered constant. A constant
would only prove the code still returns what it returned when the constant was
written down.
"""

from __future__ import annotations

import inspect

from tests import cfg_ctx, approx
from tests._fixtures import EXIT_POLICY, MIDDAY, long_pos


def test_is_worth_taking_default_is_long():
    from intraday.cost_model import is_worth_taking
    with cfg_ctx():
        implicit, _ = is_worth_taking(100.0, 10, 103.0, 99.0)
        explicit, _ = is_worth_taking(100.0, 10, 103.0, 99.0, "MIS", "LONG")
        assert implicit == explicit, "omitting direction no longer means LONG"
        # And it still refuses levels that are inverted FOR A LONG.
        bad, why = is_worth_taking(100.0, 10, 97.0, 101.0)
        assert bad is False, f"inverted long levels accepted: {why}"


def test_score_matches_the_pre_change_formula():
    """
    edge, r_target and cost_r against the expressions that were there before
    `direction` existed — recomputed here, to 1e-12.
    """
    from allocation.scoring import score, Prior
    from intraday.cost_model import round_trip
    with cfg_ctx():
        prior = Prior("t", 100, 0.10, 0.05, 0.02, -1.0, 1.5)
        got = score(100.0, 99.0, 103.0, 10, "MIS", prior, 0.5)
        explicit = score(100.0, 99.0, 103.0, 10, "MIS", prior, 0.5, "LONG")
        assert got == explicit, "score()'s default is no longer LONG"

        risk_pct = (100.0 - 99.0) / 100.0
        cost_r = round_trip(100.0, 10, product="MIS").total / (risk_pct * 100.0 * 10)
        assert approx(got["edge"], (prior.mean_r - cost_r) / 0.5), \
            f"edge {got['edge']} != the original (E[R]-cost_R)/hold_days"
        assert approx(got["r_target"], (103.0 - 100.0) / (100.0 - 99.0)), \
            "r_target no longer (target-entry)/(entry-stop)"
        assert approx(got["cost_r"], cost_r), "cost_r formula changed"


def test_gate_for_framework_default_is_long():
    from analysis.market_structure import gate_for_framework
    with cfg_ctx():
        hs = [105, 104, 103.2, 102, 101.4, 100.1, 99.4, 98.2, 97.6, 96.4, 95.5, 94.6]
        ls = [104, 103, 102.1, 101, 100.2, 99.0, 98.1, 97.0, 96.2, 95.1, 94.2, 93.3]
        assert gate_for_framework("INTRADAY", hs, ls)[:2] == \
               gate_for_framework("INTRADAY", hs, ls, "LONG")[:2], \
            "the 3-argument form no longer behaves as LONG"


def test_close_position_side_is_optional():
    """Every pre-shorting caller passes four arguments and must keep working."""
    from execution import paper_broker
    sig = inspect.signature(paper_broker.close_position)
    assert sig.parameters["side"].default is None, \
        "close_position(side=) is no longer optional — existing callers break"


def test_exit_ladder_on_a_row_with_no_direction_key():
    """
    EVERY position row written before the shorting work looks exactly like this:
    no `direction` key at all. It must behave identically to one tagged LONG, at
    every rung, or the live swing book's exits changed underneath it.
    """
    from intraday.exit_policy import evaluate_intraday_exit
    with cfg_ctx():
        legacy = {"entry_price": 100.0, "planned_stop": 99.0,
                  "planned_target": 103.0, "current_qty": 10}   # no direction
        tagged = {**legacy, "direction": "LONG"}
        for ltp in (98.5, 99.2, 100.0, 100.8, 101.5, 102.4, 103.5):
            a = evaluate_intraday_exit(dict(legacy), ltp, EXIT_POLICY, MIDDAY)
            b = evaluate_intraday_exit(dict(tagged), ltp, EXIT_POLICY, MIDDAY)
            assert a == b, f"at ltp {ltp}: untagged row gave {a}, LONG gave {b}"


def test_setup_properties_match_the_original_formulae():
    from intraday.strategies.base import Setup
    with cfg_ctx():
        s = Setup("X", "ORB", "LONG", 100.0, 99.0, 103.0, 0.7, "r", "i")
        assert approx(s.risk_pct, (100.0 - 99.0) / 100.0 * 100.0, 1e-9)
        assert approx(s.reward_pct, (103.0 - 100.0) / 100.0 * 100.0, 1e-9)
        assert approx(s.rr, (103.0 - 100.0) / (100.0 - 99.0), 1e-9)


def test_the_eight_long_engines_are_still_registered():
    """
    Was "the seven long engines" until 11-Aug-2026, when GDB (Gap-Down
    Bounce, brain_proposals#190) was added as an eighth — a deliberate,
    reviewed change, not a regression this guard should have caught. Updated
    count, same purpose: nothing about adding a new engine (or the earlier
    short work) may silently disturb the existing long engines or reshuffle
    their families.
    """
    from intraday.strategies.registry import _ALL, family_of
    with cfg_ctx():
        longs = [e for e in _ALL if e.name != "SDN"]
        assert len(longs) == 8, \
            f"expected the 8 long engines, found {[e.name for e in longs]}"
        # Families must not have been reshuffled by the short work, or by GDB.
        assert family_of("GAP") == "ORB", "GAP left the ORB family"
        assert family_of("PBK") == "VWR", "PBK left the VWR family"
        assert family_of("GDB") == "GDB", \
            "GDB should start in its own family until proven, same as VCE/RNG"
        assert family_of("SDN") == "SDN", \
            "SDN was merged into a long family — a short and a long in the same " \
            "structure have different base rates and must never average together"


def test_shorts_are_off_by_default():
    """
    Nothing about the short capability may activate without the operator's own
    switch. Checked against the DEFAULT, not against whatever is configured.
    """
    from config import cfg_bool
    with cfg_ctx():
        assert cfg_bool("intraday_allow_shorts", False) is False, \
            "intraday_allow_shorts no longer defaults to false"


TESTS = [
    ("is_worth_taking() default is LONG",        test_is_worth_taking_default_is_long),
    ("score() matches the pre-change formula",   test_score_matches_the_pre_change_formula),
    ("gate_for_framework() default is LONG",     test_gate_for_framework_default_is_long),
    ("close_position(side=) stays optional",     test_close_position_side_is_optional),
    ("exit ladder on an untagged (legacy) row",  test_exit_ladder_on_a_row_with_no_direction_key),
    ("Setup risk/reward/rr formulae",            test_setup_properties_match_the_original_formulae),
    ("the eight long engines still registered",  test_the_eight_long_engines_are_still_registered),
    ("shorts off by default",                    test_shorts_are_off_by_default),
]
