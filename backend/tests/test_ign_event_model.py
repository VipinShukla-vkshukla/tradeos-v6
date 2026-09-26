"""
IGN event study, model stage — tools/replay/ign_event_model.py.

Synthetic tables with a known truth: a planted relationship between a datapoint and the return must
be picked up out of sample; a table where every event drifts up equally (so any pick "makes money")
must NOT earn the holdout, because the model added nothing; and nothing from the calibration or test
block's outcomes may reach a fold's model or threshold.
"""

from __future__ import annotations

import json
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from tools.replay import ign_event_model as M
from tools.replay import ign_event_study as E

NAMES = [M.POLICY]


def _synth(n_days=200, per_day=25, seed=0, signal=0.0, drift=0.0, ctrl_per_day=5, noise=1.0):
    """CELL rows whose next-day-to-close return is drift + signal*f0 + noise, plus CTRL rows."""
    rng = np.random.default_rng(seed)
    d0 = date(2025, 1, 2)
    frames = []
    for d in range(n_days):
        day = (d0 + timedelta(days=d)).isoformat()
        for cell, n in ((M.CELL, per_day), ("CTRL", ctrl_per_day)):
            f = {name: rng.normal(0, 1, n) for name in M.FEATURES}
            for b in E.BOOLEAN_FEATURES:
                if b in f:
                    f[b] = (rng.random(n) < 0.5).astype(float)
            y = drift + (signal * f["idx"] if cell == M.CELL else 0.0) + rng.normal(0, noise, n)
            frames.append(pd.DataFrame(dict(f, day=day, cell=cell, symbol=[f"S{k}" for k in range(n)],
                                            fo_eod_ret_pct=y)))
    df = pd.concat(frames, ignore_index=True)
    gross = df["fo_eod_ret_pct"].to_numpy()[:, None].astype(float)
    risk = np.ones_like(gross)
    return df, gross, risk


# ── arithmetic ──────────────────────────────────────────────────────────────

def test_trades_apply_the_risk_band_and_the_cost():
    df = pd.DataFrame({"day": ["d"] * 4, "cell": M.CELL})
    gross = np.array([[1.0], [1.0], [1.0], [np.nan]])
    risk = np.array([[1.0], [0.3], [2.0], [1.0]])
    took, net_pct, R = M.trades(df, gross, risk, NAMES)
    assert list(took) == [True, False, False, False], "inside [0.4, 1.75] and a real result only"
    assert abs(net_pct[0] - (1.0 - 0.2063)) < 1e-12 and abs(R[0] - (1.0 - 0.2063) / 1.0) < 1e-12
    _, net_free, _ = M.trades(df, gross, risk, NAMES, cost_pct=0.0)
    assert net_free[0] == 1.0


def test_tau_is_the_requested_quantile_of_the_predictions():
    p = np.arange(101, dtype=float)
    assert M.tau_from(p) == 90.0 and M.tau_from(p, 0.5) == 50.0


def test_fit_ignores_rows_without_a_target_and_winsorises_it():
    rng = np.random.default_rng(3)
    X = rng.normal(0, 1, (2000, 4))
    y = 2.0 * X[:, 0] + rng.normal(0, 0.5, 2000)
    y[:50] = np.nan
    y[50:60] = 1e6                                                 # absurd outliers must not dominate the fit
    m = M.fit_model(X, y)
    pred = m.predict(X[100:])
    assert np.corrcoef(pred, X[100:, 0])[0, 1] > 0.9
    assert np.abs(pred).max() < 50, "winsorising keeps a 1e6 outlier out of the fitted values"


# ── the procedure ───────────────────────────────────────────────────────────

def test_walk_forward_finds_a_planted_relationship_and_it_beats_the_unpicked():
    df, gross, risk = _synth(signal=1.0, seed=11)
    wf = M.walk_forward(df, gross, risk, NAMES)
    assert wf["picked"]["n"] >= M.WF_MIN_N and wf["picked"]["mean"] > 1.0, wf["picked"]
    assert wf["diff"]["mean"] > 1.0 and wf["diff"]["p"] < 0.01
    assert [f["block"] for f in wf["folds"]] == list(M.WF_TEST_BLOCKS)


