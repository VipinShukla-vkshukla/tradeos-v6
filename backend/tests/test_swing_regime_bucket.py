"""
Allocator.select() computed ONE regime_bucket, shared by both books, from
whatever `regime` its caller passed — and the daemon's only real caller
(intraday/engine.py::_allocate_shadow) always passes mc.state, intraday's
15-second index reading. Swing has no 15-second regime; it has a once-a-day
one (market_regime), and regime_bucket() was written to recognise that
vocabulary too — but nothing ever fed it in for a SWING proposal.

Measured live, 21-Aug through 28-Aug-2026 (14 sessions): swing's own daily
regime read NEUTRAL every single day (always WEAK), while the bucket
actually stored on SWING's allocation_decisions rows split STRONG/WEAK
WITHIN single days, tracking intraday's tick-by-tick reading instead. A
SWING proposal's hurdle bar was being drawn from whichever bucket intraday
happened to be in at that exact moment, not from swing's own market state.

This module proves the fix: a SWING proposal buckets from `swing_regime`
when supplied, an INTRADAY proposal always keeps bucketing from `regime`
(intraday's own signal, completely untouched), and a caller that supplies
neither (every existing test, and score_hypothetical()'s own separate
path) gets exactly the old, shared-bucket behaviour.
"""

from __future__ import annotations

from tests import cfg_ctx


class NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _proposals():
    from allocation.proposal import from_intraday, from_swing
    from intraday.strategies.base import Setup

    intraday_s = Setup("RELIANCE", "ORB", "LONG", 2500.0, 2485.0, 2540.0,
                       0.75, "r", "i", meta={"family": "ORB"})
    p_intraday = from_intraday(intraday_s, 8)

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False
    p_swing = from_swing(D())

    assert p_intraday is not None and p_swing is not None
    return p_swing, p_intraday


def _seeded_allocator():
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    alloc = Allocator(sb=NoDB())
    alloc._priors = {
        "SWING/ALL":    Prior("SWING/ALL", 100, 0.30, 0.20, 0.05, -1.0, 2.0),
        "SWING/CONTINUATION": Prior("SWING/CONTINUATION", 100, 0.30, 0.20, 0.05, -1.0, 2.0),
        "INTRADAY/ORB": Prior("INTRADAY/ORB", 100, 0.35, 0.20, 0.05, -1.0, 2.0),
        "INTRADAY/ALL": Prior("INTRADAY/ALL", 200, 0.20, 0.10, 0.04, -1.0, 1.8),
    }
    alloc._hold_days = {"INTRADAY": (1.0, 50), "SWING": (5.0, 30)}
    return alloc


def test_swing_and_intraday_bucket_independently_when_swing_regime_given():
    with cfg_ctx({"alloc_hurdle_min_sample": "5"}):
        p_swing, p_intraday = _proposals()
        alloc = _seeded_allocator()

        # Intraday's live signal says STRONG (RISK_ON); swing's own daily
        # regime says WEAK (NEUTRAL) — a real, plausible divergence, since
        # swing's regime has in fact read NEUTRAL every session measured.
        verdicts = alloc.select([p_swing, p_intraday], regime="RISK_ON",
                                 swing_regime="NEUTRAL",
                                 slots_left=4, minutes_left=200)
        by_symbol = {v["proposal"].symbol: v for v in verdicts}

        assert by_symbol["TCS"]["regime_bucket"] == "WEAK", (
            "SWING proposal did not bucket from swing_regime")
        assert by_symbol["RELIANCE"]["regime_bucket"] == "STRONG", (
            "INTRADAY proposal's bucket was disturbed by swing_regime — "
            "it must keep using `regime` exactly as before")


def test_no_swing_regime_falls_back_to_shared_bucket():
    """The old call shape (no swing_regime) must reproduce the old result:
    both books bucketed off the SAME `regime` value."""
    with cfg_ctx({"alloc_hurdle_min_sample": "5"}):
        p_swing, p_intraday = _proposals()
        alloc = _seeded_allocator()

        verdicts = alloc.select([p_swing, p_intraday], regime="RISK_ON",
                                 slots_left=4, minutes_left=200)
        by_symbol = {v["proposal"].symbol: v for v in verdicts}

        assert by_symbol["TCS"]["regime_bucket"] == "STRONG"
        assert by_symbol["RELIANCE"]["regime_bucket"] == "STRONG"


def test_swing_regime_empty_string_also_falls_back():
    """An empty/falsy swing_regime (e.g. _current_regime missing from a
    not-yet-loaded policy dict) must not crash or bucket as WEAK by
    accident — it falls back to the shared bucket, same as None."""
    with cfg_ctx({"alloc_hurdle_min_sample": "5"}):
        p_swing, p_intraday = _proposals()
        alloc = _seeded_allocator()

        verdicts = alloc.select([p_swing, p_intraday], regime="RISK_ON",
                                 swing_regime="",
                                 slots_left=4, minutes_left=200)
        by_symbol = {v["proposal"].symbol: v for v in verdicts}

        assert by_symbol["TCS"]["regime_bucket"] == "STRONG"


TESTS = [
    ("swing and intraday bucket independently when swing_regime is given",
     test_swing_and_intraday_bucket_independently_when_swing_regime_given),
    ("no swing_regime -> old shared-bucket behaviour preserved",
     test_no_swing_regime_falls_back_to_shared_bucket),
    ("swing_regime='' falls back same as None, does not crash",
     test_swing_regime_empty_string_also_falls_back),
]
