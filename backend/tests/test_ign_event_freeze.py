"""
IGN event study — freezing the design and the one-look holdout (ign_event_study.run_select,
score_spec, run_reveal).

What matters here is what the holdout can and cannot do: a design chosen on noise must be refused
at the door, a frozen spec must be scored at ITS thresholds (not re-fitted to the new rows), and the
sealed table must be unreachable unless the pre-registration says the procedure earned it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np

from tests.test_ign_event_study import _frame
from tools.replay import ign_event_study as E

NAMES = ["L|nx|p1|t1|m-|b-", "L|nx|p1|t2|m-|b-"]        # fixed-percent stops: net R is what we planted


def _arrays(R):
    return {"gross": R + E.COST_PCT, "risk": np.ones_like(R), "bars": np.zeros_like(R), "reason": np.zeros_like(R)}


def _loader(plant, seed, n_days=150, cells=("A", "B", "CTRL")):
    df, R = _frame(plant=plant, seed=seed, n_days=n_days, cells=cells)
    return lambda: (df, _arrays(R), NAMES)


SPEC = {"cell": "A", "policy": NAMES[1], "filters": [{"feature": "move_pct", "op": ">=", "thr": 0.85}]}


def test_the_holdout_decision_needs_every_condition():
    ok = E.holdout_decision(100, 0.2, 0.1, 0.0)
    assert ok["accept"] and abs(ok["p"] - 0.0228) < 1e-3
    assert not E.holdout_decision(99, 0.2, 0.1, 0.0)["accept"], "too few trades"
    assert not E.holdout_decision(100, -0.1, 0.01, -0.5)["accept"], "a negative mean never passes"
    assert not E.holdout_decision(100, 0.2, 0.2, 0.0)["accept"], "p = 0.16 is not below 0.05"
    assert not E.holdout_decision(100, 0.2, 0.1, 0.25)["accept"], "no better than random entries under the same exit"


def test_select_freezes_a_planted_edge():
    spec = E.run_select(_loader(0.9, 21), write=False)
    assert spec["decision"] == "candidate", spec.get("why")
    assert spec["cell"] == "A" and spec["policy"] == NAMES[1]
    assert any(f["feature"] == "move_pct" for f in spec["filters"]), spec["filters"]
    assert spec["walk_forward"]["oos_p"] < E.WF_MAX_P and spec["walk_forward"]["oos_mean"] > 0.3
    assert spec["frontier"] and spec["frontier"] == sorted(spec["frontier"], key=lambda f: -f["win"])


def test_select_refuses_to_freeze_on_noise_most_of_the_time():
    frozen = 0
    for seed in range(14):
        spec = E.run_select(_loader(0.0, 300 + seed, n_days=120), write=False)
        frozen += spec["decision"] == "candidate"
    assert frozen <= 4, f"{frozen} of 14 noise tables earned the holdout; the gate is p<{E.WF_MAX_P} so ~1.4 expected"


def test_a_frozen_spec_is_accepted_on_a_fresh_planted_draw_and_refused_on_noise():
    df, R = _frame(plant=0.9, seed=41, cells=("A", "B", "CTRL"))
    got = E.score_spec(df, R + E.COST_PCT, np.ones_like(R), NAMES, SPEC)
    assert got["accept"] and got["mean"] > 0.5 and got["n"] > 300, got
    df0, R0 = _frame(plant=0.0, seed=42, cells=("A", "B", "CTRL"))
    assert not E.score_spec(df0, R0 + E.COST_PCT, np.ones_like(R0), NAMES, SPEC)["accept"]


def test_the_false_accept_rate_on_noise_is_near_the_nominal_five_percent():
    accepted = 0
    for seed in range(60):
        df, R = _frame(plant=0.0, seed=500 + seed, n_days=60, cells=("A", "CTRL"))
        accepted += E.score_spec(df, R + E.COST_PCT, np.ones_like(R), NAMES, SPEC)["accept"]
    assert accepted <= 9, f"{accepted} of 60 noise draws were accepted; nominal is ~3 (p < 0.05 and above the control)"


def test_score_spec_uses_the_frozen_threshold_not_the_new_rows_quantiles():
    df, R = _frame(plant=0.0, seed=43, n_days=40, cells=("A", "CTRL"))
    shifted = df.copy()
    shifted["vix"] = shifted["vix"] + 10.0                        # every row is now above the frozen 20
    spec = {"cell": "A", "policy": NAMES[0], "filters": [{"feature": "vix", "op": ">=", "thr": 20.0}]}
    got = E.score_spec(shifted, R + E.COST_PCT, np.ones_like(R), NAMES, spec)
    assert got["n"] == int((shifted["cell"] == "A").sum()), "a frozen threshold selects every row above it"
    none = E.score_spec(df, R + E.COST_PCT, np.ones_like(R), NAMES, spec)
    assert none["n"] == 0 or none["n"] < 5, "and only the rows above it in the original data"


def _spec_file(tmp, decision):
    p = Path(tmp) / "spec.json"
    body = dict(SPEC, decision=decision) if decision == "candidate" else {"decision": decision, "cell": None}
    p.write_text(json.dumps(body))
    return p


class _Sealed:
    """Make any read of the holdout table a test failure, and record whether it was attempted."""

    def __enter__(self):
        self.old = E.load_holdout
        self.called = 0

        def boom():
            self.called += 1
            raise AssertionError("the holdout was read")
        E.load_holdout = boom
        return self

    def __exit__(self, *a):
        E.load_holdout = self.old


def test_reveal_leaves_the_holdout_sealed_when_the_spec_has_no_candidate():
    with tempfile.TemporaryDirectory() as tmp, _Sealed() as sealed:
        out = E.run_reveal(_spec_file(tmp, "no_candidate"))
    assert out["revealed"] is False and sealed.called == 0 and "sealed" in out["why"]


def test_reveal_refuses_when_preflight_refuses():
    from tools.replay import study_common as C
    old = C.preflight
    C.preflight = lambda files, d, prefix: (False, "study file has uncommitted changes (R2)", "")
    try:
        with tempfile.TemporaryDirectory() as tmp, _Sealed() as sealed:
            out = E.run_reveal(_spec_file(tmp, "candidate"))
    finally:
        C.preflight = old
    assert out["revealed"] is False and "uncommitted" in out["why"] and sealed.called == 0


def test_reveal_scores_once_at_every_cost_and_writes_its_guard_file():
    from tools.replay import study_common as C
    df, R = _frame(plant=0.9, seed=44, cells=("A", "B", "CTRL"))
    old_pf, old_ld, old_dir = C.preflight, E.load_holdout, E.RESULTS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        C.preflight = lambda files, d, prefix: (True, "", "abc123def456")
        E.load_holdout = lambda: (df, _arrays(R), NAMES)
        E.RESULTS_DIR = Path(tmp)
        try:
            out = E.run_reveal(_spec_file(tmp, "candidate"))
            written = json.loads((Path(tmp) / "ign_event_holdout_abc123def456.json").read_text())
        finally:
            C.preflight, E.load_holdout, E.RESULTS_DIR = old_pf, old_ld, old_dir
    assert out["revealed"] and set(out["by_cost"]) == set(E.COST_SENSITIVITY)
    means = [out["by_cost"][k]["mean"] for k in ("charges_only", "production", "slip_10bps_leg")]
    assert means[0] > means[1] > means[2], "a higher cost can only lower net R"
    assert written["primary"]["accept"] == out["primary"]["accept"]


def test_a_selection_that_does_not_beat_random_entries_is_refused():
    df, R = _frame(plant=0.4, seed=45, cells=("A", "B", "CTRL"))
    R = R.copy()
    R[(df["cell"] == "CTRL").to_numpy()] += 0.6              # a market that lifts every entry, planted or not
    got = E.score_spec(df, R + E.COST_PCT, np.ones_like(R), NAMES, SPEC)
    assert got["mean"] > 0.2 and got["p"] < 0.05, "the selection is positive and significant on its own"
    assert got["ctrl_mean"] > got["mean"] and not got["accept"], (
        "but random entries under the same exit did better, so it is the market and not the set-up")


def test_the_frontier_lists_win_rates_high_to_low_whatever_the_expectancy():
    import pandas as pd
    n = 400
    R = np.zeros((n, 2))
    R[:, 0] = np.where(np.arange(n) % 5 == 0, -1.0, 0.1)          # wins 80% of the time, loses money
    R[:, 1] = np.where(np.arange(n) % 5 < 2, 2.0, -0.5)           # wins 40% of the time, makes money
    df = pd.DataFrame({"day": [f"2025-01-{1 + i % 28:02d}" for i in range(n)], "cell": "A"})
    fr = E.frontier(df, R, ["hi_win", "hi_edge"], "A", [], R + E.COST_PCT)
    assert [f["policy"] for f in fr] == ["hi_win", "hi_edge"], fr
    assert fr[0]["win"] > fr[1]["win"] and fr[0]["net_r"] < 0 < fr[1]["net_r"]


def test_load_holdout_refuses_a_table_holding_a_train_day():
    from tools.replay import ign_event_table as T
    old = T.HOLDOUT_TABLE
    with tempfile.TemporaryDirectory() as tmp:
        T.HOLDOUT_TABLE = Path(tmp) / "h.jsonl"
        T.HOLDOUT_TABLE.write_text(json.dumps({"symbol": "A", "day": "2026-01-05", "cell": "LIVE"}) + "\n")
        z = {"gross": np.zeros((1, 2)), "risk": np.ones((1, 2)), "bars": np.zeros((1, 2)),
             "reason": np.zeros((1, 2)), "names": np.array(["a", "b"])}
        np.savez(T.HOLDOUT_TABLE.with_suffix(".policies.npz"), **z)
        try:
            E.load_holdout()
        except AssertionError as e:
            assert "train day" in str(e)
        else:
            raise AssertionError("a train day in the holdout table must be refused")
        finally:
            T.HOLDOUT_TABLE = old


TESTS = [
    ("the holdout decision needs every condition", test_the_holdout_decision_needs_every_condition),
    ("select freezes a planted edge", test_select_freezes_a_planted_edge),
    ("select refuses to freeze on noise most of the time", test_select_refuses_to_freeze_on_noise_most_of_the_time),
    ("a frozen spec is accepted on a fresh planted draw and refused on noise",
     test_a_frozen_spec_is_accepted_on_a_fresh_planted_draw_and_refused_on_noise),
    ("the false-accept rate on noise is near the nominal five percent",
     test_the_false_accept_rate_on_noise_is_near_the_nominal_five_percent),
    ("score_spec uses the frozen threshold, not the new rows' quantiles",
     test_score_spec_uses_the_frozen_threshold_not_the_new_rows_quantiles),
    ("reveal leaves the holdout sealed when the spec has no candidate",
     test_reveal_leaves_the_holdout_sealed_when_the_spec_has_no_candidate),
    ("reveal refuses when preflight refuses", test_reveal_refuses_when_preflight_refuses),
    ("reveal scores once at every cost and writes its guard file",
     test_reveal_scores_once_at_every_cost_and_writes_its_guard_file),
    ("a selection that does not beat random entries is refused",
     test_a_selection_that_does_not_beat_random_entries_is_refused),
    ("the frontier lists win rates high to low whatever the expectancy",
     test_the_frontier_lists_win_rates_high_to_low_whatever_the_expectancy),
    ("load_holdout refuses a table holding a train day", test_load_holdout_refuses_a_table_holding_a_train_day),
]
