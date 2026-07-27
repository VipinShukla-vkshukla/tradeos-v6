"""
VWAP reclaim — the one thing that works in the midday drift.

WHY VWAP AND NOT A MOVING AVERAGE
---------------------------------
VWAP is the volume-weighted average price since the open, so it is literally
the average price paid by everyone trading today. Institutional desks are
measured against it. That gives it a property no moving average has: it is a
level people are obliged to care about, which is why price returns to it and
why reclaiming it after losing it marks a genuine change of control.

THE SETUP
---------
Price opens strong, fades below VWAP during the drift, then reclaims it and
holds. The fade shakes out weak longs; the reclaim says the sellers who pushed
it under have been absorbed. Entry is on the reclaim, stop below the swing low
that formed under VWAP, target back toward the day's high.

WHY IT IS THE DRIFT STRATEGY
----------------------------
Between 11:00 and 13:30 volume decays and ranges compress. Breakout systems
generate their worst signals here because there is no flow behind a break. Mean
reversion to a level everyone watches is the behaviour that survives thin
conditions — the same thinness that makes breaks fail makes reversion reliable.

WHAT IT REFUSES
---------------
A reclaim inside a downtrend is a bounce, not a change of control. Requires the
stock to be positive on the day and holding above the previous close, so the
trade is "strong stock that dipped", not "weak stock that paused".
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import DRIFT, PRIME, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext


class VwapReclaim:
    name = "VWR"
    phases = (PRIME, DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.vwap and ctx.ltp and ctx.bars):
            return None

        lookback = cfg_int("vwr_lookback_bars", 12)
        recent = ctx.bars[-lookback:]
        if len(recent) < 4:
            return None

        # The shape: price must have spent time BELOW vwap recently and be above
        # it now. Without the "spent time below" the trade is just "price is
        # above average", which is not a setup, it is a description.
        below = [b for b in recent if b.close < ctx.vwap]
        min_below = cfg_int("vwr_min_bars_below", 3)
        if len(below) < min_below:
            return None
        if ctx.ltp <= ctx.vwap:
            return None

        # The reclaim must be recent, not something that happened an hour ago.
        if recent[-1].close < ctx.vwap or recent[-2].close > ctx.vwap:
            # Current bar must be above and the prior one below — the crossing
            # itself. Anything else is chasing an established move.
            if not (recent[-1].close > ctx.vwap and any(
                    b.close < ctx.vwap for b in recent[-4:-1])):
                return None

        reclaim_pct = (ctx.ltp - ctx.vwap) / ctx.vwap * 100.0
        if reclaim_pct > cfg_float("vwr_max_extension_pct", 0.45):
            return None                    # already extended past the level

        # Context filter: only reclaim in a stock that is actually strong today.
        if ctx.prev_close and ctx.ltp < ctx.prev_close:
            return None
        if ctx.rs_vs_index_pct is not None and ctx.rs_vs_index_pct < 0:
            return None

        # Stop under the low made while below VWAP — that low is where the
        # sellers were, and losing it means they were not absorbed after all.
        swing_low = min(b.low for b in below) if below else min(b.low for b in recent)
        stop = swing_low * (1 - cfg_float("vwr_stop_buffer_pct", 0.08) / 100.0)
        max_risk = cfg_float("vwr_max_risk_pct", 0.90)
        if (ctx.ltp - stop) / ctx.ltp * 100.0 > max_risk:
            stop = ctx.ltp * (1 - max_risk / 100.0)

        risk = ctx.ltp - stop
        if risk <= 0:
            return None

        # Target the day's high — the level the stock already proved it can
        # reach — rather than an abstract multiple, falling back to R when the
        # high is too close to be worth the trip.
        by_r = ctx.ltp + risk * cfg_float("vwr_target_r", 2.0)
        target = max(by_r, (ctx.day_high or 0) * 1.001) if ctx.day_high else by_r

        conf = 0.45
        vr = ctx.volume_ratio()
        if vr and vr > 1.0:
            conf += min(0.2, (vr - 1.0) * 0.2)
        if ctx.rs_vs_index_pct:
            conf += min(0.2, ctx.rs_vs_index_pct * 0.06)
        if len(below) >= min_below + 2:
            conf += 0.08                   # a proper flush, not a one-bar dip
        conf = round(min(0.92, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Reclaimed VWAP {ctx.vwap:.2f} after {len(below)} bars below it; "
                       f"holding {reclaim_pct:.2f}% above"
                       + (f", volume {vr:.1f}×" if vr else "")),
            invalidation=(f"a close back under VWAP {ctx.vwap:.2f} — the reclaim failing "
                          f"means the sellers were not absorbed, and that is knowable "
                          f"before the stop at {stop:.2f}"),
            valid_phases=self.phases,
            meta={"vwap": round(ctx.vwap, 2), "bars_below": len(below),
                  "swing_low": round(swing_low, 2), "volume_ratio": vr},
        )
