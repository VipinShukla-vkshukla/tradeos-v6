"""
Score every allocation verdict — the taken, the deferred and the declined.

    python -m allocation.outcomes            resolve everything now due
    python -m allocation.outcomes --status   how much of the record is scored

WITHOUT THIS THE PROMOTION GATE CAN NEVER MOVE
-----------------------------------------------
`allocation_decisions.outcome_r` is what `tools/allocator_report` counts a
DISAGREEMENT on: a verdict with no outcome is tallied as "awaiting an outcome"
and never reaches the thirty the gate requires. Migration 041 created the
column; nothing wrote it. So the allocator could have shadowed for a year, the
row count would have climbed into the tens of thousands, and the scorecard would
have reported zero scored disagreements the entire time — the same silent-hole
failure as the plan outcomes that were NULL for 1,711 rows.

This is the producer.

THE ASYMMETRY THAT MAKES THIS HARD, STATED PLAINLY
---------------------------------------------------
A TAKEN proposal has a real outcome: the position opened, closed, and
closed_positions carries its r_multiple. That is fact.

A DECLINED or DEFERRED proposal has no fact. It has a counterfactual — what it
WOULD have returned had it been entered at the level it was refused at — and
that number is systematically generous, because it assumes a fill at the
decision price with no slippage and no partial. The policy that refuses more
benefits more from that generosity, and the allocator is the policy that refuses
more.

So counterfactuals are computed, marked, and haircut:

  · realised     from closed_positions.r_multiple. Ground truth.
  · counterfactual  forward path from stock_data_daily, target/stop first-touch,
                    resolved AGAINST the proposal on an ambiguous bar, then
                    haircut by alloc_shortfall_r.

`outcome_note` records which of the two it is, so no consumer can pool them by
accident.

WHY THE HAIRCUT IS A CONFIG KEY AND NOT A MEASUREMENT
------------------------------------------------------
M1 in the readiness review nominated the order path for execution-shortfall
instrumentation — decision price versus realised fill — and Phase 4 did not
build it. Until it exists the haircut is an assumption, not a measurement, and
it is exposed as `alloc_shortfall_r` so it is visible as one rather than buried
as a constant. Set from the intraday cost model's own slippage assumption (5bps
on a ~1% risk trade is ~0.05R) and deliberately rounded UP, because an
optimistic haircut is the failure mode that promotes the allocator wrongly.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float, cfg_int, get_supabase, today_ist, fetch_all

PAGE = 1000

REALISED = "realised"
COUNTERFACTUAL = "counterfactual"
NOT_TRIGGERED = "counterfactual: entry never reached"


def _bars(sb, symbols: list[str], since: str) -> dict[str, list[dict]]:
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


def _realised(sb, since: str) -> dict[tuple, float]:
    """(symbol, product, entry_date) -> r_multiple, for positions that closed."""
    # SORTED PAGING. `closed_positions` is 148 rows today, so this reads in one
    # page and the sort changes nothing NOW — which is exactly why it was easy
    # to leave. It is a live book that only grows, and the day it crosses 1000
    # an unsorted pager starts returning the right count built from the wrong
    # rows, silently, in the map the allocator scores realised R from.
    out: dict[tuple, float] = {}
    rows = fetch_all(lambda: sb.table("closed_positions")
                     .select("symbol,product,entry_date,r_multiple,id")
                     .gte("entry_date", since), page=PAGE)
    for r in rows:
        if r.get("r_multiple") is None:
            continue
        out[(r["symbol"], (r.get("product") or "CNC").upper(),
             str(r["entry_date"])[:10])] = float(r["r_multiple"])
    return out


def counterfactual_r(row: dict, bars: list[dict], hold_days: int) -> tuple[float, str] | None:
    """
    What this refused proposal would have returned. Pure — no I/O.

    Same first-touch walk the swing plan resolver uses, and the same tie-break:
    when one bar reaches BOTH the target and the stop, daily data cannot order
    them and it resolves as the STOP. Pessimistic by construction, which is the
    only safe direction for a number that decides whether to trust a policy.
    """
    try:
        entry, stop, tgt = float(row["entry"]), float(row["stop"]), float(row["target"])
    except (TypeError, ValueError, KeyError):
        return None
    if not (0 < stop < entry < tgt):
        return None
    risk = entry - stop

    fwd = [b for b in bars if str(b["date"])[:10] > str(row["trade_date"])[:10]]
    if not fwd:
        return None

    # Did price ever reach the entry level it was refused at? If not, the
    # proposal is not merely a loser — it never existed as a trade, and scoring
    # it as one would punish the allocator for declining something untradeable.
    entered = None
    for i, b in enumerate(fwd[:hold_days]):
        if float(b["low"] or 0) <= entry <= float(b["high"] or 0):
            entered = i
            break
    if entered is None:
        if len(fwd) < hold_days:
            return None                      # window still open
        return 0.0, NOT_TRIGGERED

    for b in fwd[entered:entered + hold_days + 1]:
        hi, lo = float(b["high"] or 0), float(b["low"] or 0)
        hit_t, hit_s = hi >= tgt, lo <= stop
        if hit_s:
            return -1.0, COUNTERFACTUAL
        if hit_t:
            return (tgt - entry) / risk, COUNTERFACTUAL

    window = fwd[entered:entered + hold_days + 1]
    if len(window) <= hold_days:
        return None                          # not enough bars yet
    close = float(window[-1]["close"] or entry)
    return (close - entry) / risk, COUNTERFACTUAL


def resolve(sb, backfill: bool = False, limit_days: int = 120) -> dict:
    hold = cfg_int("alloc_counterfactual_hold_days", 15)
    haircut = cfg_float("alloc_shortfall_r", 0.05)
    since = (today_ist() - timedelta(days=limit_days)).isoformat()

    q = (sb.table("allocation_decisions")
           .select("id,trade_date,symbol,product,framework,verdict,entry,stop,target,outcome_r")
           .gte("trade_date", since).order("trade_date"))
    if not backfill:
        q = q.is_("outcome_r", "null")

    rows, off = [], 0
    while True:
        page = q.range(off, off + PAGE - 1).execute().data or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    if not rows:
        logger.info("  nothing to resolve")
        return {"scored": 0, "pending": 0}

    realised = _realised(sb, min(str(r["trade_date"])[:10] for r in rows))
    bars = _bars(sb, sorted({r["symbol"] for r in rows}),
                 min(str(r["trade_date"])[:10] for r in rows))

    scored = pending = written = 0
    for r in rows:
        key = (r["symbol"], (r.get("product") or "CNC").upper(), str(r["trade_date"])[:10])
        if key in realised:
            # The trade actually happened. Ground truth, no haircut — a haircut
            # on a realised fill would be double-counting a cost already in it.
            out_r, note = realised[key], REALISED
        else:
            cf = counterfactual_r(r, bars.get(r["symbol"], []), hold)
            if cf is None:
                pending += 1
                continue
            out_r, note = cf
            if note == COUNTERFACTUAL:
                # Haircut applied toward zero, never past it: a losing
                # counterfactual must not be made to look better by slippage.
                out_r = out_r - haircut if out_r > 0 else out_r
                note = f"{COUNTERFACTUAL} (haircut {haircut:.2f}R, UNMEASURED — see M1)"
        scored += 1
        try:
            (sb.table("allocation_decisions")
               .update({"outcome_r": round(out_r, 4), "outcome_note": note})
               .eq("id", r["id"]).execute())
            written += 1
        except Exception as e:
            logger.warning(f"  {r['symbol']} #{r['id']}: {e}")

    logger.success(f"  resolved {scored}, wrote {written}, {pending} still inside "
                   f"their {hold}-session window")
    return {"scored": scored, "written": written, "pending": pending}


def status(sb) -> None:
    total = sb.table("allocation_decisions").select("id", count="exact").limit(1).execute().count
    done = (sb.table("allocation_decisions").select("id", count="exact")
              .not_.is_("outcome_r", "null").limit(1).execute().count)
    logger.info(f"  allocation_decisions: {done} of {total} scored "
                f"({100 * done / total if total else 0:.1f}%)")
    if not total:
        logger.warning("  nothing recorded yet — the allocator writes on the daemon's "
                       "300s flush, so this stays empty until a session runs")


def main(backfill: bool = False, show: bool = False) -> dict:
    sb = get_supabase()
    if show:
        status(sb)
        return {}
    return resolve(sb, backfill=backfill)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Score every allocation verdict")
    ap.add_argument("--backfill", action="store_true", help="re-resolve already-scored rows")
    ap.add_argument("--status", action="store_true", help="how much of the record is scored")
    a = ap.parse_args()
    main(backfill=a.backfill, show=a.status)
