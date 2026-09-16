"""
control.position_lifecycle.session_dates() — the calendar every hold-period
rule counts sessions on (stall, fast-fail, time stop, pace calibration).

It used market_regime's rows as the calendar of record, so an evening pipeline
that failed and was never re-run (03-Sep and 11-Sep-2026) deleted a session
from every position's clock. 14-Sep-2026 was an NSE holiday and must stay out.
"""

from __future__ import annotations


class _T:
    def __init__(self, rows):
        self.rows, self.f = rows, []
    def select(self, *_):
        return self
    def gte(self, col, v):
        self.f.append(lambda r: r[col] >= v)
        return self
    def order(self, *_a, **_k):
        return self
    def execute(self):
        rows = sorted((r for r in self.rows if all(f(r) for f in self.f)), key=lambda r: r["date"])
        return type("R", (), {"data": rows})()


REGIME = [{"date": d} for d in ("2026-09-01", "2026-09-02", "2026-09-04", "2026-09-07",
                                "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-15",
                                "2026-09-16")]
HOLIDAYS = [{"date": "2026-08-15"}, {"date": "2026-09-14"}]


class _SB:
    def table(self, name):
        return _T(REGIME if name == "market_regime" else HOLIDAYS)


def test_failed_pipeline_days_still_count_as_sessions():
    from control.position_lifecycle import session_dates
    cal = session_dates(_SB(), "2026-09-01", today="2026-09-17")
    assert "2026-09-03" in cal and "2026-09-11" in cal, cal
    assert "2026-09-14" not in cal, "an NSE holiday is not a session"
    assert "2026-09-12" not in cal and "2026-09-13" not in cal, "weekends are not sessions"


def test_today_is_not_added_before_its_row_exists():
    """The daemon loads the calendar in the morning; today is counted only once
    the evening row exists, exactly as before."""
    from control.position_lifecycle import session_dates
    cal = session_dates(_SB(), "2026-09-01", today="2026-09-17")
    assert "2026-09-17" not in cal and cal[-1] == "2026-09-16", cal[-3:]


def test_sessions_held_across_the_gap():
    from control.position_lifecycle import session_dates, sessions_between
    cal = session_dates(_SB(), "2026-09-01", today="2026-09-17")
    # entered 02-Sep: 03, 04, 07, 08 are four sessions by 08-Sep
    assert sessions_between(cal, "2026-09-02", "2026-09-08") == 4
    # entered 10-Sep: 11, 15, 16 (14th a holiday)
    assert sessions_between(cal, "2026-09-10", "2026-09-16") == 3


def test_no_filling_before_the_first_recorded_session():
    from control.position_lifecycle import session_dates
    cal = session_dates(_SB(), "2026-08-01", today="2026-09-17")
    assert cal[0] == "2026-09-01", cal[:3]


TESTS = [
    ("failed-pipeline days still count as sessions", test_failed_pipeline_days_still_count_as_sessions),
    ("today not added before its row exists", test_today_is_not_added_before_its_row_exists),
    ("sessions held across the 03/11-Sep gap", test_sessions_held_across_the_gap),
    ("no filling before the first recorded session", test_no_filling_before_the_first_recorded_session),
]
