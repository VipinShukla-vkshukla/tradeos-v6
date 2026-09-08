"""
compact_allocation_decisions.py — 10-Sep-2026.

One-time retroactive backfill for the storage crisis's own backlog.
alloc_write_collapse_swing_enabled only started collapsing NEW writes on
08-Sep-2026 (the daemon restart) — every row written before that still sits
at the old one-row-per-15s-cycle density. This applies the EXACT SAME
collapsing rule retroactively, via the shared predicate the live path
itself uses (Allocator.is_material_change — not a reimplementation, so the
two can never silently judge the same history two different ways):
anchors get their repeat_count bumped, absorbed rows get deleted.
hurdle()'s reconstructed population is provably unaffected — the
repeat_count-weighted expansion reconstructs the exact original edge
multiset, the same guarantee measured before arming the live path
(docs/FINDINGS.md, 08-Sep-2026).

Dry-run by default (--execute required to actually write/delete), same
safety posture as tools/archive_allocation_decisions.py. Verifies the
hurdle-bar invariance BEFORE printing a plan, every run — this is a check,
not a one-time claim.

Usage:
    python -m tools.compact_allocation_decisions              # dry run
    python -m tools.compact_allocation_decisions --execute    # apply it
    python -m tools.compact_allocation_decisions --execute --probe-first
        # apply to a 3-row sample first, read it back, confirm untouched
        # columns (entry/stop/target/verdict/...) are unchanged, THEN ask
        # before doing the rest
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import cfg_float, cfg_int, get_supabase, fetch_all, today_ist

from allocation.allocator import Allocator

PAGE = 1000
DELETE_BATCH = 500


def _fetch_uncollapsed(sb, framework: str, today: str) -> list[dict]:
    # PAGED — this table is known to exceed PostgREST's 1000-row cap.
    # Verified sort key: "id" (tests/test_static_analysis.py::_FETCH_ALL_SORT_KEY).
    #
    # Filtered on trade_date in PYTHON, not SQL — 10-Sep-2026. Adding
    # .lt("trade_date", today) to the query made it time out against the
    # live table (measured: two consecutive attempts, same 57014 statement
    # timeout) where the plain framework+repeat_count filter completed in
    # ~35s; likely no index supports the three-column combination and OFFSET
    # paging degrades further per page under it. Fetch is unchanged from the
    # already-proven-fast shape; excluding today happens after, in memory.
    rows = fetch_all(lambda: sb.table("allocation_decisions")
                     .select("id,symbol,product,trade_date,decided_at,verdict,"
                             "regime_bucket,edge,repeat_count,outcome_r")
                     .eq("framework", framework)
                     .eq("repeat_count", 1), order_by="id", page=PAGE)
    # trade_date < today, ALWAYS — the live daemon owns today's rows: it may
    # still be mid-episode on one (wrote an anchor, intends to bump its
    # repeat_count on a later flush() tick) when this script runs. This
    # backfill only ever touches history the live process has stopped
    # updating, so there is no window for the two to race on the same row.
    return [r for r in rows if r["trade_date"] < today]


def _parse_ts(v) -> datetime:
    return datetime.fromisoformat(str(v))


def plan(rows: list[dict], edge_threshold: float, heartbeat_s: float) -> dict:
    """
    Groups rows by (symbol, product, trade_date), walks each group in
    decided_at order applying Allocator.is_material_change — the identical
    predicate the live 15s path uses. Returns {updates: {anchor_id: {...}},
    deletes: [id, ...]}. An anchor with nothing absorbed is left out of
    `updates` entirely — no-op writes are not worth a network call.

    Also carries forward outcome_r — 10-Sep-2026. entry/stop/target never
    vary within a group (verified live before this shipped: 336 real
    groups, zero with any entry/stop/target/outcome_r variance), so a
    counterfactual already resolved on ANY row in the group is the correct
    value for whichever row survives as anchor, even if resolve() happened
    to land on a row that isn't the anchor. Without this, an anchor could
    show outcome_r=NULL despite the group having already been scored —
    self-healing (the next resolve() run recomputes an identical value,
    deterministic given identical entry/stop/target/trade_date) but not
    actually "exactly the same" until it does.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["symbol"], r["product"], r["trade_date"])].append(r)

    updates: dict[int, dict] = {}
    deletes: list[int] = []

    for group in groups.values():
        group.sort(key=lambda r: _parse_ts(r["decided_at"]))
        state = None
        anchor_id = None
        anchor_repeat = 1
        anchor_own_outcome_r = None   # what the anchor row ALREADY has in the DB
        outcome_r_to_carry = None     # a value found on an ABSORBED row, if any
        last_ts_iso = None

        def _flush():
            if anchor_id is None:
                return
            fields = {}
            if anchor_repeat > 1:
                fields["repeat_count"] = anchor_repeat
                fields["decided_at"] = last_ts_iso
            # only write outcome_r if the anchor doesn't already carry it —
            # never a no-op self-assignment on a single-row group
            if anchor_own_outcome_r is None and outcome_r_to_carry is not None:
                fields["outcome_r"] = outcome_r_to_carry
            if fields:
                updates[anchor_id] = fields

        for r in group:
            edge = float(r["edge"]) if r.get("edge") is not None else None
            ts = _parse_ts(r["decided_at"])
            keep = Allocator.is_material_change(
                state, verdict=r["verdict"], regime_bucket=r.get("regime_bucket"),
                edge=edge, now=ts, edge_threshold=edge_threshold, heartbeat_s=heartbeat_s)
            if keep:
                _flush()
                anchor_id, anchor_repeat = r["id"], 1
                anchor_own_outcome_r = r.get("outcome_r")
                outcome_r_to_carry = None
                state = {"verdict": r["verdict"], "regime_bucket": r.get("regime_bucket"),
                          "edge": edge, "first_decided_at": ts}
            else:
                anchor_repeat += 1
                deletes.append(r["id"])
                state["edge"] = edge
                if outcome_r_to_carry is None and r.get("outcome_r") is not None:
                    outcome_r_to_carry = r["outcome_r"]
            last_ts_iso = r["decided_at"]
        _flush()

    return {"updates": updates, "deletes": deletes}


