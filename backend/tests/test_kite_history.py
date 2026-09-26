"""
Kite history downloader — tools/kite_history/.

Everything runs against a FAKE Kite that enforces the constraints the real one does: at most 60 days per
minute request, a token that can die mid-run, rate-limit errors, instruments that are listed on a date and
have nothing before it. What is being tested is the part that can silently go wrong on a multi-hour unattended
run: that the history is complete and exact, that a crash or an expired token loses nothing already written,
that a rerun does not refetch or duplicate, and that the run stays out of the market's way.
"""

from __future__ import annotations

import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from kiteconnect.exceptions import GeneralException, InputException, NetworkException, TokenException

from tools.kite_history import download as D
from tools.kite_history import state, store, universe, verify
from tools.kite_history.universe import Target

IST = ZoneInfo("Asia/Kolkata")
SAT = datetime(2026, 9, 26, 13, 0, tzinfo=IST)           # a Saturday: the market is closed


# ── the fake broker ─────────────────────────────────────────────────────────

def _candle_rows(token: int, day: date, interval: str, with_oi: bool, factor: float = 1.0) -> list[dict]:
    """Deterministic candles for one trading day, so any stored value can be recomputed independently. `factor` is a
    retroactive corporate-action adjustment: prices scaled by it, volume by its inverse."""
    if interval == "day":
        base = 100.0 + token % 7 + day.toordinal() * 0.001
        r = {"date": datetime(day.year, day.month, day.day, tzinfo=IST), "open": round(base * factor, 2),
             "high": round((base + 1) * factor, 2), "low": round((base - 1) * factor, 2),
             "close": round((base + 0.5) * factor, 2), "volume": int((1000 + token) / factor)}
        if with_oi:
            r["oi"] = 5000 + token
        return [r]
    rows = []
    for i in range(375):
        o = 100.0 + token % 7 + day.toordinal() * 0.001 + i * 0.01
        r = {"date": datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=i),
             "open": round(o * factor, 2), "high": round((o + 0.1) * factor, 2), "low": round((o - 0.1) * factor, 2),
             "close": round((o + 0.05) * factor, 2), "volume": int((100 + i) / factor)}
        if with_oi:
            r["oi"] = 5000 + i
        rows.append(r)
    return rows


class FakeKite:
    LIMIT = {"minute": 60, "day": 2000}

    def __init__(self, series: dict, data_end: date = date(2026, 9, 25), token_dies_after: int | None = None,
                 faults: dict | None = None, always_fail: dict | None = None, refuse: dict | None = None,
                 adjustments: dict | None = None):
        self.series, self.data_end = series, data_end
        self.adjustments = dict(adjustments or {})      # token -> (ex-date, factor): every day BEFORE the ex-date is rescaled
        self.token_dies_after, self.faults = token_dies_after, dict(faults or {})
        self.always_fail, self.refuse = dict(always_fail or {}), dict(refuse or {})
        self.calls: list[tuple] = []
        self._lock = threading.Lock()

    def historical_data(self, token, from_date, to_date, interval, continuous=False, oi=False):
        with self._lock:
            n = len(self.calls)
            self.calls.append((token, from_date, to_date, interval, bool(oi)))
        if self.token_dies_after is not None and n >= self.token_dies_after:
            raise TokenException("Incorrect `api_key` or `access_token`.")
        if n in self.faults:
            raise self.faults[n]
        if token in self.always_fail:
            raise self.always_fail[token]
        if token in self.refuse:
            raise InputException(self.refuse[token])
        if (to_date - from_date).days + 1 > self.LIMIT[interval]:
            raise InputException(f"interval exceeds max limit: {self.LIMIT[interval]} days")
        first, last = self.series[token]
        adj = self.adjustments.get(token)
        out = []
        d = from_date
        while d <= to_date:
            if d.weekday() < 5 and first <= d <= (last or self.data_end) and d <= self.data_end:
                out += _candle_rows(token, d, interval, oi, adj[1] if adj and d < adj[0] else 1.0)
            d += timedelta(days=1)
        return out


def _weekdays(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


class _Clock:
    """Time that only moves when something sleeps, so pacing and pauses are testable without waiting."""

    def __init__(self, start: datetime = SAT):
        self.t = 0.0
        self.start = start

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.start + timedelta(seconds=self.t)


def _target(sym="AAA", token=1, segment="NSE", oi=False):
    return Target(segment, sym, token, sym, "NSE", "EQ", oi)


def _run(kite, root, targets, interval="minute", **kw):
    clk = kw.pop("clock", None) or _Clock()
    kw.setdefault("workers", 1)
    kw.setdefault("min_interval", 0.0)
    kw.setdefault("heartbeat_s", 0)
    return D.run(kite, root, targets, interval, now_fn=clk.now, sleep=clk.sleep, clock=clk.monotonic,
                 log=lambda m: None, **kw)


# ── store ───────────────────────────────────────────────────────────────────

def _frame(days, token=1, with_oi=False, interval="minute"):
    rows = []
    for d in days:
        rows += _candle_rows(token, d, interval, with_oi)
    return store.frame_from_candles(rows, with_oi)


def test_a_write_reads_back_exactly():
    with tempfile.TemporaryDirectory() as tmp:
        df = _frame(_weekdays(date(2026, 9, 21), date(2026, 9, 25)))
        store.write_frame(Path(tmp), "minute", "NSE", "AAA", df)
        back = store.read(Path(tmp), "minute", "NSE", "AAA")
        assert len(back) == 5 * 375 and back["ts"].dt.tz is not None and str(back["ts"].dt.tz) == "Asia/Kolkata"
        assert back["ts"].iloc[0] == pd.Timestamp("2026-09-21 09:15", tz="Asia/Kolkata")
        assert back["ts"].iloc[-1] == pd.Timestamp("2026-09-25 15:29", tz="Asia/Kolkata")
        for c in ("open", "high", "low", "close"):
            assert (back[c].to_numpy() == df[c].to_numpy()).all(), f"{c} must round-trip bit for bit"
        assert back["volume"].dtype == "int64" and (back["volume"].to_numpy() == df["volume"].to_numpy()).all()
        assert "oi" not in back.columns


def test_minute_history_is_one_file_per_year_and_reads_across_them():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        df = _frame([date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 2)])
        paths = store.write_frame(root, "minute", "NSE", "AAA", df)
        assert sorted(p.name for p in paths) == ["2025.parquet", "2026.parquet"]
        back = store.read(root, "minute", "NSE", "AAA")
        assert len(back) == 4 * 375 and back["ts"].is_monotonic_increasing
        cut = store.read(root, "minute", "NSE", "AAA", start="2025-12-31", end=date(2026, 1, 1))
        assert cut["ts"].dt.date.nunique() == 2 and cut["ts"].iloc[0].date() == date(2025, 12, 31) and cut["ts"].iloc[-1].date() == date(2026, 1, 1), (
            "a date as `end` includes the whole day")
        early = store.read(root, "minute", "NSE", "AAA", end=datetime(2025, 12, 30, 9, 30, tzinfo=IST))
        assert len(early) == 16, "a datetime as `end` is a cut-off, inclusive"