def test_a_table_where_every_event_drifts_up_does_not_earn_the_holdout():
    earned = 0
    for seed in range(6):
        df, gross, risk = _synth(signal=0.0, drift=0.6, seed=200 + seed, n_days=160)
        spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=Path(tempfile.gettempdir()) / "unused.joblib")
        earned += spec["decision"] == "candidate"
    assert earned <= 1, f"{earned} of 6 no-skill tables earned the holdout; the picked-vs-unpicked test is not biting"


def test_freeze_writes_a_model_whose_hash_is_in_the_spec():
    df, gross, risk = _synth(signal=1.0, seed=12)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.joblib"
        spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=path)
        assert spec["decision"] == "candidate" and spec["tau"] == spec["tau"]
        assert spec["model_sha256"] == M.sha256_file(path)


def test_only_the_fit_blocks_outcomes_reach_a_fold():
    df, gross, risk = _synth(signal=1.0, seed=13, n_days=150)
    took, net_pct, _ = M.trades(df, gross, risk, NAMES)
    blk = E.block_ids(df["day"].to_numpy(), M.K_BLOCKS)
    base = M.wf_fold(df, took, net_pct, blk, 3)
    scrambled = df.copy()
    late = blk >= 2                                                  # the calibration block and everything after
    rng = np.random.default_rng(0)
    scrambled.loc[late, M.TARGET] = rng.normal(50, 30, int(late.sum()))
    again = M.wf_fold(scrambled, took, net_pct, blk, 3)
    assert again["tau"] == base["tau"] and (again["pick"] == base["pick"]).all(), (
        "the threshold and the picks may depend on features alone for blocks 2 and 3")
    early = df.copy()
    early.loc[blk <= 1, M.TARGET] = rng.normal(0, 1, int((blk <= 1).sum()))
    changed = M.wf_fold(early, took, net_pct, blk, 3)
    assert changed["tau"] != base["tau"], "the fit blocks' outcomes DO matter (otherwise the model learned nothing)"


def test_the_calibration_block_is_never_scored():
    df, gross, risk = _synth(signal=1.0, seed=14, n_days=150)
    took, net_pct, _ = M.trades(df, gross, risk, NAMES)
    blk = E.block_ids(df["day"].to_numpy(), M.K_BLOCKS)
    f = M.wf_fold(df, took, net_pct, blk, 4)
    assert not (f["pick"] & (blk != 4)).any(), "only the test block's events can be picked"
    assert not (f["pick"] & (df["cell"].to_numpy() != M.CELL)).any()


# ── holdout ─────────────────────────────────────────────────────────────────

def _frozen(seed=15):
    df, gross, risk = _synth(signal=1.0, seed=seed, n_days=150)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.joblib"
        spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=path)
        import joblib
        return spec, joblib.load(path)


def test_the_holdout_uses_the_frozen_threshold_and_model_and_benchmarks_against_ctrl():
    spec, model = _frozen()
    df, gross, risk = _synth(signal=1.0, seed=99, n_days=60)
    r = M.score_holdout(df, gross, risk, NAMES, spec, model)
    assert r["accept"] and r["picked"]["mean"] > r["not_picked"]["mean"] and r["picked"]["mean"] > r["ctrl"]["mean"], r
    assert 0.02 < r["share_picked"] < 0.3, "roughly the top decile passes a threshold frozen on the calibration block"
    df0, g0, k0 = _synth(signal=0.0, seed=98, n_days=60)
    assert not M.score_holdout(df0, g0, k0, NAMES, spec, model)["accept"], "a model with nothing to find is refused"
    hi = dict(spec, tau=1e9)
    assert M.score_holdout(df, gross, risk, NAMES, hi, model)["picked"]["n"] == 0, "the threshold is the frozen one"


