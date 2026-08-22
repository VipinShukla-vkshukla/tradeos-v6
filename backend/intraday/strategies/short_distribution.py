"""
SDN — the short family. Three conditions, one structure: supply overwhelming demand.

WHY ONE FAMILY AND NOT THREE ENGINES
-------------------------------------
Stage 6 merged seven long engines into two families because thirty detections a
quarter cannot settle whether an engine has an edge, and 230 can. A short book
starting from zero has that problem in its most acute form, so it starts merged.
The condition that fired is kept in `meta.sub_engine` and the family can be split
the moment the evidence justifies it — the same contract the ORB and VWR
families run under.

THE THREE CONDITIONS
--------------------
sub_engine labels renamed 20-Aug-2026 — "VWR" and "ORB" here USED to collide
with the standalone long engines of the same name (vwap_reclaim.py, orb.py).
Same three conditions, same code, only the labels changed: VREJ, BRKD, TRP.

**VREJ — VWAP rejection.** Price rallies INTO VWAP from below and is turned away.
This is the highest-quality intraday short in the Indian market and it is not a
mirror of anything: institutional sell programmes are benchmarked to VWAP, so a
desk with stock to move sells there deliberately. Price approaching VWAP from
below and failing is the visible footprint of that. Entry on the rejection, stop
above VWAP (if it reclaims, the thesis is simply wrong), target the session low.

**BRKD — opening-range breakdown.** The first fifteen minutes set a range; price
breaks the LOW of it on expanding volume. The mirror of the ORB long, included
because the structure genuinely is symmetric — an opening range is a two-sided
auction and its resolution has no directional preference.

**TRP — the trap.** Price breaks the previous day's HIGH, fails to hold it, and
falls back inside. THIS ONE HAS NO LONG EQUIVALENT WORTH TRADING and it is the
reason a short book is not just the long book with the signs flipped.

    A failed breakout traps buyers who bought the break. Their stops sit just
    below the level they bought, so the move down has mechanical fuel that an
    ordinary breakdown does not: the sellers are forced rather than
    discretionary. The mirror trade — a failed breakdown trapping shorts — is
    the squeeze this whole module is otherwise built to avoid, which is exactly
    why the asymmetry is real and not an aesthetic preference.

WHAT THE FAMILY REFUSES, AND WHY EACH REFUSAL EXISTS
-----------------------------------------------------
  · **Anything in an uptrend.** `gate_short` in the engine blocks it, but the
    conditions below also require price under VWAP and under the previous close.
    Selling a strong stock because it paused is the single most expensive short
    a retail book can take.
  · **A stock already collapsed.** `shortability.can_short` caps how far a name
    may have already fallen. The fourth down-leg is where a short book gives
    back its month to one bounce.
  · **A stock up on the day.** Every condition requires price below the previous
    close. A short in a name that is green is a bet against demonstrated demand
    AND is the population most likely to lock at its upper circuit, which is the
    one outcome that cannot be stopped out of.
  · **Thin conditions.** Volume confirmation is required on the breakdown
    conditions. A break on no volume is a drift, and a drift reverses.

BEING EARLY IS THE WHOLE EDGE
------------------------------
Every condition requires price to still be WITHIN `intraday_short_max_chase_pct`
of the level that failed. This is not a refinement, it is what makes the rest
work: the stop belongs just above that level, so an entry two percent below it
carries a two percent stop, and no target reachable in one session pays for
that. The first version had no such rule, generated setups at any distance, and
watched the R:R floor discard every one — which reads in the logs as "the engine
found nothing" rather than "the engine arrived late".

It is also the correct trade. A trap is a trade against the buyers stuck AT the
level. Once they have been flushed, what remains is bounce risk.

TARGETS ARE DELIBERATELY MODEST, AND DEPEND ON WHERE THE LOW IS
----------------------------------------------------------------
With room above the session low, the low is the target — the most visible level
on the chart, and where resting bids sit. Reaching past it is how a won trade
becomes a squeeze.

Once price is AT or through the low there is no level left to aim at, so the
target is a measured move from the name's own daily ATR. Using the session low
unconditionally — as the first version did — set a target at or above the
current price on exactly the breakdowns worth taking, produced a reward of a few
paise against a full stop, and refused them all.

Downside is faster than upside intraday, but it is also more likely to snap
back. The cost model has the final word either way: `is_worth_taking` refuses
anything whose target does not clear the round trip by the keep-ratio.

STOPS ROUTED THROUGH base.risk_from_structure() — 19-Aug-2026, FOUND DURING
AN ENGINE AUDIT, NOT REPORTED AS A DEFECT AND FIXED LATER
------------------------------------------------------------------------------
Every other engine in this package calls `risk_from_structure()` for its stop.
This one built its own (`level * (1 + buffer)`, three times, once per
condition) and never called it — which meant SDN, carrying the large majority
of this book's live volume, was the one engine exempt from BOTH F-33's
anti-falsification fix AND `intraday_min_risk_pct` (armed the same session):
neither check runs on a stop this module never handed to the function that
performs them.

Not urgent by SDN's own history — its own risk distribution rarely sits
under the 0.6% floor (7 of 265 TAKEN rows ever have) — but real: any FUTURE
tightening of the shared stop logic would silently keep missing this engine,
and there was no explicit maximum-risk cap here at all, only the indirect
bound `_not_chasing()`'s distance-to-level check happens to produce. Fixed by
routing all three conditions' stops through the same primitive every sibling
engine uses. `intraday_short_max_risk_pct` (1.50, generously above the
observed historical range rather than a routine filter — see this module's
own §"TARGETS ARE DELIBERATELY MODEST" for why SDN's wide-stop trades are
its best, not its worst, unlike the long engines) is new; the shared
`intraday_min_risk_pct` needs no new key, it now simply reaches this engine.

ONE SIDE FINDING, FIXED AS A BYPRODUCT OF GIVING THIS ONE AUDITABLE STOP
CONSTRUCTION INSTEAD OF THREE BESPOKE ONES: `_trap`'s old stop was
`round(min(ctx.day_high, ctx.prev_high * buf) * buf, 2)` — the outer `* buf`
applied a SECOND buffer on top of `prev_high * buf` whenever that branch won
the min. Small (buf is 0.12%, so ~0.024% of stacked, likely unintended,
extra buffer) and now applied exactly once per branch, matching what the
comment at that stop has always said it does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME, DRIFT, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure

NAME = "SDN"


def _retest_and_held_short(bars: list, level: float, tolerance_pct: float) -> bool:
    """
    Pure. The SHORT mirror of orb.py::_retest_and_held() — same contract,
    breakdown instead of breakout — for BRKD, 22-Aug-2026 (F-49).

    WHY A SEPARATE FUNCTION, NOT A `direction` PARAMETER ON THE ORIGINAL.
    This project's own history — `open_positions.direction` (migration
    047), `allocation/proposal.py`'s `Proposal` (no direction field at
    all), the cost-gate call in `evaluate_intraday_setups` — is a repeated
    pattern of a direction-aware function shipping with a LONG default
    that every pre-shorting call site silently inherited. A shared,
    branching version of a function this specific (bar-by-bar, sign-
    sensitive on every comparison) is exactly the shape that risk repeats
    in. Two small, obviously mirrored functions, each hardcoded to its own
    direction, cannot drift into scoring a short as a long.

    A retest is: price already traded BELOW `level` on some earlier CLOSED
    bar (a genuine break, not the bar still forming), THEN came back UP to
    within `tolerance_pct` of `level` (retested the old support as new
    resistance), and every bar from that retest onward closed AT OR BELOW
    `level` (held — the level was not reclaimed from below). One bar
    closing back above `level` after the first touch invalidates the whole
    sequence, the same "one failure kills the sequence" contract as the
    long version.

    INFORMATIONAL ONLY, NEVER A GATE. Unlike ORB, where a weak break
    without a confirmed retest is refused outright, `_range_breakdown`
    below stamps this result into meta and gates on NOTHING — every BRKD
    setup that already clears today's checks keeps clearing them
    regardless of what this returns. The only consumer is `allocation.
    policies._confirmation_key`'s existing PRIORITY tie-break (F-48),
    which the operator was explicit about: rank retest-confirmed
    candidates first when a choice exists, never block an unconfirmed one.
    """
    probed = False
    retested = False
    for b in bars:
        if not probed:
            if b.low < level:
                probed = True
            continue
        if not retested and b.high >= level * (1 - tolerance_pct / 100.0):
            retested = True
        if retested and b.close > level:
            return False
    return retested


def confidence_is_usable(conf: float) -> bool:
    """
    SDN's own confidence runs BACKWARDS against its outcomes (18-Aug-2026).

    Measured on all 265 TAKEN-and-resolved SDN rows, bucketed by the confidence
    the engine assigned at detection:

        confidence      n    STOP%    TGT%   mean gross R
        ------------------------------------------------
        0.55 - 0.62    33    15.2%   42.4%      +0.769
        0.62 - 0.66    44     9.1%   38.6%      +0.880
        0.66 - 0.70    68    36.8%   16.2%      +0.326
        0.70 - 0.75    79    30.4%   27.8%      +0.411
        0.75 +         41    63.4%   12.2%      -0.273

    The top bucket is the only losing one and it stops out four times as often
    as the bottom one. The ordering is monotone from 0.62 upward.

    IT IS WORSE THAN A WASTED FIELD, BECAUSE CONFIDENCE IS A SELECTOR.
    `registry.evaluate_all` sorts by `-s.confidence` and `evaluate_intraday_
    setups` ranks on it, so the detections SDN is most sure about are the ones
    preferentially funded -- and SDN receives most of the paper book's slots
    through `floor_only_rank`. The book was not ignoring a bad signal; it was
    using it upside down. That is the mechanism behind the operator's own
    observation that SDN "fires but does not pick the right trades", which was
    a correct read of the book from the outside, before this was measured.

    SHIPPED INERT, AND THAT IS NOT HEDGING. This is one cut, on one engine,
    found by scanning buckets in a single session, with 41 rows in the bucket
    that carries the decision, and no out-of-sample confirmation. The honest
    form of a finding that strong and that thin is a switch the operator arms
    deliberately -- the same way `intraday_giveback_pct` waited for its own
    calibration rather than borrowing SWING's. Set
    `intraday_short_max_confidence` to 0.75 to act on it.

    The right long-term repair is to the confidence FORMULA -- a score that
    predicts its own failure is mis-specified, not merely mis-thresholded --
    but that needs the per-condition split (VWAP-rejection vs trap vs
    breakdown) this table does not separate.
    """
    cap = cfg_float("intraday_short_max_confidence", 0.0)
    return cap <= 0 or conf <= cap


class ShortDistribution:
    """Supply overwhelming demand, in three recognisable shapes."""

    name = NAME
    # THE DECLARATION MUST NAME REAL PHASES, AND BE ENFORCED — 12-Aug-2026.
    # This read ("OPENING", "MORNING", "DRIFT", "AFTERNOON"). "MORNING" is not
    # a phase this system has — session.py defines OPENING / PRIME / DRIFT /
    # AFTERNOON — and PRIME (09:30-11:00), the window SDN does almost all of
    # its work in, was absent. It did no harm only because this was the one
    # engine of nine that never checked its own `phases`, so the tuple was
    # decorative. That is the project's "a dict key and the key its consumer
    # looks up are two different claims" landmine, armed: adding the guard the
    # other eight engines have — an obvious consistency cleanup — would have
    # silently switched every SDN short off during PRIME, and the logs would
    # have read as a market with no short setups.
    # registry.py:267 also writes this tuple to strategy_config.phases, so
    # until now that column named a phase that does not exist.
    phases = (PRIME, DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        # Now enforced, like every other engine. OPENING (09:15-09:30) is
        # deliberately excluded: it is not in session.TRADEABLE, and the
        # `intraday_short_min_bars` floor below already made SDN unreachable
        # there in practice, so this is behaviour-preserving today and
        # honest tomorrow.
        if phase not in self.phases:
            return None
        # ── Preconditions common to every condition below ────────────────────
        #
        # Checked once, here, rather than three times inside the conditions —
        # and they are the difference between a short book and a way to lose
        # money quickly. All three say the same thing from different angles:
        # this name is under distribution TODAY, not merely disliked.
        if not ctx.ltp or not ctx.prev_close or not ctx.vwap:
            return None
        if len(ctx.bars) < cfg_int("intraday_short_min_bars", 15):
            return None

        # Below the previous close: the name is down on the day.
        if ctx.ltp >= ctx.prev_close:
            return None
        # Below VWAP: the average buyer today is under water, which is what
        # makes further selling self-reinforcing.
        if ctx.ltp >= ctx.vwap:
            return None

        atr = ctx.atr_pct_daily or 1.5
        chg = (ctx.ltp - ctx.prev_close) / ctx.prev_close * 100.0

        for cond in (self._vwap_rejection, self._trap, self._range_breakdown):
            s = cond(ctx, phase, atr, chg)
            if s:
                s.meta.setdefault("family", NAME)
                return s
        return None

    # ── VWR: rallied into VWAP and been turned away ──────────────────────────
    def _vwap_rejection(self, ctx, phase, atr, chg) -> Setup | None:
        near = cfg_float("intraday_short_vwap_near_pct", 0.35) / 100.0
        recent = ctx.bars[-6:]
        if len(recent) < 4:
            return None

        # Did price actually GO to VWAP recently, and get refused? Touching is
        # not enough — the bar must have traded up to it and closed back under.
        touched = [b for b in recent if b.high >= ctx.vwap * (1 - near)]
        if not touched:
            return None
        if recent[-1].close >= ctx.vwap:
            return None

        # The rejection high is the level that failed. Stop goes above it, not
        # above VWAP itself: the level the market actually refused is the honest
        # invalidation, and it is usually tighter.
        rej_high = max(b.high for b in touched)
        if rej_high <= ctx.ltp:
            return None
        # Still near VWAP, or has the move already gone without us?
        if not self._not_chasing(ctx, ctx.vwap):
            return None

        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766). Routed
        # through the shared primitive 19-Aug-2026 -- see this module's own
        # header note on why it previously wasn't.
        structural_stop = round(rej_high * (1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0), 2)
        frame = risk_from_structure(ctx.ltp, structural_stop, "SHORT",
                                    max_risk_pct=cfg_float("intraday_short_max_risk_pct", 1.50))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk
        target = self._target(ctx, atr, stop)
        if not target:
            return None

        conf = 0.58
        if chg < -1.0:
            conf += 0.06                      # already distributing, not just soft
        if len(touched) >= 2:
            conf += 0.08                      # VWAP refused more than once
        vr = ctx.volume_ratio()
        if vr and vr > 1.2:
            conf += 0.06

        if not confidence_is_usable(round(min(conf, 0.92), 2)):
            return None            # see confidence_is_usable()
        return Setup(
            symbol=ctx.symbol, strategy=NAME, direction="SHORT",
            entry=round(ctx.ltp, 2), stop=stop, target=target,
            confidence=round(min(conf, 0.92), 2),
            rationale=(f"rallied into VWAP {ctx.vwap:.2f} and was refused "
                       f"{len(touched)}x (high {rej_high:.2f}), back under at "
                       f"{ctx.ltp:.2f}, {chg:+.2f}% on the day. Institutional sell "
                       f"programmes benchmark to VWAP — this is that footprint"),
            # THE LEVEL THAT FAILED, NOT THE ONE BEHIND IT — 12-Aug-2026.
            # This published VWAP while the stop above is anchored to
            # `rej_high`, and the comment at that stop already says why
            # rej_high is right: "the level the market actually refused is the
            # honest invalidation, and it is usually tighter." Two levels for
            # one thesis, and the engine used the looser one for the exit.
            #
            # rej_high is usually BELOW VWAP (price rallied toward VWAP and was
            # refused before reaching it), so the stop fired first and the
            # invalidation was decoration — registry's reachability check
            # refused the whole condition in production on 12-Aug: TRITURBINE,
            # GESHIP, LUPIN, M&M, HDFCAMC, DRREDDY, HINDPETRO, LAURUSLABS,
            # MARUTI, CHOLAFIN, GABRIEL, ANGELONE, ADANIGREEN, every cycle.
            # VWAP stays in meta as the CONTEXT that makes the rejection
            # meaningful; the thesis dies when the rejection high is reclaimed.
            invalidation=f"reclaims the rejection high {rej_high:.2f}",
            valid_phases=self.phases,
            meta={**frame.meta(), "sub_engine": "VREJ", "vwap": round(ctx.vwap, 2),
                  "rejection_high": round(rej_high, 2),
                  "invalidation_level": round(rej_high, 2)},
        )

    # ── TRP: broke yesterday's high, could not hold it ───────────────────────
    def _trap(self, ctx, phase, atr, chg) -> Setup | None:
        """The one condition with no long equivalent worth trading."""
        if not ctx.prev_high or not ctx.day_high:
            return None
        # It must have ACTUALLY broken the level — a stock that merely approached
        # yesterday's high has trapped nobody.
        if ctx.day_high <= ctx.prev_high:
            return None
        # And it must now be back below it, decisively.
        back_inside = cfg_float("intraday_short_trap_reentry_pct", 0.20) / 100.0
        if ctx.ltp >= ctx.prev_high * (1 - back_inside):
            return None
        # The trapped buyers are AT the level. Below it by more than the chase
        # limit, they have already been flushed and this is a late entry.
        if not self._not_chasing(ctx, ctx.prev_high):
            return None

        # STOP ABOVE THE LEVEL THAT FAILED, NOT ABOVE THE SPIKE.
        #
        # The thesis is "yesterday's high was reclaimed and lost"; it dies when
        # that level is reclaimed, which is prev_high, not the wick above it.
        # Stopping above the spike high made the risk the entire overshoot — on
        # a sharp poke that is a 3% intraday stop, which no target reachable in
        # one session can pay for. The day high is used only when it is the
        # TIGHTER of the two, compared AFTER prev_high's buffer is added (a
        # tiny overshoot smaller than the buffer itself is the case day_high
        # wins) — each candidate buffered exactly once before the comparison,
        # not once more after it. See this module's own header note: the old
        # `min(day_high, prev_high * buf) * buf` applied a second buffer to
        # whichever candidate won when it was prev_high.
        buf = 1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0
        buffered_prev_high = ctx.prev_high * buf
        if ctx.day_high < buffered_prev_high:
            structural_stop = round(ctx.day_high * buf, 2)
        else:
            structural_stop = round(buffered_prev_high, 2)
        if structural_stop <= ctx.ltp:
            return None
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, structural_stop, "SHORT",
                                    max_risk_pct=cfg_float("intraday_short_max_risk_pct", 1.50))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk
        target = self._target(ctx, atr, stop)
        if not target:
            return None

        # How far above the level it poked is how many buyers are trapped.
        overshoot = (ctx.day_high - ctx.prev_high) / ctx.prev_high * 100.0
        conf = 0.62
        if overshoot > 0.5:
            conf += 0.08
        vr = ctx.volume_ratio()
        if vr and vr > 1.3:
            conf += 0.08                      # they bought it in size
        if ctx.rs_vs_index_pct is not None and ctx.rs_vs_index_pct < 0:
            conf += 0.04

        if not confidence_is_usable(round(min(conf, 0.94), 2)):
            return None            # see confidence_is_usable()
        return Setup(
            symbol=ctx.symbol, strategy=NAME, direction="SHORT",
            entry=round(ctx.ltp, 2), stop=stop, target=target,
            confidence=round(min(conf, 0.94), 2),
            rationale=(f"broke yesterday's high {ctx.prev_high:.2f} to {ctx.day_high:.2f} "
                       f"(+{overshoot:.2f}% overshoot) and failed back to {ctx.ltp:.2f}. "
                       f"Everyone who bought that break is now offside with stops "
                       f"under the level — the selling below here is forced, not "
                       f"discretionary"),
            invalidation=f"reclaims {ctx.prev_high:.2f}",
            valid_phases=self.phases,
            meta={**frame.meta(), "sub_engine": "TRP", "prev_high": round(ctx.prev_high, 2),
                  "overshoot_pct": round(overshoot, 2),
                  "invalidation_level": round(ctx.prev_high, 2)},
        )

    # ── ORB: broke the opening range low ─────────────────────────────────────
    def _range_breakdown(self, ctx, phase, atr, chg) -> Setup | None:
        rng = ctx.range_between(0, cfg_int("intraday_orb_minutes", 15))
        if not rng:
            return None
        hi, lo = rng
        if hi <= lo or ctx.ltp >= lo:
            return None
        if not self._not_chasing(ctx, lo):
            return None

        # A break on no volume is a drift, and drifts reverse. Required rather
        # than rewarded: this is the condition most prone to false signals.
        vr = ctx.volume_ratio()
        if not vr or vr < cfg_float("intraday_short_orb_min_vol_ratio", 1.1):
            return None

        structural_stop = round(min(hi, ctx.vwap) * (1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0), 2)
        if structural_stop <= ctx.ltp:
            return None
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, structural_stop, "SHORT",
                                    max_risk_pct=cfg_float("intraday_short_max_risk_pct", 1.50))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk
        target = self._target(ctx, atr, stop)
        if not target:
            return None

        conf = 0.55 + min((vr - 1.1) * 0.15, 0.15)
        if chg < -1.0:
            conf += 0.06

        if not confidence_is_usable(round(min(conf, 0.90), 2)):
            return None            # see confidence_is_usable()

        # RETEST, STAMPED NOT GATED — F-49, 22-Aug-2026. See
        # _retest_and_held_short's own docstring for the full contract and
        # why this never returns None the way ORB's equivalent can: the
        # operator was explicit that BRKD should get a PRIORITY signal
        # (allocation.policies._confirmation_key, F-48), not a second gate
        # on top of the volume/structure/confidence checks already above.
        retest_confirmed = _retest_and_held_short(
            ctx.bars_after(cfg_int("intraday_orb_minutes", 15)), lo,
            cfg_float("intraday_short_breakdown_retest_tolerance_pct", 0.15))

        return Setup(
            symbol=ctx.symbol, strategy=NAME, direction="SHORT",
            entry=round(ctx.ltp, 2), stop=stop, target=target,
            confidence=round(min(conf, 0.90), 2),
            rationale=(f"lost the opening range low {lo:.2f} at {ctx.ltp:.2f} on "
                       f"{vr:.1f}x volume, {chg:+.2f}% on the day and under VWAP "
                       f"{ctx.vwap:.2f}"),
            invalidation=f"closes back above the range low {lo:.2f}",
            valid_phases=self.phases,
            meta={**frame.meta(), "sub_engine": "BRKD", "range_low": round(lo, 2),
                  "range_high": round(hi, 2), "volume_ratio": vr,
                  "retest_confirmed": retest_confirmed,
                  "invalidation_level": round(lo, 2)},
        )

    # ── shared: are we still near the level, or chasing it? ──────────────────
    def _not_chasing(self, ctx, level: float) -> bool:
        """
        Is price still close enough to the level that failed?

        THE RULE THAT MAKES THE OTHERS WORK. Without it each condition fires at
        whatever price happens to be when the scan runs, which on a fast move is
        well below the level — and then the stop, which belongs just above that
        level, is enormous, the distance to any sensible target is already
        spent, and the R:R floor refuses the trade. Every setup was being
        generated and then discarded on arithmetic, which reads in the logs as
        "the engine found nothing" rather than as "the engine arrived late".

        It is also the correct trade. A trap is a trade against the buyers stuck
        AT the level; two percent below it they have already been flushed, and
        what is left is bounce risk. The edge is in being early, and an engine
        that cannot be early should not take the trade at all.
        """
        if not level or not ctx.ltp:
            return False
        return abs(ctx.ltp - level) / level * 100.0 <= cfg_float(
            "intraday_short_max_chase_pct", 0.60)

    # ── shared target construction ───────────────────────────────────────────
    def _target(self, ctx, atr: float, stop: float) -> float | None:
        """
        Where buyers are expected to step in — a different level depending on
        whether the session low is still ahead of us or already behind.

        WITH ROOM ABOVE THE LOW: the session low is the target. It is the most
        visible level on the chart and where resting bids sit, so reaching past
        it is how a won trade becomes a squeeze.

        ALREADY AT OR THROUGH THE LOW: there is no level left to aim at, so the
        target is a measured move from the name's own daily ATR. Taking the
        session low here — which the first version did, unconditionally — set a
        target at or above the current price, produced a reward of a few paise
        against a full stop, and silently refused every genuine breakdown
        continuation.

        The cost model still has the last word: is_worth_taking refuses anything
        whose target does not clear the round trip by the keep-ratio.
        """
        cap_pct = atr * cfg_float("intraday_short_target_atr_mult", 0.55)
        by_atr = ctx.ltp * (1 - cap_pct / 100.0)

        room = cfg_float("intraday_short_low_room_pct", 0.20) / 100.0
        if ctx.day_low and ctx.day_low < ctx.ltp * (1 - room):
            target = max(by_atr, ctx.day_low)      # max == nearest, moving down
        else:
            target = by_atr
        target = round(target, 2)

        risk = stop - ctx.ltp
        reward = ctx.ltp - target
        if risk <= 0 or reward <= 0:
            return None
        if reward / risk < cfg_float("intraday_short_min_rr", 1.3):
            return None
        return target
