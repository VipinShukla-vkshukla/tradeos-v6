"""
The expectancy ledger must score SHORTS, and must not report "R computable"
as "column present" (14-Aug-2026).

WHAT THIS CATCHES
-----------------
`tools/expectancy_ledger.py` computed its risk denominator as
`(entry - planned_stop_at_entry) * qty`. A short's stop sits ABOVE its entry,
so that product is NEGATIVE, the `risk_amt > 0` guard rejected it, and
`gross_r` / `cost_r` / `net_r` all came back None. Every one of the 8 closed
shorts on record is an SDN row — 100% of the short book — and SDN is the only
bucket in `engine_scorecard` with positive gross R. So the tool the roadmap
names for the Stage 2 friction-versus-signal call was silently long-only.

This is the fifth time in this repository that a direction-aware function was
correct while a caller of it was not; see CLAUDE.md, "a direction-aware
function's correctness proves nothing about its callers".

The second defect rides on the first. The line under the R section printed the
count of rows whose R could be COMPUTED against the words "carry
planned_stop_at_entry", which is COLUMN PRESENCE. The eight shorts have the
column and could not be scored, so the coverage authority under-reported the
Gate 1 number it exists to produce — 56 of 134 (41.8%) against a true 64 of
134 (47.8%), against a 60% gate.

PINNED AGAINST A REAL ROW, NOT A SYNTHETIC ONE
-----------------------------------------------
DEVYANI, 10-Aug-2026, the trade CLAUDE.md's `alloc_edge_absolute_floor`
landmine already names ("cleared it by 0.00009, closed 99 seconds later at
−0.813R"). Its arithmetic is checkable by hand:

    direction SHORT, entry 133.46, stop 134.26, exit 134.11, qty 65
    risk/share = 134.26 - 133.46      = 0.80
    risk       = 0.80 x 65            = 52.00
    gross P&L  = (133.46 - 134.11)x65 = -42.25   (matches realized_pnl)
    gross R    = -42.25 / 52.00       = -0.8125  (matches stored r_multiple)

Under the old formula that denominator is -52.00 and all three R fields are
None. `test_the_old_formula_would_have_dropped_this_row` asserts that
explicitly, so this file states the bug it was written against rather than
only the corrected answer.
"""

from __future__ import annotations

import io

from loguru import logger

from tests import cfg_ctx


# ── a fake PostgREST client ──────────────────────────────────────────────────
# `load()` uses .select().limit().execute() and .select().range().execute();
# `ledger()` additionally reads system_config via .select().eq().execute().
# Both are served here so the whole module stays offline, per tests/__init__.

class _Exec:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _Query:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def limit(self, n): return _Exec(self._rows[:n])
    def range(self, a, b): return _Exec(self._rows[a:b + 1])
    def execute(self): return _Exec(self._rows)


class _SB:
    def __init__(self, closed): self._closed = closed
    def table(self, name):
        return _Query(self._closed if name == "closed_positions" else [])


def _row(symbol, direction, entry, stop, exit_, qty, pnl,
         fw="INTRADAY", product="MIS", engine="SDN"):
    return {"symbol": symbol, "product": product, "framework": fw,
            "mode": "PAPER", "strategy": None, "intraday_strategy": engine,
            "direction": direction, "entry_price": entry, "exit_price": exit_,
            "actual_qty": qty, "planned_stop_at_entry": stop,
            "realized_pnl": pnl, "charges": None, "hold_days": 0,
            "exit_date": "2026-08-10", "max_favorable_excursion": None,
            "signal_id": None, "signal_date": None, "source": "paper"}


# The real DEVYANI row, values as they stand in closed_positions.
DEVYANI = _row("DEVYANI", "SHORT", 133.46, 134.26, 134.11, 65, -42.25)
# A real long from the same book, to prove the long path is untouched.
LONG_ROW = _row("ABC", "LONG", 100.00, 98.00, 104.00, 10, 40.00, engine="ORB")


def _load(rows):
    from tools.expectancy_ledger import load
    with cfg_ctx():                       # cost rates from defaults, no DB
        return load(_SB(rows))


