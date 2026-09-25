"""
IGN event study — the pre-registered selection procedure and the one-look holdout test.

    python -m tools.replay.ign_event_study audit        # descriptive: coverage, LIVE parity, base rates
    python -m tools.replay.ign_event_study select       # walk-forward selection on TRAIN, writes the spec
    python -m tools.replay.ign_event_study reveal       # the holdout, once, for committed files only

PRE-REGISTRATION (this file, committed before any selection result was produced).

QUESTION. Is there a set-up, a filter and an exit for the IGN family (intraday ignition: a stock
that has moved >= 3% on the day) whose NET expectancy, after the production MIS cost model, is
positive out of sample? "Win rate" is reported, never optimised: a 4:1 stop-to-target trade wins
80% of the time and still loses money once costs are paid.

UNIT. One row = one symbol-day per trigger cell. Cells are (move >= T% over the prior close) x
(IGN volume ratio >= V) plus LIVE, the live rule itself. `CTRL` rows (random bars on random
liquid symbol-days) are the benchmark: what the SAME exit earns entering anything at any time.

COST. Net R = (gross % - COST_PCT) / risk %. COST_PCT = 0.2063: the production MIS model at a
Rs 10-66k order, which INCLUDES 5 bps per leg of slippage (0.1063% statutory + 0.10% slippage).
Sensitivities: charges only (0.1063) and 10 bps per leg (0.3063). Fills: next bar's open;
a stop that is gapped through fills at the open; a bar spanning stop and target is a loss.
A struct-stop trade whose risk is outside RISK_BAND is not a trade: below 0.4% the stop sits inside
minute-bar noise and the cost alone is over half an R.

SEARCH SPACE (fixed here, so the number of things tried is known): 17 cells x 92 exit policies x
[no filter | one filter | two filters]. A filter is (feature, >= or <=, threshold) with the
threshold a 20/40/60/80th percentile of the selection window's own rows; a row missing the
feature is not selected by that filter. Pairs are searched only among each (cell, policy)'s
PAIR_TOP best single filters.

SELECTION. Rank by lower confidence bound of mean net R (mean - Z_SEARCH x SE, rows treated as
independent) to shortlist SHORTLIST configs, then re-rank by the DAY-CLUSTERED lower bound (rows on
one day share the market and are not independent). Minimum MIN_N trades. The winner must also have
a positive mean in at least MIN_POS_BLOCKS of the K_BLOCKS chronological blocks of train.

VALIDATING THE PROCEDURE ITSELF. Walk-forward: for each block k >= FIRST_TEST_BLOCK, run the whole
procedure (thresholds included) on blocks < k only and score its pick on block k. The pooled
out-of-sample mean is what the procedure is worth; the in-sample winner's number is not.

FREEZING. `select` writes the spec. If the walk-forward pooled out-of-sample mean is not above
WF_MIN_OOS_MEAN on at least WF_MIN_OOS_N trades with day-clustered one-sided p < WF_MAX_P, the spec
records "no_candidate", the holdout stays sealed and the study concludes that this procedure finds
no edge in the IGN family. (A bare positive mean is not enough: on pure noise it happens half the
time.)

HOLDOUT. Sessions after TRAIN_END (tools.replay.ign_event_table). The frozen spec (written by
`select`, committed) is scored once by `reveal`, which refuses unless every study file is committed
and unmodified and this file-version set has not been revealed before. ACCEPT only if, on the
holdout: n >= ACCEPT_MIN_N, mean net R > 0 with day-clustered one-sided p < ACCEPT_P, and the mean
beats the CTRL mean under the same exit. Otherwise the answer is: no deployable edge found.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NamedTuple, Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# ── pre-registered constants ────────────────────────────────────────────────
COST_PCT = 0.2063
COST_SENSITIVITY = {"charges_only": 0.1063, "production": 0.2063, "slip_10bps_leg": 0.3063}
RISK_BAND = (0.4, 1.75)
K_BLOCKS = 5
FIRST_TEST_BLOCK = 2               # 0-based: blocks 0..1 select, block 2 tests; then 0..2 -> 3; 0..3 -> 4
QUANTILES = (0.2, 0.4, 0.6, 0.8)
PAIR_TOP = 15
Z_SEARCH = 1.0
Z_FINAL = 1.645
SHORTLIST = 60
MIN_N = 150
MIN_N_WALK = 80
MIN_POS_BLOCKS = 3
ACCEPT_MIN_N = 100
ACCEPT_P = 0.05
WF_MIN_OOS_MEAN = 0.0              # the holdout is opened only if the procedure earned a positive
WF_MIN_OOS_N = 150                 # walk-forward mean on at least this many trades
WF_MAX_P = 0.10                    # ... and its day-clustered one-sided p must be below this

NUMERIC_FEATURES = (
    "idx", "move_pct", "gap_pct", "move_from_open_pct", "from_high_pct", "pos_in_range", "vwap_dev_pct",
    "pct_bars_above_vwap", "orh_dist_pct", "bars_since_1pct", "path_eff_30", "ret_30_pct", "ret_5_pct",
    "pre_range10_pct", "trig_bar_range_x", "trig_bar_vol_x", "vol_last5_x", "vr_ign", "rvol_profile",
    "value_so_far_cr", "risk_pct", "nifty_move_pct", "nifty_ret_30_pct", "nifty_from_open_pct",
    "n500_move_pct", "vix", "vix_move_pct", "rs_nifty", "rs_n500", "n500_minus_nifty", "nifty_dist_sma20",
    "nifty_ret_5d", "nifty_ret_20d", "n500_dist_sma20", "n500_ret_5d", "n500_ret_20d", "adx", "di_plus",
    "di_minus", "rsi14", "dist_sma50", "ret_1m", "atr14_pct", "dist_sma20", "ret_5d", "ret_20d", "ret_60d",
    "hi52_dist", "lo52_dist", "prev_range_pct", "prev_clv", "prev_ret1", "range_ratio", "avg20_turnover_cr",
    "price_level", "n_ign_20d", "up_streak", "prev_vol_ratio", "weekday")
BOOLEAN_FEATURES = ("above_orh", "above_st", "above_sma50", "sma50_gt_200", "nr7", "prev_day_ign", "feasible")


class Filter(NamedTuple):
    feature: str
    op: str            # ">=" | "<=" | "is"
    thr: float

    def label(self) -> str:
        return f"{self.feature} {self.op} {self.thr:.4g}" if self.op != "is" else f"{self.feature} is {bool(self.thr)}"


class Config(NamedTuple):
    cell: str
    policy: str
    filters: tuple


# ── arithmetic ──────────────────────────────────────────────────────────────

def net_r(gross: np.ndarray, risk: np.ndarray, cost_pct: float = COST_PCT,
          band: tuple[float, float] | None = RISK_BAND, struct_cols: np.ndarray | None = None) -> np.ndarray:
    """Net R per (row, policy); NaN where there was no trade. The risk band applies only to
    columns flagged in `struct_cols` (a fixed-percent stop's risk is what it says it is)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (gross.astype(float) - cost_pct) / risk.astype(float)
    if band is not None:
        out = (risk < band[0]) | (risk > band[1])
        if struct_cols is not None:
            out = out & struct_cols[None, :]
        r = np.where(out, np.nan, r)
    return r


def clustered_mean_se(x: np.ndarray, groups: np.ndarray) -> tuple[float, float, int]:
    """Mean of x and its standard error with rows clustered by `groups` (one cluster per day).
    Rows with NaN are dropped."""
    ok = ~np.isnan(x)
    x, g = x[ok], groups[ok]
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else float("nan")), float("nan"), n
    m = x.mean()
    _, inv = np.unique(g, return_inverse=True)
    cs = np.bincount(inv, weights=x - m)
    return float(m), float(np.sqrt((cs ** 2).sum()) / n), n