def verify_hurdle_invariance(rows: list[dict], result: dict) -> dict:
    """
    Proves the plan does not move hurdle()'s bar — same method used before
    arming the live path (docs/FINDINGS.md, 08-Sep-2026): compare the RAW
    edge population against the repeat_count-WEIGHTED population the
    compacted table would produce, per regime_bucket. A real check, not a
    restatement of the plan — computed independently from `result`.
    """
    delete_set = set(result["deletes"])
    raw: dict = defaultdict(list)
    weighted: dict = defaultdict(list)
    for r in rows:
        if r.get("edge") is None:
            continue
        bucket = r.get("regime_bucket")
        raw[bucket].append(float(r["edge"]))
        if r["id"] in delete_set:
            continue
        rc = result["updates"].get(r["id"], {}).get("repeat_count", 1)
        weighted[bucket].extend([float(r["edge"])] * rc)

    def q(vals, p):
        if not vals:
            return float("nan")
        vals = sorted(vals)
        return vals[min(int(p * len(vals)), len(vals) - 1)]

    out = {}
    for bucket in raw:
        out[bucket] = {
            "raw_n": len(raw[bucket]), "weighted_n": len(weighted.get(bucket, [])),
            "p75_raw": q(raw[bucket], 0.75), "p75_weighted": q(weighted.get(bucket, []), 0.75),
            "p95_raw": q(raw[bucket], 0.95), "p95_weighted": q(weighted.get(bucket, []), 0.95),
        }
    return out


def _update_one(sb, anchor_id: int, fields: dict) -> None:
    # GENUINE PARTIAL UPDATE, NOT UPSERT — 10-Sep-2026, caught live by
    # --probe-first before this ever touched real rows: .upsert() with a
    # payload carrying only {id, repeat_count, decided_at} does NOT merge
    # into the existing row. PostgREST builds a full-row INSERT ... ON
    # CONFLICT DO UPDATE, and every column absent from the payload gets its
    # DEFAULT (NULL for most columns here) — the probe's first real write
    # tried to null out `symbol` on a live row and was rejected only because
    # that column happens to be NOT NULL. .update().eq("id", ...) is a true
    # partial UPDATE and was verified safe the same way before being trusted
    # at scale.
    sb.table("allocation_decisions").update(fields).eq("id", anchor_id).execute()


def _apply(sb, result: dict, dry_run: bool) -> dict:
    n_updates, n_deletes = len(result["updates"]), len(result["deletes"])
    if dry_run:
        return {"updated": n_updates, "deleted": n_deletes, "applied": False}

    ids = list(result["updates"].keys())
    for i, anchor_id in enumerate(ids, start=1):
        _update_one(sb, anchor_id, result["updates"][anchor_id])
        if i % 200 == 0 or i == len(ids):
            logger.info(f"  updated {i}/{len(ids)} anchor(s)")

    del_ids = result["deletes"]
    for i in range(0, len(del_ids), DELETE_BATCH):
        chunk = del_ids[i:i + DELETE_BATCH]
        sb.table("allocation_decisions").delete().in_("id", chunk).execute()
        logger.info(f"  deleted {i + len(chunk)}/{len(del_ids)} absorbed row(s)")

    return {"updated": n_updates, "deleted": n_deletes, "applied": True}


