"""
F-86, 25-Aug-2026 — the standing check for the HINDCOPPER shape (F-84/
F-85): a closed CNC position whose entry_date coincides with a same-day
QTY_REDUCED/QTY_INCREASED reconcile event for that symbol. That incident
was found only because the operator happened to compare the dashboard
against a broker export by hand, weeks later — nothing in tools.health
would have surfaced it before this. `check_same_day_reconcile_drift()`
does not correct anything (F-85's own finding: the "correct" number can
be legitimate broker-side cost-basis movement this codebase cannot
safely auto-resolve) — it only flags a row as worth a manual check
against Kite's own Holdings page, matching this project's "propose,
never auto-apply" rule.
"""

from __future__ import annotations

from unittest.mock import patch


# ── _same_day_drift_symbols(), pure ─────────────────────────────────────────

def test_the_real_hindcopper_shape_is_flagged():
    """The exact incident this check exists for: entry_date 2026-08-24
    coincides with two QTY_REDUCED events for HINDCOPPER that same IST
    day (run_at in UTC, must be converted before comparing)."""
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [
        {"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
         "run_at": "2026-08-24T04:05:55+00:00"},   # 09:35 IST, same day
        {"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
         "run_at": "2026-08-24T04:11:03+00:00"},
    ]
    closed_rows = [
        {"id": 13152, "symbol": "HINDCOPPER", "entry_date": "2026-08-24",
         "exit_date": "2026-08-25"},
    ]
    flagged = _same_day_drift_symbols(reconcile_rows, closed_rows)
    assert len(flagged) == 1
    assert flagged[0]["id"] == 13152
    assert flagged[0]["symbol"] == "HINDCOPPER"
    assert set(flagged[0]["reconcile_actions"]) == {"QTY_REDUCED"}


def test_a_qty_increased_event_also_flags():
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "HAL", "action": "QTY_INCREASED",
                       "run_at": "2026-08-20T05:00:00+00:00"}]
    closed_rows = [{"id": 1, "symbol": "HAL", "entry_date": "2026-08-20",
                    "exit_date": "2026-08-21"}]
    assert len(_same_day_drift_symbols(reconcile_rows, closed_rows)) == 1


def test_a_different_symbol_is_not_flagged():
    """AARTIIND's own control case from F-84's write-up — a symbol with
    real reconcile drift on the SAME day but for a DIFFERENT ticker must
    not cross-contaminate."""
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
                       "run_at": "2026-08-24T04:05:55+00:00"}]
    closed_rows = [{"id": 99, "symbol": "AARTIIND", "entry_date": "2026-08-24",
                    "exit_date": "2026-08-25"}]
    assert _same_day_drift_symbols(reconcile_rows, closed_rows) == []


def test_a_different_day_is_not_flagged():
    """Same symbol, but the reconcile drift happened on a day OTHER than
    this position's own entry_date — ordinary mid-hold reconciliation,
    not the entry-fill-visibility problem this check targets."""
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
                       "run_at": "2026-08-21T05:00:00+00:00"}]
    closed_rows = [{"id": 1, "symbol": "HINDCOPPER", "entry_date": "2026-08-24",
                    "exit_date": "2026-08-25"}]
    assert _same_day_drift_symbols(reconcile_rows, closed_rows) == []


def test_a_qty_reduced_within_the_normal_partial_exit_window_is_not_flagged():
    """A LEGITIMATE partial exit (booked days after entry, no same-day
    fill-visibility ambiguity) must not be flagged — this check is
    specifically about the ENTRY day, not every reconcile event a
    position ever has."""
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "PPLPHARMA", "action": "QTY_REDUCED",
                       "run_at": "2026-08-10T05:00:00+00:00"}]
    closed_rows = [{"id": 1, "symbol": "PPLPHARMA", "entry_date": "2026-07-31",
                    "exit_date": "2026-08-20"}]
    assert _same_day_drift_symbols(reconcile_rows, closed_rows) == []


def test_opened_and_closed_actions_are_ignored():
    """Only QTY_REDUCED/QTY_INCREASED indicate a settlement-lag drift —
    OPENED and CLOSED are ordinary lifecycle events, not evidence
    entry_price might be unreliable."""
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [
        {"symbol": "HINDCOPPER", "action": "OPENED", "run_at": "2026-08-24T04:00:00+00:00"},
        {"symbol": "HINDCOPPER", "action": "CLOSED", "run_at": "2026-08-24T04:00:00+00:00"},
    ]
    closed_rows = [{"id": 1, "symbol": "HINDCOPPER", "entry_date": "2026-08-24",
                    "exit_date": "2026-08-25"}]
    assert _same_day_drift_symbols(reconcile_rows, closed_rows) == []


