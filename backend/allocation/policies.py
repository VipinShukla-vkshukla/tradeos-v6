"""
Two books, two mechanisms. They share a scale and nothing else.

    swing_assignment()   the full field is known at the open
    intraday_stopping()  arrivals are unseen

WHY NOT ONE POLICY
------------------
This is the one place the architecture is emphatic that unification would be a
mistake, and the reason is informational rather than aesthetic.

The swing book knows its entire candidate set at 09:15 — sixty-odd plans written
last night, immutable. That is an ASSIGNMENT problem: given every option and two
slots, which two? Reserving a slot for a high-edge plan whose zone has not been
touched yet is rational, because the plan is known to exist.

The intraday book knows nothing about the setup that will fire at 13:40. That is
a STOPPING problem: each arrival must be judged against a bar, alone, with no
knowledge of what comes next.

Forcing swing into a stopping frame throws away the swing book's single greatest
informational advantage — that it can see the whole field. Forcing intraday into
an assignment frame requires inventing a field that does not exist. Neither
error is recoverable downstream, which is why the mechanisms stay separate even
though the currency does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float, cfg_int

TAKE, DEFER, DECLINE = "TAKE", "DEFER", "DECLINE"


def intraday_stopping(scored: list[dict], bar: float, slots_left: int) -> list[dict]:
    """
    Each arrival against the bar, best first. A stopping rule.

    DEFER is not used here and that is deliberate. An intraday setup is a
    statement about the next few minutes; holding it aside to reconsider at
    14:00 is not deferring the same opportunity, it is inventing a different
    one. So an intraday proposal is taken or declined, and if it is declined it
    is recorded as declined rather than parked.
    """
    out = []
    taken = 0
    for p in sorted(scored, key=lambda x: -(x.get("edge") or float("-inf"))):
        edge = p.get("edge")
        if edge is None:
            out.append({**p, "verdict": DECLINE,
                        "reason": "not scoreable — levels incoherent or no prior"})
            continue
        # Bar first, slots second. Checking slots first meant every proposal
        # after the Nth-best was labelled "cleared the bar but slots were
        # spent" even when it never cleared the bar at all — on 2026-08-06,
        # AIAENG (edge 0.0123) and GLAXO (edge 0.0032) were both logged that
        # way against a bar of 0.0406. The verdict was always right (DECLINE
        # either way, since `taken` only advances on a real TAKE below); only
        # the reason lied about which gate actually stopped it.
        if edge < bar:
            out.append({**p, "verdict": DECLINE,
                        "reason": f"edge {edge:.4f} below the bar {bar:.4f} — "
                                  f"better is likely still to arrive"})
            continue
        if taken >= slots_left:
            out.append({**p, "verdict": DECLINE,
                        "reason": f"edge {edge:.4f} cleared the bar {bar:.4f} but "
                                  f"the slots were already spent on better"})
            continue
        out.append({**p, "verdict": TAKE,
                    "reason": f"edge {edge:.4f} clears the bar {bar:.4f}"})
        taken += 1
    return out


def swing_assignment(scored: list[dict], bar: float, slots_left: int,
                     field: list[dict] | None = None) -> list[dict]:
    """
    The whole field is known, so a slot may be RESERVED rather than spent.

    THE RESERVATION IS THE POINT.

    A plan sitting 0.4% below its entry zone with an edge well above anything
    triggering right now is worth waiting for. Spending the last slot on a
    marginal trigger at 10:00, when a materially better plan is likely to touch
    its zone by 13:00, is the specific error an assignment policy exists to
    avoid — and a stopping rule cannot see it, because a stopping rule does not
    know the better plan exists.

    RESERVATIONS DECAY. P(trigger today) falls as the session runs out: a zone
    4% away at 09:30 is a real possibility and at 14:45 it is not. So a
    reservation held on a plan whose trigger probability has collapsed is
    released, and the slot becomes spendable again. A reservation that never
    expires is just a slot the book forgot it had.

    `field` is every plan being monitored — triggered or not. Without it this
    degrades to a stopping rule and says so, rather than pretending to reserve
    against a field it cannot see.
    """
    if not field:
        logger.debug("  swing policy: no field supplied — degrading to stopping")
        return intraday_stopping(scored, bar, slots_left)

    reserve_mult = cfg_float("alloc_reserve_edge_multiple", 1.35)
    min_ptrig    = cfg_float("alloc_reserve_min_p_trigger", 0.35)

    # Untriggered plans good enough, and likely enough, to hold a slot for.
    reserved = []
    for f in field:
        if f.get("triggered"):
            continue
        e, pt = f.get("edge"), f.get("p_trigger")
        if e is None or pt is None:
            continue
        if e >= bar * reserve_mult and pt >= min_ptrig:
            reserved.append(f)
    reserved.sort(key=lambda x: -(x["edge"] * x["p_trigger"]))

    # A reservation cannot consume every slot. Being unable to act at all
    # because the book is holding out for something better is a failure mode of
    # its own, and one that produces no record to learn from.
    max_reserved = max(0, min(len(reserved), slots_left - 1))
    reserved = reserved[:max_reserved]
    spendable = slots_left - len(reserved)

    out, taken = [], 0
    for p in sorted(scored, key=lambda x: -(x.get("edge") or float("-inf"))):
        edge = p.get("edge")
        if edge is None:
            out.append({**p, "verdict": DECLINE,
                        "reason": "not scoreable — levels incoherent or no prior"})
            continue
        # A triggered plan that beats every reservation takes the slot anyway.
        # The reservation is a preference, not a lock.
        beats_all = all(edge >= r["edge"] for r in reserved) if reserved else True
        if taken >= spendable and not beats_all:
            best = reserved[0]
            out.append({**p, "verdict": DEFER,
                        "reason": (f"edge {edge:.4f} clears the bar, but a slot is held "
                                   f"for {best['symbol']} at edge {best['edge']:.4f} "
                                   f"with P(trigger)={best['p_trigger']:.0%} today")})
            continue
        if taken >= slots_left:
            out.append({**p, "verdict": DECLINE,
                        "reason": "slots spent on higher-edge plans"})
            continue
        if edge < bar:
            out.append({**p, "verdict": DECLINE,
                        "reason": f"edge {edge:.4f} below the bar {bar:.4f}"})
            continue
        out.append({**p, "verdict": TAKE,
                    "reason": (f"edge {edge:.4f} clears the bar {bar:.4f}"
                               + (f" and beats {len(reserved)} reservation(s)" if reserved else ""))})
        taken += 1
    return out


def p_trigger(dist_to_zone_pct: float | None, minutes_left: int,
              atr_pct: float | None) -> float | None:
    """
    Rough probability a plan's zone is touched before the close.

    Deliberately crude and deliberately honest about it: the distance to the
    zone measured in the name's own daily volatility, scaled by how much of the
    session is left. A plan half an ATR away with a full day remaining is likely;
    the same plan at 15:00 is not.

    Returns None when it cannot be computed, and the caller must treat None as
    "cannot reserve" rather than as zero. An unknown probability is not a low
    one, and reserving on a fabricated number is worse than not reserving.
    """
    if dist_to_zone_pct is None or not atr_pct or atr_pct <= 0:
        return None
    session = max(cfg_int("alloc_session_minutes", 360), 1)
    frac = max(0.0, min(minutes_left / session, 1.0))
    if frac <= 0:
        return 0.0
    # Distance in ATRs, discounted by the share of the session remaining.
    atrs_away = abs(dist_to_zone_pct) / atr_pct
    reach = atrs_away / max(frac, 0.05)
    if reach <= 0.25:
        return 0.9
    if reach >= 3.0:
        return 0.02
    return round(max(0.02, min(0.9, 1.0 / (1.0 + reach * reach))), 3)
