"""
The bar a proposal must clear NOW, given what else is likely to arrive.

    hurdle(regime_bucket, slots_left, minutes_left, framework, sb, max_slots)
        -> (bar, inputs)   bar is in EDGE units: net-of-cost R per rupee-day,
                           the same quantity scoring.score() returns

WHY A HURDLE AND NOT A THRESHOLD
--------------------------------
A fixed confidence floor answers "is this good enough?", which is the wrong
question. With two entries a day the question is never that — it is "is this the
best use of the entry I have left, given that the session is young and better
things usually arrive?"

So the bar moves. It is HIGH at 09:20 with both slots free, because five hours
of arrivals remain and spending a slot on a mediocre setup forecloses them. It
is LOW at 14:45 with one slot left, because an unspent slot earns nothing and
the alternative to a mediocre trade is no trade.

RISES WITH TIME REMAINING, RISES AS SLOTS RUN OUT
-------------------------------------------------
Two separate effects and they pull in opposite directions late in the day:

    more time left   -> higher bar (better is probably still coming)
    fewer slots left -> higher bar (the last slot is worth more than the first)

TWO REGIME BUCKETS, NOT ONE CURVE AND NOT PER-REGIME
-----------------------------------------------------
A pooled curve is wrong on both tails: too high in weak regimes, where few
proposals clear anything and the book sits idle for no reason, and too low in
strong regimes, where everything clears and the allocator degenerates into
first-come-first-served. Two buckets fix most of that with a sample the system
actually has. Per-regime fitting is Phase 5 and is gated on years of data, not
on cleverness.

COLD START IS DELIBERATELY AGREEABLE
------------------------------------
With insufficient history the bar is PERMISSIVE, so shadow verdicts initially
MATCH what the live system already does. A shadow allocator that agrees on day
one is the correct starting point: it proves the plumbing before it changes any
opinion, and any disagreement in week one would be a bug rather than an insight.

THE BAR IS MEASURED IN THE SAME UNITS THE SCORER PRODUCES — 05-Aug-2026
------------------------------------------------------------------------
It was not, and that emptied the intraday book for a full session.

`scoring.score()` returns `edge = (E[R] - cost_R) / hold_days`: expected R, NET
of costs, PER DAY. This module built its bar from the 75th percentile of
`outcome_pct / risk_pct` over resolved detections: realised R, GROSS, per trade.
Two different quantities compared with `<`. At 09:20 with both slots free the
bar came to +1.20 against an edge of -0.13 — a setup would have needed a prior
mean of +1.41R to clear, and the best engine in the book has never had one.

It never even got that far. The query named `regime_at_detection`, a column on
no migration and written by no code; PostgREST rejects the whole request for one
unknown column, the bare `except` swallowed it, and every call fell through to
the cold start. That cold start was 0.0 — and 0.0 against a cost-netted edge
still refuses everything, because the intraday prior mean (+0.08R) does not
cover the MIS round trip (+0.21R). Both paths led to DECLINE, all day, for every
setup, in every regime.

So the bar is now a percentile of `allocation_decisions.edge` — the allocator's
OWN scored arrivals, written by `scoring.score()`, in `scoring.score()`'s units.
That is not a convenience. It is the only construction under which the two
numbers cannot drift apart again, because there is exactly one place the
quantity is defined and both sides read it from there.

The bucket and framework arguments are now actually applied. They were accepted
and ignored, so STRONG and WEAK returned the identical bar and swing proposals
were priced against intraday detections — the pooled curve this docstring spends
a paragraph arguing against.
"""

from __future__ import annotations

import statistics
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg, cfg_bool, cfg_float, cfg_int, get_supabase, today_ist


STRONG, WEAK = "STRONG", "WEAK"

# PostgREST caps a response at 1000 rows regardless of a larger .limit() asked
# for client-side — CLAUDE.md's own documented landmine, hit again here. A
# single .limit(4000).execute() on a population of 1955 (this account's real
# SWING count today) silently returned exactly 1000 of them, in whatever order
# the server chose, and the 75th-percentile bar was computed on that arbitrary
# slice rather than the population it claimed to be. Page, don't ask harder.
PAGE = 1000


