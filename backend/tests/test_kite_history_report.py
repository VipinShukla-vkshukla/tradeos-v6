"""
Coverage/integrity report — tools/kite_history/report.py. Built on a fake-Kite download so the numbers can be
worked out by hand.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from tests.test_kite_history import FakeKite, _run, _target, _weekdays
from tools.kite_history import report, store


def _download(root: Path):
    series = {1: (date(2025, 3, 10), None), 2: (date(2026, 9, 1), None), 3: (date(2027, 1, 1), None)}
    _run(FakeKite(series), root, [_target("AAA", 1), _target("BBB", 2), _target("CCC", 3)])


def test_coverage_counts_rows_years_and_what_was_not_stored():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _download(root)
        rep = report.build(root, "minute")
        nse = rep["segments"]["NSE"]
        rows = 375 * (len(_weekdays(date(2025, 3, 10), date(2026, 9, 25))) + len(_weekdays(date(2026, 9, 1), date(2026, 9, 25))))
        assert nse["instruments"] == 3 and nse["done"] == 2 and nse["rows"] == rows
        assert nse["not_done"] == {"empty": 1}
        assert nse["starts_by_year"] == {2025: 1, 2026: 1}
        assert nse["history_years_max"] == 1.5 and nse["earliest"].startswith("2025-03-10 09:15")
        assert report.folder_gb(root, "minute", "NSE") > 0 and rep["status"] == {"done": 2, "empty": 1}


def test_the_integrity_pass_is_clean_on_a_clean_download_and_names_a_corrupt_symbol():
    import pandas as pd
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _download(root)
        rep = report.build(root, "minute", with_integrity=True)
        i = rep["integrity"]
        assert i["checked"] == 2 and i["hard_errors"] == {} and i["usual_candles_per_day"] == {375: 2}
        assert i["total_rows"] == rep["segments"]["NSE"]["rows"]
        p = store.files_for(root, "minute", "NSE", "BBB")[0]
        good = store.read_file(p)
        store._write_atomic(pd.concat([good, good.iloc[:5]], ignore_index=True), p, False)
        i2 = report.build(root, "minute", with_integrity=True)["integrity"]
        assert list(i2["hard_errors"]) == ["NSE/BBB"]
        assert i2["hard_errors"]["NSE/BBB"] == {"duplicate_ts": 5, "unsorted": 1}, "five repeated candles appended out of order"


def test_the_printed_report_carries_the_headline_numbers():
    import contextlib
    import io
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _download(root)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.print_report(report.build(root, "minute", with_integrity=True))
        text = buf.getvalue()
        for phrase in ("NSE: 2/3 done", "candles", "GB on disk", "median", "integrity: 2 symbols checked, 0 with hard errors"):
            assert phrase in text, phrase


TESTS = [
    ("coverage counts rows, years and what was not stored", test_coverage_counts_rows_years_and_what_was_not_stored),
    ("the integrity pass is clean on a clean download and names a corrupt symbol", test_the_integrity_pass_is_clean_on_a_clean_download_and_names_a_corrupt_symbol),
    ("the printed report carries the headline numbers", test_the_printed_report_carries_the_headline_numbers),
]
