"""
IGN event table builder — tools/replay/ign_event_table.py.

Runs entirely offline: `day_bars`, the daily cache and the index dailies are replaced with
synthetic data, so what is tested is the builder's own logic — which cells fire, that nothing after
the trigger reaches a row's features, that unusable days are skipped for a stated reason, that
control rows are reproducible, and that the holdout can never be read back as training data.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from tests.test_ign_event_features import _day, _poison, _spike
from tools.replay import ign_event_features as F
from tools.replay import ign_event_table as T

DAY = "2026-03-10"
PROFILE = np.linspace(0.0, 1.0, F.SESSION_MIN + 1)[1:]


def _daily_rows(n=80, px=100.0, vol=375_000.0, end=DAY):
    """n prior sessions of a flat stock, the last one the day before `end`."""
    last = date.fromisoformat(end)
    rows = []
    for k in range(n):
        d = last - timedelta(days=n - k)
        rows.append({"date": d.isoformat(), "open": px, "high": px + 0.5, "low": px - 0.5,
                     "close": px, "volume": vol})
    return rows


IDX_PX = {"NIFTY 50": 22000.0, "NIFTY 500": 20000.0, "INDIA VIX": 15.0}


def _index_rows(name="NIFTY 50", end=DAY):
    return _daily_rows(60, IDX_PX[name], 0.0, end)


class _World:
    """Swap the module's data sources for synthetic ones for the duration of one test."""

    def __init__(self, days: dict, daily=None):
        self.days, self.daily = days, daily if daily is not None else _daily_rows()

    def __enter__(self):
        self._old = (T.day_bars, T._load_daily, T._load_index_daily)
        T.day_bars = lambda sym, day: self.days.get((sym, day))
        T._load_daily = lambda sym: self.daily
        T._load_index_daily = lambda name: _index_rows(name)
        return T.Ctx(PROFILE)

    def __exit__(self, *a):
        T.day_bars, T._load_daily, T._load_index_daily = self._old


def _flat_session(n=375, at=60, to=104.0):
    return _spike(_day(n, drift=0.0, wick=0.05), at=at, to=to)


def _rows(ctx, sym="AAA", kind="event", seed=1):
    return T.symbol_day_rows(ctx, sym, DAY, kind, np.random.default_rng(seed))


def _idx_days(d):
    return {(n, DAY): _day(len(d.c), base=IDX_PX[n], drift=0.0, wick=IDX_PX[n] * 1e-4) for n in T.INDICES}


def test_the_expected_cells_fire_on_a_clean_spike():
    d = _flat_session()
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        rows, jobs = _rows(ctx)
    by = {r["cell"]: r for r in rows}
    assert {"T3_V0", "T3_V2", "T3.5_V2", "T4_V2", "LIVE"} <= set(by), set(by)
    assert by["T5_V0"]["idx"] > 60, "the +4% bar cannot trigger a 5% cell; the price has to keep climbing"
    t = by["T3.5_V2"]
    # bars 0..59 trade 1,000 each, then 30,000 each. Through bar 61: 60,000 + 2 x 30,000 = 120,000 against
    # 375,000 x 61/375 = 61,000 -> 1.97, short of 2.0. Through bar 62: 150,000 / 62,000 = 2.42.
    assert t["idx"] == 62 and abs(t["vr_ign"] - 150_000 / 62_000) < 1e-9, t["idx"]
    assert abs(t["move_pct"] - 4.04) < 1e-6 and t["hhmm"] == "10:18", t
    assert by["LIVE"]["idx"] > 62, "20 bars of ~100 behind the spike leave the structural stop far too wide"


def _gap_session(n=375):
    """Gaps up 3.8% at the open and holds there, on 30x volume: the stop is a few cents away."""
    a = lambda px: np.full(n, px)
    return F.DayBars(a(103.8), a(103.85), a(103.75), a(103.8), np.full(n, 30_000.0))


