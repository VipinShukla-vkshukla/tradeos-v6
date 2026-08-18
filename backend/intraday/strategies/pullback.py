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
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure


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
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, ctx.vwap * (1 - cfg_float("pbk_stop_buffer_pct", 0.15) / 100.0), "LONG",
                                    max_risk_pct=cfg_float("pbk_max_risk_pct", 0.80))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

        # Target the day high — proven reachable — falling back to an R
        # multiple only when the high is too close to be worth the trip.
        #
        # THIS WAS `max(by_r, day_high * 1.002)` UNTIL 12-Aug-2026, which is
        # the exact defect vwap_reclaim.py had fixed on 10-Aug and which was
        # never carried across to its sibling. Taking the LARGER of the two
        # means that whenever 2.5R sits beyond the day high — the common case
        # on a pullback, since the high was made earlier and price has since
        # come back — the target silently becomes a price the stock has never
        # traded at, past a level it has actually proven. exit_policy's
        # `use_setup_target` exits AT this field, so a target never touched is
        # a trade that rides the give-back guard down instead of banking the
        # move it genuinely had.
        by_r = ctx.ltp + risk * cfg_float("pbk_target_r", 2.5)
        min_worthwhile = ctx.ltp + risk * cfg_float("pbk_min_target_r", 1.0)
        if ctx.day_high * 1.002 >= min_worthwhile:
            target = ctx.day_high * 1.002
        else:
            target = by_r

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
            # invalidation_level DECLARED — 12-Aug-2026. This meta published no
            # key exit_policy.invalidation_level_from() recognised, so the
            # "losing VWAP on a closing basis" clause above was prose only and
            # the invalidation check never fired on a PBK trade. VWAP is the
            # anchor the whole thesis rests on; it is also where the stop sits,
            # which is what makes this entry efficient.
            meta={"frac_above_vwap": round(frac_above, 2), "touches": touches,
                  "vwap": round(ctx.vwap, 2), "invalidation_level": round(ctx.vwap, 2),
                  "dist_vwap_pct": round(dist_vwap, 2), "off_high_pct": round(off_high, 2)},
        )
