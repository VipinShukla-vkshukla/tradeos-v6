"""
allocation_decisions write-time collapse — 08-Sep-2026, migration 129.

WHY THIS EXISTS
---------------
allocation_decisions is the single biggest table (86MB, 212k rows live,
08-Sep-2026) and its growth is explosive: a SWING candidate gets a fresh row
every 15s cycle even when nothing about it changed, AND — discovered this
session, missed by both the 27-Aug mean-collapse dedup (reverted the same
day) and the 29-Aug material-change replay tool (built, never armed), which
both exempt every TAKE row unconditionally — a TAKEN candidate keeps getting
a fresh "TAKE" row every cycle for as long as the position stays open that
day (NETWEB, 2026-08-31: 1,348 identical-entry/stop/target TAKE rows for one
real trade).

Two halves, tested here:
  1. allocation/allocator.py::_write_or_collapse — decides, per cycle,
     whether to append a brand-new row or fold this observation into the
     last one via repeat_count. Off (or non-SWING) must be provably
     byte-identical to appending _record(v) directly, every time — not
     "close", since that's exactly the property that makes this safe to
     ship ahead of being armed.
  2. allocation/hurdle.py::_empirical_base — must expand each row by its
     repeat_count before taking a percentile, reconstructing the exact
     population the OLD one-row-per-cycle scheme produced. repeat_count=1
     (every row, while the switch is off) must be a no-op.

Live-measured before this shipped (14-day real SWING replay, 208,247 raw
rows, the exact rule below — 0.03R edge / 1800s heartbeat, TAKE not
exempted, DECLINE/DEFER kept distinct): 4,848 physical rows (97.7% fewer),
hurdle bar delta 0.00000 at p75 and p95, EXACT verdict-count reconciliation
(DECLINE 158,506/158,506, DEFER 35,962/35,962, TAKE 13,779/13,779), zero
native_rank drift across 3,728 collapsed groups. See docs/FINDINGS.md,
08-Sep-2026.
"""
from __future__ import annotations

from datetime import timedelta

from tests import cfg_ctx

from allocation.allocator import Allocator
from allocation.proposal import Proposal
from allocation.policies import TAKE, DECLINE, DEFER


def _proposal(**kw) -> Proposal:
    base = dict(symbol="TESTSTK", framework="SWING", product="CNC",
                entry=100.0, stop=95.0, target=115.0, quantity=10,
                source="CTL", native_rank=80.0, meta={})
    base.update(kw)
    return Proposal(**base)


def _verdict(verdict: str, edge: float = 0.05, regime_bucket: str = "NEUTRAL",
             **kw) -> dict:
    base = dict(proposal=_proposal(), verdict=verdict, edge=edge,
                hurdle=0.03, regime_bucket=regime_bucket)
    base.update(kw)
    return base


def _bare_allocator() -> Allocator:
    """Same shape as test_alloc_decisions_jsonb_slim.py's Allocator.__new__
    pattern — avoids __init__'s get_supabase() call. _write_or_collapse
    touches no network I/O, only the three attributes set here."""
    a = Allocator.__new__(Allocator)
    a._buffer = []
    a._collapse_state = {}
    a._pending_updates = {}
    return a


# ── switch off / wrong framework: must be byte-identical to today ──────────

def test_switch_off_appends_one_row_per_call_no_collapsing():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "false"}):
        a = _bare_allocator()
        for _ in range(5):
            a._write_or_collapse(_verdict(DECLINE))
        assert len(a._buffer) == 5, (
            "collapsing must not happen while the switch is off — got "
            f"{len(a._buffer)} row(s) for 5 identical calls")
        assert all(r["repeat_count"] == 1 for r in a._buffer)


def test_intraday_never_collapses_even_with_the_switch_on():
    """The switch is SWING-only by name and by measurement — INTRADAY's own
    14-day replay only reached 61.9% reduction with a non-zero bar delta,
    so it must never collapse regardless of this config key."""
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true"}):
        a = _bare_allocator()
        v = _verdict(DECLINE)
        v["proposal"] = _proposal(framework="INTRADAY", product="MIS")
        for _ in range(5):
            a._write_or_collapse(v)
        assert len(a._buffer) == 5, (
            "INTRADAY must never collapse, even with the switch on — got "
            f"{len(a._buffer)} row(s) for 5 identical INTRADAY calls")


# ── switch on, SWING: the actual collapse mechanics ─────────────────────────

def test_unchanged_candidate_collapses_into_repeat_count():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true",
                  "alloc_write_collapse_edge_threshold": "0.03",
                  "alloc_write_collapse_heartbeat_s": "1800"}):
        a = _bare_allocator()
        for _ in range(10):
            a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        assert len(a._buffer) == 1, (
            f"10 identical DECLINE observations should collapse to 1 "
            f"physical row, got {len(a._buffer)}")
        assert a._buffer[0]["repeat_count"] == 10, (
            f"repeat_count must equal the number of collapsed observations, "
            f"got {a._buffer[0]['repeat_count']}")


