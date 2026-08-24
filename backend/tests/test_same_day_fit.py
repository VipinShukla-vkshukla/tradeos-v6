"""
allocation/scoring.py::same_day_fit_multiplier() and Prior.hit_rate —
Stage D5, 24-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D, branch
feat/intraday-regression-shadow).

WHAT THIS COVERS
-----------------
Same discipline as test_regime_fit.py for its sibling mechanism: SHIPPED
AT WEIGHT 0.0, an exact no-op, pinned here so a future change to the
default cannot slip past silently. The weight-off no-op must be EXACT —
score() itself is unaffected in this stage since same_day_fit_multiplier()
is not yet called from it at all (Stage 1 is calibration-only; see
tools/same_day_calibration.py's own module docstring) — so these tests
exercise the function directly, the only caller that exists today.

Also pins hit_rate on Prior: computed from the exact same `values` list
mean_r/median_r already come from, appended as the dataclass's LAST field
so no existing positional Prior(...) construction anywhere in this
codebase needs to change.
"""

from __future__ import annotations

from tests import cfg_ctx


def _prior(n=50, hit_rate=0.60, below_floor=False):
    from allocation.scoring import Prior
    return Prior(key="INTRADAY/ORB", n=n, mean_r=0.1, median_r=0.1,
                stderr=0.1, p10=-0.5, p90=0.8, trigger_rate=None,
                below_floor=below_floor, note="", hit_rate=hit_rate)


# ── Prior.hit_rate, via _dist() ─────────────────────────────────────────────

def test_dist_computes_hit_rate_from_the_same_values():
    from allocation.scoring import _dist
    p = _dist("INTRADAY/ORB", [1.0, -0.5, 0.3, -0.2, 2.0], floor=1)
    assert p.hit_rate == 3 / 5


def test_dist_below_floor_leaves_hit_rate_none():
    from allocation.scoring import _dist
    p = _dist("INTRADAY/ORB", [1.0], floor=5)
    assert p.below_floor is True
    assert p.hit_rate is None


def test_dist_all_wins_is_hit_rate_one():
    from allocation.scoring import _dist
    p = _dist("INTRADAY/ORB", [0.5, 1.0, 0.2], floor=1)
    assert p.hit_rate == 1.0


def test_dist_all_losses_is_hit_rate_zero():
    from allocation.scoring import _dist
    p = _dist("INTRADAY/ORB", [-0.5, -1.0, -0.2], floor=1)
    assert p.hit_rate == 0.0


def test_positional_prior_construction_still_compiles_unchanged():
    """Regression pin: hit_rate was appended LAST specifically so no
    existing positional Prior(...) call site (this module's own below-
    floor branch, every test fixture across the suite) needs to change."""
    from allocation.scoring import Prior
    p = Prior("t", 100, 0.10, 0.05, 0.02, -1.0, 1.5)
    assert p.hit_rate is None


# ── same_day_fit_multiplier(), pure ─────────────────────────────────────────

def test_weight_zero_is_an_exact_noop():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "0"}):
        mult, reason = same_day_fit_multiplier("ORB", _prior(), today_wins=0, today_n=10)
    assert mult == 1.0
    assert "off" in reason.lower()


def test_default_weight_is_zero():
    """Regression pin, same as regime_fit_multiplier's own: the code
    default must stay 0.0 until Stage 1's own calibration evidence says
    otherwise (Gate D5)."""
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({}):
        mult, _ = same_day_fit_multiplier("ORB", _prior(), today_wins=0, today_n=10)
    assert mult == 1.0, (
        f"got {mult} — intraday_same_day_fit_weight's default changed from "
        f"0.0 without Gate D5's calibration evidence being updated")


def test_no_engine_family_is_a_noop_even_with_weight_on():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        mult, reason = same_day_fit_multiplier(None, _prior(), today_wins=0, today_n=10)
    assert mult == 1.0
    assert "off" in reason.lower()


def test_no_historical_prior_is_a_noop():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        mult, reason = same_day_fit_multiplier("ORB", None, today_wins=0, today_n=10)
    assert mult == 1.0
    assert "no opinion" in reason.lower()


def test_below_floor_historical_prior_is_a_noop():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        mult, reason = same_day_fit_multiplier(
            "ORB", _prior(below_floor=True), today_wins=0, today_n=10)
    assert mult == 1.0
    assert "no opinion" in reason.lower()


def test_below_min_n_today_is_a_noop():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5"}):
        # 0-for-3 against a 60% history is a real underperformance, but 3
        # trials is below the floor -- must still be a no-op.
        mult, reason = same_day_fit_multiplier("ORB", _prior(hit_rate=0.60),
                                               today_wins=0, today_n=3)
    assert mult == 1.0
    assert "floor" in reason.lower()


