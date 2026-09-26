"""
IGN event study, model stage — does a walk-forward model pick out ignition events that keep going?

    python -m tools.replay.ign_event_model walkforward    # validate the procedure on TRAIN, write the spec
    python -m tools.replay.ign_event_model reveal         # the sealed holdout, once, if the spec earned it

WHY A MODEL. The filter search (ign_event_study.py) mines ~10^6 (cell, exit, filter) combinations and
its winners fail out of sample; a planted-edge power check showed it cannot see conjunction-only
effects at this sample size. A model uses every row and every datapoint at once and is scored only on
data it never saw. An EXPLORATORY look on train (not this procedure; disclosed so it is not mistaken
for a clean result) had found weak ranking skill: out-of-sample Spearman +0.04 to +0.10 between the
prediction and the realised return, and for the model's top decile of T4_V1 events a gross hold-to-close
return of about +0.5% against a 0.21% round-trip cost. The cell and the exit below were CHOSEN from
that look, so the train numbers this stage prints are optimistic by the size of the choice. The sealed
holdout is what settles it.

PRE-REGISTRATION (committed before this procedure was run or the holdout opened).

  events      cell CELL (move >= 4% over the prior close, IGN volume ratio >= 1), one row per symbol-day.
  model       HistGradientBoostingRegressor(MODEL_PARAMS) on FEATURES; the target is the gross % return
              from the next bar's open to the last bar before the square-off, winsorised at the fit
              window's 1st/99th percentiles (fitting only).
  rule        take an event iff its predicted return >= TAU, the TOP_Q quantile of the model's
              predictions on a CALIBRATION block that the model was not fitted on.
  execution   policy POLICY (IGN's own 20-bar structural stop, no target, out at 15:14), risk band
              [0.4, 1.75]%; net % = the policy's gross % - COST_PCT (0.2063: the production MIS model
              including 5 bps/leg slippage). An event outside the risk band is not a trade.
  procedure   walk-forward on the K_BLOCKS chronological blocks of train: for test block k in
              WF_TEST_BLOCKS, fit on blocks <= k-2, calibrate TAU on block k-1, score block k. The pooled
              out-of-sample trades must show mean net % > 0 with day-clustered one-sided p < WF_MAX_P on
              at least WF_MIN_N trades, AND must beat the events the model did not pick (difference,
              one-sided p < WF_MAX_P: a market that lifts every event is not the model's doing), or the
              spec records "no_candidate" and the holdout stays sealed.
  freeze      fit on blocks 0..K-2, calibrate TAU on the last block, persist the model (its SHA-256 goes
              in the spec) and commit the spec.
  holdout     ACCEPT iff, on the holdout: n >= ACCEPT_MIN_N trades, mean net % > 0 with day-clustered
              one-sided p < ACCEPT_P, mean net % above that of the CELL's events the model did NOT pick,
              and above the CTRL rows under the same policy.
              Reported, not decisive: the same at charges-only and 10 bps/leg costs, and the mean R.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.replay import ign_event_study as E

# ── pre-registered constants ────────────────────────────────────────────────
CELL = "T4_V1"
POLICY = "L|nx|s20|t-|m-|b-"
TARGET = "fo_eod_ret_pct"
COST_PCT = E.COST_PCT
COST_SENSITIVITY = E.COST_SENSITIVITY
TOP_Q = 0.90
MODEL_PARAMS = dict(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=200,
                    l2_regularization=5.0, random_state=0)
WINSOR = (0.01, 0.99)
K_BLOCKS = 5
WF_TEST_BLOCKS = (3, 4)
WF_MIN_N = 100
WF_MAX_P = 0.10
ACCEPT_MIN_N = 60
ACCEPT_P = 0.05
FEATURES = tuple(f for f in E.NUMERIC_FEATURES + E.BOOLEAN_FEATURES if f not in ("risk_pct", "feasible"))

HERE = Path(__file__).resolve().parent
SPEC_PATH = HERE / "ign_event_model_spec.json"
MODEL_PATH = HERE / "cache" / "ign_event_model.joblib"
RESULTS_DIR = HERE / "cache"


# ── the model ───────────────────────────────────────────────────────────────

def _matrix(df, features: Sequence[str] = FEATURES) -> np.ndarray:
    return df[list(features)].to_numpy(dtype=float)


def fit_model(X: np.ndarray, y: np.ndarray):
    """Fit on winsorised targets. Rows with a missing target are not used."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    ok = ~np.isnan(y)
    lo, hi = np.quantile(y[ok], WINSOR)
    m = HistGradientBoostingRegressor(**MODEL_PARAMS)
    m.fit(X[ok], np.clip(y[ok], lo, hi))
    return m


def tau_from(pred: np.ndarray, q: float = TOP_Q) -> float:
    return float(np.quantile(pred, q))


