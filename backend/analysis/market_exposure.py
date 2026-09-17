"""
How much the swing book may buy today, read from the index and breadth.

Long breakouts and continuations fail together in a falling market. Over
31-Aug..10-Sep-2026 the evening plans won 19-28% of the time against 49-62%
in mid-August, and the book kept entering at full size, up to 10 a day.

Pure. Callers pass the market_regime rows for sessions BEFORE the trading day
(one row per session, oldest first). `market_regime.nifty_50dma` is not used:
it read 24173.825 unchanged from April to September.

    NORMAL      no change
    CORRECTION  2+ of: Nifty below its 50-session mean, Nifty 20-session change
                <= -3%, median advance/decline over 5 sessions < 0.8.
                At most swing_correction_max_new entries a day, sized
                x swing_correction_size_mult.
    RISK_OFF    swing regime label is RISK OFF and swing_risk_off_block_entries
                is on: no new swing entries.

WHAT SHIPS ON, AND WHY ONLY THAT. Checked against 56 plan days (25-Jun to
08-Sep-2026) with `tools.swing_fix_replay --exposure-premise`: days with
weakness signals preceded +0.135R per plan-day in the 24..30-Jul pullback and
-0.24R in the 01..08-Sep slide. Opposite signs, one episode each, so nothing
here predicts direction. Smaller size and fewer entries in a correction is the
risk-control half and is on. Blocking RISK OFF outright (the Jul pullback's
plans won 69-78%) and the RS-percentile / no-chase selection (refused plans
beat admitted ones, n=16) are not confirmed and default off.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from config import cfg_bool, cfg_float, cfg_int


@dataclass(frozen=True)
class Exposure:
    state: str
    block_new: bool = False
    max_new: int | None = None
    size_mult: float = 1.0
    rs_pctile: float | None = None
    reasons: tuple[str, ...] = ()


NORMAL = Exposure("NORMAL")


def correction_signals(rows: list[dict]) -> list[str]:
    closes = [float(r["nifty_price"]) for r in rows if r.get("nifty_price")]
    out: list[str] = []
    if len(closes) >= 50:
        sma = statistics.fmean(closes[-50:])
        if closes[-1] < sma:
            out.append(f"Nifty {closes[-1]:.0f} below its 50-session mean {sma:.0f}")
    if len(closes) >= 21:
        chg = (closes[-1] / closes[-21] - 1.0) * 100.0
        if chg <= cfg_float("swing_correction_nifty_20d_pct", -3.0):
            out.append(f"Nifty {chg:+.1f}% over 20 sessions")
    ad = [float(r["advance_decline_ratio"]) for r in rows[-5:]
          if r.get("advance_decline_ratio") is not None]
    if len(ad) >= 3:
        med = statistics.median(ad)
        if med < cfg_float("swing_correction_ad_median", 0.8):
            out.append(f"median A/D {med:.2f} over {len(ad)} sessions")
    return out


def exposure_for_day(regime: str | None, rows_before: list[dict]) -> Exposure:
    if not cfg_bool("swing_exposure_enabled", False):
        return NORMAL
    if (str(regime or "").strip().upper().replace("_", " ") == "RISK OFF"
            and cfg_bool("swing_risk_off_block_entries", False)):
        return Exposure("RISK_OFF", block_new=True, max_new=0, size_mult=0.0,
                        reasons=("swing regime is RISK OFF",))
    sig = correction_signals(rows_before)
    if len(sig) >= cfg_int("swing_correction_min_signals", 2):
        return Exposure("CORRECTION",
                        max_new=cfg_int("swing_correction_max_new", 2),
                        size_mult=cfg_float("swing_correction_size_mult", 0.5),
                        rs_pctile=(cfg_float("swing_correction_rs_pctile", 0.0) or None),
                        reasons=tuple(sig))
    return NORMAL


def _pctile(values: list[float], p: float) -> float:
    v = sorted(values)
    k = (len(v) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def selection_refusal(exp: Exposure, plan: dict, field_rows: list[dict],
                      price: float | None) -> str:
    """Why this plan may not be entered in the current state, or ''."""
    if exp.state != "CORRECTION":
        return ""
    if cfg_bool("swing_correction_refuse_chase", False):
        zhi = plan.get("entry_zone_high")
        if price and zhi and float(price) > float(zhi):
            return f"CORRECTION: {float(price):.2f} is above the zone high {float(zhi):.2f}"
        timing = str(plan.get("entry_timing_type") or "").upper()
        if timing in ("CHASING", "REENTRY"):
            return f"CORRECTION: {timing} setup"
    if exp.rs_pctile is not None:
        field = [float(r["rs_vs_nifty"]) for r in field_rows
                 if r.get("rs_vs_nifty") is not None]
        rs = plan.get("rs_vs_nifty")
        if len(field) >= 5:
            bar = _pctile(field, exp.rs_pctile)
            if rs is None or float(rs) < bar:
                return (f"CORRECTION: RS vs Nifty {rs if rs is not None else 'n/a'} "
                        f"below today's {exp.rs_pctile:.0f}th percentile {bar:.1f}")
    return ""


def for_entries(exp: Exposure) -> Exposure:
    """The exposure the entry path obeys: NORMAL in paper research mode, else `exp`."""
    from execution.gates import swing_research_mode
    return NORMAL if (exp.state != "NORMAL" and swing_research_mode()) else exp


def daily_cap(max_new: int, exp: Exposure) -> int:
    """The day's swing entry cap after the exposure state has had its say."""
    if exp.block_new:
        return 0
    return max_new if exp.max_new is None else min(max_new, exp.max_new)


def load_exposure(sb, today: str, regime: str | None = None) -> Exposure:
    """market_regime rows before `today`, classified. Fails open to NORMAL."""
    try:
        rows = (sb.table("market_regime")
                  .select("date,regime,computed_regime,nifty_price,advance_decline_ratio")
                  .lt("date", today).order("date", desc=True).limit(60)
                  .execute().data or [])
    except Exception:
        return NORMAL
    rows = list(reversed(rows))
    if regime is None and rows:
        regime = rows[-1].get("computed_regime") or rows[-1].get("regime")
    return exposure_for_day(regime, rows)
