"""
Does cost_verdict='TAKEN' actually mean a position opened? READ-ONLY.

    python -m tools.taken_reconciliation --days 10

WHY THIS EXISTS — 10-Aug-2026
------------------------------
The operator's own dashboard showed the real intraday book net PROFITABLE
over its 40 closed trades (+Rs 141.80, profit factor 1.55, since automation).
`tools/exit_audit.py`'s own numbers AGREE: 14 winners avg +1.327R and 26
losers avg -0.687R blend to +0.018R average across those same 40 real trades
— small, but positive, and untouched by any of the bugs found this session
(r_multiple is written directly by the live system).

`tools/engine_scorecard.py` reported the book gross -0.138R over the SAME
window and was read as "no edge, correctly refused." That reading was wrong
because the two tools measure DIFFERENT POPULATIONS: engine_scorecard counts
every `intraday_setups` row with `cost_verdict='TAKEN'` (119 deduplicated
rows across 10 sessions) — but the CLOSED book only has 40 trades. 79 "TAKEN"
detections never became a real position, and whatever those 79 rows resolved
to is being counted as if it were a real trade the system executed. THAT
gap — not a lack of edge — is the most likely explanation for the
discrepancy, and it deserves its own measurement before either number is
trusted for a lifecycle or floor decision.

WHY A "TAKEN" ROW CAN EXIST WITH NO POSITION BEHIND IT
--------------------------------------------------------
`cost_verdict='TAKEN'` is written the moment a setup clears the COST gate
(`is_worth_taking`), inside `evaluate_intraday_setups()` — before the
allocator veto or `_maybe_open_paper()`'s own capacity checks ever run. Three
separate downstream gates can still refuse the trade after that row exists:
  - the allocator's veto (`allocator_permits`)
  - `intraday_max_concurrent` / `intraday_max_new_per_day` capacity
  - `paper_broker.capacity()`'s own refusal
None of those write a new row over the TAKEN one unless the SAME setup is
re-evaluated later with a DIFFERENT verdict — a setup taken once and never
re-seen (the common case once a position opens or the setup expires) leaves
its TAKEN row exactly as first written, whether or not a position followed.

WHAT THIS TOOL DOES
--------------------
For each session, counts distinct (symbol, engine) TAKEN detections in
`intraday_setups`, and separately counts distinct INTRADAY symbols that
actually appear in `open_positions` or `closed_positions` with a matching
entry date. The gap between the two is reported per session — this is a
COARSE reconciliation (symbol + day, not a strict foreign key, because none
exists between these tables) but is sufficient to answer the one question
that matters here: is the scorecard's population close to the real book's, or
badly inflated by phantom "taken" detections that were never actually traded.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, today_ist, fetch_all

PAGE = 1000


def _rows(sb, table: str, cols: str, date_col: str, since: str, extra=None,
          order_by: str = "id") -> list[dict]:
    # SORTED PAGING. One caller passes `intraday_setups` — 8,324 rows, nine
    # pages — so this is the same defect F-23 catalogued, hidden behind a
    # table-name PARAMETER where neither F-23's `git grep 'table("intraday_
    # setups")'` census nor the literal-name static check could see it.
    def _build():
        q = sb.table(table).select(cols).gte(date_col, since)
        for k, v in (extra or {}).items():
            q = q.eq(k, v)
        return q
    return fetch_all(_build, page=PAGE, order_by=order_by)


def reconcile(days: int, sb=None) -> int:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()

    all_setups = _rows(sb, "intraday_setups",
                       "trade_date,symbol,strategy,cost_verdict",
                       "trade_date", since)
    taken = [r for r in all_setups if (r.get("cost_verdict") or "").upper() == "TAKEN"]

    # THREE BUCKETS, NOT TWO — 10-Aug-2026, after engine.py's fix. A
    # "TAKEN but never opened" setup can now have a LATER row explaining it in
    # one of two DIFFERENT ways, plus a third bucket for a genuine gap in the
    # trail:
    #   ALLOCATOR_DECLINED   the allocator vetoed it — opportunity cost, by
    #                        design, and its hypothetical outcome belongs in
    #                        the prior (excluding it would be circular: a
    #                        prior conditioned only on allocator-approved
    #                        rows reinforces whatever it already believes).
    #   BLOCKED_*            an OPERATIONAL reason downstream of the
    #                        allocator — concurrency, daily budget, paper
    #                        capacity, an already-held race, a failed fill,
    #                        or a bare exception. `_maybe_open_paper` now
    #                        records every one of these explicitly; before
    #                        this fix they were exactly this tool's original
    #                        27-of-60 "unexplained" reading.
    #                        Excluded from the prior on purpose (see
    #                        scoring.py) is NOT what happens here — these
    #                        rows are hypothetically resolved the same as any
    #                        other, which is correct: the setup itself was
    #                        never wrong, only unlucky on capacity.
    #   (neither)            still genuinely unexplained. After this fix that
    #                        should trend toward zero; anything remaining
    #                        here is either older data from before the fix
    #                        shipped, or a path this fix still missed.
    by_key: dict[tuple, str] = {}
    for r in all_setups:
        v = (r.get("cost_verdict") or "").upper()
        if v in ("TAKEN", "SHADOW"):
            continue
        by_key[(r.get("symbol"), r.get("strategy"),
               str(r.get("trade_date") or "")[:10])] = v

    opened_syms: dict[str, set] = defaultdict(set)
    for table, date_col in (("open_positions", "entry_date"),
                            ("closed_positions", "entry_date")):
        for r in _rows(sb, table, f"symbol,{date_col},framework", date_col, since,
                       extra={"framework": "INTRADAY"}):
            d = str(r.get(date_col) or "")[:10]
            if d and r.get("symbol"):
                opened_syms[d].add(r["symbol"])

    by_day: dict[str, set] = defaultdict(set)
    for r in taken:
        d = str(r.get("trade_date") or "")[:10]
        if d and r.get("symbol"):
            by_day[d].add((r["symbol"], r.get("strategy")))

    if not by_day:
        logger.error("  no TAKEN detections in this window")
        return 1

    logger.info("")
    logger.info("  TAKEN-vs-OPENED RECONCILIATION")
    logger.info("  " + "-" * 70)
    logger.info(f"  {'date':<12}{'taken symbols':>15}{'real positions':>16}{'gap':>8}   ratio")
    logger.info("  " + "-" * 70)

    total_taken, total_opened = 0, 0
    for d in sorted(by_day):
        taken_syms = {sym for sym, _eng in by_day[d]}
        n_taken, n_opened = len(taken_syms), len(opened_syms.get(d, set()))
        gap = n_taken - len(taken_syms & opened_syms.get(d, set()))
        ratio = (len(taken_syms & opened_syms.get(d, set())) / n_taken * 100) if n_taken else 0
        total_taken += n_taken
        total_opened += len(taken_syms & opened_syms.get(d, set()))
        logger.info(f"  {d:<12}{n_taken:>15}{n_opened:>16}{gap:>8}   {ratio:.0f}% became a position")

    logger.info("  " + "-" * 70)
    overall = (total_opened / total_taken * 100) if total_taken else 0
    logger.info(f"  TOTAL: {total_taken} distinct TAKEN symbol-days, "
               f"{total_opened} matched to a real position ({overall:.0f}%)")

    # Reported in three lines because the correct response to each is
    # different: a large ALLOCATOR_DECLINED count says "the prior is working
    # as intended, look elsewhere"; a large BLOCKED_* count says "there is an
    # operational leak, and now it has a name"; anything still with neither
    # is the residue this fix did not explain.
    unmatched = [(sym, eng, d) for d in by_day for sym, eng in by_day[d]
                if sym not in opened_syms.get(d, set())]
    verdict_counts: dict[str, int] = defaultdict(int)
    still_unexplained = 0
    for k in unmatched:
        v = by_key.get(k)
        if v:
            verdict_counts[v] += 1
        else:
            still_unexplained += 1

    allocator_n = verdict_counts.get("ALLOCATOR_DECLINED", 0)
    blocked_n = sum(n for v, n in verdict_counts.items() if v != "ALLOCATOR_DECLINED")

    logger.info("")
    logger.info(f"  Of the {len(unmatched)} that did not open:")
    logger.info(f"    {allocator_n:>4}  ALLOCATOR_DECLINED — the allocator vetoed them, by design")
    if blocked_n:
        for v, n in sorted(verdict_counts.items(), key=lambda kv: -kv[1]):
            if v != "ALLOCATOR_DECLINED":
                logger.info(f"    {n:>4}  {v}")
    logger.info(f"    {still_unexplained:>4}  still genuinely unexplained "
               f"(no verdict row at all beyond TAKEN)")

    if overall < 60:
        if still_unexplained > allocator_n + blocked_n:
            logger.warning(
                f"  Most of the gap ({still_unexplained} of {len(unmatched)}) "
                f"still has no verdict row at all. Either this data predates "
                f"the BLOCKED_* fix in intraday/engine.py, or a path this fix "
                f"missed still exists — check the daemon is running the "
                f"current code before assuming the latter.")
        elif blocked_n > allocator_n:
            logger.warning(
                f"  The gap is DOMINATED BY OPERATIONAL blocks ({blocked_n} "
                f"of {len(unmatched)}), not allocator vetoes — see the "
                f"breakdown above for which one. These are opportunities the "
                f"system's own gates liked, lost for reasons unrelated to "
                f"trade quality; each BLOCKED_* reason above names exactly "
                f"which control to look at.")
        else:
            logger.warning(
                f"  The gap is MOSTLY explained by allocator vetoes "
                f"({allocator_n} of {len(unmatched)}) — the allocator is "
                f"doing its job. This does NOT by itself mean the prior "
                f"population is wrong: including allocator-vetoed rows' "
                f"hypothetical outcomes is the non-circular choice. If "
                f"engine_scorecard still disagrees with the real book after "
                f"this, the next question is whether the allocator's OWN "
                f"vetoes are well-calibrated, not whether the prior's "
                f"population is.")
    else:
        logger.success(
            f"  {overall:.0f}% of TAKEN detections became a real position — "
            f"the gap is not large enough to explain a scorecard/dashboard "
            f"disagreement on its own.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Reconcile intraday_setups TAKEN rows against real "
                    "open/closed INTRADAY positions (read-only)")
    ap.add_argument("--days", type=int, default=10)
    sys.exit(reconcile(ap.parse_args().days))
