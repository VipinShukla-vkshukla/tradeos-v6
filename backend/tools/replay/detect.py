"""
The detection loop, reproduced — not called.

WHY `registry.evaluate_all()` MUST NOT BE CALLED
------------------------------------------------
`evaluate_all` iterates `enabled_engines()` (`registry.py:152-160`), which reads
**live `system_config`** through `engine_lifecycle()`. An engine RETIRED today
would produce zero detections across the entire replayed history — and the
harness would report "no evidence" for precisely the engine it was built to
judge. That is not a small distortion; it is the inversion of the tool's purpose.

So all nine engines are instantiated directly and lifecycle is recorded as a
COLUMN, never applied as a filter.

BUT THERE IS A SEAM, AND IT IS NOT PAPERED OVER
------------------------------------------------
`evaluate_all` also applies `_invalidation_is_reachable(s)` (`registry.py:168`),
which discards any setup whose stop sits beyond its own stated invalidation —
such a trade has no thesis, only a price limit. Calling `.evaluate()` directly
skips that and would record setups the live system refuses, inflating every
downstream n. **So the filter is imported**, along with `family_of`, rather than
copied. Importing a private helper is deliberate: the alternative is a second
copy of a filter whose absence changes which trades exist.

`meta["sub_engine"]` is set to `s.strategy` and `meta["family"]` to
`family_of(eng.name)`, exactly as `evaluate_all` stamps them. This matters more
than it looks: the live record's `strategy` COLUMN stores the FAMILY, while
`_setup_is_new` dedups on `symbol:strategy` where `strategy` is the SUB-ENGINE.
Getting the two the wrong way round desynchs every dedup count from the live
record's. Both are carried separately on `Detection` for that reason.

WHAT IS NOT REPRODUCED, DELIBERATELY
-------------------------------------
No gates. No cost gate, no structure gate, no AI veto, no conviction floor, no
allocator. Q2 asks about gross R, which is the only quantity both sides of a
gate carry; the complete-population evidence shows no engine separates its taken
half from its refused half at 2 SE (FINDINGS.md §4); and every gate reads live
config, which would apply today's thresholds to last year's bars.

`best` is also not reproduced — the harness records the full `found` list, since
the point is the population, not the pick.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from intraday.session import phase_at
from intraday.strategies.base import Bar, Setup, SymbolContext
# The filter is IMPORTED, not copied — see the module docstring.
from intraday.strategies.registry import _invalidation_is_reachable, family_of
from intraday.strategies.orb import OpeningRangeBreakout
from intraday.strategies.gap_and_go import GapAndGo
from intraday.strategies.prev_day_levels import PrevDayLevelRetest
from intraday.strategies.squeeze import SqueezeExpansion
from intraday.strategies.pullback import TrendPullback
from intraday.strategies.vwap_reclaim import VwapReclaim
from intraday.strategies.range_fade import RangeFade
from intraday.strategies.short_distribution import ShortDistribution
from intraday.strategies.gap_down_bounce import GapDownBounce

from tools.replay.contexts import apply_forming_bar, build_context, bars_before
from tools.replay.conventions import BAR_CLOSE, Convention, evaluation_points

# All nine, instantiated directly. Order matches `registry._ALL` so that any
# confidence tie breaks the same way it would live.
ENGINES = [
    OpeningRangeBreakout(),
    GapAndGo(),
    PrevDayLevelRetest(),
    SqueezeExpansion(),
    TrendPullback(),
    VwapReclaim(),
    RangeFade(),
    ShortDistribution(),
    GapDownBounce(),
]


@dataclass
class Detection:
    """One setup, with the provenance needed to compare it to the live record."""
    trade_date: str
    symbol: str
    ts: datetime
    phase: str
    engine: str          # the registered engine name (ORB, GAP, ...)
    sub_engine: str      # s.strategy — what _setup_is_new dedups on
    family: str          # what the live record's `strategy` column stores
    direction: str
    entry: float
    stop: float
    target: float
    rr: float
    confidence: float
    invalidation: str
    rationale: str = ""
    meta: dict = field(default_factory=dict)
    # Which look produced it: "bar_close", or the forming-bar field sampled
    # ("open"/"high"/"low"). A detection that exists only at a sub-bar look is
    # one the bar-close replay can never find, and counting them is how the
    # tick-vs-bar residual gets a size instead of an adjective.
    look: str = "bar_close"

    @property
    def dedup_key(self) -> tuple:
        """`(trade_date, symbol, sub_engine)` — `_setup_is_new`'s key."""
        return (self.trade_date, self.symbol, self.sub_engine)


