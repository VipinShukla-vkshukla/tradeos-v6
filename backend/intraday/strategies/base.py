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

from config import IST, cfg, cfg_float
from intraday import direction as D


def confirmation_pct(structure_width_pct: float, frac: float) -> float:
    """
    How far past a level price must be before the break COUNTS, in percent.

    WHY THIS EXISTS — 12-Aug-2026. ORB and VCE both bounded the break from
    above (`break_pct > max_chase` → the move is spent) and not from below, so
    a break by two paise was a valid entry. ORB's own module docstring lists
    the missing filter by name — "retest OR strength … a break by two paise is
    a quote, not a signal" — and ORB additionally paid `break_pct < 0.2` a
    +0.10 confidence BONUS, so the least confirmed breaks scored the highest.

    On 12-Aug that produced four ORB entries at +0.02–0.03% over the range and
    three VCE entries at +0.04–0.19% over the coil. All seven were closed
    SETUP_INVALIDATED within minutes, for −0.15R to −0.47R.

    THE FLOOR IS THE INVALIDATION BUFFER, AND THAT IS THE WHOLE POINT.
    `_invalidated()` cuts a long at `level * (1 - intraday_invalidation_buffer
    _pct)`. Entering at `level * (1 + 0.0002)` therefore opens a trade that is
    already inside its own kill zone: SBIN was entered 0.06% above the range
    high with the invalidation line 0.12% below it — 0.18% of total room, 0.15R
    against a 1.0R stop. No edge survives being measured on that scale; the
    trade is decided by the spread. Deriving the entry threshold from the same
    config key the exit reads means the two cannot drift apart — the project's
    "a gate and the thing it gates must be the SAME QUANTITY" rule, applied to
    an entry and its own exit.

    `frac` scales the second requirement with the structure being broken: a
    2.8%-wide opening range needs more than a 1.4%-wide one to call the break
    real. The binding constraint is whichever is larger.
    """
    buf = cfg_float("intraday_invalidation_buffer_pct", 0.12)
    return max(frac * max(structure_width_pct, 0.0), buf)


@dataclass
class RiskFrame:
    """The stop an engine actually gets, and an audit trail of how it got it."""
    stop: float
    risk: float
    risk_pct: float
    structural_stop: float
    capped: bool

    def meta(self) -> dict:
        """
        What survives on disk when the LEGACY branch (`intraday_stop_cap_mode
        =tighten`) is armed -- 18-Aug-2026.

        Under the default `refuse` mode `structural_stop == stop` always, so
        this returns `{}` and adds nothing: a field that is always identical
        to a column already stored (`stop`) is exactly the "silent default"
        this project's own rule warns about, not instrumentation.

        Under `tighten`, `structural_stop` and `stop` diverge, and this is the
        ONLY place that divergence would otherwise be lost -- `_record_setup`
        persists `s.stop` (the capped price), so the level the structure
        actually named was previously discarded before it ever reached disk.
        Recorded here, keyed into the Setup's own `meta` jsonb column, so a
        future operator who re-arms `tighten` to compare against `refuse`
        does not repeat this session's finding blind.
        """
        if not self.capped:
            return {}
        return {"structural_stop": round(self.structural_stop, 2),
                "stop_capped": True,
                "capped_risk_pct": round(self.risk_pct, 3)}


