"""
Kite daily history (24-Sep-2026) — intraday/daily_history.py.

WHAT THIS CATCHES
-----------------
The feature panel is only the quantity the replay study measured if it is built
from COMPLETED sessions. The Kite daily endpoint returns today's still-forming
candle when asked mid-session, so the worst silent failure here is a forming bar
reaching the indicators: a +5% spike would drag price above its own SuperTrend
by construction and the gate would read "healthy trend" on exactly the moves it
exists to judge. The first tests pin that down.

The rest pin the integrity guards (an unadjusted split, a raw-close mismatch),
that "cannot verify" is not "measured bad", that a failed fetch yields no panel
rather than a default, and the worker's queue/rollover behaviour with a fake
Kite object — no network.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from config import IST
from tests import cfg_ctx

TODAY = date(2026, 9, 24)


def _raw(n: int, end: date, *, start_close: float = 100.0, step: float = 1.0,
         volume: float = 1000.0) -> list[dict]:
    """n consecutive calendar-day candles ending at `end`, Kite's dict shape."""
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        c = start_close + step * i
        out.append({"date": datetime(d.year, d.month, d.day, tzinfo=IST),
                    "open": c - 0.5, "high": c + 1.0, "low": c - 1.0,
                    "close": c, "volume": volume})
    return out


def test_a_forming_bar_dated_today_never_reaches_the_indicators():
    from intraday.daily_history import to_daily_bars
    raw = _raw(30, TODAY)                       # last candle IS today's
    bars = to_daily_bars(raw, TODAY)
    assert len(bars) == 29, f"today's candle must be dropped, got {len(bars)} bars"
    assert bars[-1].date == TODAY - timedelta(days=1), bars[-1].date
    future = _raw(3, TODAY + timedelta(days=2))
    assert to_daily_bars(future, TODAY) == [], "bars dated after today are dropped too"


def test_to_daily_bars_is_sorted_deduped_and_skips_bad_rows():
    from intraday.daily_history import to_daily_bars
    raw = _raw(5, TODAY - timedelta(days=1))
    shuffled = [raw[3], raw[0], raw[4], raw[1], raw[2], dict(raw[2], close=999.0)]
    shuffled.append({"date": raw[0]["date"] - timedelta(days=9), "open": 0, "high": 0,
                     "low": 0, "close": 0, "volume": 1})        # zero price
    shuffled.append({"date": raw[0]["date"] - timedelta(days=10)})   # missing keys
    bars = to_daily_bars(shuffled, TODAY)
    assert [b.date for b in bars] == sorted(b.date for b in bars), "oldest first"
    assert len(bars) == 5, f"5 real dates; dup collapsed, 2 bad rows dropped: {len(bars)}"
    assert bars[2].close == 999.0, "on a duplicate date the later row wins"


def test_a_clean_history_is_ok_and_carries_the_panel():
    from intraday.daily_history import build_panel
    p = build_panel(_raw(260, TODAY - timedelta(days=1)), TODAY)
    assert p["ok"] is True and p["reason"] is None, p
    assert p["as_of"] == str(TODAY - timedelta(days=1)), p["as_of"]
    assert p["above_st"] is True and p["sma50_gt_200"] is True
    assert p["xcheck_max_diff_pct"] is None, "no reference given -> not checked, and says so"


def test_an_unadjusted_split_makes_the_symbol_unreliable():
    from intraday.daily_history import build_panel
    raw = _raw(260, TODAY - timedelta(days=1))
    for r in raw[:130]:                          # older half looks 2x pre-split
        for k in ("open", "high", "low", "close"):
            r[k] *= 2.0
    p = build_panel(raw, TODAY)
    assert p["ok"] is False, "a 2:1 step in the closes must be refused"
    assert "close_jump" in p["reason"], p["reason"]