def one_sided_p(mean: float, se: float) -> float:
    """P(Z >= mean/se) under the normal approximation; 1.0 when undefined."""
    if not (se == se) or se <= 0:
        return 1.0
    from math import erfc, sqrt
    return 0.5 * erfc((mean / se) / sqrt(2.0))


def block_ids(days: Sequence[str], k: int = K_BLOCKS) -> np.ndarray:
    """Chronological block of each row: unique days split into k equal-count groups."""
    days = np.asarray(days)
    u = np.unique(days)
    edges = np.array_split(u, k)
    lookup = {d: i for i, part in enumerate(edges) for d in part}
    return np.array([lookup[d] for d in days])


# ── the table as the study sees it ──────────────────────────────────────────

def prepare_table(df):
    """Every feature column as float64 (bool -> 0/1, null -> NaN), so no filter can meet an
    object column holding None. Non-feature columns are left alone."""
    import pandas as pd
    out = df.copy()
    for f in NUMERIC_FEATURES + BOOLEAN_FEATURES:
        if f in out.columns:
            out[f] = pd.to_numeric(out[f].map(lambda v: np.nan if v is None else (float(v) if not isinstance(v, str) else np.nan)),
                                   errors="coerce").astype(float)
    return out


# ── filters ─────────────────────────────────────────────────────────────────

