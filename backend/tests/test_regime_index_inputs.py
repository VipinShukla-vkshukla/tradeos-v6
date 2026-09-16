"""
compute_regime's Nifty/BankNifty inputs must come from real index history.

fetch_nifty_from_supabase() and fetch_banknifty_from_supabase() copied
nifty_50dma, nifty_200dma, nifty_weekly_rsi, banknifty_price and
banknifty_weekly_rsi from the previous market_regime row. Copying a value from
itself never changes it: all five read the same number on ~94 rows from April to
16-Sep-2026 (50DMA 24173.825, 200DMA 25108.48, weekly RSI 42.98, BankNifty 55403.6
/ RSI 41.7), so the 25-point price-structure pillar and the weekly-RSI part of
momentum scored a frozen April market.
"""

from __future__ import annotations

from datetime import date, timedelta

STALE = {"nifty_50dma": 24173.825, "nifty_200dma": 25108.4846, "nifty_weekly_rsi": 42.98,
         "banknifty_price": 55403.6, "banknifty_weekly_rsi": 41.7}


def _series(start_price: float, n: int = 300, step: float = 5.0, end: str = "2026-09-15"):
    """n weekday closes ending on `end`, oldest first, falling by `step` a day."""
    out, d, px = [], date.fromisoformat(end), start_price
    while len(out) < n:
        if d.weekday() < 5:
            out.append((d.isoformat(), px))
            px += step
        d -= timedelta(days=1)
    return list(reversed(out))


class _T:
    def __init__(self, rows):
        self.rows = rows
    @property
    def not_(self):
        return self
    def __getattr__(self, name):
        return lambda *a, **k: self
    def execute(self):
        return type("R", (), {"data": self.rows})()


class _SB:
    def table(self, name):
        if name == "global_cues":
            return _T([{"gift_nifty": 23217.6, "date": "2026-09-16"}])
        prev = {"date": "2026-09-15", "nifty_price": 23118.6, **STALE}
        return _T([prev] + [{"date": f"2026-08-{d:02d}", "nifty_price": 24000.0 - d}
                            for d in range(28, 7, -1)])


def _patch(mod, closes):
    saved = (getattr(mod, "fetch_index_closes", None), mod.fetch_index_data)
    mod.fetch_index_closes = lambda ticker: closes
    mod.fetch_index_data = lambda ticker: None
    return saved


def _restore(mod, saved):
    if saved[0] is None:
        delattr(mod, "fetch_index_closes")
    else:
        mod.fetch_index_closes = saved[0]
    mod.fetch_index_data = saved[1]


def test_nifty_dmas_come_from_history_not_the_previous_row():
    import swing.compute.compute_regime as cr
    closes = _series(25000.0, step=-3.0)
    saved = _patch(cr, closes)
    try:
        res, _src = cr.fetch_nifty_from_supabase(_SB(), "2026-09-16")
    finally:
        _restore(cr, saved)
    series = [c for d, c in closes if d < "2026-09-16"] + [23217.6]
    want50 = sum(series[-50:]) / 50
    assert res and abs(res["dma50"] - want50) < 1e-6, (res and res["dma50"], want50)
    assert abs(res["dma200"] - sum(series[-200:]) / 200) < 1e-6
    assert res["weekly_rsi"] != STALE["nifty_weekly_rsi"]


def test_no_history_leaves_the_fields_empty_rather_than_stale():
    import swing.compute.compute_regime as cr
    saved = _patch(cr, [])
    try:
        res, src = cr.fetch_nifty_from_supabase(_SB(), "2026-09-16")
    finally:
        _restore(cr, saved)
    assert res["dma50"] is None and res["dma200"] is None and res["weekly_rsi"] is None, (res, src)


def test_banknifty_is_not_carried_forward():
    import swing.compute.compute_regime as cr
    closes = _series(56000.0, step=-10.0)
    saved = _patch(cr, closes)
    try:
        res, _src = cr.fetch_banknifty_from_supabase(_SB(), "2026-09-16")
    finally:
        _restore(cr, saved)
    assert res and res["price"] == closes[-1][1], res
    assert res["weekly_rsi"] is not None and res["weekly_rsi"] != STALE["banknifty_weekly_rsi"], res


def test_weekly_rsi_matches_the_pandas_implementation():
    """index_indicators() must give the number fetch_index_data()'s pandas code gives."""
    import pandas as pd
    import swing.compute.compute_regime as cr
    closes = [(d, p + (40 if i % 3 == 0 else -25) * (1 if i % 7 else -2))
              for i, (d, p) in enumerate(_series(24000.0, step=1.5))]
    ind = cr.index_indicators(closes, None, "2026-09-16")
    s = pd.Series([c for _, c in closes], index=pd.to_datetime([d for d, _ in closes]))
    weekly = s.resample("W-FRI").last().dropna()
    delta = weekly.diff().dropna()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    want = round(100 - (100 / (1 + gain / loss)), 2)
    assert ind["weekly_rsi"] == want, (ind["weekly_rsi"], want)
    assert abs(ind["ret_20d"] - round((closes[-1][1] / closes[-21][1] - 1) * 100, 2)) < 1e-9


TESTS = [
    ("Nifty 50/200 DMA and weekly RSI come from history, not the previous row",
     test_nifty_dmas_come_from_history_not_the_previous_row),
    ("no index history leaves the fields empty rather than stale",
     test_no_history_leaves_the_fields_empty_rather_than_stale),
    ("BankNifty price and RSI are not carried forward", test_banknifty_is_not_carried_forward),
    ("weekly RSI matches the pandas implementation", test_weekly_rsi_matches_the_pandas_implementation),
]


def test_health_flags_frozen_inputs_and_passes_moving_ones():
    from tools.health import frozen_inputs
    frozen = [{"date": f"2026-09-{d}", **{k: v for k, v in STALE.items() if k != "banknifty_price"}}
              for d in (10, 15, 16)]
    assert len(frozen_inputs(frozen)) == 4, frozen_inputs(frozen)
    moving = [dict(r, nifty_50dma=24100.0 + i, nifty_200dma=24500.0 - i,
                   nifty_weekly_rsi=44.0 + i, banknifty_weekly_rsi=43.0 - i)
              for i, r in enumerate(frozen)]
    assert frozen_inputs(moving) == []


TESTS += [("health flags frozen regime inputs, passes moving ones",
           test_health_flags_frozen_inputs_and_passes_moving_ones)]
