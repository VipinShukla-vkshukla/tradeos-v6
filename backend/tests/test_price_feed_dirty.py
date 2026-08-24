"""
intraday/price_feed.py::PriceFeed's dirty-symbol tracking — Stage D3,
24-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D, branch feat/intraday-
event-core).

WHAT THIS COVERS
-----------------
The producer side of the event-driven core: a symbol is marked "dirty"
the moment its price has moved meaningfully SINCE THE LAST DRAIN, not
since the immediately-prior tick — comparing tick-to-tick would fire on
ordinary noise; comparing to "since last actually looked at" is the
question that matters for whether a fresh detection pass is worth
running. Runs on the websocket thread (on_ticks() calls _set()), so this
must stay O(1) and side-effect-free beyond the in-memory dict/set it
already is — no I/O, no logging, per price_feed.py's own documented rule.
"""

from __future__ import annotations

from tests import cfg_ctx


def _feed(symbols=("TEST",)):
    from intraday.price_feed import PriceFeed
    return PriceFeed(list(symbols))


def test_first_price_seeds_baseline_without_marking_dirty():
    """Nothing to compare the very first tick against — must not treat
    'no prior price' as an infinite move."""
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed()
        f._set("TEST", 100.0)
    assert f.drain_dirty() == set()


def test_a_small_move_does_not_mark_dirty():
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed()
        f._set("TEST", 100.0)
        f._set("TEST", 100.01)   # 0.01%, under the 0.05% threshold
    assert f.drain_dirty() == set()


def test_a_meaningful_move_marks_dirty():
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed()
        f._set("TEST", 100.0)
        f._set("TEST", 100.10)   # 0.10%, over the 0.05% threshold
    assert f.drain_dirty() == {"TEST"}


def test_drain_clears_the_dirty_set():
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed()
        f._set("TEST", 100.0)
        f._set("TEST", 100.10)
        first = f.drain_dirty()
        second = f.drain_dirty()
    assert first == {"TEST"}
    assert second == set(), "a symbol must not stay dirty across drains"


def test_drain_rebases_to_the_price_at_drain_time_not_the_next_move():
    """After a drain, the NEXT dirty check must be measured from the price
    AT DRAIN TIME, not from the original pre-move baseline -- otherwise a
    symbol that moved once and settled would be re-flagged dirty forever
    on the same historical move."""
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed()
        f._set("TEST", 100.0)
        f._set("TEST", 100.10)   # dirty vs 100.0
        f.drain_dirty()
        f._set("TEST", 100.11)   # +0.01% vs the NEW baseline (100.10) -- not dirty
    assert f.drain_dirty() == set()


def test_multiple_symbols_tracked_independently():
    with cfg_ctx({"intraday_event_core_dirty_pct": "0.05"}):
        f = _feed(["A", "B", "C"])
        f._set("A", 100.0); f._set("B", 200.0); f._set("C", 300.0)
        f._set("A", 100.10)   # dirty
        f._set("B", 200.001)  # not dirty
        # C never ticks again
    assert f.drain_dirty() == {"A"}


def test_zero_threshold_disables_dirty_tracking_entirely():
    with cfg_ctx({"intraday_event_core_dirty_pct": "0"}):
        f = _feed()
        f._set("TEST", 100.0)
        f._set("TEST", 150.0)   # a huge move, but the gate itself is off
    assert f.drain_dirty() == set()


TESTS = [
    ("first price seeds baseline without marking dirty", test_first_price_seeds_baseline_without_marking_dirty),
    ("a small move does not mark dirty", test_a_small_move_does_not_mark_dirty),
    ("a meaningful move marks dirty", test_a_meaningful_move_marks_dirty),
    ("drain clears the dirty set", test_drain_clears_the_dirty_set),
    ("drain rebases to the price at drain time, not the next move", test_drain_rebases_to_the_price_at_drain_time_not_the_next_move),
    ("multiple symbols tracked independently", test_multiple_symbols_tracked_independently),
    ("zero threshold disables dirty tracking entirely", test_zero_threshold_disables_dirty_tracking_entirely),
]
