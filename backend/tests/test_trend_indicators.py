"""
Daily trend indicators (24-Sep-2026) — intraday/trend_indicators.py.

Every expected value below is worked out by hand in the comment beside it, not
produced by running the code under test: a check whose expectation is copied
from the implementation's own output cannot fail.

The series `h=10+i, l=8+i, c=9+i` is used throughout: a perfectly steady
uptrend where every true range is exactly 2 (h-l = 2, |h-prev_close| = 2,
|l-prev_close| = 0), +DM is exactly 1 every bar and -DM is exactly 0.
"""

from __future__ import annotations

from intraday.trend_indicators import (
    DailyBar, PANEL_KEYS, feature_panel, max_close_jump, rsi, sma,
    supertrend, wilder_atr, wilder_dmi,
)


def _up(n: int) -> list[DailyBar]:
    return [DailyBar(i, 9.0 + i, 10.0 + i, 8.0 + i, 9.0 + i, 1000.0) for i in range(n)]


def _down(n: int, start: float = 100.0, step: float = 1.0) -> list[DailyBar]:
    return [DailyBar(i, start - step * i, start + 2.0 - step * i,
                     start - step * i, start + 1.0 - step * i, 1000.0)
            for i in range(n)]


def test_sma_needs_a_full_window():
    assert sma([1, 2, 3, 4], 3) == 3.0, "mean of the LAST three: (2+3+4)/3"
    assert sma([1, 2], 3) is None, "two values cannot make a 3-day average"


def test_wilder_atr_hand_computed():
    # h/l/c: (10,8,9) (11,9,10) (12,10,11) (13,11,12) (15,12,14)
    # TR1..3 = 2, 2, 2 ; TR4 = max(3, |15-12|=3, |12-12|=0) = 3
    # ATR(3) at bar 3 = (2+2+2)/3 = 2 ; at bar 4 = (2*2 + 3)/3 = 7/3
    bars = [DailyBar(0, 9, 10, 8, 9, 1), DailyBar(1, 10, 11, 9, 10, 1),
            DailyBar(2, 11, 12, 10, 11, 1), DailyBar(3, 12, 13, 11, 12, 1),
            DailyBar(4, 13, 15, 12, 14, 1)]
    atr = wilder_atr(bars, 3)
    assert atr[:3] == [None, None, None], f"no ATR before n true ranges: {atr}"
    assert abs(atr[3] - 2.0) < 1e-12, f"seed ATR should be 2.0, got {atr[3]}"
    assert abs(atr[4] - 7.0 / 3.0) < 1e-12, f"Wilder step should be 7/3, got {atr[4]}"


def test_dmi_steady_uptrend_hand_computed():
    # s_TR = 3*2 = 6 and s_+DM = 3*1 = 3 at the seed; each Wilder step is
    # s - s/3 + x, i.e. 6 - 2 + 2 = 6 and 3 - 1 + 1 = 3 — both hold constant.
    # +DI = 100*3/6 = 50 ; -DI = 0 ; DX = 100 every bar ; ADX = 100.
    r = wilder_dmi(_up(8), 3)
    assert r is not None
    pdi, mdi, adx = r
    assert abs(pdi - 50.0) < 1e-9, f"+DI should be exactly 50, got {pdi}"
    assert mdi == 0.0, f"-DM is zero every bar, so -DI must be 0, got {mdi}"
    assert abs(adx - 100.0) < 1e-9, f"one-directional trend is ADX 100, got {adx}"


def test_dmi_varying_dx_hand_computed():
    # n=2. (h,l,c): (10,8,9) (11,9,10) (12,10,11) (12,9,10) (10,7,8)
    #   TR  = 2, 2, 3, 3      +DM = 1, 1, 0, 0      -DM = 0, 0, 1, 2
    # seed (k=2): sTR 4, s+DM 2, s-DM 0          -> +DI 50,  -DI 0,  DX 100
    # k=3: sTR 4-2+3=5, s+DM 2-1+0=1, s-DM 0-0+1=1 -> +DI 20,  -DI 20, DX 0
    # k=4: sTR 5-2.5+3=5.5, s+DM 1-.5+0=.5, s-DM 1-.5+2=2.5
    #        +DI 9.0909, -DI 45.4545, DX = 100*36.3636/54.5454 = 66.6667
    # ADX seed = mean(100, 0) = 50 ; then (50*1 + 66.6667)/2 = 58.3333
    bars = [DailyBar(0, 9, 10, 8, 9, 1), DailyBar(1, 10, 11, 9, 10, 1),
            DailyBar(2, 11, 12, 10, 11, 1), DailyBar(3, 11, 12, 9, 10, 1),
            DailyBar(4, 9, 10, 7, 8, 1)]
    pdi, mdi, adx = wilder_dmi(bars, 2)
    assert abs(pdi - 100 * 0.5 / 5.5) < 1e-9, f"+DI should be 9.0909, got {pdi}"
    assert abs(mdi - 100 * 2.5 / 5.5) < 1e-9, f"-DI should be 45.4545, got {mdi}"
    assert abs(adx - 58.33333333) < 1e-6, (
        f"ADX must seed with the MEAN of the first n DX values (50) and then "
        f"smooth: expected 58.3333, got {adx}")