def test_a_raw_close_mismatch_is_refused_and_a_match_is_not():
    from intraday.daily_history import build_panel
    raw = _raw(60, TODAY - timedelta(days=1))
    good = {datetime.fromisoformat(str(r["date"])).date(): r["close"] for r in raw[-5:]}
    ok = build_panel(raw, TODAY, good)
    assert ok["ok"] is True and ok["xcheck_max_diff_pct"] == 0.0, ok
    bad = {k: v * 1.05 for k, v in good.items()}
    p = build_panel(raw, TODAY, bad)
    assert p["ok"] is False and "ref_mismatch" in p["reason"], p


def test_short_history_is_flagged_not_defaulted():
    from intraday.daily_history import build_panel
    p = build_panel(_raw(8, TODAY - timedelta(days=1)), TODAY)
    assert p["ok"] is False and "short_history" in p["reason"], p
    assert p["sma50_gt_200"] is None and p["above_st"] is None


def test_fetch_retries_a_rate_limit_and_raises_anything_else():
    from intraday.daily_history import fetch_daily

    class K:
        def __init__(self, errs):
            self.errs, self.calls = list(errs), 0

        def historical_data(self, *a):
            self.calls += 1
            if self.errs:
                raise Exception(self.errs.pop(0))
            return [{"x": 1}]

    slept = []
    k = K(["Too many requests", "Too many requests"])
    out = fetch_daily(k, 1, TODAY, TODAY, spacing_s=0.7, sleep=slept.append)
    assert out == [{"x": 1}] and k.calls == 3, (out, k.calls)
    assert 0.7 in slept and any(s >= 2.0 for s in slept), f"spacing + back-off: {slept}"
    try:
        fetch_daily(K(["Invalid token"]), 1, TODAY, TODAY, spacing_s=0, sleep=lambda s: None)
    except Exception as e:
        assert "Invalid token" in str(e)
    else:
        raise AssertionError("a non-rate-limit error must propagate, not be retried")
    try:
        fetch_daily(K(["Too many requests"] * 9), 1, TODAY, TODAY, spacing_s=0,
                    sleep=lambda s: None, retries=3)
    except RuntimeError:
        pass
    else:
        raise AssertionError("persistent rate limiting must eventually give up")


class _FakeKite:
    def __init__(self, symbols, *, missing=(), broken=()):
        self.tokens = {s: 1000 + i for i, s in enumerate(symbols) if s not in missing}
        self.broken = {self.tokens[s] for s in broken if s in self.tokens}
        self.hist_calls: list[tuple] = []

    def ltp(self, keys):
        return {k: {"instrument_token": self.tokens[k.split(":", 1)[1]],
                    "last_price": 1.0}
                for k in keys if k.split(":", 1)[1] in self.tokens}

    def historical_data(self, token, start, end, interval):
        self.hist_calls.append((token, start, end, interval))
        if token in self.broken:
            raise Exception("Invalid instrument")
        # includes today's forming candle, exactly as the live endpoint would
        return _raw(260, TODAY)