def regime_bucket(regime: str | None) -> str:
    """
    Collapse the existing regime classification into two.

    The boundary is drawn where the existing classifier already draws its own
    strongest line — RISK ON versus everything else — rather than at an invented
    threshold. A4 in the readiness review leaves this open precisely so it can be
    taken from the observed distribution instead of guessed.

    TWO VOCABULARIES REACH THIS FUNCTION, AND THEY WERE NEVER THE SAME ONE — 05-Aug-2026
    ----------------------------------------------------------------------------------
    This function was written against the SWING regime engine's states
    (`swing/compute/compute_regime.py`, `market_regime` table): five values,
    SPACE-separated — TRENDING, RISK ON, NEUTRAL, RECOVERING, RISK OFF, computed
    once a day with hysteresis.

    But the only place this function is ever actually called —
    `allocation/allocator.py:105`, fed from `intraday/engine.py`'s
    `_allocate_shadow` with `regime=mc.state` — passes the INTRADAY market
    context's state (`intraday/market_context.py`): four values,
    UNDERSCORE-separated — RISK_ON, NEUTRAL, CAUTION, RISK_OFF, recomputed every
    15 seconds from the index alone.

    `"RISK ON" in "RISK_ON"` is False. The underscore is not a space. So in
    every real cycle this system has ever run, `regime_bucket()` returned WEAK —
    the STRONG branch was dead code, unreachable from its own call site. The
    segmentation migration 044 built (STRONG vs WEAK arrival distributions for
    the allocator's bar) was silently pooling everything into WEAK, which
    defeats the purpose without breaking anything loudly enough to notice — the
    same failure SHAPE as the units bug 044 itself fixed, one level up.

    A second, independent defect sat in the same line: even fed the CORRECT
    swing string, "TRENDING" — the regime engine's strongest state, stronger
    than "RISK ON" — does not contain the substring "RISK ON" and returned WEAK.
    Substring matching against a small closed vocabulary is exactly the kind of
    check that looks like it works and is actually one enum value away from
    silently not.

    Fixed by naming every input value this function can actually receive,
    instead of pattern-matching a string. Two closed sets, not one fuzzy one.
    """
    r = (regime or "").strip().upper()

    # Swing (space-separated, daily, from market_regime via compute_regime.py).
    # TRENDING and RISK ON are both "the market is confirmed favourable" —
    # RECOVERING is included because that was this function's behaviour before
    # this fix (the docstring's own boundary is `stop <= RISK ON`, but the
    # original code additionally special-cased RECOVERING as strong, and that
    # boundary is preserved rather than silently narrowed).
    if r in ("TRENDING", "RISK ON", "RECOVERING"):
        return STRONG
    if r in ("NEUTRAL", "RISK OFF"):
        return WEAK

    # Intraday (underscore-separated, every 15s, from market_context.py). Only
    # RISK_ON is unambiguously favourable; CAUTION is already a reduced-size
    # state and belongs with WEAK, not with strength.
    if r == "RISK_ON":
        return STRONG
    if r in ("NEUTRAL", "CAUTION", "RISK_OFF"):
        return WEAK

    # Dead legacy labels from before the swing hysteresis engine existed —
    # ai/providers/ml_regime_classifier.py's own legacy_map normalises these
    # away before training for exactly this reason. "BULLISH" and "BEARISH"
    # have not been a live regime value since; kept here, mapped rather than
    # silently matched, so a value from an old row or an external caller still
    # resolves to something defensible instead of falling through to WEAK by
    # accident.
    if r == "BULLISH":
        return STRONG
    if r == "BEARISH":
        return WEAK

    # Unrecognised — including "" and None — is WEAK, not STRONG. An unknown
    # market is not evidence of strength, the same reasoning
    # market_context.classify() uses for its own missing-data case.
    return WEAK


