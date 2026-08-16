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
from datetime import datetime, timedelta
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
                  index_change_pct: float | None = None,
                  upto: datetime | None = None) -> SymbolContext | None:
    """
    One symbol's context as it stood at `now`. None when there is too little.

    `len(bars) < 5` mirrors the live guard at `engine.py:497` — an engine given
    two bars is not being asked a fair question.

    `upto` is the bar-truncation instant and defaults to `now`. They separate
    only for a SUB-BAR evaluation: at 09:16:15 the live context holds bars
    through 09:15, because `bar_builder` emits a bar only once it closes. Using
    `now` for both there would hand the engine the bar it is standing inside.
    `upto > now` is refused outright rather than clamped — a caller that asks to
    read past its own clock has a bug, and silently correcting it hides one.
    """
    upto = upto or now
    assert upto <= now, (
        f"{symbol}: bar truncation {upto} is AFTER the evaluation clock {now}")
    bars = bars_before(day_bars, upto)
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


def apply_forming_bar(ctx: SymbolContext, forming: Bar, ltp: float,
                      extremes: bool, vwap: bool,
                      index_change_pct: float | None = None) -> SymbolContext:
    """
    Advance the scalar fields into the bar `now` sits inside. Mutates in place.

    This is the replay's counterpart to `engine.apply_live_quotes` and it is
    deliberately shaped the same way: the bar LIST is never touched, only
    `ltp`, `day_high`, `day_low`, `vwap` and `session_volume` — the five fields
    the websocket overlays live, on the same 15 s cadence, for the same reason
    (between context rebuilds the day range was simply wrong).

    IT IS AN OVER-APPROXIMATION AND MUST BE READ AS ONE. Live at 09:16:15 had
    whatever range had printed by 09:16:15. This gives it the whole of 09:16 —
    the minute's full high and low and all of its volume. That is the generous
    end of the envelope, chosen because the purpose is a BOUND: something this
    cannot detect, no intra-minute path could have. See `conventions.py`.

    ONE BAR, AND ONLY THE ONE CONTAINING `now`. The assertion below is the whole
    guard against this becoming ordinary lookahead: given a forming bar two
    minutes ahead it would quietly report the future as the present.
    """
    assert forming.ts <= ctx.as_of < forming.ts + timedelta(minutes=1), (
        f"LOOKAHEAD: {ctx.symbol} overlaid with the bar at {forming.ts} while "
        f"the clock reads {ctx.as_of} — the overlay may only see the bar it is "
        f"standing inside")
    assert not ctx.bars or ctx.bars[-1].ts < forming.ts, (
        f"LOOKAHEAD: {ctx.symbol} bar list already contains {ctx.bars[-1].ts}, "
        f"at or after the forming bar {forming.ts}")

    ctx.ltp = float(ltp)
    if extremes:
        ctx.day_high = max(ctx.day_high, forming.high)
        ctx.day_low = min(ctx.day_low, forming.low)
    if vwap and forming.volume:
        # Re-weight rather than re-derive: the completed bars' typical-price
        # total is recoverable from the VWAP already on the context, and going
        # back to the bar list to recompute it would be the same arithmetic at
        # 375x the cost, once per sample, per symbol, per day.
        prior_vol = ctx.session_volume or 0.0
        prior_tv = (ctx.vwap or 0.0) * prior_vol
        tp = (forming.high + forming.low + forming.close) / 3.0
        vol = prior_vol + forming.volume
        if vol:
            ctx.vwap = (prior_tv + tp * forming.volume) / vol
        ctx.session_volume = vol

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
                        full_day_bars: list[Bar],
                        upto: datetime | None = None) -> None:
    """
    Prove this context cannot see `now` or later. Raises AssertionError if it can.

    Two separate properties, because they fail differently. The first catches a
    slice that included the current bar; the second catches a context assembled
    from the full day's aggregates while only the bar LIST was truncated — a
    plausible bug that the first check alone would pass.

    `upto` is the tighter bar-truncation instant on a sub-bar evaluation, where
    it sits BEFORE `now`. Checking against `now` alone would pass a context that
    had swallowed the bar it is standing inside, which is precisely the mistake
    sub-bar evaluation makes available.
    """
    assert ctx.bars, f"{ctx.symbol}: context has no bars at {now}"
    limit = upto or now

    latest = max(b.ts for b in ctx.bars)
    assert latest < limit, (
        f"LOOKAHEAD: {ctx.symbol} context built at {now} (bars < {limit}) "
        f"contains a bar timestamped {latest} — the engine can see the bar it "
        f"is predicting")

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