def enumerate_filters(df, features: Sequence[str] = NUMERIC_FEATURES,
                      booleans: Sequence[str] = BOOLEAN_FEATURES,
                      quantiles: Sequence[float] = QUANTILES) -> list[Filter]:
    out: list[Filter] = []
    for f in features:
        if f not in df.columns:
            continue
        v = df[f].to_numpy(dtype=float)
        v = v[~np.isnan(v)]
        if len(v) < 30 or np.ptp(v) == 0:
            continue
        for q in quantiles:
            t = float(np.quantile(v, q))
            out.append(Filter(f, ">=", t))
            out.append(Filter(f, "<=", t))
    for f in booleans:
        if f in df.columns:
            out.append(Filter(f, "is", 1.0))
            out.append(Filter(f, "is", 0.0))
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def apply_filter(df, flt: Filter) -> np.ndarray:
    v = df[flt.feature].to_numpy(dtype=float)
    if flt.op == "is":
        return (~np.isnan(v)) & (v == flt.thr)
    with np.errstate(invalid="ignore"):
        return (v >= flt.thr) if flt.op == ">=" else (v <= flt.thr)


def apply_filters(df, filters: Sequence[Filter]) -> np.ndarray:
    m = np.ones(len(df), dtype=bool)
    for f in filters:
        m &= apply_filter(df, f)
    return m


# ── the search, within ONE cell's rows ──────────────────────────────────────

def _stats(N: np.ndarray, S1: np.ndarray, S2: np.ndarray):
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = S1 / N
        var = (S2 - N * mean ** 2) / np.maximum(N - 1, 1)
        se = np.sqrt(np.maximum(var, 0) / N)
    return mean, se


def search(df, R: np.ndarray, policy_names: Sequence[str], cell: str, *, min_n: int = MIN_N,
           z: float = Z_SEARCH, pair_top: int = PAIR_TOP, shortlist: int = SHORTLIST) -> list[dict]:
    """Best (policy, filters) configs for one cell by lower confidence bound, using this window's
    rows only (thresholds included). Rows are treated as independent here; `refine` corrects that."""
    n, P = R.shape
    if n < min_n:
        return []
    filters = enumerate_filters(df)
    M = np.column_stack([np.ones(n, dtype=bool)] + [apply_filter(df, f) for f in filters]).astype(np.float32)
    valid = (~np.isnan(R)).astype(np.float32)
    Rz = np.nan_to_num(R).astype(np.float32)
    N, S1, S2 = M.T @ valid, M.T @ Rz, M.T @ (Rz * Rz)
    mean, se = _stats(N.astype(float), S1.astype(float), S2.astype(float))
    lcb = np.where(N >= min_n, mean - z * se, -np.inf)
    lcb = np.where(np.isnan(lcb), -np.inf, lcb)
    out: list[dict] = []

    def add(fi_tuple, p, m_, se_, n_, lcb_):
        out.append({"cell": cell, "policy": policy_names[p], "filters": fi_tuple, "n": int(n_),
                    "mean": float(m_), "se": float(se_), "lcb": float(lcb_)})

    flat = np.argsort(-lcb, axis=None)[: shortlist * 3]
    for idx in flat:
        fi, p = divmod(int(idx), P)
        if lcb[fi, p] == -np.inf:
            break
        add(() if fi == 0 else (filters[fi - 1],), p, mean[fi, p], se[fi, p], N[fi, p], lcb[fi, p])
    # pairs, only among each policy's best singles
    for p in range(P):
        best = np.argsort(-lcb[1:, p])[:pair_top] + 1
        best = [b for b in best if lcb[b, p] > -np.inf]
        for a in range(len(best)):
            for b in range(a + 1, len(best)):
                fa, fb = filters[best[a] - 1], filters[best[b] - 1]
                if fa.feature == fb.feature:
                    continue
                m2 = (M[:, best[a]] * M[:, best[b]]).astype(bool)
                x = R[m2, p]
                x = x[~np.isnan(x)]
                if len(x) < min_n:
                    continue
                se_ = x.std(ddof=1) / np.sqrt(len(x))
                add((fa, fb), p, x.mean(), se_, len(x), x.mean() - z * se_)
    out.sort(key=lambda d: -d["lcb"])
    return out[:shortlist]