def risk_from_structure(entry: float, structural_stop: float, direction: str,
                        *, max_risk_pct: float) -> "RiskFrame | None":
    """
    Turn a structural stop into the stop the trade will actually carry.

    IT NEVER TIGHTENS THE STOP. THAT IS THE ENTIRE POINT.

    WHAT IT REPLACED, AND WHAT THAT COST -- 18-Aug-2026.

    Eight engines carried the identical four lines:

        stop = <structure> * (1 - buffer/100)
        if (ltp - stop) / ltp * 100 > max_risk:
            stop = ltp * (1 - max_risk / 100)      # <-- here
        risk = ltp - stop

    That last assignment silently discards the structure. The engine finds the
    range low, decides the thesis dies below it, then -- when that stop is
    wider than the account can afford -- moves the stop to a price with no
    structural meaning at all, usually INSIDE the range that produced the
    setup. The trade keeps the structure's prose and loses its geometry.
    `registry._invalidation_is_reachable` (12-Aug, NATIONALUM) is this same
    defect caught at its extreme; it left the ordinary cases alone, and the
    ordinary cases are where the money went.

    MEASURED, on the 1,766 TAKEN-and-resolved rows in `intraday_setups`:

        population              n     gross mean R
        ------------------------------------------
        stop pinned to the cap  798        -0.5348
        structural stop kept    968        +0.0154
        whole book             1766        -0.2403

        per engine, pinned vs structural
        GAP   139 @ -0.235   vs   144 @ +0.587
        VWR    23 @ -0.245   vs   148 @ +0.123
        VCE    30 @ -0.598   vs   120 @ -0.175
        ORB   604 @ -0.631   vs   186 @ -0.534

    GAP is the clean experiment: one engine, one universe, one set of
    sessions, separated only by whether its own stop survived. 0.82R.

    REFUSING IS NOT THE SAME AS SIZING DOWN, AND THE DIFFERENCE IS NOT
    MEASURABLE HERE. A setup whose structural stop is unaffordable could also
    be taken with fewer shares. But `intraday_setups` stores no ATR and no
    pre-cap stop, so what a widened stop would have done cannot be
    reconstructed from any row on disk. Refusal is the conservative branch: it
    is the one whose counterfactual IS on disk, because the trades it declines
    are exactly the ones already recorded at -0.5348R.
    `intraday_stop_cap_mode` keeps the old behaviour one config row away.

    A FLOOR IS AVAILABLE AND INERT BY DEFAULT. The 0.0-0.6% risk band stops
    out 84.3% of the time across 172 rows and every engine in it is negative,
    which argues for a minimum stop distance too. It is not shipped armed,
    because that band is populated entirely by the four engines with the
    tightest caps (PBK 0.80, PDL 0.80, RNG 0.70, VCE 1.00) and this data
    cannot separate "the stop was too tight" from "those engines are bad".
    `intraday_min_risk_pct` is there for when it can.

    Returns None when the setup must be refused. The caller returns None too --
    an engine that cannot place an honest stop has not found a trade.
    """
    risk = D.risk_per_share(entry, structural_stop, direction)
    if risk <= 0 or entry <= 0:
        return None                     # coherent() owns this failure
    risk_pct = risk / entry * 100.0

    floor_pct = cfg_float("intraday_min_risk_pct", 0.0)
    if floor_pct > 0 and risk_pct < floor_pct:
        return None

    if risk_pct <= max_risk_pct:
        return RiskFrame(structural_stop, risk, risk_pct, structural_stop, False)

    if (cfg("intraday_stop_cap_mode", "refuse") or "refuse").lower() != "tighten":
        return None

    # LEGACY BRANCH, kept so the change is one config row from reversible.
    capped_stop = entry - D.sign(direction) * entry * max_risk_pct / 100.0
    capped_risk = D.risk_per_share(entry, capped_stop, direction)
    if capped_risk <= 0:
        return None
    return RiskFrame(round(capped_stop, 2), capped_risk,
                     capped_risk / entry * 100.0, structural_stop, True)


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

    # WHAT THE NON-LIVE SIDE SAID, KEPT SO PARITY HAS SOMETHING TO COMPARE
    # AGAINST — 17-Aug-2026.
    #
    # `apply_live_quotes()` overwrites day_high/day_low/vwap/prev_close in
    # place with the tick values, because that is what every engine must read.
    # The parity logger then read those same attributes and compared them to
    # the tick stream — i.e. to the value it had itself written one cycle
    # earlier. Measured over 38,683 comparisons: prev_close differed on ZERO
    # of them and day_high on 4.5%, and those 4.5% land almost entirely in the
    # two samples right after a context is first built, which is the only
    # moment the attribute still held a fetched number.
    #
    # A check comparing a feed to a copy of itself cannot fail. This dict is
    # written ONLY by refresh_contexts()/merge_live_bars() — the bar and
    # database sides — and never by the overlay, so the comparison stays
    # between two independent sources for as long as it runs.
    fetched: dict = field(default_factory=dict)

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
    """
    A tradeable intraday opportunity, fully specified.

    DIRECTION IS NOW READ, NOT JUST STORED. It was a field on this dataclass and
    a column on `intraday_setups` from the beginning, every engine hardcoded
    "LONG", and nothing consumed it — so these three properties were written as
    if the field did not exist. On a short they returned a negative risk, a
    negative reward, and an rr of exactly 0.0 (the `r > 0` guard), which would
    have sorted every short to the bottom of `evaluate_intraday_setups`' ranking
    and made it look like the engines simply never found good ones.

    The arithmetic lives in `intraday.direction` so that this class, the exit
    ladder, the scorer, the cost model and the outcome resolver cannot disagree
    about what "R" means for a short.
    """
    symbol: str
    strategy: str
    direction: str                     # LONG | SHORT — see intraday/direction.py
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
        """Distance to the stop, as a positive percent, in either direction."""
        if not self.entry:
            return 0.0
        return D.risk_per_share(self.entry, self.stop, self.direction) / self.entry * 100.0

    @property
    def reward_pct(self) -> float:
        """Distance to the target, as a positive percent, in either direction."""
        if not self.entry:
            return 0.0
        return D.reward_per_share(self.entry, self.target, self.direction) / self.entry * 100.0

    @property
    def rr(self) -> float:
        r = D.risk_per_share(self.entry, self.stop, self.direction)
        return D.reward_per_share(self.entry, self.target, self.direction) / r if r > 0 else 0.0

    @property
    def is_short(self) -> bool:
        return D.is_short(self.direction)

    def coherent(self) -> tuple[bool, str]:
        """Are these levels valid FOR THIS DIRECTION? One definition, in D."""
        return D.validate(self.entry, self.stop, self.target, self.direction)


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
