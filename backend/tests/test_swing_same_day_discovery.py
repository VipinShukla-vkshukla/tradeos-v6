"""
Phase 4 of the swing framework evolution blueprint, 26-Aug-2026 — same-day
setup discovery. Scoped to VBD/SBS/RSB only; never a second decision
system (see swing/signals/same_day_discovery.py's own module docstring for
the full reasoning, including the honest delivery_pct-proxy limitation).
"""

from __future__ import annotations

from types import SimpleNamespace

from tests import cfg_ctx


def test_simple_regime_ctx_maps_labels_correctly():
    from swing.signals.same_day_discovery import _simple_regime_ctx
    assert _simple_regime_ctx("RISK OFF")["is_bear"] is True
    assert _simple_regime_ctx("RISK ON")["is_bull"] is True
    assert _simple_regime_ctx("RECOVERING")["is_recovering"] is True
    neutral = _simple_regime_ctx(None)
    assert neutral["is_bear"] is False and neutral["is_bull"] is False


def test_build_live_stock_overlays_live_price_and_volume_onto_yesterdays_row():
    from swing.signals.same_day_discovery import _build_live_stock
    daily_row = {"symbol": "X", "close": 100.0, "sector": "IT", "delivery_pct": 40.0,
                 "market_cap": 5000}
    ctx = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=2.5)
    s = _build_live_stock(ctx, daily_row)
    assert s["close"] == 105.0
    assert s["current_price"] == 105.0
    assert s["pct_change"] == 5.0
    assert s["vol_ratio"] == 2.5
    # yesterday's static fields pass through untouched
    assert s["sector"] == "IT" and s["delivery_pct"] == 40.0 and s["market_cap"] == 5000


def test_build_live_stock_without_a_context_returns_the_static_row_unchanged():
    from swing.signals.same_day_discovery import _build_live_stock
    daily_row = {"symbol": "X", "close": 100.0}
    assert _build_live_stock(None, daily_row) == daily_row


def _vbd_qualifying_stock(**kw) -> dict:
    """Every VBD gate cleared deliberately generously — real screen_stocks.py
    run_vbd(), not a stub, so this proves _trigger() actually reaches and
    passes it."""
    s = {"symbol": "X", "sector": "test", "pct_change": 5.0, "vol_ratio": 3.0,
         "delivery_pct": 60.0, "consol_range": 5.0, "above_sma50": True,
         "adx": 25.0, "market_cap": 1000.0, "close": 105.0}
    s.update(kw)
    return s


def test_trigger_fires_vbd_on_a_real_qualifying_stock():
    """Calls the REAL run_vbd() from screen_stocks.py — no stub, no
    reimplementation — proving this module never invents a second copy of
    the trigger logic."""
    from swing.signals.same_day_discovery import _trigger
    s = _vbd_qualifying_stock()
    result = _trigger(s, sector_rank={"test": 1})
    assert result == "VBD", f"expected VBD to fire on a generously-qualifying stock, got {result}"


def test_trigger_returns_none_when_nothing_qualifies():
    from swing.signals.same_day_discovery import _trigger
    s = {"symbol": "X", "sector": "test", "pct_change": 0.5, "vol_ratio": 0.8,
         "delivery_pct": 10.0, "above_sma50": False, "market_cap": 50.0,
         "rs_vs_nifty": 0, "rsi_daily": 30, "close": 100.0}
    assert _trigger(s, sector_rank={"test": 1}) is None


