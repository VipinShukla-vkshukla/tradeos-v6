"""
AVOID stops hard-blocking (29-Aug-2026).

Measured against 24 sessions / 17,545 resolved intraday_setups rows
(28-Jul to 28-Aug-2026): VETOED_AI (n=1256) resolved at -0.034% net,
TAKEN (n=4110) resolved at -0.302% net — inverted, stable across two
sub-periods and within every engine checked. AVOID now applies its own
negative confidence_delta like PREFER does, instead of unilaterally
killing a setup nothing else rejected. docs/FINDINGS.md has the full
quantify. `intraday_ai_avoid_hard_block` restores the old behavior.
"""

from __future__ import annotations

from tests import cfg_ctx
from intraday.ai_advisor import Advice, apply
from intraday.strategies.base import Setup


def _setup(confidence: float = 0.60) -> Setup:
    return Setup(symbol="TCS", strategy="ORB", direction="LONG",
                 entry=100.0, stop=99.0, target=102.0,
                 confidence=confidence, rationale="x", invalidation="x")


def test_avoid_no_longer_blocks_by_default():
    s = _setup(0.60)
    advice = {"TCS": Advice("TCS", "ORB", "AVOID", -0.25, "crowded sector", "ai")}
    with cfg_ctx({}):
        allow, conf, note = apply(s, advice)
    assert allow is True, "AVOID must not hard-block unless intraday_ai_avoid_hard_block is set"
    assert conf == 0.35, f"expected 0.60 - 0.25 = 0.35, got {conf}"
    assert "crowded sector" in note


def test_avoid_hard_block_switch_restores_old_behavior():
    s = _setup(0.60)
    advice = {"TCS": Advice("TCS", "ORB", "AVOID", -0.25, "crowded sector", "ai")}
    with cfg_ctx({"intraday_ai_avoid_hard_block": "true"}):
        allow, conf, note = apply(s, advice)
    assert allow is False
    assert conf == 0.60, "confidence must be untouched on a hard block"
    assert "AI veto" in note


def test_avoid_confidence_clamped_at_floor():
    s = _setup(0.10)
    advice = {"TCS": Advice("TCS", "ORB", "AVOID", -0.25, "weak", "ai")}
    with cfg_ctx({}):
        allow, conf, _ = apply(s, advice)
    assert allow is True
    assert conf == 0.05, f"must clamp at the 0.05 floor, got {conf}"


def test_prefer_and_neutral_unaffected():
    s = _setup(0.60)
    for verdict, delta in (("PREFER", 0.10), ("NEUTRAL", 0.05)):
        advice = {"TCS": Advice("TCS", "ORB", verdict, delta, "ok", "ai")}
        with cfg_ctx({}):
            allow, conf, _ = apply(s, advice)
        assert allow is True
        assert conf == round(0.60 + delta, 2), f"{verdict}: expected {0.60 + delta}, got {conf}"


def test_no_advice_passes_through_unchanged():
    s = _setup(0.60)
    with cfg_ctx({}):
        allow, conf, note = apply(s, {})
    assert allow is True
    assert conf == 0.60
    assert note == ""


TESTS = [
    ("AVOID no longer blocks by default", test_avoid_no_longer_blocks_by_default),
    ("intraday_ai_avoid_hard_block restores the old block",
     test_avoid_hard_block_switch_restores_old_behavior),
    ("AVOID confidence clamped at the 0.05 floor", test_avoid_confidence_clamped_at_floor),
    ("PREFER/NEUTRAL unaffected by the AVOID change", test_prefer_and_neutral_unaffected),
    ("no advice passes a setup through unchanged", test_no_advice_passes_through_unchanged),
]