def test_merging_removes_duplicates_and_the_newer_row_wins():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = _frame([date(2026, 9, 24), date(2026, 9, 25)])
        store.write_frame(root, "minute", "NSE", "AAA", a)
        b = _frame([date(2026, 9, 25), date(2026, 9, 28)])
        b.loc[b["ts"].dt.date == date(2026, 9, 25), "close"] = 999.0
        store.write_frame(root, "minute", "NSE", "AAA", b)
        back = store.read(root, "minute", "NSE", "AAA")
        assert len(back) == 3 * 375 and back["ts"].is_unique and back["ts"].is_monotonic_increasing
        assert (back.loc[back["ts"].dt.date == date(2026, 9, 25), "close"] == 999.0).all()
        assert (back.loc[back["ts"].dt.date == date(2026, 9, 24), "close"] != 999.0).all()


def test_a_failed_write_leaves_the_previous_file_intact():
    import pyarrow.parquet as pq
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store.write_frame(root, "minute", "NSE", "AAA", _frame([date(2026, 9, 24)]))
        before = store.read(root, "minute", "NSE", "AAA")
        real = pq.write_table

        def boom(*a, **k):
            real(*a, **k)
            raise OSError("disk full")
        pq.write_table = boom
        try:
            try:
                store.write_frame(root, "minute", "NSE", "AAA", _frame([date(2026, 9, 25)]))
            except OSError:
                pass
            else:
                raise AssertionError("the failure must surface")
        finally:
            pq.write_table = real
        after = store.read(root, "minute", "NSE", "AAA")
        assert len(after) == len(before) == 375 and (after["close"].to_numpy() == before["close"].to_numpy()).all()


def test_folder_names_are_safe_on_windows_and_still_recognisable():
    assert store.safe_name("M&M") == "M&M" and store.safe_name("NIFTY 50") == "NIFTY 50"
    assert store.safe_name("A/B:C*D?") == "A_B_C_D_"
    assert store.safe_name("CON") == "_CON" and store.safe_name("com3") == "_com3"
    assert store.safe_name("TRAILING.") == "TRAILING"
    try:
        store.safe_name("  ")
    except ValueError:
        pass
    else:
        raise AssertionError("an empty name must be refused")


def test_open_interest_is_stored_for_derivatives_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store.write_frame(root, "minute", "NFO-FUT", "XFUT", _frame([date(2026, 9, 25)], with_oi=True), with_oi=True)
        fut = store.read(root, "minute", "NFO-FUT", "XFUT")
        assert "oi" in fut.columns and fut["oi"].dtype == "int64" and fut["oi"].iloc[3] == 5003
        store.write_frame(root, "minute", "NSE", "AAA", _frame([date(2026, 9, 25)]))
        assert "oi" not in store.read(root, "minute", "NSE", "AAA").columns


def test_daily_history_is_a_single_file_and_a_minute_path_needs_a_year():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = store.write_frame(root, "day", "NSE", "AAA", _frame(_weekdays(date(2025, 12, 1), date(2026, 2, 27)), interval="day"))
        assert [p.name for p in paths] == ["AAA.parquet"]
        assert store.list_symbols(root, "day", "NSE") == ["AAA"]
        try:
            store.path_for(root, "minute", "NSE", "AAA")
        except ValueError:
            pass
        else:
            raise AssertionError("a minute path without a year is ambiguous")


# ── planning ────────────────────────────────────────────────────────────────

def test_backward_windows_tile_the_range_newest_first_within_the_span():
    end, floor = date(2026, 9, 25), date(2025, 1, 10)
    w = list(D.backward_windows(end, 58, floor))
    assert w[0][1] == end and w[-1][0] == floor
    for (s, e) in w:
        assert (e - s).days + 1 <= 58
    for (s1, e1), (s2, e2) in zip(w, w[1:]):
        assert s1 - e2 == timedelta(days=1), "consecutive windows touch with no gap and no overlap"
    assert sum((e - s).days + 1 for s, e in w) == (end - floor).days + 1


def test_forward_windows_tile_the_range():
    w = list(D.forward_windows(date(2026, 1, 1), date(2026, 6, 30), 58))
    assert w[0][0] == date(2026, 1, 1) and w[-1][1] == date(2026, 6, 30)
    assert sum((e - s).days + 1 for s, e in w) == 181 and all((e - s).days + 1 <= 58 for s, e in w)


def test_the_last_completed_session():
    f = lambda y, m, d, h, mi: D.last_completed_session(datetime(y, m, d, h, mi, tzinfo=IST))
    assert f(2026, 9, 26, 13, 0) == date(2026, 9, 25), "Saturday -> Friday"
    assert f(2026, 9, 27, 9, 0) == date(2026, 9, 25), "Sunday -> Friday"
    assert f(2026, 9, 28, 10, 0) == date(2026, 9, 25), "Monday before the close -> Friday"
    assert f(2026, 9, 29, 16, 0) == date(2026, 9, 29), "Tuesday after the close settles -> today"
    assert f(2026, 9, 29, 15, 44) == date(2026, 9, 28), "a minute before it settles -> yesterday"


def test_the_quiet_window_is_weekday_market_hours_only():
    q = lambda d, h, m: D.in_quiet_window(datetime(2026, 9, d, h, m, tzinfo=IST))
    assert not q(28, 8, 49) and q(28, 8, 50) and q(28, 15, 44) and not q(28, 15, 45)
    assert not q(26, 11, 0) and not q(27, 11, 0), "weekends are free"
    assert D.in_quiet_window(datetime(2026, 9, 28, 4, 30, tzinfo=ZoneInfo("UTC"))), "04:30 UTC is 10:00 IST"


