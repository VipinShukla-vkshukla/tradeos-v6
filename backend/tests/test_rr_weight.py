"""
rank_weight_rr reduced 1.0 -> 0.4 on tercile evidence (11-Aug-2026).

WHAT THIS CATCHES
------------------
`rank_weight_rr` ran on a silent code default (cfg_float("rank_weight_rr",
1.0)) with no system_config row — invisible to validate_config, ungreppable,
untuned. allocation.scoring.rr_tercile_report() measured the reward:risk
term (implied_rr, falling back to expected_r) against forward R on n=188
resolved CONTINUATION plans and found no monotonic positive separation (LOW
tercile +0.561R vs HIGH tercile +0.448R) — evidence against the full weight
this term was carrying, once final_score's own dominance was already fixed
in migration 060. See KNOWLEDGE_BASE.md and entry_ranking.py's own comment
at this term for the full reasoning.

These tests pin: the reduced weight is what code actually uses now (a
regression here means someone reverted the default without reverting the
evidence), the weight is still an ordinary configurable lever (not
hardcoded), and the rr_cap clamp this reduction sits alongside still works
exactly as it did before.
"""

from __future__ import annotations

from tests import cfg_ctx


def _plan(implied_rr=None, expected_r=None, **kw):
    p = {"symbol": "X", "final_score": 50.0}
    if implied_rr is not None:
        p["implied_rr"] = implied_rr
    if expected_r is not None:
        p["expected_r"] = expected_r
    p.update(kw)
    return p


def test_default_weight_is_point_four_not_one():
    """Regression pin: the code default changed on evidence, not by accident."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        rk = score_plan(_plan(implied_rr=3.0))
    # rp = (min(3.0, 4.0) - 1.5) * 8.0 * weight = 1.5 * 8.0 * weight = 12.0 * weight
    expected_at_04 = 12.0 * 0.4
    expected_at_10 = 12.0 * 1.0
    assert abs(rk.components["rr"] - expected_at_04) < 1e-9, (
        f"rr component {rk.components['rr']} — expected {expected_at_04} "
        f"(weight 0.4); got something consistent with weight "
        f"{rk.components['rr'] / 12.0:.2f} instead. If this is now "
        f"{expected_at_10}, rank_weight_rr's default reverted to 1.0 "
        f"without the evidence in KNOWLEDGE_BASE.md being revisited.")


def test_weight_is_still_an_ordinary_configurable_lever():
    from analysis.entry_ranking import score_plan
    with cfg_ctx({"rank_weight_rr": "0"}):
        off = score_plan(_plan(implied_rr=3.0))
    with cfg_ctx({"rank_weight_rr": "1.0"}):
        full = score_plan(_plan(implied_rr=3.0))
    assert off.components.get("rr", 0.0) == 0.0
    assert full.components["rr"] > off.components.get("rr", 0.0)


def test_rr_cap_still_clamps_an_implausible_ratio_at_the_reduced_weight():
    """The KIMS 19.67 case: cap still applies, only the weight changed."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        capped = score_plan(_plan(implied_rr=19.67))
        at_cap = score_plan(_plan(implied_rr=4.0))
    assert abs(capped.components["rr"] - at_cap.components["rr"]) < 1e-9, (
        "an implied_rr far above rank_rr_cap must score identically to one "
        "AT the cap — the cap, not just the weight, must still be doing "
        "its job")


def test_expected_r_fallback_still_used_when_implied_rr_is_absent():
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        rk = score_plan(_plan(expected_r=2.5))
    assert rk.components.get("rr", 0.0) != 0.0, (
        "expected_r must still be read as the fallback when implied_rr is "
        "absent — the weight change must not have disturbed the chain")


TESTS = [
    ("default weight is 0.4, not 1.0", test_default_weight_is_point_four_not_one),
    ("weight is still an ordinary configurable lever",
     test_weight_is_still_an_ordinary_configurable_lever),
    ("rr_cap still clamps an implausible ratio at the reduced weight",
     test_rr_cap_still_clamps_an_implausible_ratio_at_the_reduced_weight),
    ("expected_r fallback still used when implied_rr is absent",
     test_expected_r_fallback_still_used_when_implied_rr_is_absent),
]
