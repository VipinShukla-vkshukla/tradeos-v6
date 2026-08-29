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
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_bool, cfg_float, cfg_int

TAKE, DEFER, DECLINE = "TAKE", "DEFER", "DECLINE"


def _edge_key(s: dict) -> float:
    """
    A proposal's edge for ordering, with an ABSENT edge sorted last.

    Written out rather than `s.get("edge") or float("-inf")`, which was the
    idiom here and is wrong for exactly one value: `0.0 or float("-inf")`
    evaluates to `-inf`, because 0.0 is falsy. An edge of precisely zero —
    a NEUTRAL prior against zero modelled cost — would have sorted BELOW
    every loser instead of above them. Vanishingly rare in floating point
    and completely silent when it happens, which is the only reason it
    survived; `None` is the one case that genuinely means "no opinion" and
    it is now the only case treated as such.
    """
    e = s.get("edge")
    return float("-inf") if e is None else float(e)


def _engine_of_scored(s: dict) -> str:
    """Which engine produced this proposal. '' when it cannot be determined."""
    p = s.get("proposal")
    if p is None:
        return ""
    meta = getattr(p, "meta", None) or {}
    return str(meta.get("sub_engine") or getattr(p, "source", "") or "")


def build_priority_criteria(rows: list[dict]) -> dict[str, dict[str, set]]:
    """
    Pure. `{engine: {feature: {category, ...}}}` from VALIDATED,
    favourable, CATEGORICAL `brain_proposals` rows — 22-Aug-2026, F-50.

    Categorical only: a `target_key` with fewer than 3 `/`-separated parts
    is a numeric finding (`engine/feature`, no category) and is skipped —
    see `allocation.allocator.refresh_priority_criteria`'s own docstring
    for why numeric findings are not read here. A malformed key (any other
    shape) is skipped the same way rather than raising, matching this
    module's existing tolerance for a proposal row that does not parse.
    """
    out: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        parts = str(r.get("target_key") or "").split("/")
        if len(parts) != 3:
            continue
        engine, feature, category = parts
        out[engine][feature].add(category)
    return {eng: dict(feats) for eng, feats in out.items()}


def _matches_priority_criteria(meta: dict, engine: str,
                               criteria: dict[str, dict[str, set]] | None) -> bool:
    """Pure. True when this candidate's own meta carries a value on ANY
    VALIDATED, favourable feature for its engine — one match is enough,
    this is a tie-break, not a scoring function that needs to weigh how
    many criteria agree."""
    if not criteria:
        return False
    for feature, categories in criteria.get(engine, {}).items():
        if feature == "_hour_bucket":
            continue  # computed from `ts` at study time, not a meta field
        if str(meta.get(feature)) in categories:
            return True
    return False


def _confirmation_key(s: dict, priority_criteria: dict | None = None) -> int:
    """
    0 when the proposal's own detection confirmed itself before firing
    (currently: ORB's `retest_confirmed`, F-37) OR matches a VALIDATED,
    favourable criterion from `feature_edge_study.py`'s out-of-sample
    check (F-50) — 1 otherwise. A pure TIE-BREAKER among same-engine
    candidates that would otherwise be ordered arbitrarily; never a
    second signal on top of an already-decided one, just the same
    priority question asked with more evidence than "was this retested".

    WHY THIS IS SAFE TO SHIP ARMED, UNLIKE EVERY OTHER NEW RULE THIS
    SESSION. giveback_pct, short_runway_tighten and volume_decay each ship
    inert because they can ADMIT or DECLINE a trade with zero calibration
    behind the threshold. This cannot: `_interleave_by_engine` already
    ranks same-engine candidates by edge, and within one engine's own
    prior every candidate carries very nearly the same edge (see that
    function's own 19-Aug finding) — so ties are already being broken by
    something, today by whatever order cost_r happens to produce. This
    just replaces an ARBITRARY tie-break with an EVIDENCE-BACKED one: of
    21-Aug's 6 unconfirmed ORB trades, 0 won; the one confirmed trade
    (POWERGRID) closed at +1.65R. The broader post-18-Aug sample agrees in
    direction (confirmed 33% win / +0.18% mean vs unconfirmed 0% / -0.45%,
    n=7 vs 17 — thin, but consistent). It cannot admit a candidate that
    would otherwise have been declined, or decline one that would have
    cleared — only change WHICH of several already-tied candidates gets a
    shared slot. `alloc_intraday_confirmation_priority`, default true.

    `priority_criteria` is a CACHE the caller loads once, on the slow
    timer — `allocation.allocator.Allocator.refresh_priority_criteria()`
    — never a live query from inside this pure function; see that
    method's own docstring for why. Never widens beyond FAVOURABLE,
    VALIDATED categorical findings: an unfavourable one (a category the
    data says to avoid) is deliberately not read here at all, the
    operator's own instruction being "priority criteria, not a hard
    filter" — de-prioritising is a soft block by another name.
    """
    if not cfg_bool("alloc_intraday_confirmation_priority", True):
        return 0
    p = s.get("proposal")
    meta = (getattr(p, "meta", None) or {}) if p is not None else {}
    if meta.get("retest_confirmed") is True:
        return 0
    engine = _engine_of_scored(s)
    if _matches_priority_criteria(meta, engine, priority_criteria):
        return 0
    return 1


