"""
IGN — Ignition Momentum. Violent, uncompressed acceleration, in any phase,
whether or not it is approaching an NSE circuit limit.

WHY THIS ENGINE, WHY NOW — 08-Sep-2026
----------------------------------------
Operator's own framing: stocks hitting an upper/lower circuit, or pumping
hard on volume without reaching one, are opportunities none of the existing
engines can catch. Checked against every one of them, not assumed:

  ORB    opening-range breakout, first 15 minutes only.
  GAP    opening-gap-hold, once at 09:15, and its own `gap_max_atr_frac`
         (1.60) REFUSES anything past that as "exhaustion territory" —
         exactly the population this engine exists to catch.
  PDL    prior-day level, break and retest — a specific reference level,
         not a magnitude/volume signature.
  VCE    compression RELEASING — needs prior compression. The opposite
         mechanism: IGN fires on a move that was never compressed at all.
  PBK    first pullback in a trend day — mean-reversion.
  VWR    fade and reclaim of the day's VWAP — mean-reversion.
  RNG    the low of a proven range — mean-reversion.
  SDN    the short family (VWAP rejection / trap / range breakdown) — real
         shorts, but each condition anchors to a specific structural level,
         not a raw magnitude+volume signature, and none of the three fires
         mid-session on a stock simply accelerating hard with no prior
         reference level at all.

Quantified before writing a line of trigger logic (docs/FINDINGS.md,
08-Sep-2026, "Intraday trader review"): over the 31-day window
`intraday_setups` covers, restricted to the 253 symbols the intraday
scanner has ever touched, 121 symbol-days had an >=8% single-day move and
92 of them (76%) got zero detection from any of the 8 engines above.
Real example: BALRAMCHIN, 2026-08-20, +20.01% high vs prev_close on 33.5M
shares against ~0.3-1.6M the prior week — completely undetected. The
proposed thresholds below (ign_min_pct/ign_min_atr_frac/
ign_min_volume_ratio) were checked against the real population of >=8%
move days in this same window: both sit BELOW the measured p10 of that
population (volume ratio 3.13x, move/ATR ratio 2.21x) — margin to catch a
move while it is still forming, not only once fully extended.

THE INVERSION FROM GAP, DELIBERATE
-----------------------------------
`min_move` uses the exact same max(absolute floor, ATR-relative)
construction GAP uses (gap_and_go.py) — but with NO ceiling. GAP's own
gap_max_atr_frac exists to refuse a move with no buyers left above it; IGN
is built specifically to trade INTO that population, on the theory that a
magnitude+volume signature this violent is its own confirmation, checked
continuously through PRIME/DRIFT/AFTERNOON (not a single 09:15
measurement) rather than assumed exhausted by size alone.

VOLUME IS REQUIRED, NOT MERELY REWARDED — a deliberate break from GAP/VCE/
GDB's own convention (all three pass an unconfirmed move through, just
without a confidence bonus). IGN has no structural break to lean on at all
— no opening range, no coil, no VWAP defence — so magnitude and volume
TOGETHER are the entire signal. Follows SDN's own `_range_breakdown`
precedent instead: "a break on no volume is a drift, and a drift
reverses." Accepted cost: a Population B/C symbol with no
stock_data_daily row (avg_volume_20d=None) is refused outright — watch
this in the live trade data, not solved here.

CIRCUIT DATA GATES A FREEZE, NEVER REQUIRES PROXIMITY
--------------------------------------------------------
ctx.upper_circuit/lower_circuit (intraday/strategies/base.py, populated by
intraday/engine.py's refresh_contexts()/merge_live_bars() from
kite_client.fetch_quotes() — see that function's own comments) are None on
most calls, most sessions, most symbols — every check here degrades
correctly when they are absent, because the operator explicitly wants BOTH
populations caught by the same engine: a genuine circuit-run AND a pure
volume-pump that never gets near one. Distance-to-circuit only ever
informs `confidence`; the only thing circuit data GATES is refusing an
entry into an already-frozen price (no seller/bid to fill against),
reusing control/sl_monitor.py::check_circuit_locks()'s own +/-0.1%
tolerance rather than inventing a second one.

STOP IS A LEVEL, NOT AN ATR MULTIPLE — no engine in this codebase anchors
a stop to ATR (it appears only in magnitude gates and one target cap); a
recent swing low/high, buffered by a small fixed percentage, matches
VCE/PBK/SDN/GDB's own idiom. Routed through risk_from_structure() like
every other engine — a stop too wide is REFUSED, never re-priced.

THE `can_short()` FINDING — READ BEFORE CHANGING THE SHORT LEG
------------------------------------------------------------------
`can_short()` is called centrally, exactly once, in intraday/engine.py's
ordinary 15s flow — and only on the capital-eligible `best` setup, gated
first by `shorts_live` (intraday_allow_shorts AND the market context
confirming weakness) and then the FULL call with the real stock row
(ASM/GSM/FO-ban flags) and real session runway
(shortability.can_short(ctx, stock_row, minutes_left=...)). This engine's
own `_short()` below calls ONLY the light, data-available subset —
`can_short(ctx)`, no stock/minutes_left — as a cheap first-pass filter so
an obviously-doomed short (already collapsed past
intraday_short_max_down_pct) is never even proposed. It is NOT a
substitute for the full check.

Because IGN ships ACTIVE (migration 132), the ordinary 15s cycle applies
the FULL central check automatically the moment IGN's setup is `best` — no
extra wiring needed there. The FAST-ENTRY path (event_core.py) bypasses
that central flow entirely, so it explicitly replicates BOTH the
`shorts_live` gate and the full `can_short()` call, with real data, before
ever acting on a SHORT `best` — see event_core.py's own comments at that
call site. Do not remove this engine's own light self-check on the
assumption the fast path "already handles it" — the two are independent
layers protecting two different call paths.

One more finding worth keeping in the code, not just the ledger:
`can_short()`'s upper-circuit proxy can NEVER bind on IGN's own SHORT leg
— that leg already requires the stock to be deeply negative on the day,
structurally incompatible with also reading as sharply up. The leg
actually doing protective work for IGN is `can_short()`'s "already
collapsed" floor (intraday_short_max_down_pct) plus liquidity/
surveillance where reachable.

SHIPPED ACTIVE, ON THE OPERATOR'S OWN EXPLICIT INSTRUCTION, ZERO SCORED
OUTCOMES — migration 132. Not a default; see docs/FINDINGS.md, 08-Sep-2026,
for the full trace of why (intraday stays paper regardless; the allocator
itself adds no meaningful latency; the 15s cycle interval was the real
bottleneck, closed instead by IGN's own fast-entry path on the 2-second
event_core loop, intraday_ign_fast_entry_enabled).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_bool, cfg_float, cfg_int
from intraday.session import PRIME, DRIFT, AFTERNOON
from intraday.shortability import can_short
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure


class IgnitionMomentum:
    name = "IGN"
    # Matches session.TRADEABLE — the main 15s loop's own can_enter gate
    # already excludes OPENING/PRE_OPEN/CLOSING/SQUARE_OFF regardless of
    # what this tuple declares, so declaring them here would be decorative.
    phases = (PRIME, DRIFT, AFTERNOON)

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None:
        if phase not in self.phases:
            return None
        if not (ctx.prev_close and ctx.ltp and ctx.bars):
            return None
        if len(ctx.bars) < cfg_int("ign_min_bars", 8):
            return None

        # THE OPENING HOUR IS WHERE THIS ENGINE'S EDGE ACTUALLY LIVES OR
        # DIES -- 15-Sep-2026, docs/FINDINGS.md. IGN's own full resolved
        # history (n=789, cost_verdict=TAKEN, 09-Sep..15-Sep -- its entire
        # real lifetime, not a cherry-picked slice) splits by
        # `intraday.session.hour_bucket()` into a large, clean, monotonic
        # effect: OPEN (09:15-10:00) 34.6% win, mean -0.43%, avg net
        # -Rs61.62/trade (n=361) against MID+LATE (10:00-15:15) 63.8% win,
        # avg net +Rs11.65/trade (n=428). Pooled, that is -Rs21.87/trade
        # overall -- a negative-expectancy engine on the whole population,
        # but one whose negativity is concentrated almost entirely in the
        # 46% of its detections that fire in the first 45 minutes it is
        # allowed to trade. This is not a parameter to nudge (the entry-
        # threshold change, migration 138, WAS that, and a real single-day
        # rupee check reverted it, migration 140) -- this is a structural
        # gate on WHEN the trigger is even allowed to look, matching every
        # other engine's own session-phase awareness
        # (intraday/session.py's own docstring: "applying a momentum rule
        # during the midday drift[or here, the open]... is the single
        # most common way an intraday system loses money").
        #
        # `ign_exclude_open_hour_enabled` ships ARMED (True) -- n=428 for
        # the redesigned population clears this project's own n=100
        # sufficiency bar with room to spare, unlike migration 138's n=52.
        # One config row away from reversible if real trades disagree.
        if cfg_bool("ign_exclude_open_hour_enabled", True):
            # ctx.as_of, NOT datetime.now(IST) — this function is called
            # directly by the replay harness (tools/replay/detect.py), and
            # as_of is the instant the caller actually means: the real
            # slow-timer refresh time live, or the exact simulated bar
            # timestamp in replay. now() would check the wall clock the
            # process happens to run on instead of the moment being
            # evaluated, silently wrong in replay specifically.
            from intraday.session import hour_bucket
            if ctx.as_of and hour_bucket(ctx.as_of) == "OPEN":
                return None

        chg_pct = (ctx.ltp - ctx.prev_close) / ctx.prev_close * 100.0
        atr = ctx.atr_pct_daily or 2.0
        min_move = max(cfg_float("ign_min_pct", 3.5),
                       atr * cfg_float("ign_min_atr_frac", 1.2))

        # REQUIRED, not merely rewarded — see module docstring. A missing
        # or weak volume reading refuses the setup outright, unlike GAP/
        # VCE/GDB which pass an unconfirmed move through without a bonus.
        vr = ctx.volume_ratio()
        min_vr = cfg_float("ign_min_volume_ratio", 2.00)
        if vr is None or vr < min_vr:
            return None

        if chg_pct >= min_move:
            return self._long(ctx, chg_pct, atr, vr, min_move, min_vr)
        if chg_pct <= -min_move:
            return self._short(ctx, chg_pct, atr, vr, min_move, min_vr)
        return None

    def _long(self, ctx, chg_pct, atr, vr, min_move, min_vr) -> Setup | None:
        tol = cfg_float("ign_circuit_freeze_tol_pct", 0.10) / 100.0
        if ctx.upper_circuit and abs(ctx.ltp - ctx.upper_circuit) <= ctx.ltp * tol:
            return None   # nothing to buy into, no seller at a frozen top

        lookback = cfg_int("ign_stop_lookback_bars", 20)
        window = ctx.bars[-lookback:]
        swing_low = min(b.low for b in window)
        structural_stop = swing_low * (1 - cfg_float("ign_stop_buffer_pct", 0.12) / 100.0)

        frame = risk_from_structure(ctx.ltp, structural_stop, "LONG",
                                    max_risk_pct=cfg_float("ign_max_risk_pct", 1.75))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk
        target = ctx.ltp + risk * cfg_float("ign_target_r", 1.5)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="LONG",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=self._confidence(ctx, "LONG", chg_pct, vr, min_move, min_vr),
            rationale=(f"+{chg_pct:.2f}% off prev close on {vr:.1f}x volume — "
                       f"violent, uncompressed acceleration"),
            invalidation=f"a close back below {swing_low:.2f} — the base under this move",
            valid_phases=self.phases,
            meta={**frame.meta(), "chg_pct": round(chg_pct, 2), "volume_ratio": vr,
                  "swing_low": round(swing_low, 2)},
        )

    def _short(self, ctx, chg_pct, atr, vr, min_move, min_vr) -> Setup | None:
        tol = cfg_float("ign_circuit_freeze_tol_pct", 0.10) / 100.0
        if ctx.lower_circuit and abs(ctx.ltp - ctx.lower_circuit) <= ctx.ltp * tol:
            return None   # no bid to sell into, no cover liquidity if filled

        # LIGHT self-gate only — see module docstring for exactly what this
        # does and does not cover, and why it is not a substitute for the
        # central/fast-path checks.
        ok_sh, why_sh, notes = can_short(ctx)
        if not ok_sh:
            return None

        lookback = cfg_int("ign_stop_lookback_bars", 20)
        window = ctx.bars[-lookback:]
        swing_high = max(b.high for b in window)
        structural_stop = swing_high * (1 + cfg_float("ign_stop_buffer_pct", 0.12) / 100.0)

        frame = risk_from_structure(ctx.ltp, structural_stop, "SHORT",
                                    max_risk_pct=cfg_float("ign_max_risk_pct", 1.75))
        if frame is None:
            return None
        stop, risk = frame.stop, frame.risk
        target = ctx.ltp - risk * cfg_float("ign_target_r", 1.5)

        return Setup(
            symbol=ctx.symbol, strategy=self.name, direction="SHORT",
            entry=round(ctx.ltp, 2), stop=round(stop, 2), target=round(target, 2),
            confidence=self._confidence(ctx, "SHORT", chg_pct, vr, min_move, min_vr),
            rationale=(f"{chg_pct:.2f}% off prev close on {vr:.1f}x volume — "
                       f"violent, uncompressed collapse"),
            invalidation=f"reclaims {swing_high:.2f} — the level this breakdown left behind",
            valid_phases=self.phases,
            meta={**frame.meta(), "chg_pct": round(chg_pct, 2), "volume_ratio": vr,
                  "swing_high": round(swing_high, 2), "shortability_notes": notes},
        )

    def _confidence(self, ctx, direction, chg_pct, vr, min_move, min_vr) -> float:
        # Base 0.40 — below every other engine's own base. Zero track
        # record; nothing here is proven yet. Capped at 0.85 for the same
        # reason (other engines reach 0.90-0.95).
        conf = 0.40
        conf += min(0.20, (vr - min_vr) * 0.08)
        if abs(chg_pct) > min_move * 1.5:
            conf += 0.10                       # comfortably past threshold, not marginal
        # Circuit proximity — CONFIDENCE ONLY, never a gate. See module
        # docstring for why: the operator explicitly wants a pure
        # volume-pump with no circuit in sight caught by this same engine.
        near_pct = cfg_float("ign_circuit_near_pct", 2.0)
        if direction == "LONG" and ctx.upper_circuit and ctx.ltp:
            room = (ctx.upper_circuit - ctx.ltp) / ctx.ltp * 100.0
            if 0 <= room <= near_pct:
                conf += 0.15
        elif direction == "SHORT" and ctx.lower_circuit and ctx.ltp:
            room = (ctx.ltp - ctx.lower_circuit) / ctx.ltp * 100.0
            if 0 <= room <= near_pct:
                conf += 0.15
        return round(min(0.85, conf), 2)
