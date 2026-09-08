"""
tools/compact_allocation_decisions.py — 10-Sep-2026.

Retroactive backfill for the write-collapse backlog: rows written before
alloc_write_collapse_swing_enabled was armed (08-Sep-2026) still sit at the
old one-row-per-15s-cycle density. This applies the same predicate the
live path uses (Allocator.is_material_change, imported and reused — not
reimplemented) to the ALREADY-STORED rows, folding absorbed rows into an
anchor's repeat_count and deleting them, instead of doing it at write time.

Pure-function tests only (plan(), verify_hurdle_invariance()) — no
database. The tool's own --probe-first mode is the live-data safety net,
run manually before --execute touches the real table.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tools.compact_allocation_decisions import plan, verify_hurdle_invariance


def _ts(base: str, steps: int, step_seconds: int = 15) -> str:
    """steps of step_seconds apart — 15s matches the real decision-loop cadence."""
    dt = datetime.fromisoformat(base + "+05:30") + timedelta(seconds=steps * step_seconds)
    return dt.isoformat()


def _row(id_, symbol, product, trade_date, decided_at, verdict, edge,
         regime_bucket="NEUTRAL", outcome_r=None):
    return {"id": id_, "symbol": symbol, "product": product, "trade_date": trade_date,
            "decided_at": decided_at, "verdict": verdict, "edge": edge,
            "regime_bucket": regime_bucket, "outcome_r": outcome_r}


def test_unchanged_run_collapses_to_one_anchor_with_correct_repeat_count():
    rows = [_row(i, "TESTSTK", "CNC", "2026-09-01",
                 f"2026-09-01T09:{15+i:02d}:00+05:30", "DECLINE", 0.05)
            for i in range(10)]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    assert result["updates"] == {0: {"repeat_count": 10, "decided_at": rows[-1]["decided_at"]}}
    assert result["deletes"] == list(range(1, 10))


def test_verdict_change_creates_a_second_untouched_anchor():
    rows = [
        _row(1, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30", "DECLINE", 0.05),
        _row(2, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:16:00+05:30", "DECLINE", 0.05),
        _row(3, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:17:00+05:30", "TAKE", 0.05),
    ]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    assert result["deletes"] == [2]
    assert result["updates"] == {1: {"repeat_count": 2, "decided_at": rows[1]["decided_at"]}}
    assert 3 not in result["updates"], "the TAKE row is its own anchor, nothing absorbed into it"


def test_a_take_that_stays_open_collapses_too_the_netweb_case():
    """The retroactive scan must fold repeated TAKE re-affirmations exactly
    like the live path does — the NETWEB shape (1,348 rows, 15s apart, one
    real position held ~5.6 hours). The 30-minute heartbeat still means
    several anchors survive (one per ~30-minute window, not a single one
    for the whole session — this matches the live repeat_count distribution
    actually observed, values in the 80-120 range, not 1348), but every
    TAKE row is accounted for exactly once: either it's the first TAKE
    (the real entry record, never deleted) or it's folded into some
    anchor's repeat_count via deletion. Nothing is silently dropped."""
    rows = [_row(0, "NETWEB", "CNC", "2026-08-31", "2026-08-31T09:15:00+05:30", "DECLINE", 0.02)]
    rows += [_row(i, "NETWEB", "CNC", "2026-08-31",
                  _ts("2026-08-31T09:16:00", i - 1), "TAKE", 0.05)
             for i in range(1, 1349)]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    take_ids = [r["id"] for r in rows if r["verdict"] == "TAKE"]

    # the entry (row id 1) must survive — the real trade's permanent record
    assert 1 not in result["deletes"]

    # every TAKE row accounted for exactly once: survivor (with its
    # repeat_count reflecting how many were folded into it) or deleted
    deleted_takes = set(result["deletes"]) & set(take_ids)
    surviving_takes = set(take_ids) - deleted_takes
    total_via_survivors = sum(result["updates"].get(tid, {}).get("repeat_count", 1)
                               for tid in surviving_takes)
    assert total_via_survivors == len(take_ids), (
        f"sum of repeat_count across surviving TAKE anchors ({total_via_survivors}) must "
        f"equal the true TAKE row count ({len(take_ids)})")
    # the 30-min heartbeat means this collapses to several anchors, not one —
    # but drastically fewer than 1348 physical rows either way
    assert len(surviving_takes) < 20, (
        f"expected roughly one anchor per 30-minute window across ~5.6 hours "
        f"(~11-12), got {len(surviving_takes)} — heartbeat may not be firing")


