"""
IGN daily trend gate and Kite panel wiring. The gate ships DISARMED, so the tests that matter
most are about doing nothing: gate off, a terrible panel changes no trade and is still recorded.
Then the armed arithmetic, the "no opinion" cases that must abstain, the untouched SHORT leg,
and the engine wiring that carries a panel onto a context.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from intraday.session import PRIME
from tests import cfg_ctx
from tests.test_ignition import _TIGHT_LOOKBACK, _long_ctx, _short_ctx

GOOD = {"ok": True, "reason": None, "as_of": "2026-09-23", "n_bars": 259,
        "above_st": True, "adx": 32.0, "di_plus": 28.0, "di_minus": 12.0,
        "sma50_gt_200": True, "above_sma50": True, "dist_sma50": 6.0,
        "rsi14": 61.0, "prev_vol_ratio": 1.4, "ret_1m": 8.0}
BAD = dict(GOOD, above_st=False, adx=11.0, di_minus=31.0, prev_vol_ratio=4.2,
           sma50_gt_200=False)


def _fire(ctx, extra=None):
    from intraday.strategies.ignition import IgnitionMomentum
    with cfg_ctx({**_TIGHT_LOOKBACK, **(extra or {})}):
        return IgnitionMomentum().evaluate(ctx, PRIME)


def _core(s):
    return (s.direction, s.entry, s.stop, s.target, s.confidence, s.rationale)


def test_verdict_no_panel_or_failed_integrity_abstains():
    from intraday.ign_trend import trend_verdict
    on = dict(require_above_st=True, min_adx=20.0)
    assert trend_verdict(None, **on) == ("abstain", 0, 0)
    assert trend_verdict({}, **on) == ("abstain", 0, 0)
    assert trend_verdict(dict(BAD, ok=False), **on) == ("abstain", 0, 0), (
        "a panel that failed its integrity check must not be read at all")


def test_verdict_no_enabled_check_abstains():
    from intraday.ign_trend import trend_verdict
    assert trend_verdict(BAD) == ("abstain", 0, 0), "every check off -> nothing to judge"


def test_verdict_all_must_pass_by_default():
    from intraday.ign_trend import trend_verdict
    on = dict(require_above_st=True, min_adx=20.0, max_di_minus=20.0)
    assert trend_verdict(GOOD, **on) == ("pass", 3, 3)
    assert trend_verdict(dict(GOOD, adx=15.0), **on) == ("refuse", 2, 3), (
        "min_agree 0 means ALL enabled checks; one failure refuses")


def test_verdict_majority_rule():
    from intraday.ign_trend import trend_verdict
    on = dict(require_above_st=True, min_adx=20.0, max_di_minus=20.0, min_agree=2)
    assert trend_verdict(dict(GOOD, adx=15.0), **on)[0] == "pass", "2 of 3 is enough"
    assert trend_verdict(dict(GOOD, adx=15.0, di_minus=25.0), **on) == ("refuse", 1, 3)


def test_verdict_thresholds_are_directional():
    from intraday.ign_trend import trend_verdict
    assert trend_verdict(dict(GOOD, adx=20.0), min_adx=20.0)[0] == "pass", ">= is inclusive"
    assert trend_verdict(dict(GOOD, di_minus=20.0), max_di_minus=20.0)[0] == "pass"
    assert trend_verdict(dict(GOOD, di_minus=20.1), max_di_minus=20.0)[0] == "refuse", (
        "-DI is a HIGHER-is-worse signal: above the cap refuses")
    assert trend_verdict(dict(GOOD, prev_vol_ratio=3.1), max_prev_vol_ratio=3.0)[0] == "refuse"


def test_verdict_a_missing_field_does_not_make_a_majority_impossible():
    from intraday.ign_trend import trend_verdict
    on = dict(require_above_st=True, min_adx=20.0)
    assert trend_verdict(dict(GOOD, adx=None), **on) == ("pass", 1, 1), (
        "only above_st was readable and it passed — the missing ADX is no opinion")
    assert trend_verdict(dict(GOOD, adx=None, above_st=False), **on) == ("refuse", 0, 1)
    assert trend_verdict(dict(GOOD, adx=None, above_st=None), **on) == ("abstain", 0, 0), (
        "nothing readable at all -> abstain, never refuse")


def test_disarmed_gate_changes_no_trade_and_still_records_the_panel():
    plain = _fire(_long_ctx())
    assert plain is not None, "fixture must fire, or this proves nothing"
    bad_ctx = _long_ctx()
    bad_ctx.daily_feats = dict(BAD)
    bad_ctx.delivery_pct_daily = 41.0
    # Thresholds are SET but the master switch is off: an empty gate would pass
    # this trivially, so it is the switch itself being tested, not emptiness.
    s = _fire(bad_ctx, {"ign_trend_require_above_st": "true", "ign_trend_min_adx": "20"})
    assert s is not None, "gate off: a terrible panel must not stop the setup"
    assert _core(s) == _core(plain), "gate off must leave the setup byte-identical"
    t = s.meta["trend"]
    assert t["gate"] == "off" and t["available"] is True, t
    assert t["above_st"] is False and t["adx"] == 11.0 and t["di_minus"] == 31.0, (
        "the panel must be recorded even though nothing acts on it")
    assert t["delivery_pct_daily"] == 41.0, "the named non-Kite exception rides along"
    assert plain.meta["trend"]["available"] is False, "no panel is recorded as unavailable"


def test_armed_gate_refuses_a_bad_panel_and_passes_a_good_one():
    armed = {"ign_trend_gate_enabled": "true", "ign_trend_require_above_st": "true"}
    bad = _long_ctx()
    bad.daily_feats = dict(BAD)
    assert _fire(bad, armed) is None, "close below the SuperTrend line is refused"
    good = _long_ctx()
    good.daily_feats = dict(GOOD)
    s = _fire(good, armed)
    assert s is not None and s.meta["trend"]["gate"] == "pass", s and s.meta["trend"]


def test_armed_gate_with_no_thresholds_refuses_nothing():
    bad = _long_ctx()
    bad.daily_feats = dict(BAD)
    s = _fire(bad, {"ign_trend_gate_enabled": "true"})
    assert s is not None and s.meta["trend"]["gate"] == "abstain", (
        "master switch on but every check off must still be a no-op")


def test_armed_gate_abstains_on_no_panel_and_on_a_failed_panel():
    armed = {"ign_trend_gate_enabled": "true", "ign_trend_require_above_st": "true",
             "ign_trend_min_adx": "20"}
    none_ctx = _long_ctx()
    s1 = _fire(none_ctx, armed)
    assert s1 is not None and s1.meta["trend"]["gate"] == "abstain", (
        "no panel yet (worker still running, fetch failed) is not a bad trend")
    failed = _long_ctx()
    failed.daily_feats = dict(BAD, ok=False, reason="close_jump:2.00")
    s2 = _fire(failed, armed)
    assert s2 is not None and s2.meta["trend"]["gate"] == "abstain"
    assert s2.meta["trend"]["reason"] == "close_jump:2.00", "the WHY is on the record"


def test_short_leg_is_untouched_by_the_armed_gate():
    armed = {"ign_trend_gate_enabled": "true", "ign_trend_require_above_st": "true"}
    sh = _short_ctx()
    sh.daily_feats = dict(BAD)
    plain = _fire(_short_ctx())
    s = _fire(sh, armed)
    assert plain is not None and s is not None, "the short fixture must fire"
    assert s.direction == "SHORT" and _core(s) == _core(plain), (
        "the LONG gate's switches must not touch a SHORT; it has its own gate")
    assert s.meta["trend"]["gate"] == "off", "the short gate is off unless ITS switch is on"
    assert s.meta["trend"]["above_st"] is False, "the panel is still recorded for shorts"


# ── engine wiring ───────────────────────────────────────────────────────────

class _FakeFeed:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, symbol, since=None):
        return self._bars.get(symbol, [])

    def get(self, symbol):
        return None


class _StubHistory:
    def __init__(self, panels):
        self.panels = panels

    def features(self, sym):
        return self.panels.get(sym)

    def bars(self, sym):
        return self.panels.get(sym, {}).get("_bars")


def _mk_engine():
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng._contexts = {}
    eng._daily_ref = {}
    eng._bench = []
    eng._last_bars_log_at = datetime.fromtimestamp(0, IST)
    return eng


def _bar(ts, c):
    from intraday.strategies.base import Bar
    return Bar(ts=ts, open=c, high=c + 1, low=c - 1, close=c, volume=1000.0)


def _entry(symbol):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0, atr_pct=2.0,
                         delivery_pct=30.0, sector="X", score=0.5, reason="",
                         avg_vol_20d=100_000.0)


def test_engine_without_a_history_object_yields_none_not_a_crash():
    eng = _mk_engine()
    assert eng._daily_feats("AAA") is None, "tests build engines via __new__; must degrade"


def test_engine_reads_the_panel_from_the_history_cache():
    eng = _mk_engine()
    eng._daily_history = _StubHistory({"AAA": dict(GOOD)})
    assert eng._daily_feats("AAA")["adx"] == 32.0
    assert eng._daily_feats("ZZZ") is None


def test_a_bench_only_context_is_built_with_the_panel():
    eng = _mk_engine()
    eng._daily_history = _StubHistory({"NEWNAME": dict(GOOD, _bars=["bar"])})
    eng._bench = [_entry("NEWNAME")]
    eng._daily_ref = {"NEWNAME": {"close": 95.0, "high": 96.0, "low": 94.0,
                                  "atr_pct": 2.5, "volume": 200_000.0,
                                  "value_cr": 40.0, "sector": "IT"}}
    base = datetime(2026, 9, 24, 10, 20, tzinfo=IST)
    bars = [_bar(base + timedelta(minutes=i), 100 + i) for i in range(6)]
    with cfg_ctx({"intraday_live_bars_enabled": "true",
                  "intraday_min_live_bars_for_context": "5"}):
        eng.merge_live_bars(_FakeFeed({"NEWNAME": bars}))
    assert eng._contexts["NEWNAME"].daily_feats["above_st"] is True
    assert eng._contexts["NEWNAME"].daily_bars == ["bar"], "the bars ride along for the forming panel"


def test_a_context_built_before_the_worker_finished_picks_the_panel_up_later():
    from intraday.strategies.base import SymbolContext
    eng = _mk_engine()
    base = datetime(2026, 9, 24, 10, 20, tzinfo=IST)
    first = _bar(base, 100)
    eng._contexts["AAA"] = SymbolContext(symbol="AAA", ltp=100.0, bars=[first])
    eng._bench = [_entry("AAA")]
    eng._daily_history = _StubHistory({})
    feed = _FakeFeed({"AAA": [first, _bar(base + timedelta(minutes=1), 101)]})
    with cfg_ctx({"intraday_live_bars_enabled": "true"}):
        eng.merge_live_bars(feed)
        assert eng._contexts["AAA"].daily_feats is None, "worker not done yet"
        eng._daily_history = _StubHistory({"AAA": dict(GOOD, _bars=["bar"])})
        eng.merge_live_bars(feed)
    assert eng._contexts["AAA"].daily_bars == ["bar"], "bars attach late too"
    assert eng._contexts["AAA"].daily_feats["adx"] == 32.0, (
        "the next 15s merge must attach the panel, not wait for the 300s rebuild")


def test_a_failing_history_never_takes_down_context_building():
    from intraday import daily_history
    eng = _mk_engine()

    class Boom:
        def ensure(self, *a, **k):
            raise RuntimeError("kite down")

    eng._daily_history = Boom()
    eng._start_daily_history(object(), ["AAA"], datetime(2026, 9, 24).date())
    real = daily_history.DailyHistory.ensure
    try:
        daily_history.DailyHistory.ensure = lambda *a, **k: (_ for _ in ()).throw(
            ValueError("boom"))
        eng2 = _mk_engine()
        eng2._start_daily_history(object(), ["AAA"], datetime(2026, 9, 24).date())
    finally:
        daily_history.DailyHistory.ensure = real


TESTS = [
    ("verdict: no panel / failed integrity abstains",
     test_verdict_no_panel_or_failed_integrity_abstains),
    ("verdict: no enabled check abstains", test_verdict_no_enabled_check_abstains),
    ("verdict: every enabled check must pass by default",
     test_verdict_all_must_pass_by_default),
    ("verdict: majority rule", test_verdict_majority_rule),
    ("verdict: thresholds are directional and inclusive",
     test_verdict_thresholds_are_directional),
    ("verdict: a missing field does not make a majority impossible",
     test_verdict_a_missing_field_does_not_make_a_majority_impossible),
    ("disarmed gate changes no trade and still records the panel",
     test_disarmed_gate_changes_no_trade_and_still_records_the_panel),
    ("armed gate refuses a bad panel and passes a good one",
     test_armed_gate_refuses_a_bad_panel_and_passes_a_good_one),
    ("armed gate with no thresholds refuses nothing",
     test_armed_gate_with_no_thresholds_refuses_nothing),
    ("armed gate abstains on no panel and on a failed panel",
     test_armed_gate_abstains_on_no_panel_and_on_a_failed_panel),
    ("the SHORT leg is untouched by the armed gate",
     test_short_leg_is_untouched_by_the_armed_gate),
    ("engine without a history object yields None",
     test_engine_without_a_history_object_yields_none_not_a_crash),
    ("engine reads the panel from the history cache",
     test_engine_reads_the_panel_from_the_history_cache),
    ("a bench-only context is built with the panel",
     test_a_bench_only_context_is_built_with_the_panel),
    ("a context built before the worker finished picks the panel up later",
     test_a_context_built_before_the_worker_finished_picks_the_panel_up_later),
    ("a failing history never takes down context building",
     test_a_failing_history_never_takes_down_context_building),
]
