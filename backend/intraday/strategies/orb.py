"""
ORB — Opening Range Breakout.

THE IDEA
--------
The first fifteen minutes of an Indian session absorb every overnight order,
gap adjustment and institutional rebalance. The high and low of that window are
not arbitrary: they are the prices at which the day's first real disagreement
was settled. A decisive break of that range, on volume, is the market resolving
that disagreement — and it tends to keep going.

WHY IT IS GATED SO HARD
-----------------------
ORB is the most over-traded intraday pattern in existence, and the reason it
disappoints is that most implementations take EVERY break. The filters here
exist because each one corresponds to a specific way the naive version loses:

  volume confirmation   A break on thin volume is a liquidity artefact, not a
                        decision. Requires the session to be running above its
                        own time-adjusted average.
  range sanity          A range wider than the stock's usual daily move means
                        the "breakout" is already an extended day — nothing is
                        left. A range that is too tight is noise and will be
                        broken in both directions within minutes.
  retest OR strength    Either the break has real distance behind it, or price
                        has come back to the level and held. A break by two
                        paise is a quote, not a signal. BOTH ARMS BUILT
                        19-Aug-2026 — the retest half was documented and
                        skipped for months on the belief it needed bar
                        history this engine was not given (`_retest_and_held`,
                        checked and found untrue: `ctx.bars` was always
                        there). Not a relaxation — strength-confirmed setups
                        are unaffected; retest only rescues a setup that would
                        otherwise be refused as a quote.
  phase                 PRIME only. The same pattern in the 11:00-13:30 drift
                        is the textbook false break.
  cost                  Enforced by the caller — a target inside the round trip
                        is not a trade, however clean the chart.

TARGET
------
`max(entry + risk x orb_target_r, entry + range_height)` since 19-Aug-2026 —
the range this engine already requires to be "meaningful relative to how much
the stock actually moves" is itself a legitimate distance to project forward,
matching squeeze.py's own measured-move target. Can only widen a target the
flat multiple already set, never shrink one.

NOT YET DONE — 19-Aug-2026. Regime awareness (this pattern is a trend-
continuation signal and the literature is consistent that it works in
trending tape, fails in chop) was named the same session as the two fixes
above and deliberately NOT built: the mechanism for it already exists
(`allocation.scoring.regime_fit_multiplier`) and ships at weight 0.0 precisely
because arming a regime effect on theory rather than measurement is the
mistake `hurdle.py`'s own docstring already paid for twice (05-Aug, 10-Aug
STRONG-bucket self-reference failures). This is a gap named, not closed.

DIRECTION
---------
Long only, deliberately. Shorting intraday in Indian equities requires MIS and
is squared off by the broker; more importantly, the swing framework this feeds
is long-only, so a short here would create a position the rest of the system
has no vocabulary for. This also means ORB structurally cannot take the other
half of its own pattern family — an opening-range BREAKDOWN — which the
literature treats as symmetric to the breakout. Named, not addressed here:
building it is a new engine (SDN already owns intraday shorts architecturally
via can_short()/the cover-deadline runway), not a fix to this one.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_bool, cfg_float, cfg_int
from intraday.session import PRIME
from intraday.strategies.base import Setup, SymbolContext, confirmation_pct, risk_from_structure


def _retest_and_held(bars: list, level: float, tolerance_pct: float) -> bool:
    """
    Pure. The RETEST arm of this module's own "retest OR strength" docstring,
    built 19-Aug-2026 — the STRENGTH arm (break_pct >= min_break) is the only
    one that ever existed; this was documented and never written, and the
    reason given (`bars_after()` "not given" to this engine) was checked and
    found stale. `ctx.bars` was always there; `range_between()` two lines
    above already reads it.

    A retest is: price already traded ABOVE `level` on some earlier closed
    bar (a genuine probe, not the bar that is currently forming), THEN came
    back down to within `tolerance_pct` of `level` (tested the old resistance
    as new support), and every bar from that retest onward closed at or above
    `level` (held — the level was not reclaimed). One failed retest — any bar
    after the first touch closing back below `level` — invalidates the whole
    sequence; a level that gets reclaimed once is not "held" because a LATER
    bar happens to recover.

    Only CLOSED bars. `ctx.ltp` (the current, still-forming price) is checked
    separately by the caller against `hi` — this function has no opinion on
    the live tick, only on what has already printed.
    """
    probed = False
    retested = False
    for b in bars:
        if not probed:
            if b.high > level:
                probed = True
            continue
        if not retested and b.low <= level * (1 + tolerance_pct / 100.0):
            retested = True
        if retested and b.close < level:
            return False
    return retested


class OpeningRangeBreakout:
    name = "ORB"
    phases = (PRIME,)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None

        window = cfg_int("orb_window_min", 15)
        rng = ctx.range_between(0, window)
        if not rng or not ctx.ltp:
            return None
        hi, lo = rng
        if hi <= lo:
            return None

        range_pct = (hi - lo) / lo * 100.0

        # A range must be meaningful relative to how much this stock actually
        # moves. Both bounds matter: too tight is noise, too wide means the
        # day's move has already happened inside the opening window.
        atr = ctx.atr_pct_daily or 2.0
        min_range = cfg_float("orb_min_range_frac", 0.15) * atr
        max_range = cfg_float("orb_max_range_frac", 0.70) * atr
        if range_pct < min_range:
            return None
        if range_pct > max_range:
            return None

        if ctx.ltp <= hi:
            return None                     # no break yet

        break_pct = (ctx.ltp - hi) / hi * 100.0
        max_chase = cfg_float("orb_max_chase_pct", 0.60)
        if break_pct > max_chase:
            return None                     # the move is already spent

        # ── "STRENGTH" — 12-Aug-2026 ──────────────────────────────────────
        # See base.confirmation_pct() for the full trace and why the floor is
        # the invalidation buffer.
        min_break = confirmation_pct(range_pct, cfg_float("orb_min_break_frac", 0.10))
        retest_ok = False
        if break_pct < min_break:
            # ── "RETEST" — the arm this module's docstring named on
            # 12-Aug-2026 and left unbuilt, closed 19-Aug-2026. The reason
            # given for skipping it ("needs bar history this engine is not
            # given") was checked, not assumed, and found false: ctx.bars is
            # the same field range_between() already reads two lines up.
            #
            # A weak break is not automatically a bad one — literature on this
            # pattern treats a RETESTED breakout as the higher-confidence
            # signal, not a consolation prize for missing the strength bar.
            # This is an ALTERNATIVE path to confirmation, not a relaxation of
            # the existing one: strength-confirmed setups are entirely
            # unaffected, this only rescues setups that would otherwise be
            # refused as "a quote, not a break".
            if cfg_bool("orb_retest_confirmation_enabled", True):
                retest_ok = _retest_and_held(
                    ctx.bars_after(window), hi,
                    cfg_float("orb_retest_tolerance_pct", 0.15))
            if not retest_ok:
                return None                 # neither arm confirmed — a quote

        vr = ctx.volume_ratio()
        min_vr = cfg_float("orb_min_volume_ratio", 1.20)
        if vr is not None and vr < min_vr:
            return None

        # Never fight the day's own structure: a break that is still below the
        # previous day's high is breaking into overhead supply.
        if ctx.prev_high and ctx.ltp < ctx.prev_high:
            supply_note = f" (still under PDH {ctx.prev_high:.2f})"
            if cfg_float("orb_require_above_pdh", 1.0) >= 1.0:
                return None
        else:
            supply_note = ""

        # Stop below the range low, not below the breakout level: the range is
        # the structure, and a stop at the break line is inside the noise that
        # created it. Capped so a wide range does not produce an unaffordable
        # stop.
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, lo * (1 - cfg_float("orb_stop_buffer_pct", 0.10) / 100.0), "LONG",
                                    max_risk_pct=cfg_float("orb_max_risk_pct", 1.20))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

        # MEASURED-MOVE TARGET — 19-Aug-2026, matching squeeze.py's own
        # `measured = ctx.ltp + (p_hi - p_lo)`, the sibling breakout engine in
        # this same codebase. A range this engine already required to be
        # "meaningful relative to how much this stock actually moves" (the
        # ATR-scaled sanity check above) is itself a legitimate distance to
        # project forward — classic ORB practice targets the range's own
        # height, not only a flat R-multiple that has no relationship to the
        # structure that produced the trade. `max()`, never a replacement:
        # this can only WIDEN the target, never shrink one the flat multiple
        # already set, so no existing trade's target gets closer.
        by_r = ctx.ltp + risk * cfg_float("orb_target_r", 2.0)
        target = by_r
        if cfg_bool("orb_measured_move_target_enabled", True):
            measured = ctx.ltp + (hi - lo)
            target = max(by_r, measured)

        # Confidence from the two things that actually predict follow-through.
        conf = 0.5
        if vr:
            conf += min(0.25, (vr - min_vr) * 0.15)
        if ctx.rs_vs_index_pct and ctx.rs_vs_index_pct > 0:
            conf += min(0.15, ctx.rs_vs_index_pct * 0.05)
        # Early WITHIN THE CONFIRMED BAND. The previous form (`break_pct <
        # 0.2`) paid its largest bonus to the two-paise breaks the gate above
        # now refuses outright — the engine's highest-confidence ORBs were its
        # least confirmed ones. "Early" can only mean early relative to the
        # range of breaks this engine is willing to take at all.
        if break_pct < (min_break + max_chase) / 2:
            conf += 0.10                    # confirmed, but not yet chased
        conf = round(min(0.95, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Broke the {window}-min opening range "
                       f"({lo:.2f}–{hi:.2f}, {range_pct:.2f}% wide) by {break_pct:.2f}%"
                       + (f" on {vr:.1f}× volume" if vr else "") + supply_note),
            invalidation=(f"a close back below {hi:.2f} — the break failing is the "
                          f"thesis dying, and it happens well before the stop"),
            valid_phases=self.phases,
            # `retest_confirmed`/`measured_move_used` are stamped, not just
            # assumed to help. This project's own rule: instrument first,
            # calibrate second (F-33's ATR stamp is the precedent) — whether
            # a retest-confirmed ORB actually outperforms a strength-only one
            # is a real, open, MEASURABLE question, not a claim being made
            # here. Nothing reads these fields yet; they exist so a future
            # session can ask.
            meta={**frame.meta(), "range_high": round(hi, 2), "range_low": round(lo, 2),
                  "range_pct": round(range_pct, 2), "volume_ratio": vr,
                  "break_pct": round(break_pct, 2),
                  "retest_confirmed": retest_ok,
                  "measured_move_used": target > by_r + 1e-9},
        )
