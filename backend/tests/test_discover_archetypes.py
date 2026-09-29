"""
Discovery archetype scanner — tools/replay/discover_archetypes.py.

Six trigger families, each checked against a hand-worked case or an independent slow reference computed
inside the test (never copied from the implementation): opening-range breakout, VWAP-stretch reversion, gap
continuation/fade, prior-day-level breakout, and Connors' RSI(2) extreme. Also the RSI(2) vectorisation
against the tested scalar `intraday.trend_indicators.rsi`, and that it never sees its own day's close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from intraday.trend_indicators import DailyBar, rsi as rsi_ref
from tools.replay import discover_archetypes as D

N = 375


def _flat(n=N, px=100.0):
    return np.full(n, float(px))


def _frame(day, closes, vols):
    ts = pd.date_range(f"{day} 09:15", periods=len(closes), freq="min", tz="Asia/Kolkata")
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"ts": ts, "open": o, "high": np.maximum(o, c) * 1.0002, "low": np.minimum(o, c) * 0.9998,
                         "close": c, "volume": np.asarray(vols, float)})


def _daily(days, close=100.0, volume=4_000_000.0, high=None, low=None):
    return [DailyBar(d, close, high if high is not None else close * 1.01,
                     low if low is not None else close * 0.99, close, volume) for d in days]


def _days(n, start="2024-01-01"):
    return list(pd.bdate_range(start, periods=n).date)


def _eligible(daily):
    return {daily[p].date for p in range(1, len(daily))
            if daily[p - 1].close >= 50 and daily[p - 1].close * daily[p - 1].volume / 1e7 >= 25}


def _scan(mdf, daily, rsi2=None):
    rsi2 = rsi2 if rsi2 is not None else np.full(len(daily), np.nan)
    got = D.scan_archetypes(mdf, daily, _eligible(daily), rsi2)
    return {(d, i, c) for d, by in got.items() for i, cs in by.items() for c in cs}


def test_opening_range_breakout_fires_at_the_first_bar_past_the_window_and_not_before():
    days = _days(40)
    daily = _daily(days, high=200.0, low=50.0)          # wide enough that this break can never also read as a PDHL break
    closes = _flat()
    closes[15] = 103.0          # first bar past the 15-min window: breaks both the 5- and 15-minute range
    trig = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), daily)
    orb_at15 = {c for d, i, c in trig if d == days[-1] and i == 15 and c.startswith("ORB")}
    assert orb_at15 == {"ORB5_LONG", "ORB15_LONG"}, orb_at15
    assert not any(i < 15 for d, i, c in trig if d == days[-1] and c.startswith("ORB")), "no break before the window closes"


def _degenerate_frame(day, closes, vols):
    """Bars with open == high == low == close: an exact price, no wick padding, so a boundary test can name
    the opening-range high precisely instead of guessing through the usual high/low padding."""
    ts = pd.date_range(f"{day} 09:15", periods=len(closes), freq="min", tz="Asia/Kolkata")
    c = np.asarray(closes, float)
    return pd.DataFrame({"ts": ts, "open": c, "high": c, "low": c, "close": c, "volume": np.asarray(vols, float)})


def test_opening_range_breakout_needs_to_clear_the_range_not_just_touch_it():
    days = _days(40)
    daily = _daily(days, high=200.0, low=50.0)
    tie = _flat()
    tie[15] = 100.0                                    # bars 0-14 are all exactly 100: the OR15 high IS 100.0
    trig = _scan(_degenerate_frame(days[-1], tie, np.full(N, 15000.0)), daily)
    assert not any(i == 15 and c.startswith("ORB") for _, i, c in trig), "an exact tie with the range high is not a break"

    cleared = _flat()
    cleared[15] = 100.25                                # clears the 100.0 range high by more than the break buffer
    trig2 = _scan(_degenerate_frame(days[-1], cleared, np.full(N, 15000.0)), daily)
    assert any(i == 15 and c == "ORB5_LONG" for _, i, c in trig2)

    at_the_buffer = _flat()
    at_the_buffer[15] = 100.0 * (1 + D.BREAK_BUFFER)    # exactly the comparison threshold itself: still not a clear
    trig3 = _scan(_degenerate_frame(days[-1], at_the_buffer, np.full(N, 15000.0)), daily)
    assert not any(i == 15 and c.startswith("ORB") for _, i, c in trig3), "a tie with the buffered threshold is not a break either"


def test_opening_range_breakout_short_uses_the_low_not_the_high():
    days = _days(40)
    closes = _flat()
    closes[5] = 97.0             # breaks the 5-minute low at the first eligible bar; the 15-minute window is not over yet
    trig = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), _daily(days))
    assert {c for d, i, c in trig if d == days[-1] and i == 5} == {"ORB5_SHORT"}


def test_opening_range_breakout_short_stays_silent_inside_a_wide_range():
    """The first 5 bars swing between 90 and 110 (a wide range); a bar 5 close of 100 sits INSIDE that range —
    below the high, above the low — and must not read as a break of the low."""
    days = _days(40)
    closes = _flat()
    closes[0], closes[1] = 90.0, 110.0                  # sets OR5's low and high
    closes[5] = 100.0
    trig = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), _daily(days))
    assert not any(i == 5 and c.startswith("ORB5") for _, i, c in trig), trig


def test_opening_range_breakout_needs_real_volume_not_just_a_price_move():
    days = _days(40)
    closes = _flat()
    closes[15] = 103.0
    quiet = _scan(_frame(days[-1], closes, np.full(N, 50.0)), _daily(days))          # far below IGN's own volume floor
    assert not any(c.startswith("ORB") for _, _, c in quiet), quiet
    loud = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), _daily(days))
    assert any(i == 15 and c == "ORB15_LONG" for _, i, c in loud), "the same break, with real volume, must fire"


def _vwap_ref(o, h, l, c, v):
    """VWAP deviation at every bar, via a plain running loop — independent of the vectorised cumsum in the source."""
    typ = (h + l + c) / 3.0
    cum_tpv = cum_v = 0.0
    out = []
    for i in range(len(c)):
        cum_tpv += typ[i] * v[i]
        cum_v += v[i]
        vwap = cum_tpv / cum_v if cum_v > 0 else typ[i]
        out.append((c[i] / vwap - 1.0) * 100.0)
    return np.array(out)


def test_vwap_reversion_matches_an_independently_computed_running_vwap():
    days = _days(40)
    closes = 100.0 - 0.25 * np.arange(N)              # a steady one-way drift away from the session's own average
    f = _frame(days[-1], closes, np.full(N, 1000.0))
    ref = _vwap_ref(f["open"].to_numpy(), f["high"].to_numpy(), f["low"].to_numpy(), f["close"].to_numpy(), f["volume"].to_numpy())
    want = next(i for i in range(D.MIN_IDX, N) if ref[i] <= -D.VWAP_STRETCH_PCT)
    trig = _scan(f, _daily(days))
    got = sorted(i for d, i, c in trig if d == days[-1] and c == "VWAP_REV_LONG")
    assert got == [want], (got, want)
    assert not any(c == "VWAP_REV_SHORT" for _, _, c in trig), "a one-way decline must never fire the SHORT side"


def test_gap_continue_and_fade_are_mutually_exclusive_on_the_same_gap():
    days = _days(40)
    daily = _daily(days, close=100.0)                  # prior close 100
    up_hold = _flat(px=102.5)                            # opens +2.5%, stays at the open all day: CONTINUE, not FADE
    trig = _scan(_frame(days[-1], up_hold, np.full(N, 5000.0)), daily)
    at3 = {c for d, i, c in trig if d == days[-1] and i == 3}
    assert at3 == {"GAP_UP_CONTINUE"}, at3

    up_fade = _flat(px=102.5)
    up_fade[3:] = 101.0                                   # gives back the gap by bar 3: FADE, not CONTINUE
    trig = _scan(_frame(days[-1], up_fade, np.full(N, 5000.0)), daily)
    at3 = {c for d, i, c in trig if d == days[-1] and i == 3}
    assert at3 == {"GAP_UP_FADE"}, at3


def test_a_small_gap_never_triggers_either_gap_archetype():
    days = _days(40)
    small = _flat(px=100.8)                               # 0.8%: under the 1.5% floor
    trig = _scan(_frame(days[-1], small, np.full(N, 5000.0)), _daily(days))
    assert not any(c.startswith("GAP_") for _, _, c in trig)


def test_prior_day_break_respects_the_minimum_bar_floor():
    days = _days(40)
    daily = _daily(days, close=100.0, high=105.0, low=95.0)     # yesterday's range 95-105
    closes = _flat()
    closes[D.MIN_IDX - 1] = 106.0                                # a real break, but one bar too early
    closes[D.MIN_IDX] = 106.0                                    # the same break, now at the first allowed bar
    trig = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), daily)
    got = sorted(i for d, i, c in trig if d == days[-1] and c == "PDHL_BREAK_LONG")
    assert got == [D.MIN_IDX], got


def test_a_break_that_does_not_clear_the_buffer_does_not_trigger():
    days = _days(40)
    daily = _daily(days, close=100.0, high=105.0, low=95.0)
    closes = _flat()
    closes[20] = 105.02                                          # inside the 0.1% buffer above 105 (may still read as
    trig = _scan(_frame(days[-1], closes, np.full(N, 15000.0)), daily)          # an ORB/VWAP move — that is correct;
    assert not any(c.startswith("PDHL_BREAK") for _, _, c in trig), trig        # only PDHL must stay silent)


def test_rsi2_vectorised_matches_the_tested_scalar_reference_and_is_lagged_one_session():
    rng = np.random.default_rng(5)
    days = _days(60)
    closes = 100 + np.cumsum(rng.normal(0, 1.2, len(days)))
    daily = [DailyBar(d, c, c + 1, c - 1, c, 4_000_000.0) for d, c in zip(days, closes)]
    got = D._rsi2_prior(daily, n=2)
    for p in (10, 30, 59):
        want = rsi_ref(list(closes[:p]), n=2)
        assert want is not None and abs(got[p] - want) < 1e-9, (p, got[p], want)
    bumped = list(daily)
    bumped[40] = DailyBar(days[40], closes[40], closes[40] + 50, closes[40] - 50, closes[40] + 40, 4_000_000.0)
    assert abs(D._rsi2_prior(bumped, n=2)[40] - got[40]) < 1e-12, "day 40's own close must not reach day 40's own RSI"


def test_rsi2_extreme_fires_only_at_the_open_of_the_day_after():
    days = _days(40)
    daily = _daily(days)
    rsi2 = np.full(len(daily), np.nan)
    rsi2[len(daily) - 1] = 8.0                                    # yesterday's RSI2 was oversold
    trig = _scan(_frame(days[-1], _flat(), np.full(N, 5000.0)), daily, rsi2=rsi2)
    assert trig == {(days[-1], 0, "RSI2_LONG")}, trig


def test_rsi2_extreme_is_a_real_threshold_not_just_a_direction():
    """A NEUTRAL RSI2 (50) must fire NEITHER side — otherwise the long/short thresholds could be swapped, or
    widened to the point of firing on everything, and this test would not notice."""
    days = _days(40)
    daily = _daily(days)
    rsi2 = np.full(len(daily), np.nan)
    rsi2[len(daily) - 1] = 50.0
    trig = _scan(_frame(days[-1], _flat(), np.full(N, 5000.0)), daily, rsi2=rsi2)
    assert not any(c.startswith("RSI2") for _, _, c in trig), trig


def test_a_cheap_or_illiquid_stock_never_triggers_any_archetype():
    days = _days(40)
    closes = _flat()
    closes[15], closes[20] = 103.0, 102.0
    mdf = _frame(days[-1], closes, np.full(N, 5000.0))
    cheap = [DailyBar(b.date, 30.0, 30.3, 29.7, 30.0, 4_000_000.0) for b in _daily(days)]
    scaled = mdf.assign(open=mdf["open"] * 0.3, high=mdf["high"] * 0.3, low=mdf["low"] * 0.3, close=mdf["close"] * 0.3)
    assert _scan(scaled, cheap) == set()
    assert _scan(mdf, _daily(days, volume=1_000_000.0)) == set(), "10 Cr turnover is under the 25 Cr floor"


def test_a_short_session_never_triggers_any_archetype():
    days = _days(40)
    closes = _flat(200)
    closes[15], closes[100] = 103.0, 110.0
    trig = _scan(_frame(days[-1], closes, np.full(200, 5000.0)), _daily(days))
    assert trig == set(), "under 300 bars is not a trading session"


TESTS = [
    ("opening-range breakout fires at the first bar past the window and not before", test_opening_range_breakout_fires_at_the_first_bar_past_the_window_and_not_before),
    ("opening-range breakout needs to clear the range, not just touch it", test_opening_range_breakout_needs_to_clear_the_range_not_just_touch_it),
    ("opening-range short uses the low, not the high", test_opening_range_breakout_short_uses_the_low_not_the_high),
    ("opening-range short stays silent inside a wide range", test_opening_range_breakout_short_stays_silent_inside_a_wide_range),
    ("opening-range breakout needs real volume, not just a price move", test_opening_range_breakout_needs_real_volume_not_just_a_price_move),
    ("VWAP reversion matches an independently computed running VWAP", test_vwap_reversion_matches_an_independently_computed_running_vwap),
    ("gap continue and fade are mutually exclusive on the same gap", test_gap_continue_and_fade_are_mutually_exclusive_on_the_same_gap),
    ("a small gap never triggers either gap archetype", test_a_small_gap_never_triggers_either_gap_archetype),
    ("prior-day break respects the minimum bar floor", test_prior_day_break_respects_the_minimum_bar_floor),
    ("a break that does not clear the buffer does not trigger", test_a_break_that_does_not_clear_the_buffer_does_not_trigger),
    ("RSI2 vectorised matches the tested scalar reference and is lagged one session", test_rsi2_vectorised_matches_the_tested_scalar_reference_and_is_lagged_one_session),
    ("RSI2 extreme fires only at the open of the day after", test_rsi2_extreme_fires_only_at_the_open_of_the_day_after),
    ("RSI2 extreme is a real threshold, not just a direction", test_rsi2_extreme_is_a_real_threshold_not_just_a_direction),
    ("a cheap or illiquid stock never triggers any archetype", test_a_cheap_or_illiquid_stock_never_triggers_any_archetype),
    ("a short session never triggers any archetype", test_a_short_session_never_triggers_any_archetype),
]