def refine(df, R: np.ndarray, policy_names: Sequence[str], cands: list[dict], *, z: float = Z_FINAL,
           blocks: np.ndarray | None = None, min_pos_blocks: int = 0) -> list[dict]:
    """Re-score shortlisted configs with day-clustered standard errors and, when `blocks` is
    given, require a positive mean in at least `min_pos_blocks` of them."""
    pidx = {n: i for i, n in enumerate(policy_names)}
    days = df["day"].to_numpy()
    out = []
    for c in cands:
        m = apply_filters(df, c["filters"])
        x = R[m, pidx[c["policy"]]]
        mean, se, n = clustered_mean_se(x, days[m])
        if n < 2 or not (se == se):
            continue
        rec = dict(c, mean=mean, se=se, n=n, lcb=mean - z * se, win=float((x[~np.isnan(x)] > 0).mean()))
        if blocks is not None:
            bm = []
            for b in np.unique(blocks):
                xb = R[m & (blocks == b), pidx[c["policy"]]]
                xb = xb[~np.isnan(xb)]
                bm.append(float(xb.mean()) if len(xb) else float("nan"))
            rec["block_means"] = bm
            rec["pos_blocks"] = int(sum(1 for v in bm if v == v and v > 0))
            if rec["pos_blocks"] < min_pos_blocks:
                continue
        out.append(rec)
    out.sort(key=lambda d: -d["lcb"])
    return out


def select_ranked(df, R_by_cell: dict[str, np.ndarray], cell_rows: dict[str, np.ndarray], policy_names,
                  *, min_n: int = MIN_N, blocks_all: np.ndarray | None = None,
                  min_pos_blocks: int = 0) -> list[dict]:
    """The whole procedure on one window: search every cell, refine, best first."""
    best: list[dict] = []
    for cell, rows in cell_rows.items():
        sub = df.iloc[rows]
        R = R_by_cell[cell]
        cands = search(sub, R, policy_names, cell, min_n=min_n)
        blk = blocks_all[rows] if blocks_all is not None else None
        best += refine(sub, R, policy_names, cands, blocks=blk, min_pos_blocks=min_pos_blocks)
    best.sort(key=lambda d: -d["lcb"])
    return best


def select_best(*a, **kw) -> dict | None:
    ranked = select_ranked(*a, **kw)
    return ranked[0] if ranked else None


# ── walk-forward: what is the PROCEDURE worth out of sample? ────────────────