def _quantile(sorted_vals: list[float], q: float) -> float:
    """`sorted_vals[q]` by fraction. Same indexing `_dist()` uses for p10/p90."""
    if not sorted_vals:
        return float("-inf")
    q = max(0.0, min(q, 1.0))
    return sorted_vals[min(int(q * len(sorted_vals)), len(sorted_vals) - 1)]


def _effective_percentile(pct0: float, time_mult: float, scarcity: float,
                          time_mult_max: float, scarcity_max: float,
                          cap: float = 0.95) -> float:
    """
    Maps time/scarcity pressure to WHICH PERCENTILE of today's arrivals must be
    cleared, instead of multiplying a base R number that may be tiny, zero, or
    negative — 06-Aug-2026, the units-drift bug's sibling.

    The 05-Aug fix already had to special-case a negative base because
    multiplying it makes an already-loose bar LOOSER, backwards from the
    intent. That special case is a symptom: an R number's sign and scale are
    not comparable across sessions or regimes, so "multiply it by 1.6" was
    never a meaningful operation. "Require the top X% of today's arrivals" is
    meaningful regardless of whether arrivals are currently running positive
    or negative, which is why this needs no sign-dependent branch at all.

    `reach` is `time_mult * scarcity`, the same combined pressure the old
    multiplicative bar used; `max_reach` is what it would be at the most
    selective instant (full session ahead, one slot left). The percentile
    interpolates from `pct0` (baseline, e.g. 0.75 — no particular pressure) up
    to `cap` (e.g. 0.95 — as selective as this bar ever gets) linearly in that
    reach. Clamped to never go BELOW `pct0`: patience must never lower the bar,
    the same invariant the old code enforced by a different mechanism.
    """
    reach     = max(time_mult, 1.0) * max(scarcity, 1.0)
    max_reach = max(time_mult_max, 1.0) * max(scarcity_max, 1.0)
    if max_reach <= 1.0:
        return pct0
    frac = max(0.0, min((reach - 1.0) / (max_reach - 1.0), 1.0))
    return max(pct0, min(pct0 + (cap - pct0) * frac, cap))


