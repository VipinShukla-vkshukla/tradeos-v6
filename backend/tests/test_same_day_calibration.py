"""
tools/same_day_calibration.py::evaluate() — Stage D5, 24-Aug-2026
(docs/TRADEOS_ROADMAP.md, Track D, branch feat/intraday-regression-shadow).

WHAT THIS COVERS
-----------------
The pure walk-forward loop, with synthetic rows shaped exactly like
`_intraday_priors_from_rows()`'s own expected input (this tool calls that
function directly rather than reimplementing dedup/R-conversion — see its
own module docstring). No database, no fetch — `evaluate()` is a pure
function over an in-memory row list for the identical reason `_intraday_
priors_from_rows` itself is: a replay/calibration tool must execute the
CHANGED code, not a copy of it.

DISTINCT SYMBOLS PER TRADE WITHIN ONE DAY, DELIBERATELY. `intraday_setups`
carries one row per (setup, evaluation cycle), and `_intraday_priors_
from_rows`'s own dedup collapses rows sharing (symbol, engine, trade_date)
into ONE observation — the fix this test file's own sibling bug (caught on
this tool's first live run, see same_day_calibration.py's own comment)
made necessary. Fixtures that want N independent same-day trades must use
N distinct symbols, or they collapse to n=1 exactly like a real re-recorded
setup would, and the test would be silently measuring the wrong thing.
"""

from __future__ import annotations

from tests import cfg_ctx


def _row(trade_date, engine, symbol="TEST", entry=100.0, stop=99.0,
        direction="LONG", outcome_pct=1.0, cost_pct=0.0, cost_verdict="TAKEN"):
    """A LONG trade with entry=100/stop=99 has risk_pct=1%, so
    outcome_pct=1.0 (i.e. 1%) is exactly R=+1.0 gross; outcome_pct=-1.0 is
    R=-1.0. Chosen so test fixtures can reason in whole R units."""
    return {
        "symbol": symbol, "trade_date": trade_date, "strategy": engine,
        "outcome": "TARGET" if outcome_pct > 0 else "STOP",
        "outcome_pct": outcome_pct, "entry": entry, "stop": stop,
        "direction": direction, "cost_verdict": cost_verdict,
        "cost_pct": cost_pct, "meta": {}, "confidence": 0.5,
    }


def _same_day_trades(trade_date, engine, n, outcome_pct, cost_verdict="TAKEN"):
    """N independent same-day trades for one engine -- N distinct symbols,
    so none of them collapse into another under the (symbol, engine,
    trade_date) dedup key."""
    return [_row(trade_date, engine, symbol=f"SYM{i}", outcome_pct=outcome_pct,
                cost_verdict=cost_verdict) for i in range(n)]


def test_empty_rows_produce_no_results():
    from tools.same_day_calibration import evaluate
    assert evaluate([], min_n=5, prior_floor=30) == []


def test_below_min_n_on_the_day_produces_nothing():
    from tools.same_day_calibration import evaluate
    rows = (_same_day_trades("2026-08-01", "ORB", 1, outcome_pct=1.0)
           + _same_day_trades("2026-08-01", "ORB", 1, outcome_pct=-1.0))   # 2 distinct, floor is 5
    out = evaluate(rows, min_n=5, prior_floor=30)
    assert out == []


def test_repeated_same_day_rerecords_of_one_setup_collapse_to_one_trade():
    """The bug this fix targets, pinned directly: many rows sharing one
    (symbol, engine, day) — a lingering setup re-recorded every ~15s — must
    collapse to ONE observation, not N. Below the 5-floor even with 20 rows."""
    from tools.same_day_calibration import evaluate
    rows = [_row("2026-08-01", "ORB", symbol="LINGER", outcome_pct=1.0)
           for _ in range(20)]
    out = evaluate(rows, min_n=5, prior_floor=1)
    assert out == [], (
        "20 re-records of ONE setup must collapse to n=1 and stay below "
        "the same-day floor, not be counted as 20 independent trades")


def test_first_day_has_no_history_so_multiplier_is_a_noop():
    """Day 1 for an engine has zero prior history -- same_day_fit_
    multiplier() must return no opinion regardless of how bad day 1 was,
    the identical cold-start rule this module applies everywhere else."""
    from tools.same_day_calibration import evaluate
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        rows = _same_day_trades("2026-08-01", "ORB", 5, outcome_pct=-1.0)
        out = evaluate(rows, min_n=5, prior_floor=1)
    assert len(out) == 1
    assert out[0]["flagged"] is False
    assert out[0]["historical_n"] == 0


