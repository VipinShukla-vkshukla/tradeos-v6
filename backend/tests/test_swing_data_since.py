"""
swing_data_since — the operator's cutoff (17-Sep-2026: 2026-09-01) for swing
history that the learning loop and the analysis tools may read. Nothing is
deleted; every reader that LEARNS from swing history filters at the query.
Operational readers (reconcile, telegram, backup, order paths) are untouched.
"""

from __future__ import annotations

from tests import cfg_ctx


class _Rec:
    def __init__(self, log, table):
        self.log, self.table = log, table
    @property
    def not_(self):
        return self
    def gte(self, col, v):
        self.log.append((self.table, col, v))
        return self
    def execute(self):
        return type("R", (), {"data": []})()
    def __getattr__(self, name):
        return lambda *a, **k: self


class _SB:
    def __init__(self):
        self.log = []
    def table(self, name):
        return _Rec(self.log, name)


SINCE = {"swing_data_since": "2026-09-01"}


def _filters(fn, flags):
    sb = _SB()
    with cfg_ctx(flags):
        try:
            fn(sb)
        except Exception:
            pass
    return sb.log


def _readers():
    from allocation.scoring import swing_priors, expected_hold_days
    from allocation.swing_hold_days import expected_hold_days_by_family
    from swing.signals.pace_calibration import build_family_stall_days
    return [
        ("swing_priors", swing_priors, ("signal_output_daily", "date")),
        ("expected_hold_days(SWING)", lambda sb: expected_hold_days(sb, "SWING"),
         ("closed_positions", "entry_date")),
        ("expected_hold_days_by_family", expected_hold_days_by_family,
         ("closed_positions", "entry_date")),
    ]


def test_learning_readers_apply_the_cutoff():
    missing = []
    for name, fn, (table, col) in _readers():
        if (table, col, "2026-09-01") not in _filters(fn, SINCE):
            missing.append(name)
    assert not missing, f"readers ignoring swing_data_since: {missing}"


def test_no_cutoff_when_unset():
    for name, fn, (table, col) in _readers():
        assert (table, col, "2026-09-01") not in _filters(fn, {}), name


def test_intraday_hold_days_unaffected():
    from allocation.scoring import expected_hold_days
    log = _filters(lambda sb: expected_hold_days(sb, "INTRADAY"), SINCE)
    assert not any(v == "2026-09-01" for _t, _c, v in log), log


def test_stall_clock_calibration_keeps_full_history():
    """From 01-Sep only it would cut CONTINUATION's clock from 8 to 3 sessions on one tape."""
    from swing.signals.pace_calibration import build_family_stall_days
    log = _filters(lambda sb: build_family_stall_days(sb, global_default=10), SINCE)
    assert not any(v == "2026-09-01" for _t, _c, v in log), log


TESTS = [
    ("stall-clock calibration keeps full history", test_stall_clock_calibration_keeps_full_history),
    ("swing learning readers apply swing_data_since", test_learning_readers_apply_the_cutoff),
    ("no cutoff when swing_data_since is unset", test_no_cutoff_when_unset),
    ("intraday hold-days estimate is not cut", test_intraday_hold_days_unaffected),
]
