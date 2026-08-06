"""
`regime_bucket()` against every value each REAL classifier can actually emit.

WHAT THIS CATCHES
-----------------
Two vocabularies reach this function and they were never the same one. It was
written against the SWING regime engine's SPACE-separated states ("RISK ON"),
while its only live caller — `_allocate_shadow`, via `regime=mc.state` — has
always passed the INTRADAY market context's UNDERSCORE-separated states
("RISK_ON").

`"RISK ON" in "RISK_ON"` is False. So the STRONG branch was unreachable from
production for as long as the function existed, and the regime segmentation
migration 044 built was silently pooling everything into WEAK.

A second defect sat in the same line: even fed the correct swing vocabulary,
"TRENDING" — the regime engine's own STRONGEST state — does not contain the
substring "RISK ON" and also returned WEAK.

The values below are not invented. They are the literal returns of
`compute_regime.py::apply_hysteresis()` and `market_context.py::classify()`.
See `docs/TERMINOLOGY.md` for all four vocabularies and why they stay separate.
"""

from __future__ import annotations

from tests import cfg_ctx

#: swing/compute/compute_regime.py -> market_regime.regime
SWING_STATES = {"TRENDING": "STRONG", "RISK ON": "STRONG", "NEUTRAL": "WEAK",
                "RECOVERING": "STRONG", "RISK OFF": "WEAK"}

#: intraday/market_context.py::classify() — the ONLY four it can return
INTRADAY_STATES = {"RISK_ON": "STRONG", "NEUTRAL": "WEAK",
                   "CAUTION": "WEAK", "RISK_OFF": "WEAK"}


def test_swing_vocabulary():
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        for value, want in SWING_STATES.items():
            got = regime_bucket(value)
            assert got == want, f"swing {value!r} -> {got}, expected {want}"


def test_intraday_vocabulary():
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        for value, want in INTRADAY_STATES.items():
            got = regime_bucket(value)
            assert got == want, f"intraday {value!r} -> {got}, expected {want}"


def test_strong_is_reachable_from_the_real_call_site():
    """
    The assertion that would have caught the original bug. `_allocate_shadow`
    passes `mc.state`, which is always the underscore vocabulary — so if no
    intraday state can reach STRONG, the segmentation is dead in production
    however well it behaves on swing strings.
    """
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        buckets = {v: regime_bucket(v) for v in INTRADAY_STATES}
        assert "STRONG" in buckets.values(), (
            f"STRONG is unreachable from the intraday vocabulary {buckets} — "
            f"this is the ONLY vocabulary the live caller supplies, so the "
            f"hurdle's regime segmentation is silently pooling into WEAK")


def test_the_strongest_swing_state_clears_its_own_boundary():
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        assert regime_bucket("TRENDING") == "STRONG", (
            "the swing regime engine's OWN STRONGEST state does not reach "
            "STRONG — whatever match is in use is narrower than the vocabulary "
            "it is written against")


def test_unknown_input_is_weak_not_strong():
    """An unknown market is not evidence of strength."""
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        for value in (None, "", "   ", "garbage", "RISK_MAYBE"):
            assert regime_bucket(value) == "WEAK", \
                f"{value!r} resolved to STRONG — unknown must never mean strong"


def test_case_and_whitespace_tolerance():
    from allocation.hurdle import regime_bucket
    with cfg_ctx():
        assert regime_bucket("risk on") == "STRONG"
        assert regime_bucket("  RISK_ON  ") == "STRONG"


TESTS = [
    ("swing vocabulary maps correctly",        test_swing_vocabulary),
    ("intraday vocabulary maps correctly",     test_intraday_vocabulary),
    ("STRONG reachable from the real caller",  test_strong_is_reachable_from_the_real_call_site),
    ("TRENDING clears its own boundary",       test_the_strongest_swing_state_clears_its_own_boundary),
    ("unknown input is WEAK",                  test_unknown_input_is_weak_not_strong),
    ("case and whitespace tolerated",          test_case_and_whitespace_tolerance),
]
