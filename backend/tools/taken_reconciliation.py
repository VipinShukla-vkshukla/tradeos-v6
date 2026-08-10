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
from config import get_supabase, today_ist

PAGE = 1000


def _rows(sb, table: str, cols: str, date_col: str, since: str, extra=None) -> list[dict]:
    out, off = [], 0
    while True:
        q = sb.table(table).select(cols).gte(date_col, since)
        if extra:
            for k, v in extra.items():
                q = q.eq(k, v)
        page = (q.range(off, off + PAGE - 1).execute().data) or []
        out += page
        if len(page) < PAGE:
            break
        off += PAGE
    return out


def reconcile(days: int, sb=None) -> int:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=days)).isoformat()

    all_setups = _rows(sb, "intraday_setups",
                       "trade_date,symbol,strategy,cost_verdict",
                       "trade_date", since)
    taken = [r for r in all_setups if (r.get("cost_verdict") or "").upper() == "TAKEN"]
    declined = {(r.get("symbol"), r.get("strategy"), str(r.get("trade_date") or "")[:10])
               for r in all_setups
               if (r.get("cost_verdict") or "").upper() == "ALLOCATOR_DECLINED"}

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

    # ── THE GAP HAS TWO POSSIBLE EXPLANATIONS, AND THEY MEAN OPPOSITE
    # THINGS — 10-Aug-2026, second pass on this same reconciliation. ────────
    #
    # A "TAKEN but never opened" setup either:
    #   (a) has a LATER row for the same (symbol, engine, day) with
    #       cost_verdict='ALLOCATOR_DECLINED' — the allocator saw it and
    #       vetoed it. That is BY DESIGN, not a defect: it is the allocator
    #       doing its job, and its hypothetical resolved outcome belongs in
    #       the prior population precisely because a prior conditioned only
    #       on rows the allocator already approved would be circular —
    #       reinforcing whatever it already believed rather than measuring
    #       against the full field the cost gate actually produced.
    #   (b) has NO such row — it cleared the cost gate and then simply never
    #       became a position, for a reason this table cannot see:
    #       intraday_max_concurrent/intraday_max_new_per_day capacity,
    #       paper_broker.capacity() refusing, a cross-framework hold, or an
    #       exception swallowed by _maybe_open_paper's own try/except. That
    #       IS worth investigating — these are opportunities the system's
    #       own gates liked that were lost for operational reasons having
    #       nothing to do with the trade's quality.
    #
    # Reported separately because the correct response to a large (a) is "the
    # prior is working as intended, look elsewhere" and the correct response
    # to a large (b) is "find out why paper entries are being dropped."
    unmatched = [(sym, eng, d) for d in by_day for sym, eng in by_day[d]
                if sym not in opened_syms.get(d, set())]
    explained = sum(1 for k in unmatched if k in declined)
    unexplained = len(unmatched) - explained

    logger.info("")
    logger.info(f"  Of the {len(unmatched)} that did not open: "
               f"{explained} have a matching ALLOCATOR_DECLINED row (the "
               f"allocator vetoed them — by design), {unexplained} have "
               f"NEITHER a position NOR an ALLOCATOR_DECLINED row (unexplained "
               f"by this table alone).")

    if overall < 60:
        if unexplained > explained:
            logger.warning(
                f"  The gap is DOMINATED BY UNEXPLAINED losses ({unexplained} "
                f"of {len(unmatched)}), not allocator vetoes. This points at "
                f"an OPERATIONAL leak — capacity limits, paper_broker "
                f"refusals, or a silently swallowed exception in "
                f"_maybe_open_paper — not at the prior's population choice. "
                f"Worth checking intraday_max_concurrent / "
                f"intraday_max_new_per_day against how many positions were "
                f"actually open on the worst days, and the daemon log for "
                f"'paper skip' or 'paper entry failed' lines.")
        else:
            logger.warning(
                f"  The gap is MOSTLY explained by allocator vetoes "
                f"({explained} of {len(unmatched)}) — the allocator is doing "
                f"its job. This does NOT by itself mean the prior population "
                f"is wrong: including allocator-vetoed rows' hypothetical "
                f"outcomes is the non-circular choice. If engine_scorecard "
                f"still disagrees with the real book after this, the next "
                f"question is whether the allocator's OWN vetoes are "
                f"well-calibrated, not whether the prior's population is.")
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
