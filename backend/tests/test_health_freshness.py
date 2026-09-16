"""
tools.health.check_data_freshness — did the last trading session's evening
pipeline land? It tolerated 4 calendar days, so the 03-Sep-2026 failure (never
re-run) read green on 04-Sep, and the book traded 04-Sep on 02-Sep's plans.
"""

from __future__ import annotations

HOLIDAYS = {"2026-08-15", "2026-09-14"}


def test_one_missed_evening_is_caught():
    from tools.health import freshness_problems
    latest = {"signal_output_daily": "2026-09-02", "stock_data_daily": "2026-09-02",
              "market_regime": "2026-09-02"}
    probs = freshness_problems(latest, "2026-09-04", HOLIDAYS)
    assert probs and any("2026-09-03" in p for p in probs), probs


def test_current_data_passes():
    from tools.health import freshness_problems
    latest = {"signal_output_daily": "2026-09-16", "stock_data_daily": "2026-09-16",
              "market_regime": "2026-09-16"}
    assert freshness_problems(latest, "2026-09-17", HOLIDAYS) == []


def test_weekend_and_holiday_are_not_missing_sessions():
    from tools.health import freshness_problems
    # Monday 15-Sep after Friday 11-Sep, with 14-Sep a holiday: nothing is missing
    latest = {"signal_output_daily": "2026-09-11", "market_regime": "2026-09-11"}
    assert freshness_problems(latest, "2026-09-15", HOLIDAYS) == []


def test_market_regime_is_checked():
    from tools.health import FRESHNESS_TABLES
    assert "market_regime" in FRESHNESS_TABLES


TESTS = [
    ("one missed evening pipeline is caught", test_one_missed_evening_is_caught),
    ("current data passes", test_current_data_passes),
    ("weekend and holiday are not missing sessions", test_weekend_and_holiday_are_not_missing_sessions),
    ("market_regime is part of the freshness check", test_market_regime_is_checked),
]