def test_degenerate_historical_hit_rate_is_a_noop():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        mult, reason = same_day_fit_multiplier(
            "ORB", _prior(hit_rate=1.0), today_wins=0, today_n=10)
    assert mult == 1.0
    assert "degenerate" in reason.lower()


def test_ordinary_today_within_noise_is_a_noop():
    """1-for-6 against a 62% history is well within binomial noise at
    n=6 -- must NOT be flagged."""
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5"}):
        mult, reason = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                               today_wins=2, today_n=6)
    assert mult == 1.0
    assert "not a statistical" in reason.lower()


def test_real_underperformance_is_flagged_and_dampened():
    """0-for-6 against a 62% history IS a statistical outlier
    (p ≈ 0.0035 one-sided) -- must be flagged and dampen size."""
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5",
                  "intraday_same_day_fit_max_dampen": "0.30"}):
        mult, reason = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                               today_wins=0, today_n=6)
    assert mult < 1.0, f"expected a dampened multiplier, got {mult}"
    assert abs(mult - 0.70) < 1e-9, f"expected 1.0 - 0.30*1.0 = 0.70, got {mult}"
    assert "outlier" in reason.lower()


def test_a_good_today_is_never_boosted():
    """The dampener is ONE-DIRECTIONAL by design: 6-for-6 against a 62%
    history is unusual too, but must never produce mult > 1.0."""
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5"}):
        mult, reason = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                               today_wins=6, today_n=6)
    assert mult <= 1.0, f"a good day must never boost size, got {mult}"


def test_weight_scales_the_dampening_linearly():
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "0.5",
                  "intraday_same_day_fit_min_n": "5",
                  "intraday_same_day_fit_max_dampen": "0.30"}):
        half, _ = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                          today_wins=0, today_n=6)
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5",
                  "intraday_same_day_fit_max_dampen": "0.30"}):
        full, _ = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                          today_wins=0, today_n=6)
    assert abs(half - 0.85) < 1e-9    # 1.0 - 0.30*0.5
    assert abs(full - 0.70) < 1e-9    # 1.0 - 0.30*1.0
    assert half > full, "half weight must dampen LESS than full weight"


def test_alpha_gates_what_counts_as_an_outlier():
    """A stricter alpha (smaller) makes the same underperformance harder
    to flag -- 1-for-6 has p ≈ 0.032, between a loose and a tight alpha."""
    from allocation.scoring import same_day_fit_multiplier
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5",
                  "intraday_same_day_fit_alpha": "0.10"}):
        loose, _ = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                           today_wins=1, today_n=6)
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_min_n": "5",
                  "intraday_same_day_fit_alpha": "0.01"}):
        tight, _ = same_day_fit_multiplier("ORB", _prior(hit_rate=0.62),
                                           today_wins=1, today_n=6)
    assert loose < 1.0, "p≈0.032 must clear a 0.10 alpha"
    assert tight == 1.0, "p≈0.032 must NOT clear a 0.01 alpha"


TESTS = [
    ("_dist computes hit_rate from the same values", test_dist_computes_hit_rate_from_the_same_values),
    ("_dist below floor leaves hit_rate None", test_dist_below_floor_leaves_hit_rate_none),
    ("_dist all wins is hit_rate 1.0", test_dist_all_wins_is_hit_rate_one),
    ("_dist all losses is hit_rate 0.0", test_dist_all_losses_is_hit_rate_zero),
    ("positional Prior construction still compiles unchanged", test_positional_prior_construction_still_compiles_unchanged),
    ("weight zero is an exact no-op", test_weight_zero_is_an_exact_noop),
    ("default weight is zero", test_default_weight_is_zero),
    ("no engine family is a no-op even with weight on", test_no_engine_family_is_a_noop_even_with_weight_on),
    ("no historical prior is a no-op", test_no_historical_prior_is_a_noop),
    ("below-floor historical prior is a no-op", test_below_floor_historical_prior_is_a_noop),
    ("below min_n today is a no-op", test_below_min_n_today_is_a_noop),
    ("degenerate historical hit-rate is a no-op", test_degenerate_historical_hit_rate_is_a_noop),
    ("ordinary today within noise is a no-op", test_ordinary_today_within_noise_is_a_noop),
    ("real underperformance is flagged and dampened", test_real_underperformance_is_flagged_and_dampened),
    ("a good today is never boosted", test_a_good_today_is_never_boosted),
    ("weight scales the dampening linearly", test_weight_scales_the_dampening_linearly),
    ("alpha gates what counts as an outlier", test_alpha_gates_what_counts_as_an_outlier),
]
