"""
Bars built from ticks (11-Aug-2026) — see intraday/bar_builder.py and
engine.py::merge_live_bars.

WHAT THIS CATCHES
------------------
SymbolContext.bars was built exclusively from kite.historical_data(), capped
at intraday_max_universe (40) names and refreshed once per 300s slow tick.
bar_builder.BarBuilder aggregates the tick stream itself into the same Bar
shape at zero incremental API cost, and IntradayEngine.merge_live_bars()
extends context bars from it every 15s for the full bench.

These tests pin: a tick folds into the correct minute bucket; a minute
rollover closes the previous bucket and starts a new one; the still-open
bucket is never returned as if it were finished; volume is the DELTA of
cumulative session volume across the bucket, not the cumulative figure
itself; an LTP-only tick (no cum_volume) still produces a priced bar with
zero volume rather than being dropped; a new trading day resets state so
yesterday's bars can never leak into today's; and the engine-level wiring —
extending an EXISTING context's bar list without touching its scalar fields,
standing up a NEW context for a bench-only symbol only once the minimum bar
count is met, and leaving everything untouched when the switch is off.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from tests import cfg_ctx


def _mk_builder():
    from intraday.bar_builder import BarBuilder
    return BarBuilder()


def _t(base: datetime, minute_offset: float) -> datetime:
    return base + timedelta(minutes=minute_offset)


def test_ticks_within_one_minute_form_a_single_bar():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base)
    b.record_tick("X", 101.0, base + timedelta(seconds=10))
    b.record_tick("X", 99.5, base + timedelta(seconds=20))
    b.record_tick("X", 100.5, base + timedelta(seconds=59))
    assert b.bar_count("X") == 0, "the bucket is still open — nothing has closed yet"
    # Roll into the next minute to force the first bucket closed.
    b.record_tick("X", 100.8, base + timedelta(minutes=1, seconds=1))
    bars = b.closed_bars("X")
    assert len(bars) == 1, f"expected exactly one closed bar, got {len(bars)}"
    bar = bars[0]
    assert bar.open == 100.0, f"open {bar.open} — should be the FIRST tick's price"
    assert bar.high == 101.0, f"high {bar.high}"
    assert bar.low == 99.5, f"low {bar.low}"
    assert bar.close == 100.5, f"close {bar.close} — should be the LAST tick's price"


def test_the_open_bucket_is_never_returned_as_closed():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base)
    b.record_tick("X", 105.0, base + timedelta(seconds=30))
    assert b.closed_bars("X") == [], (
        "a still-forming minute must not be readable as a finished one — "
        "an engine reading it early would see a truncated range")


def test_a_minute_rollover_starts_a_fresh_bucket():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base)
    b.record_tick("X", 110.0, base + timedelta(minutes=1))
    b.record_tick("X", 111.0, base + timedelta(minutes=1, seconds=30))
    b.record_tick("X", 90.0, base + timedelta(minutes=2))  # closes bucket 2
    bars = b.closed_bars("X")
    assert len(bars) == 2, f"expected 2 closed bars, got {len(bars)}"
    assert bars[0].ts < bars[1].ts, "closed_bars() must be oldest-first"
    assert bars[1].open == 110.0 and bars[1].high == 111.0


def test_volume_is_the_delta_across_the_bucket_not_the_cumulative_reading():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base, cum_volume=50_000)
    b.record_tick("X", 101.0, base + timedelta(seconds=30), cum_volume=52_500)
    b.record_tick("X", 100.0, base + timedelta(minutes=1), cum_volume=53_000)
    bars = b.closed_bars("X")
    assert len(bars) == 1
    assert bars[0].volume == 2500.0, (
        f"volume {bars[0].volume} — must be 52500-50000=2500, the traded "
        f"delta, not 52500 (Kite's own cumulative-since-open figure) or "
        f"53000 (the reading that actually closed the bucket)")


def test_ltp_only_ticks_still_produce_a_priced_bar_with_zero_volume():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base)                                # no cum_volume
    b.record_tick("X", 102.0, base + timedelta(seconds=30))
    b.record_tick("X", 101.0, base + timedelta(minutes=1))
    bars = b.closed_bars("X")
    assert len(bars) == 1
    assert bars[0].high == 102.0, (
        "an LTP-mode tick must still contribute PRICE information — "
        "missing volume is not a reason to drop the bar entirely")
    assert bars[0].volume == 0.0


def test_a_new_trading_day_resets_state():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    day1 = datetime(2026, 8, 10, 15, 25, 0, tzinfo=IST)
    b.record_tick("X", 100.0, day1)
    b.record_tick("X", 101.0, day1 + timedelta(minutes=1))
    assert b.bar_count("X") == 1
    day2 = datetime(2026, 8, 11, 9, 15, 0, tzinfo=IST)
    b.record_tick("X", 200.0, day2)
    assert b.bar_count("X") == 0, (
        "yesterday's closed bar must not survive into a new session — "
        "an engine reading it would think a 09:15 open had a prior bar "
        "from six hours in the past")


def test_since_filters_to_only_bars_at_or_after_the_cutoff():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    for i in range(4):
        b.record_tick("X", 100.0 + i, base + timedelta(minutes=i))
    # 4 ticks at minutes 0,1,2,3 close buckets for minutes 0,1,2 (3 is still open)
    all_bars = b.closed_bars("X")
    assert len(all_bars) == 3
    since = all_bars[1].ts
    filtered = b.closed_bars("X", since=since)
    assert len(filtered) == 2, f"expected 2 bars at/after {since}, got {len(filtered)}"


def test_distinct_symbols_do_not_share_buckets():
    from intraday.bar_builder import BarBuilder
    b = BarBuilder()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    b.record_tick("X", 100.0, base)
    b.record_tick("Y", 500.0, base)
    b.record_tick("X", 101.0, base + timedelta(minutes=1))
    b.record_tick("Y", 505.0, base + timedelta(minutes=1))
    assert b.closed_bars("X")[0].open == 100.0
    assert b.closed_bars("Y")[0].open == 500.0


# ── engine-level wiring: merge_live_bars() ──────────────────────────────────

class _FakeFeed:
    """Enough of PriceFeed's read surface for merge_live_bars()."""
    def __init__(self, bars_by_symbol: dict, prices: dict | None = None):
        self._bars = bars_by_symbol
        self._prices = prices or {}

    def bars(self, symbol, since=None):
        return self._bars.get(symbol, [])

    def get(self, symbol):
        return self._prices.get(symbol)