def test_outcome_r_resolved_on_an_absorbed_row_is_carried_to_the_anchor():
    """A past outcomes.resolve() run may have landed on ANY row in a group,
    not necessarily the one this plan picks as anchor. Verified live before
    this shipped: entry/stop/target/outcome_r never vary within a real
    group, so whichever row holds a resolved value, that value belongs on
    whichever row survives — losing it would mean the anchor shows
    unresolved status despite the group having already been scored."""
    rows = [
        _row(1, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30", "DECLINE", 0.05),
        _row(2, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:15+05:30", "DECLINE", 0.05,
             outcome_r=-0.42),
        _row(3, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:30+05:30", "DECLINE", 0.05),
    ]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    assert result["deletes"] == [2, 3]
    assert result["updates"][1]["outcome_r"] == -0.42
    assert result["updates"][1]["repeat_count"] == 3


def test_outcome_r_already_on_the_anchor_is_not_rewritten():
    """A single-row group (or one where the anchor itself already carries
    the resolved value) must not generate a no-op self-assignment update."""
    rows = [_row(1, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30",
                 "DECLINE", 0.05, outcome_r=-0.10)]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    assert result["updates"] == {}, "a single-row group needs no write at all"


def test_edge_move_past_threshold_creates_a_new_anchor():
    rows = [
        _row(1, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30", "DECLINE", 0.00),
        _row(2, "TESTSTK", "CNC", "2026-09-01", "2026-09-01T09:16:00+05:30", "DECLINE", 0.10),
    ]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    assert result["deletes"] == []
    assert result["updates"] == {}


def test_repeat_count_always_reconciles_to_the_true_row_count():
    """The reconciliation property this whole approach depends on: every
    absorbed row is accounted for in exactly one anchor's repeat_count."""
    rows = []
    rid = 0
    for sym in ("AAA", "BBB", "CCC"):
        for i in range(37):
            rows.append(_row(rid, sym, "CNC", "2026-09-01",
                             f"2026-09-01T{9 + i // 60:02d}:{i % 60:02d}:00+05:30",
                             "DECLINE" if i % 11 else "DEFER", 0.01 * (i % 4)))
            rid += 1
    result = plan(rows, edge_threshold=0.02, heartbeat_s=600)
    # every original row is accounted for exactly once: either it survives
    # as a physical row (an anchor, whose repeat_count then sums to the
    # true number of observations it represents), or it's in `deletes`.
    deleted = set(result["deletes"])
    all_ids = {r["id"] for r in rows}
    survivors = all_ids - deleted
    assert set(result["updates"].keys()).issubset(survivors), (
        "every anchor referenced in `updates` must itself be a survivor, never a deleted row")
    survivor_repeat_counts = {rid: result["updates"].get(rid, {}).get("repeat_count", 1)
                               for rid in survivors}
    assert sum(survivor_repeat_counts.values()) == len(rows), (
        f"sum of repeat_count across surviving rows ({sum(survivor_repeat_counts.values())}) "
        f"must equal the true original row count ({len(rows)}) — this is the exact "
        f"reconciliation property migration 129's own live measurement relied on")


def test_hurdle_invariance_check_is_zero_for_a_correct_plan():
    rows = [_row(0, "REPEATED", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30",
                 "DECLINE", 0.10, "STRONG")]
    rows += [_row(i, "REPEATED", "CNC", "2026-09-01",
                  f"2026-09-01T{9 + i // 60:02d}:{i % 60:02d}:00+05:30",
                  "DECLINE", 0.10, "STRONG") for i in range(1, 600)]
    rows += [_row(1000 + i, f"SYM{i}", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30",
                  "DECLINE", 0.01, "STRONG") for i in range(40)]
    result = plan(rows, edge_threshold=0.03, heartbeat_s=1800)
    invariance = verify_hurdle_invariance(rows, result)
    stats = invariance["STRONG"]
    assert stats["raw_n"] == 640
    assert stats["weighted_n"] == 640, "weighted reconstruction must reproduce the exact raw count"
    assert stats["p75_weighted"] == stats["p75_raw"]
    assert stats["p95_weighted"] == stats["p95_raw"]


def test_hurdle_invariance_check_catches_a_broken_plan():
    """Demonstrates the check can actually fail: hand it a plan that DROPS
    a row instead of folding it into repeat_count (the exact mistake the
    27-Aug reverted dedup made), and confirm a non-zero delta is reported."""
    rows = [_row(0, "REPEATED", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30",
                 "DECLINE", 0.10, "STRONG")]
    rows += [_row(i, "REPEATED", "CNC", "2026-09-01",
                  f"2026-09-01T{9 + i // 60:02d}:{i % 60:02d}:00+05:30",
                  "DECLINE", 0.10, "STRONG") for i in range(1, 600)]
    rows += [_row(1000 + i, f"SYM{i}", "CNC", "2026-09-01", "2026-09-01T09:15:00+05:30",
                  "DECLINE", 0.01, "STRONG") for i in range(40)]
    broken_result = {"deletes": [r["id"] for r in rows[1:600]], "updates": {}}  # dropped, not folded
    invariance = verify_hurdle_invariance(rows, broken_result)
    stats = invariance["STRONG"]
    assert stats["weighted_n"] == 41, f"expected the broken plan to shrink the population, got {stats['weighted_n']}"
    assert stats["p75_weighted"] != stats["p75_raw"], (
        "a plan that drops rows instead of folding them into repeat_count MUST show a "
        "non-zero delta here — if it doesn't, this check cannot fail and is worthless")


TESTS = [
    ("an unchanged run collapses to one anchor with the correct repeat_count",
     test_unchanged_run_collapses_to_one_anchor_with_correct_repeat_count),
    ("a verdict change creates a second, untouched anchor",
     test_verdict_change_creates_a_second_untouched_anchor),
    ("a TAKE that stays open collapses too (the NETWEB case)",
     test_a_take_that_stays_open_collapses_too_the_netweb_case),
    ("an edge move past the threshold creates a new anchor",
     test_edge_move_past_threshold_creates_a_new_anchor),
    ("outcome_r resolved on an absorbed row is carried to the anchor",
     test_outcome_r_resolved_on_an_absorbed_row_is_carried_to_the_anchor),
    ("outcome_r already on the anchor is not rewritten",
     test_outcome_r_already_on_the_anchor_is_not_rewritten),
    ("repeat_count always reconciles to the true row count",
     test_repeat_count_always_reconciles_to_the_true_row_count),
    ("hurdle invariance check is zero for a correct plan",
     test_hurdle_invariance_check_is_zero_for_a_correct_plan),
    ("hurdle invariance check catches a broken (row-dropping) plan",
     test_hurdle_invariance_check_catches_a_broken_plan),
]