def test_a_later_bad_day_against_real_history_is_flagged():
    """Build 30 days of history for ORB at an 80% hit-rate (a LITERAL
    100% history is a degenerate binomial population -- same_day_fit_
    multiplier() correctly refuses to test against it; realistic history
    always has some losses), then a later day that is 0-for-5 -- must be
    flagged given the weight is armed for this test."""
    from tools.same_day_calibration import evaluate
    rows = []
    for d in range(1, 31):
        outcome = 1.0 if d % 5 != 0 else -1.0   # 24 wins, 6 losses -> 0.80
        rows.append(_row(f"2026-07-{d:02d}", "ORB", outcome_pct=outcome))
    # Day 31: a clear 0-for-5 (5 distinct symbols) against a historical
    # prior that is n=30, 80% wins.
    rows += _same_day_trades("2026-08-01", "ORB", 5, outcome_pct=-1.0)

    with cfg_ctx({"intraday_same_day_fit_weight": "1.0",
                  "intraday_same_day_fit_alpha": "0.05"}):
        out = evaluate(rows, min_n=5, prior_floor=30)

    assert len(out) == 1
    row = out[0]
    assert row["trade_date"] == "2026-08-01"
    assert row["engine"] == "ORB"
    assert row["historical_n"] == 30
    assert abs(row["historical_hit_rate"] - 0.80) < 1e-9
    assert row["today_wins"] == 0
    assert row["today_n"] == 5
    assert row["flagged"] is True
    assert row["multiplier"] < 1.0


def test_refused_detections_do_not_count_toward_todays_sample():
    """A day with 2 TAKEN and 6 BLOCKED_STRUCTURE trades must be evaluated
    on the 2 TAKEN trades only -- below the 5-floor -- not on 8."""
    from tools.same_day_calibration import evaluate
    rows = (_same_day_trades("2026-08-01", "ORB", 2, outcome_pct=1.0)
           + _same_day_trades("2026-08-01", "ORB", 6, outcome_pct=-1.0,
                              cost_verdict="BLOCKED_STRUCTURE"))
    out = evaluate(rows, min_n=5, prior_floor=1)
    assert out == [], "refused detections must not fill the same-day floor"


def test_engines_are_evaluated_independently_within_one_day():
    from tools.same_day_calibration import evaluate
    rows = []
    for d in range(1, 31):
        # 80% historical hit-rate for both -- a literal 100% is a
        # degenerate binomial population same_day_fit_multiplier()
        # correctly refuses to test against (see the test above).
        outcome = 1.0 if d % 5 != 0 else -1.0
        rows.append(_row(f"2026-07-{d:02d}", "ORB", outcome_pct=outcome))
        rows.append(_row(f"2026-07-{d:02d}", "PDL", outcome_pct=outcome))
    rows += _same_day_trades("2026-08-01", "ORB", 5, outcome_pct=-1.0)
    rows += _same_day_trades("2026-08-01", "PDL", 5, outcome_pct=1.0)

    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        out = evaluate(rows, min_n=5, prior_floor=30)

    by_engine = {r["engine"]: r for r in out}
    assert by_engine["ORB"]["flagged"] is True
    assert by_engine["PDL"]["flagged"] is False


def test_walk_forward_never_lets_todays_own_rows_into_its_own_prior():
    """The historical prior for a day must be built ONLY from strictly
    earlier days -- a day cannot price itself."""
    from tools.same_day_calibration import evaluate
    rows = _same_day_trades("2026-08-01", "ORB", 10, outcome_pct=-1.0)
    with cfg_ctx({"intraday_same_day_fit_weight": "1.0"}):
        out = evaluate(rows, min_n=5, prior_floor=1)
    assert len(out) == 1
    assert out[0]["historical_n"] == 0, (
        "a single day's own rows must never appear in its own historical prior")


TESTS = [
    ("empty rows produce no results", test_empty_rows_produce_no_results),
    ("below min_n on the day produces nothing", test_below_min_n_on_the_day_produces_nothing),
    ("repeated same-day re-records of one setup collapse to one trade",
     test_repeated_same_day_rerecords_of_one_setup_collapse_to_one_trade),
    ("first day has no history so multiplier is a no-op", test_first_day_has_no_history_so_multiplier_is_a_noop),
    ("a later bad day against real history is flagged", test_a_later_bad_day_against_real_history_is_flagged),
    ("refused detections do not count toward today's sample", test_refused_detections_do_not_count_toward_todays_sample),
    ("engines are evaluated independently within one day", test_engines_are_evaluated_independently_within_one_day),
    ("walk-forward never lets today's own rows into its own prior", test_walk_forward_never_lets_todays_own_rows_into_its_own_prior),
]
