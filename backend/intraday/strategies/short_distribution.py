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
**VWR — VWAP rejection.** Price rallies INTO VWAP from below and is turned away.
This is the highest-quality intraday short in the Indian market and it is not a
mirror of anything: institutional sell programmes are benchmarked to VWAP, so a
desk with stock to move sells there deliberately. Price approaching VWAP from
below and failing is the visible footprint of that. Entry on the rejection, stop
above VWAP (if it reclaims, the thesis is simply wrong), target the session low.

**ORB — opening-range breakdown.** The first fifteen minutes set a range; price
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
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME, DRIFT, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext

NAME = "SDN"


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

        stop = round(rej_high * (1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0), 2)
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
            meta={"sub_engine": "VWR", "vwap": round(ctx.vwap, 2),
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
        # TIGHTER of the two.
        buf = 1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0
        stop = round(min(ctx.day_high, ctx.prev_high * buf) * buf, 2)
        if stop <= ctx.ltp:
            return None
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
            meta={"sub_engine": "TRP", "prev_high": round(ctx.prev_high, 2),
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

        stop = round(min(hi, ctx.vwap) * (1 + cfg_float("intraday_short_stop_buffer_pct", 0.12) / 100.0), 2)
        if stop <= ctx.ltp:
            return None
        target = self._target(ctx, atr, stop)
        if not target:
            return None

        conf = 0.55 + min((vr - 1.1) * 0.15, 0.15)
        if chg < -1.0:
            conf += 0.06

        return Setup(
            symbol=ctx.symbol, strategy=NAME, direction="SHORT",
            entry=round(ctx.ltp, 2), stop=stop, target=target,
            confidence=round(min(conf, 0.90), 2),
            rationale=(f"lost the opening range low {lo:.2f} at {ctx.ltp:.2f} on "
                       f"{vr:.1f}x volume, {chg:+.2f}% on the day and under VWAP "
                       f"{ctx.vwap:.2f}"),
            invalidation=f"closes back above the range low {lo:.2f}",
            valid_phases=self.phases,
            meta={"sub_engine": "ORB", "range_low": round(lo, 2),
                  "range_high": round(hi, 2), "volume_ratio": vr,
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
