"""
IGN event-study collector — the parts that decide WHICH Kite requests are made and how a
multi-day response is cut back into per-day cache entries (tools/replay/ign_event_data.py).
Getting either wrong would silently store one day's bars under another day's name, or leave a
needed day permanently uncollected while the run reports success.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from config import IST
from tools.replay import ign_event_data as D


def _iso(n):
    return (date(2026, 1, 1) + timedelta(days=n)).isoformat()


def test_windows_never_exceed_the_kite_span_and_cover_every_day():
    days = [_iso(n) for n in (0, 3, 20, 57, 58, 59, 100, 101, 250)]
    w = D.plan_windows(days)
    assert sorted(d for win in w for d in win) == sorted(days), "every needed day is in exactly one window"
    for win in w:
        span = (date.fromisoformat(win[-1]) - date.fromisoformat(win[0])).days
        assert span <= D.MAX_SPAN_DAYS, (win, span)
    assert w[0] == [_iso(0), _iso(3), _iso(20), _iso(57), _iso(58)], "58 days inclusive fits one request"
    assert w[1][0] == _iso(59), "day 59 starts a new window"


def test_windows_ignore_order_and_duplicates():
    assert D.plan_windows([_iso(5), _iso(1), _iso(5)]) == [[_iso(1), _iso(5)]]
    assert D.plan_windows([]) == []


def test_the_span_limit_is_below_the_documented_kite_maximum():
    assert D.MAX_SPAN_DAYS < 60, "Kite rejects a minute request longer than 60 days"


def _raw(day: str, hh: int, mm: int, px: float):
    y, m, d = map(int, day.split("-"))
    return {"date": IST.localize(datetime(y, m, d, hh, mm)), "open": px, "high": px + 1,
            "low": px - 1, "close": px + 0.5, "volume": 100}


def test_split_puts_each_bar_under_its_own_day_and_drops_unwanted_days():
    raw = [_raw("2026-03-02", 9, 15, 100), _raw("2026-03-02", 9, 16, 101),
           _raw("2026-03-03", 9, 15, 200), _raw("2026-03-04", 9, 15, 300)]
    out = D.split_by_day(raw, {"2026-03-02", "2026-03-04"})
    assert set(out) == {"2026-03-02", "2026-03-04"}, "an unwanted day must not be stored"
    assert [b.open for b in out["2026-03-02"]] == [100, 101]
    assert out["2026-03-04"][0].open == 300 and out["2026-03-04"][0].close == 300.5


def test_split_matches_the_shape_barsource_stores():
    from tools.replay.bars import Bar
    b = D.split_by_day([_raw("2026-03-02", 9, 15, 100)], {"2026-03-02"})["2026-03-02"][0]
    assert isinstance(b, Bar) and b.ts.tzinfo is not None
    assert (b.open, b.high, b.low, b.close, b.volume) == (100.0, 101.0, 99.0, 100.5, 100.0)


def test_a_wanted_day_with_no_bars_is_absent_not_empty():
    out = D.split_by_day([_raw("2026-03-02", 9, 15, 100)], {"2026-03-02", "2026-03-05"})
    assert "2026-03-05" not in out, "a holiday must not be stored as an empty cached day"


def test_missing_reports_only_uncached_days(monkeypatch=None):
    import tempfile
    from pathlib import Path
    from tools.replay import bars as B
    with tempfile.TemporaryDirectory() as tmp:
        old = B.CACHE_DIR
        B.CACHE_DIR = Path(tmp)
        try:
            B._store("ZZTEST", "2026-03-02", "minute", D.split_by_day(
                [_raw("2026-03-02", 9, 15, 100)], {"2026-03-02"})["2026-03-02"])
            assert D._missing("ZZTEST", ["2026-03-02", "2026-03-03"]) == ["2026-03-03"]
        finally:
            B.CACHE_DIR = old


def test_the_limiter_spaces_request_starts():
    import time
    lim = D._Limiter(0.05)
    t0 = time.monotonic()
    for _ in range(4):
        lim.wait()
    assert time.monotonic() - t0 >= 0.15 - 0.01, "4 waits at 50 ms are >= 3 gaps"


TESTS = [
    ("windows respect the Kite span and cover every day",
     test_windows_never_exceed_the_kite_span_and_cover_every_day),
    ("windows ignore order and duplicates", test_windows_ignore_order_and_duplicates),
    ("the span limit is below Kite's maximum", test_the_span_limit_is_below_the_documented_kite_maximum),
    ("split puts each bar under its own day", test_split_puts_each_bar_under_its_own_day_and_drops_unwanted_days),
    ("split matches the shape BarSource stores", test_split_matches_the_shape_barsource_stores),
    ("a wanted day with no bars is absent, not empty", test_a_wanted_day_with_no_bars_is_absent_not_empty),
    ("missing reports only uncached days", test_missing_reports_only_uncached_days),
    ("the limiter spaces request starts", test_the_limiter_spaces_request_starts),
]