def test_errors_are_classified_by_type_then_message():
    c = D.classify_error
    assert c(TokenException("x")) == "token" and c(GeneralException("Incorrect `api_key` or `access_token`.")) == "token"
    assert c(InputException("interval exceeds max limit")) == "input"
    assert c(NetworkException("Too many requests")) == "retry" and c(TimeoutError("timed out")) == "retry"
    assert c(GeneralException("boom")) == "retry"


def test_the_limiter_spaces_request_starts():
    clk = _Clock()
    lim = D.Limiter(0.4, clock=clk.monotonic, sleep=clk.sleep)
    starts = []
    for _ in range(5):
        lim.wait()
        starts.append(clk.t)
    assert all(abs((b - a) - 0.4) < 1e-9 for a, b in zip(starts, starts[1:]))


# ── the target list ─────────────────────────────────────────────────────────

def _rows():
    def r(seg, sym, tok, exch="NSE", typ="EQ"):
        return {"segment": seg, "tradingsymbol": sym, "instrument_token": tok, "exchange": exch, "instrument_type": typ, "name": sym}
    return [r("NSE", "RELIANCE", 1), r("NSE", "TCS", 2), r("NSE", "GSEC-GS", 3), r("NSE", "NIFTYBEESINAV", 4),
            r("NSE", "SMALLCAP-SM", 5), r("INDICES", "NIFTY 50", 6, typ="EQ"), r("INDICES", "SENSEX", 7, exch="BSE"),
            r("NFO-FUT", "RELIANCE26SEPFUT", 8, "NFO", "FUT"), r("NFO-OPT", "RELIANCE26SEP1300CE", 9, "NFO", "CE"),
            r("NSE", "RELIANCE", 1)]


def test_the_default_targets_are_mainboard_equities_all_indices_and_futures():
    t = universe.build_targets(_rows())
    got = {(x.segment, x.symbol) for x in t}
    assert got == {("INDICES", "NIFTY 50"), ("INDICES", "SENSEX"), ("NSE", "RELIANCE"), ("NSE", "TCS"), ("NFO-FUT", "RELIANCE26SEPFUT")}
    assert len(t) == 5, "a duplicate row is one target; indices on every exchange are included"
    assert [x.with_oi for x in t if x.segment == "NFO-FUT"] == [True] and not any(x.with_oi for x in t if x.segment != "NFO-FUT")
    assert t[0].segment == "INDICES" and t[-1].segment == "NFO-FUT", "indices first, futures last"


def test_targets_are_ordered_by_turnover_and_can_be_narrowed():
    t = universe.build_targets(_rows(), turnover={"TCS": 900.0, "RELIANCE": 100.0})
    nse = [x.symbol for x in t if x.segment == "NSE"]
    assert nse == ["TCS", "RELIANCE"], "the most liquid first, so an interrupted run has the important names"
    assert [x.symbol for x in universe.build_targets(_rows(), only=["tcs"])] == ["TCS"]
    assert len(universe.build_targets(_rows(), limit=2)) == 2
    allnse = universe.build_targets(_rows(), segments=("NSE-ALL",))
    assert {"GSEC-GS", "SMALLCAP-SM", "NIFTYBEESINAV"} <= {x.symbol for x in allnse}


# ── the download, end to end ────────────────────────────────────────────────

FIRST = date(2025, 3, 10)


def test_a_backfill_walks_back_to_the_listing_date_and_stops_there():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kite = FakeKite({1: (FIRST, None)})
        res = _run(kite, root, [_target()])
        assert res.counts == {"done": 1} and not res.token_expired
        back = store.read(root, "minute", "NSE", "AAA")
        expected = _weekdays(FIRST, date(2026, 9, 25))
        assert back["ts"].dt.date.nunique() == len(expected) and len(back) == 375 * len(expected), "every trading day, every minute"
        assert back["ts"].iloc[0] == pd.Timestamp("2025-03-10 09:15", tz="Asia/Kolkata")
        assert back["ts"].iloc[-1] == pd.Timestamp("2026-09-25 15:29", tz="Asia/Kolkata")
        empty_tail = [c for c in kite.calls if c[2] < FIRST]
        assert len(empty_tail) == 2, f"exactly two empty windows past the start, then it stops: {len(empty_tail)}"
        assert all((c[2] - c[1]).days + 1 <= 58 for c in kite.calls), "no request longer than the window limit"


def test_stored_values_are_exactly_what_the_broker_sent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _run(FakeKite({1: (date(2026, 9, 21), None)}), root, [_target()])
        back = store.read(root, "minute", "NSE", "AAA")
        want = store.frame_from_candles(sum((_candle_rows(1, d, "minute", False) for d in _weekdays(date(2026, 9, 21), date(2026, 9, 25))), []))
        for c in ("ts", "open", "high", "low", "close", "volume"):
            assert (back[c].to_numpy() == want[c].to_numpy()).all(), c


def test_an_instrument_with_no_history_is_marked_empty_after_the_leading_gap():
    with tempfile.TemporaryDirectory() as tmp:
        kite = FakeKite({1: (date(2027, 1, 1), None)})
        res = _run(kite, Path(tmp), [_target()])
        assert res.counts == {"empty": 1} and store.files_for(Path(tmp), "minute", "NSE", "AAA") == []
        assert len(kite.calls) == 7, f"about a year of empty windows before giving up: {len(kite.calls)}"


def test_a_rerun_skips_what_is_finished_and_refetches_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kite = FakeKite({1: (FIRST, None), 2: (FIRST, None)})
        _run(kite, root, [_target("AAA", 1), _target("BBB", 2)])
        n = len(kite.calls)
        res = _run(kite, root, [_target("AAA", 1), _target("BBB", 2)])
        assert len(kite.calls) == n and res.skipped_already_done == 2 and res.counts == {}
        redo = _run(kite, root, [_target("AAA", 1)], redo=True)
        assert len(kite.calls) > n and redo.counts == {"done": 1}
        assert len(store.read(root, "minute", "NSE", "AAA")) == 375 * len(_weekdays(FIRST, date(2026, 9, 25))), "no duplicates after a redo"


