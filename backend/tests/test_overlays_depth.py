"""
analysis/overlays.py::depth_ok() — Stage D4, 24-Aug-2026 (docs/TRADEOS_
ROADMAP.md, Track D, branch feat/intraday-depth-gate).

WHAT THIS COVERS
-----------------
The gating half of the execution-quality depth check: given a live 5-level
order book (Kite FULL-mode shape — {"buy": [...], "sell": [...]}, each a
list of {"price","quantity","orders"} dicts, best price first), does the
spread and resting depth support the already-decided entry at its planned
size? Same shape as liquidity_ok() — protection, not prediction — and the
same "disabled/missing data is never a refusal" discipline this project
insists on for every gate (see CLAUDE.md: "A cold start must be PERMISSIVE,
never 0.0").
"""

from __future__ import annotations

from tests import cfg_ctx


def _book(bid=100.0, ask=100.10, bid_qty=500, ask_qty=500, levels=1):
    """A simple N-level book, same price/qty at every level unless the
    caller wants finer control (tests that need that build the dict by hand)."""
    buy = [{"price": bid - i * 0.05, "quantity": bid_qty, "orders": 3} for i in range(levels)]
    sell = [{"price": ask + i * 0.05, "quantity": ask_qty, "orders": 3} for i in range(levels)]
    return {"buy": buy, "sell": sell}


def test_disabled_gate_waves_everything_through():
    with cfg_ctx({"overlay_depth_enabled": "false"}):
        from analysis.overlays import depth_ok
        # A deliberately terrible book -- would fail every check below --
        # must still pass while the gate itself is off.
        ok, why = depth_ok({"buy": [{"price": 90, "quantity": 1, "orders": 1}],
                            "sell": [{"price": 110, "quantity": 1, "orders": 1}]},
                           "BUY", 10000)
    assert ok is True
    assert "disabled" in why


def test_no_depth_data_is_advisory_not_a_refusal():
    """None means 'FULL mode has not ticked for this symbol yet', not
    'the book is empty' -- capture-side plumbing must never block a trade."""
    with cfg_ctx({"overlay_depth_enabled": "true"}):
        from analysis.overlays import depth_ok
        ok, why = depth_ok(None, "BUY", 100)
    assert ok is True
    assert "no depth data" in why


def test_empty_sides_are_advisory_not_a_refusal():
    with cfg_ctx({"overlay_depth_enabled": "true"}):
        from analysis.overlays import depth_ok
        ok, why = depth_ok({"buy": [], "sell": []}, "BUY", 100)
    assert ok is True
    assert "incomplete" in why


def test_normal_spread_and_depth_clears():
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "0.25",
                  "intraday_depth_levels_checked": "3"}):
        from analysis.overlays import depth_ok
        # 100.0 / 100.10 -- 0.10% spread, well within the 0.25% limit.
        ok, why = depth_ok(_book(levels=3), "BUY", 300)
    assert ok is True
    assert "spread" in why


def test_wide_spread_is_refused():
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "0.25"}):
        from analysis.overlays import depth_ok
        # 100 / 101 -- roughly 1% spread, well past the 0.25% limit.
        ok, why = depth_ok(_book(bid=100.0, ask=101.0), "BUY", 10)
    assert ok is False
    assert "spread" in why


def test_buy_consumes_the_ask_side():
    """A BUY walks the SELL (ask) levels -- thin asks must refuse a BUY
    even when the bid side is deep."""
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "5.0",
                  "intraday_depth_levels_checked": "3"}):
        from analysis.overlays import depth_ok
        book = {"buy": [{"price": 100.0, "quantity": 100000, "orders": 5}],
                "sell": [{"price": 100.10, "quantity": 50, "orders": 1}]}
        ok, why = depth_ok(book, "BUY", 500)
    assert ok is False
    assert "resting" in why


def test_sell_consumes_the_bid_side():
    """The mirror of the above: a SELL walks the BID levels."""
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "5.0",
                  "intraday_depth_levels_checked": "3"}):
        from analysis.overlays import depth_ok
        book = {"buy": [{"price": 100.0, "quantity": 50, "orders": 1}],
                "sell": [{"price": 100.10, "quantity": 100000, "orders": 5}]}
        ok, why = depth_ok(book, "SELL", 500)
    assert ok is False
    assert "resting" in why


def test_depth_summed_across_configured_levels():
    """A single thin top-of-book level should not refuse an order the
    next levels down can actually absorb -- levels_checked controls how
    far depth_ok() is willing to look."""
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "5.0",
                  "intraday_depth_levels_checked": "3"}):
        from analysis.overlays import depth_ok
        book = {"buy": [{"price": 100.0, "quantity": 1000, "orders": 1}],
                "sell": [{"price": 100.05, "quantity": 50, "orders": 1},
                        {"price": 100.10, "quantity": 50, "orders": 1},
                        {"price": 100.15, "quantity": 50, "orders": 1}]}
        ok, why = depth_ok(book, "BUY", 150)
    assert ok is True
    assert "150" in why


def test_depth_gate_ignores_levels_beyond_the_configured_count():
    with cfg_ctx({"overlay_depth_enabled": "true",
                  "intraday_max_spread_pct": "5.0",
                  "intraday_depth_levels_checked": "1"}):
        from analysis.overlays import depth_ok
        book = {"buy": [{"price": 100.0, "quantity": 1000, "orders": 1}],
                "sell": [{"price": 100.05, "quantity": 50, "orders": 1},
                        {"price": 100.10, "quantity": 1000, "orders": 1}]}
        # Only the first ask level (50) counts with levels_checked=1.
        ok, why = depth_ok(book, "BUY", 100)
    assert ok is False


def test_zero_price_levels_are_advisory_not_a_refusal():
    with cfg_ctx({"overlay_depth_enabled": "true"}):
        from analysis.overlays import depth_ok
        book = {"buy": [{"price": 0, "quantity": 100, "orders": 1}],
                "sell": [{"price": 100.10, "quantity": 100, "orders": 1}]}
        ok, why = depth_ok(book, "BUY", 10)
    assert ok is True
    assert "invalid" in why


TESTS = [
    ("disabled gate waves everything through", test_disabled_gate_waves_everything_through),
    ("no depth data is advisory, not a refusal", test_no_depth_data_is_advisory_not_a_refusal),
    ("empty sides are advisory, not a refusal", test_empty_sides_are_advisory_not_a_refusal),
    ("normal spread and depth clears", test_normal_spread_and_depth_clears),
    ("wide spread is refused", test_wide_spread_is_refused),
    ("a BUY consumes the ask side", test_buy_consumes_the_ask_side),
    ("a SELL consumes the bid side", test_sell_consumes_the_bid_side),
    ("depth summed across configured levels", test_depth_summed_across_configured_levels),
    ("depth gate ignores levels beyond the configured count", test_depth_gate_ignores_levels_beyond_the_configured_count),
    ("zero price levels are advisory, not a refusal", test_zero_price_levels_are_advisory_not_a_refusal),
]