def test_the_live_cell_waits_for_a_feasible_structural_stop():
    d = _gap_session()
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        base = {r["cell"]: r for r in _rows(ctx)[0]}
    assert base["T3.5_V2"]["idx"] == 7 and base["LIVE"]["idx"] == 7 and base["T3.5_V2"]["feasible"]
    o, h, l, c, v = (a.copy() for a in d)
    l[0:8] = 95.0                                   # the opening bars dipped far below
    wide = F.DayBars(o, h, l, c, v)
    with _World({("AAA", DAY): wide, **_idx_days(wide)}) as ctx:
        by = {r["cell"]: r for r in _rows(ctx)[0]}
    assert by["T3.5_V2"]["idx"] == 7 and not by["T3.5_V2"]["feasible"], "the cell still fires; only its stop is wide"
    assert by["LIVE"]["idx"] == 27, (
        "refused at bar 7 (risk 8.5% > 1.75%) and every bar until the 20-bar window (i-19..i) no longer "
        f"contains bars 0..7, i.e. i = 27: {by['LIVE']['idx']}")


def test_the_live_cell_uses_the_atr_relative_floor():
    d = _flat_session(to=104.0)
    daily = _daily_rows()
    for r in daily[-20:]:
        r["high"], r["low"] = 108.0, 96.0           # ATR ~ 12%: the floor becomes ~14%, not 3.5%
    with _World({("AAA", DAY): d, **_idx_days(d)}, daily) as ctx:
        rows, _ = _rows(ctx)
    cells = {r["cell"] for r in rows}
    assert "T3.5_V2" in cells and "LIVE" not in cells, cells


def test_nothing_after_the_trigger_reaches_a_rows_features():
    d = _flat_session()
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        rows, _ = _rows(ctx)
    t0 = [r for r in rows if r["cell"] == "T3.5_V2"][0]
    with _World({("AAA", DAY): _poison(d, t0["idx"]), **_idx_days(d)}) as ctx:
        rows2, _ = _rows(ctx)
    t1 = [r for r in rows2 if r["cell"] == "T3.5_V2"][0]
    assert t1["idx"] == t0["idx"]
    bad = [k for k in t0 if not k.startswith("fo_") and t0[k] != t1[k]
           and not (isinstance(t0[k], float) and np.isnan(t0[k]) and np.isnan(t1[k]))]
    assert not bad, f"features changed when only later bars were wrecked: {bad}"
    assert t0["fo_ret_30_pct"] != t1["fo_ret_30_pct"], "the outcomes SHOULD see later bars — that is what they are"


def test_unusable_days_are_skipped_with_a_reason():
    d = _flat_session()
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        assert _rows(ctx, "MISSING") == "no_minute_bars"
    short = F.DayBars(*(a[:200] for a in d))
    with _World({("AAA", DAY): short, **_idx_days(d)}) as ctx:
        assert _rows(ctx) == "short_session"
    gapped = F.DayBars(d.o * 1.3, d.h * 1.3, d.l * 1.3, d.c * 1.3, d.v)
    with _World({("AAA", DAY): gapped, **_idx_days(d)}) as ctx:
        assert _rows(ctx) == "corporate_action_gap", "a 30% open gap is a corporate action, not a signal"
    with _World({("AAA", DAY): d, **_idx_days(d)}, daily=_daily_rows(10)) as ctx:
        assert _rows(ctx) == "short_history"
    with _World({("AAA", DAY): d, **_idx_days(d)}, daily=[]) as ctx:
        assert _rows(ctx) == "no_daily"


def test_a_missing_index_gives_none_never_a_default():
    d = _flat_session()
    with _World({("AAA", DAY): d}) as ctx:                       # no index bars at all
        rows, _ = _rows(ctx)
    r = rows[0]
    assert r["nifty_move_pct"] is None and r["vix"] is None and r["rs_nifty"] is None


def test_control_rows_are_reproducible_distinct_and_in_range():
    d = _day(375, drift=0.0, wick=0.05)
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        a, _ = _rows(ctx, kind="control", seed=7)
        b, _ = _rows(ctx, kind="control", seed=7)
        c, _ = _rows(ctx, kind="control", seed=8)
    idx = [r["idx"] for r in a]
    assert idx == [r["idx"] for r in b], "the same seed gives the same bars"
    assert idx != [r["idx"] for r in c], "another seed gives others"
    assert len(idx) == T.CTRL_PER_DAY == len(set(idx))
    lo, hi = T.CTRL_IDX_RANGE
    assert all(lo <= i < hi for i in idx) and {r["cell"] for r in a} == {"CTRL"}


