"""
`apply_live_quotes` must actually reach and run its live-overlay logic, and
the range/vwap split must isolate exactly as designed.

WHAT THIS CATCHES — 07-Aug-2026 (original)
--------------------------------------------
`now` was referenced at three points in this function (the parity-log
interval check, and `as_of` on both the per-symbol and index overlays) and
assigned at none of them. With `intraday_quote_parity_log` on, the very first
reference raised `NameError` before the function ever reached the
quote-mode branch that actually overlays live day-range/VWAP/volume onto the
contexts engines read. `cycle()`'s outer `except Exception: logger.debug(...)`
swallowed it below normal log level, so quote mode had been live in
system_config since 04-Aug with zero effect — every intraday engine kept
reading contexts no fresher than the 300-second refresh_contexts() cycle, and
intraday_quote_parity — logged from inside the exact block that crashed —
collected zero rows across three days of market sessions despite being
armed the whole time.

WHAT THIS ALSO CATCHES — 08-Aug-2026 (the range/vwap split)
--------------------------------------------------------------
tools/quote_parity.py measured the six overlaid fields separately and found
day_high/day_low/day_open held clean while vwap and prev_close both FAULTed.
A single `intraday_quote_mode` switch could not promote the clean fields
without also promoting the faulty ones. `intraday_quote_mode_range` and
`intraday_quote_mode_vwap` are now independent, and `prev_close` is removed
from the overlay entirely (it is time-invariant intraday, so a live overlay
was never the right fix for its FAULT — that traces to a correctness bug in
refresh_contexts()'s own stock_data_daily lookup). These tests assert the
isolation directly: range-on-vwap-off touches range fields only, vwap-on-
range-off touches vwap only, and prev_close never moves under any
combination.
"""

from __future__ import annotations

from tests import cfg_ctx


class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


class _FakeFeed:
    """One symbol's live QUOTE-mode tick, always available."""
    def __init__(self, quotes: dict[str, dict]):
        self._quotes = quotes

    def quote(self, symbol: str):
        return self._quotes.get(symbol)


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_NoDB())


def _ctx(symbol: str):
    from intraday.strategies.base import SymbolContext
    return SymbolContext(
        symbol=symbol, ltp=100.0, bars=[],
        day_high=101.0, day_low=99.0, vwap=100.2, prev_close=98.0,
    )


_LIVE_TICK = {
    "day_high": 103.5, "day_low": 98.7, "day_open": 100.0,
    "prev_close": 111.0,  # deliberately far off — must never land regardless
    "volume": 12345.0, "vwap": 101.1,
}


def test_apply_live_quotes_does_not_crash_with_both_switches_on():
    """The exact combination live in system_config since 04-Aug: both true."""
    with cfg_ctx({"intraday_quote_mode_range": "true",
                  "intraday_quote_mode_vwap": "true",
                  "intraday_quote_parity_log": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _LIVE_TICK})
        touched = eng.apply_live_quotes(feed)   # must not raise NameError
        assert touched == 1, f"expected 1 context touched, got {touched}"


def test_range_on_vwap_off_touches_range_fields_only():
    with cfg_ctx({"intraday_quote_mode_range": "true",
                  "intraday_quote_mode_vwap": "false"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _LIVE_TICK})
        eng.apply_live_quotes(feed)
        ctx = eng._contexts["RELIANCE"]
        assert ctx.day_high == 103.5, "range switch on — day_high must overlay"
        assert ctx.day_low == 98.7
        assert ctx.day_open == 100.0
        assert ctx.session_volume == 12345.0
        assert ctx.vwap == 100.2, (
            f"vwap switch is OFF but vwap moved to {ctx.vwap} — the two "
            f"switches are not actually independent")
        assert "vwap" not in ctx.live_fields


def test_vwap_on_range_off_touches_vwap_only():
    with cfg_ctx({"intraday_quote_mode_range": "false",
                  "intraday_quote_mode_vwap": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _LIVE_TICK})
        eng.apply_live_quotes(feed)
        ctx = eng._contexts["RELIANCE"]
        assert ctx.vwap == 101.1, "vwap switch on — vwap must overlay"
        assert ctx.day_high == 101.0, (
            f"range switch is OFF but day_high moved to {ctx.day_high} — the "
            f"two switches are not actually independent")
        assert ctx.session_volume is None, "volume is grouped under range, must stay off"
        assert "day_high" not in ctx.live_fields


