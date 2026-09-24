"""
IGN's daily trend logic (FINDINGS 2026-09-24): gate verdicts for both directions, the
confidence score, the forming-candle panel and the records written to Setup.meta.

EVERYTHING HERE SHIPS INERT. The study behind it found no daily signal that separates
IGN's entries (n=1,385, all Holm p >= 0.31), so each switch defaults off:
ign_trend_gate_enabled, ign_trend_short_gate_enabled, ign_trend_confidence_weight (0.0),
ign_trend_use_forming. The records are written regardless, so evidence accrues.

Direction is an explicit, required argument wherever the logic differs: a default of
LONG is how every pre-shorting call site silently scored a short as a long.
No panel, a failed integrity check, or nothing readable is "abstain" (allow / no score),
never "refuse": an absent measurement is not a measured bad trend.
"""

from __future__ import annotations

from datetime import datetime

from config import IST, cfg_bool, cfg_float, cfg_int
from intraday.trend_indicators import PANEL_KEYS, forming_panel

LONG, SHORT = "LONG", "SHORT"

# Fixed pre-registered thresholds for the SCORE (the gates' are configurable). A test
# keeps these equal to tools/replay/ign_feature_stats.GATE_MAP.
SCORE_ADX = 20.0
SCORE_DI_MINUS = 20.0
SCORE_PREV_VOL_RATIO = 2.0

# Recorded from the forming-candle panel — a compact subset, to bound storage.
LIVE_KEYS = ("above_st", "adx", "di_plus", "di_minus", "dist_sma50", "rsi14",
             "ret_1m", "sma50_gt_200")


def _checks(feats: dict, direction: str, *, above_st: bool, adx: float, di_minus: float,
            prev_vol_ratio: float, sma_structure: bool) -> list[bool | None]:
    """One result per ENABLED check, in a fixed order: True pass, False fail, None no
    data. LONG wants price above the SuperTrend, low selling pressure, SMA50 > SMA200;
    SHORT wants the mirror. Trend strength (ADX) and yesterday-not-a-blow-off are
    direction-agnostic."""
    if direction not in (LONG, SHORT):
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
    long_ = direction == LONG
    out: list[bool | None] = []

    def add(value, passed):
        out.append(None if value is None else bool(passed(value)))

    if above_st:
        add(feats.get("above_st"), (lambda v: v is True) if long_ else (lambda v: v is False))
    if adx > 0:
        add(feats.get("adx"), lambda v: v >= adx)
    if di_minus > 0:
        add(feats.get("di_minus"), (lambda v: v <= di_minus) if long_ else (lambda v: v >= di_minus))
    if prev_vol_ratio > 0:
        add(feats.get("prev_vol_ratio"), lambda v: v <= prev_vol_ratio)
    if sma_structure:
        add(feats.get("sma50_gt_200"), (lambda v: v is True) if long_ else (lambda v: v is False))
    return out


def _decide(checks: list[bool | None], min_agree: int) -> tuple[str, int, int]:
    """(verdict, passes, evaluable). min_agree 0 = every enabled check must pass; it is
    capped at the number that had data so one missing field cannot make a majority
    rule impossible."""
    evaluable = [c for c in checks if c is not None]
    if not evaluable:
        return "abstain", 0, 0
    required = min(min_agree if min_agree > 0 else len(checks), len(evaluable))
    passes = sum(evaluable)
    return ("pass" if passes >= required else "refuse"), passes, len(evaluable)


def trend_verdict(feats: dict | None, *, require_above_st: bool = False,
                  min_adx: float = 0.0, max_di_minus: float = 0.0,
                  max_prev_vol_ratio: float = 0.0, require_sma50_gt_200: bool = False,
                  min_agree: int = 0) -> tuple[str, int, int]:
    """LONG gate. (verdict, passes, evaluable); verdict is abstain | pass | refuse.
    A threshold <= 0 or a false require_* flag switches that check off."""
    if not feats or not feats.get("ok"):
        return "abstain", 0, 0
    return _decide(_checks(feats, LONG, above_st=require_above_st, adx=min_adx,
                           di_minus=max_di_minus, prev_vol_ratio=max_prev_vol_ratio,
                           sma_structure=require_sma50_gt_200), min_agree)


def trend_verdict_short(feats: dict | None, *, require_below_st: bool = False,
                        min_adx: float = 0.0, min_di_minus: float = 0.0,
                        max_prev_vol_ratio: float = 0.0,
                        require_sma50_lt_200: bool = False,
                        min_agree: int = 0) -> tuple[str, int, int]:
    """SHORT gate, the mirror of trend_verdict: price BELOW the SuperTrend, -DI at or
    above a floor, SMA50 below SMA200. Same abstain rules."""
    if not feats or not feats.get("ok"):
        return "abstain", 0, 0
    return _decide(_checks(feats, SHORT, above_st=require_below_st, adx=min_adx,
                           di_minus=min_di_minus, prev_vol_ratio=max_prev_vol_ratio,
                           sma_structure=require_sma50_lt_200), min_agree)


