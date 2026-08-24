"""
Stage D5 (docs/TRADEOS_ROADMAP.md, Track D) — Stage 1, CALIBRATION ONLY.

    python -m tools.same_day_calibration            # run and report
    python -m tools.same_day_calibration --min-n 3  # override the floor

Walks every trading day `intraday_setups` has resolved history for and
asks, per engine: using ONLY data strictly BEFORE that day to build the
historical Prior (walk-forward — the same non-negotiable
`PHASE_E_HISTORICAL_REPLAY.md` states for Stage 3, applied here for the
identical reason: a prior that has already seen today's outcome is not
predicting anything), would `allocation.scoring.same_day_fit_multiplier()`
have flagged that day as a statistical underperformance outlier — and did
that day's own eventual hit-rate in fact land materially below the
historical rate?

THIS IS THE "MODEL COMPUTES PREDICTIONS AGAINST ALREADY-RESOLVED HISTORY
AND LOGS ITS OWN PREDICTED-VS-ACTUAL ACCURACY" STEP THE ROADMAP NAMES.
Nothing here is visible outside this table — no proposal, no config
change, no effect on any live decision. Gate D5's own Stage 2 ("proposal,
only once (1) shows real, tracked skill") reads this log; this tool only
produces it.

WHAT THIS IS NOT, AND WHY. The true design question is intra-day: "once
today's Nth trade for this engine resolves and the flag fires, does the
REST of the session actually do worse". Answering that needs a real
chronological ordering of same-day resolutions — `intraday_setups.
detected_at` exists (migration 106) but only on the unmerged
`feat/intraday-event-core` branch, stamped going forward from
24-Aug-2026 only. Until that lands here, this tool answers the coarser,
still-honest question: "is the flag, computed from the day's FULL
outcome, correlated with a day that really was unusual" — a same-day-
LEVEL calibration, not a within-day one. Recorded plainly rather than
pretending the finer question was answered; `evidence["scope"]` on every
written row says so.

Writes to `intraday_same_day_calibration` (migration 108). Read-only
against `intraday_setups`; changes no live behaviour, same guarantee
every other Track D shadow stage carries.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_int, fetch_all, get_supabase
from allocation.scoring import (_engine_of, _intraday_priors_from_rows,
                                same_day_fit_multiplier)


def _load_rows(sb) -> list[dict]:
    """
    Every detection with a resolved outcome, oldest first by `id` (the
    only column `fetch_all` may page on — see its own docstring on why
    `trade_date` alone, not being unique, silently duplicates and drops
    rows under paging). The exact select-string `intraday_priors()` uses,
    for the same reasons its own comment gives for each column: `meta`
    because `_engine_of` reads it, `confidence` because a below-floor
    engine's fallback logic touches it, `cost_verdict`/`cost_pct` because
    `_intraday_priors_from_rows` needs both to reproduce the taken-only-
    with-fallback behaviour the LIVE prior actually has at any point in
    history — not a TAKEN-only fetch, which would silently make every
    engine look better-funded than it ever was.
    """
    def _build():
        return (sb.table("intraday_setups")
                  .select("symbol,trade_date,strategy,outcome,outcome_pct,"
                          "entry,stop,direction,cost_verdict,cost_pct,meta,"
                          "confidence")
                  .not_.is_("outcome_pct", "null"))
    return fetch_all(_build)


def evaluate(rows: list[dict], min_n: int, prior_floor: int,
             probe_weight: float = 1.0) -> list[dict]:
    """
    Pure — the whole calibration walk except the fetch, so it is testable
    offline with synthetic rows the same way `_intraday_priors_from_rows`
    itself already is (`tests.test_regime_fit`'s own pattern).

    One output row per (engine, trade_date) that had at least `min_n`
    TAKEN-and-resolved trades that day — below that, there is nothing to
    test, same floor `same_day_fit_multiplier()` itself applies.

    `probe_weight` IS PASSED THROUGH TO EVERY same_day_fit_multiplier()
    CALL, DELIBERATELY NOT LEFT TO READ system_config. The shipped live
    weight is 0.0 (Stage 1 — this tool exists BECAUSE nothing is armed
    yet), so without this override every call here would hit that
    function's very first guard clause and return a no-op unconditionally
    — a calibration that can never flag anything is not a calibration, it
    is a tautology. Default 1.0 asks "what would this have flagged at
    FULL weight" — the most a real arm could ever do — so a day this
    calibration cannot flag even at full weight would not have been
    flagged at any real (weight <= 1.0) live setting either.
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = r.get("trade_date")
        if d:
            by_day[str(d)].append(r)
    days = sorted(by_day)

    out: list[dict] = []
    seen: list[dict] = []
    for day in days:
        day_rows = by_day[day]

        # WALK-FORWARD: the historical prior for `day` sees only rows from
        # STRICTLY EARLIER days. `_intraday_priors_from_rows` reproduces
        # the exact taken-only/fallback logic the live allocator's own
        # `_prior_for()` runs, so this is what a same-day monitor running
        # live ON `day` would ACTUALLY have had in hand that morning.
        historical = _intraday_priors_from_rows(seen, floor=prior_floor)

        by_engine: dict[str, list[dict]] = defaultdict(list)
        for r in day_rows:
            by_engine[_engine_of(r)].append(r)

        for eng, eng_rows in by_engine.items():
            # TAKEN ONLY, NO FALLBACK — deliberately stricter than the
            # historical side above. same_day_fit_multiplier()'s whole
            # point is "trades this system actually took, today"; a
            # refused detection is not evidence about today's TRADING,
            # only about today's DETECTING, and mixing the two in would
            # corrupt exactly the way this module's "a prior must be built
            # from the trades you would TAKE" rule already warns against.
            #
            # DEDUPED THROUGH THE SAME MACHINERY AS THE HISTORICAL SIDE —
            # NOT `_row_gross_r` PER RAW ROW. Caught live on the first real
            # run of this tool: `intraday_setups` carries one row per
            # (setup, evaluation cycle), not one row per trade — a setup
            # lingering near its level is re-recorded every ~15s while it
            # stays live (CLAUDE.md's own landmine: "ONE SETUP IS ONE
            # OBSERVATION, NOT ONE PER 15s CYCLE", the exact bug that once
            # made RNG's n=11 a single trade counted eleven times).
            # Un-deduped, this engine's first live run reported GAP at
            # today_n=670 on 20-Aug — no engine takes 670 trades in one
            # session; that is one lingering setup's cycles miscounted as
            # 670 independent observations. `_intraday_priors_from_rows`
            # already contains the correct (symbol, engine, trade_date)
            # collapse (`priors_intraday_dedup`); pre-filtering to TAKEN
            # rows FIRST and feeding only those through it makes that
            # function's own taken-only/fallback branch a true no-op (every
            # input row already qualifies) while still getting its trusted
            # dedup and R-conversion for free, rather than a second,
            # separately-written copy of that arithmetic.
            taken = [r for r in eng_rows if (r.get("cost_verdict") or "").upper() == "TAKEN"]
            if not taken:
                continue
            today = _intraday_priors_from_rows(taken, floor=1).get(f"INTRADAY/{eng}")
            if today is None or today.n < min_n:
                continue
            n = today.n
            # today.hit_rate is exactly wins/n from _dist()'s own division;
            # multiplying back by the same integer n recovers the integer
            # win count exactly (floating-point division then
            # multiplication by its own denominator does not lose
            # precision at the n this table will ever see).
            wins = round(today.hit_rate * n)

            hist = historical.get(f"INTRADAY/{eng}")
            mult, reason = same_day_fit_multiplier(eng, hist, wins, n,
                                                   probe_weight=probe_weight)

            out.append({
                "trade_date":          day,
                "engine":              eng,
                "historical_n":        hist.n if hist else 0,
                "historical_hit_rate": hist.hit_rate if (hist and hist.usable) else None,
                "today_wins":          wins,
                "today_n":             n,
                "today_hit_rate":      wins / n,
                "today_mean_r":        round(today.mean_r, 4),
                "flagged":             mult < 1.0,
                "multiplier":          round(mult, 4),
                "reason":              reason,
                "evidence": {
                    "scope": "same-day-level (full day's outcome), NOT "
                             "within-day — see this module's own docstring "
                             "for why intraday_setups.detected_at is not "
                             "yet available on this branch",
                },
            })

        seen.extend(day_rows)
    return out


def _write(sb, calibration_rows: list[dict]) -> int:
    if not calibration_rows:
        return 0
    written = 0
    for i in range(0, len(calibration_rows), 500):
        batch = calibration_rows[i:i + 500]
        (sb.table("intraday_same_day_calibration")
           .upsert(batch, on_conflict="trade_date,engine").execute())
        written += len(batch)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=None,
                    help="override intraday_same_day_fit_min_n for this run")
    ap.add_argument("--probe-weight", type=float, default=1.0,
                    help="weight to test AT, independent of the live "
                         "intraday_same_day_fit_weight config (which ships "
                         "0.0 in this stage) — default 1.0, the most a real "
                         "arm could ever do")
    ap.add_argument("--dry", action="store_true", help="compute and report, write nothing")
    args = ap.parse_args()

    sb = get_supabase()
    min_n = args.min_n if args.min_n is not None else cfg_int("intraday_same_day_fit_min_n", 5)
    prior_floor = cfg_int("priors_min_sample_intraday", 30)

    rows = _load_rows(sb)
    logger.info(f"  same_day_calibration: {len(rows)} resolved detection(s) loaded")
    if not rows:
        logger.warning("  same_day_calibration: nothing resolved yet — could not compute anything")
        return

    results = evaluate(rows, min_n, prior_floor, probe_weight=args.probe_weight)
    logger.info(f"  same_day_calibration: {len(results)} (engine, day) pair(s) "
                f"reached the {min_n}-trade same-day floor")

    if not results:
        logger.warning(
            f"  same_day_calibration: NO (engine, day) pair in the current history "
            f"ever took {min_n}+ trades in one engine on one day — could not "
            f"determine anything about the same-day monitor's calibration yet. "
            f"This is a real finding, not a failure: it says the book has not "
            f"yet generated a session dense enough for this question to be "
            f"askable, and is exactly what Gate D5's own 'stated minimum "
            f"window' criterion is waiting to accumulate.")
        return

    flagged = [r for r in results if r["flagged"]]
    logger.info(f"  same_day_calibration: {len(flagged)} of {len(results)} pair(s) "
                f"would have been flagged")
    if flagged:
        flagged_mean = statistics.fmean(r["today_mean_r"] for r in flagged)
        unflagged = [r for r in results if not r["flagged"]]
        unflagged_mean = (statistics.fmean(r["today_mean_r"] for r in unflagged)
                          if unflagged else float("nan"))
        logger.info(f"  same_day_calibration: flagged days mean R {flagged_mean:+.3f} "
                    f"vs unflagged days mean R {unflagged_mean:+.3f} "
                    f"(n={len(flagged)} vs n={len(unflagged)})")

    if args.dry:
        logger.info("  same_day_calibration: --dry, nothing written")
        return
    written = _write(sb, results)
    logger.success(f"  same_day_calibration: {written} row(s) written to "
                   f"intraday_same_day_calibration")


if __name__ == "__main__":
    main()
