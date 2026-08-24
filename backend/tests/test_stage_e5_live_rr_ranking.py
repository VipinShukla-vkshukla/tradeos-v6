"""
Track E, Stage E5 (docs/TRADEOS_ROADMAP.md) — score_plan() ranks the day's
scarce entry slots partly on implied_rr, and its own comment claims that
value is "the live figure... a plan that has already run is a worse trade
than it was when written, and only implied_rr knows that." It was not:
`implied_rr` is written ONLY by the evening pipeline (final_snapshot.py /
generate_signals.py) and nothing between then and either live-ranking call
site ever refreshed it. `analysis.trade_decision.decide()` already computes
the real thing (`rr_live`) and it went unused at BOTH places that rank
candidates for entry — `intraday/engine.py::_maybe_enter_swing` and
`tools/simulate.py::simulate_swing_entries`.

Factored into one shared, pure function — `entry_ranking.
live_ranking_input()` — once both call sites had independently grown the
identical override, rather than after a third copy drifts from the other
two; the exact shape `tools/simulate.py`'s incomplete exit-policy dict
(F-71 §3) already cost this session once.

Real-data anchor, HAL, 21-Aug-2026 (docs/FINDINGS.md, Stage E5 section):
zone_low drifted 4779 -> 4808 across three prior signal snapshots while
stop (4740.22) and target (5325.17) stayed fixed. rr_at_zone_low ranged
7.63-14.09 depending which snapshot you read; rr_live at the actual fill
(5010.20) was 1.17.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import cfg_ctx

_ENGINE_PATH = (Path(__file__).parent.parent / "intraday" / "engine.py")
_SIMULATE_PATH = (Path(__file__).parent.parent / "tools" / "simulate.py")


def test_live_ranking_input_overrides_implied_rr_when_present():
    from analysis.entry_ranking import live_ranking_input
    p = {"symbol": "HAL", "implied_rr": 14.09, "final_score": 60.0}
    out = live_ranking_input(p, 1.17)
    assert out["implied_rr"] == 1.17, (
        f"expected the live figure (1.17) to override the stale zone-low "
        f"one (14.09), got {out['implied_rr']}")
    assert out["final_score"] == 60.0, "must not touch unrelated fields"
    assert p["implied_rr"] == 14.09, "must not mutate the input dict"


def test_live_ranking_input_leaves_plan_untouched_when_rr_live_is_none():
    """A plan can legitimately have no live figure — the stale pipeline
    value is a better fallback than a fabricated zero."""
    from analysis.entry_ranking import live_ranking_input
    p = {"symbol": "HAL", "implied_rr": 14.09}
    out = live_ranking_input(p, None)
    assert out["implied_rr"] == 14.09, (
        f"rr_live=None must leave implied_rr untouched, got "
        f"{out['implied_rr']}")
    assert out is p or out == p, "should be a no-op, not a mutated copy"


def test_score_plan_ranks_hals_live_rr_below_its_stale_zone_low_rr():
    """End-to-end through score_plan(): the live-overridden plan must
    rank materially lower than the same plan on its stale zone-low rr —
    that gap is exactly what a chased entry should cost."""
    from analysis.entry_ranking import score_plan, live_ranking_input
    base = {"symbol": "HAL", "final_score": 60.0, "implied_rr": 14.09}
    with cfg_ctx({}):
        stale = score_plan(base)
        live = score_plan(live_ranking_input(base, 1.17))
    assert live.total < stale.total - 5.0, (
        f"live rr=1.17 (total {live.total:.1f}) must rank materially "
        f"below stale zone-low rr=14.09 (total {stale.total:.1f}) — got "
        f"a gap of only {stale.total - live.total:.1f}")


def _uses_live_ranking_input(path: Path, fn_name_pattern: str) -> bool:
    src = path.read_text(encoding="utf-8")
    m = re.search(fn_name_pattern, src, re.DOTALL)
    assert m, f"could not isolate the function body in {path.name}"
    return "live_ranking_input(" in m.group(0)


def test_maybe_enter_swing_calls_the_shared_override():
    """Source-inspection regression guard: _maybe_enter_swing cannot be
    called directly in a unit test (needs a live Kite session, order
    placement, the allocator — same reason no other method in this class
    has a direct test; see test_pending_fill_race.py's own docstring).
    The same class of gap check_shorts() already greps call sites for,
    because a return-value test cannot see which function a call site
    used."""
    assert _uses_live_ranking_input(
        _ENGINE_PATH, r"def _maybe_enter_swing\(.*?\n    def "), (
        "_maybe_enter_swing no longer calls live_ranking_input — the "
        "live-rr override may have reverted to stale implied_rr")


def test_simulate_swing_entries_calls_the_shared_override():
    assert _uses_live_ranking_input(
        _SIMULATE_PATH, r"def simulate_swing_entries\(.*?\ndef "), (
        "simulate_swing_entries no longer calls live_ranking_input — the "
        "live-rr override may have reverted to stale implied_rr")


TESTS = [
    ("live_ranking_input overrides implied_rr when rr_live is present",
     test_live_ranking_input_overrides_implied_rr_when_present),
    ("live_ranking_input is a no-op when rr_live is None",
     test_live_ranking_input_leaves_plan_untouched_when_rr_live_is_none),
    ("score_plan ranks HAL's live rr below its stale zone-low rr",
     test_score_plan_ranks_hals_live_rr_below_its_stale_zone_low_rr),
    ("_maybe_enter_swing calls the shared override",
     test_maybe_enter_swing_calls_the_shared_override),
    ("simulate_swing_entries calls the shared override",
     test_simulate_swing_entries_calls_the_shared_override),
]