def walk_forward(df, R_full: np.ndarray, policy_names: Sequence[str], cells: Sequence[str], *,
                 k_blocks: int = K_BLOCKS, first_test: int = FIRST_TEST_BLOCK) -> dict:
    """For each test block k, run the procedure on blocks < k only and score its pick on block k.
    Thresholds come from the selection window; block k's rows are only ever SCORED."""
    from math import ceil
    blocks = block_ids(df["day"].to_numpy(), k_blocks)
    cell_col = df["cell"].to_numpy()
    pidx = {n: i for i, n in enumerate(policy_names)}
    picks, pooled_x, pooled_d = [], [], []
    for k in range(first_test, k_blocks):
        sel = blocks < k
        sub_df, sub_R, sub_blocks = df[sel].reset_index(drop=True), R_full[sel], blocks[sel]
        rows = {c: np.flatnonzero(sub_df["cell"].to_numpy() == c) for c in cells}
        rows = {c: r for c, r in rows.items() if len(r) >= MIN_N_WALK}
        best = select_best(sub_df, {c: sub_R[r] for c, r in rows.items()}, rows, policy_names,
                           min_n=MIN_N_WALK, blocks_all=sub_blocks,
                           min_pos_blocks=ceil(MIN_POS_BLOCKS / K_BLOCKS * k)) if rows else None
        if best is None:
            picks.append({"block": k, "pick": None, "oos_n": 0, "oos_sum": 0.0})
            continue
        te = (blocks == k) & (cell_col == best["cell"])
        m = apply_filters(df[te], best["filters"])
        xr = R_full[te][m, pidx[best["policy"]]]
        keep = ~np.isnan(xr)
        x = xr[keep]
        pooled_x.append(x)
        pooled_d.append(df["day"].to_numpy()[te][m][keep])
        picks.append({"block": k, "oos_n": int(len(x)), "oos_sum": float(x.sum()) if len(x) else 0.0,
                      "oos_mean": float(x.mean()) if len(x) else float("nan"),
                      "pick": {"cell": best["cell"], "policy": best["policy"],
                               "filters": [f.label() for f in best["filters"]],
                               "in_sample_mean": best["mean"], "in_sample_n": best["n"]}})
    allx = np.concatenate(pooled_x) if pooled_x else np.array([])
    alld = np.concatenate(pooled_d) if pooled_d else np.array([])
    if len(allx):
        mean, se, n = clustered_mean_se(allx, alld)
    else:
        mean, se, n = float("nan"), float("nan"), 0
    return {"picks": picks, "oos_n": n, "oos_mean": mean, "oos_se": se, "oos_p": one_sided_p(mean, se)}


# ── stages that read the real table ─────────────────────────────────────────

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "ign_event_spec.json"
RESULTS_DIR = HERE / "cache"
PARITY_DATASET = HERE / "cache" / "ign_feature_dataset.jsonl"
REFERENCE_POLICIES = ("L|nx|s20|t1.5|m-|b-", "L|nx|s20|t2|m-|b-", "L|nx|p1|t1|m-|b-", "L|nx|p1|t0.5|m-|b-",
                      "L|nx|s20|t-|m-|b-", "S|nx|p1|t1|m-|b-")


def _struct_cols(names: Sequence[str]) -> np.ndarray:
    return np.array([n.split("|")[2].startswith("s") for n in names])


def cell_list(df) -> list[str]:
    return [c for c in df["cell"].unique() if c != "CTRL"]


def _summ(x: np.ndarray, days: np.ndarray) -> dict:
    x_ok = x[~np.isnan(x)]
    m, se, n = clustered_mean_se(x, days)
    return {"n": int(n), "mean": m, "se": se, "win": float((x_ok > 0).mean()) if n else float("nan")}


