"""
`ai_decision_batch_size` raised 5 -> 15, 29-Aug-2026. `call_ai_batched()`'s
own design sends every candidate to EVERY batch regardless of batch_size
(sector/correlation judgements need the whole field) — only the OUTPUT
list narrows per batch. That means a SMALLER batch_size produces MORE
full-context resends, not fewer; the original size (5) was set to guard
against a truncation incident (12 candidates, 2026-07-31) that predates
its actual root-cause fix (ai_thinking_enabled=false, 2026-08-01) by one
day and has not recurred since. Real daily candidate volume (8-45,
median ~20-24, measured over 20 sessions) means batch_size=5 was
producing 2-9 redundant resends on a typical evening for a risk that no
longer applies at this size.

This module proves the batch COUNT math for realistic daily volumes —
not a live AI call, which would cost real money and this test suite
never does. `cfg_ctx({})` (empty, no override) is used deliberately in
every test below EXCEPT the one that tests an explicit override — an
empty config forces `cfg_int`'s call inside call_ai_batched() to fall
through to its own literal default argument, which is the only way to
actually distinguish "the code says 5" from "the code says 15" rather
than testing whatever value the test itself happens to inject.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx


def _fake_symbols(n: int) -> list[dict]:
    return [{"symbol": f"SYM{i}"} for i in range(n)]


def _call_with_mocked_ai(n: int, cfg_values: dict) -> int:
    """Returns how many AI calls call_ai_batched() actually made for n
    candidates under the given config — the real, exercised code path,
    not a re-implementation of its arithmetic."""
    from ai.ai_decision_engine import call_ai_batched
    calls = []
    ctx = {"candidates": _fake_symbols(n)}
    with cfg_ctx(cfg_values), \
         patch("ai.ai_decision_engine.call_ai",
               side_effect=lambda p: calls.append(p) or {"ranked_candidates": []}), \
         patch("ai.ai_decision_engine.build_prompt", return_value="prompt"):
        call_ai_batched(ctx, "2026-08-29")
    return len(calls)


def test_code_default_is_15_not_5():
    """The distinguishing case: 8 candidates makes 2 calls at the OLD
    default (ceil(8/5)) and 1 call at the NEW default (ceil(8/15)).
    cfg_ctx({}) supplies NO override, so this reads call_ai_batched()'s
    own literal `cfg_int("ai_decision_batch_size", 15)` fallback — a
    test that passed under both 5 and 15 would not be testing the
    default at all, which is exactly the gap caught and fixed here
    before this file was ever registered in tools.verify."""
    n_calls = _call_with_mocked_ai(8, cfg_values={})
    assert n_calls == 1, (
        f"8 candidates made {n_calls} call(s) with no batch_size override — "
        f"expected 1 (ceil(8/15)); 2 would mean the code default is still 5")


def test_explicit_config_override_is_still_honoured():
    """A caller (or operator) that explicitly sets batch_size must still
    get exactly that — the default change must not make the config key
    itself inert."""
    n_calls = _call_with_mocked_ai(20, cfg_values={"ai_decision_batch_size": "5"})
    assert n_calls == 4, f"expected ceil(20/5)=4 calls, got {n_calls}"


def test_batch_count_for_real_measured_daily_volumes():
    """29-Aug-2026 measured range: 8 to 45 candidates/day, no config
    override (tests the actual default every real evening run gets)."""
    expected_batches_at_default_15 = {
        8: 1, 9: 1, 13: 1, 17: 2, 20: 2, 22: 2,
        24: 2, 26: 2, 29: 2, 31: 3, 45: 3,
    }
    for n, expected in expected_batches_at_default_15.items():
        n_calls = _call_with_mocked_ai(n, cfg_values={})
        assert n_calls == expected, (
            f"{n} candidates made {n_calls} call(s) at the default batch "
            f"size, expected {expected}")


def test_every_batch_still_receives_the_full_candidate_list():
    """The one property that must never regress: build_prompt is always
    called with the FULL ctx (every candidate), never a trimmed one —
    only call_ai_batched's own OUTPUT grouping narrows, per this
    module's own documented design (sector/correlation judgements need
    the whole field)."""
    from ai.ai_decision_engine import call_ai_batched
    ctx = {"candidates": _fake_symbols(31)}   # > default batch size, forces real batching
    seen_ctx_sizes = []

    def fake_build_prompt(ctx_arg, trade_date, rank_only=None):
        seen_ctx_sizes.append(len(ctx_arg["candidates"]))
        return "prompt"

    with cfg_ctx({}), \
         patch("ai.ai_decision_engine.call_ai",
               return_value={"ranked_candidates": []}), \
         patch("ai.ai_decision_engine.build_prompt", side_effect=fake_build_prompt):
        call_ai_batched(ctx, "2026-08-29")

    assert seen_ctx_sizes and all(n == 31 for n in seen_ctx_sizes), (
        f"a batch saw a trimmed candidate list ({seen_ctx_sizes}) — this "
        f"would silently destroy sector/correlation cross-candidate "
        f"judgement, the exact regression this change must not cause")


TESTS = [
    ("code default is 15, not 5 (distinguishing case)",
     test_code_default_is_15_not_5),
    ("explicit config override is still honoured",
     test_explicit_config_override_is_still_honoured),
    ("batch count matches real measured daily volumes (no override)",
     test_batch_count_for_real_measured_daily_volumes),
    ("every batch still sees the FULL candidate list",
     test_every_batch_still_receives_the_full_candidate_list),
]
