"""
Phase 5 of the swing framework evolution blueprint, 26-Aug-2026.

tools/apply_swing_lifecycle.py is the still-missing piece behind
review_swing_engine_lifecycle() (tools/weekly_review.py) — that function
proposes PROMOTE/SHADOW/RETIRE to brain_proposals and stays untouched;
nothing turned an APPROVED verdict into a real strategy_config.lifecycle
change until this. Only ever touches APPROVED rows (never PENDING — that
is not consent) and marks them APPLIED afterward, matching the same
convention every other proposal type uses in
swing.brain.backtester_and_change_manager.apply_proposal().
"""

from __future__ import annotations

from types import SimpleNamespace


class _FakeTable:
    def __init__(self, store: dict, name: str):
        self._store, self._name = store, name
        self._filters: list[tuple[str, object]] = []
        self._patch: dict | None = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def update(self, patch: dict):
        self._patch = patch
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        rows = self._store.get(self._name, [])
        matched = [r for r in rows if self._matches(r)]
        if self._patch is not None:
            for r in matched:
                r.update(self._patch)
        return SimpleNamespace(data=[dict(r) for r in matched])


class FakeSB:
    def __init__(self, brain_proposals: list[dict], strategy_config: list[dict]):
        self._store = {"brain_proposals": [dict(r) for r in brain_proposals],
                       "strategy_config": [dict(r) for r in strategy_config]}

    def table(self, name):
        return _FakeTable(self._store, name)


def _proposal(id_=1, strat="RVS", verdict="RETIRE", status="APPROVED") -> dict:
    return {"id": id_, "proposal_type": "SWING_ENGINE_LIFECYCLE",
            "target_key": strat, "current_value": "ACTIVE",
            "proposed_value": verdict, "status": status}


def test_applies_an_approved_retire_verdict():
    from tools import apply_swing_lifecycle as asl
    sb = FakeSB([_proposal()], [{"strategy": "RVS", "lifecycle": "ACTIVE"}])
    from tools import apply_swing_lifecycle as _asl_mod
    orig = _asl_mod.get_supabase
    _asl_mod.get_supabase = lambda: sb
    try:
        applied = asl.apply_approved()
    finally:
        _asl_mod.get_supabase = orig

    assert len(applied) == 1
    assert applied[0] == {"strategy": "RVS", "from": "ACTIVE", "to": "RETIRED",
                          "proposal_id": 1}
    assert sb._store["strategy_config"][0]["lifecycle"] == "RETIRED"
    assert sb._store["brain_proposals"][0]["status"] == "APPLIED"
    assert sb._store["brain_proposals"][0]["rollback_value"] == "ACTIVE"


def test_promote_maps_to_active_and_shadow_maps_to_shadow():
    from tools import apply_swing_lifecycle as asl
    sb = FakeSB(
        [_proposal(1, "TPO", "PROMOTE"), _proposal(2, "SBS", "SHADOW")],
        [{"strategy": "TPO", "lifecycle": "SHADOW"},
         {"strategy": "SBS", "lifecycle": "ACTIVE"}])
    from tools import apply_swing_lifecycle as _asl_mod
    orig = _asl_mod.get_supabase
    _asl_mod.get_supabase = lambda: sb
    try:
        applied = asl.apply_approved()
    finally:
        _asl_mod.get_supabase = orig

    by_strat = {a["strategy"]: a["to"] for a in applied}
    assert by_strat["TPO"] == "ACTIVE"
    assert by_strat["SBS"] == "SHADOW"


def test_pending_proposals_are_never_touched():
    """PENDING is not consent — only an operator-APPROVED row may ever be
    applied."""
    from tools import apply_swing_lifecycle as asl
    sb = FakeSB([_proposal(status="PENDING")], [{"strategy": "RVS", "lifecycle": "ACTIVE"}])
    from tools import apply_swing_lifecycle as _asl_mod
    orig = _asl_mod.get_supabase
    _asl_mod.get_supabase = lambda: sb
    try:
        applied = asl.apply_approved()
    finally:
        _asl_mod.get_supabase = orig

    assert applied == []
    assert sb._store["strategy_config"][0]["lifecycle"] == "ACTIVE"
    assert sb._store["brain_proposals"][0]["status"] == "PENDING"


def test_dry_run_changes_nothing():
    from tools import apply_swing_lifecycle as asl
    sb = FakeSB([_proposal()], [{"strategy": "RVS", "lifecycle": "ACTIVE"}])
    from tools import apply_swing_lifecycle as _asl_mod
    orig = _asl_mod.get_supabase
    _asl_mod.get_supabase = lambda: sb
    try:
        applied = asl.apply_approved(dry_run=True)
    finally:
        _asl_mod.get_supabase = orig

    assert len(applied) == 1   # reports what WOULD happen
    assert sb._store["strategy_config"][0]["lifecycle"] == "ACTIVE"   # but changes nothing
    assert sb._store["brain_proposals"][0]["status"] == "APPROVED"


def test_missing_strategy_config_row_is_refused_not_guessed():
    from tools import apply_swing_lifecycle as asl
    sb = FakeSB([_proposal(strat="NOSUCH")], [])
    from tools import apply_swing_lifecycle as _asl_mod
    orig = _asl_mod.get_supabase
    _asl_mod.get_supabase = lambda: sb
    try:
        applied = asl.apply_approved()
    finally:
        _asl_mod.get_supabase = orig

    assert applied == []
    assert sb._store["brain_proposals"][0]["status"] == "APPROVED"   # left alone, not silently marked applied


def test_swing_engine_lifecycle_is_review_only_in_the_generic_dispatcher():
    """The root-cause fix: SWING_ENGINE_LIFECYCLE must never reach
    apply_proposal()'s generic system_config upsert, which would silently
    write a strategy name into system_config as a bogus key."""
    from swing.brain.backtester_and_change_manager import REVIEW_ONLY
    assert "SWING_ENGINE_LIFECYCLE" in REVIEW_ONLY


TESTS = [
    ("applies an approved RETIRE verdict", test_applies_an_approved_retire_verdict),
    ("PROMOTE maps to ACTIVE, SHADOW maps to SHADOW",
     test_promote_maps_to_active_and_shadow_maps_to_shadow),
    ("PENDING proposals are never touched", test_pending_proposals_are_never_touched),
    ("dry run changes nothing", test_dry_run_changes_nothing),
    ("missing strategy_config row is refused, not guessed",
     test_missing_strategy_config_row_is_refused_not_guessed),
    ("SWING_ENGINE_LIFECYCLE is REVIEW_ONLY in the generic dispatcher",
     test_swing_engine_lifecycle_is_review_only_in_the_generic_dispatcher),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name} — {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
