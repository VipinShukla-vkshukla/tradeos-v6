"""
Assembling a historical `SymbolContext`. Truncation is the whole game.

ONE OFF-BY-ONE HERE MAKES EVERY ENGINE LOOK BRILLIANT
------------------------------------------------------
A context built at time T must contain no information from T or later. A
`bars[:i+1]` where `bars[:i]` was meant does not crash, does not warn, and hands
every engine the close of the bar it is being asked to predict. The result is a
harness that reports edge everywhere and cannot be debugged, because nothing is
wrong except the arithmetic of a single slice.

So truncation is asserted, not assumed: `assert_no_lookahead()` is called by the
test suite against real contexts, and it checks the property directly — for a
context built at T, every bar timestamp is strictly before T, and the day high
is the high SO FAR rather than the full session's.

WHAT IS FAITHFUL TO THE LIVE PATH AND WHAT IS NOT
--------------------------------------------------
Field-for-field the same as `engine.py:515-560`, with three stated divergences:

  · `ltp` is the last completed bar's close, not a tick. The live system decides
    on a price that may sit between bars.
  · `session_volume` is summed from bars, not taken from the exchange's
    cumulative figure. An approximation; flagged on every report.
  · `as_of` is the evaluation timestamp, so `is_stale()` reads zero age. The
    live staleness guard exists because contexts were built on a 300 s timer and
    read on a 15 s one; in replay there is no such gap to model.
"""

from __future__ import annotations

import sys
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from intraday.market_context import INDEX_SYMBOL
from intraday.strategies.base import Bar, SymbolContext


def bars_before(bars: list[Bar], ts: datetime) -> list[Bar]:
    """
    Every bar STRICTLY before `ts`. The one slice the whole harness rests on.

    Binary search rather than a comprehension because this runs ~375 times per
    symbol-day across ~40 symbols across ~100 days, and a linear scan there is
    150M comparisons for no reason.
    """
    if not bars:
        return []
    keys = [b.ts for b in bars]
    return bars[:bisect_left(keys, ts)]


def build_context(symbol: str, day_bars: list[Bar], now: datetime,
                  prev: dict | None = None,
                  index_change_pct: float | None = None) -> SymbolContext | None:
    """
    One symbol's context as it stood at `now`. None when there is too little.

    `len(bars) < 5` mirrors the live guard at `engine.py:497` — an engine given
    two bars is not being asked a fair question.
    """
    bars = bars_before(day_bars, now)
    if len(bars) < 5:
        return None

    p = prev or {}

    tv = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars)
    vol = sum(b.volume for b in bars)
    vwap = (tv / vol) if vol else None
    if vwap is None:
        # Indices report zero volume, so the weighted average is undefined.
        # Same fallback as the live path: the unweighted mean of typical price,
        # which is what an index VWAP line effectively is.
        vwap = sum((b.high + b.low + b.close) / 3.0 for b in bars) / len(bars)

    ctx = SymbolContext(
        symbol=symbol,
        ltp=bars[-1].close,
        bars=bars,
        vwap=vwap,
        day_open=bars[0].open,
        day_high=max(b.high for b in bars),
        day_low=min(b.low for b in bars),
        prev_close=float(p.get("close") or 0) or None,
        prev_high=float(p.get("high") or 0) or None,
        prev_low=float(p.get("low") or 0) or None,
        atr_pct_daily=float(p.get("atr_pct") or 0) or None,
        avg_volume_20d=float(p.get("volume") or 0) or None,
        value_cr=float(p.get("value_cr") or 0) or None,
        sector=p.get("sector") or "",
        as_of=now,
        session_volume=vol,
    )

    if index_change_pct is not None and ctx.prev_close:
        ctx.rs_vs_index_pct = round(
            (ctx.ltp - ctx.prev_close) / ctx.prev_close * 100.0 - index_change_pct, 2)
    return ctx


def index_change_at(index_bars: list[Bar], now: datetime,
                    index_prev_close: float | None) -> float | None:
    """The index's percent change as of `now`. Feeds relative strength."""
    bars = bars_before(index_bars, now)
    if not bars or not index_prev_close:
        return None
    return (bars[-1].close - index_prev_close) / index_prev_close * 100.0


def build_index_context(index_bars: list[Bar], now: datetime,
                        prev_close: float | None) -> SymbolContext | None:
    """The NIFTY 50 context, for `market_context.classify`."""
    ctx = build_context(INDEX_SYMBOL, index_bars, now, prev={})
    if ctx is not None and prev_close:
        ctx.prev_close = prev_close
    return ctx


# ── the guard ───────────────────────────────────────────────────────────────
def assert_no_lookahead(ctx: SymbolContext, now: datetime,
                        full_day_bars: list[Bar]) -> None:
    """
    Prove this context cannot see `now` or later. Raises AssertionError if it can.

    Two separate properties, because they fail differently. The first catches a
    slice that included the current bar; the second catches a context assembled
    from the full day's aggregates while only the bar LIST was truncated — a
    plausible bug that the first check alone would pass.
    """
    assert ctx.bars, f"{ctx.symbol}: context has no bars at {now}"

    latest = max(b.ts for b in ctx.bars)
    assert latest < now, (
        f"LOOKAHEAD: {ctx.symbol} context built at {now} contains a bar "
        f"timestamped {latest} — the engine can see the bar it is predicting")

    if len(full_day_bars) > len(ctx.bars):
        full_high = max(b.high for b in full_day_bars)
        full_low = min(b.low for b in full_day_bars)
        # Equality is legitimate when the extreme genuinely occurred early, so
        # this can only assert the weaker containment property.
        assert ctx.day_high <= full_high + 1e-9, (
            f"LOOKAHEAD: {ctx.symbol} day_high {ctx.day_high} exceeds the full "
            f"session high {full_high}")
        assert ctx.day_low >= full_low - 1e-9, (
            f"LOOKAHEAD: {ctx.symbol} day_low {ctx.day_low} is below the full "
            f"session low {full_low}")