def test_trigger_gate_fails_below_min_market_cap():
    """Confirms the REAL gate (MIN_MARKET_CAP=300cr) actually binds — a
    stock that clears every OTHER VBD condition but is too small must
    still be rejected, proving _trigger doesn't silently bypass any of
    run_vbd's own checks."""
    from swing.signals.same_day_discovery import _trigger
    s = _vbd_qualifying_stock(market_cap=100.0)
    assert _trigger(s, sector_rank={"test": 1}) is None


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name
        self._filters = []

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def gte(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self._filters.append((f"in:{col}", set(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def upsert(self, rows, on_conflict=None):
        self._upserted = rows
        for r in rows:
            self._store.setdefault(self._name, []).append(r)
        return self

    def execute(self):
        data = list(self._store.get(self._name, []))
        for col, val in self._filters:
            if col.startswith("in:"):
                real_col = col[3:]
                data = [r for r in data if r.get(real_col) in val]
            else:
                data = [r for r in data if r.get(col) == val]
        return SimpleNamespace(data=data)


class FakeSB:
    def __init__(self, tables: dict):
        self._tables = {k: list(v) for k, v in tables.items()}

    def table(self, name):
        return _FakeTable(self._tables, name)


def test_scan_skips_symbols_already_in_todays_evening_list():
    """The evening pipeline already covering a symbol today must not be
    re-discovered — same-day discovery only fills the gap, never
    duplicates the evening pipeline's own coverage."""
    from swing.signals import same_day_discovery as sdd
    sb = FakeSB({
        "signal_output_daily": [{"symbol": "X"}],
        "swing_same_day_candidates": [],
        "market_regime": [], "sector_strength": [],
        "stock_data_daily": [{"symbol": "X", "date": "2026-08-25", **_vbd_qualifying_stock()}],
    })
    ctx = SimpleNamespace(ltp=110.0, prev_close=100.0, volume_ratio=3.0)
    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}):
        out = sdd.scan(["X"], {"X": ctx}, sb, "2026-08-26")
    assert out == []


def test_scan_writes_a_genuine_new_trigger():
    from swing.signals import same_day_discovery as sdd
    daily = _vbd_qualifying_stock()
    daily["date"] = "2026-08-25"
    daily["symbol"] = "X"
    daily["sma_50"] = 90.0
    daily["atr_14"] = 3.0
    sb = FakeSB({
        "signal_output_daily": [],
        "swing_same_day_candidates": [],
        "market_regime": [{"regime": "NEUTRAL", "date": "2026-08-26"}],
        "sector_strength": [{"sector": "test", "rank": 1, "date": "2026-08-26"}],
        "stock_data_daily": [daily],
    })
    ctx = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)
    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}):
        out = sdd.scan(["X"], {"X": ctx}, sb, "2026-08-26")
    assert len(out) == 1
    assert out[0]["symbol"] == "X" and out[0]["strategy"] == "VBD"
    assert sb._tables["swing_same_day_candidates"], "must actually write the row"


def test_scan_is_a_noop_when_shadow_switch_is_off():
    from swing.signals import same_day_discovery as sdd
    sb = FakeSB({"signal_output_daily": [], "swing_same_day_candidates": []})
    with cfg_ctx({"swing_same_day_discovery_shadow": "false"}):
        out = sdd.scan(["X"], {}, sb, "2026-08-26")
    assert out == []


TESTS = [
    ("simple regime ctx maps labels correctly", test_simple_regime_ctx_maps_labels_correctly),
    ("build_live_stock overlays live price/volume onto yesterday's row",
     test_build_live_stock_overlays_live_price_and_volume_onto_yesterdays_row),
    ("build_live_stock without a context returns the static row unchanged",
     test_build_live_stock_without_a_context_returns_the_static_row_unchanged),
    ("trigger fires VBD on a real qualifying stock", test_trigger_fires_vbd_on_a_real_qualifying_stock),
    ("trigger returns None when nothing qualifies", test_trigger_returns_none_when_nothing_qualifies),
    ("trigger gate fails below MIN_MARKET_CAP", test_trigger_gate_fails_below_min_market_cap),
    ("scan skips symbols already in today's evening list",
     test_scan_skips_symbols_already_in_todays_evening_list),
    ("scan writes a genuine new trigger", test_scan_writes_a_genuine_new_trigger),
    ("scan is a no-op when the shadow switch is off", test_scan_is_a_noop_when_shadow_switch_is_off),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
        except Exception as e:
            fails += 1
            import traceback; traceback.print_exc()
            print(f"  ERROR {name} — {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