def hurdle(bucket: str, slots_left: int, minutes_left: int,
           framework: str = "INTRADAY", sb=None,
           max_slots: int | None = None) -> tuple[float, dict]:
    """
    Returns (bar, inputs). `inputs` is recorded with the verdict so the decision
    can be re-derived later — §19: a decision that cannot be reconstructed is a
    defect.

    `max_slots` is the BOOK'S OWN daily budget, passed in by the caller. It used
    to read the pooled `alloc_max_slots` (2, across both books) while the caller
    passed a per-book `slots_left`, so the scarcity term was computed against a
    denominator that had nothing to do with the numerator: an intraday book with
    4 of its 4 slots free scored as though it had already spent two of two.
    """
    base, base_meta, edges = _empirical_base(bucket, framework, sb)

    # No slot, no decision to make. Returned as an infinite bar rather than as a
    # special case so callers have one code path.
    if slots_left <= 0:
        return float("inf"), {"reason": "no slots left", "base": base,
                              "bucket": bucket, "framework": framework,
                              "slots_left": 0, **base_meta}

    # Time: a full session ahead means better is probably still coming.
    session_minutes = cfg_int("alloc_session_minutes", 360)
    time_frac = max(0.0, min(minutes_left / max(session_minutes, 1), 1.0))
    time_weight = cfg_float("alloc_time_weight", 0.6)
    time_mult = 1.0 + time_weight * time_frac

    # Scarcity: the last slot is worth more than the first.
    max_slots = max(int(max_slots or cfg_int("alloc_max_slots", 2)), 1)
    slots_left = min(slots_left, max_slots)
    scarcity_weight = cfg_float("alloc_scarcity_weight", 0.5)
    scarcity = 1.0 + scarcity_weight * ((max_slots - slots_left) / max_slots)

    pct0 = cfg_float("alloc_hurdle_percentile", 0.75)
    eff_pct = None

    if base == float("-inf"):
        # Cold start, permissive. Unaffected by time/scarcity on purpose — an
        # allocator with no opinion must stay indistinguishable from no
        # allocator, at any hour, with any number of slots left.
        bar = float("-inf")
    elif edges:
        # THE NORMAL CASE — a real arrival population exists. Percentile-space,
        # see `_effective_percentile`.
        time_mult_max = 1.0 + time_weight
        scarcity_max  = 1.0 + scarcity_weight * ((max_slots - 1) / max_slots)
        eff_pct = _effective_percentile(pct0, time_mult, scarcity,
                                        time_mult_max, scarcity_max)
        bar = _quantile(edges, eff_pct)
    else:
        # A DELIBERATELY-SET cold-start floor (`alloc_hurdle_cold_start`) with
        # no population to build a percentile from — nothing to re-slice, so
        # this preserves the original absolute-space scaling for exactly this
        # one opt-in case, still guarded against a negative floor getting
        # LOOSER under multiplication (05-Aug's fix).
        if base >= 0:
            bar = base * time_mult * scarcity
        else:
            bar = base / (time_mult * scarcity)

    # ── THE BAR MAY NEVER FALL BELOW ZERO EXPECTED R — 10-Aug-2026 ──────────
    #
    # WHAT HAPPENED. On 10-Aug the INTRADAY book scored 262 proposals and the
    # STRONG bucket's bar settled at -1.09359. DEVYANI/SDN was scored at edge
    # -1.0935 and TAKEN — clearing the bar by 0.00009 — then closed 99 seconds
    # later at -0.813R. Across that whole bucket the DECLINEd proposals
    # averaged edge -1.0937 and the TAKEn ones -1.0935: a separation of two
    # ten-thousandths across 142 proposals. The allocator was not choosing
    # between trades, it was slicing a degenerate negative distribution at its
    # own 75th percentile and calling the top slice a decision.
    #
    # WHY A PERCENTILE ALONE CANNOT CATCH THIS. `hurdle()` asks a purely
    # RELATIVE question — "is this better than what else is arriving?" — and a
    # percentile of an all-negative population is still negative. Worse, the
    # bar then DECAYS toward that negative base as the session runs out
    # (time_mult -> 1.0), so the system's willingness to take a measurably
    # losing trade RISES with the certainty that it is losing. That is sound
    # for a positive population — an unspent slot earns nothing, so take what
    # you can get at 14:45 — and exactly inverted for a negative one, because
    # the alternative to a negative-edge trade is not zero, it is BETTER than
    # zero: you keep the round trip. This file's own project rule says it —
    # "no trade costs nothing while a mediocre one costs 0.21% plus the risk."
    #
    # WHY THIS IS NOT THE "SECOND, HIDDEN COST CHARGE" `_empirical_base`
    # WARNS AGAINST. That comment is about flooring the BASE — the percentile
    # itself — which would compound with the time/scarcity multipliers and
    # demand a proposal beat its costs by the full percentile on top. This
    # floors the FINAL BAR instead, after every multiplier has been applied.
    # `edge = e_r / max(hold_days, 0.5)` and `e_r = prior.mean_r - cost_r`, and
    # max(hold_days, 0.5) is always positive, so sign(edge) == sign(e_r):
    # `edge > 0` means precisely `expected R exceeds the round trip`, charged
    # once. When the population is healthy and positive this floor never binds
    # and the percentile does all the selecting, exactly as before.
    #
    # THE COLD START IS DELIBERATELY EXEMPT. "No opinion" and "measured bad"
    # are different claims and must not produce the same answer. An allocator
    # with no history must stay indistinguishable from no allocator at all
    # (see `_cold_start`), and the gates that ARE safety controls — cost,
    # structure, event, conviction, liquidity — have all already run and none
    # of them fails open. A measured-negative population is evidence; an empty
    # one is not.
    floored = False
    if bar not in (float("inf"), float("-inf")):
        floor_edge = cfg_float("alloc_edge_absolute_floor", 0.0)
        if bar < floor_edge:
            bar, floored = floor_edge, True

    return bar, {
        "base": None if base == float("-inf") else round(base, 5),
        "bucket": bucket,
        "time_mult": round(time_mult, 3), "scarcity_mult": round(scarcity, 3),
        "effective_percentile": None if eff_pct is None else round(eff_pct, 4),
        "slots_left": slots_left, "max_slots": max_slots,
        "minutes_left": minutes_left, "framework": framework,
        "absolute_floor_applied": floored,
        **base_meta,
    }


