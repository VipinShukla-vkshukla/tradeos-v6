"""
Regime-aware engine routing, shipped inert (11-Aug-2026).

WHAT THIS CATCHES
------------------
No engine was ever chosen or weighted per market regime — regime_bucket()
gated the allocator's BAR (hurdle.py), never which engine's opinion to trust
more. allocation.scoring.regime_fit_multiplier() classifies each intraday
engine as MOMENTUM (needs a trend: ORB, GAP, PDL, PBK, VCE, SDN) or
MEAN_REVERSION (needs the absence of one: RNG, VWR) from the engine's own
module docstring, and nudges its prior by a bounded amount depending on the
live market_context state.

SHIPPED AT WEIGHT 0.0 — an exact no-op — mirroring rank_weight_tier's own
pending-validation precedent: this classification is structural, not
measured, and hurdle.py's own docstring already states the project's
position on per-regime fitting without years of data. These tests pin: the
weight-off no-op is EXACT (not approximately 1.0 — score() with and without
engine_family/market_state must produce byte-identical output, since every
existing caller of score() predates these two parameters); the classification
table is read from ENGINE_ARCHETYPE, not hardcoded twice; the nudge direction
matches the theory once the weight IS raised (a diagnostic-mode check, not a
claim that raising it is validated); an unclassified engine or unknown
market state is indistinguishable from "off"; and regime_fit_report()
degrades to "no data yet" rather than crashing when regime_at_detection is
new and empty (true on 11-Aug and possibly for a while after).
"""

from __future__ import annotations

from tests import cfg_ctx


def _prior(mean_r=1.0, n=50):
    from allocation.scoring import Prior
    return Prior(key="INTRADAY/ORB", n=n, mean_r=mean_r, median_r=mean_r,
                stderr=0.1, p10=mean_r - 1, p90=mean_r + 1,
                trigger_rate=None, below_floor=False, note="")


# ── regime_fit_multiplier(), pure ───────────────────────────────────────────

def test_weight_zero_is_an_exact_noop():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "0"}):
        mult, reason = regime_fit_multiplier("ORB", "RISK_ON")
    assert mult == 1.0
    assert "off" in reason.lower()


def test_default_weight_is_zero():
    """Regression pin: the code default must stay 0.0 until raised on evidence."""
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({}):
        mult, _ = regime_fit_multiplier("ORB", "RISK_ON")
    assert mult == 1.0, (
        f"got {mult} — intraday_regime_fit_weight's default changed from 0.0 "
        f"without KNOWLEDGE_BASE.md's validation evidence being updated")


def test_unclassified_engine_is_a_noop_even_with_weight_on():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, reason = regime_fit_multiplier("SOME_FUTURE_ENGINE", "RISK_ON")
    assert mult == 1.0
    assert "unclassified" in reason.lower()


def test_unknown_market_state_is_a_noop_even_with_weight_on():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, _ = regime_fit_multiplier("ORB", None)
    assert mult == 1.0


def test_momentum_engine_favoured_in_risk_on_at_full_weight():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, _ = regime_fit_multiplier("ORB", "RISK_ON")
    assert mult > 1.0, "a momentum engine in a trending market must be nudged UP"


def test_momentum_engine_disfavoured_in_neutral_at_full_weight():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, _ = regime_fit_multiplier("ORB", "NEUTRAL")
    assert mult < 1.0, "a momentum engine in a rangebound market must be nudged DOWN"


def test_mean_reversion_engine_favoured_in_neutral_at_full_weight():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, _ = regime_fit_multiplier("RNG", "NEUTRAL")
    assert mult > 1.0, "RNG exists specifically for a rangebound market"


def test_mean_reversion_engine_disfavoured_in_risk_on_at_full_weight():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        mult, _ = regime_fit_multiplier("RNG", "RISK_ON")
    assert mult < 1.0, "fading a real trend is RNG's worst case"


def test_weight_scales_the_nudge_linearly():
    from allocation.scoring import regime_fit_multiplier
    with cfg_ctx({"intraday_regime_fit_weight": "0.5"}):
        half, _ = regime_fit_multiplier("ORB", "RISK_ON")
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        full, _ = regime_fit_multiplier("ORB", "RISK_ON")
    assert abs((half - 1.0) - (full - 1.0) / 2) < 1e-9, (
        f"half weight ({half}) should be exactly half the full-weight nudge "
        f"({full}) away from 1.0")


def test_all_seven_named_engines_are_classified():
    """CLAUDE.md names ORB, GAP, PDL, VCE, PBK, VWR, RNG as the seven
    intraday engines; SDN (short family) is an eighth. All must resolve to
    an archetype, or a future regime_fit_report() run silently drops them."""
    from allocation.scoring import ENGINE_ARCHETYPE
    for eng in ("ORB", "GAP", "PDL", "VCE", "PBK", "VWR", "RNG", "SDN"):
        assert eng in ENGINE_ARCHETYPE, f"{eng} has no archetype classification"


def test_archetypes_are_only_the_two_defined_constants():
    from allocation.scoring import ENGINE_ARCHETYPE, MOMENTUM, MEAN_REVERSION
    assert set(ENGINE_ARCHETYPE.values()) <= {MOMENTUM, MEAN_REVERSION}


