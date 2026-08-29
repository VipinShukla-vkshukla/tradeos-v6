"""
Would writing allocation_decisions only on MATERIAL CHANGE — not the blanket
per-symbol-day average `alloc_hurdle_dedup_*` already tested and rejected —
give the same bar with fewer rows? Measurement only. Writes nothing, changes
no live bar, no live code path calls this.

WHY THIS EXISTS
---------------
`alloc_hurdle_dedup_swing`/`_intraday` collapses an entire day's repeated
polls for a symbol into ONE averaged edge before the percentile is taken —
see tools/hurdle_population_audit.py. That was measured and kept OFF for
both books (29-Aug-2026 decision, docs/FINDINGS.md).

That is not the only way to cut the ~15-20k rows/day this table receives.
Instead of averaging a whole day into one number, hold the allocator's
per-cycle verdict in memory and only WRITE a new row when something actually
changed: the verdict flips, the regime bucket changes, the edge moves past a
threshold, or a heartbeat interval has elapsed since the last write (so a
candidate hovering unchanged for hours still contributes SOMETHING to the
day's population, just not one row per 15-second cycle for the whole span).

This preserves the actual trajectory a candidate walked through the day —
unlike the mean-of-everything dedup, a real move from edge -1.20 to -0.90
still shows up as two distinct points, not one blended average. Whether
that distinction actually produces a different (better) bar than the
already-rejected dedup, or reproduces the same problem via a different
mechanism, is exactly what this script measures — it does not assume either
answer.

METHOD
------
Pull the real logged rows over the same lookback hurdle() uses. For each
(symbol, trade_date), replay the rows in the order they were actually
written and decide, cycle by cycle, whether the proposed rule would have
kept or suppressed each one. Compare the resulting population's p75/p95 —
the same two hurdle() reads — against what actually happened. Report both
the bar delta (is the gate still measuring the same thing) and the row-count
reduction (is this worth doing at all).

Two parameter sets are run side by side (a tighter and a looser threshold)
because a single arbitrary number would just be a different guess dressed
up as a measurement.

Usage:
    python -m tools.material_change_replay_audit
    python -m tools.material_change_replay_audit --framework INTRADAY
    python -m tools.material_change_replay_audit --days 30
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import cfg_int, get_supabase, today_ist, fetch_all

PAGE = 1000


@dataclass
class Rule:
    label: str
    edge_threshold: float   # keep if |edge - last_kept.edge| >= this
    heartbeat_s: float      # keep if this many seconds elapsed since last_kept


RULES = [
    Rule("tight (edge 0.01R, 5-min heartbeat)", 0.01, 300),
    Rule("loose (edge 0.02R, 15-min heartbeat)", 0.02, 900),
]


def _fetch_all(sb, framework: str, since: str) -> list[dict]:
    # SORTED PAGING — same reason as hurdle_population_audit.py: an unsorted
    # pager returns the right COUNT with the wrong rows over 20+ pages.
    return fetch_all(lambda: sb.table("allocation_decisions")
                     .select("symbol,edge,verdict,regime_bucket,trade_date,decided_at")
                     .eq("framework", framework)
                     .gte("trade_date", since)
                     .not_.is_("edge", "null"), page=PAGE)


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    q = max(0.0, min(q, 1.0))
    return sorted_vals[min(int(q * len(sorted_vals)), len(sorted_vals) - 1)]


def _parse_ts(v) -> datetime:
    return datetime.fromisoformat(str(v))


def _replay(rows: list[dict], rule: Rule) -> list[dict]:
    """
    Walk each (symbol, trade_date) group in real write order and return only
    the rows this rule would have kept. A TAKE is always kept — it is a real
    (paper or live) trade's permanent record, not a candidate hovering near
    its zone, and suppressing it would corrupt the promotion evidence, not
    just the population.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r.get("symbol"), r.get("trade_date"))].append(r)

    kept: list[dict] = []
    for key, group in groups.items():
        group.sort(key=lambda r: _parse_ts(r["decided_at"]))
        last = None
        for r in group:
            edge = float(r["edge"])
            ts = _parse_ts(r["decided_at"])
            is_take = r.get("verdict") == "TAKE"
            keep = (
                last is None
                or is_take
                or last["verdict"] == "TAKE"
                or r.get("verdict") != last["verdict"]
                or r.get("regime_bucket") != last["regime_bucket"]
                or abs(edge - last["edge"]) >= rule.edge_threshold
                or (ts - last["ts"]).total_seconds() >= rule.heartbeat_s
            )
            if keep:
                kept.append(r)
                last = {"verdict": r.get("verdict"), "regime_bucket": r.get("regime_bucket"),
                        "edge": edge, "ts": ts}
    return kept


def audit(framework: str, days: int, sb=None) -> None:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()

    rows = _fetch_all(sb, framework, since)
    logger.info("=" * 78)
    logger.info(f"MATERIAL-CHANGE REPLAY AUDIT — {framework}, since {since}")
    logger.info("=" * 78)

    if not rows:
        logger.warning("  no rows — nothing to measure")
        return

    raw_edges = sorted(float(r["edge"]) for r in rows if r.get("edge") is not None)
    floor = cfg_int("alloc_hurdle_min_sample", 40)

    logger.info(f"  raw rows (today's actual population): {len(raw_edges):>6}")
    logger.info("")

    for rule in RULES:
        kept = _replay(rows, rule)
        kept_edges = sorted(float(r["edge"]) for r in kept)
        reduction_pct = 100.0 * (1 - len(kept_edges) / max(len(raw_edges), 1))

        logger.info(f"  RULE: {rule.label}")
        logger.info(f"    rows kept: {len(kept_edges):>6} / {len(raw_edges)} "
                    f"({reduction_pct:.1f}% fewer rows -> proportionally less storage)")

        for pct, label in ((0.75, "baseline (pct0)"), (0.95, "cap (max pressure)")):
            raw_bar = _quantile(raw_edges, pct)
            kept_bar = _quantile(kept_edges, pct)
            delta = kept_bar - raw_bar
            logger.info(f"      {label:<20} p{pct:.0%}   raw {raw_bar:+.4f}   "
                        f"replayed {kept_bar:+.4f}   delta {delta:+.4f} "
                        f"({'LOWER' if delta < 0 else 'HIGHER'} under this rule)")

        if len(kept_edges) < floor:
            logger.warning(f"    replayed population ({len(kept_edges)}) is below the "
                           f"{floor}-sample floor — this rule would push {framework} "
                           f"toward cold-start (bar admits everything) more often than "
                           f"today. This is the exact failure mode from 10-Aug-2026.")
        logger.info("")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--framework", default="SWING", choices=["SWING", "INTRADAY"])
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--both", action="store_true", help="run both frameworks")
    a = ap.parse_args()
    sb = get_supabase()
    if a.both:
        audit("SWING", a.days, sb)
        logger.info("")
        audit("INTRADAY", a.days, sb)
    else:
        audit(a.framework, a.days, sb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
