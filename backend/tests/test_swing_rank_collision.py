"""
`_legacy_rank_gate_blocks` — 07-Aug-2026, the day swing_assignment's
reservation mechanism went live and the swing book still finished the
session at 0/3 entries used despite the allocator logging "3 to take" on
nearly every cycle.

Two independent rankings were both gating the same swing entry:
`entry_ranking.rank()` (04-Aug, per-stock final_score/AI-tier/timing/sector,
computed once at 09:15) and the allocator's `swing_assignment` (07-Aug,
engine-family + regime-bucket historical edge, cost-netted, reservation-
aware). Nothing tied their winners together. Confirmed against real
`allocation_decisions` data pulled the same day: the allocator's edge-ranked
TAKE winners (HEG, ACE, CHALET) were not in entry_ranking's top-6 that day,
and the one candidate that WAS rank-eligible (PIDILITIND) never had a high
enough edge to win an allocator slot either.

This exercises the fix directly: once the allocator is the live veto, its
own ranking already answered "is this the best use of today's slots" — the
older, disagreeing gate must never engage. It stays exactly as it was
(04-Aug behaviour, unchanged) whenever the allocator is off or shadow-only.
"""

from __future__ import annotations


def test_never_blocks_when_allocator_is_the_live_veto():
    from intraday.engine import _legacy_rank_gate_blocks
    # Worst possible rank position (last of the field) — would have blocked
    # under the old, unconditional gate.
    blocked = _legacy_rank_gate_blocks(
        here_total=1.0, field_totals=[100.0, 90.0, 80.0, 70.0, 60.0, 50.0],
        keep=6, alloc_live_swing=True)
    assert not blocked, (
        "the legacy rank gate must never engage once the allocator is the "
        "live veto — this is the exact collision that emptied the swing "
        "book on 07-Aug-2026 despite the allocator approving 3 candidates "
        "almost every cycle all day")


def test_still_blocks_below_the_bar_when_allocator_is_not_live():
    from intraday.engine import _legacy_rank_gate_blocks
    blocked = _legacy_rank_gate_blocks(
        here_total=45.0, field_totals=[100.0, 90.0, 80.0, 70.0, 60.0, 50.0],
        keep=6, alloc_live_swing=False)
    assert blocked, (
        "with the allocator off, this is swing's ONLY ranking and must "
        "still veto a plan outside today's top-N — the 04-Aug behaviour, "
        "unchanged")


def test_does_not_block_within_the_top_n_when_allocator_is_not_live():
    from intraday.engine import _legacy_rank_gate_blocks
    blocked = _legacy_rank_gate_blocks(
        here_total=75.0, field_totals=[100.0, 90.0, 80.0, 70.0, 60.0, 50.0],
        keep=6, alloc_live_swing=False)
    assert not blocked, (
        "a plan within today's top-N must still be allowed through when "
        "the allocator is not the veto")


def test_empty_field_never_blocks():
    from intraday.engine import _legacy_rank_gate_blocks
    blocked = _legacy_rank_gate_blocks(
        here_total=1.0, field_totals=[], keep=0, alloc_live_swing=False)
    assert not blocked, "no field to rank against must not manufacture a veto"


TESTS = [
    ("never blocks when the allocator is the live veto",
     test_never_blocks_when_allocator_is_the_live_veto),
    ("still blocks below the bar when the allocator is not live",
     test_still_blocks_below_the_bar_when_allocator_is_not_live),
    ("does not block within the top-N when the allocator is not live",
     test_does_not_block_within_the_top_n_when_allocator_is_not_live),
    ("empty field never blocks",
     test_empty_field_never_blocks),
]