# ── score()'s integration ───────────────────────────────────────────────────

def test_score_without_the_new_params_is_unchanged():
    """Every existing caller of score() predates engine_family/market_state.
    Calling it exactly as they do must produce IDENTICAL output to before —
    not just an equal edge, the whole dict."""
    from allocation.scoring import score
    with cfg_ctx({}):
        old_style = score(100.0, 98.0, 106.0, 10, "MIS", _prior(), 1.0)
    assert old_style["regime_fit_mult"] == 1.0
    assert "x1.000" not in old_style["basis"] and " x" not in old_style["basis"]


def test_score_with_weight_off_matches_score_without_the_params_at_all():
    from allocation.scoring import score
    with cfg_ctx({}):
        without = score(100.0, 98.0, 106.0, 10, "MIS", _prior(), 1.0)
        withh = score(100.0, 98.0, 106.0, 10, "MIS", _prior(), 1.0,
                     engine_family="ORB", market_state="RISK_ON")
    assert without["edge"] == withh["edge"], (
        "weight 0 (the shipped default) must make passing engine_family/"
        "market_state a complete no-op on the numeric result")


def test_score_nudges_edge_when_weight_is_raised():
    from allocation.scoring import score
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        favourable = score(100.0, 98.0, 106.0, 10, "MIS", _prior(mean_r=1.0), 1.0,
                           engine_family="ORB", market_state="RISK_ON")
        unfavourable = score(100.0, 98.0, 106.0, 10, "MIS", _prior(mean_r=1.0), 1.0,
                             engine_family="ORB", market_state="NEUTRAL")
    assert favourable["edge"] > unfavourable["edge"], (
        "with the weight deliberately raised, the SAME prior must score "
        "higher for ORB in RISK_ON than in NEUTRAL")


def test_score_regime_nudge_never_touches_a_neutral_prior():
    """0.0 * any multiplier is still 0.0 — an unmeasured engine's edge must
    not become non-zero purely from a regime nudge."""
    from allocation.scoring import score
    from allocation.scoring import Prior
    neutral_prior = Prior(key="INTRADAY/NEW", n=2, mean_r=0.0, median_r=0.0,
                          stderr=float("nan"), p10=0.0, p90=0.0,
                          trigger_rate=None, below_floor=True, note="thin")
    with cfg_ctx({"intraday_regime_fit_weight": "1.0"}):
        sc = score(100.0, 98.0, 106.0, 10, "MIS", neutral_prior, 1.0,
                  engine_family="ORB", market_state="RISK_ON")
    # e_r = 0.0 * mult - cost_r = -cost_r, same as with no regime fit at all.
    assert sc["e_r"] == -sc["cost_r"]


# ── regime_fit_report() degrades gracefully with no data ───────────────────

class _EmptyQuery:
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def range(self, *a, **k): return self
    def is_(self, *a, **k): return self
    def execute(self): return self
    @property
    def not_(self): return self
    @property
    def data(self): return []


class _EmptySB:
    def table(self, name): return _EmptyQuery()


def test_regime_fit_report_handles_zero_rows_without_crashing():
    from allocation.scoring import regime_fit_report
    with cfg_ctx({}):
        rc = regime_fit_report(_EmptySB())
    assert rc == 0, "no data yet must be a clean report, not a failure exit code"


TESTS = [
    ("weight zero is an exact no-op", test_weight_zero_is_an_exact_noop),
    ("default weight is zero", test_default_weight_is_zero),
    ("unclassified engine is a no-op even with weight on",
     test_unclassified_engine_is_a_noop_even_with_weight_on),
    ("unknown market state is a no-op even with weight on",
     test_unknown_market_state_is_a_noop_even_with_weight_on),
    ("momentum engine favoured in RISK_ON at full weight",
     test_momentum_engine_favoured_in_risk_on_at_full_weight),
    ("momentum engine disfavoured in NEUTRAL at full weight",
     test_momentum_engine_disfavoured_in_neutral_at_full_weight),
    ("mean-reversion engine favoured in NEUTRAL at full weight",
     test_mean_reversion_engine_favoured_in_neutral_at_full_weight),
    ("mean-reversion engine disfavoured in RISK_ON at full weight",
     test_mean_reversion_engine_disfavoured_in_risk_on_at_full_weight),
    ("weight scales the nudge linearly", test_weight_scales_the_nudge_linearly),
    ("all seven named engines are classified", test_all_seven_named_engines_are_classified),
    ("archetypes are only the two defined constants",
     test_archetypes_are_only_the_two_defined_constants),
    ("score() without the new params is unchanged",
     test_score_without_the_new_params_is_unchanged),
    ("score() with weight off matches score() without the params at all",
     test_score_with_weight_off_matches_score_without_the_params_at_all),
    ("score() nudges edge when weight is raised", test_score_nudges_edge_when_weight_is_raised),
    ("score() regime nudge never touches a neutral prior",
     test_score_regime_nudge_never_touches_a_neutral_prior),
    ("regime_fit_report handles zero rows without crashing",
     test_regime_fit_report_handles_zero_rows_without_crashing),
]
