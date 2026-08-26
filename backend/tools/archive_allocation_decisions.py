"""
archive_allocation_decisions.py — 27-Aug-2026.

Same archive-then-delete contract as migration 016's stock_data_daily
rolloff, adapted for a target that isn't another Postgres table: rows are
not re-derivable (unlike raw_prices/chartink_raw_data, which are re-fetchable
from source), so this exports before it deletes, and only deletes what it
already verified round-trips cleanly.

Runs manually (`python -m tools.archive_allocation_decisions`) or from the
evening pipeline behind `storage_alloc_decisions_rolloff_enabled` (ships
false). Local Parquet target for now — swap for a second Supabase project or
equivalent later without touching the caller, since the interface is just
"rows in, verified file out, rows deleted."
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd

from config import cfg_bool, cfg_int, fetch_all, get_supabase, today_ist

ARCHIVE_DIR = Path(__file__).resolve().parents[1] / "db" / "archive"


def _fetch(sb, cutoff: str) -> list[dict]:
    return fetch_all(lambda: sb.table("allocation_decisions")
                      .select("*").lt("trade_date", cutoff), order_by="id")


def _export_and_verify(rows: list[dict], cutoff: str) -> Path:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ARCHIVE_DIR / f"allocation_decisions_before_{cutoff}.parquet"
    pd.DataFrame(rows).to_parquet(out, index=False, compression="gzip")

    back = pd.read_parquet(out)
    if len(back) != len(rows):
        raise RuntimeError(f"archive verify failed: exported {len(rows)}, "
                            f"file has {len(back)}")
    if set(back["id"]) != {r["id"] for r in rows}:
        raise RuntimeError("archive verify failed: id set mismatch")
    return out


def archive_and_prune(keep_days: int | None = None, sb=None,
                       dry_run: bool = False) -> dict:
    """
    Export + verify + delete rows older than keep_days. Never deletes
    without a verified file on disk first — a failed export or a failed
    round-trip check leaves Supabase untouched and can be retried next run.
    """
    sb = sb or get_supabase()
    keep_days = keep_days if keep_days is not None else cfg_int(
        "storage_alloc_decisions_keep_days", 60)
    cutoff = (today_ist() - timedelta(days=keep_days)).isoformat()

    rows = _fetch(sb, cutoff)
    if not rows:
        return {"archived": 0, "cutoff": cutoff, "path": None}

    if dry_run:
        return {"archived": len(rows), "cutoff": cutoff, "path": None,
                "dry_run": True}

    path = _export_and_verify(rows, cutoff)
    sb.table("allocation_decisions").delete().lt("trade_date", cutoff).execute()

    # VACUUM cannot run inside a function or transaction block in Postgres,
    # so it isn't done here — the DELETE frees space for reuse by future
    # inserts regardless, and a periodic VACUUM FULL (run manually via the
    # SQL editor, or scheduled separately) is what shrinks the on-disk size
    # Supabase's storage dashboard actually reports.
    return {"archived": len(rows), "cutoff": cutoff, "path": str(path),
            "bytes": path.stat().st_size}


if __name__ == "__main__":
    import argparse
    from loguru import logger

    ap = argparse.ArgumentParser(description="Archive-then-delete aged-out allocation_decisions rows")
    ap.add_argument("--keep-days", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not cfg_bool("storage_alloc_decisions_rolloff_enabled", False):
        logger.warning("storage_alloc_decisions_rolloff_enabled is off — pass --dry-run to preview, "
                        "or arm the switch to actually run")
        raise SystemExit(1)

    result = archive_and_prune(keep_days=args.keep_days, dry_run=args.dry_run)
    logger.info(f"archived {result['archived']} row(s) older than {result['cutoff']}"
                + (f" -> {result['path']} ({result.get('bytes', 0) / 1024:.1f} KB)"
                   if result.get("path") else ""))
