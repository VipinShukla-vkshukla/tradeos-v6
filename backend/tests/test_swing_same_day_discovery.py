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
    """
    Real `.lte()` and a real, sorting `.order()`/`.limit()` — 09-Sep-2026,
    added alongside the fix these were built to prove. Before this, `.
    order()` was a no-op passthrough and `.limit()` did not exist at all;
    with only ever one row per symbol/date in the old fixtures that never
    mattered, which is exactly how the real bug (querying market_regime/
    sector_strength for TODAY, which cannot exist until that evening) went
    unnoticed by this file's own suite for two weeks of live sessions —
    the fake DB could not have caught it even if a test had tried, because
    "most recent available on or before" and "exact match on today" read
    identically when a fixture only ever has one date on file.
    """
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name
        self._filters = []
        self._order_col = None
        self._order_desc = False
        self._limit_n = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def gte(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, set(vals)))
        return self

    def order(self, col, desc=False):
        self._order_col, self._order_desc = col, desc
        return self

    def limit(self, n):
        self._limit_n = n
        return self

    def upsert(self, rows, on_conflict=None):
        self._upserted = rows
        for r in rows:
            self._store.setdefault(self._name, []).append(r)
        return self

    def execute(self):
        data = list(self._store.get(self._name, []))
        for kind, col, val in self._filters:
            if kind == "eq":
                data = [r for r in data if r.get(col) == val]
            elif kind == "lte":
                data = [r for r in data if (r.get(col) or "") <= val]
            elif kind == "in":
                data = [r for r in data if r.get(col) in val]
        if self._order_col:
            data = sorted(data, key=lambda r: r.get(self._order_col) or "",
                          reverse=self._order_desc)
        if self._limit_n is not None:
            data = data[:self._limit_n]
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
    """
    market_regime/sector_strength are dated YESTERDAY (2026-08-25), trade_
    date is TODAY (2026-08-26) — the real production shape, not the old
    fixture's same-day coincidence. This is the exact scenario that was
    broken live for two weeks: see
    test_sector_rank_uses_the_most_recent_available_date_not_an_exact_
    match_on_today below for the dedicated regression pin.
    """
    from swing.signals import same_day_discovery as sdd
    daily = _vbd_qualifying_stock()
    daily["date"] = "2026-08-25"
    daily["symbol"] = "X"
    daily["sma_50"] = 90.0
    daily["atr_14"] = 3.0
    sb = FakeSB({
        "signal_output_daily": [],
        "swing_same_day_candidates": [],
        "market_regime": [{"regime": "NEUTRAL", "date": "2026-08-25"}],
        "sector_strength": [{"sector": "test", "rank": 1, "date": "2026-08-25"}],
        "stock_data_daily": [daily],
    })
    ctx = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)
    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}):
        out = sdd.scan(["X"], {"X": ctx}, sb, "2026-08-26")
    assert len(out) == 1
    assert out[0]["symbol"] == "X" and out[0]["strategy"] == "VBD"
    assert sb._tables["swing_same_day_candidates"], "must actually write the row"


def test_sector_rank_uses_the_most_recent_available_date_not_an_exact_match_on_today():
    """
    THE BUG, reproduced directly, 09-Sep-2026. Confirmed live: sector_
    strength/market_regime for a session's OWN date are not computed until
    that evening (~22:00 IST) — the evening pipeline's own output. scan()
    used to query `.eq("date", trade_date)`, so on every live session
    (the only time this function ever runs) it read ZERO sector ranks,
    every sector defaulted to rank 99 (run_vbd/run_sbs/run_rsb's own
    `sector_rank.get(sector, 99)`), and every symbol failed their
    `s_rank > gate(10-14)` check universally — not a rare trigger, a
    structural one. Two weeks of live sessions, zero rows, ever
    (docs/FINDINGS.md, 09-Sep-2026). sector_strength/market_regime here
    exist ONLY for a date 5 days before trade_date (no row for trade_date
    itself, or the day before, or the day before that) — proving the fix
    walks back to whatever IS available, not just one day back.
    """
    from swing.signals import same_day_discovery as sdd
    daily = _vbd_qualifying_stock()
    daily["date"] = "2026-08-21"
    daily["symbol"] = "X"
    daily["sma_50"] = 90.0
    daily["atr_14"] = 3.0
    sb = FakeSB({
        "signal_output_daily": [],
        "swing_same_day_candidates": [],
        "market_regime": [{"regime": "NEUTRAL", "date": "2026-08-21"}],
        "sector_strength": [{"sector": "test", "rank": 1, "date": "2026-08-21"}],
        "stock_data_daily": [daily],
    })
    ctx = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)
    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}):
        out = sdd.scan(["X"], {"X": ctx}, sb, "2026-08-26")   # trade_date, no same-day rows at all
    assert len(out) == 1, (
        "sector_rank/regime must fall back to the most recent available "
        "date, not read as empty just because trade_date itself has none")
    assert out[0]["strategy"] == "VBD"