def test_a_lifted_control_beats_the_pick_and_refuses_it():
    spec, model = _frozen(16)
    df, gross, risk = _synth(signal=1.0, seed=97, n_days=60)
    gross = gross.copy()
    gross[(df["cell"] == "CTRL").to_numpy()] += 6.0                  # random entries did far better
    assert not M.score_holdout(df, gross, risk, NAMES, spec, model)["accept"]


class _Sealed:
    def __enter__(self):
        self.old, self.called = E.load_holdout, 0

        def boom():
            self.called += 1
            raise AssertionError("the holdout was read")
        E.load_holdout = boom
        return self

    def __exit__(self, *a):
        E.load_holdout = self.old


def test_reveal_stays_sealed_unless_the_spec_is_a_candidate_with_its_own_model_and_a_clean_preflight():
    from tools.replay import study_common as C
    with tempfile.TemporaryDirectory() as tmp, _Sealed() as sealed:
        sp = Path(tmp) / "spec.json"
        sp.write_text(json.dumps({"decision": "no_candidate"}))
        assert M.run_reveal(sp)["revealed"] is False and sealed.called == 0
        old_path = M.MODEL_PATH
        mp = Path(tmp) / "model.joblib"
        mp.write_bytes(b"not the frozen model")
        M.MODEL_PATH = mp
        try:
            sp.write_text(json.dumps({"decision": "candidate", "model_file": "model.joblib", "model_sha256": "0" * 64}))
            out = M.run_reveal(sp)
            assert out["revealed"] is False and "not the one the spec froze" in out["why"] and sealed.called == 0
            sp.write_text(json.dumps({"decision": "candidate", "model_file": "model.joblib",
                                      "model_sha256": M.sha256_file(mp)}))
            old = C.preflight
            C.preflight = lambda files, d, prefix: (False, "uncommitted changes (R2)", "")
            try:
                out = M.run_reveal(sp)
            finally:
                C.preflight = old
            assert out["revealed"] is False and "uncommitted" in out["why"] and sealed.called == 0
        finally:
            M.MODEL_PATH = old_path


def test_reveal_scores_once_and_writes_a_guard_file():
    import joblib
    from tools.replay import study_common as C
    spec, model = _frozen(17)
    df, gross, risk = _synth(signal=1.0, seed=96, n_days=60)
    df = E.prepare_table(df)
    with tempfile.TemporaryDirectory() as tmp:
        mp = Path(tmp) / "model.joblib"
        joblib.dump(model, mp)
        spec = dict(spec, model_file="model.joblib", model_sha256=M.sha256_file(mp))
        sp = Path(tmp) / "spec.json"
        sp.write_text(json.dumps(spec, default=float))
        old = (M.MODEL_PATH, M.RESULTS_DIR, C.preflight, E.load_holdout)
        M.MODEL_PATH, M.RESULTS_DIR = mp, Path(tmp)
        C.preflight = lambda files, d, prefix: (True, "", "abc123def456")
        E.load_holdout = lambda: (df, {"gross": gross, "risk": risk, "bars": gross, "reason": gross}, NAMES)
        try:
            out = M.run_reveal(sp)
            written = json.loads((Path(tmp) / "ign_event_model_holdout_abc123def456.json").read_text())
        finally:
            M.MODEL_PATH, M.RESULTS_DIR, C.preflight, E.load_holdout = old
    assert out["revealed"] and set(out["by_cost"]) == set(M.COST_SENSITIVITY)
    means = [out["by_cost"][k]["picked"]["mean"] for k in ("charges_only", "production", "slip_10bps_leg")]
    assert means[0] > means[1] > means[2], "a higher cost can only lower net %"
    assert written["primary"]["accept"] == out["primary"]["accept"]


