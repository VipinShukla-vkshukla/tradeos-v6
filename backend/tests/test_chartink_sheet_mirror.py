"""
fetch_chartink.main() — a Google Sheets 503 on the mirror write aborted the
whole evening pipeline on 02-Sep and 03-Sep-2026 before the data reached
Supabase. The sheet is a mirror nothing in the code reads.
"""

from __future__ import annotations

import pandas as pd


class _Err(Exception):
    def __init__(self, status):
        super().__init__(f"HttpError {status}")
        self.resp = type("R", (), {"status": status})()


def _patch(mod, sheet_fail_times: int, status: int = 503):
    calls = {"sheet": 0, "upsert": 0, "order": []}
    df = pd.DataFrame({"symbol": ["A", "B"]})

    def write(service, frame):
        calls["sheet"] += 1
        calls["order"].append("sheet")
        if calls["sheet"] <= sheet_fail_times:
            raise _Err(status)

    def upsert(frame):
        calls["upsert"] += 1
        calls["order"].append("upsert")

    saved = {k: getattr(mod, k) for k in ("fetch_chartink_csv", "get_sheets_service",
                                          "write_to_sheet", "upsert_to_supabase",
                                          "CHARTINK_EMAIL", "CHARTINK_PASSWORD")}
    mod.fetch_chartink_csv = lambda: df
    mod.get_sheets_service = lambda: object()
    mod.write_to_sheet = write
    mod.upsert_to_supabase = upsert
    mod.CHARTINK_EMAIL, mod.CHARTINK_PASSWORD = "x", "y"
    mod._SHEET_RETRY_SLEEP = 0
    return calls, saved


def _restore(mod, saved):
    for k, v in saved.items():
        setattr(mod, k, v)


def test_sheet_outage_does_not_stop_the_data():
    import swing.ingestion.fetch_chartink as mod
    calls, saved = _patch(mod, sheet_fail_times=99)
    try:
        n = mod.main()
    finally:
        _restore(mod, saved)
    assert calls["upsert"] == 1 and n == 2, calls
    assert calls["order"][0] == "upsert", f"Supabase must be written before the mirror: {calls['order']}"


def test_transient_sheet_error_is_retried():
    import swing.ingestion.fetch_chartink as mod
    calls, saved = _patch(mod, sheet_fail_times=1)
    try:
        mod.main()
    finally:
        _restore(mod, saved)
    assert calls["sheet"] == 2, calls


TESTS = [
    ("sheet outage does not stop Chartink data reaching Supabase", test_sheet_outage_does_not_stop_the_data),
    ("transient sheet error is retried", test_transient_sheet_error_is_retried),
]
