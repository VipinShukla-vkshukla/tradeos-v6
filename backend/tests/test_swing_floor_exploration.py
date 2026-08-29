"""
`policies.swing_assignment()` never set `floor_only_rank` — the field
`engine.allocator_permits()` reads to let a floor-declined proposal
through as a PAPER "EXPLORATION" trade (test_floor_exploration.py proves
that consumer-side mechanism works for whichever framework supplies a
ranked verdict). Traced two ways: the main reservation loop never
computed it at all, and swing's own `if not field:` fallback called
`intraday_stopping()` — the function that DOES compute it — without
passing `bar_before_floor`, so even the fallback left it unset.

Consequence: a floor-declined SWING proposal on the paper book could
never use the rescue valve migration 058 built for exactly this shape
(a prior stuck negative forever because the book that would generate new
TAKEN evidence is never allowed to try). Quantified 29-Aug-2026: not
biting today (0 SWING declines sat exactly at the absolute floor in the
last 21 days), but the mechanism must exist before it is needed, not
discovered missing after.

This module tests the PRODUCER side (does swing_assignment set the
field correctly) — test_floor_exploration.py already covers the
CONSUMER side (does allocator_permits use it correctly) generically for
either framework.
"""

from __future__ import annotations

from allocation import policies as P


def _p(symbol, edge):
    return {"proposal": None, "symbol": symbol, "edge": edge}


# A single TRIGGERED field entry: enough to make `field` truthy so the
# `if not field:` fallback does NOT engage (empty list/None both would),
# while contributing nothing to `reserved` (the reservation-building loop
# skips triggered entries) — isolates the main loop's own new logic.
_NONEMPTY_FIELD = [{"symbol": "OTHER", "triggered": True}]


def test_floor_declined_proposal_gets_ranked_with_a_field():
    """Main reservation loop: edge clears bar_before_floor but not the
    (higher, floored) bar -> DECLINE carrying floor_only_rank=0."""
    out = P.swing_assignment(
        [_p("TCS", 0.015)], bar=0.02, slots_left=5, field=_NONEMPTY_FIELD,
        bar_before_floor=0.01)
    v = out[0]
    assert v["verdict"] == "DECLINE"
    assert v["floor_only_rank"] == 0, v
    assert "rank 1 of 5" in v["reason"], v["reason"]


def test_below_bar_before_floor_gets_no_rank():
    """An edge that would not even clear the RELATIVE bar is an ordinary
    decline, not an exploration candidate — no rank at all."""
    out = P.swing_assignment(
        [_p("TCS", 0.005)], bar=0.02, slots_left=5, field=_NONEMPTY_FIELD,
        bar_before_floor=0.01)
    v = out[0]
    assert v["verdict"] == "DECLINE"
    assert "floor_only_rank" not in v, v


def test_rank_is_bounded_by_slots_left():
    """Three proposals all clear bar_before_floor; only 2 slots -> the
    third gets no rank, matching intraday_stopping's own bound."""
    proposals = [_p("A", 0.018), _p("B", 0.016), _p("C", 0.014)]
    out = P.swing_assignment(proposals, bar=0.02, slots_left=2,
                             field=_NONEMPTY_FIELD, bar_before_floor=0.01)
    by_symbol = {v["symbol"]: v for v in out}
    assert by_symbol["A"]["floor_only_rank"] == 0
    assert by_symbol["B"]["floor_only_rank"] == 1
    assert "floor_only_rank" not in by_symbol["C"], by_symbol["C"]


def test_no_field_fallback_still_passes_bar_before_floor_through():
    """field=None/empty degrades to intraday_stopping — the fallback must
    forward bar_before_floor, not silently drop it (the original gap)."""
    out = P.swing_assignment(
        [_p("TCS", 0.015)], bar=0.02, slots_left=5, field=None,
        bar_before_floor=0.01)
    v = out[0]
    assert v["verdict"] == "DECLINE"
    assert v.get("floor_only_rank") == 0, (
        "the field=None fallback dropped bar_before_floor — this is the "
        "exact gap this fix closes")


def test_no_bar_before_floor_supplied_sets_no_rank():
    """A caller that does not supply bar_before_floor (e.g. an older test
    fixture) must reproduce the old behaviour exactly — no rank, no
    crash."""
    out = P.swing_assignment([_p("TCS", 0.015)], bar=0.02, slots_left=5,
                             field=[])
    assert "floor_only_rank" not in out[0]


TESTS = [
    ("floor-declined SWING proposal gets ranked (main loop, with field)",
     test_floor_declined_proposal_gets_ranked_with_a_field),
    ("below bar_before_floor -> no rank at all",
     test_below_bar_before_floor_gets_no_rank),
    ("rank is bounded by slots_left",
     test_rank_is_bounded_by_slots_left),
    ("field=None fallback still forwards bar_before_floor",
     test_no_field_fallback_still_passes_bar_before_floor_through),
    ("no bar_before_floor supplied -> old behaviour, no rank",
     test_no_bar_before_floor_supplied_sets_no_rank),
]
