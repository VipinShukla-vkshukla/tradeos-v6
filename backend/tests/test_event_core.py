"""
intraday/event_core.py::check() — Stage D3, 24-Aug-2026 (docs/
TRADEOS_ROADMAP.md, Track D, branch feat/intraday-event-core).

WHAT THIS COVERS
-----------------
The consumer side of the event-driven core. The one property that
matters most: this function must NEVER write to intraday_setups,
execution.paper_broker, or the allocator — only to intraday_event_shadow.
Every test below constructs a fake `engine.sb` that records exactly
which tables were written to, so a regression that starts touching a
real table fails loudly here rather than being caught by inspection.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeCtx:
    def __init__(self, symbol="TEST"):
        self.symbol = symbol
        self.ltp = 100.0
        self.bars = []


class _FakeFeed:
    def __init__(self, dirty=None, prices=None):
        self._dirty = set(dirty or [])
        self._prices = prices or {}
        self.drain_calls = 0

    def drain_dirty(self):
        self.drain_calls += 1
        out = self._dirty
        self._dirty = set()
        return out

    def get(self, symbol):
        return self._prices.get(symbol)


class _FakeQuery:
    def __init__(self, sink, table_name):
        self._sink = sink
        self._table_name = table_name
        self._row = None

    def insert(self, row):
        self._row = row
        return self

    def execute(self):
        self._sink.append((self._table_name, self._row))
        return self


class _FakeSB:
    """Records every (table_name, row) insert() call so a test can assert
    exactly which tables were touched -- the one property that matters
    most for a shadow-only mechanism."""
    def __init__(self):
        self.writes: list[tuple[str, dict]] = []

    def table(self, name):
        return _FakeQuery(self.writes, name)


class _FakeEngine:
    def __init__(self, contexts=None):
        self._contexts = contexts or {}
        self.sb = _FakeSB()
        self.apply_live_quotes_calls = 0
        self.merge_live_bars_calls = 0

    def apply_live_quotes(self, feed):
        self.apply_live_quotes_calls += 1

    def merge_live_bars(self, feed):
        self.merge_live_bars_calls += 1


def _fake_setup(symbol="TEST", strategy="ORB"):
    from intraday.strategies.base import Setup
    return Setup(symbol=symbol, strategy=strategy, direction="LONG",
                entry=100.0, stop=99.0, target=103.0, confidence=0.8,
                rationale="test", invalidation="test",
                meta={"sub_engine": strategy})


def test_check_is_a_noop_when_disabled():
    from intraday.event_core import check
    engine = _FakeEngine(contexts={"TEST": _FakeCtx()})
    feed = _FakeFeed(dirty={"TEST"})
    with cfg_ctx({"intraday_event_core_enabled": "false"}):
        n = check(engine, feed)
    assert n == 0
    assert feed.drain_calls == 0, "must not even drain the feed when disabled"
    assert engine.sb.writes == []


def test_check_is_a_noop_when_nothing_is_dirty():
    from intraday.event_core import check
    engine = _FakeEngine(contexts={"TEST": _FakeCtx()})
    feed = _FakeFeed(dirty=set())
    with cfg_ctx({"intraday_event_core_enabled": "true"}):
        n = check(engine, feed)
    assert n == 0
    assert engine.sb.writes == []


def test_check_skips_a_dirty_symbol_with_no_context():
    """A symbol outside intraday_max_universe has no context yet -- same
    limit the polling loop already has, not a shadow-core-specific gap."""
    from intraday.event_core import check
    engine = _FakeEngine(contexts={})
    feed = _FakeFeed(dirty={"NOCTX"}, prices={"NOCTX": 100.0})
    with cfg_ctx({"intraday_event_core_enabled": "true"}):
        n = check(engine, feed)
    assert n == 0


def test_check_writes_only_to_intraday_event_shadow():
    """THE PROPERTY THAT MATTERS MOST. A detected setup must be logged to
    intraday_event_shadow and NOTHING else -- never intraday_setups,
    never anything execution or allocation owns."""
    from intraday.event_core import check
    from unittest.mock import patch

    engine = _FakeEngine(contexts={"TEST": _FakeCtx()})
    feed = _FakeFeed(dirty={"TEST"}, prices={"TEST": 101.0})
    with cfg_ctx({"intraday_event_core_enabled": "true"}), \
         patch("intraday.strategies.registry.evaluate_all",
              return_value=(_fake_setup(), [])):
        n = check(engine, feed)
    assert n == 1
    assert len(engine.sb.writes) == 1
    table_name, row = engine.sb.writes[0]
    assert table_name == "intraday_event_shadow"
    assert row["symbol"] == "TEST"
    assert row["strategy"] == "ORB"


def test_check_refreshes_live_quotes_and_bars_before_evaluating():
    """Reused, not reimplemented -- the same two calls the polling cycle
    already makes every 15s, so a dirty symbol's context reflects the
    tick that just marked it dirty."""
    from intraday.event_core import check
    from unittest.mock import patch

    engine = _FakeEngine(contexts={"TEST": _FakeCtx()})
    feed = _FakeFeed(dirty={"TEST"}, prices={"TEST": 101.0})
    with cfg_ctx({"intraday_event_core_enabled": "true"}), \
         patch("intraday.strategies.registry.evaluate_all", return_value=(None, [])):
        check(engine, feed)
    assert engine.apply_live_quotes_calls == 1
    assert engine.merge_live_bars_calls == 1


def test_check_updates_context_ltp_from_the_live_feed():
    from intraday.event_core import check
    from unittest.mock import patch

    ctx = _FakeCtx()
    engine = _FakeEngine(contexts={"TEST": ctx})
    feed = _FakeFeed(dirty={"TEST"}, prices={"TEST": 123.45})
    with cfg_ctx({"intraday_event_core_enabled": "true"}), \
         patch("intraday.strategies.registry.evaluate_all", return_value=(None, [])):
        check(engine, feed)
    assert ctx.ltp == 123.45


def test_check_logs_nothing_when_no_setup_is_found():
    from intraday.event_core import check
    from unittest.mock import patch

    engine = _FakeEngine(contexts={"TEST": _FakeCtx()})
    feed = _FakeFeed(dirty={"TEST"}, prices={"TEST": 100.0})
    with cfg_ctx({"intraday_event_core_enabled": "true"}), \
         patch("intraday.strategies.registry.evaluate_all", return_value=(None, [])):
        n = check(engine, feed)
    assert n == 0
    assert engine.sb.writes == []


def test_check_survives_evaluate_all_raising_for_one_symbol():
    """One bad symbol must not stop the shadow pass for the rest --
    matches evaluate_all()'s own per-engine try/except discipline."""
    from intraday.event_core import check
    from unittest.mock import patch

    engine = _FakeEngine(contexts={"BAD": _FakeCtx("BAD"), "GOOD": _FakeCtx("GOOD")})
    feed = _FakeFeed(dirty={"BAD", "GOOD"}, prices={"BAD": 100.0, "GOOD": 100.0})

    def _side_effect(ctx, phase):
        if ctx.symbol == "BAD":
            raise Exception("boom")
        return _fake_setup(symbol="GOOD"), []

    with cfg_ctx({"intraday_event_core_enabled": "true"}), \
         patch("intraday.strategies.registry.evaluate_all", side_effect=_side_effect):
        n = check(engine, feed)
    assert n == 1
    assert engine.sb.writes[0][1]["symbol"] == "GOOD"


def test_check_survives_engine_or_feed_being_none():
    from intraday.event_core import check
    with cfg_ctx({"intraday_event_core_enabled": "true"}):
        assert check(None, _FakeFeed(dirty={"X"})) == 0
        assert check(_FakeEngine(), None) == 0


TESTS = [
    ("check is a no-op when disabled", test_check_is_a_noop_when_disabled),
    ("check is a no-op when nothing is dirty", test_check_is_a_noop_when_nothing_is_dirty),
    ("check skips a dirty symbol with no context", test_check_skips_a_dirty_symbol_with_no_context),
    ("check writes only to intraday_event_shadow", test_check_writes_only_to_intraday_event_shadow),
    ("check refreshes live quotes and bars before evaluating", test_check_refreshes_live_quotes_and_bars_before_evaluating),
    ("check updates context ltp from the live feed", test_check_updates_context_ltp_from_the_live_feed),
    ("check logs nothing when no setup is found", test_check_logs_nothing_when_no_setup_is_found),
    ("check survives evaluate_all raising for one symbol", test_check_survives_evaluate_all_raising_for_one_symbol),
    ("check survives engine or feed being None", test_check_survives_engine_or_feed_being_none),
]