def trend_score(feats: dict | None, direction: str) -> float | None:
    """(passes - fails) / evaluable over the five fixed checks, in [-1, +1]; None when
    there is no usable panel or nothing readable."""
    if not feats or not feats.get("ok"):
        return None
    checks = [c for c in _checks(feats, direction, above_st=True, adx=SCORE_ADX,
                                 di_minus=SCORE_DI_MINUS, prev_vol_ratio=SCORE_PREV_VOL_RATIO,
                                 sma_structure=True) if c is not None]
    if not checks:
        return None
    passes = sum(checks)
    return (passes - (len(checks) - passes)) / len(checks)


def gate_cfg() -> dict:
    return dict(
        require_above_st=cfg_bool("ign_trend_require_above_st", False),
        min_adx=cfg_float("ign_trend_min_adx", 0.0),
        max_di_minus=cfg_float("ign_trend_max_di_minus", 0.0),
        max_prev_vol_ratio=cfg_float("ign_trend_max_prev_vol_ratio", 0.0),
        require_sma50_gt_200=cfg_bool("ign_trend_require_sma50_gt_200", False),
        min_agree=cfg_int("ign_trend_min_agree", 0),
    )


def short_gate_cfg() -> dict:
    return dict(
        require_below_st=cfg_bool("ign_trend_short_require_below_st", False),
        min_adx=cfg_float("ign_trend_short_min_adx", 0.0),
        min_di_minus=cfg_float("ign_trend_short_min_di_minus", 0.0),
        max_prev_vol_ratio=cfg_float("ign_trend_short_max_prev_vol_ratio", 0.0),
        require_sma50_lt_200=cfg_bool("ign_trend_short_require_sma50_lt_200", False),
        min_agree=cfg_int("ign_trend_short_min_agree", 0),
    )


def forming_feats(ctx) -> dict | None:
    """The panel recomputed with today's still-forming candle (open/high/low so far,
    ltp as the close, session volume), or None when it cannot be built: no usable
    as-of panel, no bars, or the last completed bar is not before today."""
    base = ctx.daily_feats
    bars = getattr(ctx, "daily_bars", None)
    if not base or not base.get("ok") or not bars or not ctx.day_open or not ctx.ltp:
        return None
    today = (ctx.as_of.astimezone(IST) if ctx.as_of else datetime.now(IST)).date()
    if bars[-1].date >= today:
        return None
    high = max(x for x in (ctx.day_high, ctx.ltp) if x)
    low = min(x for x in (ctx.day_low, ctx.ltp) if x)
    volume = ctx.session_volume or sum(b.volume for b in (ctx.bars or []))
    p = forming_panel(bars, date=today, open=ctx.day_open, high=high, low=low,
                      close=ctx.ltp, volume=volume)
    p["ok"], p["reason"] = True, None
    return p


def active_feats(ctx) -> dict | None:
    """The panel the gates and the score read: the as-of-prior-close panel, or the
    forming-candle one when ign_trend_use_forming is on (None if that cannot be built
    — abstain, not a silent fall back to the other quantity)."""
    if cfg_bool("ign_trend_use_forming", False):
        return forming_feats(ctx)
    return ctx.daily_feats


def trend_record(ctx, gate: str, score: float | None) -> dict:
    """Setup.meta['trend'] on EVERY IGN detection, gate armed or not: the Kite panel as
    it stood, delivery_pct (the one named non-Kite exception) and the score. Thresholds
    are applied offline against intraday_setups, so a detection a gate would later
    refuse is still on file."""
    f = ctx.daily_feats
    rec: dict = {"available": bool(f), "gate": gate,
                 "delivery_pct_daily": ctx.delivery_pct_daily,
                 "score": None if score is None else round(score, 3)}
    if f:
        rec.update({k: f.get(k) for k in PANEL_KEYS})
        rec.update({"ok": f.get("ok"), "reason": f.get("reason"),
                    "as_of": f.get("as_of"), "n_bars": f.get("n_bars")})
    return rec


def live_record(ctx) -> dict | None:
    """Setup.meta['trend_live']: a compact forming-candle panel, record-only. None when
    it cannot be built (no key is written then)."""
    p = forming_feats(ctx)
    if not p:
        return None
    rec = {k: p.get(k) for k in LIVE_KEYS}
    rec["as_of"] = p.get("as_of")
    return rec


def trend_meta(ctx, gate: str, score: float | None) -> dict:
    """The keys IGN adds to Setup.meta: 'trend' always, 'trend_live' when buildable."""
    out = {"trend": trend_record(ctx, gate, score)}
    live = live_record(ctx)
    if live is not None:
        out["trend_live"] = live
    return out
