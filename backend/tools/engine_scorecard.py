"""
Which engine actually works, and under what conditions? READ-ONLY.

    python -m tools.engine_scorecard                 # both books
    python -m tools.engine_scorecard --book INTRADAY
    python -m tools.engine_scorecard --by phase      # segment by session phase

WHY THIS EXISTS
---------------
`intraday/outcomes.py::engine_scorecard()` already reports hit rate and average
outcome per engine, but on the population as stored — one row per evaluation
cycle — so a setup that lingered near its level votes once per 15 seconds. That
inflation is not neutral: measured 10-Aug-2026, it FLIPPED THE SIGN on the two
highest-volume engines (ORB read +0.089 inflated, -0.215 deduplicated; VWR
+0.205 against -0.040), and RNG's entire "n=11" record was one setup counted
eleven times.

Every number here is therefore deduplicated to one observation per
(symbol, engine, trade_date) first, and every number carries the count it was
computed from. An engine's lifecycle is not a thing to decide on a mean without
its n.

THE THREE COLUMNS THAT DECIDE WHETHER AN ENGINE IS WORTH RUNNING
-----------------------------------------------------------------
    gross R    what the engine's edge is worth before friction
    cost R     what the round trip costs, as a fraction of the risk taken
               = cost_pct / risk_pct, so a TIGHT stop makes this LARGER
    net R      gross - cost. The only column that pays for anything.

An engine can have real predictive skill (positive gross R) and still lose
money because its stops are too tight relative to a round trip. That is a
SIZING/STOP problem, not a signal problem, and the two demand opposite fixes —
which is exactly why they are shown separately rather than as one net figure.
Measured on this account, 10-Aug-2026: the intraday SHORT book had gross
+0.141R against a cost of 0.293R. The signal was real. The stop was too tight
to pay for it.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_int, fetch_all

PAGE = 1000


def _fetch(sb, table: str, cols: str) -> list[dict]:
    # SORTED PAGING. This paged on `.range()` alone. LIMIT/OFFSET without an
    # ORDER BY has no stable row order across requests, so pages may repeat
    # rows and skip others while the TOTAL stays exactly right — which is why
    # nothing ever raised. Measured on the live book 15-Aug-2026, this very
    # query, three trials: 8324/8324 distinct, then 8324 rows / 5000 distinct,
    # then 8324/8324. The bad trial silently dropped 3,324 real detections and
    # triple-counted others, and every per-engine hit rate below is a MEAN over
    # these rows.
    #
    # Note this reader was invisible to F-23's own census and to
    # tests/test_static_analysis.py, because both match `.table("literal")` and
    # this one passes the table name as a PARAMETER.
    return fetch_all(lambda: sb.table(table).select(cols)
                     .not_.is_("outcome_pct", "null"), page=PAGE)


def _intraday_observations(sb) -> list[dict]:
    """One observation per (symbol, engine, day), gate-passed only.

    Gate-passed because the question is "if I take a trade from this engine,
    what happens" — the refused ones answer a different question (what standing
    down cost), which `tools/allocator_replay.py` is for.
    """
    rows = _fetch(sb, "intraday_setups",
                  "trade_date,symbol,strategy,phase,direction,entry,stop,"
                  "outcome,outcome_pct,cost_pct,cost_verdict")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if (r.get("cost_verdict") or "").upper() != "TAKEN":
            continue
        groups[(r.get("symbol"), r.get("strategy"), r.get("trade_date"))].append(r)

    out = []
    for grp in groups.values():
        r = grp[0]
        try:
            entry, stop = float(r["entry"]), float(r["stop"])
            risk_pct = abs(entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            cost_pct = statistics.fmean(float(g.get("cost_pct") or 0) for g in grp)
            net_pct = statistics.fmean(float(g["outcome_pct"]) for g in grp)
            out.append({
                "engine": r.get("strategy") or "?",
                "dir": (r.get("direction") or "LONG").upper(),
                "phase": r.get("phase") or "?",
                "date": r.get("trade_date"),
                "risk_pct": risk_pct,
                "cost_r": cost_pct / risk_pct,
                # outcome_pct is stored NET of the round trip; add it back so
                # gross and cost are two separate, independently actionable
                # numbers rather than one blended one.
                "gross_r": (net_pct + cost_pct) / risk_pct,
                "won": (r.get("outcome") == "TARGET"),
            })
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            continue
    return out


def _report(obs: list[dict], key: str, title: str, min_n: int) -> None:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        buckets[o[key]].append(o)
    if not buckets:
        logger.warning(f"  {title}: nothing resolved yet")
        return

    logger.info("")
    logger.info(f"  {title}")
    logger.info("  " + "-" * 92)
    logger.info(f"  {'bucket':<16}{'n':>5}{'days':>6}{'win%':>7}"
                f"{'gross R':>10}{'cost R':>9}{'NET R':>9}{'stop%':>8}   verdict")
    logger.info("  " + "-" * 92)

    for name, rows in sorted(buckets.items(),
                             key=lambda kv: -statistics.fmean(
                                 x["gross_r"] - x["cost_r"] for x in kv[1])):
        n = len(rows)
        days = len({x["date"] for x in rows})
        gross = statistics.fmean(x["gross_r"] for x in rows)
        cost = statistics.fmean(x["cost_r"] for x in rows)
        net = gross - cost
        stop = statistics.fmean(x["risk_pct"] for x in rows)
        win = sum(1 for x in rows if x["won"]) / n * 100

        # The verdict names WHICH problem the engine has, because the fix is
        # different for each and a single "bad" label hides that.
        if n < min_n:
            verdict = f"too thin (needs {min_n})"
        elif gross <= 0:
            verdict = "NO EDGE — signal problem"
        elif net <= 0:
            verdict = "edge real, COSTS eat it — widen stop"
        else:
            verdict = "PAYS"
        logger.info(f"  {name:<16}{n:>5}{days:>6}{win:>6.0f}%"
                    f"{gross:>+10.3f}{cost:>9.3f}{net:>+9.3f}{stop:>7.2f}%   {verdict}")
    logger.info("  " + "-" * 92)


def main(book: str, by: str, min_n: int) -> int:
    sb = get_supabase()
    if book.upper() in ("INTRADAY", "BOTH"):
        obs = _intraday_observations(sb)
        if not obs:
            logger.error("  no resolved gate-passed intraday setups")
        else:
            logger.info(f"  INTRADAY — {len(obs)} deduplicated gate-passed trades "
                        f"across {len({o['date'] for o in obs})} session(s)")
            _report(obs, "engine", "BY ENGINE", min_n)
            if by == "phase":
                _report(obs, "phase", "BY SESSION PHASE", min_n)
            _report(obs, "dir", "BY DIRECTION", min_n)

            gross = statistics.fmean(o["gross_r"] for o in obs)
            cost = statistics.fmean(o["cost_r"] for o in obs)
            logger.info("")
            if gross <= 0:
                logger.warning(
                    f"  BOOK-LEVEL: gross {gross:+.3f}R before costs. The intraday "
                    f"signal set has no edge to monetise — widening stops or cutting "
                    f"fees cannot fix a negative gross. This is engine work.")
            elif gross - cost <= 0:
                need = gross and (cost / gross)
                logger.warning(
                    f"  BOOK-LEVEL: gross {gross:+.3f}R is REAL but costs {cost:.3f}R "
                    f"eat it. Stops average {statistics.fmean(o['risk_pct'] for o in obs):.2f}%; "
                    f"cost_R scales as 1/stop, so roughly {need:.1f}x the current stop "
                    f"width breaks even on the same signals.")
            else:
                logger.success(f"  BOOK-LEVEL: net {gross - cost:+.3f}R per trade.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Per-engine performance, deduplicated (read-only)")
    ap.add_argument("--book", default="INTRADAY", choices=["INTRADAY", "BOTH"])
    ap.add_argument("--by", default="engine", choices=["engine", "phase"])
    ap.add_argument("--min-n", type=int, default=cfg_int("priors_min_sample_intraday", 30))
    a = ap.parse_args()
    sys.exit(main(a.book, a.by, a.min_n))