def test_an_expired_token_stops_the_run_and_keeps_everything_already_written():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (FIRST, None), 2: (FIRST, None), 3: (FIRST, None)}
        dying = FakeKite(series, token_dies_after=12 + 5)           # symbol 1 takes 12 requests, then symbol 2 dies part-way
        targets = [_target("AAA", 1), _target("BBB", 2), _target("CCC", 3)]
        res = _run(dying, root, targets)
        assert res.token_expired and res.counts == {"done": 1}
        prog = state.Progress(root / "_state" / "progress.db")
        assert prog.status_of("minute", "NSE", "AAA") == state.DONE and prog.status_of("minute", "NSE", "BBB") is None
        assert store.files_for(root, "minute", "NSE", "BBB") == [], "a half-walked symbol writes nothing"
        assert len(store.read(root, "minute", "NSE", "AAA")) == 375 * len(_weekdays(FIRST, date(2026, 9, 25)))
        healthy = FakeKite(series)
        res2 = _run(healthy, root, targets)
        assert res2.counts == {"done": 2} and res2.skipped_already_done == 1 and not res2.token_expired
        assert all(c[0] != 1 for c in healthy.calls), "the finished symbol was not refetched"
        prog.close()


def test_transient_failures_are_retried_with_a_backoff_and_lose_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        clk = _Clock()
        kite = FakeKite({1: (FIRST, None)}, faults={0: NetworkException("Too many requests"), 1: NetworkException("Too many requests"),
                                                    5: GeneralException("upstream hiccup")})
        res = _run(kite, Path(tmp), [_target()], clock=clk)
        assert res.counts == {"done": 1}
        assert len(store.read(Path(tmp), "minute", "NSE", "AAA")) == 375 * len(_weekdays(FIRST, date(2026, 9, 25)))
        assert clk.t >= 2.0 + 4.0 + 2.0, f"it backed off between attempts (2s, 4s, 2s): {clk.t}"


def test_a_window_that_keeps_failing_marks_the_symbol_error_and_the_next_run_retries_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad = FakeKite({1: (FIRST, None)}, always_fail={1: NetworkException("Too many requests")})
        res = _run(bad, root, [_target()])
        assert res.counts == {"error": 1} and len(bad.calls) == D.MAX_ATTEMPTS
        assert store.files_for(root, "minute", "NSE", "AAA") == []
        res2 = _run(FakeKite({1: (FIRST, None)}), root, [_target()])
        assert res2.counts == {"done": 1}


def test_a_refused_instrument_is_skipped_once_and_not_retried_forever():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kite = FakeKite({1: (FIRST, None)}, refuse={1: "no such instrument"})
        res = _run(kite, root, [_target()])
        assert res.counts == {"skipped": 1} and len(kite.calls) == 1
        prog = state.Progress(root / "_state" / "progress.db")
        assert "no such instrument" in prog.get("minute", "NSE", "AAA")["note"]
        prog.close()
        n = len(kite.calls)
        _run(kite, root, [_target()])
        assert len(kite.calls) == n, "a skipped instrument is not asked for again on the next run"


def test_the_run_pauses_through_market_hours_and_can_be_told_not_to():
    with tempfile.TemporaryDirectory() as tmp:
        clk = _Clock(datetime(2026, 9, 28, 15, 40, tzinfo=IST))          # Monday, five minutes before the quiet window ends
        seen = []

        class Spy(FakeKite):
            def historical_data(self, *a, **k):
                seen.append(clk.now())
                return super().historical_data(*a, **k)
        res = _run(Spy({1: (date(2026, 9, 21), None)}), Path(tmp), [_target()], clock=clk)
        assert res.counts == {"done": 1}
        assert min(seen) >= datetime(2026, 9, 28, 15, 45, tzinfo=IST), f"nothing was requested during market hours: {min(seen)}"
    with tempfile.TemporaryDirectory() as tmp:
        clk = _Clock(datetime(2026, 9, 28, 11, 0, tzinfo=IST))
        seen.clear()
        _run(Spy({1: (date(2026, 9, 21), None)}), Path(tmp), [_target()], clock=clk, allow_market_hours=True)
        assert min(seen) < datetime(2026, 9, 28, 11, 5, tzinfo=IST), "with the override it runs straight away"


def test_an_update_appends_only_what_is_new_and_duplicates_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (date(2026, 8, 3), None)}
        clk = _Clock(datetime(2026, 9, 19, 13, 0, tzinfo=IST))                 # Saturday: last session Fri 18 Sep
        D.run(FakeKite(series, data_end=date(2026, 9, 18)), root, [_target()], "minute", workers=1, min_interval=0.0,
              now_fn=clk.now, sleep=clk.sleep, clock=clk.monotonic, log=lambda m: None)
        first = store.read(root, "minute", "NSE", "AAA")
        assert first["ts"].iloc[-1].date() == date(2026, 9, 18)
        later = FakeKite(series, data_end=date(2026, 9, 25))
        clk2 = _Clock(datetime(2026, 9, 26, 13, 0, tzinfo=IST))
        res = D.run(later, root, [_target()], "minute", mode="update", workers=1, min_interval=0.0, now_fn=clk2.now,
                    sleep=clk2.sleep, clock=clk2.monotonic, log=lambda m: None)
        assert res.counts == {"done": 1} and len(later.calls) == 1, f"one small window, not a full walk: {len(later.calls)}"
        assert res.adjusted == 0, "consistent overlap: nothing was re-downloaded"
        back = store.read(root, "minute", "NSE", "AAA")
        assert back["ts"].is_unique and back["ts"].iloc[-1].date() == date(2026, 9, 25)
        assert len(back) == 375 * len(_weekdays(date(2026, 8, 3), date(2026, 9, 25)))
        assert (back.iloc[: len(first)]["close"].to_numpy() == first["close"].to_numpy()).all(), "history is untouched"


def test_an_update_of_an_unknown_symbol_does_a_full_backfill():
    with tempfile.TemporaryDirectory() as tmp:
        res = _run(FakeKite({1: (FIRST, None)}), Path(tmp), [_target()], mode="update")
        assert res.counts == {"done": 1} and len(store.read(Path(tmp), "minute", "NSE", "AAA")) > 100_000


