"""
Does deduplicating the hurdle's arrival population actually change the bar,
and by how much? Measurement only — writes nothing, changes no live bar.

WHY THIS EXISTS
---------------
`allocation/hurdle.py::_empirical_base()` builds its percentile population
from EVERY row in `allocation_decisions` — one row per (proposal, cycle),
with no dedup by symbol or day. A candidate that sits near its entry zone
for hours contributes hundreds of near-identical `edge` values to the very
population its own bar is measured against.

Observed 07-Aug-2026: the SWING bar climbed from 0.04454 (10:03 IST) to
0.04842 (15:25 IST) within a single session — a 9% move with no corresponding
change in market conditions the time/scarcity terms alone would explain. The
hypothesis this script tests: that drift is (at least partly) an artifact of
a few long-dwelling, strong candidates outvoting the rest of the population
by sheer repetition, not a genuine tightening of opportunity.

BOTH BOOKS SHARE THIS CODE
---------------------------
`_empirical_base(bucket, framework, sb)` is the same function for SWING and
INTRADAY, filtered only by `framework`. Any fix here changes both books'
bars. Intraday setups are shorter-lived than a swing candidate sitting near
its zone for hours, so the inflation is expected to be smaller there — this
script reports both, so that expectation is checked rather than assumed.

WHAT THIS MEASURES
-------------------
For each framework, over the same 90-day lookback `hurdle()` itself uses:
  1. Raw population size (today's method: one row per cycle) vs deduplicated
     size (one row per symbol per trade_date, mean edge) — the inflation
     ratio.
  2. The 10 most-repeated (symbol, trade_date) pairs — direct evidence of
     concentration, or its absence.
  3. The 75th and 95th percentile (pct0 and the cap `hurdle()` interpolates
     toward under time/scarcity pressure) under BOTH population definitions,
     so the actual bar delta this would produce is visible before anyone
     decides whether to ship it.

Usage:
    python -m tools.hurdle_population_audit
    python -m tools.hurdle_population_audit --framework INTRADAY
    python -m tools.hurdle_population_audit --days 30
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import cfg_int, get_supabase, today_ist, fetch_all

PAGE = 1000


def _fetch_all(sb, framework: str, since: str) -> list[dict]:
    # SORTED PAGING. `allocation_decisions` is 20,873 rows — twenty-one pages,
    # and this audit is what the hurdle's own percentile base is inspected
    # through. An unsorted pager returns the right COUNT with the wrong rows,
    # so a percentile taken over it is biased and its row total says nothing.
    return fetch_all(lambda: sb.table("allocation_decisions")
                     .select("symbol,edge,regime_bucket,trade_date,decided_at")
                     .eq("framework", framework)
                     .gte("trade_date", since)
                     .not_.is_("edge", "null"), page=PAGE)


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    q = max(0.0, min(q, 1.0))
    return sorted_vals[min(int(q * len(sorted_vals)), len(sorted_vals) - 1)]


def audit(framework: str, days: int, sb=None) -> None:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()

    from config import cfg_bool
    live = cfg_bool(f"alloc_hurdle_dedup_{framework.lower()}", False)

    rows = _fetch_all(sb, framework, since)
    logger.info("=" * 78)
    logger.info(f"HURDLE POPULATION AUDIT — {framework}, since {since}")
    logger.info(f"  alloc_hurdle_dedup_{framework.lower()} is currently "
                f"{'ON — live bar already uses the deduplicated column below' if live else 'OFF — live bar uses the raw column below, unchanged from main'}")
    logger.info("=" * 78)

    if not rows:
        logger.warning("  no rows — nothing to measure")
        return

    raw_edges = sorted(float(r["edge"]) for r in rows if r.get("edge") is not None)

    # One observation per (symbol, trade_date): mean edge across that day's
    # repeated polls. This is the proposed population.
    by_symbol_day: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        key = (r.get("symbol"), r.get("trade_date"))
        by_symbol_day[key].append(float(r["edge"]))
    dedup_edges = sorted(statistics.fmean(v) for v in by_symbol_day.values())

    inflation = len(raw_edges) / max(len(dedup_edges), 1)
    logger.info(f"  raw rows (today's population):        {len(raw_edges):>6}")
    logger.info(f"  distinct symbol-days (proposed):       {len(dedup_edges):>6}")
    logger.info(f"  inflation ratio:                       {inflation:>6.2f}x")
    logger.info("")

    # Concentration: which (symbol, day) pairs contribute the most rows to
    # the RAW population — direct evidence for or against the hypothesis.
    counts = Counter((r.get("symbol"), r.get("trade_date")) for r in rows)
    logger.info("  most-repeated (symbol, day) pairs, raw population:")
    for (sym, day), n in counts.most_common(10):
        mean_e = statistics.fmean(by_symbol_day[(sym, day)])
        logger.info(f"    {sym:<14} {day}   {n:>4} rows   mean edge {mean_e:+.4f}")
    logger.info("")

    floor = cfg_int("alloc_hurdle_min_sample", 40)
    for pct, label in ((0.75, "baseline (pct0)"), (0.95, "cap (max pressure)")):
        raw_bar = _quantile(raw_edges, pct)
        dedup_bar = _quantile(dedup_edges, pct)
        delta = dedup_bar - raw_bar
        logger.info(f"  {label:<20} p{pct:.0%}   raw {raw_bar:+.4f}   "
                    f"dedup {dedup_bar:+.4f}   delta {delta:+.4f} "
                    f"({'LOWER' if delta < 0 else 'HIGHER'} under dedup)")

    if len(dedup_edges) < floor:
        logger.warning(f"  deduplicated population ({len(dedup_edges)}) is below "
                       f"the {floor}-sample floor that gates a real percentile — "
                       f"dedup would currently push this framework/bucket toward "
                       f"cold-start more often. Worth knowing before shipping.")


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
