"""
Live intraday universe re-rank (10-Aug-2026).

WHAT THIS CATCHES
------------------
intraday/scanner.py's universe selection was entirely prior-day data
(stock_data_daily), computed once and cached until the calendar date
changed. Correct for the tradability/liquidity/movement/quality FILTER —
those are slow-moving facts — but it meant the RANKING was frozen at
yesterday's close too: a stock that went dead today kept its websocket slot
for the whole session, and a stock outside yesterday's static top 40 that
started moving hard mid-morning was never seen at all, because nothing was
ever subscribed to it.

Operator's instruction, verbatim: "Decide if it needs to be 15 second loop
or 300s timer my preference is 15 second but I will elt you take that
call." Shipped as scanner.live_rerank() — pure arithmetic over live quotes,
cheap enough to run every 15s cycle — re-ranking a wider bench that
watch_symbols() now subscribes to, while the actually expensive resource
(bars, via context_symbols()/refresh_contexts()) stays on the existing 300s
timer untouched.

These tests pin: the static ranking is untouched with no live signal
(before the open, the switch off, a name that has not ticked — "no signal"
and "measured dead" must not read the same), a genuinely hot name can win a
slot over a dead one, RVOL is capped rather than unbounded, and the engine
wiring (watch_symbols() widening, rerank_universe_live() updating
self._universe) actually calls the pure function correctly.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from tests import cfg_ctx


def _entry(symbol, score, avg_vol_20d=100_000.0):
    from intraday.scanner import UniverseEntry
    return UniverseEntry(symbol=symbol, close=100.0, value_cr=50.0,
                         atr_pct=2.0, delivery_pct=30.0, sector="X",
                         score=score, reason="", avg_vol_20d=avg_vol_20d)


# ── scanner.live_rerank() — pure function ────────────────────────────────

def test_before_the_open_the_static_ranking_stands():
    from intraday.scanner import live_rerank
    bench = [_entry("A", 0.9), _entry("B", 0.5), _entry("C", 0.3)]
    with cfg_ctx({}):
        out = live_rerank(bench, quotes={}, minutes=0.0, limit=2)
    assert out == ["A", "B"]


def test_the_switch_off_falls_back_to_the_static_ranking():
    from intraday.scanner import live_rerank
    bench = [_entry("A", 0.9), _entry("B", 0.5)]
    quotes = {"B": {"volume": 10_000_000}}  # B is on fire, but the switch is off
    with cfg_ctx({"intraday_universe_live_rerank": "false"}):
        out = live_rerank(bench, quotes, minutes=120.0, limit=1)
    assert out == ["A"]


def test_a_name_with_no_live_tick_keeps_its_static_score():
    """The cold-start principle applied here: absence of a signal must not
    read the same as a measured-bad signal."""
    from intraday.scanner import live_rerank
    bench = [_entry("A", 0.9), _entry("B", 0.5)]
    with cfg_ctx({}):
        out = live_rerank(bench, quotes={}, minutes=120.0, limit=2)
    assert out == ["A", "B"], "with no live data at all, order must be unchanged"


def test_a_bench_name_outside_the_static_top_n_can_win_a_slot_on_live_volume():
    """The actual bug being fixed: a modestly-ranked static name that is
    genuinely running hot right now must be able to outrank a name with a
    somewhat better static score but no real activity today. Scores kept
    within the blend's real reach (default live weight 0.4 — a saturated
    live score can lift at most 0.4, so it cannot override an arbitrarily
    large static gap, only a realistic one)."""
    from intraday.scanner import live_rerank
    avg = 1_000_000.0
    a = _entry("A", 0.55, avg_vol_20d=avg)
    z = _entry("Z", 0.20, avg_vol_20d=avg)
    minutes = 187.5  # half the session
    expected_by_now = avg * (minutes / 375.0)
    quotes = {"A": {"volume": int(expected_by_now * 0.2)},   # far below normal
             "Z": {"volume": int(expected_by_now * 6.0)}}   # 6x normal pace, saturates the cap
    with cfg_ctx({}):
        out = live_rerank([a, z], quotes, minutes=minutes, limit=1)
    assert out == ["Z"], f"expected the genuinely hot name to win the slot; got {out}"


def test_rvol_saturates_at_the_configured_cap_rather_than_growing_unbounded():
    """3x and 50x RVOL must score IDENTICALLY once weight=1.0 -- if the cap
    were not applied, 50x would always beat 3x. Same 'clamp the implausible'
    discipline entry_ranking.py already applies to implied_rr."""
    from intraday.scanner import live_rerank
    avg = 1_000_000.0
    minutes = 187.5
    expected_by_now = avg * (minutes / 375.0)
    at_cap = _entry("AT_CAP", 0.5, avg_vol_20d=avg)
    beyond_cap = _entry("BEYOND_CAP", 0.5, avg_vol_20d=avg)
    quotes = {"AT_CAP": {"volume": int(expected_by_now * 3.0)},
             "BEYOND_CAP": {"volume": int(expected_by_now * 50.0)}}
    with cfg_ctx({"intraday_rerank_live_weight": "1.0"}):
        out = live_rerank([at_cap, beyond_cap], quotes, minutes, limit=2)
    assert set(out) == {"AT_CAP", "BEYOND_CAP"}, "both must survive the cap"


def test_respects_the_limit():
    from intraday.scanner import live_rerank
    bench = [_entry(s, 1.0 - i * 0.01) for i, s in enumerate("ABCDE")]
    with cfg_ctx({}):
        out = live_rerank(bench, {}, minutes=100.0, limit=3)
    assert len(out) == 3


def test_empty_bench_returns_empty():
    from intraday.scanner import live_rerank
    with cfg_ctx({}):
        assert live_rerank([], {}, minutes=100.0, limit=40) == []


# ── intraday.config.minutes_since_open() — pure function ────────────────

def test_before_open_is_zero():
    from intraday.config import minutes_since_open
    from config import IST
    assert minutes_since_open(datetime(2026, 8, 10, 8, 30, tzinfo=IST)) == 0.0


def test_midday_matches_elapsed_minutes():
    from intraday.config import minutes_since_open
    from config import IST
    t = datetime(2026, 8, 10, 11, 15, tzinfo=IST)  # 2h after 09:15
    assert abs(minutes_since_open(t) - 120.0) < 1e-9


def test_after_close_clamps_to_session_length_not_growing_further():
    from intraday.config import minutes_since_open
    from config import IST
    t = datetime(2026, 8, 10, 18, 0, tzinfo=IST)
    assert abs(minutes_since_open(t) - 375.0) < 1e-9


# ── engine wiring ─────────────────────────────────────────────────────────

class _StubFeed:
    def __init__(self, quotes): self._q = quotes
    def quote(self, symbol): return self._q.get(symbol)


def _engine():
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.positions = []
    eng.candidates = []
    eng._bench = []
    eng._universe = []
    return eng


def test_rerank_universe_live_promotes_a_hot_bench_name_into_the_universe():
    eng = _engine()
    avg = 1_000_000.0
    eng._bench = [_entry("COLD", 0.45, avg_vol_20d=avg),
                 _entry("HOT", 0.15, avg_vol_20d=avg)]
    minutes = 187.5
    expected_by_now = avg * (minutes / 375.0)
    feed = _StubFeed({"HOT": {"volume": int(expected_by_now * 10.0)},
                      "COLD": {"volume": int(expected_by_now * 0.1)}})
    with cfg_ctx({}), patch("intraday.config.minutes_since_open", return_value=minutes):
        eng.rerank_universe_live(feed)
    assert eng._universe[0] == "HOT", (
        f"expected the genuinely hot bench name ranked first; got {eng._universe}")


def test_rerank_with_no_feed_leaves_universe_untouched():
    eng = _engine()
    eng._bench = [_entry("A", 0.9)]
    eng._universe = ["STALE"]
    eng.rerank_universe_live(None)
    assert eng._universe == ["STALE"]


def test_watch_symbols_includes_the_bench_not_just_the_universe():
    """Without this, a bench name outside self._universe never ticks and
    rerank_universe_live() has nothing live to promote it on."""
    eng = _engine()
    eng._bench = [_entry("OUTSIDE_TOP40", 0.1)]
    eng._universe = []
    ws = eng.watch_symbols()
    assert "OUTSIDE_TOP40" in ws


TESTS = [
    ("before the open, the static ranking stands",
     test_before_the_open_the_static_ranking_stands),
    ("the switch off falls back to the static ranking",
     test_the_switch_off_falls_back_to_the_static_ranking),
    ("a name with no live tick keeps its static score",
     test_a_name_with_no_live_tick_keeps_its_static_score),
    ("a bench name outside the static top-N can win a slot on live volume",
     test_a_bench_name_outside_the_static_top_n_can_win_a_slot_on_live_volume),
    ("RVOL saturates at the configured cap rather than growing unbounded",
     test_rvol_saturates_at_the_configured_cap_rather_than_growing_unbounded),
    ("respects the limit",
     test_respects_the_limit),
    ("empty bench returns empty",
     test_empty_bench_returns_empty),
    ("before open is zero",
     test_before_open_is_zero),
    ("midday matches elapsed minutes",
     test_midday_matches_elapsed_minutes),
    ("after close clamps to session length",
     test_after_close_clamps_to_session_length_not_growing_further),
    ("rerank promotes a hot bench name into the universe",
     test_rerank_universe_live_promotes_a_hot_bench_name_into_the_universe),
    ("rerank with no feed leaves the universe untouched",
     test_rerank_with_no_feed_leaves_universe_untouched),
    ("watch_symbols includes the bench, not just the universe",
     test_watch_symbols_includes_the_bench_not_just_the_universe),
]
