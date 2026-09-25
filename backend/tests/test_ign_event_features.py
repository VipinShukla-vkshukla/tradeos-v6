"""
IGN event-study features and outcomes — tools/replay/ign_event_features.py.

The order matters. A feature that reads one bar past its trigger would make every event look
better than it was and every later result untrustworthy, so the look-ahead tests come first: poison
every bar AFTER the trigger and require that nothing the features, the trigger or the market state
report changes. Then hand-computed values (each worked out in the comment beside it, never copied
from the implementation), and parity with the live IgnitionMomentum class on the same bars.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from tests import cfg_ctx
from tools.replay import ign_event_features as F


def _day(n=60, base=100.0, drift=0.02, vol=1000.0, wick=0.2):
    """A quiet, gently rising session. Bar i closes at base + drift*i."""
    c = base + drift * np.arange(n)
    return F.DayBars(o=c - 0.01, h=c + wick, l=c - wick, c=c, v=np.full(n, vol))


def _spike(d: F.DayBars, at: int, to: float, vol_mult=30.0) -> F.DayBars:
    """From bar `at`, price steps to `to` (and stays a little above), on heavy volume."""
    o, h, l, c, v = (a.copy() for a in d)
    for j in range(at, d.n):
        c[j] = to + 0.02 * (j - at)
        o[j] = c[j] - 0.01
        h[j], l[j] = c[j] + 0.05, c[j] - 0.05
        v[j] = d.v[j] * vol_mult
    return F.DayBars(o, h, l, c, v)


def _poison(d: F.DayBars, after: int) -> F.DayBars:
    """Wreck every bar strictly after `after` — wildly different prices and volumes."""
    o, h, l, c, v = (a.copy() for a in d)
    o[after + 1:], h[after + 1:], l[after + 1:], c[after + 1:] = 9999.0, 99999.0, 1.0, 5555.0
    v[after + 1:] = 9e12
    return F.DayBars(o, h, l, c, v)


PROFILE = np.linspace(0.0, 1.0, F.SESSION_MIN + 1)[1:]     # a flat intraday volume profile


# ── look-ahead: the tests that matter most ──────────────────────────────────

def test_the_trigger_does_not_depend_on_later_bars():
    d = _spike(_day(120), at=60, to=104.0)
    t = F.first_trigger(d, 100.0, 60_000.0, 3.0, 2.0)
    assert t is not None and t >= 60, t
    assert F.first_trigger(_poison(d, t), 100.0, 60_000.0, 3.0, 2.0) == t, (
        "wrecking every bar after the trigger must not move it")
    o, h, l, c, v = (a.copy() for a in d)
    c[t], h[t], o[t] = 100.0, 100.05, 99.99          # the trigger bar no longer closes +3%
    later = F.first_trigger(F.DayBars(o, h, l, c, v), 100.0, 60_000.0, 3.0, 2.0)
    assert later == t + 1, (
        f"changing the trigger bar itself must move the trigger (to {t + 1}), got {later}; "
        f"otherwise this test proves nothing")


def test_intraday_features_read_only_bars_up_to_the_trigger():
    d = _spike(_day(120), at=60, to=104.0)
    i = 70
    base = F.intraday_features(d, i, 100.0, 60_000.0, 60_000.0, PROFILE)
    dirty = F.intraday_features(_poison(d, i), i, 100.0, 60_000.0, 60_000.0, PROFILE)
    assert base == dirty, {k: (base[k], dirty[k]) for k in base if base[k] != dirty[k]}
    moved = F.intraday_features(_poison(d, i - 1), i, 100.0, 60_000.0, 60_000.0, PROFILE)
    assert moved != base, "poisoning the trigger bar itself must change something"


def test_market_features_read_only_bars_up_to_the_trigger():
    nifty, n500, vix = _day(120, 22000.0, 1.0), _day(120, 21000.0, 1.0), _day(120, 14.0, 0.001)
    i = 50
    args = dict(nifty_prev_close=22000.0, n500_prev_close=21000.0, vix_prev_close=14.0,
                stock_move_pct=3.5)
    base = F.market_features(nifty, n500, vix, i, **args)
    dirty = F.market_features(_poison(nifty, i), _poison(n500, i), _poison(vix, i), i, **args)
    assert base == dirty


def test_forward_outcomes_never_use_the_trigger_bar_or_earlier_for_the_path():
    d = _spike(_day(120), at=60, to=104.0)
    i = 70
    base = F.forward_outcomes(d, i, fill="next_open")
    o, h, l, c, v = (a.copy() for a in d)
    o[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1] = 1.0, 2.0, 0.5, 1.5      # wreck the past
    past_wrecked = F.forward_outcomes(F.DayBars(o, h, l, c, v), i, fill="next_open")
    assert base == past_wrecked, "next_open entry and the path must depend only on bars AFTER i"
    tc = F.forward_outcomes(d, i, fill="trigger_close")
    assert tc["entry"] == float(d.c[i]), "the replay convention enters at the trigger close"
    assert base["entry"] == float(d.o[i + 1]), "the executable convention enters at the next open"


def test_daily_features_use_only_the_bars_they_are_given():
    from intraday.trend_indicators import DailyBar
    bars = [DailyBar(date(2026, 1, 1) + timedelta(days=k), 100.0 + k, 101.0 + k, 99.0 + k,
                     100.0 + k, 1000.0) for k in range(300)]
    base = F.daily_features(bars)
    future = bars + [DailyBar(date(2027, 1, 1), 1, 9999, 1, 5000, 9e9)]
    assert F.daily_features(bars) == base and F.daily_features(future) != base, (
        "the caller truncates to prior sessions; a bar it did not pass must never be read")


# ── the trigger and IGN parity ──────────────────────────────────────────────

def test_ign_volume_ratio_is_the_live_formula():
    d = _day(10, vol=1000.0)
    r = F.ign_volume_ratio(d, prev_day_volume=3750.0)
    # bar 4: cumulative 5*1000 = 5000; mins = max(1, 4) = 4; expected = 3750*4/375 = 40*...
    # 3750 * 4 / 375 = 40 -> ratio 5000/40 = 125
    assert abs(r[4] - 125.0) < 1e-9, r[4]
    # bar 0: mins clamps to 1: 1000 / (3750*1/375) = 1000/10 = 100
    assert abs(r[0] - 100.0) < 1e-9, r[0]


def test_first_trigger_honours_move_volume_and_minimum_bars():
    d = _spike(_day(120), at=20, to=104.0, vol_mult=30.0)
    assert F.first_trigger(d, 100.0, 60_000.0, 3.0, 0.0) == 20, "no volume test: first close >= +3%"
    assert F.first_trigger(d, 100.0, 60_000.0, 8.0, 0.0) is None, "never reaches +8%"
    assert F.first_trigger(d, 100.0, 60_000.0, 5.0, 0.0) == 70, (
        "the slow drift after the spike (+0.02/bar from 104) crosses +5% only at bar 70")
    early = _spike(_day(120), at=3, to=104.0)
    assert F.first_trigger(early, 100.0, 60_000.0, 3.0, 0.0) == 7, (
        "the 8-bar minimum: bar 7 is the first that may trigger")
    assert F.first_trigger(early, 100.0, 60_000.0, 3.0, 0.0, min_idx=44) == 44, "open-hour gate"
    thin = _spike(_day(120), at=20, to=104.0, vol_mult=1.0)
    assert F.first_trigger(thin, 100.0, 1e9, 3.0, 2.0) is None, "huge prior-day volume: ratio never clears"
    assert F.first_trigger(thin, 100.0, 0.0, 3.0, 2.0) is None, "no prior-day volume: refuse, not divide"


def test_feasibility_uses_the_structural_stop():
    # 20 flat bars near 100 (low 99.8), then a jump to 103.6: the 20-bar swing low is far below,
    # risk = (103.6 - 99.8*0.9988)/103.6 = 3.8% > 1.75% -> infeasible until the base catches up
    d = _spike(_day(80), at=30, to=103.6, vol_mult=1.0)
    t = F.first_trigger(d, 100.0, 1e12 * 0 + 1.0, 3.0, 0.0)
    assert t == 30
    assert F.structural_risk_pct(d, 30) > F.MAX_RISK_PCT
    f = F.first_trigger(d, 100.0, 1.0, 3.0, 0.0, require_feasible=True)
    assert f is not None and f > 30, "the stop only becomes affordable once 20 bars sit near the highs"
    assert F.structural_risk_pct(d, f) <= F.MAX_RISK_PCT


def test_parity_with_the_live_ignition_engine():
    """The event extractor's first trigger must equal the bar the live engine first detects on."""
    from datetime import datetime
    from config import IST
    from intraday.daily_history import to_daily_bars
    from intraday.strategies.ignition import IgnitionMomentum
    from tests.test_ign_feature_study import _raw, _spike_day
    from tools.replay import detect
    from tools.replay import ign_feature_stats as S
    day_bars = _spike_day()
    d = F.DayBars(np.array([b.open for b in day_bars]), np.array([b.high for b in day_bars]),
                  np.array([b.low for b in day_bars]), np.array([b.close for b in day_bars]),
                  np.array([b.volume for b in day_bars]))
    prev = S.prev_from_daily(to_daily_bars(_raw(), date(2026, 8, 5)), date(2026, 8, 5))
    saved = detect.ENGINES
    detect.ENGINES = [IgnitionMomentum()]
    try:
        with cfg_ctx({"ign_stop_lookback_bars": "20"}):
            dets = [x for x in detect.replay_symbol_day("T", "2026-08-05", day_bars, prev=prev,
                                                        dedup_pct=-1.0)
                    if x.engine == "IGN" and x.direction == "LONG"]
    finally:
        detect.ENGINES = saved
    assert dets, "fixture must produce a live detection or this proves nothing"
    live_i = int((dets[0].ts - day_bars[0].ts).total_seconds() // 60) - 1
    mine = F.first_trigger(d, prev["close"], prev["volume"], 3.5, 2.0,
                           min_idx=F.OPEN_HOUR_END_IDX, require_feasible=True)
    assert mine == live_i, f"event extractor says bar {mine}, live IGN detects at bar {live_i}"
    assert F.first_trigger(d, prev["close"], prev["volume"], 3.5, 2.0, min_idx=F.MIN_BARS - 1,
                           require_feasible=True) <= mine, "without the open-hour gate it can only be earlier"


# ── hand-computed values ────────────────────────────────────────────────────

def test_intraday_features_hand_computed():
    # 5 bars: closes 100, 101, 102, 103, 104; highs close+1, lows close-1; volume 10 each.
    c = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
    d = F.DayBars(c - 0.5, c + 1.0, c - 1.0, c, np.full(5, 10.0))
    f = F.intraday_features(d, 4, prev_close=98.0, prev_day_volume=100.0, avg20_volume=100.0,
                            profile=np.full(375, 0.5))
    assert abs(f["move_pct"] - (104 / 98 - 1) * 100) < 1e-9            # 6.1224...
    assert abs(f["gap_pct"] - (99.5 / 98 - 1) * 100) < 1e-9            # open of bar 0 = 99.5
    assert abs(f["move_from_open_pct"] - (104 / 99.5 - 1) * 100) < 1e-9
    assert abs(f["from_high_pct"] - (104 / 105 - 1) * 100) < 1e-9      # day high 105
    assert abs(f["pos_in_range"] - (104 - 99) / (105 - 99)) < 1e-9     # low 99, high 105
    # vwap: typical = close (h+l+c)/3 = c; equal volumes -> mean of closes = 102
    assert abs(f["vwap_dev_pct"] - (104 / 102 - 1) * 100) < 1e-9
    assert f["pct_bars_above_vwap"] == 0.8, "bar 0 equals its own vwap (not above); bars 1-4 are above"
    # vr_ign: cumulative 50 / (100 * max(1,4)/375) = 50 / 1.0667 = 46.875
    assert abs(f["vr_ign"] - 46.875) < 1e-9
    # rvol_profile: 50 / (100 * 0.5) = 1.0
    assert abs(f["rvol_profile"] - 1.0) < 1e-9
    assert f["bars_since_1pct"] == 4, "the close crossed +1% vs 98 on bar 0 (100/98 = +2%)"
    assert f["above_orh"] is None, "the opening range (15 bars) is not complete yet"
    # structural stop: min low of the last 20 bars (all 5) = 99, * (1-0.0012)
    risk = (104 - 99 * (1 - 0.0012)) / 104 * 100
    assert abs(f["risk_pct"] - risk) < 1e-9 and f["feasible"] is False, "4.9% risk is not affordable"


def test_volume_profile_is_the_median_cumulative_fraction():
    full = np.ones(375)
    front = np.concatenate([np.full(100, 3.0), np.full(275, 1.0)])   # front-loaded
    p = F.volume_profile([F.DayBars(full, full, full, full, full),
                          F.DayBars(full, full, full, full, full),
                          F.DayBars(full, full, full, full, front)])
    assert abs(p[0] - 1 / 375) < 1e-12, "median picks the two ordinary days"
    assert abs(p[-1] - 1.0) < 1e-12
    short = F.DayBars(full[:100], full[:100], full[:100], full[:100], full[:100])
    p2 = F.volume_profile([short, F.DayBars(full, full, full, full, full)])
    assert abs(p2[0] - 1 / 375) < 1e-12, "a partial session is ignored, not padded into the median"
    try:
        F.volume_profile([short])
    except ValueError:
        pass
    else:
        raise AssertionError("no full session must raise, not return a made-up profile")


def test_forward_outcomes_hand_computed():
    # bars 0..9; trigger bar i=3; entry at bar 4's open = 110
    o = np.array([100, 101, 102, 103, 110, 111, 112, 113, 114, 115], float)
    h = o + 2.0
    l = o - 1.0
    c = o + 0.5
    d = F.DayBars(o, h, l, c, np.full(10, 1.0))
    f = F.forward_outcomes(d, 3)
    assert f["entry"] == 110.0 and f["entry_idx"] == 4
    assert abs(f["mfe_pct"] - (117.0 / 110 - 1) * 100) < 1e-9          # max high bar 9 = 115+2
    assert abs(f["mae_pct"] - (109.0 / 110 - 1) * 100) < 1e-9          # min low bar 4 = 110-1
    assert f["bars_to_mfe"] == 5 and f["bars_to_mae"] == 0
    assert abs(f["eod_ret_pct"] - (115.5 / 110 - 1) * 100) < 1e-9      # last close bar 9
    assert abs(f["ret_15_pct"] - f["eod_ret_pct"]) < 1e-9, "a horizon past the data clamps to the last bar"
    assert F.forward_outcomes(d, 9) is None, "no bar left to enter on"
    late = _day(400)
    assert F.forward_outcomes(late, 359) is None, "bar 360 is after the 15:15 square-off"
    assert F.forward_outcomes(late, 300) is not None
    try:
        F.forward_outcomes(d, 3, fill="whenever")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown fill convention must raise")


def test_market_features_hand_computed_and_missing_is_none():
    nifty = F.DayBars(np.full(40, 22000.0), np.full(40, 22100.0), np.full(40, 21900.0),
                      np.linspace(22000.0, 22220.0, 40), np.zeros(40))
    f = F.market_features(nifty, None, None, 39, nifty_prev_close=22000.0, n500_prev_close=None,
                          vix_prev_close=None, stock_move_pct=3.0)
    assert abs(f["nifty_move_pct"] - 1.0) < 1e-9                        # 22220/22000
    assert abs(f["nifty_ret_30_pct"] - (22220 / float(nifty.c[9]) - 1) * 100) < 1e-9
    assert abs(f["rs_nifty"] - 2.0) < 1e-9
    assert f["n500_move_pct"] is None and f["vix"] is None and f["rs_n500"] is None, (
        "missing inputs are None, never a default that looks like a measurement")
    assert F.market_features(None, None, None, 5, None, None, None, 3.0)["nifty_move_pct"] is None


def test_daily_features_hand_computed():
    from intraday.trend_indicators import DailyBar
    # 260 sessions: close = 100 + k, high = close + 1, low = close - 1, volume 1000 (last = 5000)
    bars = [DailyBar(date(2026, 1, 1) + timedelta(days=k), 100.0 + k, 101.0 + k, 99.0 + k,
                     100.0 + k, 5000.0 if k == 259 else 1000.0) for k in range(260)]
    f = F.daily_features(bars)
    last = 359.0
    assert abs(f["ret_5d"] - (last / 354.0 - 1) * 100) < 1e-9
    assert abs(f["ret_20d"] - (last / 339.0 - 1) * 100) < 1e-9
    assert abs(f["hi52_dist"] - (last / 360.0 - 1) * 100) < 1e-9        # 52w high is today's high 360
    assert abs(f["prev_range_pct"] - 2.0 / last * 100) < 1e-9
    assert abs(f["prev_clv"] - 0.5) < 1e-9, "close midway between low and high"
    assert f["nr7"] is True, "equal ranges: the last is the smallest (<=)"
    assert abs(f["avg20_volume"] - (19 * 1000 + 5000) / 20) < 1e-9
    assert f["prev_day_volume"] == 5000.0 and f["up_streak"] == 259
    assert f["n_ign_20d"] == 0, "a +1/100 daily drift is far from a +3% high"
    assert F.daily_features(bars[:20]) == {}, "too little history: no features, not defaults"


def test_index_daily_features_use_prior_sessions_only():
    rows = [{"date": (date(2026, 1, 1) + timedelta(days=k)).isoformat(), "close": 100.0 + k}
            for k in range(60)]
    day = (date(2026, 1, 1) + timedelta(days=40)).isoformat()
    f = F.index_daily_features(rows, day)
    assert f["prev_close"] == 139.0, "the session BEFORE `day`, never `day` itself"
    assert abs(f["ret_5d"] - (139 / 134 - 1) * 100) < 1e-9
    assert F.index_daily_features(rows, rows[10]["date"]) == {}


def test_the_trigger_boundary_is_inclusive():
    # 125 / 100 is exactly representable, so +25% is exactly +25.0 with no rounding to hide behind
    o = np.full(60, 100.0)
    c = np.full(60, 100.0)
    c[20:] = 125.0
    d = F.DayBars(o, c + 0.1, c - 0.1, c, np.full(60, 1000.0))
    assert F.first_trigger(d, 100.0, 60_000.0, 25.0, 0.0) == 20, "a close exactly at the threshold triggers"
    assert F.first_trigger(d, 100.0, 60_000.0, 25.0001, 0.0) is None


def test_the_open_hour_index_is_the_bar_that_closes_at_ten():
    from datetime import datetime
    from config import IST
    from intraday.session import hour_bucket
    open_dt = IST.localize(datetime(2026, 8, 5, 9, 15))
    close_of = lambda idx: open_dt + timedelta(minutes=idx + 1)         # bar i closes at 09:16 + i
    assert hour_bucket(close_of(F.OPEN_HOUR_END_IDX - 1)) == "OPEN"
    assert hour_bucket(close_of(F.OPEN_HOUR_END_IDX)) == "MID", (
        "the first bar the armed open-hour gate lets through is the one closing at 10:00")


def test_nothing_after_the_squareoff_bar_reaches_the_outcomes():
    d = _spike(_day(375), at=100, to=104.0)
    base = F.forward_outcomes(d, 150)
    o, h, l, c, v = (a.copy() for a in d)
    o[360:], h[360:], l[360:], c[360:] = 1.0, 99999.0, 0.5, 5555.0      # after the 15:15 square-off
    late_wrecked = F.forward_outcomes(F.DayBars(o, h, l, c, v), 150)
    assert base == late_wrecked, "bars from 15:15 on can neither help nor hurt a trade that is flat by then"
    assert abs(base["eod_ret_pct"] - (float(d.c[359]) / float(d.o[151]) - 1) * 100) < 1e-9


def test_prior_ignitions_are_counted_against_each_days_own_prior_close():
    from intraday.trend_indicators import DailyBar
    bars = []
    for k in range(60):
        close = 100.0 if k < 50 else 97.5
        high = close + 0.5
        if k == 45:
            high = 104.0                       # +4% over the prior close: a genuine ignition day
        if k == 50:
            high = 101.0                       # only +1% over the prior close, but 3.6% over its own
        bars.append(DailyBar(date(2026, 1, 1) + timedelta(days=k), close, high, close - 0.5, close, 1000.0))
    f = F.daily_features(bars)
    assert f["n_ign_20d"] == 1, f"only day 45 ignited; a fall from a high is not an ignition: {f['n_ign_20d']}"
    assert f["prev_day_ign"] is False
    last = bars[-1]
    bars[-1] = last._replace(high=bars[-2].close * 1.05)
    assert F.daily_features(bars)["prev_day_ign"] is True, "a +5% high on the last session"


def test_the_52_week_window_forgets_older_highs():
    from intraday.trend_indicators import DailyBar
    bars = [DailyBar(date(2025, 1, 1) + timedelta(days=k), 100.0, 101.0, 99.0, 100.0, 1000.0)
            for k in range(300)]
    bars[10] = bars[10]._replace(high=500.0)                          # 290 sessions ago
    f = F.daily_features(bars)
    assert abs(f["hi52_dist"] - (100.0 / 101.0 - 1) * 100) < 1e-9, (
        f"a high older than 252 sessions must not count: {f['hi52_dist']}")


def test_no_entry_bar_after_the_squareoff():
    d = _spike(_day(375), at=100, to=104.0)
    assert F.forward_outcomes(d, 358) is not None, "entry on bar 359, the last bar before square-off"
    assert F.forward_outcomes(d, 359) is None, "the next bar is 15:15 or later"
    assert F.forward_outcomes(d, 370) is None
    assert F.forward_outcomes(d, 359, fill="trigger_close") is None


def test_feature_panel_keys_are_all_present():
    from intraday.trend_indicators import DailyBar
    bars = [DailyBar(date(2025, 1, 1) + timedelta(days=k), 100.0 + k, 101.0 + k, 99.0 + k,
                     100.0 + k, 1000.0) for k in range(300)]
    f = F.daily_features(bars)
    for k in ("adx", "di_minus", "above_st", "atr14_pct", "dist_sma20", "ret_60d", "hi52_dist",
              "range_ratio", "nr7", "avg20_turnover_cr", "n_ign_20d", "up_streak", "prev_high"):
        assert k in f and f[k] is not None, k


TESTS = [
    ("the trigger does not depend on later bars", test_the_trigger_does_not_depend_on_later_bars),
    ("intraday features read only bars up to the trigger",
     test_intraday_features_read_only_bars_up_to_the_trigger),
    ("market features read only bars up to the trigger",
     test_market_features_read_only_bars_up_to_the_trigger),
    ("forward outcomes never use the trigger bar or earlier for the path",
     test_forward_outcomes_never_use_the_trigger_bar_or_earlier_for_the_path),
    ("daily features use only the bars they are given",
     test_daily_features_use_only_the_bars_they_are_given),
    ("IGN volume ratio is the live formula", test_ign_volume_ratio_is_the_live_formula),
    ("first trigger honours move, volume and minimum bars",
     test_first_trigger_honours_move_volume_and_minimum_bars),
    ("feasibility uses the structural stop", test_feasibility_uses_the_structural_stop),
    ("parity with the live ignition engine", test_parity_with_the_live_ignition_engine),
    ("intraday features, hand-computed", test_intraday_features_hand_computed),
    ("volume profile is the median cumulative fraction",
     test_volume_profile_is_the_median_cumulative_fraction),
    ("forward outcomes, hand-computed", test_forward_outcomes_hand_computed),
    ("market features, hand-computed; missing is None",
     test_market_features_hand_computed_and_missing_is_none),
    ("daily features, hand-computed", test_daily_features_hand_computed),
    ("index daily features use prior sessions only",
     test_index_daily_features_use_prior_sessions_only),
    ("the trigger boundary is inclusive", test_the_trigger_boundary_is_inclusive),
    ("the open-hour index is the bar that closes at ten",
     test_the_open_hour_index_is_the_bar_that_closes_at_ten),
    ("nothing after the square-off bar reaches the outcomes",
     test_nothing_after_the_squareoff_bar_reaches_the_outcomes),
    ("prior ignitions are counted against each day's own prior close",
     test_prior_ignitions_are_counted_against_each_days_own_prior_close),
    ("the 52-week window forgets older highs", test_the_52_week_window_forgets_older_highs),
    ("no entry bar after the square-off", test_no_entry_bar_after_the_squareoff),
    ("feature panel keys are all present", test_feature_panel_keys_are_all_present),
]