def test_prev_close_never_overlays_under_any_combination():
    """The field removed entirely — must not move even with both switches on."""
    with cfg_ctx({"intraday_quote_mode_range": "true",
                  "intraday_quote_mode_vwap": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _LIVE_TICK})
        eng.apply_live_quotes(feed)
        ctx = eng._contexts["RELIANCE"]
        assert ctx.prev_close == 98.0, (
            f"prev_close is {ctx.prev_close}, the live tick's 111.0 landed — "
            f"prev_close must never be overlaid, live or not")
        assert "prev_close" not in ctx.live_fields


def test_both_switches_off_touches_nothing():
    with cfg_ctx({"intraday_quote_mode_range": "false",
                  "intraday_quote_mode_vwap": "false"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _LIVE_TICK})
        touched = eng.apply_live_quotes(feed)
        assert touched == 0
        ctx = eng._contexts["RELIANCE"]
        assert ctx.day_high == 101.0 and ctx.vwap == 100.2


def test_apply_live_quotes_still_works_with_parity_logging_off():
    """Regression guard: the fix must not depend on the parity switch."""
    with cfg_ctx({"intraday_quote_mode_range": "true",
                  "intraday_quote_parity_log": "false"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": {"day_high": 105.0}})
        touched = eng.apply_live_quotes(feed)
        assert touched == 1
        assert eng._contexts["RELIANCE"].day_high == 105.0


def test_parity_batch_includes_volume_unscored():
    """
    08-Aug-2026: volume was documented as 'logged for visibility' by
    quote_parity.py's own docstring but no fetched-side value was ever
    computed to log it against — the comparison loop simply never included
    it. This asserts a volume row is now actually produced.
    """
    class _RecordingSB:
        def __init__(self):
            self.inserted = []

        def table(self, name):
            assert name == "intraday_quote_parity"
            return self

        def insert(self, rows):
            self.inserted.extend(rows)
            return self

        def execute(self):
            class _R:
                data = []
            return _R()

    from intraday.strategies.base import Bar
    from datetime import datetime
    from config import IST

    with cfg_ctx({"intraday_quote_parity_log": "true",
                  "intraday_context_refresh_s": "0"}):
        eng = _engine()
        eng.sb = _RecordingSB()
        bar = Bar(datetime(2026, 8, 7, 10, 0, tzinfo=IST), 100, 101, 99, 100.5, 5000)
        ctx = _ctx("RELIANCE")
        ctx.bars = [bar]
        eng._contexts = {"RELIANCE": ctx}
        feed = _FakeFeed({"RELIANCE": {**_LIVE_TICK, "volume": 5200.0}})
        eng.apply_live_quotes(feed)
        fields = {r["field"] for r in eng.sb.inserted}
        assert "volume" in fields, (
            f"volume was not in the parity batch — only got {fields}")
        vol_row = next(r for r in eng.sb.inserted if r["field"] == "volume")
        assert vol_row["fetched_value"] == 5000.0, (
            f"expected bar-summed volume 5000.0, got {vol_row['fetched_value']}")


TESTS = [
    ("apply_live_quotes does not crash with both switches on",
     test_apply_live_quotes_does_not_crash_with_both_switches_on),
    ("range on / vwap off touches range fields only",
     test_range_on_vwap_off_touches_range_fields_only),
    ("vwap on / range off touches vwap only",
     test_vwap_on_range_off_touches_vwap_only),
    ("prev_close never overlays under any combination",
     test_prev_close_never_overlays_under_any_combination),
    ("both switches off touches nothing",
     test_both_switches_off_touches_nothing),
    ("apply_live_quotes still works with parity logging off",
     test_apply_live_quotes_still_works_with_parity_logging_off),
    ("parity batch includes volume, unscored",
     test_parity_batch_includes_volume_unscored),
]