def test_an_unreadable_run_at_is_skipped_not_a_crash():
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
                       "run_at": "not a timestamp"}]
    closed_rows = [{"id": 1, "symbol": "HINDCOPPER", "entry_date": "2026-08-24",
                    "exit_date": "2026-08-25"}]
    assert _same_day_drift_symbols(reconcile_rows, closed_rows) == []


def test_multiple_closed_rows_are_each_checked_independently():
    from tools.health import _same_day_drift_symbols
    reconcile_rows = [{"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
                       "run_at": "2026-08-24T04:05:55+00:00"}]
    closed_rows = [
        {"id": 1, "symbol": "HINDCOPPER", "entry_date": "2026-08-24", "exit_date": "2026-08-25"},
        {"id": 2, "symbol": "AARTIIND", "entry_date": "2026-08-17", "exit_date": "2026-08-25"},
    ]
    flagged = _same_day_drift_symbols(reconcile_rows, closed_rows)
    assert len(flagged) == 1
    assert flagged[0]["id"] == 1


# ── check_same_day_reconcile_drift(), the impure wrapper ────────────────────

class _Query:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def execute(self):
        class R:
            pass
        r = R()
        r.data = self._rows
        return r


class _FakeSB:
    def __init__(self, closed_rows, reconcile_rows):
        self._closed = closed_rows
        self._reconcile = reconcile_rows
    def table(self, name):
        if name == "closed_positions":
            return _Query(self._closed)
        if name == "position_reconcile_log":
            return _Query(self._reconcile)
        raise AssertionError(f"unexpected table {name}")


def test_check_fails_when_a_row_is_flagged():
    from tools.health import check_same_day_reconcile_drift
    closed = [{"id": 13152, "symbol": "HINDCOPPER", "entry_date": "2026-08-24",
              "exit_date": "2026-08-25", "product": "CNC"}]
    reconcile = [{"symbol": "HINDCOPPER", "action": "QTY_REDUCED",
                 "run_at": "2026-08-24T04:05:55+00:00"}]
    with patch(
            "config.get_supabase", return_value=_FakeSB(closed, reconcile)):
        ok, why = check_same_day_reconcile_drift()
    assert ok is False
    assert "HINDCOPPER" in why
    assert "id=13152" in why


def test_check_passes_when_nothing_is_flagged():
    from tools.health import check_same_day_reconcile_drift
    closed = [{"id": 1, "symbol": "AARTIIND", "entry_date": "2026-08-17",
              "exit_date": "2026-08-25", "product": "CNC"}]
    reconcile: list[dict] = []
    with patch(
            "config.get_supabase", return_value=_FakeSB(closed, reconcile)):
        ok, why = check_same_day_reconcile_drift()
    assert ok is True


def test_check_passes_with_no_recent_closed_positions():
    from tools.health import check_same_day_reconcile_drift
    with patch(
            "config.get_supabase", return_value=_FakeSB([], [])):
        ok, why = check_same_day_reconcile_drift()
    assert ok is True
    assert "no CNC" in why


TESTS = [
    ("the real HINDCOPPER shape is flagged", test_the_real_hindcopper_shape_is_flagged),
    ("a QTY_INCREASED event also flags", test_a_qty_increased_event_also_flags),
    ("a different symbol is not flagged", test_a_different_symbol_is_not_flagged),
    ("a different day is not flagged", test_a_different_day_is_not_flagged),
    ("a normal-window partial exit is not flagged",
     test_a_qty_reduced_within_the_normal_partial_exit_window_is_not_flagged),
    ("OPENED/CLOSED actions are ignored", test_opened_and_closed_actions_are_ignored),
    ("an unreadable run_at is skipped, not a crash",
     test_an_unreadable_run_at_is_skipped_not_a_crash),
    ("multiple closed rows are each checked independently",
     test_multiple_closed_rows_are_each_checked_independently),
    ("check fails when a row is flagged", test_check_fails_when_a_row_is_flagged),
    ("check passes when nothing is flagged", test_check_passes_when_nothing_is_flagged),
    ("check passes with no recent closed positions",
     test_check_passes_with_no_recent_closed_positions),
]
