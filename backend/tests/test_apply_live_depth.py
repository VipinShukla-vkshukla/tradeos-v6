"""
intraday/engine.py::IntradayEngine.apply_live_depth() — Stage D4, 24-Aug-2026
(docs/TRADEOS_ROADMAP.md, Track D, branch feat/intraday-depth-gate).

WHAT THIS COVERS
-----------------
The overlay half of the depth pipeline: feed.depth(symbol) -> ctx.depth,
gated on intraday_depth_mode_enabled the same way apply_live_quotes() is
gated on its own switches (test_apply_live_quotes.py). Ships FALSE, so the
default-config case (nothing touched) is asserted explicitly, matching this
project's "a cold start must be permissive/inert, not silently doing
something" discipline.
"""

from __future__ import annotations

from tests import cfg_ctx


class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


class _FakeFeed:
    def __init__(self, depths: dict[str, dict]):
        self._depths = depths

    def depth(self, symbol: str):
        return self._depths.get(symbol)


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_NoDB())


def _ctx(symbol: str):
    from intraday.strategies.base import SymbolContext
    return SymbolContext(symbol=symbol, ltp=100.0, bars=[])


_BOOK = {"buy": [{"price": 99.9, "quantity": 100, "orders": 2}],
        "sell": [{"price": 100.1, "quantity": 100, "orders": 2}]}


def test_disabled_by_default_touches_nothing():
    with cfg_ctx({"intraday_depth_mode_enabled": "false"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        feed = _FakeFeed({"RELIANCE": _BOOK})
        touched = eng.apply_live_depth(feed)
    assert touched == 0
    assert eng._contexts["RELIANCE"].depth is None


def test_enabled_overlays_depth_onto_matching_contexts():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE"), "TCS": _ctx("TCS")}
        feed = _FakeFeed({"RELIANCE": _BOOK})   # TCS has no depth yet
        touched = eng.apply_live_depth(feed)
    assert touched == 1
    assert eng._contexts["RELIANCE"].depth == _BOOK
    assert eng._contexts["TCS"].depth is None


def test_none_feed_is_a_no_op_not_a_crash():
    """run.py calls cycle(feed=None) on any cycle where the websocket is
    down and polling took over -- apply_live_depth() must survive that."""
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        eng = _engine()
        eng._contexts = {"RELIANCE": _ctx("RELIANCE")}
        touched = eng.apply_live_depth(None)
    assert touched == 0
    assert eng._contexts["RELIANCE"].depth is None


def test_empty_contexts_returns_zero():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        eng = _engine()
        eng._contexts = {}
        feed = _FakeFeed({"RELIANCE": _BOOK})
        touched = eng.apply_live_depth(feed)
    assert touched == 0


TESTS = [
    ("disabled by default touches nothing", test_disabled_by_default_touches_nothing),
    ("enabled overlays depth onto matching contexts", test_enabled_overlays_depth_onto_matching_contexts),
    ("None feed is a no-op, not a crash", test_none_feed_is_a_no_op_not_a_crash),
    ("empty contexts returns zero", test_empty_contexts_returns_zero),
]