def test_tau_comes_from_the_calibration_blocks_predictions():
    df, gross, risk = _synth(signal=1.0, seed=21, n_days=150)
    took, net_pct, _ = M.trades(df, gross, risk, NAMES)
    blk = E.block_ids(df["day"].to_numpy(), M.K_BLOCKS)
    f = M.wf_fold(df, took, net_pct, blk, 3)
    cell = df["cell"].to_numpy() == M.CELL
    model = M.fit_model(M._matrix(df)[cell & (blk <= 1)], df[M.TARGET].to_numpy(float)[cell & (blk <= 1)])
    cal = cell & (blk == 2) & took
    assert f["tau"] == M.tau_from(model.predict(M._matrix(df)[cal])), "TAU is the 90th percentile of block 2's predictions"


def test_picked_and_not_picked_partition_the_test_trades():
    df, gross, risk = _synth(signal=1.0, seed=22, n_days=150)
    wf = M.walk_forward(df, gross, risk, NAMES)
    took, _, _ = M.trades(df, gross, risk, NAMES)
    blk = E.block_ids(df["day"].to_numpy(), M.K_BLOCKS)
    expected = int(((df["cell"].to_numpy() == M.CELL) & took & np.isin(blk, M.WF_TEST_BLOCKS)).sum())
    assert wf["picked"]["n"] + wf["not_picked"]["n"] == expected, "every test-block trade is either picked or not, never both"


def test_the_freeze_gate_refuses_a_positive_but_insignificant_pick():
    df, gross, risk = _synth(signal=0.5, drift=-0.4, noise=2.5, seed=23, n_days=160)
    wf = M.walk_forward(df, gross, risk, NAMES)
    assert wf["picked"]["mean"] > 0 and wf["picked"]["p"] > M.WF_MAX_P and wf["diff"]["p"] < M.WF_MAX_P, (
        "the fixture isolates the p gate: a positive mean, not significant, clearly better than the unpicked", wf["picked"], wf["diff"])
    spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=Path(tempfile.gettempdir()) / "unused.joblib")
    assert spec["decision"] == "no_candidate"


def test_the_freeze_gate_needs_enough_trades():
    df, gross, risk = _synth(signal=1.5, n_days=60, seed=24)
    wf = M.walk_forward(df, gross, risk, NAMES)
    assert wf["picked"]["n"] < M.WF_MIN_N and wf["picked"]["mean"] > 1.0 and wf["picked"]["p"] < 0.001 and wf["diff"]["p"] < 0.001
    spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=Path(tempfile.gettempdir()) / "unused.joblib")
    assert spec["decision"] == "no_candidate", "a strong result on too few trades does not earn the holdout"


def test_the_frozen_model_is_fitted_without_the_calibration_block():
    df, gross, risk = _synth(signal=1.0, seed=25, n_days=150)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.joblib"
        spec = M.freeze(df, gross, risk, NAMES, write=False, model_path=path)
    took, _, _ = M.trades(df, gross, risk, NAMES)
    blk = E.block_ids(df["day"].to_numpy(), M.K_BLOCKS)
    cell = df["cell"].to_numpy() == M.CELL
    model = M.fit_model(M._matrix(df)[cell & (blk <= 3)], df[M.TARGET].to_numpy(float)[cell & (blk <= 3)])
    expected = M.tau_from(model.predict(M._matrix(df)[cell & (blk == 4) & took]))
    assert spec["tau"] == expected, "the final model sees blocks 0..3 and TAU is set on block 4, out of its fit"


def test_a_pick_that_is_worse_than_the_events_it_skipped_is_refused_even_above_random_entries():
    spec, model = _frozen(26)
    df, gross, risk = _synth(signal=-1.0, drift=3.0, seed=95, n_days=60)
    gross = gross.copy()
    gross[(df["cell"] == "CTRL").to_numpy()] -= 3.0                   # random entries earn nothing
    r = M.score_holdout(df, gross, risk, NAMES, spec, model)
    assert r["picked"]["mean"] > 0 and r["picked"]["p"] < 0.05 and r["picked"]["mean"] > r["ctrl"]["mean"], r
    assert r["picked"]["mean"] < r["not_picked"]["mean"] and not r["accept"], "the model picked the WORSE events"


