"""
VCE — volatility contraction, then expansion.

THE ONE PATTERN THAT RELIABLY PRODUCES A 1%+ INTRADAY MOVE
-----------------------------------------------------------
This engine exists because of the cost model. A round trip costs ~0.21% and the
keep-ratio demands roughly a 0.7% target, which quietly disqualifies most
intraday setups at this account size. Contraction-expansion is the pattern whose
whole premise is a large move: volatility mean-reverts, so an unusually quiet
range is not a lull, it is stored energy.

The measurement is simple and does not need Bollinger bands: compare the range
of the last N bars against the range of the N before them. A ratio well under 1
means the stock has gone quiet. Quiet does not predict direction — so the trade
is only taken when the break resolves it, with volume.

WHY IT IS ALLOWED IN THE DRIFT WINDOW
-------------------------------------
Breakout engines are switched off between 11:00 and 13:30 because thin
conditions produce false breaks. This one is the exception, and the exception is
principled: the drift is exactly when contraction FORMS. A squeeze that builds
through the quiet hours and releases into the afternoon is one of the highest
expectancy moves of the day, and refusing to look during the drift would mean
only ever catching it late.

The volume requirement is what separates this from an ordinary drift-window
false break — a release without participation is just the range widening.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import PRIME, DRIFT, AFTERNOON
from intraday.strategies.base import Setup, SymbolContext, confirmation_pct, risk_from_structure


class SqueezeExpansion:
    name = "VCE"
    phases = (PRIME, DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.ltp and ctx.bars):
            return None

        n = cfg_int("vce_window_bars", 8)
        bars = ctx.bars
        if len(bars) < n * 2 + 1:
            return None

        recent, prior = bars[-n:], bars[-2 * n:-n]
        r_hi, r_lo = max(b.high for b in recent), min(b.low for b in recent)
        p_hi, p_lo = max(b.high for b in prior), min(b.low for b in prior)
        if r_lo <= 0 or p_hi <= p_lo:
            return None

        recent_rng = (r_hi - r_lo) / r_lo * 100.0
        prior_rng  = (p_hi - p_lo) / p_lo * 100.0
        ratio = recent_rng / prior_rng if prior_rng else 99.0

        # The contraction test.
        if ratio > cfg_float("vce_max_contraction_ratio", 0.65):
            return None
        # A range that is already microscopic relative to the stock's own daily
        # movement will produce a target too small to clear costs.
        atr = ctx.atr_pct_daily or 2.0
        if recent_rng < atr * cfg_float("vce_min_range_atr_frac", 0.08):
            return None

        # The expansion test: price must be breaking the coil, not sitting in it.
        if ctx.ltp <= r_hi:
            return None
        break_pct = (ctx.ltp - r_hi) / r_hi * 100.0
        if break_pct > cfg_float("vce_max_chase_pct", 0.45):
            return None

        # A RELEASE MUST CLEAR THE COIL, NOT TOUCH IT — 12-Aug-2026. Same
        # missing lower bound as ORB, same consequence: HINDALCO entered
        # 0.04% above the coil high and was cut "returned inside the coil"
        # eight minutes later. The invalidation floor matters more here than
        # in ORB, because this engine's own invalidation level IS r_hi — the
        # line it was entering within four basis points of.
        # See base.confirmation_pct().
        min_break = confirmation_pct(recent_rng, cfg_float("vce_min_break_frac", 0.10))
        if break_pct < min_break:
            return None                      # touching the coil, not leaving it

        vr = ctx.volume_ratio()
        min_vr = cfg_float("vce_min_volume_ratio", 1.40)
        if vr is not None and vr < min_vr:
            return None                      # widening, not releasing

        # Stop inside the coil. The coil low is the structure; a break that
        # returns into it has not resolved anything.
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, r_lo * (1 - cfg_float("vce_stop_buffer_pct", 0.10) / 100.0), "LONG",
                                    max_risk_pct=cfg_float("vce_max_risk_pct", 1.00))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

        # Measured move: a coil typically travels at least the range it
        # compressed FROM, which is the honest target for this pattern.
        measured = ctx.ltp + (p_hi - p_lo)
        by_r = ctx.ltp + risk * cfg_float("vce_target_r", 2.5)
        target = max(by_r, measured)

        conf = 0.5
        conf += min(0.2, (cfg_float("vce_max_contraction_ratio", 0.65) - ratio) * 0.5)
        if vr:
            conf += min(0.2, (vr - min_vr) * 0.12)
        if ctx.rs_vs_index_pct and ctx.rs_vs_index_pct > 0:
            conf += min(0.1, ctx.rs_vs_index_pct * 0.04)
        conf = round(min(0.94, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Range compressed to {ratio:.0%} of the prior {n} bars "
                       f"({recent_rng:.2f}% vs {prior_rng:.2f}%), now releasing above "
                       f"{r_hi:.2f}" + (f" on {vr:.1f}× volume" if vr else "")),
            invalidation=(f"a return inside the coil below {r_hi:.2f} — an expansion that "
                          f"goes back in has resolved nothing"),
            valid_phases=self.phases,
            meta={**frame.meta(), "contraction_ratio": round(ratio, 3), "coil_high": round(r_hi, 2),
                  "coil_low": round(r_lo, 2), "measured_target": round(measured, 2),
                  "volume_ratio": vr},
        )
