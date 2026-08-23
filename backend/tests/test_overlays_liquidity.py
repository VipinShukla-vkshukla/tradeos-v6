"""
analysis/overlays.py::liquidity_capped_budget() — Stage D2h, 24-Aug-2026.

WHAT THIS COVERS
-----------------
Found while auditing Track D's widened intraday universe (docs/FINDINGS.md
F-61): `intraday_max_position_pct` sizes EVERY name at the same flat
fraction of capital, whether it's RELIANCE or a name Population B/C just
admitted with no trading history. `liquidity_ok()` already computes the
right quantity (a position's share of the name's own daily traded value)
but only as a binary refuse-after-the-fact gate. This reuses that SAME
math to cap the budget BEFORE quantity is computed, so a thin name is
sized down instead of sized flat and then refused outright.
"""

from __future__ import annotations

from tests import cfg_ctx


def test_liquidity_capped_budget_uncapped_for_a_liquid_name():
    from analysis.overlays import liquidity_capped_budget
    with cfg_ctx({"overlay_liquidity_enabled": "true",
                  "overlay_max_share_of_daily_value": "0.005"}):
        # 500cr daily value * 0.5% = 2.5cr cap -- comfortably above a 50000 budget
        out = liquidity_capped_budget(500.0, 50_000.0)
    assert out == 50_000.0


def test_liquidity_capped_budget_shrinks_for_a_thin_name():
    from analysis.overlays import liquidity_capped_budget
    with cfg_ctx({"overlay_liquidity_enabled": "true",
                  "overlay_max_share_of_daily_value": "0.005"}):
        # 0.5cr daily value (Rs 5,000,000) * 0.5% = Rs 25,000 -- below the
        # flat Rs 50,000 budget, so the cap must bind.
        out = liquidity_capped_budget(0.5, 50_000.0)
    assert out == 25_000.0


def test_liquidity_capped_budget_unchanged_when_value_cr_is_none():
    """The one case this function deliberately does NOT fix -- no data at
    all stays liquidity_ok()'s own hard-refuse territory, not a guessed
    smaller size."""
    from analysis.overlays import liquidity_capped_budget
    with cfg_ctx({"overlay_liquidity_enabled": "true"}):
        out = liquidity_capped_budget(None, 50_000.0)
    assert out == 50_000.0


def test_liquidity_capped_budget_unchanged_when_value_cr_is_zero():
    from analysis.overlays import liquidity_capped_budget
    with cfg_ctx({"overlay_liquidity_enabled": "true"}):
        out = liquidity_capped_budget(0.0, 50_000.0)
    assert out == 50_000.0


def test_liquidity_capped_budget_noop_when_gate_disabled():
    from analysis.overlays import liquidity_capped_budget
    with cfg_ctx({"overlay_liquidity_enabled": "false"}):
        out = liquidity_capped_budget(1.0, 50_000.0)
    assert out == 50_000.0, "disabled must be a true no-op, not a smaller cap"


def test_liquidity_capped_budget_and_liquidity_ok_agree_on_the_same_cap():
    """The two functions must be computing the SAME quantity -- a budget
    capped by one and then checked by the other must always pass, or
    sizing and gating have silently drifted apart (the exact class of bug
    this project's own hurdle/edge-units history warns about)."""
    from analysis.overlays import liquidity_capped_budget, liquidity_ok
    with cfg_ctx({"overlay_liquidity_enabled": "true",
                  "overlay_max_share_of_daily_value": "0.005",
                  "overlay_min_daily_value_cr": "5.0",
                  "overlay_band_atr_pct": "9.0"}):
        capped = liquidity_capped_budget(25.0, 50_000.0)
        ok, why = liquidity_ok({"value_cr": 25.0, "atr_pct": 2.0}, planned_value=capped)
    assert ok, f"a budget this function itself capped was still refused: {why}"


TESTS = [
    ("liquidity_capped_budget uncapped for a liquid name", test_liquidity_capped_budget_uncapped_for_a_liquid_name),
    ("liquidity_capped_budget shrinks for a thin name", test_liquidity_capped_budget_shrinks_for_a_thin_name),
    ("liquidity_capped_budget unchanged when value_cr is None", test_liquidity_capped_budget_unchanged_when_value_cr_is_none),
    ("liquidity_capped_budget unchanged when value_cr is zero", test_liquidity_capped_budget_unchanged_when_value_cr_is_zero),
    ("liquidity_capped_budget no-op when gate disabled", test_liquidity_capped_budget_noop_when_gate_disabled),
    ("liquidity_capped_budget and liquidity_ok agree on the same cap", test_liquidity_capped_budget_and_liquidity_ok_agree_on_the_same_cap),
]