def test_open_interest_is_requested_for_futures_and_only_for_futures():
    with tempfile.TemporaryDirectory() as tmp:
        kite = FakeKite({1: (date(2026, 9, 1), None), 2: (date(2026, 9, 1), None)})
        _run(kite, Path(tmp), [_target("AAA", 1), _target("AFUT", 2, "NFO-FUT", oi=True)])
        assert {c[0]: c[4] for c in kite.calls} == {1: False, 2: True}
        assert "oi" in store.read(Path(tmp), "minute", "NFO-FUT", "AFUT").columns


def test_daily_history_uses_long_windows_and_the_daily_layout():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        kite = FakeKite({1: (date(2010, 6, 1), None)})
        res = _run(kite, root, [_target()], interval="day")
        assert res.counts == {"done": 1}
        back = store.read(root, "day", "NSE", "AAA")
        assert len(back) == len(_weekdays(date(2010, 6, 1), date(2026, 9, 25))) and len(kite.calls) <= 6
        assert store.files_for(root, "day", "NSE", "AAA")[0].name == "AAA.parquet"


def test_several_workers_store_exactly_what_one_worker_does():
    series = {i: (FIRST, None) for i in range(1, 7)}
    targets = [_target(f"S{i}", i) for i in range(1, 7)]
    out = {}
    for w in (1, 3):
        with tempfile.TemporaryDirectory() as tmp:
            D.run(FakeKite(series), Path(tmp), targets, "minute", workers=w, min_interval=0.0, sleep=lambda s: None,
                  now_fn=lambda: SAT, log=lambda m: None)
            out[w] = {t.symbol: store.read(Path(tmp), "minute", "NSE", t.symbol)["close"].sum() for t in targets}
    assert out[1] == out[3] and len(out[1]) == 6


def test_the_progress_summary_counts_rows_and_requests():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _run(FakeKite({1: (date(2026, 9, 1), None), 2: (date(2027, 1, 1), None)}), root, [_target("AAA", 1), _target("BBB", 2)])
        prog = state.Progress(root / "_state" / "progress.db")
        s = prog.summary("minute")
        assert s[("minute", "NSE", "done")]["symbols"] == 1 and s[("minute", "NSE", "done")]["rows"] == 375 * len(_weekdays(date(2026, 9, 1), date(2026, 9, 25)))
        assert s[("minute", "NSE", "empty")]["symbols"] == 1
        assert prog.with_status("minute", "empty") == [("NSE", "BBB")]
        prog.close()


# ── the fast path ───────────────────────────────────────────────────────────

def _raw_rows(token, days, interval="minute", with_oi=False):
    """Candles in the exact shape Kite's endpoint returns: [timestamp string, o, h, l, c, volume(, oi)]."""
    rows = []
    for d in days:
        for r in _candle_rows(token, d, interval, with_oi):
            row = [r["date"].strftime("%Y-%m-%dT%H:%M:%S%z"), r["open"], r["high"], r["low"], r["close"], r["volume"]]
            if with_oi:
                row.append(r["oi"])
            rows.append(row)
    return rows


class RawKite:
    """The endpoint pykiteconnect wraps: `_get` returns {"candles": [[...], ...]} unparsed. Backed by FakeKite's series."""

    def __init__(self, fake: FakeKite):
        self.fake, self.gets = fake, []

    def _get(self, route, url_args=None, params=None):
        self.gets.append((route, url_args, params))
        f, t = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
        rows = self.fake.historical_data(url_args["instrument_token"], f, t, url_args["interval"], False, bool(params["oi"]))
        first = None
        out = []
        for r in rows:
            row = [r["date"].strftime("%Y-%m-%dT%H:%M:%S%z"), r["open"], r["high"], r["low"], r["close"], r["volume"]]
            if "oi" in r:
                row.append(r["oi"])
            out.append(row)
        return {"candles": out}

    def instruments(self, exchange=None):
        return ["passthrough"]


def test_raw_rows_parse_to_exactly_what_the_dict_path_gives():
    days = _weekdays(date(2026, 9, 21), date(2026, 9, 25))
    for with_oi in (False, True):
        fast = store.frame_from_rows(_raw_rows(1, days, "minute", with_oi), with_oi)
        slow = store.frame_from_candles(sum((_candle_rows(1, d, "minute", with_oi) for d in days), []), with_oi)
        assert fast.equals(slow), f"with_oi={with_oi}"
        assert str(fast["ts"].dt.tz) == "Asia/Kolkata" and fast["ts"].iloc[0] == pd.Timestamp("2026-09-21 09:15", tz="Asia/Kolkata")
    no_oi = store.frame_from_rows(_raw_rows(1, days[:1], "minute", True), False)
    assert "oi" not in no_oi.columns, "open interest is dropped when it was not asked for"
    want_oi = store.frame_from_rows(_raw_rows(1, days[:1], "minute", False), True)
    assert (want_oi["oi"] == 0).all() and want_oi["oi"].dtype == "int64", "asked for but not returned: zeros, not a crash"
    empty = store.frame_from_rows([], False)
    assert empty.empty and list(empty.columns) == ["ts", "open", "high", "low", "close", "volume"]
    d = store.frame_from_rows(_raw_rows(1, days[:2], "day"), False)
    assert len(d) == 2 and d["ts"].iloc[0] == pd.Timestamp("2026-09-21 00:00", tz="Asia/Kolkata")


def test_raw_rows_are_parsed_in_bulk_not_one_candle_at_a_time():
    import time
    days = _weekdays(date(2025, 1, 1), date(2026, 9, 25))                 # ~470 days = 176k rows
    rows = _raw_rows(1, days)
    t0 = time.time()
    df = store.frame_from_rows(rows, False)
    took = time.time() - t0
    assert len(df) == len(rows) and took < 4.0, (
        f"{len(rows):,} rows took {took:.1f}s; a per-candle dateutil parse is ~15 s and caps the download at 1 request/s")


