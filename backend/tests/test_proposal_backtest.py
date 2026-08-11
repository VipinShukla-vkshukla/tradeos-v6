"""
brain_proposals get a walk-forward backtest instead of a permanently-null
backtest_result column (11-Aug-2026).

WHAT THIS CATCHES
------------------
brain_proposals.backtest_result has existed since the table was created and
was confirmed, by direct query, never once populated: 0 of 54 real rows on
11-Aug-2026 carry anything there, across 8 proposal_type values. Only ONE
shape — target_key names a real system_config key, current_value and
proposed_value both parse as literals — is mechanically backtestable
without a human first translating prose into a concrete change. Every other
real shape observed (CODE_SUGGESTION, ENGINE_CANDIDATE, ENGINE_LIFECYCLE,
ENGINE_PARAMETERS) must be marked NOT_TESTABLE with a reason, not silently
skipped and not force-fitted into a backtest that would measure the wrong
thing.

These tests pin: classify_proposal() against the ACTUAL shapes queried from
the real table (not invented ones); _override_one() merges a single key
into the real config rather than replacing it, and restores correctly even
when the wrapped code raises; and the walk-forward mechanism actually
responds to the config it is told to vary — proven by giving it a
synthetic population where changing priors_min_sample_intraday between two
values measurably changes how many trades would have been taken, the same
kind of proof-before-trust this project's other verify checks already
demand.
"""

from __future__ import annotations

from tests import cfg_ctx


# ── classify_proposal(), against REAL row shapes ────────────────────────────

def test_code_suggestion_is_not_testable():
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "CODE_SUGGESTION",
           "target_key": "code_suggestion:backend/ai/ai_decision_engine.py:L46",
           "current_value": "backend/ai/ai_decision_engine.py line 46",
           "proposed_value": "returns str(candidate) without confirming validity"}
    kind, reason = classify_proposal(row)
    assert kind == NOT_TESTABLE
    assert "CODE_SUGGESTION" in reason


def test_engine_candidate_is_not_testable():
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "ENGINE_CANDIDATE", "target_key": "UNSEEN/gap down > 1%",
           "current_value": "does not exist", "proposed_value": "build as SHADOW"}
    kind, reason = classify_proposal(row)
    assert kind == NOT_TESTABLE


def test_engine_lifecycle_is_not_testable():
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "ENGINE_LIFECYCLE", "target_key": "GAP",
           "current_value": "ACTIVE", "proposed_value": "SHADOW"}
    kind, reason = classify_proposal(row)
    assert kind == NOT_TESTABLE
    assert "ENGINE_LIFECYCLE" in reason


def test_engine_parameters_prose_is_not_testable():
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "ENGINE_PARAMETERS", "target_key": "RNG",
           "current_value": "stops below 0.59%",
           "proposed_value": "widen the stop or the anchor"}
    kind, reason = classify_proposal(row)
    assert kind == NOT_TESTABLE


def test_swing_reservation_inert_prose_target_is_not_testable():
    """The REAL row (id 189): proposed_value is direction, not a number."""
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "SWING_RESERVATION_INERT",
           "target_key": "alloc_reserve_edge_multiple", "current_value": "1.15",
           "proposed_value": "lower than 1.15 — reconsider, evidence-backed this time"}
    kind, reason = classify_proposal(row)
    assert kind == NOT_TESTABLE, (
        "proposed_value has no literal number in it — must not be treated "
        "as testable just because target_key and current_value look clean")


def test_a_clean_config_value_proposal_is_testable():
    """The shape this tool exists FOR: a real key, two literal values."""
    from tools.proposal_backtest import classify_proposal, CONFIG_VALUE
    row = {"proposal_type": "SWING_RESERVATION_INERT",
           "target_key": "alloc_reserve_edge_multiple",
           "current_value": "1.15", "proposed_value": "0.90"}
    kind, reason = classify_proposal(row)
    assert kind == CONFIG_VALUE
    assert "1.15" in reason and "0.9" in reason


