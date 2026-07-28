"""
PDL — previous-day high break and retest.

WHY PDH IS THE MOST-WATCHED LEVEL IN INDIAN INTRADAY
-----------------------------------------------------
Every charting package draws it, every desk marks it, and every overnight
position is measured against it. That shared attention is the entire edge: the
level matters because everyone agrees it matters, which makes reactions there
predictable in a way that an arbitrary price is not.

BREAK AND RETEST, NOT BREAK
---------------------------
Buying the break itself competes with every stop order sitting above the level —
you get filled in a spike and are immediately underwater when it fades. Waiting
for price to come back, touch the level from above and hold turns the old
resistance into support and gives a stop that is both tight and meaningful:
below the level, the break has failed, and there is nothing left to argue about.

That is also why this engine is patient. Most PDH breaks never retest cleanly,
and this returns nothing on those days rather than lowering the bar.

RELATIONSHIP TO ORB
-------------------
ORB trades a level created TODAY, in the first fifteen minutes. This trades a
level created YESTERDAY, by a full session. They frequently point at the same
stock, and when they do the registry records the agreement — two independent
structures aligning is genuinely more than either alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME, DRIFT, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext


class PrevDayLevelRetest:
    name = "PDL"
    phases = (PRIME, DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.prev_high and ctx.ltp and ctx.bars):
            return None

        pdh = ctx.prev_high
        bars = ctx.bars
        if len(bars) < 6:
            return None

        # 1. The level must have been broken decisively at some point today.
        broke = [b for b in bars if b.high > pdh * (1 + cfg_float("pdl_break_margin_pct", 0.08) / 100.0)]
        if not broke:
            return None

        # 2. Price must now be back AT the level, not far above it.
        tol = cfg_float("pdl_retest_tol_pct", 0.30) / 100.0
        dist = (ctx.ltp - pdh) / pdh
        if dist < 0:
            return None                      # lost the level — no longer support
        if dist > tol:
            return None                      # never came back; nothing to retest

        # 3. The retest must be HOLDING: the most recent bars have to have found
        #    support, not be mid-collapse through the level.
        look = bars[-cfg_int("pdl_hold_bars", 3):]
        if any(b.close < pdh * (1 - cfg_float("pdl_fail_margin_pct", 0.15) / 100.0) for b in look):
            return None

        # 4. A retest on rising volume is distribution, not support. The good
        #    version comes back quietly.
        vr = ctx.volume_ratio()
        max_vr = cfg_float("pdl_max_retest_volume_ratio", 2.5)
        if vr is not None and vr > max_vr:
            return None

        stop = pdh * (1 - cfg_float("pdl_stop_buffer_pct", 0.25) / 100.0)
        risk = ctx.ltp - stop
        if risk <= 0:
            return None
        max_risk = cfg_float("pdl_max_risk_pct", 0.80)
        if risk / ctx.ltp * 100.0 > max_risk:
            stop = ctx.ltp * (1 - max_risk / 100.0)
            risk = ctx.ltp - stop

        day_hi = ctx.day_high or ctx.ltp
        by_r = ctx.ltp + risk * cfg_float("pdl_target_r", 2.5)
        target = max(by_r, day_hi * 1.002)

        conf = 0.55
        if dist < tol * 0.4:
            conf += 0.12                     # right on the level, best price
        if vr and vr < 1.2:
            conf += 0.10                     # quiet retest, the healthy kind
        if ctx.rs_vs_index_pct and ctx.rs_vs_index_pct > 0:
            conf += min(0.12, ctx.rs_vs_index_pct * 0.04)
        conf = round(min(0.93, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Broke yesterday's high {pdh:.2f} and came back to retest it "
                       f"({dist * 100:.2f}% above), holding"
                       + (f" on quiet {vr:.1f}× volume" if vr else "")),
            invalidation=(f"a close back under {pdh:.2f} — old resistance failing to become "
                          f"support means the break was a spike, not a decision"),
            valid_phases=self.phases,
            meta={"pdh": round(pdh, 2), "dist_pct": round(dist * 100, 3), "volume_ratio": vr},
        )
