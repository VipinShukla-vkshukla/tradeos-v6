"""
22-Sep-2026 — /control health showed "Swing positions linked to a signal
0/1" (BLOCK-adjacent WARN): a same-day-discovered SWING paper position
(BHEL, strategy SBS, entry_rationale "screener 50" — the same-day
discovery default, migration 122) had signal_id NULL.

Investigation found the missing link is real but the position itself is
not evidence of a defect: `swing/signals/same_day_discovery.py` writes to
`swing_same_day_candidates`, never `signal_log` — deliberately, per
migration 122's own comment, so a same-day outcome can never land on an
unrelated evening WATCH signal that happens to share the symbol and
poison the evening pipeline's learning loop (the same landmine CLAUDE.md
documents for intraday outcomes vs signal_log). A same-day position
correctly has no signal_id to give.

The REAL bug: `_maybe_enter_swing()`'s PAPER branch (`if not live:`) never
resolved `find_originating_signal()` at all, for ANY candidate — unlike
its two LIVE sibling branches (the resting ladder and the marketable
chase, both a few dozen lines below it) and `control/paper_entry.py`
(the after-close swing paper cron), which all already do this. So an
ordinary EVENING-PIPELINE plan taken as a paper entry through the live
daemon — not just a same-day discovery — also lost attribution, silently,
for every paper swing entry the daemon itself ever opened.

Fix: the PAPER branch now resolves signal_id/signal_date too, but ONLY
for a candidate present in `self.candidates` (the evening pipeline's own
immutable list) — never for a same-day-only candidate, which must stay
unattributed by construction. These tests pin that exact boundary via
source inspection, the same technique test_swing_slot_full_swap.py
already established for this function: `_maybe_enter_swing` needs a live
Kite session and a real Supabase read to execute end-to-end, so its
logic is verified in its own text, not by invocation.
"""

from __future__ import annotations

import re
from pathlib import Path

_ENGINE_PATH = (Path(__file__).parent.parent / "intraday" / "engine.py")


def _paper_branch_body() -> str:
    src = _ENGINE_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"if not live:\n.*?(?=\n        # ── LIVE, RESTING LADDER)",
        src, re.DOTALL)
    assert m, "could not isolate _maybe_enter_swing's PAPER branch (if not live:)"
    return m.group(0)


def test_paper_branch_resolves_attribution_at_all():
    """The gap this whole investigation started from: before the fix, the
    PAPER branch never called find_originating_signal — every paper swing
    entry the live daemon opened had signal_id NULL, evening-sourced or
    not."""
    body = _paper_branch_body()
    assert "find_originating_signal" in body, (
        "PAPER branch of _maybe_enter_swing lost its signal_log lookup — "
        "every paper swing entry will lose attribution again, the exact "
        "gap that produced the 22-Sep-2026 health WARN")


def test_paper_branch_attribution_is_gated_to_evening_candidates_only():
    """The narrower, easier-to-get-wrong half: a same-day-discovered
    candidate must NOT be run through find_originating_signal at all — it
    has no true originating signal_log row, and the 10-day, symbol-only
    lookback in find_originating_signal() could otherwise match an
    unrelated evening WATCH signal for the same symbol, exactly the
    cross-attribution migration 122 was written to make impossible."""
    body = _paper_branch_body()
    guard_pos = body.find("any(ec.get(\"symbol\") == sym for ec in self.candidates)")
    lookup_pos = body.find("find_originating_signal")
    open_pos = body.find("paper_broker.open_position(")
    assert guard_pos != -1, (
        "PAPER branch resolves signal_id unconditionally — a same-day-only "
        "candidate (no signal_log row by design) would be run through "
        "find_originating_signal() and risk attribution to an unrelated "
        "evening signal for the same symbol")
    assert guard_pos < lookup_pos < open_pos, (
        "the evening-candidate guard must wrap the signal lookup, and both "
        "must run before the position is written — found them out of order")


def test_paper_branch_never_invents_a_default_signal_id():
    """setup['signal_id'] must only ever be set from a real resolved row
    (or left absent, i.e. None downstream) — never hardcoded or defaulted
    to something that looks like a value."""
    body = _paper_branch_body()
    assert 'setup["signal_id"] = _s.get("id")' in body, (
        "signal_id must come from the resolved signal_log row's own id, "
        "not a constant or a guess")


TESTS = [
    ("PAPER branch of _maybe_enter_swing resolves attribution at all",
     test_paper_branch_resolves_attribution_at_all),
    ("PAPER branch attribution is gated to evening-pipeline candidates only",
     test_paper_branch_attribution_is_gated_to_evening_candidates_only),
    ("PAPER branch never invents a default signal_id",
     test_paper_branch_never_invents_a_default_signal_id),
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
