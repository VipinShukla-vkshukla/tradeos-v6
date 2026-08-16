"""
The outcome rule, ported line-for-line from `outcomes.resolve_day`.

WHY THIS IS A PORT AND NOT AN IMPORT
-------------------------------------
The resolution loop is inline inside `resolve_day`, which does token lookup,
Supabase reads and Supabase WRITES. The harness cannot call it without touching
the database and without writing rows into the live detection record. So the
loop is copied.

**A copy is only defensible because it is verified against the original's own
stored output.** `verify_known_day.py::check_outcome_rule()` feeds this function
the stored `(entry, stop, target, direction)` for a fully resolved date and
requires it to reproduce the recorded outcome on >= 99% of rows. Without that
check this file would be exactly the "second copy that drifts" the architecture
warns about.

THE BAD FILL IS THE POINT
--------------------------
`outcomes.py:181-199`, whose reasoning is already written there: *"Both inside
one bar. Assume the bad one — a coarse bar cannot tell you the sequence, and
assuming the good one is how a strategy looks profitable on paper and loses
money live."*

Direction arithmetic comes from `intraday.direction`, IMPORTED. The comment at
`outcomes.py:164-171` explains why that matters: the long form applied to a
short resolves STOP on the first bar every time, because a short's stop sits
above its entry. That would retire a working short engine on an arithmetic
error, and SDN is the only short engine this system has.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from intraday import direction as D
from intraday.strategies.base import Bar


@dataclass
class Outcome:
    """How one detection resolved, on its own planned levels."""
    outcome: str          # TARGET | STOP | TIMEOUT | UNKNOWN
    exit_price: float | None
    pct: float | None     # signed IN THE TRADE'S FAVOUR
    bars_held: int


def resolve(entry: float, stop: float, target: float, direction: str,
            bars: list[Bar], after: datetime | None = None) -> Outcome:
    """
    Walk the bars after `after` and return the first level touched.

    Ordering is load-bearing and matches production exactly: both-in-one-bar
    resolves STOP, then stop alone, then target. Any other order flatters the
    result.
    """
    seq = [b for b in bars if after is None or b.ts >= after]
    if not (entry and stop and target) or not seq:
        return Outcome("UNKNOWN", None, None, 0)

    dirn = D.normalise(direction)
    outcome, exit_px = "TIMEOUT", float(seq[-1].close)
    held = len(seq)

    for i, b in enumerate(seq, 1):
        hi, lo = float(b.high), float(b.low)
        if D.is_short(dirn):
            hit_stop, hit_tgt = hi >= stop, lo <= target
        else:
            hit_stop, hit_tgt = lo <= stop, hi >= target

        if hit_stop and hit_tgt:
            # Both inside one bar. Assume the bad one — a coarse bar cannot tell
            # you the sequence, and assuming the good one is how a strategy looks
            # profitable on paper and loses money live.
            outcome, exit_px, held = "STOP", stop, i
            break
        if hit_stop:
            outcome, exit_px, held = "STOP", stop, i
            break
        if hit_tgt:
            outcome, exit_px, held = "TARGET", target, i
            break

    # Signed IN THE TRADE'S FAVOUR, not in the price's direction. A short that
    # fell 1% made +1%.
    pct = D.gain_pct(entry, exit_px, dirn)
    return Outcome(outcome, exit_px, pct, held)


def planned_r(entry: float, stop: float, direction: str,
              out: Outcome) -> float | None:
    """
    The outcome expressed in R — gross, before costs.

    Gross and separate, deliberately: `cost_pct` is 0 on every bucket blocked
    before the cost gate (FINDINGS.md PRE-1), so gross R is the only quantity
    both sides of any gate carry. Net R is computed by the caller, which knows
    the product (CNC vs MIS) and therefore the real friction.
    """
    if out.pct is None:
        return None
    risk = D.risk_per_share(entry, stop, direction)
    if not risk or not entry:
        return None
    risk_pct = risk / entry * 100.0
    return out.pct / risk_pct if risk_pct else None
