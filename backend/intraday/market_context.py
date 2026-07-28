"""
The index gate — what every engine was missing.

WHY THIS IS NOT OPTIONAL
------------------------
Six engines, all long-only, all evaluating one stock at a time with no idea what
the market is doing. On a day Nifty is breaking down through its own VWAP, every
one of them will still happily find a stock holding up and call it strength.
That is the single most reliable way an intraday system produces a cluster of
correlated losses: six "independent" signals that were all really one bet on the
market, taken at the worst moment.

Individual setups do not diversify away index risk. Intraday, correlation to the
index rises sharply exactly when it hurts — in a selloff, almost everything
moves together, and the stock that "held up" for the first hour is usually the
one that catches up in the second.

WHAT IT DOES
------------
Classifies the session into four states from the index alone, and returns a
sizing multiplier plus a permission flag. It never picks stocks and never
overrides an engine's own logic — it decides how much of what the engines find
should be acted on, which is a separate question and belongs in one place rather
than duplicated across six.

    RISK_ON     index above VWAP and prior close, trending      full size
    NEUTRAL     mixed or rangebound                             reduced
    CAUTION     index below VWAP but holding prior close        minimal
    RISK_OFF    index below both, making session lows           no new longs

RISK_OFF blocks rather than shrinks. There is no position size at which buying
into a broad selloff becomes a good intraday trade — the correct response is to
stop, not to do the same thing smaller.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_float

RISK_ON  = "RISK_ON"
NEUTRAL  = "NEUTRAL"
CAUTION  = "CAUTION"
RISK_OFF = "RISK_OFF"

INDEX_SYMBOL = "NIFTY 50"


@dataclass(frozen=True)
class MarketContext:
    state: str
    allow_longs: bool
    size_multiplier: float
    index_chg_pct: float | None
    vs_vwap_pct: float | None
    reason: str


def classify(index_ltp: float | None, index_vwap: float | None,
             index_prev_close: float | None, index_day_low: float | None = None,
             index_day_high: float | None = None) -> MarketContext:
    """
    Pure classification — no I/O, so it is testable against recorded sessions.

    Missing data yields NEUTRAL rather than RISK_ON: an unknown market is not a
    good one, and defaulting to full size when the index feed is broken would be
    the most expensive possible failure mode.
    """
    if not (index_ltp and index_prev_close):
        return MarketContext(NEUTRAL, True, cfg_float("mkt_size_neutral", 0.6),
                             None, None,
                             "index data unavailable — treating as neutral, not as strength")

    chg = (index_ltp - index_prev_close) / index_prev_close * 100.0
    vs_vwap = ((index_ltp - index_vwap) / index_vwap * 100.0) if index_vwap else None

    above_vwap = vs_vwap is not None and vs_vwap > 0
    above_prev = chg > 0

    # Near the session low is the specific condition that makes "holding up"
    # stocks dangerous — it means the selling has not finished.
    near_low = (index_day_low is not None and index_day_high is not None
                and index_day_high > index_day_low
                and (index_ltp - index_day_low) / (index_day_high - index_day_low) < 0.25)

    if above_vwap and above_prev:
        strong = chg > cfg_float("mkt_risk_on_min_chg_pct", 0.25)
        return MarketContext(
            RISK_ON, True,
            cfg_float("mkt_size_risk_on", 1.0) if strong else cfg_float("mkt_size_neutral", 0.6),
            round(chg, 2), round(vs_vwap, 2) if vs_vwap is not None else None,
            f"index +{chg:.2f}% and above VWAP — longs are with the market")

    if not above_vwap and not above_prev and near_low:
        return MarketContext(
            RISK_OFF, False, 0.0, round(chg, 2),
            round(vs_vwap, 2) if vs_vwap is not None else None,
            f"index {chg:.2f}%, below VWAP and near session lows — no new longs. "
            f"Intraday correlation rises in selloffs, so six independent-looking "
            f"setups would be one bet on the market")

    if not above_vwap:
        return MarketContext(
            CAUTION, True, cfg_float("mkt_size_caution", 0.35),
            round(chg, 2), round(vs_vwap, 2) if vs_vwap is not None else None,
            f"index {chg:+.2f}% but under VWAP — minimal size only")

    return MarketContext(
        NEUTRAL, True, cfg_float("mkt_size_neutral", 0.6),
        round(chg, 2), round(vs_vwap, 2) if vs_vwap is not None else None,
        f"index {chg:+.2f}%, mixed signals — reduced size")


def from_context(index_ctx) -> MarketContext:
    """Build from a SymbolContext assembled for the index itself."""
    if index_ctx is None:
        return classify(None, None, None)
    return classify(index_ctx.ltp, index_ctx.vwap, index_ctx.prev_close,
                    index_ctx.day_low, index_ctx.day_high)


def log_state(mc: MarketContext) -> None:
    fn = logger.warning if mc.state in (CAUTION, RISK_OFF) else logger.info
    fn(f"  Market: {mc.state} — {mc.reason} (size ×{mc.size_multiplier:g})")