def test_a_positive_but_insignificant_holdout_pick_is_refused():
    spec, model = _frozen(27)
    df, gross, risk = _synth(signal=0.8, noise=8.0, seed=94, n_days=60)
    r = M.score_holdout(df, gross, risk, NAMES, spec, model)
    assert r["picked"]["n"] >= M.ACCEPT_MIN_N and r["picked"]["mean"] > 0 and r["picked"]["p"] > M.ACCEPT_P, r
    assert r["picked"]["mean"] > r["not_picked"]["mean"] and r["picked"]["mean"] > r["ctrl"]["mean"]
    assert not r["accept"], "better than everything else but not distinguishable from luck"


def test_the_spec_files_can_be_committed():
    """preflight refuses uncommitted files, and a blanket *.json rule in .gitignore would make a
    frozen spec impossible to commit: the holdout could then never be opened, by accident or design."""
    import subprocess
    root = Path(M.__file__).resolve().parents[3]
    for path in (M.SPEC_PATH, E.SPEC_PATH):
        try:
            r = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=root, capture_output=True)
        except FileNotFoundError:
            return
        assert r.returncode == 1, f"{path.name} is git-ignored, so the freeze cannot be committed"


TESTS = [
    ("trades apply the risk band and the cost", test_trades_apply_the_risk_band_and_the_cost),
    ("tau is the requested quantile of the predictions", test_tau_is_the_requested_quantile_of_the_predictions),
    ("fit ignores rows without a target and winsorises it", test_fit_ignores_rows_without_a_target_and_winsorises_it),
    ("walk-forward finds a planted relationship and it beats the unpicked",
     test_walk_forward_finds_a_planted_relationship_and_it_beats_the_unpicked),
    ("a table where every event drifts up does not earn the holdout",
     test_a_table_where_every_event_drifts_up_does_not_earn_the_holdout),
    ("freeze writes a model whose hash is in the spec", test_freeze_writes_a_model_whose_hash_is_in_the_spec),
    ("only the fit blocks' outcomes reach a fold", test_only_the_fit_blocks_outcomes_reach_a_fold),
    ("the calibration block is never scored", test_the_calibration_block_is_never_scored),
    ("the holdout uses the frozen threshold and model and benchmarks against CTRL",
     test_the_holdout_uses_the_frozen_threshold_and_model_and_benchmarks_against_ctrl),
    ("a lifted control beats the pick and refuses it", test_a_lifted_control_beats_the_pick_and_refuses_it),
    ("reveal stays sealed unless candidate + own model + clean preflight",
     test_reveal_stays_sealed_unless_the_spec_is_a_candidate_with_its_own_model_and_a_clean_preflight),
    ("reveal scores once and writes a guard file", test_reveal_scores_once_and_writes_a_guard_file),
    ("tau comes from the calibration block's predictions", test_tau_comes_from_the_calibration_blocks_predictions),
    ("picked and not-picked partition the test trades", test_picked_and_not_picked_partition_the_test_trades),
    ("the freeze gate refuses a positive but insignificant pick",
     test_the_freeze_gate_refuses_a_positive_but_insignificant_pick),
    ("the freeze gate needs enough trades", test_the_freeze_gate_needs_enough_trades),
    ("the frozen model is fitted without the calibration block",
     test_the_frozen_model_is_fitted_without_the_calibration_block),
    ("a pick worse than the events it skipped is refused even above random entries",
     test_a_pick_that_is_worse_than_the_events_it_skipped_is_refused_even_above_random_entries),
    ("a positive but insignificant holdout pick is refused", test_a_positive_but_insignificant_holdout_pick_is_refused),
    ("the spec files can be committed", test_the_spec_files_can_be_committed),
]
