"""
Operator-caught gap, 31-Aug-2026: "if I am already holding 10 positions,
there is no way I will fill 10 NEW positions unless I sell the existing
ones — so how is the swap going to work?"

`act_on_candidates()`'s swap comparison (`_replacement_case()`) only ever
ran when `swing_max_new_per_day` (a DAILY PACING counter — how many new
entries have been submitted today) was exhausted. It never looked at
`max_positions` (portfolio_constraints.py's regime-scaled SLOT limit — how
many positions may be held at once), which `decide()` already checks via
`check_new_entry()` and which is a completely independent number.

Concretely: 10 positions held, `max_positions_risk_off=6` (live default),
`swing_max_new_per_day=10` — a candidate arrives, `decide()` refuses it at
the slot-limit check (constraint #1 in `check_new_entry()`, checked BEFORE
the daily-pace question is even relevant) and returns SKIP. Because the
action isn't BUY_NOW/CHASE_LIMIT, `evaluate_candidates()` dropped it
entirely — it never reached `act_on_candidates()`, so `_replacement_case()`
never ran and no alert fired. The exact scenario the swap alert exists for
(book full, weigh a new plan against the weakest thing held) was the one
scenario it could never see.

Fix: `Decision` gained `block_reason` (trade_decision.py) — the raw
`ConstraintVerdict.reason` code, set only when check_new_entry() refused
room, `None` for every other SKIP (stop broken, target hit, R:R too low,
missing data). `evaluate_candidates()` forwards a `block_reason ==
"max_positions"` SKIP as state SLOT_FULL instead of dropping it.
`act_on_candidates()` treats SLOT_FULL the same as daily-pace exhaustion:
no direct buy, but eligible for the replacement-case comparison.
`_maybe_enter_swing()` gained an explicit guard — SLOT_FULL candidates
carry qty=0 (decide() never sized them) so no order can be placed, but the
guard also closes a separate, pre-existing gap this investigation
surfaced: nothing there previously checked `d.action` at all, so an
APPROACHING (WAIT) candidate ranked outside the alert's top-N contenders
— reached via `if not place: self._maybe_enter_swing(c, d, ltp)` — could
carry a real, nonzero `qty` (decide() sizes BEFORE deciding BUY_NOW vs
WAIT vs SKIP) and fall through every gate to order placement, for a plan
whose live R:R had already been judged NOT good enough at the current
price. Scoped deliberately to `max_positions` only — sector/industry/
risk-budget refusals are a separate question, not addressed here.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from tests import cfg_ctx

_ENGINE_PATH = (Path(__file__).parent.parent / "intraday" / "engine.py")


def _engine():
    from intraday.engine import IntradayEngine

    class _EmptyDB:
        def table(self, *a, **k):
            return self
        def select(self, *a, **k):
            return self
        def execute(self):
            class R:
                data = []
            return R()

    return IntradayEngine(sb=_EmptyDB())


# ── decide() sets block_reason precisely, and only, on a room refusal ──────

def test_decide_sets_block_reason_max_positions_when_slots_are_full():
    from analysis.trade_decision import decide, ACT_SKIP
    row = {"symbol": "TESTCO", "planned_stop": 90.0, "planned_target": 130.0,
           "entry_zone_low": 98.0, "current_price": 100.0}
    # 3 SWING positions already held, cap set to 3 — the exact "held ==
    # cap, more entries theoretically allowed today" shape the operator
    # described, just at a smaller number so the fixture stays readable.
    held = [{"symbol": f"HELD{i}", "framework": "SWING", "sector": "x",
              "industry": "y", "invested_value": 1000.0}
            for i in range(3)]
    with cfg_ctx({"max_positions_neutral": 3, "swing_max_new_per_day": 10}):
        d = decide(row, 100.0, total_capital=300000.0,
                    open_positions=held, regime="NEUTRAL", min_rr=1.0)
    assert d.action == ACT_SKIP, f"expected SKIP with the book full, got {d.action}"
    assert d.block_reason == "max_positions", (
        f"expected block_reason='max_positions', got {d.block_reason!r} — "
        f"headline was {d.headline!r}")
    assert d.qty in (0, None), "a slot-blocked decision must not carry a size"


def test_decide_leaves_block_reason_none_on_an_ordinary_price_skip():
    """Scoping check: a plan refused for a REAL reason (stop already
    broken, nothing to do with account room) must not be mistaken for a
    slot-limit refusal — block_reason stays None. This branch returns
    before check_new_entry() is ever called, so it is the cleanest proof
    the field does not leak into unrelated SKIPs."""
    from analysis.trade_decision import decide, ACT_SKIP
    row = {"symbol": "TESTCO", "planned_stop": 100.5, "planned_target": 130.0,
           "entry_zone_low": 99.5, "current_price": 100.0}
    with cfg_ctx({"max_positions_neutral": 15, "swing_max_new_per_day": 10}):
        d = decide(row, 100.0, total_capital=300000.0,
                    open_positions=[], regime="NEUTRAL", min_rr=1.0)
    assert d.action == ACT_SKIP, f"price at/below stop must SKIP, got {d.action}"
    assert d.block_reason is None, (
        f"a broken-stop refusal is not a room refusal — block_reason must "
        f"stay None, got {d.block_reason!r}")


# ── _replacement_case(): unchanged logic, exercised directly ───────────────

def test_replacement_case_true_when_candidate_clears_the_margin():
    eng = _engine()
    eng.positions = [{"symbol": "WEAK", "framework": "SWING",
                       "entry_rationale": "rank 40 — thin setup"}]
    with cfg_ctx({"swing_replace_margin": 20.0}):
        ok, why = eng._replacement_case(SimpleNamespace(total=65.0))
    assert ok is True, f"65 vs 40+20=60 should clear the margin, got why={why!r}"
    assert "WEAK" in why and "40" in why


def test_replacement_case_false_when_candidate_does_not_clear_the_margin():
    eng = _engine()
    eng.positions = [{"symbol": "WEAK", "framework": "SWING",
                       "entry_rationale": "rank 40 — thin setup"}]
    with cfg_ctx({"swing_replace_margin": 20.0}):
        ok, why = eng._replacement_case(SimpleNamespace(total=55.0))
    assert ok is False, f"55 vs 40+20=60 must not clear the margin, got ok={ok}, why={why!r}"


def test_replacement_case_false_when_nothing_held():
    eng = _engine()
    eng.positions = []
    ok, why = eng._replacement_case(SimpleNamespace(total=999.0))
    assert ok is False and why == ""


# ── _maybe_enter_swing(): the action-type guard fires first, safely ────────

def test_maybe_enter_swing_returns_immediately_for_a_non_buyable_decision():
    """SLOT_FULL and APPROACHING/WAIT decisions both reach this function
    (the first via the new branch, the second via the pre-existing
    `if not place:` call site for candidates outside the alert's top-N).
    Neither may proceed past the very first line — this call must not
    raise (proving no config/Kite/DB access happens before the check) and
    must not need cfg_ctx to stay safe."""
    eng = _engine()
    for action in ("WAIT", "SKIP", "NO_DATA"):
        d = SimpleNamespace(action=action, qty=500, stop=90.0, target=130.0,
                             rr_live=0.4, block_reason=None)
        result = eng._maybe_enter_swing({"symbol": "TESTCO"}, d, 100.0)
        assert result is None, f"action={action} must return immediately"


def _function_body(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    m = re.search(rf"def {name}\(.*?\n    def ", src, re.DOTALL)
    assert m, f"could not isolate {name}() in {path.name}"
    return m.group(0)


def test_maybe_enter_swing_guard_precedes_every_other_gate():
    """Pin the guard's POSITION, not just its presence — it must be the
    first executable statement, before swing_auto_entry or any other rail,
    so a future edit cannot silently reintroduce the WAIT-reaches-order-
    placement gap by inserting logic above it."""
    body = _function_body(_ENGINE_PATH, "_maybe_enter_swing")
    guard_pos = body.find('if d.action not in ("BUY_NOW", "CHASE_LIMIT")')
    auto_entry_pos = body.find('cfg_bool("swing_auto_entry"')
    assert guard_pos != -1, "_maybe_enter_swing lost its action-type guard"
    assert auto_entry_pos != -1, "swing_auto_entry check itself went missing"
    assert guard_pos < auto_entry_pos, (
        "the action-type guard must run BEFORE swing_auto_entry (and every "
        "other rail) — found it after instead")


# ── evaluate_candidates() / act_on_candidates(): source-inspection guards ──
#
# Neither is callable in a unit test without a live Kite session and a real
# Supabase margins/positions read (evaluate_candidates) or a working
# notifier + allocator (act_on_candidates) — same rationale
# test_stage_e5_live_rr_ranking.py already established for
# _maybe_enter_swing itself.

def test_evaluate_candidates_forwards_slot_full_state():
    body = _function_body(_ENGINE_PATH, "evaluate_candidates")
    assert 'd.block_reason == "max_positions"' in body, (
        "evaluate_candidates() no longer checks block_reason — a "
        "slot-blocked candidate may be silently dropped again")
    assert '"state": "SLOT_FULL"' in body


def test_act_on_candidates_treats_slot_full_as_no_room():
    body = _function_body(_ENGINE_PATH, "act_on_candidates")
    assert 'e.get("state") == "SLOT_FULL"' in body, (
        "act_on_candidates() no longer reads the SLOT_FULL state — the "
        "swap comparison may stop running for a slot-blocked candidate")
    assert "not slot_full" in body, (
        "slot_full must fold into the room computation, not sit unused")


TESTS = [
    ("decide() sets block_reason=max_positions when the book is full",
     test_decide_sets_block_reason_max_positions_when_slots_are_full),
    ("decide() leaves block_reason=None on an ordinary price SKIP",
     test_decide_leaves_block_reason_none_on_an_ordinary_price_skip),
    ("_replacement_case() approves a candidate that clears the margin",
     test_replacement_case_true_when_candidate_clears_the_margin),
    ("_replacement_case() refuses a candidate that does not clear the margin",
     test_replacement_case_false_when_candidate_does_not_clear_the_margin),
    ("_replacement_case() is False with nothing held",
     test_replacement_case_false_when_nothing_held),
    ("_maybe_enter_swing() returns immediately for a non-buyable decision",
     test_maybe_enter_swing_returns_immediately_for_a_non_buyable_decision),
    ("_maybe_enter_swing() guard precedes every other gate",
     test_maybe_enter_swing_guard_precedes_every_other_gate),
    ("evaluate_candidates() forwards SLOT_FULL on a max_positions block",
     test_evaluate_candidates_forwards_slot_full_state),
    ("act_on_candidates() treats SLOT_FULL as no room",
     test_act_on_candidates_treats_slot_full_as_no_room),
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
