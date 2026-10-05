"""
A quiet RISK OFF day, and a held position that must still be able to exit.

05-Oct-2026: every shortlist row scored under `min_score_to_show` (top 46.6 vs
a floor of 50), so step 20 wrote no signals. Three things followed, all wrong:

  1. The floor ran before classification, so held DRREDDY (holding_score 25.8,
     exit threshold 30) never reached the exit rules: "weakening" in the log,
     EXIT:0 in the output.
  2. The output audit could not tell "ran, found nothing" from "never ran", so
     C07/C09/C19 paged three red errors on a legitimate day.
  3. send_alerts walked back to the 30-Sep plan and emailed its Tier-1 entry as
     if it were today's.

Each fix is tested in BOTH directions. A check that cannot pass is the defect
this file exists to prevent; a check that cannot fail is its mirror, so every
"tolerated" case has a counterpart that must still be refused.
"""

from __future__ import annotations

import json

TD = "2026-10-05"


# ── a minimal supabase double ────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data, self.count = data, len(data)


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k):  return self
    def limit(self, *a, **k):   return self
    def order(self, key, desc=False):
        self.rows = sorted(self.rows, key=lambda r: str(r.get(key, "")), reverse=desc)
        return self
    def gte(self, *a, **k):     return self
    def lte(self, *a, **k):     return self
    def neq(self, *a, **k):     return self
    def in_(self, *a, **k):     return self

    def eq(self, col, val):
        self.rows = [r for r in self.rows if col not in r or str(r[col])[:10] == str(val)[:10]]
        return self

    def execute(self):
        return _Res(self.rows)


class FakeSB:
    """Tables absent from `tables` hold one generic row, so only the table a
    test cares about can be the thing that is missing."""

    def __init__(self, tables: dict):
        self.tables = tables

    def table(self, name):
        return _Q(list(self.tables.get(name, [{"stub": 1}])))


def _marker(signals: int) -> dict:
    return {"date": TD, "symbol": "__SIGNAL_RUN__",
            "conviction_reason": json.dumps({
                "signals": signals, "msl_rows": 46, "below_floor": 46,
                "min_score": 50, "top_score": 46.6, "regime": "RISK OFF"})}


def _dq():
    from swing.compute import data_quality_monitor as dq
    return dq


# ── 1. the floor is for entries, not for positions we hold ───────────────────

def test_held_name_under_the_floor_is_still_classified():
    from swing.signals.generate_signals import passes_score_floor
    assert passes_score_floor(35.8, 50, in_position=True), \
        "a held name under the floor must still reach the exit rules"
    return True, "held 35.8 < 50 passes"


def test_new_entry_under_the_floor_is_still_refused():
    from swing.signals.generate_signals import passes_score_floor
    assert not passes_score_floor(46.6, 50, in_position=False), \
        "the floor must keep doing its job for NEW entries"
    assert passes_score_floor(50.0, 50, in_position=False)
    assert passes_score_floor(61.0, 50, in_position=False)
    return True, "new 46.6 refused, 50.0 and 61.0 admitted"


def test_drreddy_case_end_to_end_gives_exit():
    """The actual 05-Oct row, through the floor and then the exit rules."""
    from swing.signals.generate_signals import (
        passes_score_floor, classify_open_position_signal)
    row = {"symbol": "DRREDDY", "final_score": 35.8, "holding_score": 25.8}
    assert passes_score_floor(row["final_score"], 50, True)
    sig, state, why = classify_open_position_signal(row, {}, {"holding_exit": 30.0})
    assert (sig, state) == ("EXIT", "OPEN_POSITION"), (sig, state, why)
    return True, f"{sig} ({why})"


# ── 2. the output audit tells "ran, found nothing" from "never ran" ──────────

def test_c09_tolerates_a_recorded_empty_run():
    sb = FakeSB({"ai_context": [_marker(0)]})
    r = _dq().c09_final_picks_validity(sb, TD)
    assert r["ok"] and r["severity"] == "OK", r
    return True, r["message"][:70]


def test_c09_still_fails_with_no_marker():
    sb = FakeSB({"ai_context": []})
    r = _dq().c09_final_picks_validity(sb, TD)
    assert not r["ok"] and r["severity"] == "ERROR", r
    return True, "no marker -> ERROR"


def test_c19_tolerates_a_recorded_empty_run():
    sb = FakeSB({"signal_log": [{"date": "2026-09-30"}], "ai_context": [_marker(0)]})
    r = _dq().c19_signal_date_alignment(sb, TD)
    assert r["ok"], r
    return True, r["message"][:70]


def test_c19_still_fails_when_signals_were_claimed_but_not_written():
    """Marker says 3 signals, signal_log has none for the date: a real fault."""
    sb = FakeSB({"signal_log": [{"date": "2026-09-30"}], "ai_context": [_marker(3)]})
    r = _dq().c19_signal_date_alignment(sb, TD)
    assert not r["ok"] and r["severity"] == "ERROR", r
    return True, "marker with 3 signals but no rows -> ERROR"


