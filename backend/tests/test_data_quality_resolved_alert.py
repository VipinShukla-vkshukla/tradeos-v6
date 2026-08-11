"""
11-Aug-2026: a cron run failed with real C01/C06 ERRORs and alerted on
Telegram. The operator manually reran the pipeline; it succeeded.
data_anomalies correctly went back to clean the moment the rerun's checks
passed (main()'s delete-then-insert already means "the latest verdict" —
see that comment's own history, 27-Jul-2026). But nothing ever told
TELEGRAM. _send_error_alert() only fires `if errors:`, so the clean rerun
correctly sent nothing new — and correctly-sent-nothing looked, from the
chat, identical to "the pipeline is still broken and nobody has checked".

This pins the fix: main() now reads which checks were ERROR for today
BEFORE the delete-then-insert supersedes them, and fires a RESOLVED
follow-up specifically on the transition (errors before, none now) — never
on an ordinary already-clean run, and never alongside a fresh error alert.
"""

from __future__ import annotations

from unittest.mock import patch


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Just enough of a Supabase query builder to drive data_anomalies
    through main()'s read-prior / delete / insert sequence."""
    def __init__(self, store, table):
        self.store, self.table = store, table
        self._op, self._filters, self._rows = None, [], None

    def select(self, *_a):
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def delete(self):
        self._op = "delete"
        return self

    def insert(self, rows):
        self._op, self._rows = "insert", rows
        return self

    def execute(self):
        if self.table != "data_anomalies":
            return _Result([])
        date = next((v for c, v in self._filters if c == "date"), None)
        if self._op == "select":
            sev = next((v for c, v in self._filters if c == "severity"), None)
            rows = self.store.get(date, [])
            if sev:
                rows = [r for r in rows if r.get("severity") == sev]
            return _Result(rows)
        if self._op == "delete":
            self.store[date] = []
            return _Result([])
        if self._op == "insert":
            for r in self._rows:
                self.store.setdefault(r["date"], []).append(r)
            return _Result([])
        return _Result([])


class _StubSB:
    def __init__(self, seed_errors_for_date: dict[str, list[str]] | None = None):
        self.store: dict[str, list[dict]] = {}
        for date, checks in (seed_errors_for_date or {}).items():
            self.store[date] = [{"check_name": c, "severity": "ERROR"} for c in checks]

    def table(self, name):
        return _Query(self.store, name)


def _fake_check(name: str, severity: str):
    """A synthetic check with data_quality_monitor's own (sb, td) -> result
    contract, ignoring both arguments — this test is about main()'s
    alert-branching, not about any individual check's own logic."""
    from swing.compute.data_quality_monitor import _result
    ok = severity == "OK"
    def fn(sb, td):
        return _result(name, ok, "ERROR" if not ok else "ERROR", f"{name} synthetic {severity}")
    return fn


def _run_main_with_one_check(sb, severity: str, td: str = "2026-08-11"):
    import swing.compute.data_quality_monitor as dqm
    check = {"C01": ("synthetic", _fake_check("C01_synthetic", severity))}
    with patch.object(dqm, "get_supabase", return_value=sb), \
         patch.object(dqm, "get_trade_date", return_value=td), \
         patch.object(dqm, "is_kill_switch_active", return_value=False), \
         patch.object(dqm, "INPUT_CHECKS", check), \
         patch.object(dqm, "OUTPUT_CHECKS", {}), \
         patch("alerts.send_alerts.send_message") as mock_send:
        dqm.main(phase="all")
    return mock_send


def test_resolved_alert_fires_when_a_prior_error_clears():
    sb = _StubSB(seed_errors_for_date={"2026-08-11": ["C01_chartink_row_count"]})
    mock_send = _run_main_with_one_check(sb, "OK")
    assert mock_send.call_count == 1, f"expected exactly one Telegram send, got {mock_send.call_count}"
    text = mock_send.call_args[0][0]
    assert "Resolved" in text and "C01_chartink_row_count" in text, text


def test_resolved_alert_does_not_fire_on_an_ordinary_clean_run():
    """No prior ERROR for today — a routine clean run has nothing to
    report and must stay silent, exactly as before this change."""
    sb = _StubSB()
    mock_send = _run_main_with_one_check(sb, "OK")
    assert mock_send.call_count == 0, "an ordinary clean run must not send anything"


def test_error_alert_fires_and_resolved_does_not_when_still_broken():
    """Errors both before and after this run: only the (existing) error
    alert should fire, never a spurious 'resolved' alongside it."""
    sb = _StubSB(seed_errors_for_date={"2026-08-11": ["C01_chartink_row_count"]})
    mock_send = _run_main_with_one_check(sb, "ERROR")
    assert mock_send.call_count == 1, f"expected exactly one Telegram send, got {mock_send.call_count}"
    text = mock_send.call_args[0][0]
    assert "Resolved" not in text
    assert "ERROR" in text.upper() or "❌" in text


def test_send_resolved_alert_message_names_every_cleared_check():
    from swing.compute.data_quality_monitor import _send_resolved_alert
    prior = [{"check_name": "C01_chartink_row_count"}, {"check_name": "C06_msl_completeness"}]
    with patch("alerts.send_alerts.send_message") as mock_send:
        _send_resolved_alert(prior, "2026-08-11")
    assert mock_send.call_count == 1
    text = mock_send.call_args[0][0]
    assert "C01_chartink_row_count" in text and "C06_msl_completeness" in text


def test_send_resolved_alert_does_not_raise_on_empty_input():
    from swing.compute.data_quality_monitor import _send_resolved_alert
    with patch("alerts.send_alerts.send_message") as mock_send:
        _send_resolved_alert([], "2026-08-11")
    assert mock_send.call_count == 1  # still sends -- caller (main) is what gates on non-empty


TESTS = [
    ("resolved alert fires when a prior error clears",
     test_resolved_alert_fires_when_a_prior_error_clears),
    ("resolved alert does not fire on an ordinary clean run",
     test_resolved_alert_does_not_fire_on_an_ordinary_clean_run),
    ("error alert fires and resolved does not when still broken",
     test_error_alert_fires_and_resolved_does_not_when_still_broken),
    ("send_resolved_alert message names every cleared check",
     test_send_resolved_alert_message_names_every_cleared_check),
    ("send_resolved_alert does not raise on empty input",
     test_send_resolved_alert_does_not_raise_on_empty_input),
]
