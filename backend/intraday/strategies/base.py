"""
What every intraday engine must produce, and what it is given to work with.

DESIGN NOTE — WHY A SETUP CARRIES ITS OWN INVALIDATION
------------------------------------------------------
A swing signal can afford to say "buy here, stop there" and be re-evaluated
tomorrow. An intraday setup cannot: the thesis that justified it can die in
four minutes, long before the stop is touched, and holding to the stop because
nothing formally invalidated the trade is how a 0.3% loss becomes 1.2%.

So every Setup states the condition under which it is no longer the trade it
was — a VWAP reclaim that loses VWAP again, an ORB that closes back inside the
range — and the engine exits on that, not only on price.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import IST


@dataclass
class Bar:
    """One intraday candle. Minute or 5-minute, as configured."""
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SymbolContext:
    """
    Everything an engine may read about one symbol, assembled once per cycle.

    Assembled centrally rather than fetched per-engine: six engines each pulling
    their own history would multiply API calls by six for identical data, and
    would let two engines disagree about the same bar.
    """
    symbol: str
    ltp: float
    bars: list[Bar]                    # today's bars, oldest first
    vwap: float | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    atr_pct_daily: float | None = None  # daily ATR%, for sanity-checking targets
    avg_volume_20d: float | None = None
    value_cr: float | None = None       # prev day's traded value, Rs crore — analysis.overlays.liquidity_ok()
    sector: str = ""
    # Relative strength against the index, computed the same way for every
    # engine so "strong today" means one thing across the system.
    rs_vs_index_pct: float | None = None

    # ── Provenance, so no decision runs on data of unknown age ──────────────
    #
    # WHY AGE IS A FIELD AND NOT A COMMENT.
    #
    # Bars and reference levels are assembled on the 300-second slow timer,
    # while the decision loop runs every 15. A breakout at 10:47 was therefore
    # judged against a day range built at 10:45 — and nothing anywhere said so,
    # because a stale context and a fresh one were the same object.
    #
    # Worse is the failure that has no upper bound: a dead websocket with a
    # live cache. The prices keep reading, the range keeps reading, everything
    # looks healthy, and every one of those numbers is from whenever the socket
    # died. "Live" becomes confident garbage. The guard is the deliverable, not
    # the quote-mode upgrade.
    as_of: datetime | None = None            # when the reference levels were built
    live_fields: tuple[str, ...] = ()        # which fields came from the tick stream

    # Cumulative traded volume for the session, from the exchange rather than
    # summed from bars. Separate from avg_volume_20d, which is a daily average
    # and answers a different question ("is today busy?" vs "how busy so far?").
    session_volume: float | None = None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        """Seconds since the reference levels were assembled. None if unknown."""
        if self.as_of is None:
            return None
        return ((now or datetime.now(self.as_of.tzinfo)) - self.as_of).total_seconds()

    def is_stale(self, max_age_s: float, now: datetime | None = None) -> bool:
        """
        Too old to decide on.

        Unknown age counts as STALE, deliberately. The alternative reads "we do
        not know when this was true, so proceed", which is the exact reasoning
        that lets a dead feed keep trading.
        """
        age = self.age_seconds(now)
        return age is None or age > max_age_s

    def range_between(self, start_min: int, end_min: int) -> tuple[float, float] | None:
        """High/low between N and M minutes after the open. The ORB primitive."""
        if not self.bars:
            return None
        open_dt = self.bars[0].ts.replace(hour=9, minute=15, second=0, microsecond=0)
        sel = [b for b in self.bars
               if start_min <= (b.ts - open_dt).total_seconds() / 60 < end_min]
        if not sel:
            return None
        return max(b.high for b in sel), min(b.low for b in sel)

    def volume_ratio(self) -> float | None:
        """Today's volume so far against the 20-day average, time-adjusted."""
        if not self.bars or not self.avg_volume_20d:
            return None
        traded = sum(b.volume for b in self.bars)
        open_dt = self.bars[0].ts.replace(hour=9, minute=15, second=0, microsecond=0)
        mins = max(1.0, (self.bars[-1].ts - open_dt).total_seconds() / 60)
        # Scale the daily average down to the elapsed fraction of the session.
        expected = self.avg_volume_20d * (mins / 375.0)
        return round(traded / expected, 2) if expected else None


@dataclass
class Setup:
    """A tradeable intraday opportunity, fully specified."""
    symbol: str
    strategy: str
    direction: str                     # LONG (SHORT is defined but gated off)
    entry: float
    stop: float
    target: float
    confidence: float                  # 0-1, the engine's own conviction
    rationale: str
    invalidation: str                   # the condition that kills the thesis
    valid_phases: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(IST))

    @property
    def risk_pct(self) -> float:
        return (self.entry - self.stop) / self.entry * 100.0 if self.entry else 0.0

    @property
    def reward_pct(self) -> float:
        return (self.target - self.entry) / self.entry * 100.0 if self.entry else 0.0

    @property
    def rr(self) -> float:
        r = self.entry - self.stop
        return (self.target - self.entry) / r if r > 0 else 0.0


class IntradayStrategy(Protocol):
    """
    The contract. Engines are pure: given a context, return a Setup or None.

    No I/O, no broker calls, no database. That is what makes them testable
    against recorded sessions and what keeps six engines from each inventing
    their own view of the same bar.
    """

    name: str
    phases: tuple[str, ...]

    def evaluate(self, ctx: SymbolContext, phase: str) -> Setup | None: ...