def test_dmi_steady_downtrend_mirrors():
    r = wilder_dmi(_down(8), 3)
    assert r is not None
    pdi, mdi, adx = r
    assert pdi == 0.0, f"+DI must be 0 in a pure downtrend, got {pdi}"
    assert mdi > 40.0, f"-DI must be large, got {mdi}"
    assert adx > 99.0, f"got {adx}"


def test_dmi_needs_two_n_bars():
    assert wilder_dmi(_up(5), 3) is None, "n=3 needs 6 bars for ADX; 5 is too few"
    assert wilder_dmi(_up(6), 3) is not None


def test_rsi_hand_computed():
    # closes 10, 11, 10, 11  ->  deltas +1, -1, +1 ; n = 2
    # seed: avg gain (1+0)/2 = .5, avg loss (0+1)/2 = .5   -> RSI 50
    # step: gain (.5*1+1)/2 = .75, loss (.5*1+0)/2 = .25   -> RS 3 -> RSI 75
    assert abs(rsi([10, 11, 10], 2) - 50.0) < 1e-9
    assert abs(rsi([10, 11, 10, 11], 2) - 75.0) < 1e-9
    assert rsi([1, 2, 3, 4], 3) == 100.0, "no losses at all is RSI 100"
    assert rsi([1, 2], 3) is None


def test_supertrend_steady_uptrend_line_is_the_lower_band():
    # period 3, mult 1: ATR = 2, hl2 = 9+i, lower band = 9+i-2 = 7+i, and it
    # only ever rises, so the SuperTrend line IS the lower band and price is
    # above it by exactly 2 (close 9+i vs line 7+i).
    st = supertrend(_up(12), period=3, mult=1.0)
    line, above = st[-1]
    assert abs(line - (7.0 + 11)) < 1e-9, f"line should be 18.0, got {line}"
    assert above is True
    assert st[0] is None and st[1] is None and st[2] is None, "no line before the ATR"


def test_supertrend_uptrend_survives_a_shallow_dip_inside_the_band():
    # THE test that separates a stateful SuperTrend from a per-bar one. After
    # 12 rising bars the line is the lower band, 18.0 (=7+11), upper band 22.
    # A dip bar (h20 l18.5 c19): TR = max(1.5, 0, 1.5) = 1.5, ATR = (2*2+1.5)/3,
    # hl2 = 19.25, lower band 17.42 < 18 so the line does not loosen: it stays
    # 18.0 and close 19 is above it. A per-bar rule ("down whenever close <=
    # the upper band, 21.08") would call this bar down.
    dip = DailyBar(12, 19.0, 20.0, 18.5, 19.0, 1000.0)
    line, above = supertrend(_up(12) + [dip], period=3, mult=1.0)[-1]
    assert abs(line - 18.0) < 1e-9, f"the lower band must not loosen: {line}"
    assert above is True, "an established uptrend is not broken by a dip inside the band"


def test_supertrend_flips_down_on_a_crash():
    crash = DailyBar(12, 20.0, 20.0, 5.0, 6.0, 1000.0)
    st = supertrend(_up(12) + [crash], period=3, mult=1.0)
    assert st[-1][1] is False, f"close 6 is far below the ~18 line: {st[-1]}"
    # a weak bounce that stays under the tightened upper band stays down
    bounce = DailyBar(13, 6.0, 9.0, 5.5, 8.0, 1000.0)
    st2 = supertrend(_up(12) + [crash, bounce], period=3, mult=1.0)
    assert st2[-1][1] is False, f"a small bounce must stay below the line: {st2[-1]}"


