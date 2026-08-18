"""
Minute bars aggregated from the tick stream itself, not fetched.

WHY THIS EXISTS — 11-Aug-2026
------------------------------
`IntradayEngine.refresh_contexts()` is the only source of `SymbolContext.bars`,
and it is built exclusively from `kite.historical_data()` — one REST call per
symbol, on an endpoint rate limited far more tightly than quotes, refreshed
once per 300-second slow tick. Two consequences follow directly from that,
and both were named in the 11-Aug institutional review as the binding
constraint on trade selection — not engine logic, not the allocator:

  1. LATENCY. A breakout at 10:47 is judged against bars that are up to five
     minutes stale, because the bars only update on the slow timer.
  2. UNIVERSE SIZE. Only `intraday_max_universe` (40) symbols get bars at
     all, because 40 `historical_data` calls per refresh is already close to
     what the rate limit tolerates. A name outside that 40 is invisible to
     every engine regardless of how it is moving right now — the live
     re-rank (scanner.live_rerank(), 10-Aug) can promote it into the watched
     40, but only on the NEXT 300s slow tick, so it still cannot be traded
     on until bars exist for it.

Both are the SAME constraint — the historical_data budget — and this module
routes around it rather than raising it. Every tick the websocket already
delivers is enough to build a real OHLCV minute bar with zero incremental API
cost: quote mode carries cumulative day volume (see price_feed.py), so a
volume figure is available too, not just price. This class turns that stream
into exactly the `Bar` shape engines already read, so `SymbolContext.bars`
can be extended from it with no change to a single engine — see
`engine.py::merge_live_bars`, the other half of this fix.

WHAT THIS DOES NOT REPLACE. `historical_data()` stays the source of the
FIRST bars of a session — a tick-built bar cannot reconstruct 09:15-09:20 if
the daemon connects at 09:22, and a freshly-subscribed symbol has no history
at all. `merge_live_bars()` backfills from history first and only appends or
stands up live-built bars once the tick stream has run long enough to trust,
the same "not enough bars yet" guard `refresh_contexts()` already applies to
the historical side (`len(raw) < 5: continue`).

PURE AGGREGATION, SEPARATE FROM THE THREAD THAT FEEDS IT. `record_tick` is
the only method that mutates state and is O(1) — a dict lookup and a few
float comparisons — so it is safe to call from `PriceFeed.on_ticks`, which is
documented to allow no I/O and no logging because it runs on the websocket
thread for every tick of every watched symbol. Reads (`closed_bars`) copy out
from behind the same lock so a strategy evaluating one symbol is never left
holding a reference into memory a tick handler is mutating.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# THE CONTINUOUS SESSION, AND WHY TICKS OUTSIDE IT ARE NOT BARS — 17-Aug-2026.
#
# The socket is subscribed from 09:00, fifteen minutes before the continuous
# session opens, and Kite delivers ticks throughout the pre-open call auction.
# Their last_price is the PREVIOUS CLOSE until the auction publishes an
# equilibrium price. `record_tick` folded them like any other tick, so every
# tick-built series began with a ~09:00 bar priced at yesterday's close.
#
# That bar is not a price this stock traded at today, and it sits OUTSIDE
# today's real range in whichever direction the stock gapped. Any max/min over
# the series therefore returns the previous close instead of the day's true
# extreme: on 17-Aug BELRISE's tick-built day_high read 255.35 (its 14-Aug
# close) against a real session high of 247.77, and MAZDOCK's day_low read
# 2580.00 (its 14-Aug close) against a real session low of 2593.00 at that
# moment. Nineteen of the forty names carrying a bench-only context were wrong
# that way inside the first fifteen minutes.
#
# `base.range_between()` was already immune by accident — it anchors on a
# hardcoded 09:15 and offsets from there, so a 09:00 bar lands at minute -15
# and falls out of every window. `volume_ratio()`, `merge_live_bars()`'s
# day_open/day_high/day_low, and anything else taking a plain max/min over
# `ctx.bars` were not. The fix belongs here, at the one place a tick becomes a
# bar, rather than in each consumer remembering to skip a bar it should never
# have been given.
#
# Same reasoning at the other end: the closing session runs to 15:40 and its
# prints are not continuous-session candles either.
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


@dataclass
class _OpenBar:
    minute: datetime
    open: float
    high: float
    low: float
    close: float
    vol_at_open: float | None    # cumulative session volume when this bucket opened
    vol_now: float | None        # latest cumulative session volume seen in it


# Bounds memory and guarantees a stale session can never leak into a new one
# — same reasoning as scanner._cache's date-keyed invalidation, not a size
# one, because a thin symbol should not carry yesterday's handful of bars
# forward indefinitely just because it never accumulates many today.
_MAX_BARS_PER_SYMBOL = 400   # a full 09:15-15:30 session is 375 one-minute bars


class BarBuilder:
    """Per-symbol minute OHLCV, built live from whatever ticks arrive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed: dict[str, list[_OpenBar]] = {}
        self._open: dict[str, _OpenBar] = {}
        self._session_date: date | None = None

    def record_tick(self, symbol: str, price: float, ts: datetime,
                    cum_volume: float | None = None) -> None:
        """
        Fold one tick into the bucket for its minute.

        `cum_volume` is the tick's CUMULATIVE session volume (Kite's
        `volume_traded`, present only in QUOTE/FULL mode), not a per-tick
        size — the bucket's own volume is the delta between the cumulative
        reading when it opened and the latest one seen, computed lazily in
        `closed_bars()`. In LTP mode `cum_volume` is always None and the
        resulting bars simply carry volume=0.0, same as a thin symbol would
        report on the historical side — a strict addition of price
        information, never a worse view than not having live bars at all.
        """
        if not symbol or not price:
            return
        d = ts.date()
        minute = ts.replace(second=0, microsecond=0)
        # Pre-open and post-close prints are not session candles — see
        # SESSION_OPEN above. The day-rollover reset stays OUTSIDE this filter,
        # deliberately: the socket is subscribed from 09:00 and the first tick
        # of the day used to be what cleared yesterday's buckets. Returning
        # early before that reset would leave `closed_bars()` serving
        # yesterday's session to anything that read it between 09:00 and 09:15
        # — a stale-range bug strictly worse than the pre-open one this filter
        # exists to remove.
        in_session = SESSION_OPEN <= ts.time() < SESSION_CLOSE
        with self._lock:
            if self._session_date != d:
                # A NEW SESSION MUST START FROM ZERO BARS, NOT YESTERDAY'S.
                self._closed.clear()
                self._open.clear()
                self._session_date = d
            if not in_session:
                return

            cur = self._open.get(symbol)
            if cur is None or cur.minute != minute:
                if cur is not None:
                    bucket = self._closed.setdefault(symbol, [])
                    bucket.append(cur)
                    if len(bucket) > _MAX_BARS_PER_SYMBOL:
                        del bucket[:-_MAX_BARS_PER_SYMBOL]
                self._open[symbol] = _OpenBar(
                    minute=minute, open=price, high=price, low=price,
                    close=price, vol_at_open=cum_volume, vol_now=cum_volume)
                return

            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            if cum_volume is not None:
                cur.vol_now = cum_volume
                if cur.vol_at_open is None:
                    # First volume-bearing tick of a bucket that opened on an
                    # LTP-only print (e.g. mode switched mid-bucket) — treat
                    # this reading as the baseline rather than inventing a
                    # volume figure for ticks already folded into high/low.
                    cur.vol_at_open = cum_volume

    def closed_bars(self, symbol: str, since: datetime | None = None) -> list:
        """
        Every COMPLETED minute bar for `symbol`, oldest first, as the `Bar`
        shape (`intraday.strategies.base.Bar`) engines already consume. The
        still-forming bucket is deliberately excluded — a partial minute
        must never be read as a finished one, the same reason
        `historical_data()` only ever returns elapsed bars.
        """
        from intraday.strategies.base import Bar
        with self._lock:
            raw = list(self._closed.get(symbol, ()))
        out = [Bar(ts=b.minute, open=b.open, high=b.high, low=b.low,
                   close=b.close,
                   volume=max(0.0, (b.vol_now or 0) - (b.vol_at_open or 0)))
               for b in raw]
        if since is not None:
            out = [b for b in out if b.ts >= since]
        return out

    def bar_count(self, symbol: str) -> int:
        with self._lock:
            return len(self._closed.get(symbol, ()))
