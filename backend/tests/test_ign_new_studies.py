"""
The short-side and forming-candle studies and their shared helpers (24-Sep-2026):
tools/replay/study_common.py, ign_short_study.py, ign_forming_study.py.

Two things carry the weight. (1) The holdout guard and the arming rule must be able to
REFUSE: an uncommitted file, a second look, a feature confirmed with the sign a gate check
cannot act on. (2) The forming study must measure the SAME quantity the live code computes:
the offline rebuild is compared, field by field, with intraday/ign_trend.forming_feats on a
context built the way the replay builds it.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from config import IST
from tests import cfg_ctx
from tests._fixtures import OPEN, bars as _bars
from tools.replay import ign_feature_stats as S
from tools.replay import study_common as C


def _mods():
    from tools.replay import ign_forming_study as F
    from tools.replay import ign_short_study as SH
    return F, SH


def _row(day, r, **f):
    base = {"ok": True, "above_st": True, "adx": 25.0, "di_minus": 15.0, "prev_vol_ratio": 1.2,
            "sma50_gt_200": True, "dist_sma50": 3.0, "rsi14": 55.0}
    base.update(f)
    return S.Row(symbol="X", day=day, r=r, feats=base)


def _planted(n, seed, *, sign=-1, effect=0.9):
    """above_st drives R by `effect` in direction `sign`; every other feature is noise."""
    rng = np.random.default_rng(seed)
    days = [(date(2026, 3, 2) + timedelta(days=i)).isoformat() for i in range(max(4, n // 4))]
    rows = []
    for i in range(n):
        st = bool(rng.random() < 0.5)
        r = float(rng.normal(0, 1) + (sign * effect if st else 0.0))
        rows.append(S.Row(f"S{i}", days[i % len(days)], r, {
            "ok": True, "above_st": st, "adx": float(rng.uniform(10, 50)),
            "di_minus": float(rng.uniform(5, 40)), "prev_vol_ratio": float(rng.uniform(0.3, 4)),
            "sma50_gt_200": bool(rng.random() < 0.6), "dist_sma50": float(rng.normal(3, 5)),
            "rsi14": float(rng.uniform(30, 80))}))
    return rows


# ── the shared guard and arming rule ────────────────────────────────────────

def _preflight_with(dirty=(), head_matches=True, existing=False):
    root = Path(C.__file__).resolve().parents[3]
    tmp = Path(tempfile.mkdtemp())
    saved = C._git

    def fake(*args, cwd=None):
        if args[:2] == ("rev-parse", "--show-toplevel"):
            return str(root)
        if args[0] == "status":
            return " M x" if Path(args[-1]).name in dirty else ""
        if args[0] == "rev-parse" and args[1].startswith("HEAD:"):
            return "a" * 40 if head_matches else "b" * 40
        if args[0] == "hash-object":
            return "a" * 40
        return ""

    C._git = fake
    files = [Path(C.__file__), Path(S.__file__)]
    if existing:
        (tmp / f"demo_{'aaaaaa' * 2}.json").write_text("{}")
    try:
        return C.preflight(files, tmp, "demo")
    finally:
        C._git = saved


def test_preflight_passes_only_when_committed_and_unrevealed():
    ok, why, sha = _preflight_with()
    assert ok and why == "" and sha == "aaaaaa" * 2, (ok, why, sha)


def test_preflight_refuses_each_way_it_can():
    ok, why, _ = _preflight_with(dirty=("study_common.py",))
    assert not ok and "uncommitted" in why and "study_common.py" in why, why
    ok, why, _ = _preflight_with(head_matches=False)
    assert not ok and "not the committed version" in why, why
    ok, why, _ = _preflight_with(existing=True)
    assert not ok and "already revealed" in why, why


def test_the_new_studies_guard_every_file_they_depend_on():
    F, SH = _mods()
    for mod in (F, SH):
        names = {Path(p).name for p in mod.STUDY_FILES}
        assert {Path(mod.__file__).name, "study_common.py", "ign_feature_stats.py"} <= names, (
            f"{mod.__name__}: editing a shared file after the look must invalidate it: {names}")


def test_train_stage_takes_a_hypothesis_list_and_respects_its_sign():
    rows = _planted(600, seed=41, sign=-1)
    with_neg = C.train_stage(rows, (("above_st", -1), ("adx", 0), ("rsi14", 0)))
    assert "above_st" in with_neg["candidates"], with_neg
    wrong = C.train_stage(rows, (("above_st", +1), ("adx", 0), ("rsi14", 0)))
    assert "above_st" not in wrong["candidates"], (
        "hypothesised +, observed strongly -: must not become a candidate")
    two_sided = C.train_stage(rows, (("above_st", 0), ("adx", 0), ("rsi14", 0)))
    assert "above_st" in two_sided["candidates"]
    assert S.resolve_signs(two_sided["tests"]) == [("above_st", -1)], (
        "a two-sided candidate carries the sign it showed on TRAIN")


def test_armable_requires_the_sign_the_gate_check_acts_on():
    tests = [{"feature": "above_st", "confirmed": True, "rho": -0.3},
             {"feature": "adx", "confirmed": True, "rho": +0.2},
             {"feature": "di_minus", "confirmed": True, "rho": +0.2},
             {"feature": "rsi14", "confirmed": True, "rho": +0.4},
             {"feature": "prev_vol_ratio", "confirmed": False, "rho": -0.3}]
    long_sign = {"above_st": +1, "adx": +1, "di_minus": -1, "prev_vol_ratio": -1}
    assert C.armable(tests, long_sign) == ["adx"], (
        "above_st confirmed NEGATIVE cannot arm a 'require above' check; di_minus confirmed "
        "POSITIVE cannot arm a 'cap -DI' check; rsi14 has no check; prev_vol_ratio is unconfirmed")
    short_sign = {"above_st": -1, "adx": +1, "di_minus": +1}
    assert C.armable(tests, short_sign) == ["above_st", "adx", "di_minus"]


def test_generic_gate_uses_the_supplied_verdict_function_and_still_refuses():
    from intraday.ign_trend import trend_verdict, trend_verdict_short
    SH = _mods()[1]
    rows = _planted(600, seed=42, sign=-1)         # above_st True -> LOWER R, so a SHORT wants False
    v = C.gate_verdict(rows, ["above_st"], gate_map=SH.GATE_MAP, off_cfg=SH.OFF_CFG,
                       verdict_fn=trend_verdict_short)
    assert v["arm"] is True and v["cfg"]["require_below_st"] is True and v["diff"] > 0.5, v
    flat = C.gate_verdict(_planted(600, seed=43, effect=0.0), ["above_st"], gate_map=SH.GATE_MAP,
                          off_cfg=SH.OFF_CFG, verdict_fn=trend_verdict_short)
    assert flat["arm"] is False, "no separation must not arm"
    assert C.gate_verdict(rows, [], gate_map=SH.GATE_MAP, off_cfg=SH.OFF_CFG,
                          verdict_fn=trend_verdict_short)["arm"] is False
    F = _mods()[0]
    lg = C.gate_verdict(_planted(600, seed=44, sign=+1), ["above_st"], gate_map=F.GATE_MAP,
                        off_cfg=F.OFF_CFG, verdict_fn=trend_verdict)
    assert lg["arm"] is True and lg["cfg"]["require_above_st"] is True, lg


def test_shared_train_stage_applies_holm_across_the_whole_list():
    """Without the correction, seven independent two-sided tests at alpha 0.10 hand a false
    candidate to ~half of all pure-noise datasets; with it, about one in ten."""
    feats = ("above_st", "adx", "di_minus", "prev_vol_ratio", "sma50_gt_200", "dist_sma50", "rsi14")
    hyp = tuple((f, 0) for f in feats)
    saved = S.N_PERM
    S.N_PERM = 1000
    try:
        hits = sum(1 for seed in range(300, 312)
                   if C.train_stage(_planted(300, seed=seed, effect=0.0), hyp)["candidates"])
    finally:
        S.N_PERM = saved
    assert hits <= 3, (
        f"{hits} of 12 pure-noise datasets produced a candidate; Holm at {S.HOLM_ALPHA} "
        f"across 7 tests should give ~1, an uncorrected list ~6")


def test_generic_gate_size_share_and_interval_floors_all_bite():
    from intraday.ign_trend import trend_verdict_short
    SH = _mods()[1]

    def go(rows):
        return C.gate_verdict(rows, ["above_st"], gate_map=SH.GATE_MAP, off_cfg=SH.OFF_CFG,
                              verdict_fn=trend_verdict_short)
    # kept = above_st False. Survivors look great but only 5% remain -> share floor
    tiny = [_row(f"2026-03-{2 + i % 20:02d}", 3.0 if i % 20 == 0 else -2.0, above_st=(i % 20 != 0))
            for i in range(400)]
    small = go(tiny)
    assert small["arm"] is False and small["kept_frac"] < S.MIN_KEPT_FRAC, small
    # a +0.3R difference on 16 vs 16 noisy trades: size and share pass, the interval does not
    kept = [_row("2026-03-02", 2.3 if i % 2 else -1.7, above_st=False) for i in range(16)]
    dropped = [_row("2026-03-03", 2.0 if i % 2 else -2.0, above_st=True) for i in range(16)]
    noisy = go(kept + dropped)
    assert noisy["diff"] >= S.MIN_IMPROVEMENT_R and noisy["kept_frac"] >= S.MIN_KEPT_FRAC, noisy
    assert noisy["diff_ci_lo"] <= 0 and noisy["arm"] is False, noisy


def test_pre_registration_of_the_new_studies_matches_what_the_gates_can_carry():
    F, SH = _mods()
    from intraday.trend_indicators import PANEL_KEYS
    for mod in (F, SH):
        feats = [f for f, _ in mod.HYPOTHESES]
        assert len(feats) == len(set(feats)) and set(feats) <= set(PANEL_KEYS), (mod.__name__, feats)
        assert set(mod.GATE_MAP) <= set(feats), "only pre-registered features may arm"
        assert set(mod.GATE_SIGN) == set(mod.GATE_MAP), "every check needs its acting sign"
    assert all(s == 0 for _, s in F.HYPOTHESES), "forming hypotheses are all two-sided"
    from intraday import ign_trend as T
    assert set(SH.OFF_CFG) == set(T.short_gate_cfg()), "short off-config must match the gate's parameters"
    assert set(F.OFF_CFG) == set(T.gate_cfg())
    # the arming values and the confidence-score thresholds are one set of numbers
    assert SH.GATE_MAP["adx"][1] == T.SCORE_ADX and SH.GATE_MAP["di_minus"][1] == T.SCORE_DI_MINUS
    assert SH.GATE_MAP["prev_vol_ratio"][1] == T.SCORE_PREV_VOL_RATIO
    assert SH.HYPOTHESES[0] == ("above_st", -1) and SH.GATE_SIGN["above_st"] == -1, (
        "for a SHORT, price BELOW the SuperTrend is the hypothesis")


# ── the short study ─────────────────────────────────────────────────────────

def _daily_short(**over):
    base = [{"date": "2026-08-31", "low": 99, "close": 100.0, "volume": 5_000_000.0},
            {"date": "2026-09-01", "low": 95.0, "close": 96.0, "volume": 10_000_000.0}]
    base[1].update(over)
    return {"AAA": base}


def test_short_candidates_pass_a_realistic_faller_and_every_filter_bites():
    SH = _mods()[1]
    win = ("2026-09-01", "2026-09-30")
    assert SH.short_candidate_days(_daily_short(), *win) == [("AAA", "2026-09-01")]
    assert SH.short_candidate_days(_daily_short(low=99.0), *win) == [], "no fall"
    assert SH.short_candidate_days(_daily_short(volume=5_500_000.0), *win) == [], "no volume"
    thin = _daily_short()
    thin["AAA"][0]["volume"] = 1000.0
    assert SH.short_candidate_days(thin, *win) == [], "prior-day turnover too thin"
    cheap = _daily_short()
    cheap["AAA"][0]["close"] = 20.0
    assert SH.short_candidate_days(cheap, *win) == [], "below the price floor"
    assert SH.short_candidate_days(_daily_short(low=0), *win) == [], "a zero low is bad data"
    assert SH.short_candidate_days(_daily_short(), "2026-09-02", "2026-09-30") == []


def test_first_short_ignores_longs_and_respects_the_open_hour():
    SH = _mods()[1]

    class D:
        def __init__(self, hh, mm, direction):
            self.ts = datetime(2026, 9, 1, hh, mm, tzinfo=IST)
            self.direction = direction
    dets = [D(9, 20, "LONG"), D(9, 40, "SHORT"), D(10, 5, "SHORT"), D(11, 0, "LONG")]
    assert (SH.first_short(dets, "ungated").ts.hour, SH.first_short(dets, "ungated").ts.minute) == (9, 40)
    assert SH.first_short(dets, "live").ts.minute == 5
    assert SH.first_short([D(9, 30, "SHORT")], "live") is None
    assert SH.first_short([D(11, 0, "LONG")], "ungated") is None, "a LONG is not a SHORT"


def _raw(n=40, end=date(2026, 8, 4), close=100.0, volume=3_000_000.0):
    return [{"date": end - timedelta(days=n - 1 - i), "open": close, "high": close + 1.0,
             "low": close - 1.0, "close": close, "volume": volume} for i in range(n)]


class _Src:
    def __init__(self, bars):
        self.bars = bars

    def get(self, sym, day):
        return self.bars


def _drop_day():
    """Volumes are twice the long fixture's: a SHORT needs Rs 50 Cr of prior-day turnover
    (twice the long floor), so the prior-day volume below is 6M and the session must keep pace."""
    seq = [(100.0, 100.3, 99.7, 100.0, 40_000)] * 75
    seq += [(100.0 - i, 100.1 - i, 99.4 - i, 99.5 - i, 80_000) for i in range(4)]
    seq += [(95.5, 95.7, 95.3, 95.5 - (i % 2) * 0.05, 60_000) for i in range(25)]
    seq += [(95.4 - 0.4 * i, 95.5 - 0.4 * i, 95.0 - 0.4 * i, 95.1 - 0.4 * i, 60_000) for i in range(12)]
    return _bars(seq)


def _run_short(day_bars, raw):
    from intraday.exit_policy import load_intraday_policy
    from intraday.strategies.ignition import IgnitionMomentum
    from tools.replay import detect
    SH = _mods()[1]
    saved = detect.ENGINES
    detect.ENGINES = [IgnitionMomentum()]
    try:
        with cfg_ctx({"ign_stop_lookback_bars": "20"}):
            return SH.process_symbol_day("TESTCO", "2026-08-05", _Src(day_bars), raw,
                                         load_intraday_policy(engine="IGN"))
    finally:
        detect.ENGINES = saved


def test_a_real_collapse_is_detected_short_walked_and_scored():
    r = _run_short(_drop_day(), _raw(volume=6_000_000.0))
    assert r["status"] == "ok", r
    live = r["live"]
    assert live["hour"] == "MID" and live["stop"] > live["entry"] > live["target"], live
    assert live["action"] == "EXIT_TARGET" and abs(live["r"] - 1.5) < 0.05, (
        f"a run down to the target must score about +1.5R for a SHORT: {live}")
    quiet = _run_short(_bars([(100.0, 100.3, 99.7, 100.0, 40_000)] * 120), _raw(volume=6_000_000.0))
    assert quiet["status"] == "no_short_detection" and quiet["n_dets"] == 0, quiet


def test_a_short_symbol_day_never_vanishes_without_a_status():
    assert _run_short([], _raw())["status"] == "no_bars"
    assert _run_short(_drop_day(), _raw(5))["status"] == "no_prev", "fewer than 15 prior daily bars"
    gap = _bars([(70.0, 70.5, 69.5, 70.0, 40_000)] * 30)
    assert _run_short(gap, _raw(volume=6_000_000.0))["status"] == "gap_guard", (
        "a -30% open against the prior close reads as an unadjusted split/bonus")


def test_a_spike_up_day_is_not_recorded_as_a_short():
    from tests.test_ign_feature_study import _spike_day
    r = _run_short(_spike_day(), _raw())
    assert r["status"] == "no_short_detection", r
    assert r["n_dets"] >= 1, "IGN DID detect the long spike; it just is not a short"
    assert "live" not in r and "ungated" not in r


# ── the forming study ───────────────────────────────────────────────────────

def _session_bars(n=100, drift=0.06):
    seq = [(100.0 + i * drift, 100.2 + i * drift, 99.8 + i * drift, 100.0 + i * drift + 0.05,
            20_000 + i) for i in range(n)]
    return _bars(seq)


def test_the_study_rebuild_equals_the_live_computation_field_by_field():
    """THE quantity check. If these differ the study would measure something the gate
    never sees."""
    from intraday import ign_trend as T
    from intraday.daily_history import to_daily_bars
    from intraday.trend_indicators import PANEL_KEYS, feature_panel
    from tools.replay.contexts import build_context
    F = _mods()[0]
    day = date(2026, 8, 5)
    raw = _raw(260)
    prior = to_daily_bars(raw, day)
    day_bars = _session_bars()
    now = day_bars[80].ts + timedelta(minutes=1)          # a bar-close evaluation instant
    ctx = build_context("TESTCO", day_bars, now,
                        prev={"close": 100.0, "high": 101.0, "low": 99.0, "atr_pct": 2.0,
                              "volume": 3_000_000.0, "value_cr": 50.0, "sector": ""})
    ctx.daily_bars = prior
    ctx.daily_feats = dict(feature_panel(prior), ok=True, reason=None)
    live = T.forming_feats(ctx)
    study = F.forming_at(prior, day_bars, now, day)
    assert live is not None and study is not None
    for k in PANEL_KEYS:
        assert live[k] == study[k], f"{k}: live {live[k]!r} != study {study[k]!r}"
    assert live["as_of"] == study["as_of"] == str(day)


def test_forming_at_reads_only_bars_strictly_before_the_detection():
    F = _mods()[0]
    day = date(2026, 8, 5)
    prior = __import__("intraday.daily_history", fromlist=["x"]).to_daily_bars(_raw(260), day)
    bars = _session_bars()
    det_ts = bars[60].ts
    base = F.forming_at(prior, bars, det_ts, day)
    from dataclasses import replace
    poisoned = list(bars)
    poisoned[60] = replace(poisoned[60], high=9999.0, low=1.0, close=5555.0, volume=9e9)
    poisoned[70] = replace(poisoned[70], high=9999.0, low=1.0, close=5555.0, volume=9e9)
    dirty = F.forming_at(prior, poisoned, det_ts, day)
    assert base == dirty, "the bar AT the detection instant and everything after must not be read"
    assert F.forming_at(prior, [], det_ts, day) is None


def test_forming_rows_counts_every_way_a_record_can_be_lost():
    F = _mods()[0]
    from tools.replay import ign_feature_study as X
    day, sym = "2026-08-05", "TESTCO"
    bars = _session_bars()
    det = bars[80]
    ts = (det.ts + timedelta(minutes=1)).isoformat()
    entry = bars[80].close
    good = {"symbol": sym, "day": day, "status": "ok", "feats": {"ok": True},
            "live": {"ts": ts, "entry": entry, "r": 0.7, "hour": "MID", "action": "EXIT_TARGET"}}
    recs = [good,
            dict(good, symbol="NODAILY"),
            dict(good, symbol="NOBARS"),
            dict(good, symbol="BADPANEL", feats={"ok": False}),
            dict(good, symbol="MISMATCH", live=dict(good["live"], entry=entry + 5)),
            {"symbol": "SKIP", "day": day, "status": "no_long_detection"}]
    tmp = Path(tempfile.mkdtemp())
    ds = tmp / "ds.jsonl"
    ds.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    saved = (X.DATASET, F._cached_daily, F.load_cached)
    X.DATASET = ds
    F._cached_daily = lambda s: None if s == "NODAILY" else _raw(260)
    F.load_cached = lambda s, d, i: [] if s == "NOBARS" else bars
    try:
        rows, n = F.forming_rows()
    finally:
        X.DATASET, F._cached_daily, F.load_cached = saved
    assert [r.symbol for r in rows] == [sym], [r.symbol for r in rows]
    assert n == {"records": 5, "no_daily": 1, "no_bars": 1, "as_of_not_ok": 1,
                 "unbuildable": 0, "entry_mismatch": 1, "used": 1}, n
    assert rows[0].feats["forming"] is True and rows[0].r == 0.7


TESTS = [
    ("preflight passes only when committed and unrevealed",
     test_preflight_passes_only_when_committed_and_unrevealed),
    ("preflight refuses each way it can", test_preflight_refuses_each_way_it_can),
    ("the new studies guard every file they depend on",
     test_the_new_studies_guard_every_file_they_depend_on),
    ("train stage takes a hypothesis list and respects its sign",
     test_train_stage_takes_a_hypothesis_list_and_respects_its_sign),
    ("armable requires the sign the gate check acts on",
     test_armable_requires_the_sign_the_gate_check_acts_on),
    ("generic gate uses the supplied verdict function and still refuses",
     test_generic_gate_uses_the_supplied_verdict_function_and_still_refuses),
    ("shared train stage applies Holm across the whole list",
     test_shared_train_stage_applies_holm_across_the_whole_list),
    ("generic gate: size, share and interval floors all bite",
     test_generic_gate_size_share_and_interval_floors_all_bite),
    ("pre-registration matches what the gates can carry",
     test_pre_registration_of_the_new_studies_matches_what_the_gates_can_carry),
    ("short candidates pass a realistic faller and every filter bites",
     test_short_candidates_pass_a_realistic_faller_and_every_filter_bites),
    ("first_short ignores longs and respects the open hour",
     test_first_short_ignores_longs_and_respects_the_open_hour),
    ("a real collapse is detected short, walked and scored",
     test_a_real_collapse_is_detected_short_walked_and_scored),
    ("a short symbol-day never vanishes without a status",
     test_a_short_symbol_day_never_vanishes_without_a_status),
    ("a spike-up day is not recorded as a short", test_a_spike_up_day_is_not_recorded_as_a_short),
    ("the study rebuild equals the live computation field by field",
     test_the_study_rebuild_equals_the_live_computation_field_by_field),
    ("forming_at reads only bars strictly before the detection",
     test_forming_at_reads_only_bars_strictly_before_the_detection),
    ("forming_rows counts every way a record can be lost",
     test_forming_rows_counts_every_way_a_record_can_be_lost),
]
