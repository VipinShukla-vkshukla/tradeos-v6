"""
The replay tool must not count one lingering setup as many candidates
(10-Aug-2026, second pass).

WHAT THIS CATCHES
-----------------
The first version of `tools/allocator_replay.py` treated every
`intraday_setups` row for a session as an independent trade opportunity and
ranked/selected the top-`cap` by edge. A setup that lingers near its zone gets
re-logged every cycle it is re-evaluated — the same mechanism migration 054
measured on the SWING side (one symbol, 548-600 rows in a session) — so one
real setup could occupy several of the top-cap slots at once, counting its
single real outcome multiple times. The first live run against real data
showed 502 "candidates" on 08-05 against a ~40-symbol universe, which is
duplicate rows, not 502 detections, and the run reported NEW deteriorating by
-46.44R on an 8-session sample before this fix.

The live system never has this problem — it only ever gets one real shot at a
lingering setup. This dedup restores that property to the retrospective
ranking without touching the population priors are built from, which must stay
undeduplicated to match `scoring.intraday_priors()` exactly.
"""

from __future__ import annotations

from tools.allocator_replay import _dedupe_candidates


def _row(id_, symbol, strategy, verdict, outcome_pct=0.0):
    return {"id": id_, "symbol": symbol, "strategy": strategy,
            "cost_verdict": verdict, "outcome_pct": outcome_pct}


def test_a_lingering_decline_collapses_to_one_row():
    """Six ALLOCATOR_DECLINED rows for the same hovering setup must become
    one, or the ranking counts one opportunity six times."""
    rows = [_row(i, "PAYTM", "GAP", "ALLOCATOR_DECLINED") for i in range(1, 7)]
    out = _dedupe_candidates(rows)
    assert len(out) == 1, f"{len(out)} row(s) survived — expected 1"


def test_the_survivor_is_the_most_recent_when_never_taken():
    """When a setup was never taken, the freshest snapshot of its price/edge
    is the representative one — the id sequence is the tool's only ordering
    signal (ts is not selected)."""
    rows = [_row(1, "PAYTM", "GAP", "ALLOCATOR_DECLINED"),
            _row(5, "PAYTM", "GAP", "ALLOCATOR_DECLINED"),
            _row(3, "PAYTM", "GAP", "ALLOCATOR_DECLINED")]
    out = _dedupe_candidates(rows)
    assert len(out) == 1 and out[0]["id"] == 5


def test_a_taken_row_always_wins_over_its_own_declines():
    """A setup declined three times before finally clearing the bar and being
    taken must be represented by the TAKEN row — that row's outcome_pct is
    what a real position would have carried, not the earlier near-misses."""
    rows = [_row(1, "PAYTM", "GAP", "ALLOCATOR_DECLINED"),
            _row(2, "PAYTM", "GAP", "ALLOCATOR_DECLINED"),
            _row(3, "PAYTM", "GAP", "TAKEN", outcome_pct=0.79),
            _row(4, "PAYTM", "GAP", "ALLOCATOR_DECLINED")]
    out = _dedupe_candidates(rows)
    assert len(out) == 1
    assert out[0]["cost_verdict"] == "TAKEN"
    assert out[0]["outcome_pct"] == 0.79


def test_different_symbols_and_engines_are_never_collapsed_together():
    """The dedup key is (symbol, strategy) — genuinely distinct opportunities
    on the same day must all survive."""
    rows = [_row(1, "PAYTM", "GAP", "ALLOCATOR_DECLINED"),
            _row(2, "PAYTM", "ORB", "ALLOCATOR_DECLINED"),   # same symbol, diff engine
            _row(3, "RELIANCE", "GAP", "ALLOCATOR_DECLINED")]  # diff symbol, same engine
    out = _dedupe_candidates(rows)
    assert len(out) == 3, f"{len(out)} survived — distinct setups were merged"


def test_a_single_row_is_unaffected():
    """The common case — a setup logged exactly once — must pass through
    unchanged, or every ordinary session's count would shrink for no reason."""
    rows = [_row(1, "PAYTM", "GAP", "TAKEN", outcome_pct=0.5)]
    out = _dedupe_candidates(rows)
    assert out == rows


TESTS = [
    ("a lingering decline collapses to one row",
     test_a_lingering_decline_collapses_to_one_row),
    ("the survivor is the most recent when never taken",
     test_the_survivor_is_the_most_recent_when_never_taken),
    ("a taken row always wins over its own declines",
     test_a_taken_row_always_wins_over_its_own_declines),
    ("different symbols and engines are never collapsed together",
     test_different_symbols_and_engines_are_never_collapsed_together),
    ("a single row is unaffected",
     test_a_single_row_is_unaffected),
]


def test_the_replay_executes_the_LIVE_prior_function():
    """THE FAILURE THAT MADE THIS CHECK NECESSARY.

    `allocator_replay` originally reimplemented `intraday_priors()` in a local
    `_priors_from()`. The prior-deduplication fix landed in scoring.py and the
    tool's very next run produced byte-identical output — same -9.94R, same
    bars — because it had never been executing that function. A tool that
    measures a change must run the changed code.

    Asserted by MONKEYPATCHING the live function and requiring the tool's NEW
    path to observe it. A grep for the import would pass on a file that
    imports and then ignores it; this cannot."""
    from unittest.mock import patch
    from tools import allocator_replay as AR

    sentinel = {"INTRADAY/ALL": object()}
    with patch("allocation.scoring.intraday_priors", return_value=sentinel) as m:
        got = AR._new_priors([{"strategy": "ORB"}], 30)
    assert m.called, "the NEW path did not call scoring.intraday_priors()"
    assert got is sentinel, "the NEW path did not return what the live function gave it"


def test_the_legacy_baseline_does_NOT_track_the_live_function():
    """The OLD baseline must stay frozen — it is the fixed reference point the
    comparison is against. If it tracked scoring.py, every future fix would
    move both sides and the tool would always report 'no change'."""
    from unittest.mock import patch
    from tools import allocator_replay as AR

    rows = [{"strategy": "ORB", "direction": "LONG", "entry": 100.0, "stop": 99.0,
             "outcome_pct": 0.5, "cost_pct": 0.21, "cost_verdict": "TAKEN",
             "symbol": "A", "trade_date": "2026-08-05"}]
    with patch("allocation.scoring.intraday_priors",
               side_effect=AssertionError("legacy must not call the live function")):
        AR._legacy_priors(rows, 30)   # must not raise


TESTS += [
    ("the replay executes the LIVE prior function",
     test_the_replay_executes_the_LIVE_prior_function),
    ("the legacy baseline does NOT track the live function",
     test_the_legacy_baseline_does_NOT_track_the_live_function),
]