def trades(df, gross: np.ndarray, risk: np.ndarray, names: Sequence[str], policy: str = POLICY,
           cost_pct: float = COST_PCT):
    """(mask of rows that were trades, net % per row) for one policy, under the risk band."""
    j = list(names).index(policy)
    R = E.net_r(gross[:, j:j + 1], risk[:, j:j + 1], cost_pct, E.RISK_BAND, E._struct_cols([names[j]]))
    took = ~np.isnan(R[:, 0])
    return took, gross[:, j].astype(float) - cost_pct, R[:, 0]


def _summ(x: np.ndarray, days: np.ndarray) -> dict:
    mean, se, n = E.clustered_mean_se(x, days)
    return {"n": int(n), "mean": mean, "se": se, "p": E.one_sided_p(mean, se) if n else 1.0}


# ── walk-forward ────────────────────────────────────────────────────────────

def wf_fold(df, took: np.ndarray, net_pct: np.ndarray, blk: np.ndarray, k: int) -> dict:
    """One fold: fit on blocks <= k-2, calibrate TAU on block k-1, score block k. Only the fit
    blocks' outcomes are ever read; the calibration and test blocks contribute features alone."""
    cell = (df["cell"].to_numpy() == CELL)
    fit = cell & (blk <= k - 2)
    cal = cell & (blk == k - 1) & took
    test = cell & (blk == k) & took
    X = _matrix(df)
    y = df[TARGET].to_numpy(dtype=float)
    model = fit_model(X[fit], y[fit])
    tau = tau_from(model.predict(X[cal]))
    pred = np.full(len(df), np.nan)
    pred[test] = model.predict(X[test])
    pick = test & (pred >= tau)
    return {"k": k, "tau": tau, "pick": pick, "test": test, "pred": pred, "n_fit": int(fit.sum())}


def walk_forward(df, gross: np.ndarray, risk: np.ndarray, names: Sequence[str]) -> dict:
    took, net_pct, _ = trades(df, gross, risk, names)
    blk = E.block_ids(df["day"].to_numpy(), K_BLOCKS)
    days = df["day"].to_numpy()
    folds, picks, rest = [], np.zeros(len(df), bool), np.zeros(len(df), bool)
    for k in WF_TEST_BLOCKS:
        f = wf_fold(df, took, net_pct, blk, k)
        picks |= f["pick"]
        rest |= f["test"] & ~f["pick"]
        folds.append({"block": k, "tau": f["tau"], "n_fit": f["n_fit"], "picked": _summ(net_pct[f["pick"]], days[f["pick"]]),
                      "not_picked": _summ(net_pct[f["test"] & ~f["pick"]], days[f["test"] & ~f["pick"]])})
    out = {"folds": folds, "picked": _summ(net_pct[picks], days[picks]),
           "not_picked": _summ(net_pct[rest], days[rest])}
    out["picked"]["gross_mean"] = float(np.nanmean(net_pct[picks] + COST_PCT)) if picks.any() else float("nan")
    a, b = out["picked"], out["not_picked"]
    se = float(np.sqrt(a["se"] ** 2 + b["se"] ** 2)) if a["se"] == a["se"] and b["se"] == b["se"] else float("nan")
    out["diff"] = {"mean": a["mean"] - b["mean"], "se": se, "p": E.one_sided_p(a["mean"] - b["mean"], se)}
    return out


# ── freezing ────────────────────────────────────────────────────────────────

def sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def freeze(df, gross, risk, names, *, model_path: Path = MODEL_PATH, write: bool = True,
           spec_path: Path | None = None) -> dict:
    import joblib
    from datetime import datetime
    wf = walk_forward(df, gross, risk, names)
    took, _, _ = trades(df, gross, risk, names)
    blk = E.block_ids(df["day"].to_numpy(), K_BLOCKS)
    earned = (wf["picked"]["n"] >= WF_MIN_N and wf["picked"]["mean"] == wf["picked"]["mean"]
              and wf["picked"]["mean"] > 0 and wf["picked"]["p"] < WF_MAX_P and wf["diff"]["p"] < WF_MAX_P)
    spec = {"created": datetime.now().isoformat(timespec="seconds"), "cell": CELL, "policy": POLICY,
            "cost_pct": COST_PCT, "walk_forward": wf, "train_days": [df["day"].min(), df["day"].max()],
            "params": MODEL_PARAMS, "top_q": TOP_Q, "features": list(FEATURES)}
    if not earned:
        spec.update({"decision": "no_candidate",
                     "why": (f"walk-forward picked {wf['picked']['n']} trades, mean net "
                             f"{wf['picked']['mean']:+.3f}% (p {wf['picked']['p']:.2f}), beating the unpicked by "
                             f"{wf['diff']['mean']:+.3f}% (p {wf['diff']['p']:.2f}); the gate needs >= {WF_MIN_N} trades, "
                             f"a positive mean and both p < {WF_MAX_P}")})
    else:
        cell = (df["cell"].to_numpy() == CELL)
        X = _matrix(df)
        y = df[TARGET].to_numpy(dtype=float)
        last = K_BLOCKS - 1
        model = fit_model(X[cell & (blk <= last - 1)], y[cell & (blk <= last - 1)])
        tau = tau_from(model.predict(X[cell & (blk == last) & took]))
        model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, model_path)
        spec.update({"decision": "candidate", "tau": tau, "model_sha256": sha256_file(model_path),
                     "model_file": model_path.name})
    if write:
        (spec_path or SPEC_PATH).write_text(json.dumps(spec, indent=1, default=float), encoding="utf-8")
    return spec


