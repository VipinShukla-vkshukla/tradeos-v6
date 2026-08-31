"""
Egress fix, 31-Aug-2026 — third finding in the same allocator/egress
scenario sweep, after the hurdle-population cache
(test_hurdle_population_cache.py).

Same root shape, different function: `order_manager._today_totals()` —
the daily entry-count/notional check `act_on_candidates()`,
`_maybe_enter_swing()` and `_log_swing_state()` all consult — issued its
own intraday_broker_log fetch (unfiltered by framework; the function
filters client-side) EVERY TIME any of those three call sites ran, which
in `act_on_candidates()`'s case is once per candidate walked, not once
per cycle. Measured live: 21,214 of one session's 187,801 total API
requests (11.3%) were this exact query, repeated dozens of times within
a single 15-second cycle for a number that only changes when THIS
process places a new order.

Unlike the hurdle population (allocation/hurdle.py, cached on the 300s
slow timer because it is a large, slow-moving statistic), this number
gates a hard daily cap and needs to stay tied to the decision cycle
itself — cached for exactly one `cycle()` invocation (~15s,
eval_interval_s()), never longer. `IntradayEngine._today_totals_cache`
is reset to `{}` at the top of every `cycle()` call;
`_today_totals_cached()` fills it on first use per framework within that
cycle and reuses it for every subsequent call in the same cycle.

Projected: ~21,214 requests/day for this query down to roughly one per
cycle (~1,500/day) — about a 93% cut in this category.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENGINE_PATH = (Path(__file__).parent.parent / "intraday" / "engine.py")


class _CountingDB:
    """Counts real fetches so a test can assert the cache is actually
    doing something, not just returning a plausible-looking number."""
    def __init__(self, rows):
        self._rows = rows
        self.fetch_count = 0

    def table(self, name):
        assert name == "intraday_broker_log", (
            f"_today_totals() queries a different table now ({name!r}) — "
            f"update this fixture")
        return self

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self

    def execute(self):
        self.fetch_count += 1
        class R:
            pass
        r = R()
        r.data = self._rows
        return r


def _engine(sb):
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=sb)


_SAMPLE_ROWS = [
    {"price": 100.0, "quantity": 10, "action": "PLACED", "framework": "SWING", "side": "BUY"},
    {"price": 200.0, "quantity": 5, "action": "PLACED", "framework": "SWING", "side": "BUY"},
    {"price": 50.0, "quantity": 20, "action": "PLACED", "framework": "INTRADAY", "side": "BUY"},
]


def test_repeated_calls_within_one_cycle_hit_the_database_once():
    """The actual fix: N calls to _today_totals_cached() for the same
    framework in one cycle must issue exactly ONE real fetch, not N."""
    db = _CountingDB(_SAMPLE_ROWS)
    eng = _engine(db)

    for _ in range(10):
        n, mine, all_ = eng._today_totals_cached("SWING")

    assert db.fetch_count == 1, (
        f"expected exactly 1 fetch across 10 calls in one cycle, got "
        f"{db.fetch_count} — the cache is not being consulted")
    assert n == 2, f"expected 2 SWING BUY/PLACED rows, got {n}"


def test_different_frameworks_within_one_cycle_each_fetch_once():
    """SWING and INTRADAY are cached independently — asking for both in
    the same cycle costs 2 fetches, not 1 and not N."""
    db = _CountingDB(_SAMPLE_ROWS)
    eng = _engine(db)

    for _ in range(5):
        eng._today_totals_cached("SWING")
    for _ in range(5):
        eng._today_totals_cached("INTRADAY")

    assert db.fetch_count == 2, (
        f"expected exactly 1 fetch per distinct framework (2 total), got "
        f"{db.fetch_count}")


def test_resetting_the_cache_forces_a_fresh_fetch():
    """Simulates cycle() -> cycle() -> cycle(): each new cycle must see
    today's ACTUAL current count, not a stale one from minutes ago — this
    is what makes the cache safe for something that gates a hard cap."""
    db = _CountingDB(_SAMPLE_ROWS)
    eng = _engine(db)

    eng._today_totals_cached("SWING")
    eng._today_totals_cached("SWING")
    assert db.fetch_count == 1, "cache must hold within one cycle"

    # A new order landed between cycles — the DB would now report 3, not 2.
    db._rows = _SAMPLE_ROWS + [
        {"price": 300.0, "quantity": 1, "action": "PLACED", "framework": "SWING", "side": "BUY"}]
    eng._today_totals_cache = {}  # what cycle() does at its own top

    n, _, _ = eng._today_totals_cached("SWING")
    assert db.fetch_count == 2, "resetting the cache must force exactly one fresh fetch"
    assert n == 3, f"expected the fresh fetch to see the new order (n=3), got {n}"


def test_result_matches_calling_today_totals_directly():
    """The cache must not change the ANSWER, only how often it is asked —
    same contract as the hurdle-population cache's equivalent test."""
    from execution.order_manager import _today_totals
    db_direct = _CountingDB(_SAMPLE_ROWS)
    db_cached = _CountingDB(_SAMPLE_ROWS)
    eng = _engine(db_cached)

    direct = _today_totals(db_direct, "SWING")
    cached = eng._today_totals_cached("SWING")
    assert direct == cached, (
        f"cached path returned {cached}, direct call returned {direct} — "
        f"the cache is returning a different answer, not just a faster one")


def test_cycle_clears_the_cache_at_its_own_top():
    """Source-inspection regression pin: cycle() cannot be invoked
    directly in a unit test (needs a live Kite session, order placement,
    the allocator — same reason no other method on this class has a
    direct test; see test_pending_fill_race.py's own docstring). Pins
    that the reset is the FIRST thing cycle() does, before any of the
    work whose results _today_totals_cached() feeds."""
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    # cycle() is the last method in the file today, so the closing anchor
    # is "the next top-level def OR end of string", not just the former.
    m = re.search(r"    def cycle\(.*?(?:\n    def |\Z)", src, re.DOTALL)
    assert m, "could not isolate cycle()'s body"
    body = m.group(0)
    reset_pos = body.find("self._today_totals_cache = {}")
    assert reset_pos != -1, (
        "cycle() no longer resets _today_totals_cache — cached counts "
        "would leak across cycles and could let entries exceed the daily cap")
    # Must precede evaluate_candidates()/act_on_candidates() — the reset
    # is worthless if something already read the (previous cycle's) cache
    # before it is cleared.
    first_use_pos = body.find("evaluate_candidates(")
    assert first_use_pos == -1 or reset_pos < first_use_pos, (
        "the cache reset must run before evaluate_candidates()/"
        "act_on_candidates(), not after")


TESTS = [
    ("repeated calls within one cycle hit the database once",
     test_repeated_calls_within_one_cycle_hit_the_database_once),
    ("different frameworks within one cycle each fetch once",
     test_different_frameworks_within_one_cycle_each_fetch_once),
    ("resetting the cache forces a fresh fetch",
     test_resetting_the_cache_forces_a_fresh_fetch),
    ("cached result matches calling _today_totals() directly",
     test_result_matches_calling_today_totals_directly),
    ("cycle() clears the cache at its own top, before first use",
     test_cycle_clears_the_cache_at_its_own_top),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