def test_the_fast_client_asks_for_the_raw_endpoint_and_passes_everything_else_through():
    fake = FakeKite({1: (date(2026, 9, 1), None)})
    raw = RawKite(fake)
    fk = D.FastKite(raw)
    df = fk.fetch_frame(1, date(2026, 9, 21), date(2026, 9, 25), "minute", True)
    route, url_args, params = raw.gets[0]
    assert route == "market.historical" and url_args == {"instrument_token": 1, "interval": "minute"}
    assert params == {"from": "2026-09-21", "to": "2026-09-25", "interval": "minute", "continuous": 0, "oi": 1}
    assert len(df) == 5 * 375 and "oi" in df.columns
    assert fk.instruments("NSE") == ["passthrough"], "unrelated calls reach the real client"
    fk.fetch_frame(1, date(2026, 9, 21), date(2026, 9, 25), "minute", False)
    assert raw.gets[1][2]["oi"] == 0


def test_a_backfill_through_the_fast_client_stores_exactly_what_the_dict_path_stores():
    stored = {}
    for name, make in (("dict", lambda f: f), ("fast", lambda f: D.FastKite(RawKite(f)))):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = FakeKite({1: (FIRST, None), 2: (FIRST, None)})
            _run(make(fake), root, [_target("AAA", 1), _target("FFF", 2, "NFO-FUT", oi=True)])
            stored[name] = {s: store.read(root, "minute", seg, s) for seg, s in (("NSE", "AAA"), ("NFO-FUT", "FFF"))}
    for sym in ("AAA", "FFF"):
        assert stored["dict"][sym].equals(stored["fast"][sym]), sym
    assert len(stored["fast"]["AAA"]) == 375 * len(_weekdays(FIRST, date(2026, 9, 25))) and "oi" in stored["fast"]["FFF"].columns


def test_the_heartbeat_reports_rate_and_retries():
    lines = []
    fake = FakeKite({1: (date(2026, 9, 21), None)}, faults={0: NetworkException("Too many requests")})
    with tempfile.TemporaryDirectory() as tmp:
        clk = _Clock()
        D.run(fake, Path(tmp), [_target()], "minute", workers=1, min_interval=0.0, now_fn=clk.now, sleep=clk.sleep,
              clock=clk.monotonic, log=lines.append, heartbeat_s=0.01)
        import time as _t
        _t.sleep(0.05)
    r, retries, lat, errs = D.STATS.snapshot()
    assert retries >= 1 and errs.get("NetworkException", 0) >= 1 and r >= 3


# ── Kite re-adjusts its history ─────────────────────────────────────────────

SAT_19 = datetime(2026, 9, 19, 13, 0, tzinfo=IST)


def _full(kite, token, start, end, interval="minute"):
    """What a from-scratch download of [start, end] would give, in Kite's request windows."""
    frames = [store.frame_from_candles(kite.historical_data(token, s, e, interval, False, False)) for s, e in D.forward_windows(start, end, 58)]
    return store.combine([f for f in frames if not f.empty])


def _same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    return (a["ts"].to_list() == b["ts"].to_list()
            and all((a[c].to_numpy() == b[c].to_numpy()).all() for c in ("open", "high", "low", "close", "volume")))


def _update_at(kite, root, now, targets=None):
    clk = _Clock(now)
    return D.run(kite, root, targets or [_target()], "minute", mode="update", workers=1, min_interval=0.0, now_fn=clk.now,
                 sleep=clk.sleep, clock=clk.monotonic, log=lambda m: None, heartbeat_s=0)


def _backfill_at(kite, root, now, targets=None):
    clk = _Clock(now)
    return D.run(kite, root, targets or [_target()], "minute", workers=1, min_interval=0.0, now_fn=clk.now,
                 sleep=clk.sleep, clock=clk.monotonic, log=lambda m: None, heartbeat_s=0)


def test_the_overlap_check_flags_a_rescaling_and_ignores_noise():
    days = _weekdays(date(2026, 9, 21), date(2026, 9, 25))
    old = _frame(days)

    def scaled(f):
        n = old.copy()
        for c in ("open", "high", "low", "close"):
            n[c] = n[c] * f
        return n
    n, fac = D.overlap_check(old, scaled(0.5))
    assert n == len(old) and abs(fac - 0.5) < 1e-9, "a 1:1 bonus halves every earlier price"
    assert D.overlap_check(old, scaled(1.0005))[1] is None, "0.05% is inside the tolerance"
    assert abs(D.overlap_check(old, scaled(1.01))[1] - 1.01) < 1e-9, "a 1% rescale (a small rights adjustment) is caught"
    odd = old.copy()
    odd.loc[:10, "close"] *= 2
    assert D.overlap_check(old, odd)[1] is None, "a handful of odd bars is not a re-adjustment"
    assert D.overlap_check(old, _frame([date(2026, 9, 28)])) == (0, None), "no shared minutes: nothing to compare"
    assert D.overlap_check(old.iloc[0:0], old) == (0, None) and D.overlap_check(old, old.iloc[0:0]) == (0, None)
    zero = old.copy()
    zero["close"] = 0.0
    assert D.overlap_check(zero, old) == (0, None), "a zero old close is skipped, not divided by"


def test_replace_frame_overwrites_and_removes_stale_years():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store.write_frame(root, "minute", "NSE", "AAA", _frame([date(2025, 12, 30), date(2026, 1, 2)]))
        assert [p.name for p in store.files_for(root, "minute", "NSE", "AAA")] == ["2025.parquet", "2026.parquet"]
        new = _frame([date(2026, 1, 2)], token=2)
        paths = store.replace_frame(root, "minute", "NSE", "AAA", new)
        assert [p.name for p in paths] == ["2026.parquet"]
        assert [p.name for p in store.files_for(root, "minute", "NSE", "AAA")] == ["2026.parquet"], "the year it no longer covers is gone"
        assert _same(store.read(root, "minute", "NSE", "AAA"), new), "replaced, not merged with the old 2026 rows"
        try:
            store.replace_frame(root, "minute", "NSE", "AAA", new.iloc[0:0])
        except ValueError:
            assert len(store.read(root, "minute", "NSE", "AAA")) == 375, "and a refused replace leaves the data alone"
        else:
            raise AssertionError("replacing a history with nothing must be refused")


