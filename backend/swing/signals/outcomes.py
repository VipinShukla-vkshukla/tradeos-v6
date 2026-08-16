"""
What every swing plan would have done, whether or not it was traded.

    python -m swing.signals.outcomes              resolve everything now due
    python -m swing.signals.outcomes --backfill   re-resolve the whole history
    python -m swing.signals.outcomes --status     how much of the field is scored

THE UNBIASED DENOMINATOR, FOR THE SWING BOOK
--------------------------------------------
The architecture calls measurement this system's defining asset: it records the
proposals it rejected and resolves the outcome of every detection it never
traded. With two entries taken against fifty-odd daily candidates, more than
ninety percent of the information lives in the trades NOT taken.

For the intraday book that is true. `intraday/outcomes.resolve_day` scores every
detection at the close, and on 04-Aug-2026 it had resolved 595 of 595 — a
perfect record.

For the swing book it was fiction. `signal_output_daily` carries seven outcome
columns, written as NULL by final_snapshot and filled in by nothing:

    outcome_entered  outcome_category  outcome_return_pct  outcome_hold_days
    outcome_entry_price  outcome_exit_price  outcome_exit_reason

Measured the same day: **0 resolved out of 1,711**. `ai_decision_engine` reads
outcome_category and outcome_return_pct to tell the model how past signals fared
and has therefore always read NULL. Every prior the allocator is meant to be
built on, for the book that holds real money, had no producer at all.

This module is that producer. It is deliberately a near-copy of the intraday
one in shape — same idea, same verdict vocabulary, same re-runnability — because
two outcome resolvers that disagree about what "TARGET" means would poison the
comparison the allocator exists to make.

WHAT IS RESOLVED, AND HOW
-------------------------
A plan is a conditional order: buy inside the entry zone, stop below, target
above. So resolution walks forward through daily bars and answers, in order:

    was the entry zone ever touched within the trigger window?
        no  -> NOT_TRIGGERED. Recorded, not discarded — "the zone was never
               reached" is the most common thing that happens to a plan and a
               prior built only on triggered plans is biased toward the ones
               that moved.
    from the touch, which came first, target or stop?
        target -> TARGET      stop -> STOP      neither -> TIMEOUT

INTRABAR AMBIGUITY IS RESOLVED AGAINST THE PLAN.
When one bar's low reaches the stop AND its high reaches the target, daily bars
cannot say which came first. This calls it a STOP. That is pessimistic by
construction and it is the right direction: a prior that resolves ties in the
plan's favour reports an edge the account would never have realised.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from config import cfg_int, get_supabase, today_ist, fetch_all


PAGE = 1000

# Verdict vocabulary. Identical in meaning to intraday/outcomes so the two
# populations can be pooled by the scoring layer without a translation table.
TARGET, STOP, TIMEOUT, NOT_TRIGGERED = "TARGET", "STOP", "TIMEOUT", "NOT_TRIGGERED"


def _bars(sb, symbols: list[str], since: str) -> dict[str, list[dict]]:
    """Daily OHLC per symbol from `since`, oldest first."""
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 60):
        chunk = symbols[i:i + 60]
        off = 0
        while True:
            rows = (sb.table("stock_data_daily")
                      .select("symbol,date,high,low,close")
                      .in_("symbol", chunk).gte("date", since)
                      .order("date").range(off, off + PAGE - 1).execute().data) or []
            for r in rows:
                out.setdefault(r["symbol"], []).append(r)
            if len(rows) < PAGE:
                break
            off += PAGE
    return out


def resolve_plan(plan: dict, bars: list[dict], trigger_days: int,
                 hold_days: int) -> dict | None:
    """
    Walk one plan forward through daily bars. Pure — no I/O, so it can be
    replayed against a recorded session and cannot disagree between callers.

    Returns None when there are not enough forward bars to reach a verdict yet;
    the plan stays unresolved rather than being scored on a partial window.
    """
    lo   = plan.get("entry_zone_low")
    hi   = plan.get("entry_zone_high")
    stop = plan.get("planned_stop")
    tgt  = plan.get("planned_target")
    if lo is None or hi is None:
        return None
    lo, hi = float(lo), float(hi)

    # LEVELS ARE OPTIONAL, THE ZONE IS NOT.
    #
    # planned_stop and planned_target were only written from 28-Jul-2026 — 465
    # of 1,711 plans carry them. Refusing to resolve the other 1,246 would throw
    # away the half of the prior that does not need them: whether the entry zone
    # was reached at all, and what the plan returned over its horizon.
    #
    # That half matters more than it looks. The readiness review's S2 warns that
    # a plan's forward outcome assumes it COULD have been entered at its recorded
    # level, and that where a zone was never touched the assumption is untested —
    # so priors must be segmented by whether the entry level was reached rather
    # than pooled. Trigger rate is exactly that segmentation, and it is knowable
    # for every plan ever written.
    #
    # Without levels a plan can still be NOT_TRIGGERED or TIMEOUT with a real
    # forward return. It simply cannot be TARGET or STOP, and it is not labelled
    # as either.
    scoreable = stop is not None and tgt is not None
    if scoreable:
        stop, tgt = float(stop), float(tgt)
        if not (stop < hi and tgt > lo):
            scoreable = False                         # incoherent levels

    forward = [b for b in bars if str(b["date"]) > str(plan["date"])]
    if not forward:
        return None

    # ── Did the zone ever fill? ──────────────────────────────────────────────
    entry_i = entry_px = None
    for i, b in enumerate(forward[:trigger_days]):
        blo, bhi = float(b["low"] or 0), float(b["high"] or 0)
        if blo <= hi and bhi >= lo:                   # bar overlaps the zone
            # Filled at the zone high when the bar gapped past it, otherwise at
            # the zone itself. A limit inside the zone fills at the limit.
            entry_px = hi if blo > lo else min(hi, max(lo, blo))
            entry_i  = i
            break

    if entry_i is None:
        if len(forward) < trigger_days:
            return None                               # window still open
        return {
            "outcome_entered":     False,
            "outcome_category":    NOT_TRIGGERED,
            "outcome_return_pct":  None,
            "outcome_hold_days":   None,
            "outcome_entry_price": None,
            "outcome_exit_price":  None,
            "outcome_exit_reason": f"zone {lo:.2f}-{hi:.2f} never touched in "
                                   f"{trigger_days} sessions",
        }

    # ── From the fill, which came first? ─────────────────────────────────────
    window = forward[entry_i:entry_i + hold_days + 1]
    for n, b in enumerate(window):
        if not scoreable:
            break                                     # no levels to test against
        bhi, blo = float(b["high"] or 0), float(b["low"] or 0)
        hit_t, hit_s = bhi >= tgt, blo <= stop
        if hit_t or hit_s:
            # Both in one bar: daily data cannot order them. Resolve against
            # the plan — a prior that flatters itself here is worse than none.
            px, why = ((stop, "stop") if hit_s else (tgt, "target"))
            return {
                "outcome_entered":     True,
                "outcome_category":    STOP if hit_s else TARGET,
                "outcome_return_pct":  round((px - entry_px) / entry_px * 100, 3),
                "outcome_hold_days":   n,
                "outcome_entry_price": round(entry_px, 2),
                "outcome_exit_price":  round(px, 2),
                "outcome_exit_reason": (f"{why} hit on {b['date']}"
                                        + (" (same bar as target — resolved against "
                                           "the plan)" if hit_s and hit_t else "")),
            }

    if len(window) <= hold_days:
        return None                                   # not enough bars yet

    last = window[-1]
    px   = float(last["close"] or entry_px)
    return {
        "outcome_entered":     True,
        "outcome_category":    TIMEOUT,
        "outcome_return_pct":  round((px - entry_px) / entry_px * 100, 3),
        "outcome_hold_days":   len(window) - 1,
        "outcome_entry_price": round(entry_px, 2),
        "outcome_exit_price":  round(px, 2),
        "outcome_exit_reason": (f"neither level reached in {hold_days} sessions"
                                if scoreable else
                                f"no stop/target recorded on this plan — "
                                f"{hold_days}-session forward return only, "
                                f"NOT a target/stop verdict"),
    }


def resolve(sb, backfill: bool = False, limit_days: int = 400) -> dict:
    """Resolve every plan whose forward window has closed."""
    trigger_days = cfg_int("swing_outcome_trigger_days", 5)
    hold_days    = cfg_int("swing_outcome_hold_days", 15)

    since = str(today_ist() - timedelta(days=limit_days))
    q = (sb.table("signal_output_daily")
           # No `id` column on this table — the signal id lives in signal_log.
           # Rows are keyed on (date, symbol), which is what the update matches.
           .select("date,symbol,entry_zone_low,entry_zone_high,planned_stop,"
                   "planned_target,outcome_category")
           .gte("date", since).order("date"))
    if not backfill:
        q = q.is_("outcome_category", "null")

    plans, off = [], 0
    while True:
        page = q.range(off, off + PAGE - 1).execute().data or []
        plans += page
        if len(page) < PAGE:
            break
        off += PAGE

    if not plans:
        logger.info("  nothing to resolve")
        return {"scored": 0, "pending": 0}

    oldest  = min(str(p["date"]) for p in plans)
    symbols = sorted({p["symbol"] for p in plans})
    logger.info(f"  {len(plans)} plan(s) across {len(symbols)} symbol(s) since {oldest}")
    bars = _bars(sb, symbols, oldest)

    scored = pending = written = 0
    for p in plans:
        verdict = resolve_plan(p, bars.get(p["symbol"], []), trigger_days, hold_days)
        if verdict is None:
            pending += 1
            continue
        scored += 1
        # signal_output_daily has no `id` in some deployments — the snapshot is
        # keyed on (date, symbol). Match on both rather than assuming a surrogate.
        try:
            (sb.table("signal_output_daily").update(verdict)
               .eq("date", p["date"]).eq("symbol", p["symbol"]).execute())
            written += 1
        except Exception as e:
            logger.warning(f"  {p['symbol']} {p['date']}: {e}")

    logger.success(f"  resolved {scored}, wrote {written}, {pending} still inside "
                   f"their {hold_days}-session window")
    return {"scored": scored, "written": written, "pending": pending}


def status(sb) -> None:
    total = sb.table("signal_output_daily").select("date", count="exact").limit(1).execute().count
    done  = (sb.table("signal_output_daily").select("date", count="exact")
               .not_.is_("outcome_category", "null").limit(1).execute().count)
    logger.info(f"  signal_output_daily: {done} of {total} plans resolved "
                f"({100 * done / total if total else 0:.1f}%)")
    if not done:
        logger.warning("  the swing book's unbiased denominator is EMPTY — every prior "
                       "built from it would be fabricated. Run without --status.")
        return
    import collections
    # SORTED PAGING, (symbol, date) — this table has no `id` column. Read-only
    # status reporting; the resolution path above is untouched.
    rows = fetch_all(lambda: sb.table("signal_output_daily")
                     .select("outcome_category,outcome_return_pct,symbol,date")
                     .not_.is_("outcome_category", "null"),
                     page=PAGE, order_by="symbol,date")
    c = collections.Counter(r["outcome_category"] for r in rows)
    for k, n in c.most_common():
        rs = [float(r["outcome_return_pct"]) for r in rows
              if r["outcome_category"] == k and r["outcome_return_pct"] is not None]
        avg = f"avg {sum(rs)/len(rs):+.2f}%" if rs else ""
        logger.info(f"    {k:<15} {n:>5}  {avg}")


def main(backfill: bool = False, show: bool = False) -> dict:
    sb = get_supabase()
    if show:
        status(sb)
        return {}
    return resolve(sb, backfill=backfill)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Resolve every swing plan's forward outcome")
    ap.add_argument("--backfill", action="store_true", help="re-resolve already-scored plans")
    ap.add_argument("--status", action="store_true", help="how much of the field is scored")
    a = ap.parse_args()
    main(backfill=a.backfill, show=a.status)
