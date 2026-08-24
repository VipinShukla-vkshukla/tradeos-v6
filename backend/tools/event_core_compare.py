"""
Gate D3's own comparison — did the tick-triggered shadow core
(intraday/event_core.py) agree with the trusted polling loop, and how
much sooner did it react?

    python -m tools.event_core_compare --date 2026-08-25
    python -m tools.event_core_compare --days 10

READ-ONLY. Reads intraday_event_shadow and intraday_setups, writes
nothing.

WHAT "MATCH" MEANS
-------------------
A shadow detection and a trusted-loop detection are the SAME observation
when they share (symbol, sub_engine) and their `detected_at` timestamps
fall within `--window` seconds of each other (60s default — comfortably
wider than eval_interval_s's own 15s cadence, so a genuine same-event
pair is not missed over ordinary cycle-boundary timing). The closest
trusted-loop row within the window is the match; ties go to whichever
came first.

WHAT THIS CANNOT MEASURE YET
------------------------------
`intraday_setups.detected_at` (migration 106) is populated only from the
moment this stage shipped — a row written before that has no detection
timestamp and is excluded from matching entirely, not treated as a
non-match. Gate D3 itself needs a real accumulated window (10 sessions or
200 directly-comparable decisions) this tool cannot manufacture; it can
only report honestly on whatever has actually accumulated by the time it
is run.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, today_ist, fetch_all


@dataclass
class MatchResult:
    matched: int
    shadow_only: int
    latency_gaps_s: list[float]   # positive = shadow reacted sooner
    disagreements: list[dict]     # matched pairs whose direction differed


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def compare_day(sb, date_str: str, window_s: int = 60) -> MatchResult:
    """PURE-ish (one read per table, no writes) — the whole comparison in
    one function so a caller (or a test) can hand it fixture rows without
    hitting the database, same shape as allocation.scoring's own read/
    compute split."""
    shadow_rows = (sb.table("intraday_event_shadow").select(
        "symbol,sub_engine,direction,detected_at")
        .eq("trade_date", date_str).execute().data or [])
    # fetch_all(), NOT a plain .eq().execute() — intraday_setups re-records
    # a lingering setup on every evaluation cycle it is still near its
    # level (ORB: 234 rows for 23 distinct setups, measured live), so even
    # ONE trading day's rows can exceed PostgREST's 1000-row cap. This
    # project's own static-analysis check (test_static_analysis.py)
    # explicitly excludes this table from "a day filter alone is enough" —
    # caught by that check before this ever shipped, not after.
    setup_rows = fetch_all(
        lambda: sb.table("intraday_setups").select(
            "id,symbol,strategy,direction,detected_at,meta").eq("trade_date", date_str),
        order_by="id")

    # sub_engine on intraday_setups lives in meta, same defensive read
    # allocation/scoring.py::_engine_of() already needs.
    import json as _json
    setups = []
    for r in setup_rows:
        dt = _parse_ts(r.get("detected_at"))
        if dt is None:
            continue
        meta = r.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        sub_engine = (meta or {}).get("sub_engine") or r.get("strategy")
        setups.append({"symbol": r.get("symbol"), "sub_engine": sub_engine,
                       "direction": r.get("direction"), "detected_at": dt})

    window = timedelta(seconds=window_s)
    matched, shadow_only, gaps, disagreements = 0, 0, [], []

    for s in shadow_rows:
        s_dt = _parse_ts(s.get("detected_at"))
        if s_dt is None:
            continue
        candidates = [u for u in setups
                     if u["symbol"] == s.get("symbol")
                     and u["sub_engine"] == s.get("sub_engine")
                     and abs((u["detected_at"] - s_dt).total_seconds()) <= window_s]
        if not candidates:
            shadow_only += 1
            continue
        best = min(candidates, key=lambda u: abs((u["detected_at"] - s_dt).total_seconds()))
        matched += 1
        gap = (best["detected_at"] - s_dt).total_seconds()
        gaps.append(gap)
        if best["direction"] != s.get("direction"):
            disagreements.append({
                "symbol": s.get("symbol"), "sub_engine": s.get("sub_engine"),
                "shadow_direction": s.get("direction"),
                "trusted_direction": best["direction"],
            })

    return MatchResult(matched=matched, shadow_only=shadow_only,
                       latency_gaps_s=gaps, disagreements=disagreements)


def main():
    ap = argparse.ArgumentParser(description="Gate D3 comparison: shadow core vs trusted loop")
    ap.add_argument("--date", help="single trade_date, YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1, help="most recent N calendar days if --date omitted")
    ap.add_argument("--window", type=int, default=60, help="match window, seconds")
    a = ap.parse_args()

    sb = get_supabase()
    if a.date:
        dates = [a.date]
    else:
        today = today_ist()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(a.days)]

    total = MatchResult(0, 0, [], [])
    for d in dates:
        r = compare_day(sb, d, window_s=a.window)
        total.matched += r.matched
        total.shadow_only += r.shadow_only
        total.latency_gaps_s.extend(r.latency_gaps_s)
        total.disagreements.extend(r.disagreements)
        if r.matched or r.shadow_only:
            logger.info(f"  {d}: {r.matched} matched, {r.shadow_only} shadow-only, "
                       f"{len(r.disagreements)} disagreement(s)")

    n = len(total.latency_gaps_s)
    logger.info("─" * 60)
    logger.info(f"TOTAL: {total.matched} matched, {total.shadow_only} shadow-only, "
               f"{len(total.disagreements)} disagreement(s)")
    if n:
        mean_gap = sum(total.latency_gaps_s) / n
        logger.info(f"  mean latency gap: {mean_gap:+.1f}s "
                   f"(positive = shadow reacted sooner)")
    else:
        logger.info("  no matched pairs yet — Gate D3 needs real accumulated "
                    "sessions before this comparison means anything")
    for d in total.disagreements[:10]:
        logger.info(f"  disagreement: {d}")
    return {"matched": total.matched, "shadow_only": total.shadow_only,
           "disagreements": len(total.disagreements),
           "mean_latency_gap_s": (sum(total.latency_gaps_s) / n) if n else None}


if __name__ == "__main__":
    main()
