"""
GAP — gap up, hold, continue.

WHY THIS DESERVES ITS OWN ENGINE IN THE INDIAN MARKET
-----------------------------------------------------
NSE has a fourteen-hour overnight void and no meaningful pre-market for most
stocks. Everything that happened in the US session, in crude, in the rupee, in
an earnings release at 6pm, gets expressed in a single opening print. That makes
Indian gaps unusually information-dense compared with markets that trade nearly
around the clock.

A gap that HOLDS is the cleanest continuation signal of the session: the market
repriced the stock overnight, and the first thirty minutes failed to take that
repricing back. A gap that fills is the opposite — the repricing is rejected —
which is why the same measurement drives an opposite trade in gap_fill.

The two are mutually exclusive by construction: one requires the gap to hold
above its own low, the other requires it to break. They cannot both fire.

WHY IT IS NOT SIMPLY "BUY GAPS"
-------------------------------
Naive gap trading loses because the size of a gap is not its quality:

  too small   under half an ATR is noise, not repricing
  too large   an exhaustion gap has no buyers left above it, and the first
              seller collapses it — the cost model would reject the trade
              anyway once the stop widens to fit
  no hold     a gap trading below its opening range low has already been
              rejected; buying it is buying a failed thesis
  no volume   a gap on thin volume is a quote adjustment, not participation
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME
from intraday.strategies.base import Setup, SymbolContext


class GapAndGo:
    name = "GAP"
    phases = (PRIME,)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.prev_close and ctx.day_open and ctx.ltp and ctx.bars):
            return None

        gap_pct = (ctx.day_open - ctx.prev_close) / ctx.prev_close * 100.0
        atr = ctx.atr_pct_daily or 2.0

        min_gap = max(cfg_float("gap_min_pct", 0.60), atr * cfg_float("gap_min_atr_frac", 0.25))
        max_gap = atr * cfg_float("gap_max_atr_frac", 1.60)
        if gap_pct < min_gap:
            return None
        if gap_pct > max_gap:
            return None                     # exhaustion territory

        # The hold test. The opening range low is where the gap was defended;
        # trading below it means the gap has already been rejected.
        window = cfg_int("gap_hold_window_min", 15)
        rng = ctx.range_between(0, window)
        if not rng:
            return None
        or_high, or_low = rng
        if ctx.ltp < or_low:
            return None
        # Never buy a gap that has given back most of itself.
        retrace = (ctx.day_open - ctx.ltp) / (ctx.day_open - ctx.prev_close) * 100.0
        if retrace > cfg_float("gap_max_retrace_pct", 50.0):
            return None

        vr = ctx.volume_ratio()
        min_vr = cfg_float("gap_min_volume_ratio", 1.30)
        if vr is not None and vr < min_vr:
            return None

        # Stop below the opening range low — below the gap itself is usually too
        # far to size, and the range low is the level actually being defended.
        stop = or_low * (1 - cfg_float("gap_stop_buffer_pct", 0.10) / 100.0)
        max_risk = cfg_float("gap_max_risk_pct", 1.30)
        if (ctx.ltp - stop) / ctx.ltp * 100.0 > max_risk:
            stop = ctx.ltp * (1 - max_risk / 100.0)
        risk = ctx.ltp - stop
        if risk <= 0:
            return None

        target = ctx.ltp + risk * cfg_float("gap_target_r", 2.0)

        conf = 0.5
        if vr:
            conf += min(0.22, (vr - min_vr) * 0.14)
        if retrace < 20:
            conf += 0.12                    # barely gave anything back
        if ctx.rs_vs_index_pct and ctx.rs_vs_index_pct > 0:
            conf += min(0.12, ctx.rs_vs_index_pct * 0.04)
        conf = round(min(0.94, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Gapped +{gap_pct:.2f}% and held — {retrace:.0f}% retraced, "
                       f"still above the {window}-min low {or_low:.2f}"
                       + (f", volume {vr:.1f}×" if vr else "")),
            invalidation=(f"a close below {or_low:.2f} — the gap being given back is the "
                          f"repricing failing, and it is visible long before the stop"),
            valid_phases=self.phases,
            meta={"gap_pct": round(gap_pct, 2), "retrace_pct": round(retrace, 1),
                  "or_low": round(or_low, 2), "volume_ratio": vr},
        )
