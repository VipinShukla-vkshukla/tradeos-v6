"""
Reuse yesterday's AI verdict for a materially unchanged candidate —
29-Aug-2026. Quantified before building: over 21 days, 212 candidates
repeated on consecutive days, ZERO byte-identical (avg entry-zone drift
~2%, ATR-based zones recompute from fresh price data every evening even
for a persisting setup) — so an exact-match cache would silently never
fire. Within a 2% tolerance, 116 of 212 (55%) qualify.

Scoped to candidate ranking only — position_actions (feeding the live
TIGHTEN_SL action, Track E F-70) is structurally separate and never
touched by anything here.
"""

from __future__ import annotations

from unittest.mock import patch

from ai.ai_decision_engine import _cache_eligible, find_reusable_candidates


# ── _cache_eligible (pure) ──────────────────────────────────────────────

def test_same_strategy_within_tolerance_is_eligible():
    today = {"strategy": "CTL", "entry_zone_low": 100.5, "entry_zone_high": 105.0}
    prior = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 104.5}
    assert _cache_eligible(today, prior, tolerance_pct=1.0)


def test_different_strategy_is_never_eligible_even_at_identical_prices():
    today = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    prior = {"strategy": "MOM", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    assert not _cache_eligible(today, prior, tolerance_pct=1.0)


def test_drift_beyond_tolerance_is_not_eligible():
    today = {"strategy": "CTL", "entry_zone_low": 103.0, "entry_zone_high": 105.0}
    prior = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    # 3% drift on entry_zone_low, tolerance is 1%
    assert not _cache_eligible(today, prior, tolerance_pct=1.0)


def test_exact_match_is_eligible_at_zero_tolerance():
    today = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    prior = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    assert _cache_eligible(today, prior, tolerance_pct=0.0)


def test_missing_field_is_never_eligible():
    today = {"strategy": "CTL", "entry_zone_low": None, "entry_zone_high": 105.0}
    prior = {"strategy": "CTL", "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    assert not _cache_eligible(today, prior, tolerance_pct=5.0)


def test_zero_prior_value_does_not_crash_with_division():
    today = {"strategy": "CTL", "entry_zone_low": 1.0, "entry_zone_high": 105.0}
    prior = {"strategy": "CTL", "entry_zone_low": 0.0, "entry_zone_high": 105.0}
    assert not _cache_eligible(today, prior, tolerance_pct=1.0)


def test_missing_strategy_on_either_side_is_never_eligible():
    today = {"strategy": None, "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    prior = {"strategy": None, "entry_zone_low": 100.0, "entry_zone_high": 105.0}
    assert not _cache_eligible(today, prior, tolerance_pct=1.0)


# ── find_reusable_candidates (impure wrapper, fake sb) ──────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def lte(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def execute(self):
        class R: pass
        r = R(); r.data = self._rows
        return r


class _FakeSb:
    """Serves canned rows per table — enough to drive
    find_reusable_candidates through a real prior-day lookup without a
    live database."""
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


def _prior_final_picks_row(entries: list[dict]) -> dict:
    import json
    return {"conviction_reason": json.dumps({"ranked_candidates": entries})}


def test_matching_symbol_within_tolerance_is_reused():
    sb = _FakeSb({
        "master_shortlist": [{"symbol": "TCS", "entry_zone_low": 3800.0,
                              "entry_zone_high": 3850.0}],
        "signal_log": [{"symbol": "TCS", "strategy": "CTL"}],
        "ai_context": [_prior_final_picks_row([
            {"symbol": "TCS", "tier": "TIER_1", "conviction": "HIGH",
             "thesis": "strong breakout", "action": "ENTER_NOW"},
        ])],
        "signal_log_dummy": [],
    })
    candidates = [{"symbol": "TCS", "strategy": "CTL",
                  "entry_zone_low": 3802.0, "entry_zone_high": 3848.0}]

    with patch("ai.ai_decision_engine.get_last_trading_date",
              return_value="2026-08-28"):
        reused, to_rank = find_reusable_candidates(sb, candidates, "2026-08-29")
    assert "TCS" in reused
    assert reused["TCS"]["tier"] == "TIER_1"
    assert reused["TCS"]["_reused_from"] == "2026-08-28"
    assert to_rank == []


def test_a_reused_prior_entry_never_chains():
    """Yesterday's own entry was ITSELF a reuse (from the day before) —
    must not reuse a reuse. Bounds staleness to exactly one trading day,
    never an unbounded chain."""
    sb = _FakeSb({
        "master_shortlist": [{"symbol": "TCS", "entry_zone_low": 3800.0,
                              "entry_zone_high": 3850.0}],
        "signal_log": [{"symbol": "TCS", "strategy": "CTL"}],
        "ai_context": [_prior_final_picks_row([
            {"symbol": "TCS", "tier": "TIER_1", "_reused_from": "2026-08-27"},
        ])],
    })
    candidates = [{"symbol": "TCS", "strategy": "CTL",
                  "entry_zone_low": 3800.0, "entry_zone_high": 3850.0}]

    with patch("ai.ai_decision_engine.get_last_trading_date",
              return_value="2026-08-28"):
        reused, to_rank = find_reusable_candidates(sb, candidates, "2026-08-29")
    assert reused == {}
    assert to_rank == ["TCS"]


def test_no_prior_day_data_falls_back_to_ranking_everything():
    sb = _FakeSb({})   # nothing for any table
    candidates = [{"symbol": "TCS", "strategy": "CTL",
                  "entry_zone_low": 3800.0, "entry_zone_high": 3850.0}]

    with patch("ai.ai_decision_engine.get_last_trading_date",
              return_value="2026-08-28"):
        reused, to_rank = find_reusable_candidates(sb, candidates, "2026-08-29")
    assert reused == {}
    assert to_rank == ["TCS"]


def test_prior_trading_date_resolution_failure_degrades_safely():
    sb = _FakeSb({})
    candidates = [{"symbol": "TCS", "strategy": "CTL",
                  "entry_zone_low": 3800.0, "entry_zone_high": 3850.0}]

    with patch("ai.ai_decision_engine.get_last_trading_date",
              side_effect=RuntimeError("boom")):
        reused, to_rank = find_reusable_candidates(sb, candidates, "2026-08-29")
    assert reused == {}
    assert to_rank == ["TCS"], "a resolution failure must still analyse every candidate"


def test_mixed_symbols_split_correctly():
    """One reusable, one drifted too far, one with no prior data at all —
    each must land in the right bucket."""
    sb = _FakeSb({
        "master_shortlist": [
            {"symbol": "TCS", "entry_zone_low": 3800.0, "entry_zone_high": 3850.0},
            {"symbol": "INFY", "entry_zone_low": 1500.0, "entry_zone_high": 1520.0},
        ],
        "signal_log": [
            {"symbol": "TCS", "strategy": "CTL"},
            {"symbol": "INFY", "strategy": "CTL"},
        ],
        "ai_context": [_prior_final_picks_row([
            {"symbol": "TCS", "tier": "TIER_1"},
            {"symbol": "INFY", "tier": "TIER_2"},
        ])],
    })
    candidates = [
        {"symbol": "TCS", "strategy": "CTL",
         "entry_zone_low": 3801.0, "entry_zone_high": 3849.0},   # within tolerance
        {"symbol": "INFY", "strategy": "CTL",
         "entry_zone_low": 1560.0, "entry_zone_high": 1580.0},   # drifted ~4%
        {"symbol": "WIPRO", "strategy": "CTL",
         "entry_zone_low": 400.0, "entry_zone_high": 410.0},     # no prior data
    ]

    with patch("ai.ai_decision_engine.get_last_trading_date",
              return_value="2026-08-28"):
        reused, to_rank = find_reusable_candidates(sb, candidates, "2026-08-29")
    assert set(reused.keys()) == {"TCS"}
    assert set(to_rank) == {"INFY", "WIPRO"}


TESTS = [
    ("same strategy within tolerance -> eligible",
     test_same_strategy_within_tolerance_is_eligible),
    ("different strategy never eligible, even at identical prices",
     test_different_strategy_is_never_eligible_even_at_identical_prices),
    ("drift beyond tolerance -> not eligible",
     test_drift_beyond_tolerance_is_not_eligible),
    ("exact match eligible at zero tolerance",
     test_exact_match_is_eligible_at_zero_tolerance),
    ("missing field -> never eligible", test_missing_field_is_never_eligible),
    ("zero prior value does not crash", test_zero_prior_value_does_not_crash_with_division),
    ("missing strategy on either side -> never eligible",
     test_missing_strategy_on_either_side_is_never_eligible),
    ("matching symbol within tolerance is reused",
     test_matching_symbol_within_tolerance_is_reused),
    ("a reused prior entry never chains", test_a_reused_prior_entry_never_chains),
    ("no prior-day data -> ranks everything fresh",
     test_no_prior_day_data_falls_back_to_ranking_everything),
    ("prior-trading-date resolution failure degrades safely",
     test_prior_trading_date_resolution_failure_degrades_safely),
    ("mixed symbols split into the right buckets", test_mixed_symbols_split_correctly),
]
