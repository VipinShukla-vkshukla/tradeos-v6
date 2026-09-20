"""
analysis/setup_quality.py — the scoring itself, offline.

The module is instrumentation, not a gate, so these checks are about it being a
HONEST measurement: a missing factor must not read as an average one, one
outlier must not carry the mean, and every declared factor must actually be
reachable from the feature mapping.
"""

from __future__ import annotations

from analysis.setup_quality import (CLIP, FACTORS, MIN_FACTORS, Quality, features_from,
                                    score)


def _full(**over) -> dict:
    """A feature dict with every factor present, each at its own centre."""
    f = {x.name: x.centre for x in FACTORS}
    f.update(over)
    return f


def test_a_plan_at_every_centre_scores_zero():
    q = score(_full())
    assert q.n_used == len(FACTORS)
    assert abs(q.total) < 1e-9, q.total


def test_too_few_factors_is_none_not_zero():
    """
    None and 0.0 are different claims. Zero is the score of a perfectly typical
    plan; a plan we could not measure must not be indistinguishable from it —
    the same confusion that made the allocator's cold start refuse everything.
    """
    f = _full()
    for name in list(f)[:len(FACTORS) - MIN_FACTORS + 1]:
        f.pop(name)
    q = score(f)
    assert q.n_used < MIN_FACTORS
    assert q.total is None, q.total
    assert q.as_row()["setup_quality"] is None


def test_exactly_the_minimum_still_scores():
    f = _full()
    for name in list(f)[:len(FACTORS) - MIN_FACTORS]:
        f.pop(name)
    q = score(f)
    assert q.n_used == MIN_FACTORS
    assert q.total is not None


def test_direction_broad_but_not_hot_beats_hot_and_extended():
    """The signs, read as a trade rather than as arithmetic."""
    hot = score(features_from(
        {"dist_vwap_20d_pct": 18.0, "ret_12m": 5, "days_to_trigger_est": 0,
         "sector_rank_at_entry": 1, "base_score": 92},
        {"avg_ret_1m": 12.0, "composite_score": 0.55, "avg_rsi_weekly": 66,
         "avg_rs_vs_nifty": 9.0, "breadth_score": 0.50},
        {"avg_ret_1m": 13.0}))
    broad = score(features_from(
        {"dist_vwap_20d_pct": 2.0, "ret_12m": 60, "days_to_trigger_est": 6,
         "sector_rank_at_entry": 8, "base_score": 70},
        {"avg_ret_1m": 1.0, "composite_score": 0.85, "avg_rsi_weekly": 55,
         "avg_rs_vs_nifty": 0.5, "breadth_score": 0.95},
        {"avg_ret_1m": 1.5}))
    assert hot.n_used == broad.n_used == len(FACTORS)
    assert broad.total > hot.total, (broad.total, hot.total)


def test_one_absurd_value_cannot_carry_the_mean():
    base = score(_full()).total
    wild = score(_full(ret_12m=1e9)).total
    assert abs(wild - base) <= CLIP / len(FACTORS) + 1e-9, (base, wild)


def test_a_bool_is_not_a_measurement():
    """True is an int in Python and would otherwise be scored as 1."""
    q = score(_full(base_score=True))
    assert "base_score" in q.missing
    assert q.n_used == len(FACTORS) - 1


def test_every_declared_factor_is_reachable_from_the_mapping():
    """
    The consumer-side check. A factor added to FACTORS without a line in
    features_from() would never be populated, and the score would quietly drop
    to MIN_FACTORS-1 inputs without anything failing.
    """
    keys = set(features_from({}, {}, {}))
    declared = {f.name for f in FACTORS}
    assert declared == keys, (f"declared but never mapped: {sorted(declared - keys)}; "
                              f"mapped but not declared: {sorted(keys - declared)}")


def test_factor_table_is_well_formed():
    names = [f.name for f in FACTORS]
    assert len(names) == len(set(names)), "duplicate factor name"
    for f in FACTORS:
        assert f.sign in (1, -1), (f.name, f.sign)
        assert f.scale > 0, (f.name, f.scale)


def test_components_are_recorded_for_every_used_factor():
    q = score(_full(ret_12m=100))
    row = q.as_row()
    assert set(row["setup_quality_components"]["z"]) == {f.name for f in FACTORS}
    assert row["setup_quality_components"]["n_used"] == len(FACTORS)
    assert isinstance(q, Quality)


DECISION_PATHS = (
    "analysis/trade_decision.py", "analysis/entry_ranking.py",
    "allocation/scoring.py", "allocation/allocator.py", "allocation/hurdle.py",
    "control/position_lifecycle.py", "control/paper_entry.py",
    "execution/gates.py", "execution/order_manager.py",
)


def test_setup_quality_is_not_read_by_any_decision():
    """
    The claim in the docstring, enforced.

    "Nothing reads it for a trading decision" is the whole reason this score is
    allowed to exist while failing its holdout. If someone later wires it into
    ranking, sizing or a gate, that is a decision to be argued with evidence —
    not something that should be possible to do quietly.
    """
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    used = []
    for rel in DECISION_PATHS:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if "setup_quality" in line and not line.lstrip().startswith("#"):
                    used.append(f"{rel}:{i}")
    assert not used, ("setup_quality reached a decision path; it lost to the production "
                      f"ranker out of sample (see the module docstring): {used}")


TESTS = [
    ("setup_quality is not read by any decision", test_setup_quality_is_not_read_by_any_decision),
    ("a plan at every centre scores zero", test_a_plan_at_every_centre_scores_zero),
    ("too few factors is None, not zero", test_too_few_factors_is_none_not_zero),
    ("exactly the minimum still scores", test_exactly_the_minimum_still_scores),
    ("broad-but-not-hot beats hot-and-extended", test_direction_broad_but_not_hot_beats_hot_and_extended),
    ("one absurd value cannot carry the mean", test_one_absurd_value_cannot_carry_the_mean),
    ("a bool is not a measurement", test_a_bool_is_not_a_measurement),
    ("every declared factor is reachable from the mapping", test_every_declared_factor_is_reachable_from_the_mapping),
    ("factor table is well formed", test_factor_table_is_well_formed),
    ("components recorded for every used factor", test_components_are_recorded_for_every_used_factor),
]