def _cold_start(n: int, floor: int, why: str) -> tuple[float, dict]:
    """
    The bar when there is not enough history to have an opinion.

    PERMISSIVE, AND THAT IS THE ENTIRE CONTRACT. This module's own docstring
    promises a cold start that AGREES with the live path so the plumbing is
    proved before any opinion changes. The old cold start returned 0.0, which
    against a cost-netted edge is not agreement — it is a refusal of every
    proposal whose expected R does not exceed its own round trip, which on the
    intraday book is all of them. An allocator with no data must not be
    distinguishable from no allocator at all.

    `alloc_hurdle_cold_start` is still honoured when the operator has SET it,
    so a deliberate hard floor remains available. Unset means permissive; the
    difference is now explicit rather than resting on a default of 0.0 that
    reads as harmless and is not.
    """
    raw = cfg("alloc_hurdle_cold_start", "").strip()
    if raw:
        try:
            return float(raw), {"cold_start": True, "cold_start_value": float(raw),
                                "n": n, "sample_floor": floor, "note": why}
        except ValueError:
            pass
    return float("-inf"), {"cold_start": True, "cold_start_value": None,
                           "n": n, "sample_floor": floor, "note": why}


def _empirical_base(bucket: str, framework: str, sb=None
                    ) -> tuple[float, dict, list[float] | None]:
    """
    The base bar, from the arrival distribution the allocator itself recorded.

    THE POPULATION IS `allocation_decisions`, NOT `intraday_setups`.

    The bar must be the same quantity as the thing it is compared against, and
    the only way to guarantee that permanently is to read it from where that
    quantity is produced. `scoring.score()` writes `edge` into every
    allocation_decisions row — every proposal, TAKE and DECLINE alike, which is
    exactly the unbiased arrival distribution a stopping rule needs. Reading the
    bar from that column makes a units drift structurally impossible: there is
    one definition of edge and both sides of the `<` read it.

    The previous population was resolved detections converted to gross R. It was
    a different quantity (gross, per trade, no cost) AND it was unreachable,
    because the select named a column that does not exist. See the module
    docstring; it cost a full session of the intraday book.

    SEGMENTED, BECAUSE THE ARGUMENTS ARE NOT DECORATION. Framework and regime
    bucket both filter. A swing plan is not priced against intraday arrivals and
    a RISK OFF morning is not priced against a RISK ON one. Where the bucket
    column is not yet populated the query falls back to pooled AND SAYS SO in
    the returned inputs, so a verdict never claims a segmentation it did not get.

    Returns (base, meta, edges). meta is recorded with the verdict; `edges` is
    the full sorted arrival population and is NOT part of meta — meta flows
    into `allocation_decisions.hurdle_inputs` (JSON, persisted every verdict),
    and a population that can run to thousands of rows has no business being
    written into every single one of them. `edges` is `None` on a cold start,
    exactly when there is no population to re-slice — `hurdle()` uses it to
    pick a different percentile of the SAME data without a second query.
    """
    floor = cfg_int("alloc_hurdle_min_sample", 40)
    pct   = cfg_float("alloc_hurdle_percentile", 0.75)
    days  = cfg_int("alloc_hurdle_lookback_days", 90)
    fw    = (framework or "INTRADAY").upper()

    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()
    pooled = False
    try:
        sb = sb or get_supabase()

        # A FRESH BUILDER PER ATTEMPT, NOT ONE REUSED ACROSS BOTH.
        #
        # supabase-py's query builder mutates in place: q.eq(...) returns the
        # SAME object it was called on, not a copy. `q is q.eq(...)` is True.
        # The original code built one `q`, called `.eq("regime_bucket", ...)`
        # on it for the bucketed attempt, then reused that same `q` for the
        # "pooled" fallback expecting the bucket filter to be gone — it was
        # never gone. The pooled retry silently re-ran the identical bucketed
        # query, so a thin bucket fell straight to cold start on every call
        # instead of pooling, and the "pooled_across_buckets" flag this
        # function's own docstring promises has never once been able to be
        # True. Two independent builders is the fix, not a workaround.
        def base_query():
            return (sb.table("allocation_decisions")
                      .select("symbol,edge,framework,regime_bucket,trade_date")
                      .eq("framework", fw)
                      .gte("trade_date", since)
                      .not_.is_("edge", "null"))

        # PAGED, NOT .limit(4000). A single request asking for 4000 rows still
        # gets at most 1000 back — PostgREST's own server-side cap, which a
        # bigger client-side .limit() cannot raise. Confirmed on this account:
        # 1955 real SWING rows, 1000 returned, the 75th percentile computed on
        # whichever 1000 the server happened to return. A fresh builder every
        # page for the same reason base_query() exists — .range() mutates too.
        def fetch_all(filtered: bool) -> list:
            out, off = [], 0
            while True:
                q = base_query()
                if filtered:
                    q = q.eq("regime_bucket", bucket)
                page = q.range(off, off + PAGE - 1).execute().data or []
                out += page
                if len(page) < PAGE:
                    break
                off += PAGE
            return out

        rows = fetch_all(filtered=True)
    except Exception as e:
        # LOUD. This is the failure that emptied the book: the same query threw
        # for a year of sessions behind logger.debug, and a silent fall-through
        # to the cold start looked exactly like a quiet market.
        logger.warning(f"  hurdle: the arrival distribution could not be read "
                       f"({str(e)[:90]}) — falling back to the cold-start bar. "
                       f"The allocator has NO empirical opinion this session.")
        base, meta = _cold_start(0, floor, f"query failed: {str(e)[:60]}")
        return base, meta, None

    # DEDUPLICATION — 07-Aug-2026, OFF by default for BOTH books, decided
    # per-framework so a change to one never touches the other's live bar.
    #
    # One row per (proposal, 15-second cycle), no dedup by symbol or day: a
    # candidate that sits near its entry zone for hours contributes hundreds
    # of near-identical edge values to the very population its own bar gets
    # compared against. Confirmed live, SWING, 07-Aug-2026: ten different
    # symbols each logged 548-600 rows in a single session — widespread
    # repetition across many names, not a couple of outliers, which
    # complicates rather than simply confirms the original hypothesis. See
    # tools/hurdle_population_audit.py for the actual p75/p95 delta this
    # produces before trusting either reading of it.
    #
    # Intraday setups are minutes long, not hours — the same mechanism is
    # expected to inflate its population less, but "expected" is not
    # "measured", which is why this defaults OFF there too rather than
    # inheriting swing's evidence.
    dedup = cfg_bool(f"alloc_hurdle_dedup_{fw.lower()}", False)

    def _extract(rowset: list) -> list[float]:
        if not dedup:
            out = []
            for r in rowset:
                try:
                    out.append(float(r["edge"]))
                except (TypeError, ValueError, KeyError):
                    continue
            return out
        by_symbol_day: dict[tuple, list[float]] = {}
        for r in rowset:
            try:
                e = float(r["edge"])
            except (TypeError, ValueError, KeyError):
                continue
            key = (r.get("symbol"), r.get("trade_date"))
            by_symbol_day.setdefault(key, []).append(e)
        return [statistics.fmean(v) for v in by_symbol_day.values()]

    # THE FLOOR CHECK MUST RUN ON WHAT WILL ACTUALLY BE USED, NOT ON THE RAW
    # ROW COUNT — 10-Aug-2026. Before this, "enough rows in this bucket?" was
    # decided on raw_n (one row per 15s cycle), then dedup collapsed that same
    # bucket's rows down to one-per-symbol-per-day AFTER the pool-or-segment
    # decision had already been made. A bucket with 600+ raw rows from a
    # handful of lingering candidates passed the raw check, segmented, then
    # deduped down to 31 distinct symbol-days — below the 40-sample floor —
    # with no path left to try pooling across buckets, so it fell straight to
    # a cold start. Cold start returns bar=-inf: EVERY candidate clears it.
    # Caught turning on alloc_hurdle_dedup_intraday for the first time, live,
    # before the next cycle could act on it; reverted within the same minute.
    # Fix: extract (raw or deduped, per the switch) BEFORE the floor check,
    # and pool on THAT count, exactly mirroring what the raw-only code already
    # did for the un-deduplicated case.
    # ── A BUCKET MAY NOT BE ITS OWN EVIDENCE — 10-Aug-2026 ──────────────────
    #
    # `regime_bucket()`'s STRONG branch was unreachable dead code until the
    # 05-Aug vocabulary fix ("RISK ON" in "RISK_ON" is False), so the STRONG
    # bucket has almost no history inside the lookback window: 07-Aug logged
    # ZERO STRONG rows. On 10-Aug the index turned risk-on and 142 proposals
    # landed in that empty bucket. Watch what the live log did with it:
    #
    #   13:08:22   INTRADAY slots 8 bar -1.07428 POOLED   <40 rows, pooled
    #   13:12:31   INTRADAY slots 8 bar -1.09359          >=40 rows, segmented
    #
    # Between those two cycles the bucket crossed the 40-sample floor ON ROWS
    # THE ALLOCATOR ITSELF HAD WRITTEN IN THE PRECEDING THREE HOURS, and from
    # that moment its bar was the 75th percentile of its own morning output.
    # A closed loop with no external anchor: whatever it decided at 09:30
    # became the standard it judged 13:30 against. That is what produced the
    # 0.0002 TAKE/DECLINE separation the absolute floor above now backstops.
    #
    # SEGMENTATION NOW REQUIRES SETTLED EVIDENCE — rows from PRIOR trading
    # days. Today's rows still COUNT toward the percentile once a bucket has
    # earned the right to segment (they are genuine arrivals, and "is better
    # still to arrive today" is the question the bar is asking); they simply
    # cannot be what grants that right. A bucket whose only evidence is the
    # session currently being decided must pool across buckets instead, which
    # is a weaker answer than segmenting and a far better one than grading its
    # own homework.
    #
    # Unconditional rather than switched: a bucket bootstrapping itself is a
    # defect, not a policy, and a switch here would only be a way to turn the
    # defect back on.
    today_s = today_ist().isoformat()

    def _settled(rowset: list) -> list:
        return [r for r in rowset if str(r.get("trade_date") or "") < today_s]

    raw_n = len(rows)
    edges = _extract(rows)
    settled_n = len(_extract(_settled(rows)))
    if settled_n < floor:
        pooled = True
        rows = fetch_all(filtered=False)
        raw_n = len(rows)
        edges = _extract(rows)
        settled_n = len(_extract(_settled(rows)))

    if len(edges) < floor:
        base, meta = _cold_start(len(edges), floor,
                                 f"only {len(edges)} scored arrival(s) for {fw}"
                                 + (" (deduplicated)" if dedup else ""))
        return base, meta, None

    edges.sort()
    base = edges[min(int(pct * len(edges)), len(edges) - 1)]
    # NOT floored at zero. The old floor was written to stop a mostly-negative
    # population talking the allocator into a losing trade — but it was applied
    # to a GROSS distribution, where zero is roughly break-even. On the net
    # edge scale zero already has costs subtracted, so flooring there says "only
    # ever take a proposal that beats its own costs by the full percentile",
    # which is a second, hidden cost charge. The percentile is the bar; the
    # relative comparison is what selects, and `scoring.score()` has already
    # taken the costs out once.
    return float(base), {"cold_start": False, "n": len(edges), "raw_n": raw_n,
                         "settled_n": settled_n,
                         "deduplicated": dedup,
                         "sample_floor": floor, "percentile": pct,
                         "pooled_across_buckets": pooled,
                         "lookback_days": days, "population": "allocation_decisions.edge"}, edges