def _captured(fn, level="INFO") -> str:
    sink = io.StringIO()
    hid = logger.add(sink, level=level)
    try:
        fn()
    finally:
        logger.remove(hid)
    return sink.getvalue()


# ── BUG-1 ────────────────────────────────────────────────────────────────────

def test_the_old_formula_would_have_dropped_this_row():
    """State the defect, not just the fix. `entry - stop` is NEGATIVE on a
    correctly-placed short stop, so the old `risk_amt > 0` guard refused it."""
    entry, stop, qty = 133.46, 134.26, 65
    assert (entry - stop) * qty < 0, (
        "the row this file pins is no longer short-shaped — pick another")


def test_devyani_short_is_scored_and_matches_the_hand_computation():
    out = _load([DEVYANI])
    assert len(out) == 1
    t = out[0]
    assert t["gross_r"] is not None, (
        "the short was dropped from every R statistic — BUG-1 has regressed")
    # risk = (134.26 - 133.46) * 65 = 52.00 ; gross R = -42.25 / 52.00
    assert abs(t["risk_amt"] - 52.00) < 1e-6, t["risk_amt"]
    assert abs(t["gross_r"] - (-0.8125)) < 1e-6, t["gross_r"]


def test_devyani_matches_the_r_multiple_the_lifecycle_stored():
    """Two independent computations of one number must agree. The book's own
    `r_multiple` for this row is -0.813, written by position_lifecycle.py:880
    from the same `D.risk_per_share` this tool now imports."""
    t = _load([DEVYANI])[0]
    assert abs(round(t["gross_r"], 3) - (-0.813)) < 1e-9, round(t["gross_r"], 3)


def test_friction_and_net_r_are_consistent_for_the_short():
    """net R is gross R minus friction in R — the identity must hold in both
    directions, or the short book would be scored on a different basis."""
    t = _load([DEVYANI])[0]
    assert t["cost_r"] is not None and t["cost_r"] > 0
    assert abs(t["net_r"] - (t["gross_r"] - t["cost_r"])) < 1e-9
    assert t["net_r"] < t["gross_r"], "friction must make a trade worse, never better"


def test_the_long_path_is_bit_identical():
    """sign=+1 must reproduce the old arithmetic exactly. risk = (100-98)*10
    = 20; gross R = 40/20 = +2.0."""
    t = _load([LONG_ROW])[0]
    assert abs(t["risk_amt"] - 20.0) < 1e-9, t["risk_amt"]
    assert abs(t["gross_r"] - 2.0) < 1e-9, t["gross_r"]


def test_a_stop_on_the_genuinely_wrong_side_is_still_refused():
    """The one case the old guard was right about must survive. A SHORT whose
    stop sits BELOW entry is not a wide stop, it is a bad row — and
    D.risk_per_share returns 0.0 for it rather than inventing a denominator."""
    bad = _row("BAD", "SHORT", 100.00, 98.00, 99.00, 10, 10.00)
    t = _load([bad])[0]
    assert t["gross_r"] is None and t["net_r"] is None and t["risk_amt"] is None


def test_a_missing_direction_reads_as_long():
    """`D.normalise` treats an absent direction as LONG, deliberately — every
    pre-shorting row has one. This tool must inherit that, not invent its own."""
    row = {**_row("OLD", "LONG", 100.00, 98.00, 104.00, 10, 40.00), "direction": None}
    t = _load([row])[0]
    assert abs(t["gross_r"] - 2.0) < 1e-9, t["gross_r"]


# ── BUG-2 ────────────────────────────────────────────────────────────────────