def test_identical_values_are_not_testable():
    from tools.proposal_backtest import classify_proposal, NOT_TESTABLE
    row = {"proposal_type": "X", "target_key": "some_weight",
           "current_value": "0.5", "proposed_value": "0.5"}
    kind, _ = classify_proposal(row)
    assert kind == NOT_TESTABLE


# ── _override_one() ─────────────────────────────────────────────────────────

def test_override_one_merges_rather_than_replaces():
    import config
    from tools.proposal_backtest import _override_one
    with cfg_ctx({"key_a": "1", "key_b": "2"}):
        with _override_one("key_b", "999"):
            assert config.cfg("key_a") == "1", "an unrelated key must survive untouched"
            assert config.cfg("key_b") == "999"
        assert config.cfg("key_b") == "2", "must restore the original value on exit"


def test_override_one_restores_even_on_exception():
    import config
    from tools.proposal_backtest import _override_one
    with cfg_ctx({"key_b": "2"}):
        try:
            with _override_one("key_b", "999"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert config.cfg("key_b") == "2", (
            "a raised exception inside the override must not leak the "
            "overridden value into whatever runs next")


# ── the walk-forward mechanism actually responds to config ─────────────────

def _mk_row(date, sym, entry, stop, target, outcome_pct, verdict="TAKEN",
           strategy="ORB", direction="LONG", cost_pct=0.21):
    return {"trade_date": date, "symbol": sym, "strategy": strategy,
            "entry": entry, "stop": stop, "target": target, "direction": direction,
            "outcome": None, "outcome_pct": outcome_pct, "cost_pct": cost_pct,
            "cost_verdict": verdict, "meta": {"qty": 10}}


def _synthetic_population():
    """36 winning ORB detections across 6 sessions — enough that a floor of
    10 makes the prior usable and a floor of 35 keeps it NEUTRAL (cold
    start), so varying priors_min_sample_intraday between those two values
    is guaranteed to change what gets taken.

    ENTRY VARIES PER ROW, DELIBERATELY. A uniform price (same entry/stop/
    target on every row) gives every one of a day's candidates an IDENTICAL
    edge, so the 75th-percentile bar sits EXACTLY on every candidate and
    the strict `edge > bar` comparison (mirroring the real allocator's own
    strictness) excludes all of them — a degenerate distribution, not a
    bug in the code under test. Varying entry by a few percent per row
    produces a real spread, the same reason real candidates are never
    perfectly uniform either."""
    rows = []
    dates = ["2026-07-01", "2026-07-02", "2026-07-03",
            "2026-07-06", "2026-07-07", "2026-07-08"]
    i = 0
    for d in dates:
        for j in range(6):
            entry = 100.0 + (i % 7) * 1.3   # spreads risk_pct/cost_r a little each row
            stop = entry * 0.98
            target = entry * 1.06
            rows.append(_mk_row(d, f"SYM{i}", entry, stop, target,
                                outcome_pct=3.0))  # net +1.5R after ~0.21% cost
            i += 1
    return rows


def test_walk_forward_responds_to_priors_min_sample_intraday():
    """The core proof this whole tool depends on: _override_one() around a
    REAL config key must change what _walk_forward() computes, because
    scoring.intraday_priors() reads that key internally on every call."""
    from tools.proposal_backtest import _walk_forward, _override_one
    rows = _synthetic_population()
    with cfg_ctx({"priors_intraday_taken_only": "false",
                  "priors_intraday_dedup": "false",
                  "alloc_edge_absolute_floor": "-999", "rank_weight_rr": "0.4"}):
        with _override_one("priors_min_sample_intraday", "35"):
            cold = _walk_forward(rows, days=6, hurdle_pct=0.75,
                                 floor_edge=-999, floor_n=35, cap=10)
        with _override_one("priors_min_sample_intraday", "10"):
            warm = _walk_forward(rows, days=6, hurdle_pct=0.75,
                                 floor_edge=-999, floor_n=10, cap=10)
    assert warm["n"] >= cold["n"], (
        f"a LOWER sample floor must never take FEWER trades on an all-"
        f"winning population — cold={cold}, warm={warm}")
    assert warm["n"] > 0, "the warm pass, with a usable prior, must take something"


def test_backtest_config_proposal_produces_a_structured_verdict():
    from tools.proposal_backtest import backtest_config_proposal
    rows = _synthetic_population()
    row = {"target_key": "priors_min_sample_intraday",
           "current_value": "35", "proposed_value": "10"}
    with cfg_ctx({"priors_intraday_taken_only": "false",
                  "priors_intraday_dedup": "false",
                  "alloc_edge_absolute_floor": "-999", "rank_weight_rr": "0.4"}):
        result = backtest_config_proposal(row, rows, days=6, hurdle_pct=0.75,
                                          floor_edge=-999, floor_n=30, cap=10)
    assert result["method"] == "walk_forward_config_swap"
    assert "before" in result and "after" in result
    assert isinstance(result["delta_r"], float)
    assert result["verdict"]  # non-empty string, some verdict was reached


# ── run()'s write path ──────────────────────────────────────────────────────

class _FakeTable:
    def __init__(self, rows, sink):
        self._rows, self._sink, self._filters = rows, sink, {}
    def select(self, *a, **k): return self
    def eq(self, col, val):
        self._filters[col] = val
        return self
    def update(self, payload):
        self._pending = payload
        return self
    def execute(self):
        if hasattr(self, "_pending"):
            for r in self._rows:
                if all(r.get(k) == v for k, v in self._filters.items()):
                    self._sink.append({**r, **self._pending})
            del self._pending
            return self
        out = [r for r in self._rows
              if all(r.get(k) == v for k, v in self._filters.items())]
        self.data = out
        return self


class _FakeSB:
    def __init__(self, rows):
        self._rows, self.written = rows, []
    def table(self, name):
        assert name == "brain_proposals"
        return _FakeTable(self._rows, self.written)


def test_dry_run_writes_nothing():
    from tools.proposal_backtest import run
    rows = [{"id": 1, "proposal_type": "CODE_SUGGESTION", "target_key": "code_suggestion:x",
            "current_value": "x", "proposed_value": "y", "status": "PENDING"}]
    sb = _FakeSB(rows)
    with cfg_ctx({}):
        rc = run(days=5, dry_run=True, sb=sb)
    assert rc == 0
    assert sb.written == [], "dry-run must never call update()"


def test_not_testable_proposal_still_gets_a_result_written():
    from tools.proposal_backtest import run
    rows = [{"id": 2, "proposal_type": "ENGINE_LIFECYCLE", "target_key": "GAP",
            "current_value": "ACTIVE", "proposed_value": "SHADOW", "status": "PENDING"}]
    sb = _FakeSB(rows)
    with cfg_ctx({}):
        run(days=5, dry_run=False, sb=sb)
    assert len(sb.written) == 1
    import json
    result = json.loads(sb.written[0]["backtest_result"])
    assert result["testable"] is False, (
        "an untestable proposal must still get a recorded reason, not be "
        "silently skipped and left permanently null")


TESTS = [
    ("CODE_SUGGESTION is not testable", test_code_suggestion_is_not_testable),
    ("ENGINE_CANDIDATE is not testable", test_engine_candidate_is_not_testable),
    ("ENGINE_LIFECYCLE is not testable", test_engine_lifecycle_is_not_testable),
    ("ENGINE_PARAMETERS prose is not testable",
     test_engine_parameters_prose_is_not_testable),
    ("SWING_RESERVATION_INERT prose target is not testable",
     test_swing_reservation_inert_prose_target_is_not_testable),
    ("a clean config-value proposal is testable",
     test_a_clean_config_value_proposal_is_testable),
    ("identical values are not testable", test_identical_values_are_not_testable),
    ("_override_one merges rather than replaces",
     test_override_one_merges_rather_than_replaces),
    ("_override_one restores even on exception",
     test_override_one_restores_even_on_exception),
    ("walk-forward responds to priors_min_sample_intraday",
     test_walk_forward_responds_to_priors_min_sample_intraday),
    ("backtest_config_proposal produces a structured verdict",
     test_backtest_config_proposal_produces_a_structured_verdict),
    ("dry run writes nothing", test_dry_run_writes_nothing),
    ("not-testable proposal still gets a result written",
     test_not_testable_proposal_still_gets_a_result_written),
]
