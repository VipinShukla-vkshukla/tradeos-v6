"""
RNG — buy the low of an established intraday range.

THE COMPLEMENT TO EVERY BREAKOUT ENGINE IN THIS PACKAGE
--------------------------------------------------------
ORB, GAP, PDL and VCE all pay when a level breaks. Most sessions, levels do not
break — the stock oscillates inside a range all day and every breakout engine
returns nothing, correctly. That is a large fraction of trading days with no
opportunity at all unless something is built for it.

This is that something: in a range that has been respected several times, the
low is the highest-probability entry on the chart, precisely because the
breakout engines have already declined to fire.

WHY IT COUNTS TOUCHES BEFORE TRUSTING THE RANGE
-----------------------------------------------
Two points define a line and prove nothing. A range earns the name once both
edges have been rejected more than once — that is evidence of two-sided
participation rather than a trend that happens to have paused. Requiring
confirmed touches on BOTH sides is what stops this engine from buying the low of
a stock that is simply falling in steps.

WHY IT REFUSES A RANGE THAT IS TOO NARROW
-----------------------------------------
The cost model again. Buying the low of a 0.4% range to sell the high nets
roughly nothing after a 0.21% round trip. The range must be wide enough that
edge-to-edge clears costs with margin, which in practice means it has to be a
decent fraction of the stock's daily ATR.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import DRIFT, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure


class RangeFade:
    name = "RNG"
    phases = (DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.ltp and ctx.bars and ctx.day_high and ctx.day_low):
            return None

        bars = ctx.bars
        if len(bars) < cfg_int("rng_min_bars", 20):
            return None

        hi, lo = ctx.day_high, ctx.day_low
        width_pct = (hi - lo) / lo * 100.0
        atr = ctx.atr_pct_daily or 2.0

        # Wide enough to be worth trading edge-to-edge after costs...
        if width_pct < atr * cfg_float("rng_min_width_atr_frac", 0.35):
            return None
        # ...but not so wide that it is a trend, not a range.
        if width_pct > atr * cfg_float("rng_max_width_atr_frac", 1.10):
            return None

        tol = cfg_float("rng_touch_tol_pct", 0.20) / 100.0

        def _touches(level: float, use_low: bool) -> int:
            count, armed = 0, False
            for b in bars:
                px = b.low if use_low else b.high
                near = abs(px - level) / level <= tol
                if near and not armed:
                    count, armed = count + 1, True
                elif abs(px - level) / level > tol * 2.5:
                    armed = False
            return count

        low_touches  = _touches(lo, True)
        high_touches = _touches(hi, False)
        need = cfg_int("rng_min_touches_each", 2)
        if low_touches < need or high_touches < need:
            return None                      # not a proven range

        # Price must be AT the low, which is the only place this trade works.
        dist_low = (ctx.ltp - lo) / lo
        if dist_low > cfg_float("rng_max_dist_from_low_pct", 0.25) / 100.0:
            return None

        # A range low being tested on heavy volume is usually the range breaking,
        # not holding. This is the mirror of the breakout engines' volume rule.
        vr = ctx.volume_ratio()
        if vr is not None and vr > cfg_float("rng_max_volume_ratio", 1.80):
            return None

        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, lo * (1 - cfg_float("rng_stop_buffer_pct", 0.25) / 100.0), "LONG",
                                    max_risk_pct=cfg_float("rng_max_risk_pct", 0.70))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

        # Target the opposite edge, shaded inside it — exiting where the sellers
        # are known to be beats asking for the last few paise.
        target = lo + (hi - lo) * cfg_float("rng_target_frac", 0.85)
        if target <= ctx.ltp:
            return None

        conf = 0.45
        conf += min(0.18, (low_touches - need) * 0.06 + (high_touches - need) * 0.04)
        if vr and vr < 1.0:
            conf += 0.10                     # quiet test of the low
        conf = round(min(0.88, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Range {lo:.2f}–{hi:.2f} ({width_pct:.2f}% wide) held "
                       f"{low_touches}× at the low and {high_touches}× at the high; "
                       f"testing the low again"),
            invalidation=(f"a close below {lo:.2f} — the range breaking down turns this from "
                          f"the best entry on the chart into the worst"),
            valid_phases=self.phases,
            # invalidation_level DECLARED, not inferred — 12-Aug-2026. This
            # meta carries both edges of the range, and exit_policy's key list
            # checked range_high first, so a LONG taken at the range LOW got an
            # invalidation level ABOVE its own entry and was invalidated on the
            # first evaluation, every time. RNG has never completed a trade.
            meta={**frame.meta(), "range_low": round(lo, 2), "range_high": round(hi, 2),
                  "invalidation_level": round(lo, 2),
                  "width_pct": round(width_pct, 2), "low_touches": low_touches,
                  "high_touches": high_touches, "volume_ratio": vr},
        )
