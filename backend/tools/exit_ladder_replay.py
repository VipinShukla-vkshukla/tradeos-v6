"""
What would the give-back guard have done to trades already closed? READ-ONLY.

    python -m tools.exit_ladder_replay --days 20

WHY THIS IS A DIFFERENT KIND OF BACKTEST FROM allocator_replay.py
-------------------------------------------------------------------
`allocator_replay.py` replays a DISCRETE DECISION (take/decline this proposal)
against a DISCRETE RECORDED OUTCOME (what that exact setup resolved to). There
is no path-dependency: the historical row already IS the answer for "what if
this policy had chosen it."

An exit-ladder change is not that. `closed_positions` stores only SUMMARY
excursion numbers — max_favorable_excursion (the single best price reached)
and the realised r_multiple (however the position actually closed) — not the
bar-by-bar price path. So this tool cannot re-run the state machine
(`evaluate_intraday_exit`) tick by tick against history; it can only ESTIMATE
what a different give-back threshold would have captured, using the peak and
the actual outcome as bounds.

THE ESTIMATE, PRECISELY
------------------------
For a position that peaked at `peak_r` (from MFE) and actually closed at
`actual_r` (from r_multiple):

    if peak_r < giveback_min_r:
        unaffected — the guard could never have reached its own floor
    elif actual_r >= peak_r * (1 - giveback_pct/100):
        unaffected — whatever DID close it already kept at least as much
        of the peak as the guard would have demanded
    else:
        the guard WOULD have fired before the real exit did, and is
        estimated to have captured peak_r * (1 - giveback_pct/100) —
        the threshold itself, since that is the earliest point at which
        it could have triggered

WHAT THIS ASSUMES, AND WHY THAT MATTERS
-----------------------------------------------
This is a LOWER-CONFIDENCE estimate, not a replay, for two named reasons:

  1. ORDER OF RUNGS. The real exit might have been session-end, invalidation
     or a trail that would ALSO have fired at or before the give-back
     threshold — this tool cannot know which rung would have won the race
     in real time, only that a give-back exit was AVAILABLE by that point.
     For a session that ended EXIT_SQUAREOFF with the peak already given
     back, give-back would very likely have pre-empted it; for one that hit
     STOP_LOSS_HIT after a modest peak, it might not have had time to fire
     first. The estimate does not distinguish these cases.

  2. PATH SHAPE. A position that whipsawed through the give-back threshold
     twice before finally reversing is treated identically to one that
     crossed it once and kept falling — the summary data cannot tell them
     apart.

Both biases point the SAME direction: this likely OVERSTATES how much the
guard would have helped, because it assumes it always wins any race with
another rung. Read the output as "the ceiling on what give-back could have
captured," not as a promise.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_float, today_ist

# IMPORTED, NOT REDEFINED — 10-Aug-2026. This module originally carried its
# own MIN_RISK_PCT = 0.15 copy. tools.exit_audit later grew a SECOND guard
# (MAX_PLAUSIBLE_R) after the first one turned out not to explain the actual
# corrupted rows on this account's real data — the near-zero-risk hypothesis
# was wrong for them. Redefining the constant here would have meant this
# tool silently kept running the superseded, incomplete guard while
# exit_audit moved on, which is exactly the "two definitions of one idea
# drift apart" failure this codebase has hit before (regime_bucket() against
# two vocabularies, the intraday prior's key prefix). One definition, in
# exit_audit, imported here.
from tools.exit_audit import MIN_RISK_PCT, MAX_PLAUSIBLE_R

PAGE = 1000


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(sb, since: str) -> list[dict]:
    out, off = [], 0
    while True:
        page = (sb.table("closed_positions")
                .select("symbol,entry_price,direction,r_multiple,"
                        "exit_reason,max_favorable_excursion,planned_stop_at_entry,"
                        "framework,entry_date")
                .eq("framework", "INTRADAY")
                .gte("entry_date", since)
                .range(off, off + PAGE - 1).execute().data) or []
        out += page
        if len(page) < PAGE:
            break
        off += PAGE
    return out


def replay(days: int, giveback_pct: float, giveback_min_r: float, sb=None) -> int:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = _rows(sb, since)
    if not rows:
        logger.error("  no closed INTRADAY positions in this window")
        return 1

    unaffected, affected, unmeasurable = [], [], 0
    for r in rows:
        entry = _f(r.get("entry_price"))
        stop = _f(r.get("planned_stop_at_entry"))
        actual_r = _f(r.get("r_multiple"))
        mfe = _f(r.get("max_favorable_excursion"))
        if not entry or actual_r is None:
            continue
        if not stop or not mfe:
            unmeasurable += 1
            continue
        risk = abs(entry - stop)
        if risk <= 0 or (risk / entry * 100.0) < MIN_RISK_PCT:
            unmeasurable += 1
            continue

        is_short = (r.get("direction") or "LONG").upper() == "SHORT"
        peak_r = ((entry - mfe) if is_short else (mfe - entry)) / risk
        # Same symptom-based net as tools.exit_audit — a plausible risk
        # distance is not sufficient on its own; see that module's guard for
        # why (the risk-only guard did not explain this account's real
        # corrupted rows).
        if abs(peak_r) > MAX_PLAUSIBLE_R:
            unmeasurable += 1
            continue

        rec = {"symbol": r.get("symbol"), "reason": r.get("exit_reason") or "?",
               "actual_r": actual_r, "peak_r": peak_r}

        if peak_r < giveback_min_r:
            rec["giveback_r"] = actual_r
            unaffected.append(rec)
            continue
        floor_r = peak_r * (1.0 - giveback_pct / 100.0)
        if actual_r >= floor_r:
            rec["giveback_r"] = actual_r
            unaffected.append(rec)
        else:
            rec["giveback_r"] = floor_r
            affected.append(rec)

    all_recs = unaffected + affected
    if not all_recs:
        logger.error(f"  0 of {len(rows)} rows had both a plausible stop and an "
                     f"MFE to estimate from ({unmeasurable} unmeasurable)")
        return 1

    old_total = sum(x["actual_r"] for x in all_recs)
    new_total = sum(x["giveback_r"] for x in all_recs)

    logger.info("")
    logger.info(f"  EXIT-LADDER ESTIMATE — give-back at {giveback_pct:.0f}% / "
                f"min {giveback_min_r:.1f}R, {len(all_recs)} measurable position(s) "
                f"({unmeasurable} unmeasurable, excluded)")
    logger.info("  " + "-" * 78)
    if affected:
        logger.info(f"  {len(affected)} position(s) WOULD HAVE been cut early by give-back:")
        logger.info(f"  {'symbol':<14}{'actual exit':<20}{'actual R':>10}"
                    f"{'peak R':>9}{'giveback R':>12}")
        for x in sorted(affected, key=lambda a: a["actual_r"] - a["giveback_r"]):
            logger.info(f"  {x['symbol'] or '?':<14}{x['reason']:<20}"
                        f"{x['actual_r']:>+10.3f}{x['peak_r']:>+9.2f}{x['giveback_r']:>+12.3f}")
    else:
        logger.info("  0 positions would have been affected by this threshold")
    logger.info("  " + "-" * 78)
    logger.info(f"  ACTUAL exits (as they really closed):   total {old_total:+.2f}R"
                f"  ({len(all_recs)} positions)")
    logger.info(f"  GIVE-BACK ceiling estimate:              total {new_total:+.2f}R"
                f"  ({len(affected)} would have changed)")
    logger.info(f"  Estimated ceiling on the improvement:    {new_total - old_total:+.2f}R")
    logger.info("  " + "-" * 78)
    logger.warning(
        "  This is a CEILING, not a forecast — see this tool's own module "
        "docstring. It assumes give-back always wins any race with another "
        "exit rung, which the summary data cannot rule out or confirm.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Estimate the give-back guard's effect on already-closed "
                    "INTRADAY positions (read-only, approximate — see docstring)")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--pct", type=float, default=None,
                    help="override intraday_giveback_pct for the test")
    ap.add_argument("--min-r", type=float, default=None,
                    help="override intraday_giveback_min_r for the test")
    a = ap.parse_args()
    pct = a.pct if a.pct is not None else cfg_float("intraday_giveback_pct", 50.0)
    min_r = a.min_r if a.min_r is not None else cfg_float("intraday_giveback_min_r", 1.0)
    sys.exit(replay(a.days, pct, min_r))
