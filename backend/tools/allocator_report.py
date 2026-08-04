"""
Is the allocator ahead of greedy, and is there yet enough evidence to say?

    python -m tools.allocator_report            the promotion scorecard
    python -m tools.allocator_report --today    what it decided today, and why
    python -m tools.allocator_report --ledger   every verdict, ordered by edge

THE GATE IS DENOMINATED IN DISAGREEMENTS, NOT SESSIONS
------------------------------------------------------
This is the whole reason the tool exists, and it is the number every other
report would get wrong.

A session where the allocator and the greedy path chose the same trade tells you
nothing. It is not weak evidence — it is ZERO evidence, because both policies
produce the same outcome and no comparison is possible. Counting sessions, or
counting decisions, produces a large and meaningless number that will look like
progress for months.

What discriminates is a DISAGREEMENT: the greedy path would have taken X, the
allocator would have taken Y (or nothing). Only then does the forward outcome
say which policy was right. Thirty of those, scored, is the bar — and at two
entries a day against fifty proposals, most days produce none.

WHY THE COUNTERFACTUAL IS HAIRCUT
---------------------------------
A deferred or declined proposal is scored on what it would have returned, at the
price it was refused at. That is systematically generous to whichever policy
refuses more — and the allocator is the policy that refuses more. A zero-slippage
paper counterfactual would show it winning by construction.

So the haircut exists, and where execution shortfall has never been measured this
tool says the comparison is PROVISIONAL rather than quietly presenting a
flattered number. M1 in the readiness review nominated the order path for that
instrumentation and Phase 4 did not build it; until it does, treat every
counterfactual here as an upper bound on the allocator's advantage.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

PAGE = 1000
TAKE, DEFER, DECLINE = "TAKE", "DEFER", "DECLINE"


def _load(sb, trade_date: str | None = None) -> list[dict]:
    rows, off = [], 0
    while True:
        q = sb.table("allocation_decisions").select("*")
        if trade_date:
            q = q.eq("trade_date", trade_date)
        page = q.order("decided_at").range(off, off + PAGE - 1).execute().data or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE
    return rows


def _greedy_choice(day_rows: list[dict], slots: int = 2) -> set[tuple]:
    """
    What the pre-allocator path would have taken, reconstructed.

    Greedy is "highest native rank first, until the slots are gone" — the
    ranked-first plan wins, which is exactly what entry_ranking drove before the
    allocator was demoted to one input among several. Reconstructing it from the
    same rows the allocator saw is what makes the comparison fair: both policies
    are shown the identical proposal set.
    """
    ranked = sorted(day_rows, key=lambda r: -(float(r.get("native_rank") or 0)))
    return {(r["symbol"], r["product"]) for r in ranked[:slots]}


def scorecard(sb) -> int:
    rows = _load(sb)
    if not rows:
        logger.error("  no allocation decisions recorded yet")
        logger.info("  The allocator records only when alloc_shadow_enabled is true")
        logger.info("  AND the daemon has run a session. Nothing accrues overnight.")
        return 1

    days = sorted({r["trade_date"] for r in rows})
    logger.info("=" * 76)
    logger.info("ALLOCATOR PROMOTION SCORECARD")
    logger.info("=" * 76)
    logger.info(f"  {len(rows)} verdict(s) across {len(days)} session(s), "
                f"{days[0]} to {days[-1]}")

    v = Counter(r["verdict"] for r in rows)
    logger.info(f"  TAKE {v[TAKE]}  DEFER {v[DEFER]}  DECLINE {v[DECLINE]}")
    shadow = sum(1 for r in rows if r.get("shadow"))
    logger.info(f"  {shadow} in shadow, {len(rows) - shadow} live")

    # ── the only number that matters ───────────────────────────────────────
    disagreements, agreed, unresolved = [], 0, 0
    for d in days:
        day = [r for r in rows if r["trade_date"] == d]
        greedy = _greedy_choice(day)
        alloc  = {(r["symbol"], r["product"]) for r in day if r["verdict"] == TAKE}
        if greedy == alloc:
            agreed += 1
            continue
        for r in day:
            k = (r["symbol"], r["product"])
            in_g, in_a = k in greedy, k in alloc
            if in_g == in_a:
                continue
            if r.get("outcome_r") is None:
                unresolved += 1
                continue
            disagreements.append({
                "date": d, "symbol": r["symbol"],
                "chosen_by": "allocator" if in_a else "greedy",
                "outcome_r": float(r["outcome_r"]),
                "edge": r.get("edge"), "verdict": r["verdict"],
            })

    logger.info("")
    logger.info("-- Disagreements, the only observations that discriminate --------------")
    logger.info(f"  sessions where the two policies agreed : {agreed}  (zero information)")
    logger.info(f"  scored disagreements                   : {len(disagreements)}")
    logger.info(f"  disagreements awaiting an outcome      : {unresolved}")

    need = 30
    if len(disagreements) < need:
        logger.warning(f"  {need - len(disagreements)} more scored disagreement(s) needed "
                       f"before promotion can be considered.")
        logger.warning("  This is denominated in information, not time. Sessions of "
                       "agreement never move it.")

    if disagreements:
        a = [d["outcome_r"] for d in disagreements if d["chosen_by"] == "allocator"]
        g = [d["outcome_r"] for d in disagreements if d["chosen_by"] == "greedy"]
        logger.info("")
        logger.info("-- Realised R on the disagreements -------------------------------------")
        for label, xs in (("allocator picked", a), ("greedy picked  ", g)):
            if xs:
                se = (statistics.stdev(xs) / len(xs) ** 0.5) if len(xs) > 1 else float("nan")
                logger.info(f"  {label}  n={len(xs):<4} mean {statistics.fmean(xs):+.3f} "
                            f"+/-{se:.3f}   median {statistics.median(xs):+.3f}")
        if a and g:
            diff = statistics.fmean(a) - statistics.fmean(g)
            logger.info("")
            (logger.success if diff > 0 else logger.error)(
                f"  ALLOCATOR IS {'AHEAD' if diff > 0 else 'BEHIND'} BY {abs(diff):.3f} R "
                f"per disagreement")
            logger.warning("  PROVISIONAL: execution shortfall has never been measured, so "
                           "the counterfactual is not haircut. This figure flatters the "
                           "policy that refuses more, which is the allocator. Treat it as "
                           "an upper bound.")

    # ── completeness: buffered writes can leave silent holes ───────────────
    logger.info("")
    logger.info("-- Record completeness -------------------------------------------------")
    per_day = Counter(r["trade_date"] for r in rows)
    thin = [d for d, n in per_day.items() if n < 3]
    if thin:
        logger.warning(f"  {len(thin)} session(s) recorded fewer than 3 verdicts: "
                       f"{', '.join(sorted(thin)[:5])}")
        logger.warning("  Verdicts are buffered and flushed on the 300s timer. A daemon "
                       "that stopped between flushes loses that window, and the gap is "
                       "invisible in the row count alone.")
    else:
        logger.success(f"  every session carries verdicts (min {min(per_day.values())}, "
                       f"max {max(per_day.values())})")

    below = sum(1 for r in rows if r.get("prior_below_floor"))
    if below:
        logger.warning(f"  {below} of {len(rows)} verdicts used a NEUTRAL prior below the "
                       f"sample floor — those decisions carry no empirical opinion, and "
                       f"promoting on them would be promoting on a coin")
    return 0


def today(sb) -> int:
    from config import today_ist
    rows = _load(sb, today_ist().isoformat())
    if not rows:
        logger.info("  no decisions recorded today")
        return 0
    logger.info("=" * 76)
    logger.info(f"TODAY'S ALLOCATION LEDGER — {today_ist()}")
    logger.info("=" * 76)
    logger.info(f"  {'symbol':<12} {'book':<9} {'verdict':<8} {'edge':>9} {'hurdle':>9}  why")
    for r in sorted(rows, key=lambda x: -(float(x.get("edge") or -9e9))):
        e = f"{float(r['edge']):+.4f}" if r.get("edge") is not None else "   n/a"
        h = f"{float(r['hurdle']):.4f}" if r.get("hurdle") is not None else "  n/a"
        fn = logger.success if r["verdict"] == TAKE else logger.info
        fn(f"  {r['symbol']:<12} {r['framework']:<9} {r['verdict']:<8} {e:>9} {h:>9}  "
           f"{(r.get('reason') or '')[:60]}")
    return 0


def ledger(sb) -> int:
    rows = _load(sb)
    if not rows:
        logger.error("  nothing recorded")
        return 1
    logger.info(f"  {len(rows)} verdict(s). By source:")
    for src, n in Counter(r.get("source") or "?" for r in rows).most_common():
        sub = [r for r in rows if (r.get("source") or "?") == src]
        edges = [float(r["edge"]) for r in sub if r.get("edge") is not None]
        taken = sum(1 for r in sub if r["verdict"] == TAKE)
        m = f"mean edge {statistics.fmean(edges):+.4f}" if edges else "no edge computed"
        logger.info(f"    {src:<16} n={n:<5} taken {taken:<4} {m}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Allocator promotion evidence")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--today", action="store_true", help="today's ledger, ordered by edge")
    g.add_argument("--ledger", action="store_true", help="every verdict, grouped by source")
    a = ap.parse_args(argv)

    from config import get_supabase
    sb = get_supabase()
    if a.today:
        return today(sb)
    if a.ledger:
        return ledger(sb)
    return scorecard(sb)


if __name__ == "__main__":
    sys.exit(main())
