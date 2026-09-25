"""
IGN event study selection procedure — tools/replay/ign_event_study.py.

The arithmetic first (hand-worked), then the procedure as a whole on synthetic tables where the
truth is known: a planted edge must be FOUND, and a table of pure noise must NOT produce a
positive out-of-sample number. The second is the test that matters — a search over thousands of
(cell, exit, filter) combinations will always find something in-sample, and walk-forward scoring is
what tells that apart from an edge. If it ever leaks (thresholds from the test block, a selection
window that includes it), a null table scores positive and this fails.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from tools.replay import ign_event_study as E

POLICIES = ["p_null", "p_plant"]


def _frame(n_days=150, per_day=30, cells=("A", "B"), seed=0, plant=0.0):
    rng = np.random.default_rng(seed)
    rows, R = [], []
    d0 = date(2025, 1, 2)
    for d in range(n_days):
        day = (d0 + timedelta(days=d)).isoformat()
        for c in cells:
            mv = rng.normal(0.0, 1.0, per_day)
            frame = pd.DataFrame({
                "day": day, "cell": c, "symbol": [f"S{k}" for k in range(per_day)],
                "move_pct": mv, "vr_ign": rng.lognormal(0, 0.5, per_day), "gap_pct": rng.normal(0, 1, per_day),
                "rsi14": rng.uniform(20, 80, per_day), "adx": rng.uniform(5, 50, per_day),
                "vix": np.full(per_day, 15.0) + rng.normal(0, 1, per_day),
                "idx": rng.integers(10, 300, per_day), "above_orh": rng.random(per_day) < 0.5})
            rows.append(frame)
            null = rng.normal(0.0, 1.1, per_day)
            pl = rng.normal(-0.05, 1.1, per_day) + (plant * ((mv >= 0.85) & (c == cells[0])) if plant else 0.0)
            R.append(np.column_stack([null, pl]))
    return E.prepare_table(pd.concat(rows, ignore_index=True)), np.vstack(R)


# ── arithmetic, worked by hand ──────────────────────────────────────────────

def test_net_r_is_gross_less_cost_over_risk_and_no_trade_stays_no_trade():
    gross = np.array([[2.0, 2.0, np.nan]])
    risk = np.array([[1.0, 1.0, 1.0]])
    r = E.net_r(gross, risk, 0.2, band=None)
    assert abs(r[0, 0] - 1.8) < 1e-12 and np.isnan(r[0, 2])
    tiny = E.net_r(np.array([[0.5, 0.5]]), np.array([[0.3, 0.3]]), 0.2, band=(0.4, 1.75),
                   struct_cols=np.array([True, False]))
    assert np.isnan(tiny[0, 0]), "a structural stop under the risk floor is not a trade"
    assert abs(tiny[0, 1] - 1.0) < 1e-12, "a fixed-percent stop is not subject to the band: (0.5-0.2)/0.3"
    big = E.net_r(np.array([[1.0]]), np.array([[2.0]]), 0.2, band=(0.4, 1.75), struct_cols=np.array([True]))
    assert np.isnan(big[0, 0]), "a structural stop over the ceiling is not a trade"


def test_the_clustered_standard_error_is_wider_when_a_day_moves_together():
    x = np.array([1.0, 1.0, -1.0, -1.0])
    g = np.array(["a", "a", "b", "b"])
    m, se, n = E.clustered_mean_se(x, g)
    assert m == 0.0 and n == 4
    assert abs(se - np.sqrt(8) / 4) < 1e-12, "cluster sums of (x - mean) are +2 and -2: sqrt(4+4)/4"
    naive = x.std(ddof=1) / 2
    assert se > naive, "same-day rows that move together are not four independent observations"
    m2, se2, n2 = E.clustered_mean_se(np.array([1.0, np.nan, 3.0]), np.array(["a", "a", "b"]))
    assert n2 == 2 and m2 == 2.0, "NaN rows are dropped"


def test_one_sided_p():
    assert abs(E.one_sided_p(1.645, 1.0) - 0.05) < 1e-3
    assert abs(E.one_sided_p(0.0, 1.0) - 0.5) < 1e-12
    assert E.one_sided_p(1.0, 0.0) == 1.0 and E.one_sided_p(1.0, float("nan")) == 1.0
    assert E.one_sided_p(-3.0, 1.0) > 0.99


def test_blocks_are_chronological_equal_count_and_never_split_a_day():
    days = np.array([f"2025-01-{d:02d}" for d in range(1, 11) for _ in range(3)])
    b = E.block_ids(days, 5)
    assert list(np.unique(b)) == [0, 1, 2, 3, 4]
    assert all(len(set(b[days == d])) == 1 for d in np.unique(days))
    assert all((np.diff(b) >= 0)), "later days never land in an earlier block"
    assert [int((b == k).sum()) for k in range(5)] == [6] * 5


def test_filters_use_the_rows_quantiles_skip_constants_and_exclude_missing():
    df = pd.DataFrame({"day": ["d"] * 100, "cell": "A",
                       "rsi14": np.arange(100.0), "adx": np.full(100, 5.0),
                       "above_orh": [1.0] * 50 + [0.0] * 50})
    fl = E.enumerate_filters(df)
    names = {f.feature for f in fl}
    assert "rsi14" in names and "adx" not in names, "a constant column cannot split anything"
    thr = sorted({f.thr for f in fl if f.feature == "rsi14"})
    assert np.allclose(thr, np.quantile(np.arange(100.0), E.QUANTILES))
    assert len(fl) == len(set(fl))
    df.loc[10, "rsi14"] = np.nan
    assert not E.apply_filter(df, E.Filter("rsi14", ">=", 0.0))[10], "a missing value is not selected by >="
    assert not E.apply_filter(df, E.Filter("rsi14", "<=", 1e9))[10], "nor by <="
    assert E.apply_filter(df, E.Filter("above_orh", "is", 1.0)).sum() == 50


def test_prepare_table_turns_bools_and_nulls_into_floats():
    raw = pd.DataFrame({"day": ["d", "d", "d"], "cell": "A", "above_orh": [True, None, False],
                        "rsi14": [50, None, 60], "feasible": [True, True, False]})
    t = E.prepare_table(raw)
    assert t["above_orh"].dtype == float and np.isnan(t["above_orh"][1]) and t["above_orh"][0] == 1.0
    assert t["rsi14"].dtype == float and np.isnan(t["rsi14"][1])


# ── the procedure on tables where the truth is known ────────────────────────

def test_search_finds_a_planted_edge_and_names_the_right_filter():
    df, R = _frame(plant=0.9, seed=3)
    cells = {c: np.flatnonzero(df["cell"].to_numpy() == c) for c in ("A", "B")}
    best = E.select_best(df, {c: R[r] for c, r in cells.items()}, cells, POLICIES)
    assert best["cell"] == "A" and best["policy"] == "p_plant", best
    assert any(f.feature == "move_pct" for f in best["filters"]), [f.label() for f in best["filters"]]
    assert best["mean"] > 0.5 and best["lcb"] > 0.2


def test_a_null_table_gives_a_negative_lower_bound_for_the_unfiltered_book():
    df, R = _frame(plant=0.0, seed=4)
    rows = np.flatnonzero(df["cell"].to_numpy() == "A")
    cands = E.search(df.iloc[rows], R[rows], POLICIES, "A")
    unf = [c for c in cands if not c["filters"]]
    assert all(c["lcb"] < 0.05 for c in unf), "no edge, so the unfiltered lower bound cannot clear zero"


def test_walk_forward_finds_the_planted_edge_out_of_sample():
    df, R = _frame(plant=0.9, seed=5)
    wf = E.walk_forward(df, R, POLICIES, ("A", "B"))
    assert wf["oos_n"] > 100 and wf["oos_mean"] > 0.4, wf
    assert all(p["pick"]["cell"] == "A" for p in wf["picks"] if p["pick"])


def test_walk_forward_on_pure_noise_is_not_positive():
    means, ns = [], []
    for seed in range(8):
        df, R = _frame(plant=0.0, seed=100 + seed, n_days=120)
        wf = E.walk_forward(df, R, POLICIES, ("A", "B"))
        if wf["oos_n"]:
            means.append(wf["oos_mean"])
            ns.append(wf["oos_n"])
    assert means, "the procedure should pick something on most noise tables"
    pooled = float(np.average(means, weights=ns))
    assert abs(pooled) < 0.12, f"noise must not look like an edge out of sample: {pooled:.3f} over {ns}"
    assert max(means) < 0.45, f"one noise table scored {max(means):.2f}: the selection window is leaking"


def test_refine_uses_clustered_errors_and_the_block_rule():
    rng = np.random.default_rng(1)
    n_days, per = 60, 20
    days = np.repeat([f"2025-{1 + d // 28:02d}-{1 + d % 28:02d}" for d in range(n_days)], per)
    shock = np.repeat(rng.normal(0.0, 1.0, n_days), per)
    R = (shock + rng.normal(0, 0.3, n_days * per)).reshape(-1, 1) + 0.3
    df = pd.DataFrame({"day": days, "cell": "A"})
    cand = [{"cell": "A", "policy": "p", "filters": (), "n": len(df), "mean": 0.3, "se": 0.01, "lcb": 0.29}]
    out = E.refine(df, R, ["p"], cand)
    assert out[0]["se"] > 0.05, "days that move together leave ~60 independent observations, not 1,200"
    assert out[0]["lcb"] < 0.29
    blocks = E.block_ids(days, 5)
    Rb = np.where(blocks[:, None] < 2, 0.5, -0.5) + rng.normal(0, 0.1, (len(df), 1))
    kept = E.refine(df, Rb, ["p"], cand, blocks=blocks, min_pos_blocks=3)
    dropped = E.refine(df, Rb, ["p"], cand, blocks=blocks, min_pos_blocks=2)
    assert kept == [] and len(dropped) == 1 and dropped[0]["pos_blocks"] == 2


def test_search_prefers_the_better_established_of_two_equal_means():
    rng = np.random.default_rng(9)
    n = 3000
    big = np.zeros(n, dtype=bool)
    big[:1500] = True                                          # 1,500 rows
    small = np.zeros(n, dtype=bool)
    small[1500:1800] = True                                    # 300 rows
    df = pd.DataFrame({"day": [f"2025-01-{1 + i % 28:02d}" for i in range(n)], "cell": "A",
                       "above_orh": big.astype(float), "nr7": small.astype(float)})
    noise = np.where(np.arange(n) % 2 == 0, 0.8, -0.8)         # every group's sample mean is exactly its level
    R = (np.where(big | small, 0.3, -0.5) + noise)[:, None]
    out = E.search(df, R, ["p"], "A", min_n=100)
    top = [c for c in out if len(c["filters"]) == 1][0]
    assert top["filters"] == (E.Filter("above_orh", "is", 1.0),), (
        "similar means, five times the trades: the larger group has the higher lower bound", top)


def test_search_never_returns_a_config_below_the_minimum_trade_count():
    rng = np.random.default_rng(10)
    n = 2000
    lucky = np.zeros(n)
    lucky[:20] = 1.0
    df = pd.DataFrame({"day": [f"2025-01-{1 + i % 28:02d}" for i in range(n)], "cell": "A",
                       "nr7": lucky, "above_orh": (rng.random(n) < 0.5).astype(float)})
    R = rng.normal(-0.2, 1.0, (n, 1))
    R[:20, 0] = 8.0                                                      # a huge mean on 20 trades
    out = E.search(df, R, ["p"], "A", min_n=150)
    assert out and all(c["n"] >= 150 for c in out), "20 lucky trades must not be able to win the search"
    assert all(not any(f.feature == "nr7" and f.thr == 1.0 for f in c["filters"]) for c in out)


def test_pair_filters_use_two_different_features():
    df, R = _frame(plant=0.9, seed=6)
    rows = np.flatnonzero(df["cell"].to_numpy() == "A")
    out = E.search(df.iloc[rows], R[rows], POLICIES, "A")
    pairs = [c for c in out if len(c["filters"]) == 2]
    assert pairs, "the planted table should produce pairs"
    assert all(c["filters"][0].feature != c["filters"][1].feature for c in pairs)


def test_feature_scan_ranks_the_related_feature_first_and_reports_fifths():
    rng = np.random.default_rng(12)
    n = 2000
    f1 = rng.normal(0, 1, n)
    df = pd.DataFrame({"day": ["d"] * n, "cell": "A", "move_pct": f1, "vr_ign": rng.normal(0, 1, n),
                       "adx": np.full(n, 3.0), "rsi14": np.where(rng.random(n) < 0.3, np.nan, rng.normal(0, 1, n))})
    x = 0.5 * f1 + rng.normal(0, 1, n)
    out = E.feature_scan(E.prepare_table(df), x)
    assert out[0]["feature"] == "move_pct" and out[0]["rho"] > 0.3
    assert "adx" not in {r["feature"] for r in out}, "a constant feature has nothing to say"
    q = out[0]["quintile_means"]
    assert q[0] < q[2] < q[4] and out[0]["spread"] == q[4] - q[0], q
    rsi = [r for r in out if r["feature"] == "rsi14"][0]
    assert rsi["n"] < 1600, "rows missing the feature are left out of its scan"
    xs = x.copy()
    xs[:1950] = np.nan                                             # no trades on most rows
    assert E.feature_scan(E.prepare_table(df), xs) == [], "under 100 trades there is nothing to scan"


TESTS = [
    ("net R is gross less cost over risk; no trade stays no trade",
     test_net_r_is_gross_less_cost_over_risk_and_no_trade_stays_no_trade),
    ("the clustered standard error is wider when a day moves together",
     test_the_clustered_standard_error_is_wider_when_a_day_moves_together),
    ("one-sided p", test_one_sided_p),
    ("blocks are chronological, equal-count and never split a day",
     test_blocks_are_chronological_equal_count_and_never_split_a_day),
    ("filters use the rows' quantiles, skip constants, exclude missing",
     test_filters_use_the_rows_quantiles_skip_constants_and_exclude_missing),
    ("prepare_table turns bools and nulls into floats", test_prepare_table_turns_bools_and_nulls_into_floats),
    ("search finds a planted edge and names the right filter",
     test_search_finds_a_planted_edge_and_names_the_right_filter),
    ("a null table's unfiltered lower bound cannot clear zero",
     test_a_null_table_gives_a_negative_lower_bound_for_the_unfiltered_book),
    ("walk-forward finds a planted edge out of sample", test_walk_forward_finds_the_planted_edge_out_of_sample),
    ("walk-forward on pure noise is not positive", test_walk_forward_on_pure_noise_is_not_positive),
    ("refine uses clustered errors and the block rule", test_refine_uses_clustered_errors_and_the_block_rule),
    ("search prefers the better-established of two equal means",
     test_search_prefers_the_better_established_of_two_equal_means),
    ("search never returns a config below the minimum trade count",
     test_search_never_returns_a_config_below_the_minimum_trade_count),
    ("pair filters use two different features", test_pair_filters_use_two_different_features),
    ("feature scan ranks the related feature first and reports fifths",
     test_feature_scan_ranks_the_related_feature_first_and_reports_fifths),
]
