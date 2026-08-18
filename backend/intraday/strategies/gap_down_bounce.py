"""
GDB — Gap-Down Bounce. From a brain proposal, not hand-designed.

WHERE THIS CAME FROM
---------------------
tools/discover_engines.py::moved_but_unseen() measures, for every prior-day
condition it tracks, whether a big intraday move followed and whether any of
the seven existing engines caught it. On 2026-08-10 it flagged one bucket
twice (05-Aug, re-raised 10-Aug because nothing had been built yet — see
brain_proposals id 188/190):

    after 'gap down > 1%', 34% of 41 symbol-days produced a 3.46%+ move
    against a 20% background (1.7x lift). 13 of those went undetected by
    every engine, averaging 6.00% with 92% closing strong

READ THIS CAREFULLY BEFORE ASSUMING THE DIRECTION
--------------------------------------------------
"Gap down" names the CONDITION, not the trade. The MOVE measured is
(day_high - day_open) / day_open — how far price traveled ABOVE the open
during the session — and "closing strong" means close > open. A gap-down
day that goes on to make a big high above its own open, closing strong, is a
BOUNCE, not a breakdown. This is a mean-reversion LONG engine: buy the
recovery off a gap-down open, not short the gap.

WHY NO EXISTING ENGINE CATCHES THIS — CONFIRMED, NOT ASSUMED
--------------------------------------------------------------
vwap_reclaim.py (VWR) is the closest existing engine — same reclaim
mechanic — and it EXPLICITLY EXCLUDES this population: "Requires the stock
to be positive on the day and holding above the previous close, so the
trade is 'strong stock that dipped', not 'weak stock that paused'"
(vwap_reclaim.py, the ctx.ltp < ctx.prev_close guard). A gap-down bounce is
by definition still negative on the day for most of its life. That guard is
exactly why discover_engines found this shape uncovered rather than a
mis-tuned gate — there was never a code path that could have fired here.

MECHANISM — reuses the reclaim logic VWR already has calibrated (including
its 10-Aug single-bar-crossing fix, so this does not reopen a bug already
found once), pointed at the population VWR was built to refuse:

    1. Gapped down at least gdb_min_gap_pct (matches the proposal's own
       1% threshold), bounded above by gdb_max_gap_atr_frac so a genuine
       exhaustion/crash gap is not read as a bounce setup — a stock down
       6% on real news is a different regime than one down 1.2% on
       overnight noise.
    2. Spent real time below VWAP (this is near-automatic on a gap-down
       open) and has just reclaimed it — the single-bar crossing VWR's own
       10-Aug fix requires, not a stale multi-bar-old reclaim.
    3. Volume confirms participation, not a quiet drift back to average.

PHASE SCOPING IS A MODELLING CHOICE, NOT SOMETHING THE DATA PROVED.
---------------------------------------------------------------------
discover_engines works from stock_data_daily — one row per symbol-day — so
it has no view of WHEN during the session the high formed. OPENING and
PRIME are chosen because panic-driven gaps typically resolve early (sellers
exhaust, buyers step in before the DRIFT window's thin conditions), not
because the backtest measured intraday timing. Revisit once SHADOW data
accumulates its own timing evidence.

LAUNCHED SHADOW, NOT ACTIVE — matching brain_proposals id 190's own
proposed_value ("build as SHADOW") and this project's discovery-tool
convention (score ~20 outcomes before promotion). registry.py's engine
lifecycle defaults an unconfigured engine to ACTIVE — deliberately, so a
forgotten config row doesn't silently do nothing — which means this one
NEEDS its system_config row seeded explicitly (migration) rather than
relying on that default. See intraday_engine_gdb_lifecycle.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int
from intraday.session import OPENING, PRIME
from intraday.strategies.base import Setup, SymbolContext, confirmation_pct, risk_from_structure


class GapDownBounce:
    name = "GDB"
    phases = (OPENING, PRIME)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.prev_close and ctx.day_open and ctx.ltp and ctx.bars and ctx.vwap):
            return None

        gap_pct = (ctx.day_open - ctx.prev_close) / ctx.prev_close * 100.0
        atr = ctx.atr_pct_daily or 2.0

        # Must be down at least the proposal's own threshold — bigger than a
        # plain ATR fraction so this stays a genuine gap, not routine noise,
        # mirroring how gap_and_go.py sizes ITS minimum on the upside.
        min_gap = max(cfg_float("gdb_min_gap_pct", 1.00), atr * cfg_float("gdb_min_gap_atr_frac", 0.35))
        if gap_pct > -min_gap:
            return None
        # Too large and this is an exhaustion/crash gap, not overnight noise
        # a normal session absorbs — a different regime "buy the dip" does
        # not apply to. Mirrors gap_and_go.py's upside exhaustion bound.
        max_gap = atr * cfg_float("gdb_max_gap_atr_frac", 2.00)
        if gap_pct < -max_gap:
            return None

        lookback = cfg_int("gdb_lookback_bars", 12)
        recent = ctx.bars[-lookback:]
        if len(recent) < 4:
            return None

        # The shape: real time spent below VWAP (near-automatic on a
        # gap-down open), and above it NOW. Without "spent time below" this
        # is just "price is above average", a description, not a setup —
        # same reasoning as vwap_reclaim.py's identical check.
        below = [b for b in recent if b.close < ctx.vwap]
        min_below = cfg_int("gdb_min_bars_below", 2)
        if len(below) < min_below:
            return None
        if ctx.ltp <= ctx.vwap:
            return None

        # THE SINGLE-BAR CROSSING, not a stale reclaim several bars back.
        # This is vwap_reclaim.py's 10-Aug-2026 fix (engine_scorecard: n=34,
        # gross -0.059R, "NO EDGE — signal problem"), reused verbatim rather
        # than risking the same bug a second time in a sibling engine.
        if not (recent[-1].close > ctx.vwap and recent[-2].close < ctx.vwap):
            return None

        reclaim_pct = (ctx.ltp - ctx.vwap) / ctx.vwap * 100.0
        if reclaim_pct > cfg_float("gdb_max_extension_pct", 0.50):
            return None                    # already extended past the level

        # ── AND A LOWER BOUND — 12-Aug-2026 ──────────────────────────────
        # Same one-sided-bound defect ORB, VCE and GAP all had: the reclaim
        # was capped from above and unbounded from below, so an entry one
        # paise over VWAP qualified. VWAP is also this setup's own
        # invalidation level, so such an entry opens INSIDE the band that
        # closes it — `_invalidated` cuts at vwap*(1-buffer), which would sit
        # above the entry. There is no structure width to scale against here
        # (VWAP is a line, not a range), so the floor is the invalidation
        # buffer itself — the one number that makes entry and exit agree.
        # See base.confirmation_pct().
        if reclaim_pct < confirmation_pct(0.0, 0.0):
            return None                    # touching VWAP, not reclaiming it

        vr = ctx.volume_ratio()
        min_vr = cfg_float("gdb_min_volume_ratio", 1.10)
        if vr is not None and vr < min_vr:
            return None                    # a quiet drift back, not real buying

        # Stop under the low made while below VWAP — the panic low. Losing
        # it again means the bounce failed, same logic as VWR's swing_low.
        swing_low = min(b.low for b in below) if below else min(b.low for b in recent)
        # The stop is STRUCTURAL and stays structural. When it is wider than
        # this engine can afford the setup is REFUSED, not re-priced onto a
        # level the structure never named -- base.risk_from_structure has the
        # measurement (pinned -0.5348R vs structural +0.0154R, n=1766).
        frame = risk_from_structure(ctx.ltp, swing_low * (1 - cfg_float("gdb_stop_buffer_pct", 0.10) / 100.0), "LONG",
                                    max_risk_pct=cfg_float("gdb_max_risk_pct", 1.10))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk

        # Target the day's high once it clears a worthwhile distance — a
        # level the stock already proved it can reach today — falling back
        # to an R-multiple only when there is nothing proven left to reach
        # for. Same shape as VWR's own (also 10-Aug-fixed) target logic.
        by_r = ctx.ltp + risk * cfg_float("gdb_target_r", 2.0)
        min_worthwhile = ctx.ltp + risk * cfg_float("gdb_min_target_r", 1.0)
        if ctx.day_high and ctx.day_high * 1.001 >= min_worthwhile:
            target = ctx.day_high * 1.001
        else:
            target = by_r
        if target <= ctx.ltp:
            return None

        conf = 0.40
        if vr:
            conf += min(0.20, (vr - min_vr) * 0.15)
        if len(below) >= min_below + 2:
            conf += 0.10                   # a proper flush, not one bar
        # Deeper gaps that still found a bid are a stronger signal than a
        # barely-qualifying one, up to the exhaustion bound above.
        conf += min(0.10, (-gap_pct - min_gap) * 0.05)
        conf = round(min(0.85, conf), 2)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=conf,
            rationale=(f"Gapped {gap_pct:.2f}%, reclaimed VWAP {ctx.vwap:.2f} after "
                       f"{len(below)} bars below it; holding {reclaim_pct:.2f}% above"
                       + (f", volume {vr:.1f}×" if vr else "")),
            invalidation=(f"a close back under VWAP {ctx.vwap:.2f} — the bounce failing "
                          f"means the panic low was not actually absorbed, knowable "
                          f"before the stop at {stop:.2f}"),
            valid_phases=self.phases,
            meta={"gap_pct": round(gap_pct, 2), "vwap": round(ctx.vwap, 2),
                  "bars_below": len(below), "swing_low": round(swing_low, 2),
                  "volume_ratio": vr, "source_proposal": "brain_proposals#190"},
        )
