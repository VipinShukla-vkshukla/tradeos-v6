"""
ai_tier and ai_conviction removed from the evening AI's output, 29-Aug-2026
— not just demoted to weight 0 (04-Aug), actually stopped being written.
Measured against real resolved outcomes: TIER_1/HIGH-conviction picks
UNDERPERFORMED TIER_3/LOW-conviction ones (E[R] -0.18 vs +0.37, n=47/99;
avg% -1.35 vs +3.28, n=30/39), stable across two separate two-week
periods — the self-rated judgment was inversely predictive, not merely
unvalidated. Chase allowance is now a fixed constant
(SWING_CHASE_PCT_FLAT), not an AI-proposed-then-clamped number — chase
>2% turned net negative on real resolved outcomes (n=17-24/band).

See docs/FINDINGS.md for the full quantify pass; this module proves the
write path actually reflects it.
"""

from __future__ import annotations

from ai.ai_decision_engine import write_signal_enrichment, SWING_CHASE_PCT_FLAT


class _FakeTable:
    def __init__(self, store: dict):
        self.store = store
    def update(self, values):
        self._pending = values
        return self
    def eq(self, *a, **k):
        return self
    def execute(self):
        self.store["last_update"] = self._pending
        return self


class _FakeSb:
    def __init__(self):
        self.updates: list[dict] = []
    def table(self, name):
        store = {}
        t = _FakeTable(store)
        real_execute = t.execute
        def execute():
            r = real_execute()
            self.updates.append({"table": name, **store["last_update"]})
            return r
        t.execute = execute
        return t


def _candidate(symbol="TCS", entry_zone_high=3850.0, entry_id=1):
    return {"symbol": symbol, "id": entry_id, "entry_zone_high": entry_zone_high}


def test_ai_conviction_and_confidence_never_written():
    sb = _FakeSb()
    result = {"ranked_candidates": [
        {"symbol": "TCS", "action": "ENTER_NOW", "thesis": "strong breakout"},
    ]}
    write_signal_enrichment(sb, result, [_candidate()], "2026-08-29", "deepseek")
    signal_log_writes = [u for u in sb.updates if u["table"] == "signal_log"]
    assert signal_log_writes, "expected a signal_log write"
    row = signal_log_writes[0]
    assert "ai_conviction" not in row, f"ai_conviction was written: {row}"
    assert "ai_confidence" not in row, f"ai_confidence was written: {row}"


def test_conviction_reason_carries_no_tier_prefix():
    """The old format prefixed conviction_reason with '[TIER_1] ...' —
    confirm that's gone, not just that the field still exists."""
    sb = _FakeSb()
    result = {"ranked_candidates": [
        {"symbol": "TCS", "action": "ENTER_NOW", "thesis": "strong breakout"},
    ]}
    write_signal_enrichment(sb, result, [_candidate()], "2026-08-29", "deepseek")
    row = [u for u in sb.updates if u["table"] == "signal_log"][0]
    reason = row["ai_conviction_reason"]
    assert "TIER" not in reason, f"tier label leaked into conviction_reason: {reason!r}"
    assert "strong breakout" in reason


def test_chase_pct_is_fixed_for_entry_actions_regardless_of_ai_input():
    """Even if a (now-unused) max_chase_pct field is present in the AI's
    raw output, it must be ignored — the ceiling is the fixed constant,
    not a clamp on an AI-proposed number."""
    sb = _FakeSb()
    result = {"ranked_candidates": [
        {"symbol": "TCS", "action": "ENTER_NOW", "thesis": "x",
         "max_chase_pct": 8.0},   # legacy-shaped field, must be ignored
    ]}
    write_signal_enrichment(sb, result, [_candidate()], "2026-08-29", "deepseek")
    row = [u for u in sb.updates if u["table"] == "signal_log"][0]
    assert row["ai_max_chase_pct"] == SWING_CHASE_PCT_FLAT, (
        f"expected the fixed {SWING_CHASE_PCT_FLAT}, got {row['ai_max_chase_pct']} "
        f"— an AI-proposed number leaked through")


def test_chase_pct_is_zero_for_non_entry_actions():
    sb = _FakeSb()
    result = {"ranked_candidates": [
        {"symbol": "TCS", "action": "WAIT_FOR_TRIGGER", "thesis": "x"},
    ]}
    write_signal_enrichment(sb, result, [_candidate()], "2026-08-29", "deepseek")
    row = [u for u in sb.updates if u["table"] == "signal_log"][0]
    assert row["ai_max_chase_pct"] == 0.0


def test_master_shortlist_write_also_has_no_conviction_or_rank():
    sb = _FakeSb()
    result = {"ranked_candidates": [
        {"symbol": "TCS", "action": "ENTER_NOW", "thesis": "x"},
    ]}
    write_signal_enrichment(sb, result, [_candidate()], "2026-08-29", "deepseek")
    row = [u for u in sb.updates if u["table"] == "master_shortlist"][0]
    assert "ai_conviction" not in row
    assert "ai_shortlist_rank" not in row


TESTS = [
    ("ai_conviction/ai_confidence never written", test_ai_conviction_and_confidence_never_written),
    ("conviction_reason carries no tier prefix", test_conviction_reason_carries_no_tier_prefix),
    ("chase pct is fixed regardless of AI input", test_chase_pct_is_fixed_for_entry_actions_regardless_of_ai_input),
    ("chase pct is zero for non-entry actions", test_chase_pct_is_zero_for_non_entry_actions),
    ("master_shortlist write has no conviction/rank", test_master_shortlist_write_also_has_no_conviction_or_rank),
]