def test_the_split_boundary_is_the_last_train_day():
    assert T.split_of(T.TRAIN_END) == "train"
    nxt = (date.fromisoformat(T.TRAIN_END) + timedelta(days=1)).isoformat()
    assert T.split_of(nxt) == "holdout"
    assert T.split_of("2025-01-02") == "train"


def test_policy_names_are_unique_and_span_both_sides():
    pols = T.core_policies()
    assert len(pols) == 92, f"60 long + 8 pullback + 24 short; a name collision would shrink it: {len(pols)}"
    assert sum(1 for n in pols if n.startswith("S|")) == 24
    assert all(T._pname(p) == n for n, p in pols.items())
    assert sum(1 for p in pols.values() if p.be_after_r and p.target_r and p.target_r <= p.be_after_r) == 0, (
        "a breakeven trigger at or beyond the target can never act")


def test_load_train_refuses_a_table_that_contains_a_holdout_day():
    old = T.TRAIN_TABLE
    with tempfile.TemporaryDirectory() as tmp:
        T.TRAIN_TABLE = Path(tmp) / "t.jsonl"
        bad_day = (date.fromisoformat(T.TRAIN_END) + timedelta(days=3)).isoformat()
        T.TRAIN_TABLE.write_text(json.dumps({"symbol": "A", "day": bad_day, "cell": "LIVE"}) + "\n")
        z = {"gross": np.zeros((1, 2)), "risk": np.ones((1, 2)), "bars": np.zeros((1, 2)),
             "reason": np.zeros((1, 2)), "names": np.array(["a", "b"])}
        np.savez(T.TRAIN_TABLE.with_suffix(".policies.npz"), **z)
        try:
            T.load_train()
        except AssertionError as e:
            assert "holdout" in str(e)
        else:
            raise AssertionError("a holdout day in the train table must be refused")
        finally:
            T.TRAIN_TABLE = old


def test_each_index_is_measured_against_its_own_prior_close():
    d = _flat_session()
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        r = _rows(ctx)[0][0]
    assert abs(r["nifty_move_pct"]) < 0.01 and abs(r["n500_move_pct"]) < 0.01, (
        "flat indices against their own prior closes read ~0%, whatever their level")
    assert abs(r["vix_move_pct"]) < 0.01 and abs(r["vix"] - 15.0) < 0.01, r["vix_move_pct"]


def test_the_live_cell_needs_ignitions_volume_ratio_of_two():
    a = lambda px: np.full(375, px)
    d = F.DayBars(a(103.8), a(103.85), a(103.75), a(103.8), np.full(375, 1500.0))   # ratio ~1.7
    with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
        by = {r["cell"]: r for r in _rows(ctx)[0]}
    assert "T3.5_V1" in by and "T3.5_V2" not in by and "LIVE" not in by, sorted(by)


def test_daily_context_stops_at_the_day_before():
    d = _flat_session()
    daily = _daily_rows() + [
        {"date": DAY, "open": 150.0, "high": 160.0, "low": 140.0, "close": 150.0, "volume": 9e9},
        {"date": (date.fromisoformat(DAY) + timedelta(days=1)).isoformat(),
         "open": 150.0, "high": 160.0, "low": 140.0, "close": 150.0, "volume": 9e9}]
    with _World({("AAA", DAY): d, **_idx_days(d)}, daily) as ctx:
        r = _rows(ctx)[0][0]
    assert r["prev_close"] == 100.0 and r["prev_day_volume"] == 375_000.0, (
        "the event day's own daily bar (and any later one) must not become the 'prior' session")
    assert r["prev_high"] == 100.5


def test_control_bars_never_repeat_within_a_day():
    d = _day(375, drift=0.0, wick=0.05)
    old = T.CTRL_IDX_RANGE
    T.CTRL_IDX_RANGE = (15, 20)                       # five candidates, three drawn: repeats are likely if allowed
    try:
        with _World({("AAA", DAY): d, **_idx_days(d)}) as ctx:
            for seed in range(60):
                idx = [r["idx"] for r in _rows(ctx, kind="control", seed=seed)[0]]
                assert len(idx) == len(set(idx)) == 3, (seed, idx)
    finally:
        T.CTRL_IDX_RANGE = old


