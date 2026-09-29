"""
IGN archive scanner — tools/replay/ign_archive_events.py.

The scanner finds every ignition trigger across a symbol's whole multi-year history in one vectorised pass.
Its only job is to give exactly the answer the tested `first_trigger` gives day by day, so the tests are: a
day worked out by hand, the same answer as the slow reference on random multi-week histories, and each of the
rules that decide which days may trigger at all (liquidity, opening gap, session length).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from intraday.trend_indicators import DailyBar
from tools.replay import ign_archive_events as A
from tools.replay.ign_archive_checks import reference_triggers

N = 375


def _flat(n=N, px=100.0):
    return np.full(n, float(px))


def _frame(day, closes, vols):
    """One session of minute bars: open = previous close, high/low a hair either side."""
    ts = pd.date_range(f"{day} 09:15", periods=len(closes), freq="min", tz="Asia/Kolkata")
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": ts, "open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998,
                         "close": c, "volume": np.asarray(vols, float)})


def _daily(days, close=100.0, volume=4_000_000.0):
    return [DailyBar(d, close, close * 1.01, close * 0.99, close, volume) for d in days]


def _days(n, start="2024-01-01"):
    return list(pd.bdate_range(start, periods=n).date)


def _eligible(daily):
    return {daily[p].date for p in range(1, len(daily))
            if daily[p - 1].close >= 50 and daily[p - 1].close * daily[p - 1].volume / 1e7 >= 25}


def _scan(mdf, daily):
    got = A.scan_triggers(mdf, daily, A._atr_pct_prior(daily), _eligible(daily))
    return {(d, i, c) for d, by in got.items() for i, cs in by.items() for c in cs}


def _hand_day(vol_per_bar):
    """Prior close 100, prior-day volume 4,000,000 (turnover 40 Cr). Bars close at 100 except: bars 20-29 at 103.2 (+3.2%),
    bars 30-39 at 104.2 (+4.2%), bars 40+ at 105.5 (+5.5%)."""
    closes = _flat()
    closes[20:30], closes[30:40], closes[40:] = 103.2, 104.2, 105.5
    days = _days(40)
    return _frame(days[-1], closes, np.full(N, float(vol_per_bar))), _daily(days), days[-1]


def test_a_day_worked_out_by_hand():
    # Bar 20 is the first +3.0% close (3.2% is under 3.5%); bar 30 the first +3.5% and +4.0% close; bar 40 the first +5.0%.
    # Volume ratio at bar i is cumvol / (prev_volume * i / 375) = 10,700 * (i+1) / (10,666.7 * i) = 1.003 * (i+1)/i:
    # it passes a 1x bar and fails a 2x bar. Moves and volume are separate rules.
    mdf, daily, day = _hand_day(10_700)
    got = _scan(mdf, daily)
    at = lambda i: {c for d, j, c in got if d == day and j == i}
    assert at(20) == {"T3_V0", "T3_V1"}, at(20)
    assert at(30) == {"T3.5_V0", "T3.5_V1", "T4_V0", "T4_V1"}, at(30)
    assert at(40) == {"T5_V0", "T5_V1"}, at(40)
    assert not any(c.endswith(("_V2", "_V3")) for _, _, c in got), "a volume ratio of about 1.0x must not pass 2x or 3x"
    assert not any(c == "LIVE" for _, _, c in got), "the stop under this jump would be over 3% of price: infeasible for LIVE"


def test_a_high_volume_day_passes_the_volume_cells():
    mdf, daily, day = _hand_day(40_000)          # ratio about 3.75x
    got = _scan(mdf, daily)
    assert "T3_V3" in {c for d, i, c in got if d == day and i == 20}
    assert "T5_V3" in {c for d, i, c in got if d == day and i == 40}


def test_each_cell_reports_only_its_first_bar_of_the_day():
    mdf, daily, day = _hand_day(10_700)
    seen = [c for d, i, c in _scan(mdf, daily) if d == day]
    assert len(seen) == len(set(seen)), "each cell fires once a day, at its first qualifying bar"


def test_a_cheap_or_illiquid_stock_never_triggers():
    mdf, daily, day = _hand_day(10_700)
    px = 30.0 / 100.0                                                       # the same path scaled to a Rs 30 stock
    cheap_m = mdf.assign(open=mdf["open"] * px, high=mdf["high"] * px, low=mdf["low"] * px, close=mdf["close"] * px)
    cheap_d = [DailyBar(b.date, 30.0, 30.3, 29.7, 30.0, 4_000_000.0) for b in daily]           # price under 50
    assert _scan(cheap_m, cheap_d) == set()
    assert _scan(mdf, _daily([b.date for b in daily], 100.0, 1_000_000.0)) == set(), "10 Cr turnover is under the 25 Cr floor"


def test_a_bad_opening_gap_is_dropped_however_big_the_move():
    days = _days(40)
    closes = _flat(px=130.0)                              # opens 30% above the prior close: a data error or an unadjusted event
    assert _scan(_frame(days[-1], closes, np.full(N, 10_700.0)), _daily(days)) == set()


def test_a_short_session_is_ignored():
    days = _days(40)
    closes = _flat(200)
    closes[100:] = 105.0
    assert _scan(_frame(days[-1], closes, np.full(200, 10_700.0)), _daily(days)) == set(), "under 300 bars is not a trading session"


def _with_cell(t, v):
    """Run the scanner with a single custom cell, then put the real cells back."""
    class _Ctx:
        def __enter__(self):
            self.old = A.cells
            A.cells = lambda: [("EQ", t, v)]

        def __exit__(self, *exc):
            A.cells = self.old
    return _Ctx()


def test_every_threshold_is_inclusive_at_exactly_the_boundary():
    # Prior close 128 and a close of 136 is exactly +6.25% (both are powers of two apart, so no rounding): a 6.25% cell
    # must fire on that bar. The same holds at exactly the minimum bar index, and at a volume ratio of exactly 1.0x.
    days = _days(40)
    daily = _daily(days, close=128.0, volume=3_750_000.0)               # 10,000 shares per minute-of-day, turnover 48 Cr
    with _with_cell(6.25, 0.0):
        closes = _flat(px=128.0)
        closes[20:] = 136.0
        assert {(i, c) for _, i, c in _scan(_frame(days[-1], closes, np.full(N, 10_000.0)), daily)} == {(20, "EQ")}, "move exactly on the threshold"
        closes = _flat(px=128.0)
        closes[A.MIN_IDX:] = 136.0
        assert {(i, c) for _, i, c in _scan(_frame(days[-1], closes, np.full(N, 10_000.0)), daily)} == {(A.MIN_IDX, "EQ")}, "first allowed bar"
        closes = _flat(px=128.0)
        closes[A.MIN_IDX - 1:] = 136.0
        assert {i for _, i, _ in _scan(_frame(days[-1], closes, np.full(N, 10_000.0)), daily)} == {A.MIN_IDX}, "one bar too early is not a trigger; the next bar is"
    with _with_cell(6.25, 1.0):
        closes = _flat(px=128.0)
        closes[30:] = 136.0
        vols = np.full(N, 10_000.0)
        vols[30] = 0.0                                                    # 30 bars of 10,000 seen by bar 30: cumvol 300,000 = 1.0 x (10,000 * 30)
        assert {(i, c) for _, i, c in _scan(_frame(days[-1], closes, vols), daily)} == {(30, "EQ")}, "volume ratio exactly on the threshold"


def test_live_fires_on_a_slow_feasible_grind_and_not_before():
    # Gap open +2.2%, then +0.03 a bar: the first +3.5% close is bar 44 (103.52; bar 43 is 103.49). The 20-bar low is about
    # 0.7% below it, inside IGN's 1.75% risk cap, and volume is about 2.05x: LIVE fires at bar 44 and nowhere else.
    days = _days(40)
    closes = np.minimum(102.2 + 0.03 * np.arange(N), 103.6)          # it stops at +3.6%: no bar on this day ever reaches +4%
    got = _scan(_frame(days[-1], closes, np.full(N, 21_400.0)), _daily(days))
    live = sorted(i for _, i, c in got if c == "LIVE")
    assert live == [44], live
    assert "T3.5_V2" in {c for _, i, c in got if i == 44}


def test_atr_is_lagged_one_session_so_a_day_never_sees_its_own_range():
    rng = np.random.default_rng(3)
    days = _days(60)
    bars = []
    for d in days:
        o = 100 + rng.normal(0, 2)
        bars.append(DailyBar(d, o, o + abs(rng.normal(0, 2)) + 0.5, o - abs(rng.normal(0, 2)) - 0.5, o + rng.normal(0, 1), 1e6))
    from intraday.trend_indicators import wilder_atr
    atr = A._atr_pct_prior(bars)
    for p in (20, 35, 59):
        want = wilder_atr(bars[:p], 14)[-1] / bars[p - 1].close * 100.0
        assert abs(atr[p] - want) < 1e-9, (p, atr[p], want)
    bars2 = list(bars)
    b = bars[35]
    bars2[35] = DailyBar(b.date, b.open, b.high * 5, b.low / 5, b.close, b.volume)      # a wild range on day 35 itself
    assert abs(A._atr_pct_prior(bars2)[35] - atr[35]) < 1e-12, "day 35's own range must not reach day 35's ATR"


def test_row_features_cache_gives_the_identical_answer_as_no_cache_at_all():
    """dfeat_cache is pure memoisation of daily_features(pos), which does not depend on the intraday trigger
    bar: two events on the same day must get the identical daily-context fields whether or not a cache is
    used, and a cache HIT must not leak into a DIFFERENT day's fields."""
    days = _days(40)
    daily = _daily(days)
    closes = _flat()
    closes[20], closes[100] = 103.0, 96.0             # two distinct trigger bars on the same day
    g = _frame(days[-1], closes, np.full(N, 5000.0))
    d = A.F.DayBars(g["open"].to_numpy(float), g["high"].to_numpy(float), g["low"].to_numpy(float),
                    g["close"].to_numpy(float), g["volume"].to_numpy(float))
    pos = len(daily) - 1
    profile = np.full(A.F.SESSION_MIN, 1.0 / A.F.SESSION_MIN)
    no_cache_20, _ = A.row_features("X", days[-1], d, 20, daily, pos, {}, {}, profile)
    no_cache_100, _ = A.row_features("X", days[-1], d, 100, daily, pos, {}, {}, profile)
    cache: dict = {}
    cached_20, _ = A.row_features("X", days[-1], d, 20, daily, pos, {}, {}, profile, cache)
    cached_100, _ = A.row_features("X", days[-1], d, 100, daily, pos, {}, {}, profile, cache)
    assert len(cache) == 1, "one day, one cached computation, however many bars trigger on it"
    daily_fields = ("atr14_pct", "dist_sma20", "adx", "di_plus", "rsi14", "avg20_volume", "up_streak")
    for f in daily_fields:
        assert cached_20[f] == no_cache_20[f] == cached_100[f] == no_cache_100[f], f
    assert cached_20["move_pct"] != cached_100["move_pct"], "the intraday-dependent fields must still differ"

    # a genuinely different day's shape (not just a different price level, which normalises out of a % ATR):
    # a sharp trend instead of a flat tape. A DIFFERENT position in the SAME cache dict must not collide.
    trending = _daily(days, close=100.0)
    trending = [A.DailyBar(b.date, b.open + k * 0.7, b.high + k * 0.7, b.low + k * 0.7, b.close + k * 0.7, b.volume)
               for k, b in enumerate(trending)]
    other_row, _ = A.row_features("X", days[-1], d, 20, trending, pos - 5, {}, {}, profile, cache)
    assert (pos - 5) in cache and pos in cache and cache[pos - 5] is not cache[pos]
    assert other_row["atr14_pct"] != cached_20["atr14_pct"], "a different position must get its own computation"


