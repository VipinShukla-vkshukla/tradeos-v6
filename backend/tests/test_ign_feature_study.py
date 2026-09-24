"""
IGN feature study glue — tools/replay/ign_feature_study.py.

The statistics are tested in test_ign_feature_stats. This pins what surrounds
them: every symbol-day leaves a record with a status (a silent gap is a lie in the
denominator), the real detection-and-R path on a synthetic session, that nothing
dated on or after the replay day can reach `prev` or the panel (lookahead), the
holdout preflight refusing to reveal, and that a re-run resumes rather than repeats.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from config import IST
from tests import cfg_ctx
from tests._fixtures import OPEN, bars as _bars

DAY = "2026-08-05"


def _mod():
    from tools.replay import ign_feature_study as X
    return X


def _raw(n: int = 40, *, end=date(2026, 8, 4), close=100.0, volume=3_000_000.0):
    return [{"date": end - timedelta(days=n - 1 - i), "open": close, "high": close + 1.0,
             "low": close - 1.0, "close": close, "volume": volume} for i in range(n)]


class _Src:
    def __init__(self, day_bars):
        self.day_bars = day_bars

    def get(self, sym, day):
        return self.day_bars


def _spike_day():
    """75 quiet minutes, a 4.5% spike on rising volume, 25 tight minutes near the
    top (so the 20-bar stop window is tight), then a run to the 1.5R target."""
    seq = [(100.0, 100.3, 99.7, 100.0, 20_000)] * 75
    seq += [(100.0 + i, 100.6 + i, 99.9 + i, 100.9 + i, 40_000) for i in range(4)]
    seq += [(104.5, 104.7, 104.3, 104.5 + (i % 2) * 0.05, 30_000) for i in range(25)]
    seq += [(104.6 + 0.4 * i, 105.0 + 0.4 * i, 104.5 + 0.4 * i, 104.9 + 0.4 * i, 30_000)
            for i in range(12)]
    return _bars(seq)


def _quiet_day():
    return _bars([(100.0, 100.3, 99.7, 100.0, 20_000)] * 120)


def _run(day_bars, raw, extra_cfg=None):
    from intraday.exit_policy import load_intraday_policy
    from tools.replay import detect
    from intraday.strategies.ignition import IgnitionMomentum
    X = _mod()
    saved = detect.ENGINES
    detect.ENGINES = [IgnitionMomentum()]
    try:
        with cfg_ctx(extra_cfg or {}):
            policy = load_intraday_policy(engine="IGN")
            return X.process_symbol_day("TESTCO", DAY, _Src(day_bars), raw, policy)
    finally:
        detect.ENGINES = saved


def test_a_missing_or_unusable_day_is_recorded_with_a_status_not_dropped():
    assert _run([], _raw())["status"] == "no_bars"
    assert _run(_quiet_day(), _raw(5))["status"] == "no_prev", "fewer than 15 prior daily bars"
    gap = _bars([(130.0, 130.5, 129.5, 130.0, 20_000)] * 30)
    r = _run(gap, _raw())
    assert r["status"] == "gap_guard" and r["gap_pct"] == 30.0, (
        "a +30% open against the prior close reads as an unadjusted split/bonus")


def test_a_quiet_day_is_no_long_detection_with_its_panel_kept():
    r = _run(_quiet_day(), _raw())
    assert r["status"] == "no_long_detection" and r["n_dets"] == 0, r
    assert r["feats"]["ok"] is True and r["feats"]["n_bars"] == 40


def test_a_real_spike_is_detected_walked_and_scored():
    r = _run(_spike_day(), _raw(), {"ign_stop_lookback_bars": "20"})
    assert r["status"] == "ok", r
    live, ungated = r["live"], r["ungated"]
    assert live["ts"] == ungated["ts"], "no open-hour detection here, so the two populations agree"
    assert live["hour"] == "MID" and live["chg_pct"] >= 3.5, live
    assert live["stop"] < live["entry"] < live["target"], live
    assert live["action"] == "EXIT_TARGET" and abs(live["r"] - 1.5) < 0.05, (
        f"the run to the target must score about +1.5R through the live ladder: {live}")
    assert r["feats"]["ok"] is True and r["feats"]["above_st"] is not None


def test_nothing_dated_on_or_after_the_replay_day_can_reach_prev_or_the_panel():
    clean = _run(_quiet_day(), _raw())
    poisoned_raw = _raw() + [
        {"date": date(2026, 8, 5), "open": 500.0, "high": 900.0, "low": 1.0, "close": 700.0,
         "volume": 9e9},
        {"date": date(2026, 8, 6), "open": 500.0, "high": 900.0, "low": 1.0, "close": 700.0,
         "volume": 9e9}]
    dirty = _run(_quiet_day(), poisoned_raw)
    assert dirty["status"] == clean["status"] == "no_long_detection", (
        "a same-day daily row would put prev_close at 700 and turn a quiet day into a gap-guard skip")
    assert dirty["feats"] == clean["feats"], "the panel must be identical: lookahead guard"


def _dataset(tmp: Path, recs: list[dict]) -> Path:
    p = tmp / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return p


def test_load_rows_keeps_only_ok_rows_for_the_population_and_counts_every_status():
    X = _mod()
    ok = {"symbol": "A", "day": "2026-03-02", "status": "ok", "feats": {"ok": True},
          "live": {"r": 1.2, "hour": "MID", "action": "EXIT_TARGET"},
          "ungated": {"r": -1.0, "hour": "OPEN", "action": "EXIT_STOP"}}
    recs = [ok, {"symbol": "B", "day": "2026-03-02", "status": "no_long_detection"},
            {"symbol": "C", "day": "2026-03-02", "status": "no_bars"},
            dict(ok, symbol="D", live=None),
            dict(ok, symbol="E", status="gap_guard", live={"r": 9.9, "hour": "MID"})]
    tmp = Path(tempfile.mkdtemp())
    saved = X.DATASET
    X.DATASET = _dataset(tmp, recs)
    try:
        live, status = X.load_rows("live")
        ung, _ = X.load_rows("ungated")
    finally:
        X.DATASET = saved
    assert [r.symbol for r in live] == ["A"], (
        "an ok row without that population is not a row, and neither is a non-ok "
        "row that happens to carry one")
    assert ung[0].r == -1.0 and ung[0].hour_bucket == "OPEN"
    assert status == {"ok": 2, "no_long_detection": 1, "no_bars": 1, "gap_guard": 1}, status


def test_collection_resumes_instead_of_repeating():
    X = _mod()
    tmp = Path(tempfile.mkdtemp())
    saved = X.DATASET
    X.DATASET = _dataset(tmp, [{"symbol": "A", "day": "2026-03-02", "status": "ok"},
                               {"symbol": "B", "day": "2026-03-03", "status": "no_bars"}])
    try:
        assert X._done_keys() == {("A", "2026-03-02"), ("B", "2026-03-03")}, (
            "a no_bars symbol-day is DONE too, or every re-run would retry it forever")
    finally:
        X.DATASET = saved
    X.DATASET = tmp / "does_not_exist.jsonl"
    try:
        assert X._done_keys() == set()
    finally:
        X.DATASET = saved


def _preflight_with(dirty=(), head_matches=True, existing=False):
    X = _mod()
    root = X.HERE.parents[2]
    tmp = Path(tempfile.mkdtemp())
    saved_git, saved_res = X._git, X.RESULTS
    X.RESULTS = tmp

    def fake_git(*args):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "status":
            return " M changed" if Path(args[-1]).name in dirty else ""
        if args[0] == "rev-parse" and args[1].startswith("HEAD:"):
            return "a" * 40 if head_matches else "b" * 40
        if args[0] == "hash-object":
            return "a" * 40
        return ""

    X._git = fake_git
    if existing:
        (tmp / f"ign_feature_holdout_{'aaaaaa' * 2}.json").write_text("{}")
    try:
        return X.preflight()
    finally:
        X._git, X.RESULTS = saved_git, saved_res


def test_preflight_passes_when_committed_and_unrevealed():
    ok, why, sha = _preflight_with()
    assert ok and why == "" and sha == "aaaaaa" * 2, (ok, why, sha)


def test_preflight_refuses_uncommitted_study_or_stats_code():
    for name in ("ign_feature_study.py", "ign_feature_stats.py"):
        ok, why, _ = _preflight_with(dirty=(name,))
        assert not ok and "uncommitted" in why and name in why, (name, why)


def test_preflight_refuses_a_file_that_is_not_the_committed_version():
    ok, why, _ = _preflight_with(head_matches=False)
    assert not ok and "not the committed version" in why, why


def test_preflight_refuses_a_second_look():
    ok, why, _ = _preflight_with(existing=True)
    assert not ok and "already revealed" in why, why


TESTS = [
    ("a missing or unusable day is recorded with a status, not dropped",
     test_a_missing_or_unusable_day_is_recorded_with_a_status_not_dropped),
    ("a quiet day is no_long_detection with its panel kept",
     test_a_quiet_day_is_no_long_detection_with_its_panel_kept),
    ("a real spike is detected, walked and scored",
     test_a_real_spike_is_detected_walked_and_scored),
    ("nothing dated on or after the replay day reaches prev or the panel",
     test_nothing_dated_on_or_after_the_replay_day_can_reach_prev_or_the_panel),
    ("load_rows keeps ok rows for the population and counts every status",
     test_load_rows_keeps_only_ok_rows_for_the_population_and_counts_every_status),
    ("collection resumes instead of repeating",
     test_collection_resumes_instead_of_repeating),
    ("preflight passes when committed and unrevealed",
     test_preflight_passes_when_committed_and_unrevealed),
    ("preflight refuses uncommitted study or stats code",
     test_preflight_refuses_uncommitted_study_or_stats_code),
    ("preflight refuses a file that is not the committed version",
     test_preflight_refuses_a_file_that_is_not_the_committed_version),
    ("preflight refuses a second look", test_preflight_refuses_a_second_look),
]
