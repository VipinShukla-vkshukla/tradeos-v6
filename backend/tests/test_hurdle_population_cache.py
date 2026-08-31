"""
Egress fix, 31-Aug-2026 — same allocator scenario sweep as
test_hurdle_minutes_left_framework.py, next finding.

Queried Supabase's own request logs directly rather than guessing at the
cause of a Fri/Mon egress spike the operator flagged. Of 187,801 API
requests in one session, 119,255 (63%) were GET /rest/v1/allocation_
decisions, and 118,977 of those matched hurdle.py::_empirical_base()'s
own paginated arrival-population fetch — the SWING/WEAK bucket alone
re-ran its full ~29-85-page fetch 1,401 times, essentially once per
15-second cycle, because nothing upstream ever cached it.

THE FIX. `hurdle()` gained `cached_population`, an optional pre-fetched
`(base, meta, edges)` tuple — when supplied, `_empirical_base()`'s own
network call never runs. `Allocator.refresh_hurdle_populations()` fetches
all four (framework, bucket) combinations once and stores them, wired
into the SAME 300-second slow timer `refresh_priors()` already uses
(intraday/run.py) rather than the 15-second decision loop. `select()`
reads the cache and passes it through; a cache miss (`None`, the state
before the first refresh has ever run) falls through to hurdle()'s live
fetch exactly as before — additive, not a default-behaviour change.

Projected effect: full re-fetches drop from ~once per 15s cycle to once
per 300s, a ~20x reduction in fetch FREQUENCY. Since page-count-per-fetch
is unchanged, request VOLUME for this category drops by the same ratio —
118,977 requests/day to roughly 5,900, an ~95% cut in the dominant
traffic category (63% of total), projecting to roughly a 55-60% cut in
TOTAL daily API request volume.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self):
        return self

    def order(self, col, *a, **k):
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows):
        self.data = rows

    def execute(self):
        return self


class _FakeSB:
    """Same fixture shape as test_hurdle_percentile.py's own."""
    def __init__(self, edges: list[float], framework: str, bucket: str):
        self._rows = [{"edge": e, "framework": framework, "regime_bucket": bucket,
                       "trade_date": "2026-08-06"} for e in edges]

    def table(self, name):
        return _FakeQuery(self._rows)


class _RaisingDB:
    """Any query is a test failure — proves the cached path never touches
    the network, which is the entire point of this fix."""
    def table(self, *a, **k):
        raise RuntimeError("hurdle() touched the database despite a warm cache")


def _spread_edges(n=60, lo=-0.5, hi=0.5):
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


# ── hurdle(): cached_population bypasses _empirical_base() entirely ────────

def test_hurdle_with_cached_population_never_touches_sb():
    """sb=_RaisingDB() would fail this test on any query at all — reaching
    a real float bar proves _empirical_base()'s fetch never ran."""
    from allocation.hurdle import hurdle
    edges = _spread_edges()
    with cfg_ctx({"alloc_hurdle_min_sample": 30}):
        bar, inputs = hurdle("WEAK", 5, 200, "SWING", sb=_RaisingDB(),
                             max_slots=15, cached_population=(-0.1, {}, sorted(edges)))
    assert isinstance(bar, float), f"expected a real bar, got {bar!r}"


def test_hurdle_cached_population_produces_the_same_bar_as_a_live_fetch():
    """The cache must not just avoid the DB — it must produce the IDENTICAL
    answer a live fetch would have, or callers get a faster wrong number."""
    from allocation.hurdle import hurdle, _empirical_base
    edges = _spread_edges()
    with cfg_ctx({"alloc_hurdle_min_sample": 30}):
        live_sb = _FakeSB(edges, "SWING", "WEAK")
        population = _empirical_base("WEAK", "SWING", live_sb)

        bar_cached, _ = hurdle("WEAK", 5, 200, "SWING", sb=_RaisingDB(),
                               max_slots=15, cached_population=population)
        bar_live, _ = hurdle("WEAK", 5, 200, "SWING", sb=_FakeSB(edges, "SWING", "WEAK"),
                             max_slots=15)
    assert bar_cached == bar_live, (
        f"cached path gave {bar_cached}, live fetch gave {bar_live} — the "
        f"cache is returning a different answer, not just a faster one")


def test_hurdle_without_cached_population_still_fetches_live():
    """Backward compatibility: omitting the parameter (every caller before
    this change) must behave exactly as before — a real fetch against sb."""
    from allocation.hurdle import hurdle
    edges = _spread_edges()
    with cfg_ctx({"alloc_hurdle_min_sample": 30}):
        bar, inputs = hurdle("WEAK", 5, 200, "SWING",
                             sb=_FakeSB(edges, "SWING", "WEAK"), max_slots=15)
    assert isinstance(bar, float) and inputs.get("bucket") == "WEAK"


# ── Allocator.refresh_hurdle_populations() ──────────────────────────────────

