"""
final_score stopped dominating entry_ranking by sheer magnitude (10-Aug-2026).

WHAT THIS CATCHES
------------------
Every component `score_plan()` adds is a small delta from a neutral midpoint,
gated by its own `cfg_float` weight — except `final_score`, which was added at
its full 0-100 value with no weight at all. A screener score of 80 alone
contributed +80 points, more than every other component in the function
combined, so a plan could out-rank a better R:R / better-timed / AI-reviewed
plan purely because its screener score happened to be high — even though the
KB's own tercile measurement (0.516 / 0.491 / 0.511R, n=125 resolved
CONTINUATION plans) found final_score flat against forward R.

The operator's instruction was explicit: fix this "end to end" — not a
partial tweak. These tests pin: the component is now centered like its
peers, it is now weighted (so it can be tuned to zero without touching code,
the same lever `rank_weight_tier` already uses), and — the actual bug — a
plan with a stronger real signal (R:R, timing) now beats a plan that only
had a higher raw screener score.
"""

from __future__ import annotations

from tests import cfg_ctx


def _plan(final_score, **kw):
    p = {"symbol": "X", "final_score": final_score}
    p.update(kw)
    return p


def test_screener_component_is_centered_not_raw():
    """A final_score of 50 (the neutral midpoint) must contribute ~0, not 50 —
    the exact accounting error this fix closes."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        rk = score_plan(_plan(50.0))
    assert abs(rk.components["screener"]) < 1e-9, (
        f"final_score=50 (neutral) contributed {rk.components['screener']}, "
        f"expected ~0 — the component must be centered like every other one")


def test_screener_component_respects_its_own_weight():
    """rank_weight_screener must scale the (centered) contribution, the same
    lever rank_weight_tier and rank_weight_conviction already use — proving
    the term is no longer hardcoded at implicit weight 1.0."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({"rank_weight_screener": "0.0"}):
        off = score_plan(_plan(90.0))
    with cfg_ctx({"rank_weight_screener": "1.0"}):
        full = score_plan(_plan(90.0))
    assert off.components.get("screener", 0.0) == 0.0
    assert full.components["screener"] > off.components.get("screener", 0.0)


def test_default_weight_no_longer_lets_screener_dominate_the_sum():
    """Pinned regression: pre-fix, final_score=90 alone contributed +90 —
    larger than the entire rest of the function's plausible range. Post-fix,
    at the shipped default (0.3), it must stay a minor contributor."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        rk = score_plan(_plan(90.0))
    assert abs(rk.components["screener"]) < 10.0, (
        f"screener component is {rk.components['screener']} at the default "
        f"weight — still large enough to dominate a typical plan's total")


def test_a_better_real_signal_now_outranks_a_higher_screener_score():
    """The actual bug, end to end: a plan with a mediocre screener score but
    a strong live R:R must be able to outrank a plan with a high screener
    score and a weak R:R — impossible before this fix, since final_score
    alone (up to +100) swamped rr's bounded contribution (max ~+20)."""
    from analysis.entry_ranking import rank
    weak_rr_high_screener = _plan(95.0, symbol="A", implied_rr=1.6,
                                   entry_timing_type="LATE")
    strong_rr_low_screener = _plan(55.0, symbol="B", implied_rr=3.8,
                                    entry_timing_type="OPTIMAL")
    with cfg_ctx({}):
        ranked = rank([weak_rr_high_screener, strong_rr_low_screener])
    assert ranked[0].symbol == "B", (
        f"expected the plan with the real edge (better R:R, OPTIMAL timing) "
        f"to rank first; got {[r.symbol for r in ranked]} — final_score is "
        f"still dominating the ranking")


def test_reason_string_still_starts_with_screener():
    """`why()`'s sort keys off the first word of each reason string matching
    a comp dict key (`comp.get(r.split()[0].lower())`) — must not regress."""
    from analysis.entry_ranking import score_plan
    with cfg_ctx({}):
        rk = score_plan(_plan(70.0))
    assert rk.reasons[0].startswith("screener")


TESTS = [
    ("screener component is centered, not raw",
     test_screener_component_is_centered_not_raw),
    ("screener component respects its own weight",
     test_screener_component_respects_its_own_weight),
    ("default weight no longer lets screener dominate the sum",
     test_default_weight_no_longer_lets_screener_dominate_the_sum),
    ("a better real signal now outranks a higher screener score",
     test_a_better_real_signal_now_outranks_a_higher_screener_score),
    ("reason string still starts with 'screener'",
     test_reason_string_still_starts_with_screener),
]