def test_sector_rank_never_reads_a_date_after_trade_date():
    """The other half of `.lte()` — a sector_strength row dated AFTER
    trade_date (a clock skew, or a symbol whose own date field is
    corrupted) must never be used; only <= trade_date is real history."""
    from swing.signals import same_day_discovery as sdd
    daily = _vbd_qualifying_stock()
    daily["date"] = "2026-08-25"
    daily["symbol"] = "X"
    sb = FakeSB({
        "signal_output_daily": [],
        "swing_same_day_candidates": [],
        "market_regime": [{"regime": "NEUTRAL", "date": "2026-08-27"}],   # AFTER trade_date
        "sector_strength": [{"sector": "test", "rank": 1, "date": "2026-08-27"}],  # AFTER trade_date
        "stock_data_daily": [daily],
    })
    ctx = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)
    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}):
        out = sdd.scan(["X"], {"X": ctx}, sb, "2026-08-26")
    assert out == [], (
        "a future-dated sector_strength/market_regime row must not be "
        "used — sector_rank must read back empty, failing VBD's own "
        "s_rank > gate check exactly as it should with no real history")


def test_a_trigger_exception_for_one_symbol_does_not_abort_the_whole_batch():
    """
    _trigger() was the one call in scan()'s loop with no try/except —
    09-Sep-2026, fixed alongside the sector_rank bug above. Every other
    per-symbol step (compute_entry_zones, compute_trade_plan) already
    caught its own exception and moved on; without this fix a single bad
    row would abort the ENTIRE cycle for every other watched symbol,
    silently, since run.py's own caller wraps the whole scan() call in
    one bare except that cannot distinguish "one bad symbol" from
    "nothing triggered today".
    """
    from swing.signals import same_day_discovery as sdd
    from unittest.mock import patch

    daily_a = _vbd_qualifying_stock()
    daily_a["date"] = "2026-08-25"; daily_a["symbol"] = "A"
    daily_a["sma_50"] = 90.0; daily_a["atr_14"] = 3.0
    daily_b = _vbd_qualifying_stock()
    daily_b["date"] = "2026-08-25"; daily_b["symbol"] = "B"
    daily_b["sma_50"] = 90.0; daily_b["atr_14"] = 3.0
    sb = FakeSB({
        "signal_output_daily": [],
        "swing_same_day_candidates": [],
        "market_regime": [{"regime": "NEUTRAL", "date": "2026-08-25"}],
        "sector_strength": [{"sector": "test", "rank": 1, "date": "2026-08-25"}],
        "stock_data_daily": [daily_a, daily_b],
    })
    ctx_a = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)
    ctx_b = SimpleNamespace(ltp=105.0, prev_close=100.0, volume_ratio=3.0)

    real_trigger = sdd._trigger

    def _boom_on_a(s, sector_rank):
        if s.get("symbol") == "A":
            raise RuntimeError("boom")
        return real_trigger(s, sector_rank)

    with cfg_ctx({"swing_same_day_discovery_shadow": "true"}), \
         patch.object(sdd, "_trigger", side_effect=_boom_on_a):
        out = sdd.scan(["A", "B"], {"A": ctx_a, "B": ctx_b}, sb, "2026-08-26")
    assert len(out) == 1 and out[0]["symbol"] == "B", (
        "A's exception must not stop B from being discovered")


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
    ("sector_rank uses the most recent available date, not an exact match on today",
     test_sector_rank_uses_the_most_recent_available_date_not_an_exact_match_on_today),
    ("sector_rank never reads a date after trade_date",
     test_sector_rank_never_reads_a_date_after_trade_date),
    ("a trigger exception for one symbol does not abort the whole batch",
     test_a_trigger_exception_for_one_symbol_does_not_abort_the_whole_batch),
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