def test_column_presence_and_computability_are_reported_separately():
    """Three rows carry the column; only two can be scored. One number cannot
    honestly stand for both, and the one that was printed was the wrong one
    for a gate that turns on coverage."""
    from tools.expectancy_ledger import ledger
    rows = _load([
        DEVYANI,                                                  # scorable short
        LONG_ROW,                                                 # scorable long
        _row("BAD", "SHORT", 100.0, 98.0, 99.0, 10, 10.0),        # has stop, unscorable
        {**_row("NOSTOP", "LONG", 50.0, 0, 52.0, 10, 20.0),
         "planned_stop_at_entry": None},                          # no stop at all
    ])
    import config
    saved, config.get_supabase = config.get_supabase, lambda: _SB([])
    try:
        out = _captured(lambda: ledger(rows))
    finally:
        config.get_supabase = saved

    assert "PRESENT on  3 of 4" in out, f"column-presence count wrong: {out!r}"
    assert "R COMPUTABLE on                   2 of 4" in out, \
        f"computability count wrong: {out!r}"
    assert "column coverage" in out and "usable for R stats" in out, \
        f"the two counts are not labelled: {out!r}"


def test_the_unscorable_row_is_named_not_absorbed():
    """A stop on the wrong side is a data defect. Naming the symbol is what
    makes it fixable; folding it into a percentage is what hid it."""
    from tools.expectancy_ledger import ledger
    rows = _load([DEVYANI, _row("BAD", "SHORT", 100.0, 98.0, 99.0, 10, 10.0)])
    import config
    saved, config.get_supabase = config.get_supabase, lambda: _SB([])
    try:
        out = _captured(lambda: ledger(rows), level="WARNING")
    finally:
        config.get_supabase = saved
    assert "BAD" in out and "wrong side" in out, out


# ── BUG-3 ────────────────────────────────────────────────────────────────────

def test_exit_audit_accepts_days_and_windows_on_exit_date():
    """The flag both spec documents already use must exist AND must filter.
    A flag that parses and does nothing is the same defect one level up."""
    import datetime as _dt
    from tools.exit_audit import audit_closed

    def _er(symbol, exit_date):
        return {"symbol": symbol, "framework": "INTRADAY", "direction": "LONG",
                "entry_price": 100.0, "exit_price": 101.0, "r_multiple": 0.5,
                "exit_reason": "TARGET_HIT", "max_favorable_excursion": 1.2,
                "max_adverse_excursion": -0.3, "planned_stop_at_entry": 98.0,
                "planned_target_at_entry": 104.0, "realized_pnl": 10.0,
                "charges": 1.0, "exit_date": exit_date}

    today = _dt.date.today()
    rows = [_er("RECENT", (today - _dt.timedelta(days=3)).isoformat()),
            _er("OLD",    (today - _dt.timedelta(days=200)).isoformat()),
            _er("UNDATED", None)]

    out = _captured(lambda: audit_closed(_SB(rows), "INTRADAY", days=30))
    assert "1 of 3 closed row(s)" in out, f"the window did not filter: {out!r}"
    assert "1 carry no exit_date and are excluded" in out, out
    assert "last 30d" in out, f"the window is not stated on the header: {out!r}"

    everything = _captured(lambda: audit_closed(_SB(rows), "INTRADAY"))
    assert "ALL history" in everything, everything
    assert "3 with a stop and an R" in everything, everything


TESTS = [
    ("the old formula would have dropped this row",
     test_the_old_formula_would_have_dropped_this_row),
    ("DEVYANI short is scored and matches the hand computation",
     test_devyani_short_is_scored_and_matches_the_hand_computation),
    ("DEVYANI matches the r_multiple the lifecycle stored",
     test_devyani_matches_the_r_multiple_the_lifecycle_stored),
    ("friction and net R are consistent for the short",
     test_friction_and_net_r_are_consistent_for_the_short),
    ("the long path is bit-identical",
     test_the_long_path_is_bit_identical),
    ("a stop on the genuinely wrong side is still refused",
     test_a_stop_on_the_genuinely_wrong_side_is_still_refused),
    ("a missing direction reads as LONG",
     test_a_missing_direction_reads_as_long),
    ("column presence and computability are reported separately",
     test_column_presence_and_computability_are_reported_separately),
    ("the unscorable row is named, not absorbed",
     test_the_unscorable_row_is_named_not_absorbed),
    ("exit_audit accepts --days and windows on exit_date",
     test_exit_audit_accepts_days_and_windows_on_exit_date),
]