def test_c19_still_fails_with_no_marker():
    sb = FakeSB({"signal_log": [{"date": "2026-09-30"}], "ai_context": []})
    r = _dq().c19_signal_date_alignment(sb, TD)
    assert not r["ok"] and r["severity"] == "ERROR", r
    return True, "no marker -> ERROR"


def test_c07_tolerates_only_the_signals_step_on_an_empty_run():
    ok_sb = FakeSB({"signal_log": [], "ai_context": [_marker(0)]})
    r = _dq().c07_pipeline_completeness(ok_sb, TD)
    assert "20_signals" not in str(r["affected"]) and "signal_log" not in r["affected"], r

    bad_sb = FakeSB({"signal_log": [], "ai_context": [_marker(0)], "master_shortlist": []})
    r2 = _dq().c07_pipeline_completeness(bad_sb, TD)
    assert "master_shortlist" in r2["affected"] and not r2["ok"], \
        "an empty-run marker must excuse step 20 ONLY, not every missing table"
    return True, "step 20 excused; master_shortlist still reported"


def test_c07_still_fails_with_no_marker():
    sb = FakeSB({"signal_log": [], "ai_context": []})
    r = _dq().c07_pipeline_completeness(sb, TD)
    assert "signal_log" in r["affected"] and r["severity"] == "ERROR", r
    return True, "no marker -> signal_log missing, ERROR"


# ── 3. a stale plan is not presented as today's ──────────────────────────────

def _data(signal_date):
    return {"signal_date": signal_date, "display_date": TD,
            "signals": [{"symbol": "X", "signal_type": "BUY_CANDIDATE"}],
            "final_picks": {"ranked_candidates": [{"symbol": "X"}]},
            "open_pos": [{"symbol": "DRREDDY"}], "regime": {"regime": "RISK OFF"}}


def test_session_day_drops_an_older_plan_but_keeps_positions():
    from alerts.send_alerts import suppress_stale_plan
    d = suppress_stale_plan(_data("2026-09-30"), TD, is_session=True)
    assert d["signals"] == [] and d["final_picks"] is None
    assert d["stale_signal_date"] == "2026-09-30"
    assert d["open_pos"] and d["regime"], "positions and regime are live; keep them"
    return True, "signals/picks dropped, positions kept"


def test_todays_plan_is_untouched():
    from alerts.send_alerts import suppress_stale_plan
    d = suppress_stale_plan(_data(TD), TD, is_session=True)
    assert len(d["signals"]) == 1 and "stale_signal_date" not in d
    return True, "same-date plan passes through"


def test_weekend_run_keeps_the_last_plan():
    from alerts.send_alerts import suppress_stale_plan
    d = suppress_stale_plan(_data("2026-10-01"), "2026-10-04", is_session=False)
    assert len(d["signals"]) == 1, "no session today: showing the last plan is correct"
    return True, "non-session day unchanged"


def test_the_loop_actually_uses_the_floor_helper():
    """A correct helper nobody calls fixes nothing (the direction-spine lesson):
    assert through the CALL SITE, and that the old bare comparison is gone."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "swing/signals/generate_signals.py").read_text()
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    assert "passes_score_floor(score, min_score, in_pos)" in src, \
        "generate() does not route the floor through passes_score_floor"
    assert not re.search(r"if\s+score\s*<\s*min_score\s*:", src), \
        "the bare `if score < min_score: continue` is back — it skips held names"
    assert "record_signal_run(sb, run_date_str" in src, \
        "generate() never leaves the __SIGNAL_RUN__ marker"
    return True, "call sites present, bare floor gone"


TESTS = [
    ("generate() routes the floor through the helper and leaves a marker", test_the_loop_actually_uses_the_floor_helper),
    ("held name under the floor still reaches exit rules", test_held_name_under_the_floor_is_still_classified),
    ("new entry under the floor still refused",            test_new_entry_under_the_floor_is_still_refused),
    ("DRREDDY 05-Oct end to end gives EXIT",               test_drreddy_case_end_to_end_gives_exit),
    ("C09 tolerates a recorded empty run",                 test_c09_tolerates_a_recorded_empty_run),
    ("C09 still fails with no marker",                     test_c09_still_fails_with_no_marker),
    ("C19 tolerates a recorded empty run",                 test_c19_tolerates_a_recorded_empty_run),
    ("C19 still fails when signals claimed but unwritten", test_c19_still_fails_when_signals_were_claimed_but_not_written),
    ("C19 still fails with no marker",                     test_c19_still_fails_with_no_marker),
    ("C07 excuses step 20 only",                           test_c07_tolerates_only_the_signals_step_on_an_empty_run),
    ("C07 still fails with no marker",                     test_c07_still_fails_with_no_marker),
    ("session day drops an older plan, keeps positions",   test_session_day_drops_an_older_plan_but_keeps_positions),
    ("today's plan untouched",                             test_todays_plan_is_untouched),
    ("weekend run keeps the last plan",                    test_weekend_run_keeps_the_last_plan),
]
