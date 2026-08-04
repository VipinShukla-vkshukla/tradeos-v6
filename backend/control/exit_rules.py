"""
When to cut, when to bank, and when to let it run.

THE PROBLEM WITH THE POLICY THIS EXTENDS
-----------------------------------------
The existing rules are sound but symmetric: book half at 1.5R, trail after 2R,
exit everything at 3R. That last rule is the expensive one. It treats every
trade the same at the moment they stop being the same — a stock grinding
sideways at 3R and a stock in a genuine multi-week trend at 3R get identical
treatment, and the trend is exactly what pays for the losers.

Measured on the 70 realised trades: median capture ratio 3%, meaning almost all
of the favourable excursion was given back. Cutting the winners at a fixed
multiple guarantees that never improves.

WHAT THIS ADDS
--------------
A THIRD decision at the hard target, instead of an automatic exit:

    EXIT   the move is mature, momentum is fading, or the structure has broken
    RUN    the trend is intact and leading — convert to a runner, trail wider,
           and stop asking for a target at all

The evidence for "intact and leading" is not a feeling. It is the same data the
screening engines already compute, so no new inputs and no new source of truth:

    trend structure   price above its rising 20/50 DMA, higher lows holding
    momentum          RSI still in trend territory (not exhausted, not decaying)
    participation     ADX holding up, volume not drying out
    relative strength outperforming the index over the recent window
    sector            still in a leading sector rather than a rotating-out one

RISK IS NEVER RE-ADDED
----------------------
A runner has already booked half at 1.5R and its stop is at or above breakeven,
so the decision to run risks only open profit, never capital. That asymmetry is
the whole reason this is safe to automate: the worst case for a runner is
giving back part of a gain that was never guaranteed, while the worst case for
holding a loser is a real loss. So the loss-side rules stay strict and
unconditional, and only the profit side gets discretion.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_bool, cfg_float, cfg_int, get_supabase  # noqa: F401


@dataclass
class TrendQuality:
    """Evidence about whether a position is still worth holding."""
    score: float                       # 0-1
    verdict: str                       # STRONG | INTACT | FADING | BROKEN
    reasons: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    checks: int = 0                    # how many inputs were actually available

    @property
    def has_evidence(self) -> bool:
        """
        Did anything at all get measured?

        This exists because `checks == 0` and "measured, and it looks fine" were
        indistinguishable: the no-data branch returned INTACT/0.50, whose
        should_run is True. Combined with a bug that made the trend context
        always empty, EVERY position reaching the hard target was converted to
        a runner on literally zero evidence — a decision this module exists to
        prevent. The two cases now answer differently.
        """
        return self.checks >= cfg_int("exit_runner_min_checks", 3)

    @property
    def should_run(self) -> bool:
        return self.verdict in ("STRONG", "INTACT") and self.has_evidence


def assess_trend(sig: dict, pos: dict | None = None) -> TrendQuality:
    """
    Score a position's trend on evidence the pipeline already computes.

    Pure — takes a signal_output_daily/stock_data_daily row and returns a
    verdict. No I/O, so it is testable against recorded sessions and cannot
    disagree with itself between callers.

    Missing inputs count as NEITHER for nor against. An unknown is not evidence
    of strength, and treating it as such is how a stock with no data becomes a
    "runner" — but it is not evidence of weakness either, so it must not force
    an exit that the loss-side rules did not call for.
    """
    for_, against = [], []
    checks = 0

    def _f(k):
        v = sig.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # ── Structure: is price still above a rising base? ──────────────────────
    dist50 = _f("dist_sma50")
    if dist50 is not None:
        checks += 1
        if dist50 > 0:
            for_.append(f"above 50DMA (+{dist50:.1f}%)")
        else:
            against.append(f"below 50DMA ({dist50:.1f}%)")

    ws = (sig.get("weekly_structure") or "").upper()
    if ws:
        checks += 1
        if any(k in ws for k in ("HIGHER", "UPTREND", "BULLISH", "HH")):
            for_.append(f"weekly structure {ws.lower()}")
        elif any(k in ws for k in ("LOWER", "DOWNTREND", "BEARISH", "LL")):
            against.append(f"weekly structure {ws.lower()}")

    # ── Momentum: still in trend territory, not exhausted ───────────────────
    rsi = _f("rsi_daily")
    if rsi is not None:
        checks += 1
        hot = cfg_float("exit_runner_rsi_exhausted", 82)
        cold = cfg_float("exit_runner_rsi_min", 50)
        if rsi >= hot:
            against.append(f"RSI {rsi:.0f} exhausted")
        elif rsi >= cold:
            for_.append(f"RSI {rsi:.0f} in trend")
        else:
            against.append(f"RSI {rsi:.0f} lost trend")

    ms = (sig.get("momentum_state") or "").upper()
    if ms:
        checks += 1
        if ms in ("HOT", "ACCELERATING", "STRONG"):
            for_.append(f"momentum {ms.lower()}")
        elif ms in ("COOLING", "DECELERATING", "WEAK", "COLD"):
            against.append(f"momentum {ms.lower()}")

    # ── Participation: is anyone still trading it? ──────────────────────────
    adx = _f("adx")
    if adx is not None:
        checks += 1
        if adx >= cfg_float("exit_runner_min_adx", 22):
            for_.append(f"ADX {adx:.0f}")
        else:
            against.append(f"ADX {adx:.0f} — trend losing definition")

    vr = _f("vol_ratio")
    if vr is not None:
        checks += 1
        if vr >= cfg_float("exit_runner_min_vol_ratio", 0.75):
            for_.append(f"volume {vr:.1f}x")
        else:
            against.append(f"volume {vr:.1f}x drying up")

    # ── Structure: is the sequence of highs and lows still rising? ─────────
    # Weighted like any other check rather than made decisive: a position can
    # be structurally fine and still be worth banking, and structurally shaky
    # while momentum and leadership say otherwise. It earns a vote, not a veto.
    hi = sig.get("_structure_highs")
    lo = sig.get("_structure_lows")
    if hi and lo and len(hi) >= 10:
        try:
            from analysis.market_structure import for_framework, UPTREND, CONFIRMED_UP, DOWNTREND
            st, _ = for_framework("SWING", hi, lo)
            checks += 1
            if st.state in (UPTREND, CONFIRMED_UP):
                for_.append(f"structure {st.state.lower().replace('_', ' ')}")
            elif st.state == DOWNTREND:
                against.append("structure has turned down — lower highs and lower lows")
        except Exception:
            pass

    # ── Leadership: outperforming, in a sector still working ────────────────
    rs = _f("rs_vs_nifty")
    if rs is not None:
        checks += 1
        if rs > 0:
            for_.append(f"RS +{rs:.1f} vs index")
        else:
            against.append(f"RS {rs:.1f} lagging index")

    srank = _f("sector_rank_at_entry") or _f("sector_rank")
    if srank is not None:
        checks += 1
        if srank <= cfg_float("exit_runner_max_sector_rank", 8):
            for_.append(f"sector rank {srank:.0f}")
        else:
            against.append(f"sector rank {srank:.0f} — rotating out")

    if checks == 0:
        return TrendQuality(0.5, "INTACT",
                            ["no trend data available — neither confirming nor "
                             "contradicting, so the loss-side rules alone decide"],
                            [], checks=0)

    score = len(for_) / max(checks, 1)
    strong = cfg_float("exit_runner_strong_score", 0.75)
    intact = cfg_float("exit_runner_intact_score", 0.55)
    broken = cfg_float("exit_runner_broken_score", 0.30)

    if score >= strong:
        verdict = "STRONG"
    elif score >= intact:
        verdict = "INTACT"
    elif score >= broken:
        verdict = "FADING"
    else:
        verdict = "BROKEN"
    return TrendQuality(round(score, 2), verdict, for_, against, checks=checks)


def target_decision(pos: dict, gain_r: float, tq: TrendQuality,
                    policy: dict) -> dict:
    """
    What to do at the hard target. EXIT, or convert to a runner.

    Only ever reached with the position already profitable and its stop at or
    above breakeven, so both branches risk open profit rather than capital.
    """
    # INSTRUMENTATION, ON EVERY BRANCH.
    #
    # The two failure modes this rule can have are indistinguishable from
    # outside it: the logic cannot fire because its evidence is empty, or it
    # fires and correctly declines. Both show up as "no runners". So every
    # branch below carries the evidence count and the verdict, including — and
    # especially — the ones that decline, and position_lifecycle persists them.
    obs = {"runner_evidence": tq.checks, "runner_verdict": tq.verdict}

    if not cfg_bool("exit_runners_enabled", True):
        return {"action": "EXIT_TARGET", "reason": "TARGET_HIT",
                "detail": f"{gain_r:.2f}R — runners disabled",
                "new_sl": None, "book_qty": 0, **obs}

    # THE CAP THAT WAS COMPUTED AND THROWN AWAY.
    #
    # `max_runners` and `already` were both read here and then never referenced
    # again, so exit_max_runners has been a setting the operator could tune and
    # nothing enforced. `already` read pos["runner_since_r"], a column that did
    # not exist in any migration until 031 — which is *why* it stayed unused:
    # counting concurrent runners needs a marker, and converting to a runner
    # left none.
    #
    # Enforcing it changes which positions run, so it is behind its own switch
    # defaulting to false. The count comes from the caller, which holds the
    # book; this function still sees one position and stays pure.
    if cfg_bool("exit_runner_cap_enforced", False):
        max_runners = cfg_int("exit_max_runners", 2)
        already     = int(policy.get("runners_open") or 0)
        if pos.get("runner_since_r") in (None, ""):      # not already one itself
            if already >= max_runners:
                return {
                    "action": "EXIT_TARGET", "reason": "TARGET_HIT",
                    "detail": (f"{gain_r:.2f}R at target and the trend is "
                               f"{tq.verdict.lower()}, but {already} runner(s) are "
                               f"already open against a cap of {max_runners} — "
                               f"banking rather than adding a third open-profit bet"),
                    "new_sl": None, "book_qty": 0, **obs,
                }

    # NO EVIDENCE IS A REASON TO BANK, NOT A REASON TO RUN.
    #
    # Separated from the "trend has faded" branch because they are different
    # statements and the operator should be able to tell them apart on the
    # alert. "I looked and it is weakening" and "I could not look" both mean
    # bank the gain, but only the second one means something is broken upstream.
    if not tq.has_evidence:
        return {
            "action": "EXIT_TARGET", "reason": "TARGET_HIT",
            "detail": (f"{gain_r:.2f}R at target with only {tq.checks} trend "
                       f"input(s) available — not enough to justify holding past "
                       f"the target, so banking it. A runner has to be earned on "
                       f"evidence, and there is none here"),
            "new_sl": None, "book_qty": 0, **obs,
        }

    if not tq.should_run:
        return {
            "action": "EXIT_TARGET", "reason": "TARGET_HIT",
            "detail": (f"{gain_r:.2f}R and the trend is {tq.verdict.lower()} "
                       f"({', '.join(tq.against[:3]) or 'no supporting evidence'}) "
                       f"— banking it"),
            "new_sl": None, "book_qty": 0, **obs,
        }

    # Convert to a runner: widen the trail and stop asking for a target.
    entry = float(pos.get("entry_price") or 0)
    stop0 = float(pos.get("planned_stop") or 0)
    risk = (entry - stop0) if (entry and stop0 and stop0 < entry) else entry * 0.05
    hwm = max(float(pos.get("high_water_mark") or entry), entry)
    trail_r = cfg_float("exit_runner_trail_r", 2.0)
    new_sl = hwm - trail_r * risk
    cur_sl = float(pos.get("active_sl") or 0)

    return {
        "action": "RUN", "reason": "TREND_INTACT",
        "detail": (f"{gain_r:.2f}R at target but trend is {tq.verdict} "
                   f"({tq.score:.0%}: {', '.join(tq.reasons[:3])}) — holding as a "
                   f"runner, trailing {trail_r}R below the high. Risk is already "
                   f"off: half booked and the stop is at or above entry"),
        # Never loosen. A runner that ratchets down is not a runner, it is a
        # slower way to give the gain back.
        "new_sl": round(max(new_sl, cur_sl), 2) if new_sl > cur_sl else None,
        "book_qty": 0,
        # The R at which it became a runner. This is the marker the cap counts
        # and the field capture-ratio analysis splits on.
        "runner_since_r": round(gain_r, 3), **obs,
        "trend": {"score": tq.score, "verdict": tq.verdict,
                  "for": tq.reasons, "against": tq.against},
    }


def deterioration_check(pos: dict, gain_r: float, tq: TrendQuality) -> dict | None:
    """
    Exit a PROFITABLE position whose thesis has broken, before the trail catches it.

    The trail is a price mechanism and only reacts after the damage. This reacts
    to the reason — momentum gone, structure broken, sector rotating out — while
    there is still profit to protect. Applies only above a floor of profit,
    because below it the ordinary stop is the right tool and second-guessing a
    working trade on indicator noise is how good positions get cut early.
    """
    if not cfg_bool("exit_deterioration_enabled", True):
        return None
    floor = cfg_float("exit_deterioration_min_r", 1.0)
    if gain_r < floor:
        return None
    # Same asymmetry as the runner decision, in the other direction: an absent
    # signal must not manufacture an exit any more than it manufactures a hold.
    if not tq.has_evidence:
        return None
    if tq.verdict != "BROKEN":
        return None
    return {
        "action": "EXIT_DETERIORATION", "reason": "THESIS_BROKEN",
        "detail": (f"{gain_r:.2f}R but the setup has broken down "
                   f"({', '.join(tq.against[:3])}) — taking the profit while it "
                   f"is there rather than waiting for the trail"),
        "new_sl": None, "book_qty": 0,
        "trend": {"score": tq.score, "verdict": tq.verdict, "against": tq.against},
    }


def load_signal_context(sb, symbols: list[str]) -> dict[str, dict]:
    """
    Latest signal/indicator row per symbol, for trend assessment.

    Prefers signal_output_daily — the immutable snapshot — and falls back to
    stock_data_daily so a position that has dropped out of the shortlist can
    still be assessed. A position not in today's shortlist is exactly the case
    where you most want to know whether it is deteriorating.
    """
    out: dict[str, dict] = {}
    if not symbols:
        return out

    # Recent daily highs/lows per symbol, for the structure check. Fetched once
    # here rather than per position, and attached to the same context dict so
    # assess_trend stays pure.
    bars: dict[str, dict] = {}
    try:
        raw = (sb.table("stock_data_daily").select("symbol,date,high,low")
                 .in_("symbol", symbols).order("date", desc=True)
                 .limit(len(symbols) * 70).execute().data or [])
        for r in reversed(raw):
            b = bars.setdefault(r["symbol"], {"h": [], "l": []})
            if r.get("high") and r.get("low"):
                b["h"].append(float(r["high"]))
                b["l"].append(float(r["low"]))
    except Exception as e:
        logger.debug(f"  structure bars unavailable: {e}")
    for table, cols in (
        ("signal_output_daily",
         "symbol,date,dist_sma50,rsi_daily,adx,vol_ratio,rs_vs_nifty,"
         "momentum_state,weekly_structure,sector_rank_at_entry"),
        ("stock_data_daily",
         "symbol,date,dist_sma50,rsi_daily,adx,vol_ratio,rs_vs_nifty"),
    ):
        missing = [s for s in symbols if s not in out]
        if not missing:
            break
        try:
            rows = (sb.table(table).select(cols).in_("symbol", missing)
                      .order("date", desc=True).limit(len(missing) * 4)
                      .execute().data or [])
            for r in rows:
                out.setdefault(r["symbol"], r)
        except Exception as e:
            logger.debug(f"  trend context from {table} unavailable: {e}")

    for sym, ctx in out.items():
        b = bars.get(sym)
        if b:
            ctx["_structure_highs"] = b["h"]
            ctx["_structure_lows"] = b["l"]
    return out
