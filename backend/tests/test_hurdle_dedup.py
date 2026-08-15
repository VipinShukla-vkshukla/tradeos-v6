"""
The hurdle's arrival-population dedup — 07-Aug-2026, OFF by default for
BOTH books, decided per-framework via `alloc_hurdle_dedup_swing` /
`alloc_hurdle_dedup_intraday`.

WHY THIS EXISTS
-----------------
`_empirical_base()` builds its percentile population from every row in
`allocation_decisions` — one per (proposal, 15-second cycle), no dedup by
symbol or day. Confirmed live, SWING, 07-Aug-2026: ten different symbols
each logged 548-600 rows in a single session — a candidate that sits near
its entry zone for hours contributes hundreds of near-identical `edge`
values to the very population its own bar is compared against.

Operator's explicit requirement, same day: prove intraday's bar is
UNAFFECTED unless intraday's own switch is turned on — "keep intraday as it
is currently in main [while] we work on swing only." This file's job is
that isolation guarantee, plus the dedup mechanics themselves.
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
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
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
    """Ignores filters entirely, same as test_hurdle_percentile.py's fake —
    this exercises the dedup mechanism, not the query's WHERE clauses."""
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows)


def _row(symbol, trade_date, edge):
    return {"symbol": symbol, "edge": edge, "framework": "SWING",
            "regime_bucket": "STRONG", "trade_date": trade_date}


def _heavily_repeated_population():
    """One symbol polled 600 times (mirrors TVSMOTOR's 600-row day, 07-Aug)
    at a HIGH edge, plus 40 other symbols polled once each at a LOW edge —
    the shape that would let repetition alone drag the raw percentile up
    toward the repeated symbol's own value."""
    rows = [_row("REPEATED", "2026-08-07", 0.10) for _ in range(600)]
    rows += [_row(f"SYM{i}", "2026-08-07", 0.01) for i in range(40)]
    return rows


def test_dedup_off_by_default_leaves_the_raw_population_untouched():
    from allocation.hurdle import _empirical_base
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        sb = _FakeSB(_heavily_repeated_population())
        base, meta, edges = _empirical_base("STRONG", "SWING", sb)
        assert meta["deduplicated"] is False
        assert len(edges) == 640, (
            f"default (switch unset) must use every raw row, got {len(edges)}")


def test_dedup_on_collapses_repeats_to_one_observation_per_symbol_day():
    from allocation.hurdle import _empirical_base
    with cfg_ctx({"alloc_hurdle_min_sample": "10",
                  "alloc_hurdle_dedup_swing": "true"}):
        sb = _FakeSB(_heavily_repeated_population())
        base, meta, edges = _empirical_base("STRONG", "SWING", sb)
        assert meta["deduplicated"] is True
        assert meta["raw_n"] == 640, f"raw_n must still record the true row count, got {meta['raw_n']}"
        assert len(edges) == 41, (
            f"expected 41 distinct (symbol, day) pairs (1 repeated + 40 singles), "
            f"got {len(edges)} — repetition is not being collapsed")


def test_dedup_moves_the_percentile_away_from_the_repeated_symbol():
    """
    The actual point of the fix: under raw pooling, 600 copies of 0.10 push
    the 75th percentile to 0.10 even though 40 of 41 distinct opportunities
    that day were 0.01. Deduplicated, one symbol cannot out-vote the rest.
    """
    from allocation.hurdle import _empirical_base
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        sb = _FakeSB(_heavily_repeated_population())
        raw_base, _, _ = _empirical_base("STRONG", "SWING", sb)
    with cfg_ctx({"alloc_hurdle_min_sample": "10",
                  "alloc_hurdle_dedup_swing": "true"}):
        sb = _FakeSB(_heavily_repeated_population())
        dedup_base, _, _ = _empirical_base("STRONG", "SWING", sb)
    assert raw_base > dedup_base, (
        f"raw p75 ({raw_base}) should sit near the repeated symbol's own edge "
        f"(0.10) while dedup p75 ({dedup_base}) should sit near the other 40 "
        f"symbols' edge (0.01) — if they're equal, repetition isn't actually "
        f"being neutralised")


def test_swing_dedup_switch_does_not_touch_intraday():
    """
    The operator's explicit requirement: turning dedup on for SWING must
    leave INTRADAY's population exactly as it is in main today.
    """
    from allocation.hurdle import _empirical_base
    rows = [_row("X", "2026-08-07", 0.10) for _ in range(600)]
    rows += [_row(f"Y{i}", "2026-08-07", 0.01) for i in range(40)]
    for r in rows:
        r["framework"] = "INTRADAY"

    with cfg_ctx({"alloc_hurdle_min_sample": "10",
                  "alloc_hurdle_dedup_swing": "true"}):
        sb = _FakeSB(rows)
        base, meta, edges = _empirical_base("STRONG", "INTRADAY", sb)
        assert meta["deduplicated"] is False, (
            "alloc_hurdle_dedup_swing must not affect INTRADAY's own "
            "deduplication state")
        assert len(edges) == 640, (
            f"INTRADAY's population must be untouched by the SWING-only "
            f"switch, got {len(edges)} instead of the full 640 raw rows")


def test_intraday_dedup_switch_does_not_touch_swing():
    """Same isolation, the other direction."""
    from allocation.hurdle import _empirical_base
    with cfg_ctx({"alloc_hurdle_min_sample": "10",
                  "alloc_hurdle_dedup_intraday": "true"}):
        sb = _FakeSB(_heavily_repeated_population())   # framework=SWING
        base, meta, edges = _empirical_base("STRONG", "SWING", sb)
        assert meta["deduplicated"] is False, (
            "alloc_hurdle_dedup_intraday must not affect SWING's own "
            "deduplication state")


TESTS = [
    ("dedup off by default leaves the raw population untouched",
     test_dedup_off_by_default_leaves_the_raw_population_untouched),
    ("dedup on collapses repeats to one observation per symbol-day",
     test_dedup_on_collapses_repeats_to_one_observation_per_symbol_day),
    ("dedup moves the percentile away from the repeated symbol",
     test_dedup_moves_the_percentile_away_from_the_repeated_symbol),
    ("swing dedup switch does not touch intraday",
     test_swing_dedup_switch_does_not_touch_intraday),
    ("intraday dedup switch does not touch swing",
     test_intraday_dedup_switch_does_not_touch_swing),
]
