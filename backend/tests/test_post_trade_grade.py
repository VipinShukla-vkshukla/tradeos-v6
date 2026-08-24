"""
F-68 follow-up, 24-Aug-2026 — `entry_grade` persistence (Track E, Stage E2
-> E6 prerequisite).

`grade_trade_entry()` has existed and been called on every analysed closed
trade since this file existed, but its return value was used only to word
a lesson's prose and tallied into a run-level log line, then discarded —
`entry_grade` (migration 093) is the first time it survives to a durable
column. It had no test coverage before this either; both matter now that
a Stage E6 correlation study depends on it being computed correctly.

Real-data anchor: `backfill_entry_grades()` run live, 24-Aug-2026, 88
closed SWING trades — GABRIEL (bought despite the pipeline's own
AVOID_ENTRY three sessions running, CLAUDE.md's own landmine) graded D.
The correlation itself, run the same session: grade C (n=76) sits near
breakeven (avg R -0.01), grade D (n=10) outperforms it (avg R +0.30) —
the OPPOSITE of what the grade claims to measure, on a sample too thin
(n=10) to trust either way. Not wired into any live decision for exactly
that reason — see docs/FINDINGS.md F-68 follow-up.
"""

from __future__ import annotations


def _ctx(**kw) -> dict:
    base = {"signal_type": "BUY_CANDIDATE", "score_adjusted": 60,
            "struct_edge": "NO", "risk_score": 40, "regime": "NEUTRAL",
            "entry_timing_type": "OPTIMAL", "reentry_mode": "",
            "trend_maturity": "", "breakout_readiness": 0}
    base.update(kw)
    return base


def test_no_signal_context_defaults_to_c():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry({}, {}) == "C"


def test_grade_f_risk_off_regime():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(_ctx(regime="RISK OFF"), {}) == "F"


def test_grade_f_score_below_45():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(_ctx(score_adjusted=30), {}) == "F"


def test_grade_f_extended_timing_without_reentry():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(
        _ctx(entry_timing_type="EXTENDED", reentry_mode=""), {}) == "F"


def test_grade_d_elevated_risk_score():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(_ctx(risk_score=70), {}) == "D"


def test_grade_d_caution_regime():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(_ctx(regime="CAUTION"), {}) == "D"


def test_grade_d_exhausted_trend():
    from ai.post_trade_analysis import grade_trade_entry
    assert grade_trade_entry(_ctx(trend_maturity="EXHAUSTED"), {}) == "D"


def test_grade_a_prime_setup_fully_aligned():
    from ai.post_trade_analysis import grade_trade_entry
    ctx = _ctx(signal_type="PRIME_SETUP", score_adjusted=80,
               struct_edge="YES", risk_score=30, regime="BULLISH")
    assert grade_trade_entry(ctx, {}) == "A"


def test_grade_a_downgrades_to_b_without_struct_edge():
    """Same as the A case above but struct_edge=NO — must NOT be A."""
    from ai.post_trade_analysis import grade_trade_entry
    ctx = _ctx(signal_type="PRIME_SETUP", score_adjusted=80,
               struct_edge="NO", risk_score=30, regime="BULLISH")
    assert grade_trade_entry(ctx, {}) == "B"


def test_grade_b_breakout_setup_decent_score():
    from ai.post_trade_analysis import grade_trade_entry
    ctx = _ctx(signal_type="BREAKOUT_SETUP", score_adjusted=65)
    assert grade_trade_entry(ctx, {}) == "B"


def test_grade_c_standard_buy_candidate():
    from ai.post_trade_analysis import grade_trade_entry
    ctx = _ctx(signal_type="BUY_CANDIDATE", score_adjusted=58)
    assert grade_trade_entry(ctx, {}) == "C"


def test_gabriel_shaped_context_grades_d():
    """
    Real-data anchor, not a synthetic fixture. GABRIEL's own 03-Aug review
    carried eap_action=AVOID_ENTRY and elevated risk before it was bought
    on 06-Aug anyway (CLAUDE.md's own landmine). A high risk_score is
    exactly what grade_trade_entry() reads to catch this shape — confirmed
    live in backfill_entry_grades()'s real run: GABRIEL graded D.
    """
    from ai.post_trade_analysis import grade_trade_entry
    ctx = _ctx(signal_type="BUY_CANDIDATE", score_adjusted=68, risk_score=72)
    assert grade_trade_entry(ctx, {}) == "D"


TESTS = [
    ("no signal context defaults to C", test_no_signal_context_defaults_to_c),
    ("grade F on RISK OFF regime", test_grade_f_risk_off_regime),
    ("grade F below score 45", test_grade_f_score_below_45),
    ("grade F extended timing without reentry", test_grade_f_extended_timing_without_reentry),
    ("grade D elevated risk score", test_grade_d_elevated_risk_score),
    ("grade D caution regime", test_grade_d_caution_regime),
    ("grade D exhausted trend", test_grade_d_exhausted_trend),
    ("grade A prime setup fully aligned", test_grade_a_prime_setup_fully_aligned),
    ("grade A downgrades to B without struct edge", test_grade_a_downgrades_to_b_without_struct_edge),
    ("grade B breakout setup decent score", test_grade_b_breakout_setup_decent_score),
    ("grade C standard buy candidate", test_grade_c_standard_buy_candidate),
    ("GABRIEL-shaped context grades D", test_gabriel_shaped_context_grades_d),
]
