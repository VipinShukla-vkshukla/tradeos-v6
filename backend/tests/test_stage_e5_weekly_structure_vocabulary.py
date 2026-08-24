"""
Track E, Stage E5 (docs/TRADEOS_ROADMAP.md, piece 3) — assess_trend()'s
weekly_structure check matched against HIGHER/UPTREND/BULLISH/HH and
LOWER/DOWNTREND/BEARISH/LL. compute_msl.py has never emitted any of those
— its actual four values are STRONG/CONSOLIDATING/CAUTION/WEAK (1886/395/
263/228 rows respectively, confirmed live via SQL, 24-Aug-2026). So this
check could not match either branch for ANY position, ever — yet
`checks += 1` still fired whenever the field was non-empty (the majority
of rows), silently deflating `score = len(for_) / checks` for most trend
assessments this whole session's Stage E4 work (deterioration_check,
early invalidation, the 3R runner decision) reads `tq.verdict` from. Same
shape as the documented "RISK ON" vs "RISK_ON" collision (migration 048).

compute_msl.py's own classification: STRONG = weekly higher-high AND
higher-low (score 90); CONSOLIDATING = higher-low only (65, still
building a base); CAUTION = higher-high only (42 — a new high WITHOUT
the higher-low sequence, a real distribution warning); WEAK = neither
(15). CONSOLIDATING stays neutral (checks not incremented) — genuinely
ambiguous, matching this function's own "missing inputs count as
neither" philosophy rather than reinterpreting an ambiguous state as
bullish evidence.
"""

from __future__ import annotations


def test_strong_counts_as_for():
    from control.exit_rules import assess_trend
    tq = assess_trend({"weekly_structure": "STRONG"})
    assert tq.checks == 1, f"expected exactly 1 check counted, got {tq.checks}"
    assert tq.score == 1.0, f"STRONG alone must score 1.0, got {tq.score}"
    assert any("strong" in r for r in tq.reasons), (
        f"STRONG must appear in the for_ reasons, got {tq.reasons}")


def test_weak_counts_as_against():
    from control.exit_rules import assess_trend
    tq = assess_trend({"weekly_structure": "WEAK"})
    assert tq.checks == 1, f"expected exactly 1 check counted, got {tq.checks}"
    assert tq.score == 0.0, f"WEAK alone must score 0.0, got {tq.score}"
    assert any("weak" in a for a in tq.against), (
        f"WEAK must appear in the against reasons, got {tq.against}")


def test_caution_counts_as_against():
    """A new weekly high WITHOUT the higher-low sequence — a real
    distribution warning, not a reason for extra patience."""
    from control.exit_rules import assess_trend
    tq = assess_trend({"weekly_structure": "CAUTION"})
    assert tq.checks == 1, f"expected exactly 1 check counted, got {tq.checks}"
    assert tq.score == 0.0, f"CAUTION alone must score 0.0, got {tq.score}"
    assert any("caution" in a for a in tq.against), (
        f"CAUTION must appear in the against reasons, got {tq.against}")


def test_consolidating_stays_neutral_not_counted():
    """Genuinely ambiguous (holding support, not yet confirming either
    way) — must not force a directional read, and must not even count
    toward the checks denominator (that WAS the bug: counting toward the
    denominator without ever contributing to for_ or against silently
    deflated every other position's score)."""
    from control.exit_rules import assess_trend
    tq = assess_trend({"weekly_structure": "CONSOLIDATING"})
    assert tq.checks == 0, (
        f"CONSOLIDATING alone must not increment checks at all — the "
        f"original bug's exact shape, just from a different value — got "
        f"checks={tq.checks}")


def test_strong_no_longer_dilutes_a_real_evidence_set():
    """The quantified impact: a position with real bullish evidence
    elsewhere (RSI in trend) plus a STRONG weekly structure must now
    score a full 1.0, not 0.5 — under the pre-fix vocabulary, STRONG
    incremented checks to 2 while contributing to for_ zero times,
    diluting an otherwise-clean signal to exactly half credit."""
    from control.exit_rules import assess_trend
    tq = assess_trend({"weekly_structure": "STRONG", "rsi_daily": 60.0})
    assert tq.checks == 2, f"expected 2 checks (weekly + RSI), got {tq.checks}"
    assert tq.score == 1.0, (
        f"both real signals point the same way — score must be a clean "
        f"1.0, not diluted by a check that could never contribute; got "
        f"{tq.score}")


TESTS = [
    ("STRONG weekly structure counts as for_", test_strong_counts_as_for),
    ("WEAK weekly structure counts as against", test_weak_counts_as_against),
    ("CAUTION weekly structure counts as against",
     test_caution_counts_as_against),
    ("CONSOLIDATING stays neutral, not counted",
     test_consolidating_stays_neutral_not_counted),
    ("STRONG no longer dilutes a real evidence set",
     test_strong_no_longer_dilutes_a_real_evidence_set),
]
