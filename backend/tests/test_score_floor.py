"""
The min_score_to_show floor: held names bypass it, and an all-below day is named.

WHAT THIS CATCHES
-----------------
On 05-Oct-2026 `generate_signals` emitted zero signals of ANY type from a 46-row
shortlist (max final_score 46.6 against a floor of 50). `if score < min_score:
continue` ran before a held name was classified, so:

1. COALINDIA (41.2) and DRREDDY (35.8), both held, never reached
   `classify_open_position_signal` - which reads holding_score/lifecycle, not
   final_score - and their EXIT/REDUCE alerts could not be raised. The weaker the
   holding, the more certainly it was dropped.
2. The step reported OK, the audit said "step 15 wrote the wrong date", and the
   dashboard said "Stale ... Run: python run_pipeline.py". Re-running reproduces
   the same empty result. Three honest-looking messages, none of them the cause.

The audit must still ERROR on genuine staleness. A check that cannot FAIL is not
a check, so the stale cases below are asserted as ERROR, not just the new WARN.
"""

from __future__ import annotations

from pathlib import Path

from tests import cfg_ctx

from swing.signals.score_floor import passes_score_floor, floor_state, describe_all_below
from swing.compute.data_quality_monitor import c19_evaluate, c19_signal_date_alignment

FLOOR = 50.0
# The shortlist as it stood on 2026-10-05 (summary stats only: 46 rows, max 46.6).
OCT5 = [46.6, 41.2, 35.8] + [20.0] * 43


def test_held_name_below_floor_still_passes():
    assert passes_score_floor(41.2, FLOOR, held=True)
    assert passes_score_floor(0.0, FLOOR, held=True)


def test_unheld_name_is_still_floored():
    # The floor must keep working for candidates, or the fix is just "no floor".
    assert not passes_score_floor(46.6, FLOOR, held=False)
    assert passes_score_floor(50.0, FLOOR, held=False)      # boundary: >= passes
    assert passes_score_floor(54.2, FLOOR, held=False)      # 29-Sep: the bar is clearable


def test_all_below_detected_on_oct5_shortlist():
    s = floor_state(OCT5, FLOOR)
    assert s["all_below"] and s["total"] == 46 and s["below"] == 46
    assert s["max_score"] == 46.6


def test_one_row_at_floor_is_not_all_below():
    assert not floor_state(OCT5 + [50.0], FLOOR)["all_below"]


def test_empty_shortlist_is_not_explained_as_below_floor():
    # Nothing screened is a different fault; it must stay an ERROR upstream.
    s = floor_state([], FLOOR)
    assert not s["all_below"] and s["max_score"] is None


def test_message_names_the_cause_and_says_rerun_is_futile():
    m = describe_all_below(floor_state(OCT5, FLOOR))
    assert "46.6" in m and "50" in m and "will not change" in m


def test_c19_aligned_is_ok():
    ok, sev, _ = c19_evaluate("2026-10-05", "2026-10-05", None)
    assert ok and sev == "OK"


def test_c19_lag_explained_by_floor_is_warn():
    ok, sev, msg = c19_evaluate("2026-09-30", "2026-10-05", floor_state(OCT5, FLOOR))
    assert not ok and sev == "WARN" and "min_score_to_show" in msg


def test_c19_genuine_staleness_still_errors():
    # Shortlist HAS rows that clear the floor, yet signals lag: a real fault.
    st = floor_state([61.0, 20.0], FLOOR)
    ok, sev, msg = c19_evaluate("2026-09-30", "2026-10-05", st)
    assert not ok and sev == "ERROR" and "step 20" in msg


def test_c19_empty_shortlist_with_lag_still_errors():
    ok, sev, _ = c19_evaluate("2026-09-30", "2026-10-05", floor_state([], FLOOR))
    assert not ok and sev == "ERROR"


# ── Through the consumer's own entry point, with a fake client ───────────────

class _Q:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self):
        class R: pass
        r = R(); r.data = self._rows; return r


class _SB:
    def __init__(self, sig_date, msl_scores):
        self._t = {"signal_log": [{"date": sig_date}],
                   "master_shortlist": [{"final_score": s} for s in msl_scores]}
    def table(self, name): return _Q(self._t[name])


def test_c19_end_to_end_reads_floor_from_config():
    with cfg_ctx({"min_score_to_show": "50"}):
        r = c19_signal_date_alignment(_SB("2026-09-30", OCT5), "2026-10-05")
        assert r["severity"] == "WARN", r
        r = c19_signal_date_alignment(_SB("2026-09-30", [61.0]), "2026-10-05")
        assert r["severity"] == "ERROR", r
    with cfg_ctx({"min_score_to_show": "40"}):
        # Lower floor: 46.6 clears it, so the same lag is no longer "explained".
        r = c19_signal_date_alignment(_SB("2026-09-30", OCT5), "2026-10-05")
        assert r["severity"] == "ERROR", r


def test_generate_signals_applies_the_floor_through_the_helper():
    # A correct helper proves nothing about its caller: assert the call site.
    src = (Path(__file__).resolve().parent.parent
           / "swing/signals/generate_signals.py").read_text(encoding="utf-8")
    assert "passes_score_floor(score, min_score, in_pos)" in src
    assert "if score < min_score:" not in src


TESTS = [
    ("held name below the floor still passes", test_held_name_below_floor_still_passes),
    ("unheld name is still floored", test_unheld_name_is_still_floored),
    ("all-below detected on the 05-Oct shortlist", test_all_below_detected_on_oct5_shortlist),
    ("one row at the floor is not all-below", test_one_row_at_floor_is_not_all_below),
    ("empty shortlist is not explained as below-floor", test_empty_shortlist_is_not_explained_as_below_floor),
    ("message names the cause and says rerun is futile", test_message_names_the_cause_and_says_rerun_is_futile),
    ("C19 aligned is OK", test_c19_aligned_is_ok),
    ("C19 lag explained by the floor is WARN", test_c19_lag_explained_by_floor_is_warn),
    ("C19 genuine staleness still ERRORs", test_c19_genuine_staleness_still_errors),
    ("C19 empty shortlist with lag still ERRORs", test_c19_empty_shortlist_with_lag_still_errors),
    ("C19 end to end reads the floor from config", test_c19_end_to_end_reads_floor_from_config),
    ("generate_signals applies the floor via the helper", test_generate_signals_applies_the_floor_through_the_helper),
]
