"""
Entry-lag analysis — per-engine, from EXISTING data (docs/FINDINGS.md,
10-Sep-2026). Companion to IGN's exit-lag probe (migration 135), but for
the entry side, and built differently on purpose.

WHY NO NEW PROBE OR MIGRATION
------------------------------
The exit-lag probe needed new writes because nothing else records what a
2-second look would have found for an OPEN position. Entries are
different: `intraday_event_shadow` (Stage D3, migration 105) already logs
every engine's 2-second-cadence detections, for every engine with
`intraday_event_core_enabled` on -- which is all of them, live, since
24-Aug-2026 (SDN 6036 rows, GAP 2497, IGN 2351, VCE 1766, VWR 852, ORB 577,
GDB 105 as of 10-Sep). `intraday_setups` already records the ordinary
15-second loop's own real detections. Both carry (trade_date, symbol,
strategy, detected_at). The lag is answerable by joining data that is
ALREADY being collected -- no new write path, no new column, changes
NOTHING about live trading, and is a strictly SAFER instrument than a new
probe would have been.

WHAT THIS MEASURES
-------------------
For each (trade_date, symbol, strategy) key present in BOTH tables, the
gap between the 2s shadow log's FIRST sighting and the 15s loop's own
`detected_at` -- positive means the 2s loop saw it first, by that many
seconds. A key present only in the shadow log (the 2s loop found it, the
15s loop never recorded it at all) is a MISS, not a lag -- reported
separately, since it answers a different question (did the ordinary loop
miss a real setup entirely) than the calibration question this tool
exists for (how much EARLIER would the same detection have fired).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime

from config import fetch_all, get_supabase


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def load_shadow(sb=None, since: str | None = None) -> dict[tuple, datetime]:
    """Earliest 2s-loop sighting per (trade_date, symbol, strategy)."""
    sb = sb or get_supabase()

    def q():
        query = sb.table("intraday_event_shadow").select(
            "trade_date,symbol,strategy,detected_at")
        if since:
            query = query.gte("trade_date", since)
        return query

    rows = fetch_all(q, order_by="id")
    earliest: dict[tuple, datetime] = {}
    for r in rows:
        ts = _parse_ts(r.get("detected_at"))
        if not ts:
            continue
        key = (r["trade_date"], r["symbol"], (r.get("strategy") or "").upper())
        if key not in earliest or ts < earliest[key]:
            earliest[key] = ts
    return earliest


def load_real(sb=None, since: str | None = None) -> dict[tuple, datetime]:
    """
    Earliest 15s-loop sighting per (trade_date, symbol, strategy).

    Reads `ts`, NOT `detected_at` -- checked directly (10-Sep-2026):
    `detected_at` is populated on 0% of RNG/GAP/PDL/PBK rows and only
    partially on every other engine (SDN 22%, ORB 23%, VWR 22%, VCE 52%),
    while `ts` is 100% populated across all ten. A join keyed on
    `detected_at` would have silently reported "zero real detections ever"
    for four engines and undercounted every other one -- a real, separate
    data-quality gap worth its own line in docs/FINDINGS.md, not something
    this tool should quietly work around by picking whichever column
    happens to look populated on the engine being checked first.
    """
    sb = sb or get_supabase()

    def q():
        query = sb.table("intraday_setups").select(
            "trade_date,symbol,strategy,ts")
        if since:
            query = query.gte("trade_date", since)
        return query

    rows = fetch_all(q, order_by="id")
    earliest: dict[tuple, datetime] = {}
    for r in rows:
        ts = _parse_ts(r.get("ts"))
        if not ts:
            continue
        key = (r["trade_date"], r["symbol"], (r.get("strategy") or "").upper())
        if key not in earliest or ts < earliest[key]:
            earliest[key] = ts
    return earliest


def compute_lag(shadow: dict[tuple, datetime], real: dict[tuple, datetime]
                ) -> dict[str, dict]:
    """Per-engine lag stats. Returns {engine: {matched, shadow_only, lags_sec}}."""
    by_engine: dict[str, dict] = defaultdict(
        lambda: {"matched": 0, "shadow_only": 0, "lags_sec": []})

    for key, shadow_ts in shadow.items():
        engine = key[2]
        real_ts = real.get(key)
        d = by_engine[engine]
        if real_ts is None:
            d["shadow_only"] += 1
            continue
        d["matched"] += 1
        d["lags_sec"].append((real_ts - shadow_ts).total_seconds())
    return dict(by_engine)


def report(stats: dict[str, dict]) -> None:
    print()
    print("=" * 78)
    print("ENTRY-LAG ANALYSIS — 2s shadow log vs 15s real detections, per engine")
    print("=" * 78)
    print(f"{'engine':<8}{'matched':>9}{'shadow-only':>13}{'mean lag':>12}"
          f"{'median lag':>13}{'max lag':>10}")
    for engine, d in sorted(stats.items(), key=lambda kv: -kv[1]["matched"]):
        lags = d["lags_sec"]
        if lags:
            mean_l = statistics.mean(lags)
            med_l = statistics.median(lags)
            max_l = max(lags)
        else:
            mean_l = med_l = max_l = float("nan")
        print(f"{engine:<8}{d['matched']:>9}{d['shadow_only']:>13}"
              f"{mean_l:>10.1f}s{med_l:>11.1f}s{max_l:>9.1f}s")
    print()
    print("  'shadow-only' = the 2s loop found a real setup the 15s loop's own")
    print("  intraday_setups record never has at all -- a MISS, not a lag; a")
    print("  separate, real question (does event_core see something detect_all()")
    print("  never fires on) from how much earlier an already-shared detection")
    print("  would have fired.")


if __name__ == "__main__":
    sb = get_supabase()
    shadow = load_shadow(sb)
    real = load_real(sb)
    stats = compute_lag(shadow, real)
    report(stats)
