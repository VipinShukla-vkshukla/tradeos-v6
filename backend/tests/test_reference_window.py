"""
Excluded history stays VISIBLE as a labelled reference. swing_data_since cuts
what the system LEARNS from; without a reference column every analysis number
describes one falling market (25-Jun..08-Sep: 560 plans before the cutoff, 82
after), and a weak strategy cannot be told from a weak market.
"""

from __future__ import annotations

from tests import cfg_ctx

CUT = {"swing_data_since": "2026-09-01"}


def test_replay_windows_split_at_the_cutoff():
    from tools.swing_fix_replay import split_date, window_for
    with cfg_ctx(CUT):
        assert split_date() == "2026-09-01"
        assert window_for("2026-09-04") == "current"
        assert window_for("2026-08-31") == "reference"


def test_replay_window_without_a_cutoff_is_unchanged():
    from tools.swing_fix_replay import split_date, window_for
    with cfg_ctx({}):
        assert split_date() == "2026-08-14"
        assert window_for("2026-08-20") == "current"


def test_weekly_review_reference_reads_only_pre_cutoff():
    """The reference block must filter to BEFORE the cutoff, never mix."""
    from tools.weekly_review import swing_reference_block

    log = []

    class _Q:
        @property
        def not_(self):
            return self
        def lt(self, col, v):
            log.append(("lt", col, v))
            return self
        def gte(self, col, v):
            log.append(("gte", col, v))
            return self
        def execute(self):
            return type("R", (), {"data": []})()
        def __getattr__(self, _n):
            return lambda *a, **k: self

    sb = type("SB", (), {"table": lambda self, _n: _Q()})()
    with cfg_ctx(CUT):
        swing_reference_block(sb)
    assert ("lt", "date", "2026-09-01") in log, log
    assert not any(f[0] == "gte" and f[2] == "2026-09-01" for f in log), log


def test_weekly_review_reference_skipped_without_a_cutoff():
    from tools.weekly_review import swing_reference_block

    called = []

    class _SB:
        def table(self, _n):
            called.append(_n)
            raise AssertionError("must not query when no cutoff is set")

    with cfg_ctx({}):
        swing_reference_block(_SB())
    assert not called


TESTS = [
    ("replay windows split at the cutoff", test_replay_windows_split_at_the_cutoff),
    ("replay window without a cutoff is unchanged", test_replay_window_without_a_cutoff_is_unchanged),
    ("weekly_review reference reads only pre-cutoff", test_weekly_review_reference_reads_only_pre_cutoff),
    ("weekly_review reference skipped without a cutoff", test_weekly_review_reference_skipped_without_a_cutoff),
]