def test_a_retroactive_adjustment_replaces_the_whole_history_with_no_mixed_scales():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (FIRST, None)}
        _backfill_at(FakeKite(series, data_end=date(2026, 9, 18)), root, SAT_19)
        bogus = store.frame_from_candles([{"date": datetime(2025, 6, 1, 9, 15, tzinfo=IST), "open": 1.0, "high": 1.0, "low": 1.0,
                                           "close": 1.0, "volume": 1}])          # a candle Kite has since dropped (a Sunday)
        store.write_frame(root, "minute", "NSE", "AAA", bogus)
        before = store.read(root, "minute", "NSE", "AAA")
        assert len(before) == 375 * len(_weekdays(FIRST, date(2026, 9, 18))) + 1
        later = FakeKite(series, data_end=date(2026, 9, 25), adjustments={1: (date(2026, 9, 22), 0.5)})   # 1:1 bonus, ex-date 22 Sep
        res = _update_at(later, root, SAT)
        assert res.counts == {"done": 1} and res.adjusted == 1 and res.requests > 10, "a full re-walk, not one window"
        after = store.read(root, "minute", "NSE", "AAA")
        want = _full(later, 1, FIRST, date(2026, 9, 25))
        assert _same(after, want), "every stored candle is on the new scale, and the candle Kite dropped is gone: replaced, not merged"
        assert after["ts"].is_unique and len(after) == 375 * len(_weekdays(FIRST, date(2026, 9, 25)))
        assert abs(after["close"].iloc[0] - before["close"].iloc[0] * 0.5) < 0.011
        prog = state.Progress(root / "_state" / "progress.db")
        rec = prog.get("minute", "NSE", "AAA")
        assert rec["status"] == state.DONE and rec["note"].startswith("re-downloaded") and "0.5000" in rec["note"]
        prog.close()
        assert "re-downloaded after Kite re-adjusted" in res.line()
        again = _update_at(later, root, SAT)
        assert again.adjusted == 0 and again.requests == 1, "once replaced, the next update is a normal one-window update"
        assert _same(store.read(root, "minute", "NSE", "AAA"), want)


def test_a_redownload_that_cannot_reach_back_keeps_the_old_history_and_is_retried():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (FIRST, None)}
        _backfill_at(FakeKite(series, data_end=date(2026, 9, 18)), root, SAT_19)
        before = store.read(root, "minute", "NSE", "AAA")
        adj = {1: (date(2026, 9, 22), 0.5)}
        short = FakeKite({1: (FIRST + timedelta(days=120), None)}, data_end=date(2026, 9, 25), adjustments=adj)
        res = _update_at(short, root, SAT)
        assert res.counts == {"error": 1} and res.adjusted == 0
        assert _same(store.read(root, "minute", "NSE", "AAA"), before), "the old files were not touched"
        prog = state.Progress(root / "_state" / "progress.db")
        rec = prog.get("minute", "NSE", "AAA")
        assert rec["status"] == state.ERROR and rec["note"].startswith(D.ADJ_PENDING) and rec["rows"] == len(before)
        prog.close()
        healthy = FakeKite(series, data_end=date(2026, 9, 25), adjustments=adj)
        res2 = _update_at(healthy, root, SAT)
        assert res2.counts == {"done": 1} and res2.adjusted == 1
        assert _same(store.read(root, "minute", "NSE", "AAA"), _full(healthy, 1, FIRST, date(2026, 9, 25)))


def test_a_pending_adjustment_is_finished_by_a_plain_backfill_too():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (FIRST, None)}
        _backfill_at(FakeKite(series, data_end=date(2026, 9, 18)), root, SAT_19)
        adj = {1: (date(2026, 9, 22), 0.5)}
        _update_at(FakeKite({1: (FIRST + timedelta(days=120), None)}, data_end=date(2026, 9, 25), adjustments=adj), root, SAT)
        healthy = FakeKite(series, data_end=date(2026, 9, 25), adjustments=adj)
        res = _backfill_at(healthy, root, SAT)                                   # not `update`
        assert res.adjusted == 1 and res.counts == {"done": 1}
        assert _same(store.read(root, "minute", "NSE", "AAA"), _full(healthy, 1, FIRST, date(2026, 9, 25))), (
            "a backfill must never MERGE into a symbol that is waiting for its re-adjusted history")


def test_a_symbol_whose_only_shared_day_is_its_last_stored_day_is_still_checked():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        series = {1: (date(2026, 9, 18), None)}
        _backfill_at(FakeKite(series, data_end=date(2026, 9, 18)), root, SAT_19)
        later = FakeKite(series, data_end=date(2026, 9, 25), adjustments={1: (date(2026, 9, 22), 0.5)})
        res = _update_at(later, root, SAT)
        assert res.adjusted == 1, "the last stored day is always inside the re-fetched window, so it is always compared"
        assert _same(store.read(root, "minute", "NSE", "AAA"), _full(later, 1, date(2026, 9, 18), date(2026, 9, 25)))


# ── verify ──────────────────────────────────────────────────────────────────

def test_verify_passes_a_clean_series_and_flags_each_defect():
    days = _weekdays(date(2026, 9, 21), date(2026, 9, 25))
    clean = _frame(days)
    cal = set(days)
    r = verify.verify_frame(clean, "minute", cal)
    assert not verify.has_hard_errors(r) and r["days"] == 5 and r["outside_session"] == 0 and r["missing_days"] == 0
    assert r["bars_mode"] == 375 and r["days_off_mode"] == 0
    short = clean[~((clean["ts"].dt.date == date(2026, 9, 22)) & (clean["ts"].dt.time > pd.Timestamp("15:14").time()))]
    rs = verify.verify_frame(short, "minute", cal)
    assert rs["bars_mode"] == 375 and rs["days_off_mode"] == 1, "a day with 360 candles is counted as off the usual 375"

    dup = pd.concat([clean, clean.iloc[:3]], ignore_index=True)
    assert verify.verify_frame(dup)["duplicate_ts"] == 3
    unsorted = clean.iloc[::-1].reset_index(drop=True)
    assert verify.verify_frame(unsorted)["unsorted"] > 0
    bad = clean.copy()
    bad.loc[5, "high"] = bad.loc[5, "low"] - 1
    assert verify.verify_frame(bad)["high_lt_low"] == 1
    bad = clean.copy()
    bad.loc[7, "open"] = bad.loc[7, "high"] + 5
    assert verify.verify_frame(bad)["ohlc_outside_range"] == 1
    bad = clean.copy()
    bad.loc[9, "close"] = 0.0
    assert verify.verify_frame(bad)["nonpositive_price"] >= 1
    bad = clean.copy()
    bad.loc[11, "volume"] = -1
    assert verify.verify_frame(bad)["negative_volume"] == 1

    late = clean.copy()
    late.loc[0, "ts"] = pd.Timestamp("2026-09-21 18:15", tz="Asia/Kolkata")
    assert verify.verify_frame(late.sort_values("ts"))["outside_session"] == 1, "a muhurat-style evening candle is information, not an error"
    assert not verify.has_hard_errors(verify.verify_frame(late.sort_values("ts")))

    jumpy = clean.copy()
    jumpy.loc[jumpy["ts"].dt.date >= date(2026, 9, 24), ["open", "high", "low", "close"]] *= 2.0
    assert verify.verify_frame(jumpy)["big_day_jumps"] == ["2026-09-24"]

    holes = clean[clean["ts"].dt.date != date(2026, 9, 23)]
    assert verify.verify_frame(holes, "minute", cal)["missing_days"] == 1
    assert verify.verify_frame(pd.DataFrame({"ts": pd.to_datetime([]).tz_localize("Asia/Kolkata"), "open": [], "high": [], "low": [],
                                             "close": [], "volume": []}))["rows"] == 0