def test_the_volume_profile_comes_from_train_controls_only():
    n = F.SESSION_MIN
    flat = F.DayBars(*(np.full(n, 100.0) for _ in range(4)), np.full(n, 1000.0))
    front = np.zeros(n)
    front[0] = 1_000_000.0
    loaded = F.DayBars(*(np.full(n, 100.0) for _ in range(4)), front)
    lists = {"controls": [[f"T{k}", "2026-01-05"] for k in range(250)]
                         + [[f"H{k}", "2026-09-01"] for k in range(400)]}
    days = {(s, dd): (flat if s.startswith("T") else loaded) for s, dd in lists["controls"]}
    with _World(days):
        prof = T._control_profile(lists)
    assert np.allclose(prof, F.volume_profile([flat]) ), "the 400 holdout sessions must not move the profile"
    too_few = {"controls": [[f"T{k}", "2026-01-05"] for k in range(50)]}
    with _World({(s, dd): flat for s, dd in too_few["controls"]}):
        try:
            T._control_profile(too_few)
        except SystemExit:
            pass
        else:
            raise AssertionError("fewer than 200 full control sessions must stop the build, not build a thin profile")


def test_load_train_refuses_a_policy_matrix_out_of_step_with_the_table():
    old = T.TRAIN_TABLE
    with tempfile.TemporaryDirectory() as tmp:
        T.TRAIN_TABLE = Path(tmp) / "t.jsonl"
        T.TRAIN_TABLE.write_text(json.dumps({"symbol": "A", "day": "2026-01-05", "cell": "LIVE"}) + "\n")
        z = {"gross": np.zeros((2, 2)), "risk": np.ones((2, 2)), "bars": np.zeros((2, 2)),
             "reason": np.zeros((2, 2)), "names": np.array(["a", "b"])}
        np.savez(T.TRAIN_TABLE.with_suffix(".policies.npz"), **z)
        try:
            T.load_train()
        except AssertionError as e:
            assert "out of step" in str(e)
        else:
            raise AssertionError("a 1-row table with a 2-row policy matrix must be refused")
        finally:
            T.TRAIN_TABLE = old


TESTS = [
    ("the expected cells fire on a clean spike", test_the_expected_cells_fire_on_a_clean_spike),
    ("the LIVE cell waits for a feasible structural stop", test_the_live_cell_waits_for_a_feasible_structural_stop),
    ("the LIVE cell uses the ATR-relative floor", test_the_live_cell_uses_the_atr_relative_floor),
    ("nothing after the trigger reaches a row's features", test_nothing_after_the_trigger_reaches_a_rows_features),
    ("unusable days are skipped with a reason", test_unusable_days_are_skipped_with_a_reason),
    ("a missing index gives None, never a default", test_a_missing_index_gives_none_never_a_default),
    ("control rows are reproducible, distinct and in range",
     test_control_rows_are_reproducible_distinct_and_in_range),
    ("the split boundary is the last train day", test_the_split_boundary_is_the_last_train_day),
    ("policy names are unique and span both sides", test_policy_names_are_unique_and_span_both_sides),
    ("load_train refuses a table containing a holdout day",
     test_load_train_refuses_a_table_that_contains_a_holdout_day),
    ("each index is measured against its own prior close", test_each_index_is_measured_against_its_own_prior_close),
    ("the LIVE cell needs IGN's volume ratio of two", test_the_live_cell_needs_ignitions_volume_ratio_of_two),
    ("daily context stops at the day before", test_daily_context_stops_at_the_day_before),
    ("control bars never repeat within a day", test_control_bars_never_repeat_within_a_day),
    ("the volume profile comes from train controls only", test_the_volume_profile_comes_from_train_controls_only),
    ("load_train refuses a policy matrix out of step with the table",
     test_load_train_refuses_a_policy_matrix_out_of_step_with_the_table),
]