def _wait(dh, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with dh._lock:
            if dh._thread is None and not dh._queue:
                return
        time.sleep(0.01)
    raise AssertionError("worker did not drain")


def test_worker_loads_panels_from_completed_sessions_only():
    from intraday.daily_history import DailyHistory
    kite = _FakeKite(["AAA", "BBB"])
    with cfg_ctx({"daily_history_spacing_s": "0"}):
        dh = DailyHistory(sleep=lambda s: None)
        assert dh.ensure(kite, ["AAA", "BBB"], TODAY) == 2
        _wait(dh)
    a = dh.features("AAA")
    assert a is not None and a["ok"] is True, a
    assert a["as_of"] == str(TODAY - timedelta(days=1)), (
        f"the forming candle dated {TODAY} must not be the last bar: {a['as_of']}")
    assert a["n_bars"] == 259, a["n_bars"]
    for _, start, end, interval in kite.hist_calls:
        assert interval == "day" and end == TODAY - timedelta(days=1), (
            "the request itself must end yesterday, not only be filtered afterwards")
        assert (TODAY - start).days == 420, start
    assert dh.features("ZZZ") is None, "an unknown symbol has no panel — never a default"


def test_failures_yield_no_panel_and_are_retried_later_not_hammered():
    from intraday.daily_history import DailyHistory
    kite = _FakeKite(["AAA", "BBB", "CCC"], missing=("CCC",), broken=("BBB",))
    clock = [1000.0]
    with cfg_ctx({"daily_history_spacing_s": "0"}):
        dh = DailyHistory(sleep=lambda s: None, clock=lambda: clock[0])
        dh.ensure(kite, ["AAA", "BBB", "CCC"], TODAY)
        _wait(dh)
        s = dh.stats()
        assert s["ok"] == 1 and s["failed"] == 2, s
        assert dh.features("BBB") is None and dh.features("CCC") is None
        assert dh.ensure(kite, ["AAA", "BBB", "CCC"], TODAY) == 0, (
            "a just-failed symbol must not be re-queued every 15s cycle")
        clock[0] += 901.0
        assert dh.ensure(kite, ["AAA", "BBB", "CCC"], TODAY) == 2, (
            "after the retry interval the failures get another go")
        _wait(dh)


def test_a_new_trading_day_discards_yesterdays_panels():
    from intraday.daily_history import DailyHistory
    kite = _FakeKite(["AAA"])
    with cfg_ctx({"daily_history_spacing_s": "0"}):
        dh = DailyHistory(sleep=lambda s: None)
        dh.ensure(kite, ["AAA"], TODAY)
        _wait(dh)
        assert dh.features("AAA") is not None
        tomorrow = TODAY + timedelta(days=1)
        assert dh.ensure(kite, ["AAA"], tomorrow) == 1, (
            "yesterday's panel must be gone, so AAA is queued afresh")
        _wait(dh)
        assert dh.stats()["day"] == tomorrow


def test_the_switch_off_does_nothing_and_touches_no_api():
    from intraday.daily_history import DailyHistory
    kite = _FakeKite(["AAA"])
    with cfg_ctx({"daily_history_enabled": "false"}):
        dh = DailyHistory(sleep=lambda s: None)
        assert dh.ensure(kite, ["AAA"], TODAY) == 0
        assert kite.hist_calls == [] and dh.features("AAA") is None
    with cfg_ctx({}):
        assert DailyHistory().ensure(None, ["AAA"], TODAY) == 0, "no Kite session -> no-op"


TESTS = [
    ("a forming bar dated today never reaches the indicators",
     test_a_forming_bar_dated_today_never_reaches_the_indicators),
    ("bars are sorted, de-duplicated, bad rows skipped",
     test_to_daily_bars_is_sorted_deduped_and_skips_bad_rows),
    ("a clean history is ok and carries the panel",
     test_a_clean_history_is_ok_and_carries_the_panel),
    ("an unadjusted split makes the symbol unreliable",
     test_an_unadjusted_split_makes_the_symbol_unreliable),
    ("a raw-close mismatch is refused and a match is not",
     test_a_raw_close_mismatch_is_refused_and_a_match_is_not),
    ("short history is flagged, not defaulted",
     test_short_history_is_flagged_not_defaulted),
    ("fetch retries a rate limit and raises anything else",
     test_fetch_retries_a_rate_limit_and_raises_anything_else),
    ("worker loads panels from completed sessions only",
     test_worker_loads_panels_from_completed_sessions_only),
    ("failures yield no panel and are retried later, not hammered",
     test_failures_yield_no_panel_and_are_retried_later_not_hammered),
    ("a new trading day discards yesterday's panels",
     test_a_new_trading_day_discards_yesterdays_panels),
    ("the switch off does nothing and touches no API",
     test_the_switch_off_does_nothing_and_touches_no_api),
]
