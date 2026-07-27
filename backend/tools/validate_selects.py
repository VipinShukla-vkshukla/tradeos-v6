"""
Validate every .table(X).select("a,b,c") in the codebase against the live schema.

WHY THIS EXISTS
---------------
Migration 008 dropped 20 columns after an audit that checked which columns were
NULL and which had WRITERS. It did not check for READERS. `master_shortlist.
suggested` was 100% NULL with no writer — and was still named in the SELECT
lists of ai_decision_engine and market_intelligence_engine.

PostgREST rejects the ENTIRE query for one unknown column. So a column that
contributed nothing but NULLs took down steps 18 and 19 completely on the first
evening run after the migration, producing "0 ranked" and no AI picks at all.
The failure was invisible in advance and total on arrival.

Grep is not enough to prevent a repeat, because a column name appears in
comments, dict keys, and .get() calls that are all harmless. What matters is
specifically the SELECT lists, because those are the only place an unknown
column is fatal rather than merely useless.

    python -m tools.validate_selects          # report
    python -m tools.validate_selects --strict # exit 1 on any problem (CI)

Run it after every migration, and before pushing one.
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

# .table("name") ... .select("cols")  — tolerating newlines and adjacent
# string-literal concatenation, which is how the long lists are written.
PATTERN = re.compile(
    r'\.table\(\s*["\'](?P<table>\w+)["\']\s*\)\s*'
    r'(?:\.\s*)?\.?select\(\s*(?P<cols>(?:["\'][^"\']*["\']\s*)+)',
    re.S,
)

SKIP_DIRS = {"__pycache__", ".git", "node_modules", "venv", ".venv", "migrations"}
SKIP_FILES = {"ingest_sheets - Copy.py"}

# Not column lists: PostgREST embedded resources and wildcards.
def _split_cols(raw: str) -> list[str]:
    joined = "".join(re.findall(r'["\']([^"\']*)["\']', raw))
    if "*" in joined or "(" in joined:
        return []
    return [c.strip() for c in joined.split(",") if c.strip()]


def live_columns(sb, table: str) -> set[str] | None:
    """Column names as PostgREST sees them. None when the table is unreachable."""
    try:
        rows = sb.table(table).select("*").limit(1).execute().data
    except Exception:
        return None
    if rows:
        return set(rows[0].keys())
    # An empty table returns no keys, so probe one column at a time instead of
    # reporting every column as missing.
    return set()


def scan() -> list[tuple[Path, int, str, list[str]]]:
    out = []
    for path in BASE.rglob("*.py"):
        if any(p in SKIP_DIRS for p in path.parts) or path.name in SKIP_FILES:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in PATTERN.finditer(src):
            cols = _split_cols(m.group("cols"))
            if not cols:
                continue
            line = src[:m.start()].count("\n") + 1
            out.append((path, line, m.group("table"), cols))
    return out


def main(strict: bool = False) -> int:
    sb = get_supabase()
    sites = scan()
    logger.info(f"Scanned {len(set(s[0] for s in sites))} files, "
                f"{len(sites)} select() sites")

    schema: dict[str, set[str] | None] = {}
    problems = 0
    empty_tables: set[str] = set()

    for path, line, table, cols in sites:
        if table not in schema:
            schema[table] = live_columns(sb, table)
        live = schema[table]

        if live is None:
            logger.error(f"  {path.relative_to(BASE)}:{line} — table '{table}' does not exist")
            problems += 1
            continue
        if not live:
            # Empty table: verify column-by-column rather than guessing.
            missing = []
            for c in cols:
                try:
                    sb.table(table).select(c).limit(1).execute()
                except Exception:
                    missing.append(c)
            if missing:
                logger.error(f"  {path.relative_to(BASE)}:{line} — {table} missing: "
                             f"{', '.join(missing)}")
                problems += 1
            empty_tables.add(table)
            continue

        missing = [c for c in cols if c not in live]
        if missing:
            logger.error(
                f"  {path.relative_to(BASE)}:{line} — {table} has no column(s): "
                f"{', '.join(missing)}"
            )
            logger.error("     PostgREST fails the WHOLE query for one unknown column, "
                         "so this select returns nothing at all.")
            problems += 1

    if problems:
        logger.error(f"\n{problems} broken select site(s). Every one of them is a step "
                     f"that will fail completely, not degrade.")
    else:
        logger.success(f"All {len(sites)} select sites match the live schema")
    if empty_tables:
        logger.info(f"  (verified column-by-column for empty tables: {', '.join(sorted(empty_tables))})")

    return 1 if (problems and strict) else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Validate select() column lists against the DB")
    ap.add_argument("--strict", action="store_true", help="exit 1 when problems are found")
    sys.exit(main(ap.parse_args().strict))
