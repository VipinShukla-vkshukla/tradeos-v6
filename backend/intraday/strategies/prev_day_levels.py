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
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure


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

        # 2. Price must now be back NEAR the level, not far above it.
        #
        # Widened from 0.30% to 0.55%, because 0.30% left no band at all once
        # step 4 required the thesis to have more room than the round trip
        # costs: that needs ~0.29% above the level, and a 0.29-0.30% window is a
        # setup that can never fire.
        #
        # The wider tolerance also changes the trade for the better. Buying
        # exactly at the level means buying where the noise lives and where
        # every other stop sits; a retest that has held and lifted slightly is
        # the same thesis with evidence that it worked.
        tol = cfg_float("pdl_retest_tol_pct", 0.55) / 100.0
        dist = (ctx.ltp - pdh) / pdh
        if dist < 0:
            return None                      # lost the level — no longer support
        if dist > tol:
            return None                      # never came back; nothing to retest

        # 3. The retest must be HOLDING: the most recent bars have to have found
        #    support, not be mid-collapse through the level.
        #
        # The fail margin is clamped BELOW the exit policy's invalidation buffer,
        # and that is not a detail. This engine called a retest "holding" while a
        # bar closed 0.15% under the level; exit_policy declares the setup dead
        # at 0.12% under. So a setup could be created in a state the exit engine
        # already considered invalid — born dead, alerted, entered, and cut on
        # the next tick. On 30 Jul PDL produced 25 of 41 detections and hit
        # target zero times, with both losses exiting SETUP_INVALIDATED inside
        # 0.4% of entry.
        #
        # Two components disagreeing about the same level is worse than either
        # threshold being wrong, because no value of one fixes the other.
        inval_buf = cfg_float("intraday_invalidation_buffer_pct", 0.12)
        fail_pct = min(cfg_float("pdl_fail_margin_pct", 0.15), inval_buf * 0.75)
        look = bars[-cfg_int("pdl_hold_bars", 3):]
        if any(b.close < pdh * (1 - fail_pct / 100.0) for b in look):
            return None

        # 4. THE THESIS NEEDS MORE ROOM THAN THE TRADE COSTS.
        #
        # Entry sits 0 to 0.30% above the level; invalidation triggers 0.12%
        # below it. Enter right at the level and the thesis has 0.12% of room
        # against a 0.21% round trip — noise smaller than your own friction
        # ends the trade, and no stop placement can rescue that.
        #
        # So the distance from entry down to the invalidation level must exceed
        # the round trip by a margin. In practice this means PDL only takes the
        # retest once price has lifted clearly off the level, which is also the
        # version that was working.
        from intraday.cost_model import round_trip as _rt
        inval_level = pdh * (1 - inval_buf / 100.0)
        room_pct = (ctx.ltp - inval_level) / ctx.ltp * 100.0
        cost_pct = _rt(ctx.ltp, max(1, int(6000 // ctx.ltp))).pct_of_position
        need = cost_pct * cfg_float("pdl_min_room_x_cost", 2.0)
        if room_pct < need:
            return None

        # 5. A retest on rising volume is distribution, not support. The good
        #    version comes back quietly.
        vr = ctx.volume_ratio()
        max_vr = cfg_float("pdl_max_retest_volume_ratio", 2.5)
        if vr is not None and vr > max_vr:
            return None

        # Stop below the INVALIDATION level, not merely below the PDH.
        #
        # The stop sat 0.25% under the level while invalidation fires at 0.12%
        # under, so invalidation always triggered first and the stop was
        # decorative — every PDL exit was an invalidation, never a stop, and the
        # R the position was sized against was never the R that could be lost.
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(
            ctx.ltp, min(pdh * (1 - cfg_float("pdl_stop_buffer_pct", 0.25) / 100.0),
                         inval_level * (1 - 0.05 / 100.0)),
            "LONG", max_risk_pct=cfg_float("pdl_max_risk_pct", 0.80))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

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