def audit(train_loader=None) -> dict:
    """Descriptive only. Nothing here selects anything."""
    from tools.replay.ign_event_table import load_train, TRAIN_END
    df, res, names = (train_loader or load_train)()
    df = prepare_table(df)
    R = net_r(res["gross"], res["risk"], COST_PCT, RISK_BAND, _struct_cols(names))
    days = df["day"].to_numpy()
    out = {"rows": len(df), "symbol_days": int(df.groupby(["symbol", "day"]).ngroups),
           "days": [df["day"].min(), df["day"].max()], "cells": {}}
    pidx = {n: i for i, n in enumerate(names)}
    blocks = block_ids(days)
    for c in sorted(df["cell"].unique()):
        m = (df["cell"] == c).to_numpy()
        rec = {"n_rows": int(m.sum()), "n_symbol_days": int(df[m].groupby(["symbol", "day"]).ngroups)}
        for pol in REFERENCE_POLICIES:
            if pol not in pidx:
                continue
            j = pidx[pol]
            s = _summ(R[m, j], days[m])
            g = res["gross"][m, j].astype(float)
            s["gross_pct"] = float(np.nanmean(g)) if np.isfinite(g).any() else float("nan")
            s["by_block"] = [float(np.nanmean(R[m & (blocks == b), j])) if np.isfinite(R[m & (blocks == b), j]).any() else float("nan")
                             for b in range(K_BLOCKS)]
            rec[pol] = s
        out["cells"][c] = rec
    # LIVE-cell parity with the earlier IGN replay study
    if PARITY_DATASET.exists() and (df["cell"] == "LIVE").any():
        prior = {}
        for line in PARITY_DATASET.open(encoding="utf-8"):
            r = json.loads(line)
            u = r.get("ungated") or r.get("live")
            if u and r["day"] <= TRAIN_END:
                prior[(r["symbol"], r["day"])] = u["ts"][11:16]
        first = df["day"].min()
        prior = {k: v for k, v in prior.items() if k[1] >= "2026-03-02"}
        live = df[(df["cell"] == "LIVE") & (df["day"] >= "2026-03-02")]
        mine = {(r.symbol, r.day): r.hhmm for r in live.itertuples()}
        both = set(prior) & set(mine)
        out["live_parity"] = {"earlier_only": len(set(prior) - set(mine)), "table_only": len(set(mine) - set(prior)),
                              "both": len(both), "same_minute": sum(1 for k in both if prior[k] == mine[k])}
    return out


def _fmt(x, w=7, d=3):
    return f"{x:{w}.{d}f}" if isinstance(x, (int, float)) and x == x else " " * (w - 3) + "nan"


def print_audit(a: dict) -> None:
    print(f"rows {a['rows']}  symbol-days {a['symbol_days']}  {a['days'][0]}..{a['days'][1]}")
    for pol in REFERENCE_POLICIES:
        print(f"\n  policy {pol}   (net R at cost {COST_PCT}%, day-clustered SE)")
        print(f"  {'cell':9s}{'rows':>7s}{'trades':>8s}{'win%':>7s}{'gross%':>8s}{'netR':>8s}{'se':>7s}   by block")
        for c, r in a["cells"].items():
            s = r.get(pol)
            if not s:
                continue
            print(f"  {c:9s}{r['n_rows']:7d}{s['n']:8d}{100 * s['win']:7.1f}{_fmt(s['gross_pct'], 8)}{_fmt(s['mean'], 8)}"
                  f"{_fmt(s['se'], 7)}   " + " ".join(_fmt(b, 6, 2) for b in s["by_block"]))
    if "live_parity" in a:
        print("\n  LIVE cell vs the earlier IGN study (same window):", a["live_parity"])


# ── freezing the design ─────────────────────────────────────────────────────

def _filter_dict(f: Filter) -> dict:
    return {"feature": f.feature, "op": f.op, "thr": f.thr}


def frontier(df, R: np.ndarray, names: Sequence[str], cell: str, filters: Sequence[Filter],
             gross: np.ndarray) -> list[dict]:
    """The win-rate / expectancy trade-off across EVERY exit policy for one cell and filter set:
    what a higher hit rate costs. Sorted by win rate, high to low."""
    rows = np.flatnonzero((df["cell"].to_numpy() == cell) & apply_filters(df, filters))
    out = []
    for j, nm in enumerate(names):
        x = R[rows, j]
        ok = ~np.isnan(x)
        if ok.sum() < 30:
            continue
        m, se, n = clustered_mean_se(x, df["day"].to_numpy()[rows])
        out.append({"policy": nm, "n": int(n), "win": float((x[ok] > 0).mean()), "net_r": m, "se": se,
                    "gross_pct": float(np.nanmean(gross[rows, j]))})
    out.sort(key=lambda d: -d["win"])
    return out


