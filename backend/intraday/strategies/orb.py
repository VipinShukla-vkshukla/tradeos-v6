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
                        paise is a quote, not a signal.
  phase                 PRIME only. The same pattern in the 11:00-13:30 drift
                        is the textbook false break.
  cost                  Enforced by the caller — a target inside the round trip
                        is not a trade, however clean the chart.

DIRECTION
---------
Long only, deliberately. Shorting intraday in Indian equities requires MIS and
is squared off by the broker; more importantly, the swing framework this feeds
is long-only, so a short here would create a position the rest of the system
has no vocabulary for.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME
from intraday.strategies.base import Setup, SymbolContext, confirmation_pct


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

        # ── THE "STRENGTH" ARM OF `retest OR strength` — 12-Aug-2026 ─────────
        # This module's own docstring has always specified this filter; only
        # the upper bound above was ever implemented. See
        # base.confirmation_pct() for the full trace and why the floor is the
        # invalidation buffer. The retest arm remains unbuilt: it needs bar
        # history this engine is not given, and claiming it in a docstring
        # while checking neither is what let 0.02% breaks through for weeks.
        min_break = confirmation_pct(range_pct, cfg_float("orb_min_break_frac", 0.10))
        if break_pct < min_break:
            return None                     # a quote, not a break

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
        stop = lo * (1 - cfg_float("orb_stop_buffer_pct", 0.10) / 100.0)
        max_risk = cfg_float("orb_max_risk_pct", 1.20)
        if (ctx.ltp - stop) / ctx.ltp * 100.0 > max_risk:
            stop = ctx.ltp * (1 - max_risk / 100.0)

        risk = ctx.ltp - stop
        if risk <= 0:
            return None
        target = ctx.ltp + risk * cfg_float("orb_target_r", 2.0)

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
            meta={"range_high": round(hi, 2), "range_low": round(lo, 2),
                  "range_pct": round(range_pct, 2), "volume_ratio": vr,
                  "break_pct": round(break_pct, 2)},
        )
