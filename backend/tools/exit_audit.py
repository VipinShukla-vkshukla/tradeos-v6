"""
Are we cutting winners early and holding losers? READ-ONLY.

    python -m tools.exit_audit                # both books
    python -m tools.exit_audit --book SWING
    python -m tools.exit_audit --open-only    # just the live stop-breach check

WHY THIS EXISTS
---------------
Two operator observations, 10-Aug-2026, which are the same question from both
ends:

  (a) GABRIEL sat at -5.47% from entry and was still held. Either its stop is
      wider than that and this is an ordinary drawdown, or the stop should have
      fired and did not. NOTHING IN THE SYSTEM CURRENTLY DISTINGUISHES THOSE
      TWO CASES from the outside, which is why it reads as a failure either
      way.
  (b) Realised profits are small even on the paper intraday book, while losers
      appear to run.

An exit ladder is judged on ASYMMETRY, not on either half alone. Cutting a
winner at +0.4R is only a mistake if the trade went to +2R afterwards, and that
is knowable: `closed_positions` carries max_favorable_excursion and
max_adverse_excursion alongside the realised r_multiple, so "how much of the
available move did we keep" is a measurable number and not a matter of opinion.

    capture   = realised R / MFE in R      1.0 = took the whole move
                                           0.3 = gave back 70% of it
    overrun   = MAE in R beyond -1.0       0.0 = the stop held
                                           0.4 = bled 40% of a further R past it

THE THIRD NUMBER, AND THE ONE THAT ANSWERS (a)
-----------------------------------------------
For OPEN positions, an unrealised loss deeper than the planned stop means the
stop did not fire. That is not a drawdown, it is a broken exit, and it is worth
its own loud line rather than being averaged into a statistic.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase

PAGE = 1000


def _rows(sb, table: str, cols: str) -> list[dict]:
    out, off = [], 0
    while True:
        page = (sb.table(table).select(cols)
                .range(off, off + PAGE - 1).execute().data) or []
        out += page
        if len(page) < PAGE:
            break
        off += PAGE
    return out


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def check_open_stops(sb) -> int:
    """(a) — is any OPEN position already past the stop it was given?"""
    rows = _rows(sb, "open_positions",
                 "symbol,framework,product,direction,entry_price,current_price,"
                 "planned_stop,active_sl,status,current_qty,actual_qty,unrealized_pnl")
    live = [r for r in rows if (r.get("status") or "").upper() == "ACTIVE"]
    if not live:
        logger.info("  no ACTIVE positions")
        return 0

    logger.info("")
    logger.info("  OPEN POSITIONS — is price already past the stop?")
    logger.info("  " + "-" * 86)
    logger.info(f"  {'symbol':<14}{'book':<10}{'entry':>10}{'now':>10}"
                f"{'stop':>10}{'P&L%':>9}{'to stop':>10}   state")
    logger.info("  " + "-" * 86)

    breached = 0
    for r in sorted(live, key=lambda x: x.get("symbol") or ""):
        entry = _f(r.get("entry_price"))
        now = _f(r.get("current_price"))
        # active_sl is the CURRENT stop (it moves with trails); planned_stop is
        # where it started. A breach is judged against whichever is in force.
        stop = _f(r.get("active_sl")) or _f(r.get("planned_stop"))
        if not entry or not now:
            continue
        is_short = (r.get("direction") or "LONG").upper() == "SHORT"
        pnl_pct = ((entry - now) if is_short else (now - entry)) / entry * 100.0

        if stop:
            # Distance to the stop, signed so negative always means "past it".
            to_stop = ((stop - now) if is_short else (now - stop)) / entry * 100.0
            if to_stop < 0:
                state, breached = "STOP BREACHED — exit did not fire", breached + 1
            else:
                state = "ok"
            stop_s, to_s = f"{stop:>10.2f}", f"{to_stop:>9.2f}%"
        else:
            state = "NO STOP RECORDED — nothing can fire"
            breached += 1
            stop_s, to_s = f"{'—':>10}", f"{'—':>10}"

        logger.info(f"  {r.get('symbol') or '?':<14}"
                    f"{(r.get('framework') or 'SWING'):<10}"
                    f"{entry:>10.2f}{now:>10.2f}{stop_s}{pnl_pct:>8.2f}%{to_s}   {state}")
    logger.info("  " + "-" * 86)

    if breached:
        logger.error(
            f"  {breached} position(s) are past their stop or have none recorded. "
            f"A stop that does not fire is not a wide stop, it is an absent one — "
            f"check the GTT actually rests at that price (tools.health 'broker') "
            f"and that evaluate_exit is being reached for this book.")
    else:
        logger.success("  every open position is still on the correct side of its stop "
                       "— drawdowns here are ordinary, not failed exits")
    return breached


def audit_closed(sb, book: str | None) -> None:
    """(b) — capture on winners, overrun on losers, split by exit reason."""
    rows = _rows(sb, "closed_positions",
                 "symbol,framework,direction,entry_price,exit_price,r_multiple,"
                 "exit_reason,max_favorable_excursion,max_adverse_excursion,"
                 "planned_stop_at_entry,planned_target_at_entry,realized_pnl,charges")
    if book:
        rows = [r for r in rows if (r.get("framework") or "SWING").upper() == book.upper()]
    if not rows:
        logger.warning(f"  no closed positions{' for ' + book if book else ''}")
        return

    # ── A DEGENERATE RISK DENOMINATOR MUST NOT BE AVERAGED IN — 10-Aug-2026 ──
    #
    # `risk = abs(entry - planned_stop_at_entry)`, and dividing a real price
    # swing by a near-zero risk produces an R number in the hundreds. First
    # live run of this tool: SWING's 7 BROKER_EXIT rows averaged MFE -20.228R,
    # and several INTRADAY exit reasons showed |MFE R| in the hundreds
    # (-143.636, +164.433, -109.340) — none of those are physically reachable
    # in a session against a real stop, and every one of them shares the same
    # signature: a `planned_stop_at_entry` sitting implausibly close to entry.
    #
    # BROKER_EXIT is the tell for the SWING case: a position reconciled from
    # Kite holdings rather than opened through decide()'s own planning path
    # never had a real stop computed for it, so the column likely holds a
    # placeholder. Whatever the exact cause on the intraday side, the fix is
    # the same either way — a stop distance under MIN_RISK_PCT of entry is not
    # a real stop for either book (this account's tightest MEASURED intraday
    # stop across all seven engines is 0.43%, per tools.engine_scorecard), so
    # anything tighter is data, not risk, and must be excluded from the
    # capture/overrun arithmetic rather than silently blended into the mean —
    # exactly the "check that cannot fail" failure shape this project keeps
    # finding, one level up: a STATISTIC that cannot be wrong is not one.
    #
    # Excluded rows are counted and reported, not dropped silently. AND ONLY
    # THE EXCURSION FIELDS ARE EXCLUDED — `r_multiple` is not recomputed here;
    # it is read as stored, and `_upsert_position`/`_close_position` derive it
    # from `pos["planned_stop"]`, the position's CURRENT (possibly trailed)
    # stop at close time, which is a DIFFERENT column from
    # `planned_stop_at_entry` and can be sound even when the entry-time column
    # is not. Dropping a row's realised R because its entry-stop snapshot is
    # bad would throw away good data for a reason that never touched it.
    MIN_RISK_PCT = 0.15

    recs, excluded = [], 0
    for r in rows:
        entry = _f(r.get("entry_price"))
        realised = _f(r.get("r_multiple"))
        if not entry or realised is None:
            continue
        stop = _f(r.get("planned_stop_at_entry"))
        mfe_r = mae_r = None
        if stop:
            risk = abs(entry - stop)
            if risk > 0 and (risk / entry * 100.0) >= MIN_RISK_PCT:
                is_short = (r.get("direction") or "LONG").upper() == "SHORT"
                mfe = _f(r.get("max_favorable_excursion"))
                mae = _f(r.get("max_adverse_excursion"))
                # Excursions are stored as PRICES. Convert to R in the trade's
                # own favour, so a short that fell is positive.
                mfe_r = ((entry - mfe) if is_short else (mfe - entry)) / risk if mfe else None
                mae_r = ((mae - entry) if is_short else (entry - mae)) / risk if mae else None
            else:
                excluded += 1
        recs.append({"fw": (r.get("framework") or "SWING").upper(),
                     "reason": r.get("exit_reason") or "?",
                     "r": realised, "mfe_r": mfe_r, "mae_r": mae_r})

    if excluded:
        logger.warning(
            f"  {excluded} closed trade(s) have planned_stop_at_entry under "
            f"{MIN_RISK_PCT}% of entry price — not a real stop for either book, "
            f"and dividing a genuine price swing by it would produce an R number "
            f"in the hundreds (measured on the first run of this tool: SWING's "
            f"BROKER_EXIT rows averaged MFE -20.228R; several INTRADAY exit "
            f"reasons showed |MFE R| over 100). realised R is kept for these "
            f"rows — it comes from a different column, computed at close time "
            f"— only their capture/overrun contribution is dropped.")

    if not recs:
        logger.warning("  closed positions exist but none carry both a planned stop "
                       "and an r_multiple — nothing measurable")
        return

    wins = [x for x in recs if x["r"] > 0]
    losses = [x for x in recs if x["r"] <= 0]
    cap = [x["r"] / x["mfe_r"] for x in wins if x["mfe_r"] and x["mfe_r"] > 0]
    # How far a LOSER ran in your favour before turning — the give-back guard's
    # whole reason to exist.
    lost_gift = [x["mfe_r"] for x in losses if x["mfe_r"] is not None]
    overrun = [max(0.0, -x["r"] - 1.0) for x in losses]

    logger.info("")
    logger.info(f"  CLOSED TRADES{' — ' + book if book else ''}: {len(recs)} with "
                f"a stop and an R on record")
    logger.info("  " + "-" * 78)
    logger.info(f"    winners            {len(wins):>4}   avg {statistics.fmean([x['r'] for x in wins]):+.3f}R"
                if wins else "    winners               0")
    logger.info(f"    losers             {len(losses):>4}   avg {statistics.fmean([x['r'] for x in losses]):+.3f}R"
                if losses else "    losers                0")
    if cap:
        logger.info(f"    capture on winners        {statistics.fmean(cap):.0%} of the move "
                    f"that was available (n={len(cap)})")
    if lost_gift:
        pos = [g for g in lost_gift if g > 0.3]
        logger.info(f"    losers that were up >0.3R first   {len(pos)} of {len(lost_gift)}"
                    + (f", peaking at {statistics.fmean(pos):+.2f}R" if pos else ""))
    if overrun:
        bad = [o for o in overrun if o > 0.1]
        logger.info(f"    losers worse than -1R      {len(bad)} of {len(losses)}"
                    + (f", by {statistics.fmean(bad):.2f}R on average" if bad else ""))
    logger.info("  " + "-" * 78)

    by: dict[str, list[dict]] = defaultdict(list)
    for x in recs:
        by[x["reason"]].append(x)
    logger.info(f"  {'exit reason':<24}{'n':>5}{'avg R':>9}{'avg MFE R':>11}{'capture':>10}")
    logger.info("  " + "-" * 78)
    for reason, xs in sorted(by.items(), key=lambda kv: -statistics.fmean(x["r"] for x in kv[1])):
        mfes = [x["mfe_r"] for x in xs if x["mfe_r"] is not None]
        c = [x["r"] / x["mfe_r"] for x in xs if x["mfe_r"] and x["mfe_r"] > 0]
        logger.info(f"  {reason:<24}{len(xs):>5}"
                    f"{statistics.fmean(x['r'] for x in xs):>+9.3f}"
                    f"{(statistics.fmean(mfes) if mfes else 0):>+11.3f}"
                    f"{(f'{statistics.fmean(c):.0%}' if c else '—'):>10}")
    logger.info("  " + "-" * 78)

    logger.info("")
    if cap and statistics.fmean(cap) < 0.5:
        logger.warning(
            f"  WINNERS ARE BEING CUT: only {statistics.fmean(cap):.0%} of the move "
            f"that was actually available is being kept. The rungs to look at are "
            f"the partial book (intraday_partial_book_r), the trail distance "
            f"(intraday_trail_r / exit_runner_trail_r) and the invalidation exit — "
            f"whichever exit_reason above shows a high avg MFE R against a low "
            f"avg R is the one giving the move back.")
    if overrun and statistics.fmean(overrun) > 0.15:
        logger.warning(
            f"  LOSERS ARE RUNNING PAST THE STOP by {statistics.fmean(overrun):.2f}R "
            f"on average. On a book with a working stop this number is ~0.")
    if lost_gift and len([g for g in lost_gift if g > 1.0]) > len(lost_gift) * 0.3:
        logger.warning(
            "  Over 30% of losing trades were up more than a full R before turning. "
            "That is the give-back guard's exact case, and intraday_giveback_pct "
            "is currently 0 (disabled).")


def main(book: str | None, open_only: bool) -> int:
    sb = get_supabase()
    check_open_stops(sb)
    if not open_only:
        for b in ([book] if book else ["SWING", "INTRADAY"]):
            audit_closed(sb, b)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exit-ladder efficiency audit (read-only)")
    ap.add_argument("--book", choices=["SWING", "INTRADAY"])
    ap.add_argument("--open-only", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.book, a.open_only))
