"""
Does a swing Proposal ever carry the engine that actually produced it?

WHAT THIS CATCHES — 06-Aug-2026
--------------------------------
Two independent bugs compounded so that the answer was NO, always, for every
swing proposal the allocator ever scored:

  1. `intraday/engine.py::_allocate_shadow` read `e.get("plan")` from the
     dicts `evaluate_candidates` builds, which have never had a "plan" key —
     only "candidate". `from_swing` therefore always received `plan=None`.
  2. `allocation/proposal.py::from_swing` read `plan.get("strategy_source")`,
     a name that is real earlier in the swing pipeline (screen_stocks.py,
     compute_msl.py) but does not exist on a `signal_output_daily` row or the
     candidate dicts built from one — the real column there is `strategy`.

Either bug alone would have been enough; both were present, so `source`
silently fell to the literal fallback "CONTINUATION" for every swing
proposal regardless of which engine fired, and `native_rank` (from
`final_score`) was always 0.0. Meanwhile `scoring.swing_priors()` was keying
its own buckets on `signal_type` — a WORKFLOW status (WATCH, BUY_CANDIDATE,
...), never an engine name — so even a correctly-wired `source` would not
have found a matching class key. `swing_family()` is now the one function
both sides use to turn a raw `strategy` string into a key, so they cannot
drift apart the way `signal_type` vs `strategy_source` vs `strategy` just did.
"""

from __future__ import annotations

from tests import cfg_ctx


def test_swing_family_maps_single_engines():
    from allocation.scoring import swing_family
    assert swing_family("CTL") == "CONTINUATION"
    assert swing_family("SEC") == "CONTINUATION"
    assert swing_family("MOM") == "MOM"
    assert swing_family("RVS") == "RVS"
    assert swing_family("PEAD") == "PEAD"
    assert swing_family("ACC") == "ACC"


def test_swing_family_combo_of_continuation_engines_stays_continuation():
    from allocation.scoring import swing_family
    assert swing_family("CTL+SEC") == "CONTINUATION"
    assert swing_family("CTL+RSB+SEC") == "CONTINUATION"


def test_swing_family_mixed_combo_prefers_the_isolated_family():
    """
    A combo that includes an engine the blueprint deliberately split out
    (MOM, RVS, PEAD, ACC) must not be silently diluted back into
    CONTINUATION — that dilution is the exact contamination the family split
    exists to prevent.
    """
    from allocation.scoring import swing_family
    assert swing_family("CTL+MOM+SEC") == "MOM"
    assert swing_family("RVS+SEC") == "RVS"


def test_swing_family_empty_or_unknown_falls_back_to_all():
    from allocation.scoring import swing_family
    assert swing_family(None) == "ALL"
    assert swing_family("") == "ALL"
    assert swing_family("NOT_A_REAL_ENGINE") == "ALL"


def test_swing_family_strips_a_legacy_parenthetical_annotation():
    """
    07-Aug-2026: closed_positions.strategy carries "CTL (Legacy)" for 21 of
    75 historical SWING closes — 89% of the sample together with plain
    "CTL" — and the un-stripped lookup resolved it to "ALL" instead of
    CONTINUATION, the identical shape as the signal_type/strategy
    column-confusion this file's own header documents. "CTL (Legacy)" is
    CTL; the parenthetical is metadata about when it was labelled.
    """
    from allocation.scoring import swing_family
    assert swing_family("CTL (Legacy)") == "CONTINUATION", (
        f"got {swing_family('CTL (Legacy)')!r} — a legacy-labelled engine "
        f"is falling out of its own family again")
    assert swing_family("MOM (Legacy)") == "MOM", (
        "the strip must be generic (any trailing parenthetical), not a "
        "hardcoded alias for CTL alone")
    assert swing_family("CTL (Legacy)+SEC") == "CONTINUATION"
    assert swing_family("CTL (Legacy)+MOM") == "MOM"


def test_from_swing_reads_strategy_and_final_score_from_the_candidate():
    """
    `plan` here is what `_allocate_shadow` now actually passes: the candidate
    dict (a signal_output_daily row), carrying `strategy` and `final_score`.
    """
    from allocation.proposal import from_swing

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx():
        candidate = {"strategy": "CTL+SEC", "final_score": 78.5}
        p = from_swing(D(), candidate)
        assert p is not None
        assert p.source == "CONTINUATION", (
            f"source was {p.source!r}, expected CONTINUATION from strategy "
            f"'CTL+SEC' — from_swing is not reading the real column")
        assert p.native_rank == 78.5, (
            f"native_rank was {p.native_rank!r}, expected the candidate's own "
            f"final_score (78.5) — it silently defaulted to 0.0 before this fix")


def test_from_swing_resolves_a_non_continuation_engine():
    from allocation.proposal import from_swing

    class D:
        symbol = "ZOMATO"; action = "BUY_NOW"; entry = 250.0; stop = 240.0
        target = 280.0; qty = 10; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx():
        p = from_swing(D(), {"strategy": "MOM", "final_score": 61.0})
        assert p.source == "MOM"


def test_swing_priors_and_from_swing_agree_on_the_same_key():
    """
    The whole point: whatever key `swing_priors()` builds for a MOM plan must
    be the SAME key `_prior_for()` looks up for a MOM Proposal, or the class
    lookup is dead again regardless of either bug individually.
    """
    from allocation.proposal import from_swing
    from allocation.scoring import swing_family

    class D:
        symbol = "TATASTEEL"; action = "BUY_NOW"; entry = 150.0; stop = 145.0
        target = 165.0; qty = 20; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx():
        p = from_swing(D(), {"strategy": "MOM", "final_score": 70.0})
        assert p.source == swing_family("MOM") == "MOM"


TESTS = [
    ("swing_family maps single engines to their family",
     test_swing_family_maps_single_engines),
    ("swing_family combo of CONTINUATION engines stays CONTINUATION",
     test_swing_family_combo_of_continuation_engines_stays_continuation),
    ("swing_family mixed combo prefers the isolated family",
     test_swing_family_mixed_combo_prefers_the_isolated_family),
    ("swing_family empty or unknown falls back to ALL",
     test_swing_family_empty_or_unknown_falls_back_to_all),
    ("swing_family strips a legacy parenthetical annotation",
     test_swing_family_strips_a_legacy_parenthetical_annotation),
    ("from_swing reads strategy and final_score from the candidate",
     test_from_swing_reads_strategy_and_final_score_from_the_candidate),
    ("from_swing resolves a non-CONTINUATION engine",
     test_from_swing_resolves_a_non_continuation_engine),
    ("swing_priors and from_swing agree on the same key",
     test_swing_priors_and_from_swing_agree_on_the_same_key),
]
