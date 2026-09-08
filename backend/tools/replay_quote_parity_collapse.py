"""
replay_quote_parity_collapse.py — 10-Sep-2026, migration 131.

Would quote_parity.should_log()'s write-time collapse have produced the
SAME range_verdict()/vwap_verdict() pass/fail as the raw, uncollapsed
data? Measurement only. Writes nothing, changes no live behaviour, calls
the actual production verdict functions rather than reimplementing them —
this is what the check_quote_parity() health check itself would have
computed, run against two different populations of the same real data.

METHOD
------
Pull every real intraday_quote_parity row. Group by (symbol, field, date),
replay each group in the order it was actually written, and apply
should_log() exactly as intraday/engine.py::apply_live_quotes() does —
same predicate, imported, not reimplemented. Compare row counts (is this
worth doing) and range_verdict()/vwap_verdict() output (is the gate still
measuring the same thing) between the raw population and the collapsed one.

Usage:
    python -m tools.replay_quote_parity_collapse
    python -m tools.replay_quote_parity_collapse --days 3
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import cfg_float, get_supabase, today_ist, fetch_all

from tools.quote_parity import should_log, range_verdict, vwap_verdict

PAGE = 1000


def _fetch(sb, since: str) -> list[dict]:
    return fetch_all(lambda: sb.table("intraday_quote_parity")
                     .select("symbol,field,diff_pct,ts")
                     .gte("ts", since)
                     .not_.is_("diff_pct", "null"), order_by="id", page=PAGE)


def _parse_ts(v) -> datetime:
    return datetime.fromisoformat(str(v))


def replay(rows: list[dict], vwap_tolerance: float, heartbeat_s: float) -> list[dict]:
    """Returns the rows should_log() would have kept, in original order."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        ts = _parse_ts(r["ts"])
        groups[(r["symbol"], r["field"], ts.date().isoformat())].append(r)

    kept: list[dict] = []
    for group in groups.values():
        group.sort(key=lambda r: _parse_ts(r["ts"]))
        state = None
        for r in group:
            ts = _parse_ts(r["ts"])
            diff = float(r["diff_pct"])
            if should_log(r["field"], diff, state, ts, vwap_tolerance, heartbeat_s):
                kept.append(r)
                state = {"diff_pct": diff, "logged_at": ts}
    return kept


def audit(days: int, sb=None) -> None:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()
    # Same defaults as intraday/engine.py::apply_live_quotes() and migration
    # 131's own INSERT — three copies of one number is exactly the kind of
    # drift this project has been burned by before, so if you change one,
    # change all three (or better, refactor to one shared constant).
    vwap_tolerance = cfg_float("quote_parity_vwap_collapse_tolerance", 0.04)
    heartbeat_s = cfg_float("quote_parity_heartbeat_s", 7200.0)

    rows = _fetch(sb, since)
    logger.info("=" * 86)
    logger.info(f"QUOTE-PARITY WRITE-COLLAPSE REPLAY — since {since}, "
               f"vwap_tolerance={vwap_tolerance}, heartbeat_s={heartbeat_s}")
    logger.info("=" * 86)

    if not rows:
        logger.warning("  no rows — nothing to measure")
        return

    by_field_raw = defaultdict(int)
    for r in rows:
        by_field_raw[r["field"]] += 1

    kept = replay(rows, vwap_tolerance, heartbeat_s)
    by_field_kept = defaultdict(int)
    for r in kept:
        by_field_kept[r["field"]] += 1

    logger.info(f"  raw rows:   {len(rows):>7}")
    logger.info(f"  kept rows:  {len(kept):>7}  "
               f"({100.0 * (1 - len(kept) / len(rows)):.1f}% fewer)")
    logger.info("")
    for field in sorted(by_field_raw):
        raw_n, kept_n = by_field_raw[field], by_field_kept.get(field, 0)
        pct = 100.0 * (1 - kept_n / raw_n) if raw_n else 0.0
        logger.info(f"    {field:<12} {raw_n:>7} -> {kept_n:>6}  ({pct:5.1f}% fewer)")

    logger.info("")
    logger.info("  VERDICT INVARIANCE — the actual production functions, called twice:")
    for name, fn in (("range_verdict", range_verdict), ("vwap_verdict", vwap_verdict)):
        raw_ok, raw_detail = fn(rows)
        kept_ok, kept_detail = fn(kept)
        match = "MATCH" if raw_ok == kept_ok else "MISMATCH"
        log = logger.info if raw_ok == kept_ok else logger.error
        log(f"    {name}: raw={raw_ok} ({raw_detail})")
        log(f"    {name}: collapsed={kept_ok} ({kept_detail})  [{match}]")
        if raw_ok != kept_ok:
            logger.error(f"    {name} VERDICT CHANGED under collapse — do not arm "
                         f"quote_parity_write_collapse_enabled until this is understood")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    audit(a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