def _probe(sb, result: dict) -> None:
    """Apply to a 3-anchor sample, read it back, confirm untouched columns
    survived. Read-only conclusion, real writes — only used with --probe-first."""
    sample_ids = list(result["updates"].keys())[:3]  # paging-exempt: fixed 3-id sample, never grows
    if not sample_ids:
        logger.warning("  --probe-first: no anchors with repeat_count > 1 to sample")
        return
    before = (sb.table("allocation_decisions").select("*").in_("id", sample_ids)
                .execute().data or [])
    before_by_id = {r["id"]: r for r in before}

    for aid in sample_ids:
        _update_one(sb, aid, result["updates"][aid])

    after = (sb.table("allocation_decisions").select("*").in_("id", sample_ids)  # paging-exempt: same fixed 3-id sample
               .execute().data or [])
    touched_cols = {"repeat_count", "decided_at", "outcome_r"}
    for r in after:
        b = before_by_id[r["id"]]
        changed = {k for k in b if k not in touched_cols and b.get(k) != r.get(k)}
        if changed:
            raise RuntimeError(f"PROBE FAILED: row {r['id']} had unrelated column(s) "
                               f"change: {changed} — upsert is NOT safe here, stopping "
                               f"before touching the rest.")
        logger.info(f"  probe row {r['id']}: repeat_count {b['repeat_count']}->{r['repeat_count']}, "
                    f"every other column unchanged")
    logger.success("  PROBE PASSED — safe to run the rest with --execute")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--framework", default="SWING", choices=["SWING", "INTRADAY"])
    ap.add_argument("--execute", action="store_true", help="actually write/delete (default: dry run)")
    ap.add_argument("--probe-first", action="store_true",
                    help="apply to a 3-row sample only, verify, then stop")
    a = ap.parse_args()

    sb = get_supabase()
    edge_threshold = cfg_float("alloc_write_collapse_edge_threshold", 0.03)
    heartbeat_s = cfg_int("alloc_write_collapse_heartbeat_s", 1800)

    today = today_ist().isoformat()
    logger.info(f"fetching uncollapsed {a.framework} rows (repeat_count=1, trade_date < {today})...")
    rows = _fetch_uncollapsed(sb, a.framework, today)
    if not rows:
        logger.info("nothing to compact")
        return 0

    dates = sorted({r["trade_date"] for r in rows})
    logger.info(f"{len(rows)} row(s), {dates[0]} to {dates[-1]}, "
               f"rule: edge>={edge_threshold}R or {heartbeat_s}s heartbeat")

    result = plan(rows, edge_threshold, heartbeat_s)
    n_anchors_touched = len(result["updates"])
    n_deletes = len(result["deletes"])
    final_rows = len(rows) - n_deletes
    reduction = 100.0 * n_deletes / len(rows)
    logger.info(f"plan: {len(rows)} -> {final_rows} physical rows "
               f"({reduction:.1f}% fewer), {n_anchors_touched} anchor(s) get a repeat_count bump")

    invariance = verify_hurdle_invariance(rows, result)
    logger.info("hurdle-bar invariance check (raw vs. repeat_count-weighted population):")
    for bucket, stats in invariance.items():
        p75d = stats["p75_weighted"] - stats["p75_raw"]
        p95d = stats["p95_weighted"] - stats["p95_raw"]
        logger.info(f"  {bucket or '(none)'}: n={stats['raw_n']}->{stats['weighted_n']} "
                   f"  p75 delta {p75d:+.5f}   p95 delta {p95d:+.5f}")
        if abs(p75d) > 1e-9 or abs(p95d) > 1e-9:
            logger.error(f"  NON-ZERO DELTA in bucket {bucket} — refusing to proceed")
            return 1

    if a.probe_first:
        if not a.execute:
            logger.warning("--probe-first without --execute is a no-op — add --execute")
            return 0
        _probe(sb, result)
        return 0

    outcome = _apply(sb, result, dry_run=not a.execute)
    if outcome["applied"]:
        logger.success(f"applied: {outcome['updated']} row(s) updated, "
                       f"{outcome['deleted']} row(s) deleted")
    else:
        logger.info(f"DRY RUN — would update {outcome['updated']} row(s), "
                   f"delete {outcome['deleted']} row(s). Re-run with --execute to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