def run_select(train_loader=None, write: bool = True, spec_path: Path | None = None) -> dict:
    from datetime import datetime
    from tools.replay.ign_event_table import load_train
    df, res, names = (train_loader or load_train)()
    df = prepare_table(df)
    R = net_r(res["gross"], res["risk"], COST_PCT, RISK_BAND, _struct_cols(names))
    cells = cell_list(df)
    wf = walk_forward(df, R, names, cells)
    blocks = block_ids(df["day"].to_numpy())
    rows = {c: np.flatnonzero(df["cell"].to_numpy() == c) for c in cells}
    rows = {c: r for c, r in rows.items() if len(r) >= MIN_N}
    ranked = select_ranked(df, {c: R[r] for c, r in rows.items()}, rows, names, blocks_all=blocks,
                           min_pos_blocks=MIN_POS_BLOCKS)
    spec: dict = {"created": datetime.now().isoformat(timespec="seconds"), "cost_pct": COST_PCT,
                  "risk_band": list(RISK_BAND), "train_days": [df["day"].min(), df["day"].max()],
                  "walk_forward": {"oos_n": wf["oos_n"], "oos_mean": wf["oos_mean"], "oos_se": wf["oos_se"],
                                   "oos_p": wf["oos_p"], "picks": wf["picks"]}}
    spec["top"] = [{"cell": c["cell"], "policy": c["policy"], "filters": [f.label() for f in c["filters"]],
                    "n": c["n"], "mean": c["mean"], "se": c["se"], "lcb": c["lcb"], "win": c["win"],
                    "pos_blocks": c.get("pos_blocks")} for c in ranked[:15]]
    earned = (wf["oos_n"] >= WF_MIN_OOS_N and wf["oos_mean"] == wf["oos_mean"] and wf["oos_mean"] > WF_MIN_OOS_MEAN
              and wf["oos_p"] < WF_MAX_P)
    if ranked and earned:
        b = ranked[0]
        spec.update({"decision": "candidate", "cell": b["cell"], "policy": b["policy"],
                     "filters": [_filter_dict(f) for f in b["filters"]],
                     "train": {"n": b["n"], "mean": b["mean"], "se": b["se"], "win": b["win"],
                               "pos_blocks": b.get("pos_blocks"), "block_means": b.get("block_means")},
                     "frontier": frontier(df, R, names, b["cell"], b["filters"], res["gross"].astype(float))})
    else:
        spec.update({"decision": "no_candidate", "cell": None, "policy": None, "filters": [],
                     "why": ("no config passed selection" if not ranked else
                             f"walk-forward pooled OOS mean {wf['oos_mean']:.3f}R (p {wf['oos_p']:.2f}) on {wf['oos_n']} trades did not earn the holdout")})
    if write:
        (spec_path or SPEC_PATH).write_text(json.dumps(spec, indent=1, default=float), encoding="utf-8")
    return spec


def print_select(spec: dict) -> None:
    wf = spec["walk_forward"]
    print(f"train {spec['train_days'][0]}..{spec['train_days'][1]}   cost {spec['cost_pct']}%   risk band {spec['risk_band']}")
    print(f"\nWALK-FORWARD (the procedure run on earlier blocks, scored on the next):  pooled OOS net R "
          f"{_fmt(wf['oos_mean'], 7, 3)}R (se {_fmt(wf['oos_se'], 5, 3)}, one-sided p {_fmt(wf['oos_p'], 5, 3)}) on {wf['oos_n']} trades")
    for p in wf["picks"]:
        pk = p["pick"]
        if pk:
            print(f"   block {p['block']}: picked {pk['cell']} {pk['policy']} {pk['filters']}  in-sample {pk['in_sample_mean']:+.3f}R "
                  f"(n {pk['in_sample_n']})  ->  OOS {_fmt(p['oos_mean'], 7, 3)}R (n {p['oos_n']})")
        else:
            print(f"   block {p['block']}: nothing passed")
    print("\nTOP CONFIGS ON ALL OF TRAIN (day-clustered lower bound, in-sample; read with the walk-forward line above):")
    for t in spec["top"][:10]:
        print(f"   {t['cell']:8s} {t['policy']:24s} n {t['n']:5d}  net R {t['mean']:+.3f} (se {t['se']:.3f}, lcb {t['lcb']:+.3f})  "
              f"win {100 * t['win']:.0f}%  pos blocks {t['pos_blocks']}  {t['filters']}")
    print(f"\nDECISION: {spec['decision']}" + (f" — {spec['why']}" if spec.get("why") else ""))
    if spec.get("frontier"):
        print("\nWIN RATE vs EXPECTANCY for the chosen cell and filters, across every exit:")
        for f in spec["frontier"][:25]:
            print(f"   {f['policy']:24s} win {100 * f['win']:5.1f}%  net R {f['net_r']:+.3f}  gross {f['gross_pct']:+.3f}%  n {f['n']}")