def _bar(ts, o, h, l, c, v=1000.0):
    from intraday.strategies.base import Bar
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def _entry(symbol, score=0.5):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0,
                         atr_pct=2.0, delivery_pct=30.0, sector="X",
                         score=score, reason="", avg_vol_20d=100_000.0)


def _mk_engine():
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)  # bypass __init__'s I/O
    eng._contexts = {}
    eng._daily_ref = {}
    eng._bench = []
    eng._last_bars_log_at = datetime.fromtimestamp(0, IST)
    return eng


def test_existing_context_gets_new_bars_appended_not_replaced():
    from intraday.strategies.base import SymbolContext
    eng = _mk_engine()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    old_bar = _bar(base, 100, 101, 99, 100.5)
    eng._contexts["AUBANK"] = SymbolContext(
        symbol="AUBANK", ltp=100.5, bars=[old_bar],
        day_high=101, day_low=99, vwap=100.2)
    eng._bench = [_entry("AUBANK")]
    new_bar = _bar(base + timedelta(minutes=1), 100.5, 103, 100, 102)
    feed = _FakeFeed({"AUBANK": [old_bar, new_bar]})
    with cfg_ctx({"intraday_live_bars_enabled": "true",
                  "intraday_min_live_bars_for_context": "5"}):
        touched = eng.merge_live_bars(feed)
    assert touched == 1
    ctx = eng._contexts["AUBANK"]
    assert len(ctx.bars) == 2, f"expected old+new bar, got {len(ctx.bars)}"
    assert ctx.day_high == 101, (
        "merge_live_bars must not touch scalar fields — that is "
        "apply_live_quotes()'s job, not this one's")