def _interleave_by_engine(scored: list[dict],
                          priority_criteria: dict | None = None) -> list[dict]:
    """
    Pure. Every engine's BEST candidate first, then every engine's second, and
    so on — each round internally ordered by edge.

    WHY THE POOLED SORT WAS NOT ENOUGH — 19-Aug-2026
    ------------------------------------------------
    `edge` is `prior.mean_r - cost_r`, and `prior` is keyed on the ENGINE. So
    within one cycle every candidate from a given engine carries very nearly
    the same edge — they share the prior, and only `cost_r` (friction over
    that setup's own risk) separates them. A pooled descending sort therefore
    does not rank SETUPS, it ranks ENGINES, and then hands the whole slot
    budget to the top engine's candidates in whatever order friction happens
    to break their near-tie.

    Measured on the live book, 13-19 Aug 2026: 29 of 32 closed intraday
    positions came from ONE engine (SDN), while ORB detected 561 TAKEN rows
    across the same window and closed one position. That is not the allocator
    preferring better trades; it is the allocator preferring a better ENGINE
    and there being no mechanism by which a second engine's best idea competes
    with a first engine's fifth-best.

    THIS CANNOT ADMIT ANYTHING THAT FAILS THE BAR. Interleaving reorders the
    queue; it does not lower `bar`, and the caller still declines every
    proposal whose edge sits under it. The only outcomes that change are those
    where a lower-ranked engine's candidate ALREADY cleared the bar and lost
    its slot to a same-engine sibling — which is precisely the case this is
    for, and precisely why it is safe to have on by default.
    """
    # Edge first, then confirmation as a tie-break — see _confirmation_key's
    # own docstring for why this is the one new ranking rule this session
    # ships armed. It never outranks edge; it only decides order among
    # candidates edge already could not separate.
    ordered = sorted(scored, key=lambda x: (-_edge_key(x), _confirmation_key(x, priority_criteria)))
    seen: dict[str, int] = {}
    tagged = []
    for s in ordered:
        eng = _engine_of_scored(s)
        rank = seen.get(eng, 0)
        seen[eng] = rank + 1
        tagged.append((rank, s))
    # (round, then edge, then confirmation within the round). Fully
    # determined by the triple, so this does not depend on the stability of
    # the sort above for its result.
    tagged.sort(key=lambda t: (t[0], -_edge_key(t[1]), _confirmation_key(t[1], priority_criteria)))
    return [s for _, s in tagged]


