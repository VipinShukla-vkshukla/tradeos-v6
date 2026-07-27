"""
Audit what send_alerts READS against what the pipeline actually WRITES.

THE FAILURE THIS FINDS
----------------------
validate_selects.py catches columns that do not EXIST — those fail loudly,
because PostgREST rejects the whole query. This catches the quieter problem: a
column that exists, is selected, is rendered, and is NULL on every row because
nothing has written it since some upstream module was retired.

Nothing errors. The digest simply renders a blank, or an `or ""` fallback, and
the reader assumes there was nothing to report. That is how the Positions tab
came to key off `lifecycle`, `event_risk` and `target_1..3` — all real columns,
all permanently NULL — and rendered every position as "Holding" no matter what
state it was really in.

    python -m tools.audit_alert_reads
    python -m tools.audit_alert_reads --days 5

Reports, per table, the columns send_alerts reads and how many recent rows have
a non-NULL value. 0% over a meaningful sample means the read is dead.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase

BASE = Path(__file__).parent.parent
TARGET = BASE / "alerts" / "send_alerts.py"

# Tables send_alerts renders rows from, and how to fetch a recent sample.
SAMPLES: dict[str, dict] = {
    "signal_output_daily": {"order": "date"},
    "open_positions":      {"order": None},
    "signal_log":          {"order": "date"},
}


def _selected_columns(src: str, table: str) -> set[str]:
    """Columns named in .table(<table>).select(...) sites."""
    out: set[str] = set()
    pat = re.compile(
        r'\.table\(\s*["\']' + re.escape(table) + r'["\']\s*\)\s*'
        r'(?:\.\s*)?\.?select\(\s*((?:["\'][^"\']*["\']\s*)+)', re.S)
    for m in pat.finditer(src):
        joined = "".join(re.findall(r'["\']([^"\']*)["\']', m.group(1)))
        if "*" in joined:
            return set()          # select("*") tells us nothing specific
        out |= {c.strip() for c in joined.split(",") if c.strip()}
    return out


_WRITER_CACHE: dict[str, bool] = {}
_ALL_SRC: str | None = None


def _has_writer(column: str) -> bool:
    """
    Does anything in the codebase ASSIGN this column?

    Matches the two shapes writes actually take here — a dict literal key
    (`"planned_stop": value`) and an item assignment (`update["exit_signal"] =`).
    Reading files once and caching keeps this cheap across ~50 lookups.

    send_alerts and tools/ are excluded: a column only send_alerts mentions is
    read, not written, which is exactly the case being detected.
    """
    global _ALL_SRC
    if column in _WRITER_CACHE:
        return _WRITER_CACHE[column]
    if _ALL_SRC is None:
        chunks = []
        for path in BASE.rglob("*.py"):
            parts = set(path.parts)
            if parts & {"__pycache__", "tools"} or path.name in (
                    "send_alerts.py", "ingest_sheets - Copy.py"):
                continue
            try:
                chunks.append(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        _ALL_SRC = "\n".join(chunks)
    pat = re.compile(r'["\']' + re.escape(column) + r'["\']\s*(?::|\]\s*=)')
    _WRITER_CACHE[column] = bool(pat.search(_ALL_SRC))
    return _WRITER_CACHE[column]


def _dict_reads(src: str) -> set[str]:
    """Keys pulled off row dicts — p.get("x"), signal.get("x"), s.get("x")."""
    return set(re.findall(r'\b(?:p|s|signal|pos|row|sig)\.get\(\s*["\']([a-z0-9_]+)["\']', src))


def main(days: int = 5) -> int:
    src = TARGET.read_text(encoding="utf-8")
    sb = get_supabase()
    dict_keys = _dict_reads(src)
    dead_total = 0

    for table, cfg in SAMPLES.items():
        try:
            q = sb.table(table).select("*")
            if cfg["order"]:
                q = q.order(cfg["order"], desc=True)
            rows = q.limit(400).execute().data or []
        except Exception as e:
            logger.error(f"{table}: unreachable — {e}")
            continue
        if not rows:
            logger.warning(f"{table}: no rows to audit")
            continue

        live_cols = set(rows[0].keys())
        # Columns this table could plausibly serve to send_alerts: those named
        # in an explicit select, plus dict keys that match a real column here.
        selected = _selected_columns(src, table)
        candidates = (selected or set()) | (dict_keys & live_cols)
        candidates &= live_cols
        if not candidates:
            continue

        filled: dict[str, int] = {}
        for c in candidates:
            filled[c] = sum(1 for r in rows if r.get(c) not in (None, "", [], {}))

        empty = sorted(c for c, n in filled.items() if n == 0)
        thin = sorted(c for c, n in filled.items()
                      if 0 < n < max(1, len(rows) // 20))

        # NULL-on-every-row is not the same as unwritable, and conflating them
        # makes this tool cry wolf. exit_signal, partial_bookings and
        # original_qty are all NULL today simply because no position has exited
        # or booked a partial yet — they have writers and will fill the moment
        # one does. Only a column with NO writer anywhere is truly dead, and an
        # audit that flags the other kind gets ignored, which costs more than
        # the audit is worth.
        dead = [c for c in empty if not _has_writer(c)]
        pending = [c for c in empty if _has_writer(c)]

        logger.info(f"\n═══ {table} — {len(rows)} recent rows, "
                    f"{len(candidates)} columns read by send_alerts ═══")
        if dead:
            dead_total += len(dead)
            logger.error(f"  DEAD ({len(dead)}) — read, NULL on every row, and NO writer "
                         f"anywhere in the codebase. These always render blank:")
            for c in dead:
                logger.error(f"      {c}")
        if pending:
            logger.info(f"  pending ({len(pending)}) — has a writer, not triggered yet "
                        f"(e.g. no exit or partial has occurred): {', '.join(sorted(pending))}")
        if thin:
            logger.warning(f"  SPARSE ({len(thin)}) — populated on under 5% of rows:")
            for c in thin:
                logger.warning(f"      {c} ({filled[c]}/{len(rows)})")
        if not dead and not thin:
            logger.success("  every column read is populated")

    logger.info("")
    if dead_total:
        logger.error(
            f"{dead_total} dead read(s). None of these raise — the digest renders a "
            f"blank and the reader concludes there was nothing to report, which is "
            f"the most expensive kind of wrong an alert can be."
        )
    else:
        logger.success("No dead reads — everything send_alerts renders has a writer.")
    return dead_total


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Audit send_alerts reads vs pipeline writes")
    ap.add_argument("--days", type=int, default=5)
    sys.exit(0 if main(ap.parse_args().days) == 0 else 0)
