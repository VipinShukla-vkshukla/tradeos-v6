"""
The one shape both books emit. Adapters, and nothing else.

WHAT THIS MODULE IS FORBIDDEN FROM DOING
----------------------------------------
It forms no opinion. It cannot promote a refused proposal, cannot re-derive a
level, cannot decide anything. It takes what `analysis.trade_decision.decide()`
and the intraday engines ALREADY returned and expresses it in one shape.

That restriction is the whole design. This repository has already lived through
three divergent risk-reward models giving three answers for one stock on one
day, and the cure was `decide()` becoming the single source of truth called by
the pipeline, the dashboard, Telegram and the daemon alike. An adapter that
"improves" a level, or that admits a proposal `decide()` declined, would
reintroduce exactly that — and it would be worse this time, because the
allocator's whole claim is that it ranks the same decisions the live path makes.

So: if `decide()` says WAIT, there is no proposal. Not a low-scoring one. None.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import IST


# The swing actions that represent "this is takeable now". Anything else —
# WAIT, AVOID, HOLD, an event block — produces no proposal at all.
TAKEABLE_SWING = ("BUY", "BUY_NOW", "ENTER", "STAGED_ENTRY")


@dataclass(frozen=True)
class Proposal:
    """One candidate for capital, from either book, on one scale."""
    symbol:      str
    framework:   str            # SWING | INTRADAY
    product:     str            # CNC | MIS — mandatory, never inferred downstream
    entry:       float
    stop:        float
    target:      float
    quantity:    int
    source:      str            # engine or family that produced it
    native_rank: float          # the book's own confidence/score, 0-100
    # LONG | SHORT. Defaulted so every pre-shorting construction of this class
    # is unaffected — swing is CNC-only and always LONG by construction, so
    # from_swing() passes it explicitly rather than relying on the default,
    # the same reasoning intraday.direction's own module docstring gives for
    # never defaulting silently at a site that COULD be either.
    direction:   str = "LONG"
    meta:        dict = field(default_factory=dict)
    created_at:  datetime = field(default_factory=lambda: datetime.now(IST))

    @property
    def risk_per_share(self) -> float:
        from intraday.direction import risk_per_share
        return risk_per_share(self.entry, self.stop, self.direction)

    @property
    def value(self) -> float:
        return self.entry * self.quantity

    @property
    def coherent(self) -> bool:
        """
        Levels that make arithmetic sense. Everything downstream assumes it.

        WAS `0 < stop < entry < target` — the LONG shape, unconditionally. A
        short's stop sits ABOVE entry and its target BELOW it, so every short
        proposal ever built here failed this check and was scored as
        incoherent before allocation/scoring.py ever saw it — the identical
        failure class the sign-convention spine fixed in Setup.coherent(),
        reached through a second, independent implementation that this class
        had always kept for itself instead of importing.
        """
        from intraday.direction import validate
        if not (self.entry > 0 and self.quantity > 0):
            return False
        ok, _ = validate(self.entry, self.stop, self.target, self.direction)
        return ok

    def key(self) -> tuple[str, str]:
        # (symbol, product) — the same key open_positions has been unique on
        # since migration 028. A bare symbol would let the swing CNC tranche and
        # the intraday MIS tranche of one name collide, which is the exact
        # corruption that key exists to prevent.
        return (self.symbol, self.product)


def from_swing(decision, plan: dict | None = None) -> Proposal | None:
    """
    Adapt a `trade_decision.Decision`. Returns None when it is not takeable.

    Reads the levels decide() computed. It does not recompute them, and it does
    not fall back to the plan's levels when decide() left one empty — an absent
    level means decide() could not size the trade, and inventing one here would
    manufacture a proposal the live path would never place.
    """
    if decision is None:
        return None
    action = str(getattr(decision, "action", "") or "").upper()
    if action not in TAKEABLE_SWING:
        return None

    entry = getattr(decision, "entry", None)
    stop  = getattr(decision, "stop", None)
    tgt   = getattr(decision, "target", None)
    qty   = getattr(decision, "qty", None)
    if None in (entry, stop, tgt) or not qty:
        return None

    p = Proposal(
        symbol      = decision.symbol,
        framework   = "SWING",
        product     = "CNC",
        entry       = float(entry),
        stop        = float(stop),
        target      = float(tgt),
        quantity    = int(qty),
        source      = str((plan or {}).get("strategy_source") or "CONTINUATION"),
        native_rank = float((plan or {}).get("final_score") or 0.0),
        # CNC by construction — this system does not short the delivery book,
        # and Indian cash-market rules make an unhedged retail short outside
        # intraday square-off impractical regardless. Explicit rather than
        # relying on the class default, matching product="CNC" above.
        direction   = "LONG",
        meta        = {
            "action":      action,
            "rr_live":     getattr(decision, "rr_live", None),
            "headline":    getattr(decision, "headline", ""),
            # Recorded so a decision made on a stale close is never mistaken for
            # one made on a live price when the allocator is scored later.
            "stale_price": bool(getattr(decision, "stale_price", False)),
        },
    )
    return p if p.coherent else None


def from_intraday(setup, quantity: int, product: str = "MIS") -> Proposal | None:
    """
    Adapt an intraday `Setup`.

    A SHADOWED ENGINE PRODUCES NO PROPOSAL. Stage 6 made shadow mean "evaluates
    and records but never receives capital"; letting one through here would
    route it back to capital through a different door, which is precisely the
    back door that stage closed.

    `source` is the FAMILY, matching how detections are now scored, so the
    allocator's record and the learning loop group on the same thing.
    """
    if setup is None or quantity <= 0:
        return None
    if (setup.meta or {}).get("lifecycle") == "SHADOW":
        return None

    p = Proposal(
        symbol      = setup.symbol,
        framework   = "INTRADAY",
        product     = product,
        entry       = float(setup.entry),
        stop        = float(setup.stop),
        target      = float(setup.target),
        quantity    = int(quantity),
        source      = str((setup.meta or {}).get("family") or setup.strategy),
        native_rank = round(float(setup.confidence) * 100.0, 1),
        # Carried from the setup, not defaulted. Before this the allocator's
        # OWN proposal object had no way to express a short at all — a short
        # Setup adapted here silently became a LONG Proposal, coherent()
        # rejected it (stop sits above entry, which fails the long-only
        # 0<stop<entry<target shape), and every short was scored
        # "not scoreable — levels incoherent" before scoring.score() ever
        # looked at it. Independent of, and found alongside, the missing
        # direction=best.direction argument at the setup's cost-model gate.
        direction   = getattr(setup, "direction", "LONG"),
        meta        = {
            "sub_engine":  (setup.meta or {}).get("sub_engine") or setup.strategy,
            "rationale":   getattr(setup, "rationale", ""),
            "phase":       (setup.meta or {}).get("phase"),
        },
    )
    return p if p.coherent else None