def intraday_stopping(scored: list[dict], bar: float, slots_left: int,
                      bar_before_floor: float | None = None,
                      priority_criteria: dict | None = None) -> list[dict]:
    """
    Each arrival against the bar, best first. A stopping rule.

    DEFER is not used here and that is deliberate. An intraday setup is a
    statement about the next few minutes; holding it aside to reconsider at
    14:00 is not deferring the same opportunity, it is inventing a different
    one. So an intraday proposal is taken or declined, and if it is declined it
    is recorded as declined rather than parked.

    `floor_only_rank` — 12-Aug-2026, THE RANKING IS THE PRODUCT
    -----------------------------------------------------------
    When the absolute edge floor clamps the bar, every proposal declines and
    `engine.allocator_permits` used to wave the ENTIRE cycle through on a paper
    book. That waiver bypassed not just the floor but the two things that were
    working: the edge ORDER and `slots_left`. On 12-Aug the log read "5
    proposal(s) scored, 0 to take, 5 refused" and then opened all five. The
    allocator ranked them and the ranking was thrown away — so the paper book
    had no selection of any kind, and the day's budget went to whatever arrived
    first.

    This computes, in the SAME pass and by the SAME rule, which proposals would
    have been taken had only the RELATIVE question been asked — the percentile
    of what is arriving, `bar_before_floor`. Those get an integer rank; anyone
    else keeps a bare DECLINE. The verdict itself is unchanged, so the live
    book and `allocation_decisions` see exactly what they saw before.

    The point is that paper and live now run one decision procedure with one
    ranking and one slot budget. The only remaining divergence is whether the
    absolute floor binds, and it is bounded to the top `slots_left` instead of
    being unbounded.
    """
    out = []
    taken = 0
    taken_ex_floor = 0
    # ONE ORDERING, USED BY BOTH THE VERDICT AND `floor_only_rank`. The
    # exploration rank is computed inside this same loop precisely so paper
    # and live cannot disagree about which proposals were best; changing the
    # queue here therefore changes both together, which is the property that
    # made it safe to change at all.
    queue = (_interleave_by_engine(scored, priority_criteria)
             if cfg_bool("alloc_intraday_engine_fairness", True)
             else sorted(scored, key=lambda x: -_edge_key(x)))
    for p in queue:
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
        # Would the RELATIVE bar alone have taken it, and is there room? Same
        # order, same budget — only the floor is set aside. Computed before the
        # verdict branches so it is one pass over one sorted list, not a second
        # ranking that could disagree with this one.
        rank = None
        if (bar_before_floor is not None and edge >= bar_before_floor
                and taken_ex_floor < slots_left):
            rank = taken_ex_floor
            taken_ex_floor += 1

        if edge < bar:
            v = {**p, "verdict": DECLINE,
                 "reason": f"edge {edge:.4f} below the bar {bar:.4f} — "
                           f"better is likely still to arrive"}
            if rank is not None:
                v["floor_only_rank"] = rank
                v["reason"] += (f" (rank {rank + 1} of {slots_left} against the "
                                f"pre-floor bar {bar_before_floor:.4f})")
            out.append(v)
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
                     field: list[dict] | None = None,
                     bar_before_floor: float | None = None) -> list[dict]:
    """
    The whole field is known, so a slot may be RESERVED rather than spent.

    `bar_before_floor` / `floor_only_rank` — 29-Aug-2026, closing the SWING
    half of `intraday_stopping`'s own 12-Aug fix (see that function's
    docstring for the incident). The paper-book floor-exploration carve-out
    in `engine.allocator_permits` reads `v.get("floor_only_rank")` for
    EITHER book, but this function never set it — SWING's own `elif`
    fallback below (`if not field: ... return intraday_stopping(...)`)
    called that function WITHOUT `bar_before_floor` either, so even the
    fallback path left it unset. A floor-declined SWING proposal on the
    paper book could therefore never use the rescue valve migration 058
    built for exactly this shape — measured 29-Aug-2026: not yet biting
    (0 SWING declines sat exactly at the absolute floor in the last 21
    days), but the mechanism needs to exist before swing_priors() is ever
    tightened, not after — see docs/FINDINGS.md's own entry on why a
    taken-only swing prior is not being armed today.

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
        return intraday_stopping(scored, bar, slots_left,
                                 bar_before_floor=bar_before_floor)

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

    out, taken, taken_ex_floor = [], 0, 0
    for p in sorted(scored, key=lambda x: -(x.get("edge") or float("-inf"))):
        edge = p.get("edge")
        if edge is None:
            out.append({**p, "verdict": DECLINE,
                        "reason": "not scoreable — levels incoherent or no prior"})
            continue
        # Same rank, same ordering, same purpose as intraday_stopping's own
        # floor_only_rank — computed here rather than borrowed because this
        # loop's own edge-descending order (not that function's engine-fair
        # interleave) is the one SWING's verdicts are actually decided by.
        rank = None
        if (bar_before_floor is not None and edge >= bar_before_floor
                and taken_ex_floor < slots_left):
            rank = taken_ex_floor
            taken_ex_floor += 1
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
            v = {**p, "verdict": DECLINE,
                 "reason": f"edge {edge:.4f} below the bar {bar:.4f}"}
            if rank is not None:
                v["floor_only_rank"] = rank
                v["reason"] += (f" (rank {rank + 1} of {slots_left} against the "
                                f"pre-floor bar {bar_before_floor:.4f})")
            out.append(v)
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