# ── the one-look holdout ────────────────────────────────────────────────────

def score_holdout(df, gross, risk, names, spec: dict, model, cost_pct: float = COST_PCT) -> dict:
    took, net_pct, R = trades(df, gross, risk, names, spec["policy"], cost_pct)
    days = df["day"].to_numpy()
    cell = (df["cell"].to_numpy() == spec["cell"]) & took
    X = _matrix(df, spec["features"])
    pred = np.full(len(df), np.nan)
    pred[cell] = model.predict(X[cell])
    pick = cell & (pred >= spec["tau"])
    rest = cell & ~pick
    ctrl = (df["cell"].to_numpy() == "CTRL") & took
    picked, rested, ctrl_s = _summ(net_pct[pick], days[pick]), _summ(net_pct[rest], days[rest]), _summ(net_pct[ctrl], days[ctrl])
    accept = bool(picked["n"] >= ACCEPT_MIN_N and picked["mean"] > 0 and picked["p"] < ACCEPT_P
                  and picked["mean"] > rested["mean"] and picked["mean"] > ctrl_s["mean"])
    return {"picked": picked, "not_picked": rested, "ctrl": ctrl_s, "accept": accept,
            "mean_r": float(np.nanmean(R[pick])) if pick.any() else float("nan"),
            "win_pct": float((net_pct[pick] > 0).mean() * 100) if pick.any() else float("nan"),
            "share_picked": float(pick.sum() / max(1, cell.sum()))}


def run_reveal(spec_path: Path | None = None) -> dict:
    import joblib
    from tools.replay import study_common as C
    from tools.replay import ign_event_features, ign_event_sim, ign_event_table
    spec_path = spec_path or SPEC_PATH
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("decision") != "candidate":
        return {"revealed": False, "why": f"the spec is '{spec.get('decision')}': there is no model to test, the holdout stays sealed"}
    model_path = MODEL_PATH.with_name(spec["model_file"])
    if not model_path.exists() or sha256_file(model_path) != spec["model_sha256"]:
        return {"revealed": False, "why": "the model file is missing or is not the one the spec froze"}
    files = [Path(__file__), Path(E.__file__), Path(ign_event_table.__file__), Path(ign_event_sim.__file__),
             Path(ign_event_features.__file__), spec_path]
    ok, why, sha = C.preflight(files, RESULTS_DIR, "ign_event_model_holdout")
    if not ok:
        return {"revealed": False, "why": why}
    df, res, names = E.load_holdout()
    df = E.prepare_table(df)
    model = joblib.load(model_path)
    out = {"revealed": True, "sha": sha, "holdout_days": [df["day"].min(), df["day"].max()], "by_cost": {}}
    for label, c in COST_SENSITIVITY.items():
        out["by_cost"][label] = score_holdout(df, res["gross"], res["risk"], names, spec, model, c)
    out["primary"] = out["by_cost"]["production"]
    (RESULTS_DIR / f"ign_event_model_holdout_{sha}.json").write_text(json.dumps(out, indent=1, default=float), encoding="utf-8")
    return out


def _load_train():
    from tools.replay.ign_event_table import load_train
    df, res, names = load_train()
    return E.prepare_table(df), res["gross"], res["risk"], names


def print_wf(spec: dict) -> None:
    wf = spec["walk_forward"]
    print(f"cell {spec['cell']}  policy {spec['policy']}  cost {spec['cost_pct']}%  top {100 * (1 - spec['top_q']):.0f}% by predicted return")
    for f in wf["folds"]:
        a, b = f["picked"], f["not_picked"]
        print(f"  test block {f['block']}: fitted on {f['n_fit']} rows, tau {f['tau']:+.3f}%   picked n {a['n']:4d} net {a['mean']:+.3f}% (se {a['se']:.3f})"
              f"   not picked n {b['n']:4d} net {b['mean']:+.3f}% (se {b['se']:.3f})")
    a, b = wf["picked"], wf["not_picked"]
    print(f"  POOLED out-of-sample: picked n {a['n']} net {a['mean']:+.3f}% (se {a['se']:.3f}, one-sided p {a['p']:.3f}, gross {a['gross_mean']:+.3f}%)"
          f"   not picked n {b['n']} net {b['mean']:+.3f}%   difference {wf['diff']['mean']:+.3f}% (p {wf['diff']['p']:.3f})")
    print(f"\nDECISION: {spec['decision']}" + (f" — {spec['why']}" if spec.get("why") else ""))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("stage", choices=("walkforward", "reveal"))
    args = p.parse_args()
    if args.stage == "walkforward":
        df, gross, risk, names = _load_train()
        spec = freeze(df, gross, risk, names)
        print_wf(spec)
    else:
        print(json.dumps(run_reveal(), indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
