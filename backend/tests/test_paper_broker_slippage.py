"""
execution/paper_broker.py::_slippage_pct()/simulate_fill() — Stage D2h,
24-Aug-2026 (docs/FINDINGS.md F-61).

WHAT THIS COVERS
-----------------
Found while auditing Track D's widened intraday universe: paper slippage
was ONE flat figure (`cost_slippage_bps`) for every symbol, so a
Population B/C fill — structurally the thinnest names in the whole
universe — was modelled with the same execution quality as a Nifty-50
name. `value_cr`, threaded from Setup.meta through simulate_fill(), scales
slippage up below a configurable liquidity threshold. `value_cr=None`
(every pre-Stage-D2h call site, including swing's own entry) must be a
true no-op — this is additive, not a silent tightening of every fill.
"""

from __future__ import annotations

from tests import cfg_ctx


def test_slippage_pct_flat_when_value_cr_is_none():
    from execution.paper_broker import _slippage_pct
    with cfg_ctx({"cost_slippage_bps": "5.0"}):
        assert _slippage_pct(None) == 0.0005


def test_slippage_pct_unchanged_for_a_liquid_name():
    from execution.paper_broker import _slippage_pct
    with cfg_ctx({"cost_slippage_bps": "5.0",
                  "cost_slippage_thin_threshold_cr": "25.0",
                  "cost_slippage_thin_multiplier": "3.0"}):
        assert _slippage_pct(500.0) == 0.0005


def test_slippage_pct_scaled_up_for_a_thin_name():
    from execution.paper_broker import _slippage_pct
    with cfg_ctx({"cost_slippage_bps": "5.0",
                  "cost_slippage_thin_threshold_cr": "25.0",
                  "cost_slippage_thin_multiplier": "3.0"}):
        assert _slippage_pct(10.0) == 0.0015


def test_slippage_pct_unchanged_at_exactly_the_threshold():
    """The boundary itself is still "liquid enough" -- only STRICTLY below
    the threshold widens slippage."""
    from execution.paper_broker import _slippage_pct
    with cfg_ctx({"cost_slippage_bps": "5.0",
                  "cost_slippage_thin_threshold_cr": "25.0",
                  "cost_slippage_thin_multiplier": "3.0"}):
        assert _slippage_pct(25.0) == 0.0005


def test_slippage_pct_flat_for_zero_value_cr():
    """Zero/unknown liquidity is not "infinitely thin" -- it is the same
    "no data" case liquidity_capped_budget() also leaves alone; widening
    slippage on a guess would be inventing a number, not measuring one."""
    from execution.paper_broker import _slippage_pct
    with cfg_ctx({"cost_slippage_bps": "5.0"}):
        assert _slippage_pct(0.0) == 0.0005


def test_simulate_fill_uses_wider_slippage_for_a_thin_name():
    """End-to-end through the real consumer, not just the helper in
    isolation — a BUY MARKET fill on a thin name must land at a worse
    price than the same fill on a liquid one."""
    from execution.paper_broker import simulate_fill
    with cfg_ctx({"cost_slippage_bps": "5.0",
                  "cost_slippage_thin_threshold_cr": "25.0",
                  "cost_slippage_thin_multiplier": "3.0"}):
        liquid = simulate_fill("LIQUID", "BUY", 10, "MARKET", None, 100.0,
                               product="MIS", value_cr=500.0)
        thin = simulate_fill("THIN", "BUY", 10, "MARKET", None, 100.0,
                             product="MIS", value_cr=10.0)
    assert thin.fill_price > liquid.fill_price, (
        f"thin fill {thin.fill_price} should be worse (higher, on a BUY) "
        f"than liquid fill {liquid.fill_price}")


def test_simulate_fill_without_value_cr_matches_pre_stage_behaviour():
    """The exact backward-compatibility guarantee every pre-existing call
    site (paper_entry.py, engine.py's own swing path) depends on."""
    from execution.paper_broker import simulate_fill
    with cfg_ctx({"cost_slippage_bps": "5.0"}):
        no_arg = simulate_fill("X", "BUY", 10, "MARKET", None, 100.0, product="MIS")
        explicit_none = simulate_fill("X", "BUY", 10, "MARKET", None, 100.0,
                                      product="MIS", value_cr=None)
    assert no_arg.fill_price == explicit_none.fill_price == 100.05


TESTS = [
    ("slippage_pct flat when value_cr is None", test_slippage_pct_flat_when_value_cr_is_none),
    ("slippage_pct unchanged for a liquid name", test_slippage_pct_unchanged_for_a_liquid_name),
    ("slippage_pct scaled up for a thin name", test_slippage_pct_scaled_up_for_a_thin_name),
    ("slippage_pct unchanged at exactly the threshold", test_slippage_pct_unchanged_at_exactly_the_threshold),
    ("slippage_pct flat for zero value_cr", test_slippage_pct_flat_for_zero_value_cr),
    ("simulate_fill uses wider slippage for a thin name", test_simulate_fill_uses_wider_slippage_for_a_thin_name),
    ("simulate_fill without value_cr matches pre-stage behaviour", test_simulate_fill_without_value_cr_matches_pre_stage_behaviour),
]
