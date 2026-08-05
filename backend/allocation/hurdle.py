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

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg, cfg_float, cfg_int, get_supabase, today_ist


STRONG, WEAK = "STRONG", "WEAK"


def regime_bucket(regime: str | None) -> str:
    """
    Collapse the existing regime classification into two.

    The boundary is drawn where the existing classifier already draws its own
    strongest line — RISK ON versus everything else — rather than at an invented
    threshold. A4 in the readiness review leaves this open precisely so it can be
    taken from the observed distribution instead of guessed.
    """
    r = (regime or "").upper()
    return STRONG if ("RISK ON" in r or r in ("BULLISH", "RECOVERING")) else WEAK


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
    base, base_meta = _empirical_base(bucket, framework, sb)

    # No slot, no decision to make. Returned as an infinite bar rather than as a
    # special case so callers have one code path.
    if slots_left <= 0:
        return float("inf"), {"reason": "no slots left", "base": base,
                              "bucket": bucket, "framework": framework,
                              "slots_left": 0, **base_meta}

    # Time: a full session ahead means better is probably still coming.
    session_minutes = cfg_int("alloc_session_minutes", 360)
    time_frac = max(0.0, min(minutes_left / max(session_minutes, 1), 1.0))
    time_mult = 1.0 + cfg_float("alloc_time_weight", 0.6) * time_frac

    # Scarcity: the last slot is worth more than the first.
    max_slots = max(int(max_slots or cfg_int("alloc_max_slots", 2)), 1)
    slots_left = min(slots_left, max_slots)
    scarcity  = 1.0 + cfg_float("alloc_scarcity_weight", 0.5) * (
        (max_slots - slots_left) / max_slots)

    # A PERMISSIVE BAR STAYS PERMISSIVE THROUGH THE MULTIPLIERS.
    #
    # The bar is multiplicative, which silently assumes it is positive. A cold
    # start of -inf survives that; a NEGATIVE empirical base does not — -0.4
    # scaled by 1.6 becomes -0.64, i.e. the multipliers make an already-loose
    # bar LOOSER early in the session, which is backwards. Patience must never
    # lower the bar, so the multipliers are applied to the distance above the
    # break-even point rather than to the raw number.
    if base == float("-inf"):
        bar = float("-inf")
    elif base >= 0:
        bar = base * time_mult * scarcity
    else:
        bar = base / (time_mult * scarcity)

    return bar, {
        "base": None if base == float("-inf") else round(base, 5),
        "bucket": bucket,
        "time_mult": round(time_mult, 3), "scarcity_mult": round(scarcity, 3),
        "slots_left": slots_left, "max_slots": max_slots,
        "minutes_left": minutes_left, "framework": framework,
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


def _empirical_base(bucket: str, framework: str, sb=None) -> tuple[float, dict]:
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

    Returns (base, meta). meta is recorded with the verdict.
    """
    floor = cfg_int("alloc_hurdle_min_sample", 40)
    pct   = cfg_float("alloc_hurdle_percentile", 0.75)
    days  = cfg_int("alloc_hurdle_lookback_days", 90)
    fw    = (framework or "INTRADAY").upper()

    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()
    pooled = False
    try:
        sb = sb or get_supabase()
        q = (sb.table("allocation_decisions")
               .select("edge,framework,regime_bucket,trade_date")
               .eq("framework", fw)
               .gte("trade_date", since)
               .not_.is_("edge", "null"))
        rows = (q.eq("regime_bucket", bucket).limit(4000).execute().data) or []
        if len(rows) < floor:
            # Not enough in this bucket yet. Pooling across buckets is a weaker
            # answer than segmenting, and a far better one than a cold start —
            # but the verdict must record which it got.
            pooled = True
            rows = (q.limit(4000).execute().data) or []
    except Exception as e:
        # LOUD. This is the failure that emptied the book: the same query threw
        # for a year of sessions behind logger.debug, and a silent fall-through
        # to the cold start looked exactly like a quiet market.
        logger.warning(f"  hurdle: the arrival distribution could not be read "
                       f"({str(e)[:90]}) — falling back to the cold-start bar. "
                       f"The allocator has NO empirical opinion this session.")
        return _cold_start(0, floor, f"query failed: {str(e)[:60]}")

    edges = []
    for r in rows:
        try:
            edges.append(float(r["edge"]))
        except (TypeError, ValueError, KeyError):
            continue

    if len(edges) < floor:
        return _cold_start(len(edges), floor,
                           f"only {len(edges)} scored arrival(s) for {fw}")

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
    return float(base), {"cold_start": False, "n": len(edges),
                         "sample_floor": floor, "percentile": pct,
                         "pooled_across_buckets": pooled,
                         "lookback_days": days, "population": "allocation_decisions.edge"}
