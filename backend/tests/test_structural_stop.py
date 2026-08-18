"""
The stop an engine names is the stop the trade carries (18-Aug-2026).

WHAT THIS CATCHES
-----------------
Eight engines shared four lines that found a structural stop — the range low,
the swing low, VWAP, the previous day's high — and then, when that stop was
wider than the engine's `*_max_risk_pct`, moved it to a price the structure
never named. The trade kept the structure's prose ("dies below the range") and
lost the structure's geometry, usually ending up with a stop INSIDE the range
that produced the setup.

`registry._invalidation_is_reachable` (12-Aug, NATIONALUM) caught the extreme
tail of this — the cases where the stop and the named invalidation had come so
far apart the structural exit could never fire. It left the ordinary ones
alone, and the ordinary ones are where the money went.

MEASURED, over the 1,766 TAKEN-and-resolved rows in `intraday_setups`:

    stop pinned to the cap    n=798   gross mean R  -0.5348
    structural stop kept      n=968   gross mean R  +0.0154
    whole book               n=1766   gross mean R  -0.2403

    GAP, the clean experiment (same engine, same sessions, same universe,
    separated only by whether its own stop survived):
        pinned      139 @ -0.235R
        structural  144 @ +0.587R

WHY THESE CHECKS CAN FAIL
-------------------------
Demonstrated against a reconstruction of the pre-fix implementation, not
against a hypothetical: with `risk_from_structure` replaced by the four lines
it removed, five of the six checks below fail. `intraday_stop_cap_mode` keeps
that branch reachable in production too, so the revert is one config row.
"""

from __future__ import annotations

from tests import cfg_ctx
from intraday.strategies.base import risk_from_structure


# Entry 100, structural stop 97.5 — a 2.5% stop against a 1.2% cap.
ENTRY, WIDE_STOP, CAP = 100.0, 97.5, 1.20

_REFUSE = {"intraday_stop_cap_mode": "refuse", "intraday_min_risk_pct": "0.0"}


def test_an_unaffordable_structural_stop_is_refused_not_tightened():
    with cfg_ctx(_REFUSE):
        frame = risk_from_structure(ENTRY, WIDE_STOP, "LONG", max_risk_pct=CAP)
    assert frame is None, (
        f"a 2.5% structural stop against a {CAP}% cap must refuse the setup; "
        f"got a frame with stop={frame.stop if frame else None}")


def test_an_affordable_structural_stop_is_passed_through_untouched():
    with cfg_ctx(_REFUSE):
        frame = risk_from_structure(ENTRY, 99.2, "LONG", max_risk_pct=CAP)
    assert frame is not None, "a 0.8% stop under a 1.2% cap must be accepted"
    assert frame.stop == 99.2, f"stop must be untouched, got {frame.stop}"
    assert not frame.capped
    assert abs(frame.risk_pct - 0.8) < 1e-9, frame.risk_pct


def test_the_stop_is_never_moved_closer_to_entry_than_the_structure():
    """The whole defect in one assertion, swept across a range of caps."""
    with cfg_ctx(_REFUSE):
        for cap in (0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 3.0):
            frame = risk_from_structure(ENTRY, WIDE_STOP, "LONG", max_risk_pct=cap)
            if frame is None:
                continue
            assert frame.stop <= WIDE_STOP + 1e-9, (
                f"cap={cap}% produced stop {frame.stop}, tighter than the "
                f"structural {WIDE_STOP} — this is the defect")


def test_shorts_are_handled_on_their_own_side():
    """A short's stop sits ABOVE entry; `entry - stop` would invert the test."""
    with cfg_ctx(_REFUSE):
        tight = risk_from_structure(ENTRY, 100.8, "SHORT", max_risk_pct=CAP)
        wide = risk_from_structure(ENTRY, 102.5, "SHORT", max_risk_pct=CAP)
    assert tight is not None and abs(tight.risk_pct - 0.8) < 1e-9, (
        f"a short stopped 0.8% above entry must be accepted, got {tight}")
    assert wide is None, "a short stopped 2.5% above entry must be refused at a 1.2% cap"


def test_the_legacy_branch_is_still_reachable_and_still_wrong():
    """
    NOT a defence of the old behaviour — a demonstration that the checks above
    can fail. If this stops failing under 'tighten', they have stopped testing.
    """
    with cfg_ctx({"intraday_stop_cap_mode": "tighten", "intraday_min_risk_pct": "0.0"}):
        frame = risk_from_structure(ENTRY, WIDE_STOP, "LONG", max_risk_pct=CAP)
    assert frame is not None, "the legacy branch must still produce a setup"
    assert frame.capped, "the legacy branch must flag that it moved the stop"
    assert frame.stop > WIDE_STOP, (
        "under 'tighten' the stop is moved toward entry — that is the behaviour "
        "the other checks in this module exist to refuse")


def test_the_minimum_risk_floor_is_inert_until_armed():
    """
    Shipped unarmed on purpose. The 0.0-0.6% band stops out 84.3% of the time
    (n=172) but is populated entirely by the four engines with the tightest
    caps, so this data cannot separate "stop too tight" from "engine is bad".
    """
    with cfg_ctx(_REFUSE):
        assert risk_from_structure(ENTRY, 99.7, "LONG", max_risk_pct=CAP) is not None, \
            "a 0.3% stop must survive while the floor is unset"
    with cfg_ctx({"intraday_stop_cap_mode": "refuse", "intraday_min_risk_pct": "0.5"}):
        assert risk_from_structure(ENTRY, 99.7, "LONG", max_risk_pct=CAP) is None, \
            "with the floor armed at 0.5%, a 0.3% stop must be refused"


TESTS = [
    ("an unaffordable structural stop is refused, not tightened",
     test_an_unaffordable_structural_stop_is_refused_not_tightened),
    ("an affordable structural stop passes through untouched",
     test_an_affordable_structural_stop_is_passed_through_untouched),
    ("the stop is never moved closer to entry than the structure",
     test_the_stop_is_never_moved_closer_to_entry_than_the_structure),
    ("shorts are measured on their own side of entry",
     test_shorts_are_handled_on_their_own_side),
    ("the legacy branch is still reachable and still wrong",
     test_the_legacy_branch_is_still_reachable_and_still_wrong),
    ("the minimum-risk floor is inert until armed",
     test_the_minimum_risk_floor_is_inert_until_armed),
]