def test_refresh_hurdle_populations_warms_all_four_combinations():
    from allocation.allocator import Allocator
    edges = _spread_edges()

    class _MultiFakeSB:
        """Returns the SWING/WEAK population for that combo, cold-start
        (empty) for the other three — enough to prove all four keys exist
        in the cache after refresh, without needing four separate fixtures."""
        def table(self, name):
            return _FakeQuery(list(_FakeSB(edges, "SWING", "WEAK")._rows))

    with cfg_ctx({"alloc_hurdle_min_sample": 30}):
        alloc = Allocator(sb=_MultiFakeSB())
        alloc.refresh_hurdle_populations()

    expected_keys = {("SWING", "STRONG"), ("SWING", "WEAK"),
                     ("INTRADAY", "STRONG"), ("INTRADAY", "WEAK")}
    assert set(alloc._hurdle_populations.keys()) == expected_keys, (
        f"expected all 4 (framework, bucket) combinations warmed, got "
        f"{set(alloc._hurdle_populations.keys())}")


# ── Allocator.select(): the actual egress fix, end to end ──────────────────

def test_select_with_warm_cache_never_touches_the_database_for_hurdle():
    """The money test: a full select() call, with a pre-warmed cache and a
    DB stub that raises on ANY query, must complete successfully. This is
    the exact call shape that ran 1,401 times against the real database in
    one session before this fix."""
    from allocation.proposal import from_swing
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx({"alloc_hurdle_min_sample": 5}):
        p_swing = from_swing(D())
        assert p_swing is not None

        alloc = Allocator(sb=_RaisingDB())
        alloc._priors = {"SWING/CONTINUATION":
                         Prior("SWING/CONTINUATION", 100, 0.30, 0.15, 0.05, -1.0, 2.0),
                         "SWING/ALL": Prior("SWING/ALL", 150, 0.25, 0.12, 0.04, -1.0, 1.9)}
        alloc._hold_days = {"SWING": (5.0, 30)}
        # Pre-warm exactly as refresh_hurdle_populations() would, without
        # needing a working sb to do it (that method is tested separately
        # above) — WEAK is what regime="NEUTRAL" resolves to.
        alloc._hurdle_populations = {("SWING", "WEAK"): (-0.1, {}, _spread_edges())}

        verdicts = alloc.select([p_swing], regime="NEUTRAL",
                                slots_by_framework={"SWING": 3},
                                max_slots_by_framework={"SWING": 15},
                                minutes_left=200)
    assert len(verdicts) == 1 and verdicts[0]["proposal"].symbol == "TCS", (
        "select() with a warm cache must still return a real verdict, not "
        "just avoid crashing")


def test_select_with_a_cold_cache_falls_back_to_a_live_fetch():
    """Before the first refresh ever runs (a fresh Allocator, or a combo
    refresh_hurdle_populations() has not warmed yet), select() must not
    error or silently skip the hurdle — it must fall through to exactly
    today's live-fetch path."""
    from allocation.proposal import from_swing
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    class D:
        symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
        target = 3950.0; qty = 5; rr_live = 3.0
        headline = "x"; stale_price = False

    with cfg_ctx({"alloc_hurdle_min_sample": 5}):
        p_swing = from_swing(D())
        alloc = Allocator(sb=_FakeSB(_spread_edges(), "SWING", "WEAK"))
        alloc._priors = {"SWING/CONTINUATION":
                         Prior("SWING/CONTINUATION", 100, 0.30, 0.15, 0.05, -1.0, 2.0),
                         "SWING/ALL": Prior("SWING/ALL", 150, 0.25, 0.12, 0.04, -1.0, 1.9)}
        alloc._hold_days = {"SWING": (5.0, 30)}
        assert alloc._hurdle_populations == {}, "cache must start empty"

        verdicts = alloc.select([p_swing], regime="NEUTRAL",
                                slots_by_framework={"SWING": 3},
                                max_slots_by_framework={"SWING": 15},
                                minutes_left=200)
    assert len(verdicts) == 1, "a cold cache must still produce a real verdict via live fetch"


TESTS = [
    ("hurdle() with cached_population never touches sb",
     test_hurdle_with_cached_population_never_touches_sb),
    ("cached population produces the same bar as a live fetch",
     test_hurdle_cached_population_produces_the_same_bar_as_a_live_fetch),
    ("hurdle() without cached_population still fetches live (backward compat)",
     test_hurdle_without_cached_population_still_fetches_live),
    ("refresh_hurdle_populations() warms all 4 (framework, bucket) combos",
     test_refresh_hurdle_populations_warms_all_four_combinations),
    ("select() with a warm cache never touches the database for hurdle",
     test_select_with_warm_cache_never_touches_the_database_for_hurdle),
    ("select() with a cold cache falls back to a live fetch",
     test_select_with_a_cold_cache_falls_back_to_a_live_fetch),
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
