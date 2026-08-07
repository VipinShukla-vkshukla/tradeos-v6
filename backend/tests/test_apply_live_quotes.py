"""
`apply_live_quotes` must actually reach and run its live-overlay logic.

WHAT THIS CATCHES — 07-Aug-2026
--------------------------------
`now` was referenced at three points in this function (the parity-log
interval check, and `as_of` on both the per-symbol and index overlays) and
assigned at none of them. With `intraday_quote_parity_log` on, the very first
reference raised `NameError` before the function ever reached the
`intraday_quote_mode` branch that actually overlays live day-range/VWAP/
volume onto the contexts engines read. `cycle()`'s outer
`except Exception: logger.debug(...)` swallowed it below normal log level, so
`intraday_quote_mode=true` had been live in system_config since 04-Aug with
zero effect — every intraday engine kept reading contexts no fresher than the
300-second refresh_contexts() cycle, and intraday_quote_parity — logged from
inside the exact block that crashed — collected zero rows across three days
of market sessions despite being armed the whole time.

This exercises the real function against a fake feed with BOTH switches on
(the exact combination that was silently broken), and asserts the live
values actually land on the context — not just that no exception is raised.
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
        day_high=101.0, day_low=99.0, vwap=100.2,
    )


def test_apply_live_quotes_does_not_crash_with_both_switches_on():
    """The exact combination live in system_config since 04-Aug: both true."""
    with cfg_ctx({"intraday_quote_mode": "true", "intraday_quote_parity_log": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": {
            "day_high": 103.5, "day_low": 98.7, "day_open": 100.0,
            "prev_close": 99.0, "volume": 12345.0, "vwap": 101.1,
        }})
        touched = eng.apply_live_quotes(feed)   # must not raise NameError
        assert touched == 1, f"expected 1 context touched, got {touched}"


def test_apply_live_quotes_actually_overlays_the_live_values():
    with cfg_ctx({"intraday_quote_mode": "true", "intraday_quote_parity_log": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": {
            "day_high": 103.5, "day_low": 98.7, "day_open": 100.0,
            "prev_close": 99.0, "volume": 12345.0, "vwap": 101.1,
        }})
        eng.apply_live_quotes(feed)
        ctx = eng._contexts["RELIANCE"]
        assert ctx.day_high == 103.5, (
            f"day_high is {ctx.day_high}, the live tick's 103.5 never landed — "
            f"the overlay silently did not run")
        assert ctx.vwap == 101.1
        assert ctx.session_volume == 12345.0
        assert "day_high" in ctx.live_fields


def test_apply_live_quotes_still_works_with_parity_logging_off():
    """Regression guard: the fix must not depend on the parity switch."""
    with cfg_ctx({"intraday_quote_mode": "true", "intraday_quote_parity_log": "false"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": {"day_high": 105.0}})
        touched = eng.apply_live_quotes(feed)
        assert touched == 1
        assert eng._contexts["RELIANCE"].day_high == 105.0


TESTS = [
    ("apply_live_quotes does not crash with both switches on",
     test_apply_live_quotes_does_not_crash_with_both_switches_on),
    ("apply_live_quotes actually overlays the live values",
     test_apply_live_quotes_actually_overlays_the_live_values),
    ("apply_live_quotes still works with parity logging off",
     test_apply_live_quotes_still_works_with_parity_logging_off),
]