def evaluate_one(ctx: SymbolContext, phase: str) -> list[Setup]:
    """
    Every engine against one context. The `evaluate_all` loop, without its
    config-driven filter and without picking a `best`.
    """
    found: list[Setup] = []
    for eng in ENGINES:
        try:
            s = eng.evaluate(ctx, phase)
            if s and not _invalidation_is_reachable(s):
                # Same refusal the live system makes: the structural exit can
                # never fire, so the trade has no thesis, only a price limit.
                s = None
            if s:
                s.meta["lifecycle"] = "REPLAY"   # recorded, never used to filter
                s.meta["sub_engine"] = s.strategy
                s.meta["family"] = family_of(eng.name)
                s.meta["engine"] = eng.name
                found.append(s)
        except Exception as ex:
            # One misbehaving engine must not stop the others — same contract as
            # production, for the same reason.
            logger.debug(f"  replay: {eng.name} failed on {ctx.symbol}: {ex}")
    found.sort(key=lambda s: -s.confidence)
    return found


def replay_symbol_day(symbol: str, day: str, day_bars: list[Bar],
                      prev: dict | None = None,
                      index_bars: list[Bar] | None = None,
                      index_prev_close: float | None = None,
                      dedup_pct: float = 0.35,
                      convention: Convention = BAR_CLOSE) -> list[Detection]:
    """
    Every detection for one symbol on one day, deduplicated as the live loop does.

    CADENCE AND WHAT IS KNOWN AT EACH LOOK — see `conventions.py`. The default
    `BAR_CLOSE` evaluates once per completed minute bar (~375/day) with `ltp`
    equal to that bar's close; the live loop evaluates every 15 s on a tick
    price that, measured over 212 recorded detections, equals no nearby bar
    close 64.6% of the time. The other conventions widen the look; none of them
    can invent the price, and `FULL_OVERLAY` is explicitly a BOUND rather than a
    reproduction.

    DEDUP. `_setup_is_new`'s rule, reproduced: a setup is new when its
    symbol+sub_engine has not been seen today, or when its entry has moved by
    more than `intraday_setup_dedup_pct`. Reproduced rather than imported
    because the live version is a method on the daemon holding session state.
    """
    if not day_bars:
        return []

    out: list[Detection] = []
    seen: dict[str, float] = {}          # sub_engine key -> last recorded entry

    idx_prev = index_prev_close
    for pt in evaluation_points(day_bars, convention):
        now = pt.now

        phase = phase_at(now)
        if phase in ("CLOSED", "PRE_OPEN"):
            continue

        idx_chg = None
        if index_bars and idx_prev:
            ib = bars_before(index_bars, pt.upto)
            if ib:
                idx_chg = (ib[-1].close - idx_prev) / idx_prev * 100.0

        ctx = build_context(symbol, day_bars, now, prev=prev,
                            index_change_pct=idx_chg, upto=pt.upto)
        if ctx is None:
            continue
        if pt.forming is not None:
            apply_forming_bar(ctx, pt.forming, pt.ltp,
                              extremes=convention.running_extremes,
                              vwap=convention.running_vwap,
                              index_change_pct=idx_chg)

        for s in evaluate_one(ctx, phase):
            key = f"{s.symbol}:{s.strategy}"
            prev_entry = seen.get(key)
            if prev_entry is not None:
                if not (prev_entry and
                        abs(s.entry - prev_entry) / prev_entry > dedup_pct / 100.0):
                    continue
            seen[key] = s.entry
            out.append(Detection(
                trade_date=day, symbol=s.symbol, ts=now, phase=phase,
                engine=s.meta.get("engine", ""), sub_engine=s.strategy,
                family=s.meta.get("family", ""), direction=s.direction,
                entry=s.entry, stop=s.stop, target=s.target, rr=s.rr,
                confidence=s.confidence, invalidation=s.invalidation,
                rationale=s.rationale, meta=dict(s.meta), look=pt.look,
            ))
    return out
