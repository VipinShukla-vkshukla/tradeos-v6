"""
IGN feature study statistics (24-Sep-2026) — tools/replay/ign_feature_stats.py.

The study exists because an earlier analysis treated 54 independent symbol-days
as 3,824 observations. So the tests that matter are the ones that make the
machinery REJECT: pure noise must not produce a candidate, a relationship whose
sign flips between train and holdout must not be confirmed, and a gate that
cannot beat the dropped group by a real margin must not arm. Each of those has a
paired test where a real effect DOES pass — a gate that cannot pass is the same
defect as one that cannot fail.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np

from config import IST
from tools.replay import ign_feature_stats as S


def _feats(**kw) -> dict:
    base = {"ok": True, "above_st": True, "adx": 25.0, "di_minus": 15.0,
            "prev_vol_ratio": 1.2, "sma50_gt_200": True, "dist_sma50": 3.0,
            "rsi14": 55.0, "di_plus": 25.0, "above_sma50": True, "ret_1m": 4.0}
    base.update(kw)
    return base


def _row(day: str, r: float, sym: str = "X", **f) -> S.Row:
    return S.Row(symbol=sym, day=day, r=r, feats=_feats(**f))


def _days(n: int) -> list[str]:
    d0 = date(2026, 3, 2)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _dataset(n: int, seed: int, *, effect: float = 0.0) -> list[S.Row]:
    """n rows over n//4 days. `effect` is how much above_st adds to gross R;
    every other feature is independent noise."""
    rng = np.random.default_rng(seed)
    days = _days(max(4, n // 4))
    rows = []
    for i in range(n):
        st = bool(rng.random() < 0.5)
        r = float(rng.normal(0, 1.0) + (effect if st else 0.0))
        rows.append(S.Row(
            symbol=f"S{i}", day=days[i % len(days)], r=r,
            feats={"ok": True, "above_st": st, "adx": float(rng.uniform(10, 50)),
                   "di_minus": float(rng.uniform(5, 40)),
                   "prev_vol_ratio": float(rng.uniform(0.3, 4)),
                   "sma50_gt_200": bool(rng.random() < 0.6),
                   "dist_sma50": float(rng.normal(3, 5)),
                   "rsi14": float(rng.uniform(30, 80))}))
    return rows


# ── pre-registration integrity ──────────────────────────────────────────────

def test_the_pre_registered_list_matches_what_the_panel_and_the_gate_can_carry():
    from intraday.strategies.ignition import trend_verdict
    from intraday.trend_indicators import PANEL_KEYS
    feats = [f for f, _ in S.HYPOTHESES]
    assert len(feats) == 7 and len(set(feats)) == 7
    assert set(feats) <= set(PANEL_KEYS), "every hypothesis names a real panel field"
    assert set(S.GATE_MAP) <= set(feats), "only pre-registered features may arm"
    assert "dist_sma50" not in S.GATE_MAP and "rsi14" not in S.GATE_MAP
    cfg = S.gate_config(list(S.GATE_MAP))
    verdict, _, n = trend_verdict(_feats(), **cfg)
    assert verdict == "pass" and n == 5, (verdict, n, cfg)
    assert set(S.gate_config([])) == set(S.gate_config(list(S.GATE_MAP)))


# ── the primitives ──────────────────────────────────────────────────────────

def test_spearman_and_the_no_variation_case():
    x = np.arange(10.0)
    assert abs(S.spearman(x, x * 3 + 1) - 1.0) < 1e-12
    assert abs(S.spearman(x, -x) + 1.0) < 1e-12
    assert S.spearman(np.ones(10), x) != S.spearman(np.ones(10), x), (
        "a constant feature is NaN — no variation is not a zero effect")


def test_permutation_p_is_small_for_a_real_effect_and_not_for_noise():
    rng = np.random.default_rng(3)
    x = rng.normal(size=200)
    real = S.perm_pvalue(x, x * 0.5 + rng.normal(size=200), seed=1)
    noise = S.perm_pvalue(x, rng.normal(size=200), seed=1)
    assert real < 0.01, f"a real relationship must be detected, p={real}"
    assert noise > 0.05, f"independent noise must not be, p={noise}"
    flag = (rng.random(200) < 0.5).astype(float)
    p_bool = S.perm_pvalue(flag, flag * 0.8 + rng.normal(size=200), seed=1)
    assert p_bool < 0.01, "booleans (heavy ties) must work too"


def test_holm_known_values_and_nan_handling():
    adj = S.holm([0.01, 0.04, 0.03])
    assert [round(a, 6) for a in adj] == [0.03, 0.06, 0.06], adj
    adj2 = S.holm([0.01, float("nan"), 0.03])
    assert adj2[1] != adj2[1] and abs(adj2[0] - 0.02) < 1e-12, (
        "a NaN test must not count toward the number of comparisons")
    assert S.holm([0.9])[0] == 0.9


def test_bootstrap_ci_straddles_zero_for_noise_and_excludes_it_for_signal():
    rng = np.random.default_rng(5)
    x = rng.normal(size=300)
    lo, hi = S.bootstrap_ci(x, rng.normal(size=300), n_boot=400)
    assert lo < 0 < hi, (lo, hi)
    lo, hi = S.bootstrap_ci(x, x + rng.normal(size=300), n_boot=400)
    assert lo > 0.3, (lo, hi)


# ── unit of independence and selection ──────────────────────────────────────

def _det(hh: int, mm: int, direction: str = "LONG"):
    class D:
        pass
    d = D()
    d.ts = datetime(2026, 9, 1, hh, mm, tzinfo=IST)
    d.direction = direction
    return d


def test_first_detection_live_skips_the_open_hour_and_ignores_shorts():
    dets = [_det(9, 20, "SHORT"), _det(9, 40), _det(10, 5), _det(11, 0)]
    assert S.first_detection(dets, "ungated").ts.hour == 9
    assert S.first_detection(dets, "ungated").ts.minute == 40, "the SHORT is not a LONG"
    live = S.first_detection(dets, "live")
    assert (live.ts.hour, live.ts.minute) == (10, 5), "live = first at/after 10:00"
    assert S.first_detection([_det(9, 30)], "live") is None
    assert S.first_detection([_det(11, 0, "SHORT")], "ungated") is None
    assert S.first_detection(list(reversed(dets)), "live").ts.minute == 5, "order-independent"


def test_hour_bucket_boundaries():
    assert S.hour_bucket_of(datetime(2026, 9, 1, 9, 59, tzinfo=IST)) == "OPEN"
    assert S.hour_bucket_of(datetime(2026, 9, 1, 10, 0, tzinfo=IST)) == "MID"
    assert S.hour_bucket_of(datetime(2026, 9, 1, 13, 0, tzinfo=IST)) == "LATE"


def _daily(**over):
    # prior day: 100 x 5M shares = Rs 50 Cr turnover, above the Rs 25 Cr floor
    base = [{"date": "2026-08-31", "high": 101, "close": 100.0, "volume": 5_000_000.0},
            {"date": "2026-09-01", "high": 105, "close": 104.0, "volume": 10_000_000.0}]
    base[1].update(over)
    return {"AAA": base}


def test_candidate_days_passes_a_realistic_mover_and_each_filter_bites():
    ok = S.candidate_days(_daily(), "2026-09-01", "2026-09-30")
    assert ok == [("AAA", "2026-09-01")], "a +5% day on 2x volume must be a candidate"
    assert S.candidate_days(_daily(high=101.0), "2026-09-01", "2026-09-30") == [], "no move"
    assert S.candidate_days(_daily(volume=5_500_000.0), "2026-09-01", "2026-09-30") == [], "no volume"
    thin = _daily()
    thin["AAA"][0]["volume"] = 1000.0
    assert S.candidate_days(thin, "2026-09-01", "2026-09-30") == [], "prior-day turnover too thin"
    cheap = _daily()
    cheap["AAA"][0]["close"] = 20.0
    assert S.candidate_days(cheap, "2026-09-01", "2026-09-30") == [], "below the price floor"
    assert S.candidate_days(_daily(), "2026-09-02", "2026-09-30") == [], "outside the window"


def test_gap_guard_refuses_a_split_and_admits_a_normal_gap():
    assert S.gap_ok(100.0, 103.0) is True
    assert S.gap_ok(100.0, 50.0) is False, "a 2:1 split reads as a -50% gap"
    assert S.gap_ok(100.0, 0.0) is False and S.gap_ok(0.0, 100.0) is False


def test_prev_from_daily_uses_only_bars_before_the_day():
    from intraday.trend_indicators import DailyBar
    d0 = date(2026, 8, 1)
    bars = [DailyBar(d0 + timedelta(days=i), 9.0 + i, 10.0 + i, 8.0 + i, 9.0 + i, 1000.0 + i)
            for i in range(25)]
    day = d0 + timedelta(days=20)
    p = S.prev_from_daily(bars, day)
    last = bars[19]
    assert p["close"] == last.close and p["high"] == last.high and p["low"] == last.low
    assert p["volume"] == last.volume, "prior-day raw volume, as live puts in avg_volume_20d"
    assert abs(p["atr_pct"] - 2.0 / last.close * 100.0) < 1e-9, (
        "steady series: every TR is 2, so ATR(14) is 2")
    assert abs(p["value_cr"] - last.close * last.volume / 1e7) < 1e-12
    assert S.prev_from_daily(bars, d0 + timedelta(days=10)) is None, "fewer than 15 prior bars"


# ── split, train, holdout, gate ─────────────────────────────────────────────

def test_time_split_cuts_on_days_and_holds_out_the_newest():
    rows = [_row(d, 0.0, sym=f"S{i}") for i, d in enumerate(_days(30) * 2)]
    train, hold = S.time_split(rows)
    assert {r.day for r in train}.isdisjoint({r.day for r in hold}), "no day straddles"
    assert max(r.day for r in train) < min(r.day for r in hold), "holdout is the NEWEST days"
    assert abs(len({r.day for r in hold}) - 10) <= 1
    tr, ho = S.time_split(rows[:2])
    assert ho == [] and len(tr) == 2, "too few days -> everything is train"


def test_feature_pairs_skips_failed_panels_and_missing_values():
    rows = [_row("2026-03-02", 1.0), _row("2026-03-03", 2.0, adx=None),
            S.Row("Y", "2026-03-04", 3.0, dict(_feats(), ok=False))]
    x, y = S.feature_pairs(rows, "adx")
    assert len(x) == 1 and y[0] == 1.0, "only the ok panel with a value counts"
    xb, _ = S.feature_pairs(rows[:1], "above_st")
    assert xb[0] == 1.0, "booleans become 0/1"


class _fast:
    """Fewer permutations for the noise-rate loops. 1,000 permutations still
    resolve p to 0.001, far finer than the 0.10 Holm bar being tested."""

    def __enter__(self):
        self.saved = S.N_PERM
        S.N_PERM = 1000

    def __exit__(self, *a):
        S.N_PERM = self.saved


def test_train_stage_finds_a_real_effect_and_only_that():
    rows = _dataset(600, seed=21, effect=0.8)
    out = S.train_stage(rows)
    assert "above_st" in out["candidates"], out
    assert set(out["candidates"]) <= {"above_st"}, (
        f"the other six features are pure noise and must not pass: {out['candidates']}")
    t = {x["feature"]: x for x in out["tests"]}
    assert t["above_st"]["p_holm"] <= S.HOLM_ALPHA and t["above_st"]["rho"] > 0.15


def test_pure_noise_almost_never_produces_a_candidate():
    with _fast():
        hits = sum(1 for seed in range(30, 38)
                   if S.train_stage(_dataset(300, seed=seed))["candidates"])
    assert hits <= 2, (
        f"Holm at {S.HOLM_ALPHA} across 7 features allows a false candidate in "
        f"roughly one dataset in ten; {hits} of 8 pure-noise datasets produced one")


def test_a_wrong_sign_on_train_is_not_a_candidate_even_when_significant():
    rows = _dataset(600, seed=22, effect=-0.9)          # above_st HURTS
    out = S.train_stage(rows)
    assert "above_st" not in out["candidates"], (
        "hypothesised +, observed strongly -: must not be carried forward")


def test_two_sided_candidates_carry_their_train_sign_forward():
    tests = [{"feature": "dist_sma50", "sign": 0, "candidate": True, "rho": -0.2},
             {"feature": "rsi14", "sign": 0, "candidate": True, "rho": 0.2},
             {"feature": "adx", "sign": 1, "candidate": True, "rho": 0.3},
             {"feature": "di_minus", "sign": -1, "candidate": False, "rho": -0.4}]
    assert S.resolve_signs(tests) == [("dist_sma50", -1), ("rsi14", 1), ("adx", 1)]


def test_holdout_confirms_a_stable_effect_and_rejects_a_flipped_one():
    real = _dataset(600, seed=23, effect=0.8)
    out = S.holdout_stage(real, [("above_st", 1)])
    assert out["confirmed"] == ["above_st"], out
    flipped = _dataset(600, seed=24, effect=-0.8)
    out2 = S.holdout_stage(flipped, [("above_st", 1)])
    assert out2["confirmed"] == [], "same hypothesis, opposite sign in the holdout"
    # A single noise dataset is a coin flip (a 95% CI excludes zero 5% of the
    # time and the sign matches half of those: ~2.5%; measured 2/60 = 3.3% at
    # n=120), so assert the RATE over many, not one lucky seed.
    false_confirms = sum(
        1 for seed in range(100, 140)
        if S.holdout_stage(_dataset(120, seed=seed), [("above_st", 1)])["confirmed"])
    assert false_confirms <= 4, (
        f"{false_confirms} of 40 pure-noise holdouts were 'confirmed'; expected ~1")


def test_gate_verdict_arms_a_real_separation_and_refuses_the_rest():
    hold = _dataset(600, seed=26, effect=0.8)
    v = S.gate_verdict(hold, ["above_st"])
    assert v["arm"] is True and v["diff"] > 0.5, v
    assert v["cfg"]["require_above_st"] is True and v["cfg"]["min_adx"] == 0.0
    none = S.gate_verdict(_dataset(600, seed=27), ["above_st"])
    assert none["arm"] is False, "no separation -> not armed"
    assert S.gate_verdict(hold, ["dist_sma50"])["arm"] is False, "no gate check exists"
    assert S.gate_verdict(hold, [])["arm"] is False
    tiny = [_row(f"2026-03-{2 + i % 20:02d}", -2.0 if i % 20 else 3.0, sym=f"T{i}",
                 above_st=(i % 20 == 0)) for i in range(400)]
    small = S.gate_verdict(tiny, ["above_st"])
    assert small["arm"] is False and small["kept_frac"] < S.MIN_KEPT_FRAC, (
        "a gate that throws away 95% of trades must not arm however good the survivors look")


def test_gate_does_not_arm_on_a_difference_the_bootstrap_cannot_tell_from_zero():
    # kept: 16 trades averaging +0.3R but swinging +2.3 / -1.7; dropped: 16 trades
    # averaging 0.0 swinging +2 / -2. The point difference (+0.3R, kept 50%)
    # clears both the size and share floors — only the interval says no.
    kept = [_row(f"2026-03-{2 + i:02d}", 2.3 if i % 2 else -1.7, sym=f"K{i}", above_st=True)
            for i in range(16)]
    dropped = [_row(f"2026-03-{2 + i:02d}", 2.0 if i % 2 else -2.0, sym=f"D{i}",
                    above_st=False) for i in range(16)]
    v = S.gate_verdict(kept + dropped, ["above_st"])
    assert v["diff"] >= S.MIN_IMPROVEMENT_R and v["kept_frac"] >= S.MIN_KEPT_FRAC, v
    assert v["diff_ci_lo"] <= 0, f"the interval must span zero here: {v}"
    assert v["arm"] is False, "a lucky-looking small sample must not arm a live gate"


def test_hour_effect_reports_open_vs_later_and_says_when_it_cannot():
    rows = [S.Row(f"S{i}", "2026-03-02", -1.0, {}, hour_bucket="OPEN") for i in range(40)]
    rows += [S.Row(f"M{i}", "2026-03-02", 0.2, {}, hour_bucket="MID") for i in range(40)]
    h = S.hour_effect(rows)
    assert abs(h["diff"] - 1.2) < 1e-9 and h["ci"][0] > 0, h
    assert "note" in S.hour_effect(rows[:3])


def test_describe():
    d = S.describe([_row("2026-03-02", r) for r in (1.0, -1.0, 2.0, -2.0)])
    assert d["n"] == 4 and d["mean_r"] == 0.0 and d["win"] == 0.5 and d["median_r"] == 0.0
    assert S.describe([]) == {"n": 0}


TESTS = [
    ("pre-registration matches what the panel and gate can carry",
     test_the_pre_registered_list_matches_what_the_panel_and_the_gate_can_carry),
    ("spearman, and the no-variation case", test_spearman_and_the_no_variation_case),
    ("permutation p: real effect small, noise not",
     test_permutation_p_is_small_for_a_real_effect_and_not_for_noise),
    ("Holm known values and NaN handling", test_holm_known_values_and_nan_handling),
    ("bootstrap CI: straddles zero for noise, excludes it for signal",
     test_bootstrap_ci_straddles_zero_for_noise_and_excludes_it_for_signal),
    ("first detection: live skips the open hour, ignores shorts",
     test_first_detection_live_skips_the_open_hour_and_ignores_shorts),
    ("hour bucket boundaries", test_hour_bucket_boundaries),
    ("candidate days: a realistic mover passes and each filter bites",
     test_candidate_days_passes_a_realistic_mover_and_each_filter_bites),
    ("gap guard refuses a split", test_gap_guard_refuses_a_split_and_admits_a_normal_gap),
    ("prev built only from bars before the day",
     test_prev_from_daily_uses_only_bars_before_the_day),
    ("time split cuts on days, holds out the newest",
     test_time_split_cuts_on_days_and_holds_out_the_newest),
    ("feature pairs skip failed panels and missing values",
     test_feature_pairs_skips_failed_panels_and_missing_values),
    ("train stage finds a real effect and only that",
     test_train_stage_finds_a_real_effect_and_only_that),
    ("pure noise almost never produces a candidate",
     test_pure_noise_almost_never_produces_a_candidate),
    ("a wrong sign on train is not a candidate",
     test_a_wrong_sign_on_train_is_not_a_candidate_even_when_significant),
    ("two-sided candidates carry their train sign forward",
     test_two_sided_candidates_carry_their_train_sign_forward),
    ("holdout confirms a stable effect, rejects a flipped one",
     test_holdout_confirms_a_stable_effect_and_rejects_a_flipped_one),
    ("gate verdict arms a real separation and refuses the rest",
     test_gate_verdict_arms_a_real_separation_and_refuses_the_rest),
    ("gate does not arm on a difference the bootstrap cannot tell from zero",
     test_gate_does_not_arm_on_a_difference_the_bootstrap_cannot_tell_from_zero),
    ("hour effect", test_hour_effect_reports_open_vs_later_and_says_when_it_cannot),
    ("describe", test_describe),
]