def test_supertrend_recovers_when_price_truly_reclaims():
    bars = _up(12)
    crash = DailyBar(12, 20.0, 20.0, 5.0, 6.0, 1000.0)
    rally = [DailyBar(13 + i, 30.0 + 10 * i, 40.0 + 10 * i, 30.0 + 10 * i,
                      40.0 + 10 * i, 1000.0) for i in range(3)]
    st = supertrend(bars + [crash] + rally, period=3, mult=1.0)
    assert st[-1][1] is True, f"a real reclaim must flip the trend back up: {st[-1]}"


def test_max_close_jump_flags_an_unadjusted_split():
    smooth = [DailyBar(i, 100, 101, 99, 100 + i, 1) for i in range(10)]
    assert max_close_jump(smooth) < 1.05
    split = smooth[:5] + [DailyBar(5 + i, 50, 51, 49, 50 + i, 1) for i in range(5)]
    assert max_close_jump(split) > 1.9, "a 2:1 step must read as ~2.0"


def test_feature_panel_short_history_is_none_not_a_default():
    p = feature_panel(_up(30))
    assert p["sma50_gt_200"] is None and p["dist_sma50"] is None, "30 bars is no SMA50"
    assert p["di_minus"] is not None, "30 bars is enough for a 14-period DMI"
    assert p["n_bars"] == 30
    assert feature_panel([])["above_st"] is None


def test_feature_panel_full_history_uptrend():
    p = feature_panel(_up(260))
    assert p["above_st"] is True
    assert p["sma50_gt_200"] is True, "a 260-bar steady rise has SMA50 > SMA200"
    assert p["above_sma50"] is True
    assert p["di_minus"] == 0.0 and p["adx"] > 99.0
    assert p["rsi14"] == 100.0
    assert p["prev_vol_ratio"] == 1.0, "flat 1000 volume: last / mean(prior 20) = 1"
    # ret_1m: last close is 9+259 = 268; closes[-22] is index 238 -> 9+238 = 247
    assert abs(p["ret_1m"] - (268.0 / 247.0 - 1) * 100) < 1e-3, p["ret_1m"]
    assert set(PANEL_KEYS) <= set(p), "every advertised key is present"


def test_prev_vol_ratio_excludes_the_day_itself_from_its_baseline():
    # 20 days at 1000, then a 3000 day: 3000 / mean(prior 20 = 1000) = 3.0.
    # Folding the day into its own baseline would give 3000 / 1100 = 2.727.
    bars = _up(30)
    bars = bars[:-1] + [bars[-1]._replace(volume=3000.0)]
    assert feature_panel(bars)["prev_vol_ratio"] == 3.0, feature_panel(bars)["prev_vol_ratio"]


def test_feature_panel_downtrend_reads_opposite():
    p = feature_panel(_down(260, start=1000.0, step=3.0))
    assert p["above_st"] is False
    assert p["sma50_gt_200"] is False
    assert p["di_plus"] == 0.0 and p["di_minus"] > 40.0


TESTS = [
    ("sma needs a full window", test_sma_needs_a_full_window),
    ("Wilder ATR — hand-computed", test_wilder_atr_hand_computed),
    ("DMI — steady uptrend, hand-computed", test_dmi_steady_uptrend_hand_computed),
    ("DMI — varying DX, ADX seed and smoothing hand-computed",
     test_dmi_varying_dx_hand_computed),
    ("DMI — downtrend mirrors", test_dmi_steady_downtrend_mirrors),
    ("DMI — needs 2n bars", test_dmi_needs_two_n_bars),
    ("RSI — hand-computed", test_rsi_hand_computed),
    ("SuperTrend — steady uptrend line is the lower band",
     test_supertrend_steady_uptrend_line_is_the_lower_band),
    ("SuperTrend — an uptrend survives a shallow dip inside the band (stateful)",
     test_supertrend_uptrend_survives_a_shallow_dip_inside_the_band),
    ("SuperTrend — flips down on a crash",
     test_supertrend_flips_down_on_a_crash),
    ("SuperTrend — recovers on a real reclaim",
     test_supertrend_recovers_when_price_truly_reclaims),
    ("close-jump guard flags an unadjusted split",
     test_max_close_jump_flags_an_unadjusted_split),
    ("feature panel — short history is None, not a default",
     test_feature_panel_short_history_is_none_not_a_default),
    ("feature panel — full-history uptrend", test_feature_panel_full_history_uptrend),
    ("feature panel — prev_vol_ratio baseline excludes the day itself",
     test_prev_vol_ratio_excludes_the_day_itself_from_its_baseline),
    ("feature panel — downtrend reads opposite",
     test_feature_panel_downtrend_reads_opposite),
]
