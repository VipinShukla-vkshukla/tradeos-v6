"""
Kite history command line — the parts that read the folder rather than Kite (`status`, `verify`) and the
refusal to run without a valid token. The download itself is covered in test_kite_history.py.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from argparse import Namespace
from datetime import date
from pathlib import Path

import pandas as pd

from tests.test_kite_history import FakeKite, _frame, _run, _target, _weekdays
from tools.kite_history import __main__ as CLI
from tools.kite_history import store


def _capture(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*a)
    return rc, buf.getvalue()


def test_status_reports_coverage_size_and_problem_symbols():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _run(FakeKite({1: (date(2026, 9, 1), None), 2: (date(2027, 1, 1), None)}, refuse={3: "no such instrument"} | {}),
             root, [_target("AAA", 1), _target("BBB", 2)])
        rc, out = _capture(CLI.cmd_status, Namespace(root=str(root)))
        assert rc == 0 and "done" in out and "empty" in out and "GB of Parquet" in out
        rows = 375 * len(_weekdays(date(2026, 9, 1), date(2026, 9, 25)))
        assert f"{rows:,}" in out


def test_status_on_an_empty_folder_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = _capture(CLI.cmd_status, Namespace(root=tmp))
        assert rc == 0 and "nothing downloaded yet" in out


def test_verify_exits_zero_on_clean_data_and_one_on_a_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        days = _weekdays(date(2026, 9, 21), date(2026, 9, 25))
        store.write_frame(root, "minute", "INDICES", "NIFTY 50", _frame(days, 9))
        store.write_frame(root, "minute", "NSE", "AAA", _frame(days, 1))
        args = Namespace(root=str(root), interval="minute", segments="INDICES,NSE", sample=None)
        rc, out = _capture(CLI.cmd_verify, args)
        assert rc == 0 and "0 with hard errors" in out
        good = _frame(days, 1)
        bad = pd.concat([good, good.iloc[:4]], ignore_index=True)          # four duplicate candles, written past the merge
        store._write_atomic(bad, store.path_for(root, "minute", "NSE", "AAA", 2026), False)
        rc, out = _capture(CLI.cmd_verify, args)
        assert rc == 1 and "1 with hard errors" in out and "duplicate_ts" in out


def test_without_a_valid_token_the_cli_refuses_and_says_how_to_fix_it():
    import kite.kite_client as kc
    old = kc.get_kite
    kc.get_kite = lambda: None
    try:
        rc, out = None, io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                CLI._kite()
            except SystemExit as e:
                rc = e.code
        assert rc == 2 and "token_manager --login-url" in out.getvalue() and "--exchange" in out.getvalue()
    finally:
        kc.get_kite = old


def test_the_readme_written_into_the_folder_names_every_limit():
    text = CLI.README.format(root="D:\\kite_history")
    for phrase in ("NOT adjusted", "no tick", "Expired futures", "START of the candle", "read(r\"D:\\kite_history\""):
        assert phrase.lower() in text.lower(), phrase


TESTS = [
    ("status reports coverage, size and problem symbols", test_status_reports_coverage_size_and_problem_symbols),
    ("status on an empty folder says so", test_status_on_an_empty_folder_says_so),
    ("verify exits zero on clean data and one on a corrupt file", test_verify_exits_zero_on_clean_data_and_one_on_a_corrupt_file),
    ("without a valid token the CLI refuses and says how to fix it", test_without_a_valid_token_the_cli_refuses_and_says_how_to_fix_it),
    ("the README written into the folder names every limit", test_the_readme_written_into_the_folder_names_every_limit),
]