def test_bench_only_symbol_gets_no_context_below_the_minimum_bar_count():
    eng = _mk_engine()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    eng._bench = [_entry("NEWNAME")]
    bars = [_bar(base + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100 + i)
            for i in range(3)]  # only 3 — below the default floor of 5
    feed = _FakeFeed({"NEWNAME": bars})
    with cfg_ctx({"intraday_live_bars_enabled": "true",
                  "intraday_min_live_bars_for_context": "5"}):
        touched = eng.merge_live_bars(feed)
    assert touched == 0
    assert "NEWNAME" not in eng._contexts, (
        "a bench name with only 3 live bars must read as 'not enough bars "
        "yet', the same guard the historical side applies at len(raw) < 5 "
        "— not be handed to an engine as a thin, misleading context")


def test_bench_only_symbol_stands_up_a_context_once_the_floor_is_met():
    eng = _mk_engine()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    eng._bench = [_entry("NEWNAME")]
    eng._daily_ref = {"NEWNAME": {"close": 95.0, "high": 96.0, "low": 94.0,
                                  "atr_pct": 2.5, "volume": 200_000.0,
                                  "value_cr": 40.0, "sector": "IT"}}
    bars = [_bar(base + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100 + i)
            for i in range(6)]
    feed = _FakeFeed({"NEWNAME": bars}, prices={"NEWNAME": 106.0})
    with cfg_ctx({"intraday_live_bars_enabled": "true",
                  "intraday_min_live_bars_for_context": "5"}):
        touched = eng.merge_live_bars(feed)
    assert touched == 1
    ctx = eng._contexts.get("NEWNAME")
    assert ctx is not None, "6 live bars clears the floor of 5 — a context should exist"
    assert ctx.ltp == 106.0
    assert ctx.prev_close == 95.0, "daily-reference row must seed prev_close"
    assert ctx.sector == "IT"
    assert len(ctx.bars) == 6
    assert ctx.live_fields == ("bars",)


def test_switch_off_leaves_contexts_completely_untouched():
    from intraday.strategies.base import SymbolContext
    eng = _mk_engine()
    base = datetime(2026, 8, 11, 9, 20, 0, tzinfo=IST)
    eng._contexts["AUBANK"] = SymbolContext(symbol="AUBANK", ltp=100.0, bars=[])
    eng._bench = [_entry("AUBANK"), _entry("NEWNAME")]
    bars = [_bar(base + timedelta(minutes=i), 100, 101, 99, 100) for i in range(6)]
    feed = _FakeFeed({"AUBANK": bars, "NEWNAME": bars})
    with cfg_ctx({"intraday_live_bars_enabled": "false"}):
        touched = eng.merge_live_bars(feed)
    assert touched == 0
    assert eng._contexts["AUBANK"].bars == [], "OFF must restore exactly today's behaviour"
    assert "NEWNAME" not in eng._contexts


def test_no_feed_is_a_no_op_not_a_crash():
    eng = _mk_engine()
    with cfg_ctx({"intraday_live_bars_enabled": "true"}):
        assert eng.merge_live_bars(None) == 0


TESTS = [
    ("ticks within one minute form a single bar", test_ticks_within_one_minute_form_a_single_bar),
    ("the open bucket is never returned as closed", test_the_open_bucket_is_never_returned_as_closed),
    ("a minute rollover starts a fresh bucket", test_a_minute_rollover_starts_a_fresh_bucket),
    ("volume is the delta across the bucket, not the cumulative reading",
     test_volume_is_the_delta_across_the_bucket_not_the_cumulative_reading),
    ("LTP-only ticks still produce a priced bar with zero volume",
     test_ltp_only_ticks_still_produce_a_priced_bar_with_zero_volume),
    ("a new trading day resets state", test_a_new_trading_day_resets_state),
    ("since filters to only bars at or after the cutoff",
     test_since_filters_to_only_bars_at_or_after_the_cutoff),
    ("distinct symbols do not share buckets", test_distinct_symbols_do_not_share_buckets),
    ("existing context gets new bars appended, not replaced",
     test_existing_context_gets_new_bars_appended_not_replaced),
    ("bench-only symbol gets no context below the minimum bar count",
     test_bench_only_symbol_gets_no_context_below_the_minimum_bar_count),
    ("bench-only symbol stands up a context once the floor is met",
     test_bench_only_symbol_stands_up_a_context_once_the_floor_is_met),
    ("switch off leaves contexts completely untouched",
     test_switch_off_leaves_contexts_completely_untouched),
    ("no feed is a no-op, not a crash", test_no_feed_is_a_no_op_not_a_crash),
]