def test_scanner_equals_the_tested_reference_on_random_histories():
    """Seven random 70-day histories with spike days; every (day, bar, cell) must agree with the slow reference, LIVE included."""
    total = 0
    for seed in range(7):
        rng = np.random.default_rng(seed)
        days = _days(70)
        rows, daily, prev = [], [], 200.0
        for k, day in enumerate(days):
            step = rng.normal(0, 0.0007, N)
            if k >= 30 and rng.random() < 0.5:
                a = int(rng.integers(8, 250))
                step[a:a + int(rng.integers(5, 25))] += rng.uniform(0.001, 0.006)
            c = prev * np.exp(np.cumsum(step) + rng.normal(0, 0.004))
            v = rng.integers(3000, 9000, N).astype(float)
            f = _frame(day, c, v)
            f.loc[0, "open"] = prev
            rows.append(f)
            daily.append(DailyBar(day, float(f["open"].iloc[0]), float(f["high"].max()), float(f["low"].min()), float(c[-1]), float(v.sum())))
            prev = float(c[-1])
        mdf = pd.concat(rows, ignore_index=True)
        atr, elig = A._atr_pct_prior(daily), _eligible(daily)
        got = A.scan_triggers(mdf, daily, atr, elig)
        fg = {(d, i, c) for d, by in got.items() for i, cs in by.items() for c in cs}
        fr = reference_triggers(mdf, daily, atr, elig)
        assert fg == fr, (seed, sorted(fr - fg)[:3], sorted(fg - fr)[:3])
        total += len(fr)
    assert total > 200, f"the random histories must contain triggers to compare (got {total})"


TESTS = [
    ("a day worked out by hand: first bar per cell, moves and volume are separate rules", test_a_day_worked_out_by_hand),
    ("a high-volume day passes the volume cells", test_a_high_volume_day_passes_the_volume_cells),
    ("each cell reports only its first bar of the day", test_each_cell_reports_only_its_first_bar_of_the_day),
    ("a cheap or illiquid stock never triggers", test_a_cheap_or_illiquid_stock_never_triggers),
    ("a bad opening gap is dropped however big the move", test_a_bad_opening_gap_is_dropped_however_big_the_move),
    ("a short session is ignored", test_a_short_session_is_ignored),
    ("every threshold is inclusive at exactly the boundary", test_every_threshold_is_inclusive_at_exactly_the_boundary),
    ("LIVE fires on a slow feasible grind and not before", test_live_fires_on_a_slow_feasible_grind_and_not_before),
    ("ATR is lagged one session so a day never sees its own range", test_atr_is_lagged_one_session_so_a_day_never_sees_its_own_range),
    ("row_features cache gives the identical answer as no cache at all", test_row_features_cache_gives_the_identical_answer_as_no_cache_at_all),
    ("scanner equals the tested reference on random histories", test_scanner_equals_the_tested_reference_on_random_histories),
]
