"""
Health check for IGN's Kite trend panel (24-Sep-2026) — tools/health.py.

A health check that cannot fail is not a check (five were found reporting green
while what they watched was broken). So every verdict here is tested in BOTH
directions: the shapes that must FAIL — the code shipped but nothing writes it,
a forming bar leaked into the panel, a stale panel, collapsing coverage — and a
realistic healthy day that must PASS.
"""

from __future__ import annotations

import json

from tools.health import ign_trend_verdict


def _row(day: str, *, as_of="2026-09-25", ok=True, available=True, reason=None,
         trend=True, as_json=False) -> dict:
    meta = {"chg_pct": 4.0}
    if trend:
        meta["trend"] = {"available": available, "ok": ok, "reason": reason,
                         "as_of": as_of, "adx": 25.0}
    return {"trade_date": day, "meta": json.dumps(meta) if as_json else meta}


AFTER = "2026-09-29"      # a Tuesday after the grace period (shipped 24-Sep + 3 days)
DURING = "2026-09-25"


def test_no_detections_is_nothing_to_check():
    ok, msg = ign_trend_verdict([])
    assert ok and "nothing to check" in msg, msg


def test_grace_period_tolerates_no_record_yet():
    ok, msg = ign_trend_verdict([_row(DURING, trend=False) for _ in range(5)])
    assert ok and "grace" in msg, msg


def test_shipped_but_not_written_after_the_grace_period_fails():
    ok, msg = ign_trend_verdict([_row(AFTER, trend=False) for _ in range(6)])
    assert not ok and "not writing" in msg, (
        "the code shipped days ago and no detection carries a record: red")


def test_a_midday_restart_is_not_a_failure():
    rows = ([_row(AFTER, trend=False) for _ in range(3)]
            + [_row(AFTER, as_of="2026-09-28") for _ in range(12)])
    ok, msg = ign_trend_verdict(rows)
    assert ok, f"some detections before the daemon restarted is expected: {msg}"


def test_a_healthy_day_passes():
    rows = [_row(AFTER, as_of="2026-09-28") for _ in range(20)]
    ok, msg = ign_trend_verdict(rows)
    assert ok and "100%" in msg and "none leaks" in msg, msg


def test_a_forming_bar_leak_fails():
    same_day = ign_trend_verdict([_row(AFTER, as_of=AFTER)] + [_row(AFTER, as_of="2026-09-28")] * 15)
    assert not same_day[0] and "FORMING" in same_day[1], same_day
    future = ign_trend_verdict([_row(AFTER, as_of="2026-09-30")])
    assert not future[0], "an as_of AFTER the trade date is worse, not better"


def test_a_stale_panel_fails_but_a_weekend_gap_does_not():
    ok, msg = ign_trend_verdict([_row(AFTER, as_of="2026-09-18") for _ in range(12)])
    assert not ok and "5 days" in msg, msg
    monday = ign_trend_verdict([_row("2026-09-28", as_of="2026-09-25") for _ in range(12)])
    assert monday[0], f"Friday's close feeding Monday is 3 days and normal: {monday}"


def test_collapsing_coverage_fails_and_names_why():
    rows = ([_row(AFTER, as_of="2026-09-28") for _ in range(5)]
            + [_row(AFTER, ok=False, reason="close_jump:2.00", as_of="2026-09-28") for _ in range(5)]
            + [_row(AFTER, available=False, ok=False, as_of=None) for _ in range(4)])
    ok, msg = ign_trend_verdict(rows)
    assert not ok and "usable" in msg and "close_jump=5" in msg, msg


def test_a_panel_that_failed_its_integrity_check_is_not_usable_coverage():
    # 9 good + 3 that are PRESENT but failed the split/jump/short-history check:
    # 9/12 = 75% usable. Counting a present-but-refused panel as usable would
    # read 100% and hide that a quarter of detections are being abstained on.
    rows = ([_row(AFTER, as_of="2026-09-28") for _ in range(9)]
            + [_row(AFTER, ok=False, reason="ref_mismatch:3.10%", as_of="2026-09-28")
               for _ in range(3)])
    ok, msg = ign_trend_verdict(rows)
    assert not ok and "9/12" in msg and "ref_mismatch=3" in msg, msg


def test_a_small_sample_is_not_judged_on_coverage():
    rows = [_row(AFTER, available=False, ok=False, as_of=None) for _ in range(4)]
    ok, msg = ign_trend_verdict(rows)
    assert ok, f"4 detections is too few to call coverage failing: {msg}"


def test_meta_stored_as_a_json_string_is_read():
    ok, msg = ign_trend_verdict([_row(AFTER, as_of=AFTER, as_json=True)])
    assert not ok and "FORMING" in msg, (
        "older rows stored meta as a JSON STRING inside the jsonb column; the "
        "check must read them, not skip them as 'no record'")


def test_the_check_is_registered():
    from tools import health
    names = {c[0]: c for c in health.CHECKS}
    assert "ign_trend" in names and names["ign_trend"][2] is health.check_ign_trend_panel


TESTS = [
    ("no detections is nothing to check", test_no_detections_is_nothing_to_check),
    ("grace period tolerates no record yet", test_grace_period_tolerates_no_record_yet),
    ("shipped but not written after the grace period FAILS",
     test_shipped_but_not_written_after_the_grace_period_fails),
    ("a midday restart is not a failure", test_a_midday_restart_is_not_a_failure),
    ("a healthy day passes", test_a_healthy_day_passes),
    ("a forming-bar leak FAILS", test_a_forming_bar_leak_fails),
    ("a stale panel fails, a weekend gap does not",
     test_a_stale_panel_fails_but_a_weekend_gap_does_not),
    ("collapsing coverage FAILS and names why",
     test_collapsing_coverage_fails_and_names_why),
    ("a panel that failed its integrity check is not usable coverage",
     test_a_panel_that_failed_its_integrity_check_is_not_usable_coverage),
    ("a small sample is not judged on coverage",
     test_a_small_sample_is_not_judged_on_coverage),
    ("meta stored as a JSON string is read",
     test_meta_stored_as_a_json_string_is_read),
    ("the check is registered", test_the_check_is_registered),
]