# ── the one-look holdout ────────────────────────────────────────────────────

def load_holdout():
    """The sealed table. Only `run_reveal` calls this, and only after `preflight`."""
    import pandas as pd
    from tools.replay.ign_event_table import HOLDOUT_TABLE, TRAIN_END
    df = pd.read_json(HOLDOUT_TABLE, lines=True)
    z = np.load(HOLDOUT_TABLE.with_suffix(".policies.npz"))
    assert len(df) == len(z["gross"]), "table and policy matrix are out of step"
    assert df["day"].min() > TRAIN_END, "a train day is in the holdout table"
    return df, {k: z[k] for k in ("gross", "risk", "bars", "reason")}, [str(x) for x in z["names"]]


def holdout_decision(n: int, mean: float, se: float, ctrl_mean: float) -> dict:
    p = one_sided_p(mean, se)
    return {"n": n, "mean": mean, "se": se, "p": p, "ctrl_mean": ctrl_mean,
            "accept": bool(n >= ACCEPT_MIN_N and mean > 0 and p < ACCEPT_P and mean > ctrl_mean)}


def score_spec(df, gross: np.ndarray, risk: np.ndarray, names: Sequence[str], spec: dict,
               cost_pct: float = COST_PCT) -> dict:
    """The frozen design on `df`: the spec's cell, its filters at the spec's thresholds, its exit.
    Also the CTRL rows under the same exit, as the benchmark."""
    R = net_r(gross, risk, cost_pct, RISK_BAND, _struct_cols(names))
    j = list(names).index(spec["policy"])
    flts = [Filter(f["feature"], f["op"], f["thr"]) for f in spec["filters"]]
    d = df["day"].to_numpy()
    m = (df["cell"].to_numpy() == spec["cell"]) & apply_filters(df, flts)
    mean, se, n = clustered_mean_se(R[m, j], d[m])
    c = df["cell"].to_numpy() == "CTRL"
    cmean, cse, cn = clustered_mean_se(R[c, j], d[c])
    out = holdout_decision(n, mean, se, cmean)
    x = R[m, j]
    x = x[~np.isnan(x)]
    out.update({"win": float((x > 0).mean()) if len(x) else float("nan"), "ctrl_n": cn, "ctrl_se": cse,
                "gross_pct": float(np.nanmean(gross[m, j])) if m.any() else float("nan")})
    return out


def run_reveal(spec_path: Path | None = None) -> dict:
    from tools.replay import study_common as C
    from tools.replay import ign_event_features, ign_event_sim, ign_event_table
    spec_path = spec_path or SPEC_PATH
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("decision") != "candidate":
        return {"revealed": False, "why": f"the spec is '{spec.get('decision')}': there is no design to test, the holdout stays sealed"}
    files = [Path(__file__), Path(ign_event_table.__file__), Path(ign_event_sim.__file__),
             Path(ign_event_features.__file__), spec_path]
    ok, why, sha = C.preflight(files, RESULTS_DIR, "ign_event_holdout")
    if not ok:
        return {"revealed": False, "why": why}
    df, res, names = load_holdout()
    df = prepare_table(df)
    out = {"revealed": True, "sha": sha, "spec": {k: spec[k] for k in ("cell", "policy", "filters")},
           "holdout_days": [df["day"].min(), df["day"].max()], "by_cost": {}}
    for label, cost in COST_SENSITIVITY.items():
        out["by_cost"][label] = score_spec(df, res["gross"], res["risk"], names, spec, cost)
    out["primary"] = out["by_cost"]["production"]
    (RESULTS_DIR / f"ign_event_holdout_{sha}.json").write_text(json.dumps(out, indent=1, default=float), encoding="utf-8")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("stage", choices=("audit", "select", "reveal"))
    args = p.parse_args()
    if args.stage == "audit":
        print_audit(audit())
    elif args.stage == "select":
        print_select(run_select())
    else:
        print(json.dumps(run_reveal(), indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
