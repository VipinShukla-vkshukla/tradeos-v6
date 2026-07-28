"""
PBK — the first pullback in a trend day.

THE HIGHEST-QUALITY INTRADAY ENTRY THERE IS
-------------------------------------------
Breakouts pay you for being right about a level. Pullbacks pay you for being
right about a trend, and they do it at a better price with a tighter stop —
which matters enormously here, because a tighter stop is what lets a 0.8% target
clear a 0.21% round trip.

A trend day has a signature: price holds above VWAP essentially all session,
each dip is bought at a higher low, and the stock leads its index. The first
controlled pullback to the anchor in that structure is where the people who
missed the open get in. The trade is entering with them, not before them.

WHY IT REFUSES A SECOND AND THIRD PULLBACK
------------------------------------------
Each successive test of an anchor is weaker — the buyers who were waiting have
been filled. Counting touches and stopping after the configured limit is the
difference between trading a trend and trading its exhaustion. Most systems that
"buy dips to VWAP" have no such counter and end up buying the dip that finally
breaks.

WHY IT DOES NOT USE A MOVING AVERAGE ALONE
------------------------------------------
An EMA is a derived line nobody is obliged to defend. VWAP is where the day's
money actually sits. The anchor here is VWAP, with the recent higher-low
structure as confirmation that the pullback is controlled rather than a
breakdown that has not finished yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext


class TrendPullback:
    name = "PBK"
    phases = (PRIME, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.vwap and ctx.ltp and ctx.bars and ctx.day_high):
            return None

        bars = ctx.bars
        if len(bars) < cfg_int("pbk_min_bars", 15):
            return None

        # 1. It must actually be a trend day: price above VWAP for most of it.
        above = sum(1 for b in bars if b.close >= ctx.vwap)
        frac_above = above / len(bars)
        if frac_above < cfg_float("pbk_min_frac_above_vwap", 0.70):
            return None

        # 2. Price must be pulling back INTO the anchor, not sitting at highs
        #    (nothing to buy) and not below it (no longer a pullback).
        dist_vwap = (ctx.ltp - ctx.vwap) / ctx.vwap * 100.0
        if dist_vwap < 0:
            return None
        if dist_vwap > cfg_float("pbk_max_dist_vwap_pct", 0.45):
            return None
        off_high = (ctx.day_high - ctx.ltp) / ctx.day_high * 100.0
        if off_high < cfg_float("pbk_min_off_high_pct", 0.30):
            return None                      # still at the high, no pullback yet

        # 3. Count how many times the anchor has already been tested. Each test
        #    spends some of the demand that makes this work.
        touches, armed = 0, False
        tol = cfg_float("pbk_touch_tol_pct", 0.25) / 100.0
        for b in bars:
            near = abs(b.low - ctx.vwap) / ctx.vwap <= tol
            if near and not armed:
                touches += 1
                armed = True
            elif (b.low - ctx.vwap) / ctx.vwap > tol * 2:
                armed = False
        if touches > cfg_int("pbk_max_touches", 2):
            return None

        # 4. Higher-low structure — the pullback is controlled.
        recent = bars[-cfg_int("pbk_structure_bars", 10):]
        lows = [b.low for b in recent]
        if len(lows) >= 4 and min(lows[-3:]) < min(lows[:3]) * (1 - tol):
            return None                      # making lower lows, not a pullback

        # Stop just under the anchor: losing VWAP on a trend day is the trend
        # ending, so the stop and the invalidation are the same event — which is
        # exactly what makes this entry efficient.
        stop = ctx.vwap * (1 - cfg_float("pbk_stop_buffer_pct", 0.15) / 100.0)
        risk = ctx.ltp - stop
        if risk <= 0:
            return None
        max_risk = cfg_float("pbk_max_risk_pct", 0.80)
        if risk / ctx.ltp * 100.0 > max_risk:
            stop = ctx.ltp * (1 - max_risk / 100.0)
            risk = ctx.ltp - stop

        # Target the day high first — proven reachable — then extend by R.
        by_r = ctx.ltp + risk * cfg_float("pbk_target_r", 2.5)
        target = max(by_r, ctx.day_high * 1.002)

        conf = 0.55
        conf += min(0.15, (frac_above - 0.70) * 0.5)
        if touches == 1:
            conf += 0.12                     # first test is the good one
        if ctx.rs_vs_index_pct and ctx.rs_vs_index_pct > 0.5:
            conf += min(0.15, ctx.rs_vs_index_pct * 0.05)
        conf = round(min(0.95, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Trend day — {frac_above:.0%} of bars above VWAP {ctx.vwap:.2f}; "
                       f"pullback #{touches} to the anchor, {off_high:.2f}% off the high"),
            invalidation=(f"losing VWAP {ctx.vwap:.2f} on a closing basis — on a trend day "
                          f"that is the trend ending, not a dip"),
            valid_phases=self.phases,
            meta={"frac_above_vwap": round(frac_above, 2), "touches": touches,
                  "dist_vwap_pct": round(dist_vwap, 2), "off_high_pct": round(off_high, 2)},
        )