TESTS = [
    ("a write reads back exactly", test_a_write_reads_back_exactly),
    ("minute history is one file per year and reads across them", test_minute_history_is_one_file_per_year_and_reads_across_them),
    ("merging removes duplicates and the newer row wins", test_merging_removes_duplicates_and_the_newer_row_wins),
    ("a failed write leaves the previous file intact", test_a_failed_write_leaves_the_previous_file_intact),
    ("folder names are safe on Windows and still recognisable", test_folder_names_are_safe_on_windows_and_still_recognisable),
    ("open interest is stored for derivatives only", test_open_interest_is_stored_for_derivatives_only),
    ("daily history is a single file and a minute path needs a year", test_daily_history_is_a_single_file_and_a_minute_path_needs_a_year),
    ("backward windows tile the range newest first within the span", test_backward_windows_tile_the_range_newest_first_within_the_span),
    ("forward windows tile the range", test_forward_windows_tile_the_range),
    ("the last completed session", test_the_last_completed_session),
    ("the quiet window is weekday market hours only", test_the_quiet_window_is_weekday_market_hours_only),
    ("errors are classified by type then message", test_errors_are_classified_by_type_then_message),
    ("the limiter spaces request starts", test_the_limiter_spaces_request_starts),
    ("the default targets are mainboard equities, all indices and futures", test_the_default_targets_are_mainboard_equities_all_indices_and_futures),
    ("targets are ordered by turnover and can be narrowed", test_targets_are_ordered_by_turnover_and_can_be_narrowed),
    ("a backfill walks back to the listing date and stops there", test_a_backfill_walks_back_to_the_listing_date_and_stops_there),
    ("stored values are exactly what the broker sent", test_stored_values_are_exactly_what_the_broker_sent),
    ("an instrument with no history is marked empty after the leading gap", test_an_instrument_with_no_history_is_marked_empty_after_the_leading_gap),
    ("a rerun skips what is finished and refetches nothing", test_a_rerun_skips_what_is_finished_and_refetches_nothing),
    ("an expired token stops the run and keeps everything already written", test_an_expired_token_stops_the_run_and_keeps_everything_already_written),
    ("transient failures are retried with a backoff and lose nothing", test_transient_failures_are_retried_with_a_backoff_and_lose_nothing),
    ("a window that keeps failing marks the symbol error and the next run retries it", test_a_window_that_keeps_failing_marks_the_symbol_error_and_the_next_run_retries_it),
    ("a refused instrument is skipped once and not retried forever", test_a_refused_instrument_is_skipped_once_and_not_retried_forever),
    ("the run pauses through market hours and can be told not to", test_the_run_pauses_through_market_hours_and_can_be_told_not_to),
    ("an update appends only what is new and duplicates nothing", test_an_update_appends_only_what_is_new_and_duplicates_nothing),
    ("an update of an unknown symbol does a full backfill", test_an_update_of_an_unknown_symbol_does_a_full_backfill),
    ("open interest is requested for futures and only for futures", test_open_interest_is_requested_for_futures_and_only_for_futures),
    ("daily history uses long windows and the daily layout", test_daily_history_uses_long_windows_and_the_daily_layout),
    ("several workers store exactly what one worker does", test_several_workers_store_exactly_what_one_worker_does),
    ("the progress summary counts rows and requests", test_the_progress_summary_counts_rows_and_requests),
    ("verify passes a clean series and flags each defect", test_verify_passes_a_clean_series_and_flags_each_defect),
    ("raw rows parse to exactly what the dict path gives", test_raw_rows_parse_to_exactly_what_the_dict_path_gives),
    ("raw rows are parsed in bulk, not one candle at a time", test_raw_rows_are_parsed_in_bulk_not_one_candle_at_a_time),
    ("the fast client asks for the raw endpoint and passes everything else through", test_the_fast_client_asks_for_the_raw_endpoint_and_passes_everything_else_through),
    ("a backfill through the fast client stores exactly what the dict path stores", test_a_backfill_through_the_fast_client_stores_exactly_what_the_dict_path_stores),
    ("the heartbeat reports rate and retries", test_the_heartbeat_reports_rate_and_retries),
    ("the overlap check flags a rescaling and ignores noise", test_the_overlap_check_flags_a_rescaling_and_ignores_noise),
    ("replace_frame overwrites and removes stale years", test_replace_frame_overwrites_and_removes_stale_years),
    ("a retroactive adjustment replaces the whole history with no mixed scales", test_a_retroactive_adjustment_replaces_the_whole_history_with_no_mixed_scales),
    ("a redownload that cannot reach back keeps the old history and is retried", test_a_redownload_that_cannot_reach_back_keeps_the_old_history_and_is_retried),
    ("a pending adjustment is finished by a plain backfill too", test_a_pending_adjustment_is_finished_by_a_plain_backfill_too),
    ("a symbol whose only shared day is its last stored day is still checked", test_a_symbol_whose_only_shared_day_is_its_last_stored_day_is_still_checked),
]
