"""
`IntradayEngine._score_proposals()` extraction — 08-Sep-2026, built to let
IGN's fast-entry path (event_core.py) get a verdict for ONE candidate on the
2-second loop without touching `self._verdicts`, the shared, cycle-scoped
dict `allocator_permits()`, the swing alert-kind logic and the pick-label
logic all read as the ORDINARY 15s cycle's full-candidate view.

WHY SOURCE INSPECTION, NOT A RUNTIME CALL — same reason as
test_today_totals_cache.py::test_cycle_clears_the_cache_at_its_own_top and
test_pending_fill_race.py's own docstring: `_score_proposals()` constructs a
real `Allocator` and calls `refresh_priors()`/`refresh_priority_criteria()`/
`refresh_hurdle_populations()` on first use — DB-shaped calls no lightweight
fake `sb` can stand in for without reimplementing half the allocator's own
plumbing. The actual behavioral correctness of this extraction was verified
live this session: `tools.verify` (1280/142, unchanged), then
`tools.simulate` end-to-end against the real book (23 real SWING candidates
flowed through `_allocate_shadow()` -> `_score_proposals()` -> `Allocator.
select()`, zero errors, "Nothing was written" as expected) — see
docs/FINDINGS.md, 08-Sep-2026. What THIS module pins, permanently, is the
STRUCTURAL property that made that verification meaningful and that could
silently regress later: that the extraction is a pure delegation, not a
reimplementation, and that the shared `self._verdicts` dict is assigned in
exactly one place.
"""
from __future__ import annotations

import re
from pathlib import Path

_ENGINE_PATH = (Path(__file__).parent.parent / "intraday" / "engine.py")


def _method_body(src: str, name: str) -> str:
    """Same isolation idiom as test_today_totals_cache.py's own regex —
    `def NAME(` to the next top-level `def ` or end of file."""
    m = re.search(rf"    def {re.escape(name)}\(.*?(?:\n    def |\Z)", src, re.DOTALL)
    assert m, f"could not isolate {name}()'s body — has it been renamed or moved?"
    return m.group(0)


def test_allocate_shadow_delegates_scoring_to_score_proposals():
    """The whole point of the extraction: _allocate_shadow() must not
    reimplement or duplicate the slots/minutes_left/field/select() logic —
    it must call the new method for it."""
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    body = _method_body(src, "_allocate_shadow")
    assert "self._score_proposals(props)" in body, (
        "_allocate_shadow() no longer delegates to _score_proposals() — "
        "the scoring logic may have been re-inlined or duplicated")
    # The heavy per-cycle computation must live in _score_proposals(), not
    # here — if any of these reappear inline in _allocate_shadow(), the
    # extraction has been undone (partially or fully) and IGN's fast path
    # would be scoring against duplicated, potentially drifting logic.
    for leaked in ("self._allocator.select(", "swing_policies.p_trigger(",
                   "self._allocator.score_hypothetical("):
        assert leaked not in body, (
            f"{leaked!r} found inside _allocate_shadow() — the scoring "
            f"computation has leaked back out of _score_proposals()")


def test_verdicts_assigned_in_exactly_one_place():
    """self._verdicts is the shared, cycle-scoped dict allocator_permits()
    and the swing alert-kind/pick-label logic all read. _score_proposals()
    must NEVER assign it — only _allocate_shadow() may, exactly once, or a
    fast-path caller scoring one candidate mid-cycle would silently narrow
    the shared view until the next 15s cycle overwrites it."""
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    score_body = _method_body(src, "_score_proposals")
    # Checks for ASSIGNMENT specifically, not mere mention — the method's
    # own docstring explains this exact hazard in prose, which a plain
    # substring check would misfire on.
    assert not re.search(r"self\._verdicts\s*=", score_body), (
        "_score_proposals() assigns self._verdicts — this is exactly the "
        "correctness hazard the extraction exists to avoid (see this "
        "module's own docstring and docs/FINDINGS.md, 08-Sep-2026)")

    shadow_body = _method_body(src, "_allocate_shadow")
    assigns = re.findall(r"self\._verdicts\s*=", shadow_body)
    # Two matches expected: the early "self._verdicts = {}" reset (so a
    # stale verdict cannot survive an early return) and the real assignment
    # after scoring. Both belong to _allocate_shadow(); neither belongs
    # anywhere else in the file.
    assert len(assigns) == 2, (
        f"expected exactly 2 assignments to self._verdicts inside "
        f"_allocate_shadow() (the early reset + the real one), found "
        f"{len(assigns)} — if this changed, re-check the early-return "
        f"reset is still in place")

    other_assigns = len(re.findall(r"self\._verdicts\s*=", src)) - len(assigns)
    assert other_assigns == 0, (
        f"found {other_assigns} assignment(s) to self._verdicts OUTSIDE "
        f"_allocate_shadow() — only that function may own this shared state")


def test_score_proposals_returns_the_verdict_list_only():
    """Pins the return contract IGN's fast-entry path depends on: a plain
    list of verdict dicts, not a tuple, not something requiring the caller
    to know about `slots` or any other internal — so event_core.py's own
    call site (`v = engine._score_proposals([proposal])`) stays this simple."""
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    body = _method_body(src, "_score_proposals")
    assert "-> list[dict]" in body.split("\n", 1)[0] or \
           re.search(r"def _score_proposals\([^)]*\)\s*->\s*list\[dict\]", body), (
        "_score_proposals()'s return type annotation changed from list[dict] "
        "— check event_core.py's own call site still unpacks it correctly")
    assert re.search(r"return self\._allocator\.select\(", body), (
        "_score_proposals() no longer ends by returning Allocator.select()'s "
        "own result directly — if it now post-processes or wraps it, "
        "_allocate_shadow()'s own takes/self._verdicts computation (which "
        "assumes a plain verdict-dict list) needs re-checking too")


def test_minutes_left_defaults_exactly_as_before_when_not_overridden():
    """The ordinary 15s call site (_allocate_shadow -> _score_proposals(props),
    no override) must compute minutes_left exactly as the pre-extraction
    code did — max(st.minutes_to_squareoff or 0, 0) — so today's hurdle()
    time-term behaviour for both books is unchanged. A fast-path caller may
    override it; the ordinary one must not need to."""
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    body = _method_body(src, "_score_proposals")
    assert "minutes_left: int | None = None" in _method_body(src, "_score_proposals").split("\n")[0] or \
           re.search(r"minutes_left:\s*int \| None\s*=\s*None", body.split("\n", 1)[0]), (
        "_score_proposals()'s minutes_left parameter no longer defaults to "
        "None — the ordinary call site relies on this default to reproduce "
        "today's exact minutes_to_squareoff computation")
    assert "if minutes_left is None:" in body, (
        "_score_proposals() no longer guards minutes_left with 'if is None' "
        "— check it still falls back to max(st.minutes_to_squareoff or 0, 0)")
    assert "max(st.minutes_to_squareoff or 0, 0)" in body, (
        "the exact pre-extraction minutes_to_squareoff computation is "
        "missing from _score_proposals() — this is the F-88-adjacent fix "
        "this project already paid to get right once")


TESTS = [
    ("_allocate_shadow delegates scoring to _score_proposals, no duplication",
     test_allocate_shadow_delegates_scoring_to_score_proposals),
    ("self._verdicts is assigned in exactly one place",
     test_verdicts_assigned_in_exactly_one_place),
    ("_score_proposals returns the plain verdict list, nothing more",
     test_score_proposals_returns_the_verdict_list_only),
    ("minutes_left defaults exactly as before when not overridden",
     test_minutes_left_defaults_exactly_as_before_when_not_overridden),
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
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
