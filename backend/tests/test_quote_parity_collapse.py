"""
quote_parity.should_log() — 10-Sep-2026, migration 131.

intraday_quote_parity logs a comparison every 300s per symbol per field —
150,686 rows over 5 trading days at the time this was measured. should_log()
decides which of those are worth a physical row and which are duplicates of
the last one logged for that (symbol, field) today.

Measured live before this shipped (docs/FINDINGS.md, 10-Sep-2026), at the
shipped defaults (vwap_tolerance=0.04, heartbeat_s=7200):
150,686 -> 10,085 rows (93.3% fewer), with range_verdict()/vwap_verdict()
producing an IDENTICAL pass/fail verdict computed over the collapsed set
as over the raw one — see tools/replay_quote_parity_collapse.py. The
heartbeat default was NOT the first one tried — 1800s (copied uncritically
from the allocator's own write-collapse) only reached 86.0%, because it
turned out to be the binding constraint, not the material-change
threshold. Swept against the real data before settling on 7200s.

Pure-function tests only, no database.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tools.quote_parity import should_log

VWAP_TOL = 0.04
HEARTBEAT_S = 7200.0
NOW = datetime.fromisoformat("2026-09-10T09:30:00+05:30")


def test_volume_is_never_logged_under_collapse():
    """SCORED excludes volume — it has never affected any verdict. Collapse
    drops it unconditionally, regardless of state or how much it changed."""
    assert should_log("volume", 5.0, None, NOW, VWAP_TOL, HEARTBEAT_S) is False
    state = {"diff_pct": 1.0, "logged_at": NOW - timedelta(hours=5)}
    assert should_log("volume", 99.0, state, NOW, VWAP_TOL, HEARTBEAT_S) is False


def test_prev_close_logs_once_then_never_again_that_day():
    state = None
    assert should_log("prev_close", -4.18, state, NOW, VWAP_TOL, HEARTBEAT_S) is True
    state = {"diff_pct": -4.18, "logged_at": NOW}
    # even far later the same day, even with a "changed" value (shouldn't
    # happen in practice — prev_close is static — but the rule must not
    # depend on that not happening)
    later = NOW + timedelta(hours=6)
    assert should_log("prev_close", -3.90, state, later, VWAP_TOL, HEARTBEAT_S) is False


def test_day_high_exact_match_is_skipped():
    state = {"diff_pct": 0.02, "logged_at": NOW}
    soon = NOW + timedelta(seconds=300)
    assert should_log("day_high", 0.02, state, soon, VWAP_TOL, HEARTBEAT_S) is False


def test_day_high_any_distinct_value_is_logged_however_small():
    """The whole safety argument for exact-match collapse is that the set
    of DISTINCT values is preserved exactly — so even a tiny, non-zero
    change must always be logged, unlike vwap's tolerance band."""
    state = {"diff_pct": 0.0200, "logged_at": NOW}
    soon = NOW + timedelta(seconds=300)
    assert should_log("day_high", 0.0201, state, soon, VWAP_TOL, HEARTBEAT_S) is True


def test_day_low_same_exact_match_rule_as_day_high():
    state = {"diff_pct": -0.01, "logged_at": NOW}
    soon = NOW + timedelta(seconds=300)
    assert should_log("day_low", -0.01, state, soon, VWAP_TOL, HEARTBEAT_S) is False
    assert should_log("day_low", -0.015, state, soon, VWAP_TOL, HEARTBEAT_S) is True


def test_vwap_within_tolerance_is_skipped():
    state = {"diff_pct": 0.10, "logged_at": NOW}
    soon = NOW + timedelta(seconds=300)
    assert should_log("vwap", 0.10 + VWAP_TOL - 0.001, state, soon, VWAP_TOL, HEARTBEAT_S) is False


def test_vwap_past_tolerance_is_logged():
    state = {"diff_pct": 0.10, "logged_at": NOW}
    soon = NOW + timedelta(seconds=300)
    assert should_log("vwap", 0.10 + VWAP_TOL + 0.001, state, soon, VWAP_TOL, HEARTBEAT_S) is True


def test_vwap_tolerance_stays_at_most_half_the_tightest_engine_gate():
    """Not a should_log() test per se — a guard against this file's own
    default drifting past the safety margin the migration's rationale
    depends on. The tightest real engine gate is 0.08% (vwr_stop_buffer_pct,
    VWAP_ENGINE_TOLERANCES in tools/quote_parity.py); the shipped default
    (0.04) sits at exactly half of it, chosen deliberately, not a
    coincidence of rounding."""
    from tools.quote_parity import VWAP_ENGINE_TOLERANCES
    tightest = min(default for _, default, _ in VWAP_ENGINE_TOLERANCES)
    assert VWAP_TOL <= tightest / 2, (
        f"collapse tolerance {VWAP_TOL} exceeds half the tightest "
        f"engine gate {tightest} — a collapse could start merging a safe value "
        f"with an unsafe one across the boundary")


def test_heartbeat_forces_a_write_even_with_nothing_changed():
    state = {"diff_pct": 0.02, "logged_at": NOW - timedelta(seconds=HEARTBEAT_S + 1)}
    assert should_log("day_high", 0.02, state, NOW, VWAP_TOL, HEARTBEAT_S) is True


def test_heartbeat_does_not_apply_to_prev_close():
    """prev_close is static all session — even past the heartbeat window,
    it must stay suppressed once logged today; re-checking a value that
    cannot change adds nothing, unlike day_high/day_low/vwap."""
    state = {"diff_pct": -4.18, "logged_at": NOW - timedelta(seconds=HEARTBEAT_S + 1)}
    assert should_log("prev_close", -4.18, state, NOW, VWAP_TOL, HEARTBEAT_S) is False


def test_first_observation_of_the_day_is_always_logged():
    for field in ("day_high", "day_low", "vwap", "prev_close"):
        assert should_log(field, 0.01, None, NOW, VWAP_TOL, HEARTBEAT_S) is True


TESTS = [
    ("volume is never logged under collapse",
     test_volume_is_never_logged_under_collapse),
    ("prev_close logs once then never again that day",
     test_prev_close_logs_once_then_never_again_that_day),
    ("day_high: an exact match is skipped",
     test_day_high_exact_match_is_skipped),
    ("day_high: any distinct value is logged, however small",
     test_day_high_any_distinct_value_is_logged_however_small),
    ("day_low: same exact-match rule as day_high",
     test_day_low_same_exact_match_rule_as_day_high),
    ("vwap: within tolerance is skipped",
     test_vwap_within_tolerance_is_skipped),
    ("vwap: past tolerance is logged",
     test_vwap_past_tolerance_is_logged),
    ("vwap tolerance stays at most half the tightest engine gate",
     test_vwap_tolerance_stays_at_most_half_the_tightest_engine_gate),
    ("heartbeat forces a write even with nothing changed",
     test_heartbeat_forces_a_write_even_with_nothing_changed),
    ("heartbeat does not apply to prev_close",
     test_heartbeat_does_not_apply_to_prev_close),
    ("first observation of the day is always logged",
     test_first_observation_of_the_day_is_always_logged),
]
