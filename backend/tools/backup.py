"""
Take the system of record off the platform, weekly.

    python -m tools.backup                    write a dump, then verify it
    python -m tools.backup --out DIR          somewhere other than data/backups
    python -m tools.backup --verify FILE      re-check an existing dump
    python -m tools.backup --list             what a dump would cover, and why

WHY THIS EXISTS
---------------
The free plan provides no backups at all. Not a shorter retention window — none.
Every decision this system has ever made, every resolved outcome that its
priors are built from, and the configuration that governs both, exist in exactly
one place. Losing the project means losing the measurement record, and the
measurement record is the thing the architecture calls this system's defining
asset. Signals can be regenerated; a year of resolved outcomes cannot.

WHAT IS COVERED, AND WHY IT IS A DENYLIST
-----------------------------------------
Everything except bulk price history, which is 84% of the database and is
re-ingestible from the exchange on demand. Dumping it weekly would spend
~700 MB/month of a ~5 GB egress budget re-downloading data the NSE will hand
back for free.

The list of exclusions is explicit and the default is INCLUDE, deliberately.
An allowlist would mean a table added next year is silently absent from every
backup until someone notices during a restore, and "the check that silently
covered nothing" is the failure this repository has hit four times. A denylist
fails the other way: a new bulk table makes backups fat and visible, which is a
complaint rather than a loss.

WHY NOT pg_dump
---------------
There is no direct Postgres connection string anywhere in this project's
configuration — only the PostgREST URL and the service key. Introducing a
database password to run pg_dump would add a credential to the blast radius of
migration 007 for a file format nothing here needs. Paged JSON through the
client already in use restores through the same client, which is also the only
restore path that has been tested.

RESTORE
-------
    python -m tools.backup --verify FILE      proves the file parses and is complete

Restoring is deliberately NOT automated. Writing 40 tables back into a live
book is not something that should be one flag away from a tired operator at
midnight; the dump is plain JSON and a targeted restore is a short script
against the table that was actually lost.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


# Bulk, derived, re-ingestible. Excluded by name so an omission is a decision
# with a reason attached rather than a table nobody thought about.
BULK_TABLES = {
    "stock_data_daily":   "86 indicator columns recomputable from price; re-ingestible from bhavcopy",
    "stock_data_archive":  "the roll-off's own output — a slim copy of the above",
    "price_history_yf":   "vendor price history, re-fetchable",
    "raw_prices":         "raw ingestion staging, re-fetchable",
    "chartink_raw_data":  "screener output, regenerated nightly",
}

# PostgREST caps a response at 1000 rows and says nothing about it. Anything
# that pages has to page explicitly or it silently backs up the first page.
PAGE = 1000


def _tables(sb) -> list[str]:
    """Every public table, from the storage view that already enumerates them."""
    # sort-exempt: v_storage_usage is one row per public table — 56 rows,
    # measured 15-Aug-2026 — and the result is sorted by name on return
    # anyway. A catalogue view cannot reach the cap without the schema having
    # 1000 tables.
    rows, off = [], 0
    while True:
        page = (sb.table("v_storage_usage").select("table_name")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE
    return sorted(r["table_name"] for r in rows)


def _redact(name: str, rows: list[dict]) -> tuple[list[dict], int]:
    """
    Strip credentials before they reach a file.

    This runs with the service key, which BYPASSES the RLS that migration 007
    added — so a naive dump of system_config writes the Kite access token, the
    Gmail app password and the Discord webhook to disk in plaintext, and then a
    weekly job uploads that file somewhere. That is the exact disclosure 007
    was written to close, reopened by a backup.

    is_secret is the same marker RLS uses, so this cannot drift from it: any
    row a future credential is added to is redacted the moment it is flagged,
    which is also the moment it is hidden from the dashboard.

    A restore therefore returns configuration and not credentials. That is the
    correct behaviour and not a shortcoming — credentials are rotated after an
    incident, never restored from a file written before it.
    """
    if name != "system_config":
        return rows, 0
    n = 0
    for r in rows:
        if r.get("is_secret"):
            r["value"] = "__REDACTED__"
            n += 1
    return rows, n


def _dump_table(sb, name: str) -> tuple[list[dict], int]:
    # SORTED PAGING, AND THIS ONE IS THE BACKUP. Unsorted LIMIT/OFFSET can
    # return the right row COUNT built from duplicated and skipped rows —
    # measured on this book at 8324 rows / 5000 distinct. A backup written that
    # way is corrupt in the one way nobody checks: the file is the right size.
    #
    # The sort key cannot be hardcoded because this runs over EVERY table and
    # several have no `id` — `signal_output_daily`, `open_positions` (keyed on
    # (symbol, product) since migration 028) and the views. So probe one row
    # and prefer `id`, falling back to the first column, which PostgREST
    # returns in declaration order.
    from config import fetch_all
    probe = sb.table(name).select("*").limit(1).execute().data or []
    cols = list(probe[0].keys()) if probe else []
    if not cols:
        return _redact(name, [])
    key = "id" if "id" in cols else cols[0]
    out = fetch_all(lambda: sb.table(name).select("*"), page=PAGE, order_by=key)
    return _redact(name, out)


def prune(out_dir: Path, keep: int) -> list[str]:
    """
    Keep the last `keep` dumps and no more.

    A weekly job writing 6 MB forever fills the VM disk in a few years, which
    would mean solving one storage ceiling by building another one somewhere
    less watched. Oldest first, and never the newest even if keep is set to 0.
    """
    dumps = sorted(out_dir.glob("tradeos_record_*.json.gz"))
    dropped = []
    for p in dumps[:max(len(dumps) - max(keep, 1), 0)]:
        p.unlink()
        dropped.append(p.name)
    return dropped


def backup(sb, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path  = out_dir / f"tradeos_record_{stamp}.json.gz"

    covered = [t for t in _tables(sb) if t not in BULK_TABLES]
    payload = {
        "taken_at":  datetime.now(timezone.utc).isoformat(),
        "excluded":  BULK_TABLES,
        "tables":    {},
    }

    failed, redacted = [], 0
    for t in covered:
        try:
            payload["tables"][t], n = _dump_table(sb, t)
            redacted += n
        except Exception as e:
            # One unreadable table must not cost the other thirty-nine. Recorded
            # in the file so a restore cannot mistake "absent" for "was empty".
            failed.append(t)
            payload["tables"][t] = {"__error__": f"{type(e).__name__}: {e}"}
            logger.error(f"  ✗ {t}: {e}")

    payload["secrets_redacted"] = redacted
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, default=str)

    rows = sum(len(v) for v in payload["tables"].values() if isinstance(v, list))
    logger.success(f"  {path.name}  {path.stat().st_size/1048576:.1f} MB  "
                   f"{len(covered)} tables  {rows:,} rows  "
                   f"{redacted} secret(s) redacted")
    if failed:
        logger.error(f"  {len(failed)} table(s) FAILED and are recorded as errors "
                     f"inside the dump: {', '.join(failed)}")
    return path


def verify(path: Path) -> bool:
    """
    A backup nobody has opened is a belief, not a backup.

    Reads the file back the way a restore would, and fails on the three ways a
    dump can be worthless while looking fine on disk: it does not parse, it
    carries a table that errored during the dump, or it is empty of the records
    the whole exercise exists to preserve.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        logger.error(f"  ✗ {path.name} does not read back: {type(e).__name__}: {e}")
        return False

    tables = payload.get("tables", {})
    broken = [t for t, v in tables.items() if not isinstance(v, list)]
    rows   = sum(len(v) for v in tables.values() if isinstance(v, list))

    logger.info(f"  taken_at  {payload.get('taken_at')}")
    logger.info(f"  tables    {len(tables)}   rows {rows:,}   "
                f"size {path.stat().st_size/1048576:.1f} MB")

    if broken:
        logger.error(f"  ✗ {len(broken)} table(s) failed during the dump: {', '.join(broken)}")
        return False

    # The record tables are the reason this file exists. An empty one means the
    # dump ran, succeeded, and preserved nothing that mattered.
    for must in ("system_config", "signal_output_daily", "closed_positions"):
        if must in tables and not tables[must]:
            logger.error(f"  ✗ {must} is empty — the dump captured no record")
            return False

    # A dump carrying live credentials is worse than no dump, because it gets
    # copied. Checked by reading the file rather than by trusting the writer.
    leaked = [r.get("key") for r in tables.get("system_config", [])
              if r.get("is_secret") and r.get("value") != "__REDACTED__"]
    if leaked:
        logger.error(f"  ✗ {len(leaked)} secret(s) written in plaintext: "
                     f"{', '.join(str(k) for k in leaked)} — DELETE THIS FILE AND ROTATE")
        return False
    logger.info(f"  secrets   {payload.get('secrets_redacted', 0)} redacted")

    logger.success(f"  ✓ {path.name} reads back complete")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly off-platform dump of the system of record")
    ap.add_argument("--out", default=None, help="directory for the dump")
    ap.add_argument("--verify", default=None, help="verify an existing dump and exit")
    ap.add_argument("--list", action="store_true", help="show coverage without dumping")
    ap.add_argument("--keep", type=int, default=8,
                    help="how many dumps to retain (default 8 ≈ two months of weeklies)")
    a = ap.parse_args(argv)

    if a.verify:
        return 0 if verify(Path(a.verify)) else 1

    from config import get_supabase, BASE_DIR
    sb = get_supabase()

    if a.list:
        covered = [t for t in _tables(sb) if t not in BULK_TABLES]
        logger.info(f"  covered ({len(covered)}): {', '.join(covered)}")
        logger.info("")
        for t, why in BULK_TABLES.items():
            logger.info(f"  excluded  {t:<22} {why}")
        return 0

    out = Path(a.out) if a.out else Path(BASE_DIR) / "data" / "backups"
    path = backup(sb, out)
    ok   = verify(path)
    # Prune only AFTER the new dump has been proven readable. Deleting the old
    # one first would trade a verified backup for an unverified one.
    if ok:
        for name in prune(out, a.keep):
            logger.info(f"  pruned    {name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
