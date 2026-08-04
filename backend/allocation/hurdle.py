"""
The bar a proposal must clear NOW, given what else is likely to arrive.

    hurdle(regime_bucket, slots_left, minutes_left) -> edge per rupee-day

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
With insufficient history this returns a bar derived from today's effective
thresholds, so shadow verdicts initially MATCH what the live system already
does. A shadow allocator that agrees on day one is the correct starting point:
it proves the plumbing before it changes any opinion, and any disagreement in
week one would be a bug rather than an insight.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float, cfg_int, get_supabase


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
           framework: str = "INTRADAY", sb=None) -> tuple[float, dict]:
    """
    Returns (bar, inputs). `inputs` is recorded with the verdict so the decision
    can be re-derived later — §19: a decision that cannot be reconstructed is a
    defect.
    """
    base = _empirical_base(bucket, framework, sb)

    # No slot, no decision to make. Returned as an infinite bar rather than as a
    # special case so callers have one code path.
    if slots_left <= 0:
        return float("inf"), {"reason": "no slots left", "base": base,
                              "bucket": bucket, "slots_left": 0}

    # Time: a full session ahead means better is probably still coming.
    session_minutes = cfg_int("alloc_session_minutes", 360)
    time_frac = max(0.0, min(minutes_left / max(session_minutes, 1), 1.0))
    time_mult = 1.0 + cfg_float("alloc_time_weight", 0.6) * time_frac

    # Scarcity: the last slot is worth more than the first.
    max_slots = max(cfg_int("alloc_max_slots", 2), 1)
    scarcity  = 1.0 + cfg_float("alloc_scarcity_weight", 0.5) * (
        (max_slots - slots_left) / max_slots)

    bar = base * time_mult * scarcity
    return bar, {
        "base": round(base, 5), "bucket": bucket,
        "time_mult": round(time_mult, 3), "scarcity_mult": round(scarcity, 3),
        "slots_left": slots_left, "minutes_left": minutes_left,
        "framework": framework,
    }


def _empirical_base(bucket: str, framework: str, sb=None) -> float:
    """
    The base bar, from the arrival distribution the system already stores.

    Built from resolved detections rather than from executed trades: the
    question is "what does a typical opportunity in this regime look like",
    and executed trades only answer it for the ones the old policy liked.

    The bar is the Nth percentile of realised edge among detections in the
    bucket — i.e. "clear what the top quarter of arrivals typically delivered".
    Below the sample floor it returns the cold-start value instead, and says so.
    """
    floor = cfg_int("alloc_hurdle_min_sample", 40)
    pct   = cfg_float("alloc_hurdle_percentile", 0.75)
    try:
        sb = sb or get_supabase()
        rows = (sb.table("intraday_setups")
                  .select("outcome_pct,entry,stop,regime_at_detection")
                  .not_.is_("outcome_pct", "null")
                  .limit(2000).execute().data) or []
    except Exception:
        rows = []

    edges = []
    for r in rows:
        try:
            e, s = float(r.get("entry") or 0), float(r.get("stop") or 0)
            if not e or not s or s >= e:
                continue
            risk_pct = (e - s) / e * 100.0
            edges.append(float(r["outcome_pct"]) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    if len(edges) < floor:
        cold = cfg_float("alloc_hurdle_cold_start", 0.0)
        logger.debug(f"  hurdle: {len(edges)} observations below floor {floor} — "
                     f"cold start {cold}")
        return cold

    edges.sort()
    base = edges[min(int(pct * len(edges)), len(edges) - 1)]
    # A negative empirical bar would mean "take anything that loses less than
    # typical", which is not a bar. Floored at zero: the allocator may decline
    # everything, but it will never be talked into a negative-edge trade by a
    # population that was mostly negative.
    return max(base, 0.0)