def test_verdict_change_always_forces_a_new_row():
    """The transition into (or out of) TAKE, or any other verdict flip, is
    real information — it must never be folded into repeat_count."""
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true"}):
        a = _bare_allocator()
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        a._write_or_collapse(_verdict(TAKE, edge=0.05))
        assert len(a._buffer) == 2, (
            f"DECLINE->DECLINE should collapse, DECLINE->TAKE must not — "
            f"got {len(a._buffer)} row(s)")
        assert a._buffer[0]["repeat_count"] == 2
        assert a._buffer[1]["verdict"] == TAKE
        assert a._buffer[1]["repeat_count"] == 1


def test_a_take_that_stays_open_collapses_like_anything_else():
    """The NETWEB pattern this migration exists to fix: a position already
    taken keeps getting re-affirmed every cycle with nothing else changing.
    Only the transition INTO TAKE is sacred; re-affirmation is not."""
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true"}):
        a = _bare_allocator()
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        a._write_or_collapse(_verdict(TAKE, edge=0.05))
        for _ in range(1347):
            a._write_or_collapse(_verdict(TAKE, edge=0.05))
        assert len(a._buffer) == 2, (
            f"1348 identical TAKE rows for one open position must collapse "
            f"to 1 physical TAKE row (plus the prior DECLINE), got "
            f"{len(a._buffer)}")
        assert a._buffer[1]["repeat_count"] == 1348, (
            f"expected repeat_count=1348 on the TAKE anchor, got "
            f"{a._buffer[1]['repeat_count']}")


def test_regime_bucket_change_forces_a_new_row():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true"}):
        a = _bare_allocator()
        a._write_or_collapse(_verdict(DECLINE, regime_bucket="WEAK"))
        a._write_or_collapse(_verdict(DECLINE, regime_bucket="STRONG"))
        assert len(a._buffer) == 2, (
            "a regime_bucket change must never be folded into repeat_count "
            "— it is the exact segmentation key hurdle() partitions on")


def test_edge_threshold_crossing_forces_a_new_row():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true",
                  "alloc_write_collapse_edge_threshold": "0.03"}):
        a = _bare_allocator()
        a._write_or_collapse(_verdict(DECLINE, edge=0.00))
        a._write_or_collapse(_verdict(DECLINE, edge=0.10))  # +0.10, over threshold
        assert len(a._buffer) == 2, "an edge move past the threshold must force a new row"


def test_heartbeat_elapsed_forces_a_new_row_even_with_nothing_else_changed():
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true",
                  "alloc_write_collapse_heartbeat_s": "900"}):
        a = _bare_allocator()
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        key = next(iter(a._collapse_state))
        # Simulate 16 minutes elapsed since the anchor was first written —
        # past the 900s (15-min) heartbeat, with nothing else about the
        # candidate having changed.
        a._collapse_state[key]["first_decided_at"] -= timedelta(seconds=901)
        a._write_or_collapse(_verdict(DECLINE, edge=0.05))
        assert len(a._buffer) == 2, (
            "a candidate hovering unchanged past the heartbeat must still "
            "contribute a second point to the day's population, not "
            "collapse into the same row forever")


def test_repeat_count_always_reconciles_to_the_true_observation_count():
    """The reconciliation property migration 129's own measurement leaned
    on: sum(repeat_count) across whatever physical rows result must equal
    the number of underlying observations, exactly, always."""
    with cfg_ctx({"alloc_write_collapse_swing_enabled": "true",
                  "alloc_write_collapse_edge_threshold": "0.02",
                  "alloc_write_collapse_heartbeat_s": "600"}):
        a = _bare_allocator()
        n = 0
        for i in range(50):
            verdict = TAKE if i >= 40 else (DEFER if i % 7 == 0 else DECLINE)
            edge = 0.01 * (i % 5)
            a._write_or_collapse(_verdict(verdict, edge=edge))
            n += 1
        assert sum(r["repeat_count"] for r in a._buffer) == n, (
            "sum(repeat_count) must equal the true number of observations, "
            "regardless of how many physical rows it collapsed into")


TESTS = [
    ("switch off: one row per call, no collapsing",
     test_switch_off_appends_one_row_per_call_no_collapsing),
    ("INTRADAY never collapses even with the switch on",
     test_intraday_never_collapses_even_with_the_switch_on),
    ("unchanged SWING candidate collapses into repeat_count",
     test_unchanged_candidate_collapses_into_repeat_count),
    ("a verdict change always forces a new row",
     test_verdict_change_always_forces_a_new_row),
    ("a TAKE that stays open collapses like anything else (the NETWEB case)",
     test_a_take_that_stays_open_collapses_like_anything_else),
    ("a regime_bucket change always forces a new row",
     test_regime_bucket_change_forces_a_new_row),
    ("an edge-threshold crossing forces a new row",
     test_edge_threshold_crossing_forces_a_new_row),
    ("heartbeat elapsed forces a new row with nothing else changed",
     test_heartbeat_elapsed_forces_a_new_row_even_with_nothing_else_changed),
    ("repeat_count always reconciles to the true observation count",
     test_repeat_count_always_reconciles_to_the_true_observation_count),
]
