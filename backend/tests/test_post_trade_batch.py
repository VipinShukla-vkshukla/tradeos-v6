"""
Batched post-trade AI lessons (option 5A, 15-Nov-2026) — `main()` now
analyses pending trades in batches of `post_trade_batch_size` (default 10)
with ONE raw_completion call per batch, mirroring ai_decision_engine's own
call_ai_batched() design.

Before this, every closed trade made its own AI call, each paying the full
provider-chain retry tax (two dead :free models + an empty-content
reasoning attempt) before a working model answered — ~45-75s of retries
per lesson, which is what made the evening pipeline "re-evaluate models it
already evaluated" and torch the shared 50-request/day free quota.

This module proves, with the real code path (not a re-implementation):
  1. the code DEFAULT batch size is 10 (distinguishing case, same trick as
     test_ai_decision_batch_size.py's default-is-15 proof),
  2. an explicit config override is honoured,
  3. one raw_completion call per batch, with max_tokens scaling with N,
  4. symbol-mapped array items map back onto the right trades,
  5. an unusable item falls back to RULE_BASED for that trade only, and
  6. an unparseable whole batch falls back to per-trade lessons without
     losing any trade.

No live AI call and no database: every loader is mocked, per this suite's
own rule that it never costs real money.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests import cfg_ctx


def _trade(i: int) -> dict:
    return {
        "id": f"t{i}",
        "symbol": f"SYM{i}",
        "strategy": "TEST",
        "sector": "TEST",
        "entry_date": "2026-11-10",
        "exit_date": "2026-11-14",
        "pnl_pct": 3.0,
        "framework": "SWING",
    }


def _lesson_json(sym: str) -> dict:
    return {
        "symbol": sym,
        "scenario_type": "Win/Trend-Follow",
        "trigger_event": "result beat",
        "what_expected": "expected follow-through",
        "what_happened": "followed through",
        "what_failed": "Nothing — trade worked as planned",
        "root_cause": "held with the trend",
        "corrective_rule": f"Rule: hold {sym} while trend intact",
        "confidence_note": "validated on B-grade setup",
    }


def _run_batch(trades, cfg_values, array_payload=None, raw_side_effect=None):
    """Call analyze_trades_batch with every loader mocked; return
    (lessons, raw_completion_kwargs_list)."""
    from ai import post_trade_analysis as pta

    calls = []

    def fake_raw_completion(prompt, max_tokens=1500, call_site=None, **kw):
        calls.append({"prompt": prompt, "max_tokens": max_tokens,
                      "call_site": call_site})
        if raw_side_effect is not None:
            raise raw_side_effect
        if array_payload is not None:
            return json.dumps(array_payload)
        return json.dumps([_lesson_json(t["symbol"]) for t in trades])

    with cfg_ctx(cfg_values), \
            patch.object(pta, "load_signal_context", return_value={}), \
            patch.object(pta, "load_msl_trajectory", return_value=None), \
            patch.object(pta, "analyse_trajectory", return_value={}), \
            patch.object(pta, "generate_rule_based_lesson",
                         side_effect=lambda *a: {
                             "scenario_type": "RULE_BASED_TEST",
                             "corrective_rule": "Rule: test"}), \
            patch.object(pta, "load_regime_at_entry", return_value={}), \
            patch.object(pta, "load_events_at_entry", return_value=""), \
            patch.object(pta, "load_existing_lessons", return_value=[]), \
            patch("ai.ai_router.raw_completion", side_effect=fake_raw_completion):
        lessons = pta.analyze_trades_batch(trades, sb=MagicMock())
    return lessons, calls


def test_code_default_batch_size_is_10():
    """No batch_size override (ai_provider set only to satisfy the
    disabled/ml gate — the router itself is mocked): 25 trades must make
    ceil(25/10) = 3 AI calls (10, 10, 5) — not 1 call per trade (25), not
    batches of 5."""
    lessons, calls = _run_batch([_trade(i) for i in range(25)],
                                cfg_values={"ai_provider": "openrouter"})
    assert len(calls) == 3, (
        f"25 trades made {len(calls)} AI call(s) with no override — "
        f"expected 3 (batches of 10); 25 means the default is 1, 5 means "
        f"the default is still something else")
    assert len(lessons) == 25
    assert all(l["source"].startswith("AI:") for l in lessons)


def test_explicit_override_is_honoured():
    """Explicit batch_size=5 must produce ceil(12/5) = 3 calls."""
    lessons, calls = _run_batch([_trade(i) for i in range(12)],
                                cfg_values={"ai_provider": "openrouter",
                                            "post_trade_batch_size": "5"})
    assert len(calls) == 3, f"expected ceil(12/5)=3 calls, got {len(calls)}"


def test_max_tokens_scale_with_batch_size():
    """max_tokens = 400 per trade + 500 header, from the code default —
    a 10-trade batch must ask for 4500, not the old hardcoded 800."""
    lessons, calls = _run_batch([_trade(i) for i in range(10)],
                                cfg_values={"ai_provider": "openrouter"})
    assert calls and calls[0]["max_tokens"] == 4500, (
        f"expected 4500 max_tokens for a 10-trade batch, "
        f"got {calls[0]['max_tokens'] if calls else None}")


def test_array_items_map_to_trades_by_symbol():
    """Items returned out of order still land on the right trades."""
    trades = [_trade(1), _trade(2), _trade(3)]
    shuffled = [_lesson_json(t["symbol"]) for t in reversed(trades)]
    lessons, _ = _run_batch(trades,
                            cfg_values={"ai_provider": "openrouter",
                                        "post_trade_batch_size": "3"},
                            array_payload=shuffled)
    by_symbol = {l["linked_symbols"][0]: l for l in lessons}
    assert by_symbol["SYM1"]["corrective_rule"] == "Rule: hold SYM1 while trend intact"
    assert by_symbol["SYM2"]["corrective_rule"] == "Rule: hold SYM2 while trend intact"
    assert by_symbol["SYM3"]["corrective_rule"] == "Rule: hold SYM3 while trend intact"


def test_unusable_item_falls_back_to_rule_based_for_that_trade_only():
    trades = [_trade(1), _trade(2), _trade(3)]
    payload = [_lesson_json("SYM1"), {"symbol": "SYM2"},   # no lesson keys
               _lesson_json("SYM3")]
    lessons, _ = _run_batch(trades,
                            cfg_values={"ai_provider": "openrouter",
                                        "post_trade_batch_size": "3"},
                            array_payload=payload)
    by_symbol = {l["linked_symbols"][0]: l for l in lessons}
    assert by_symbol["SYM1"]["source"].startswith("AI:")
    assert by_symbol["SYM2"]["source"] == "RULE_BASED", (
        "an AI item without scenario_type/corrective_rule must not be "
        "stored as an AI lesson")
    assert by_symbol["SYM3"]["source"].startswith("AI:")


def test_unparseable_whole_batch_falls_back_without_losing_trades():
    """raw_completion raising (quota dead / network) must NOT lose trades:
    every trade still comes out as a complete, rule-based lesson — the same
    floor today's per-trade path lands on."""
    lessons, calls = _run_batch([_trade(i) for i in range(4)],
                                cfg_values={"ai_provider": "openrouter",
                                            "post_trade_batch_size": "4"},
                                raw_side_effect=RuntimeError("429"))
    assert len(lessons) == 4
    assert all(l["source"] == "RULE_BASED" for l in lessons)
    assert all(l["scenario_type"] for l in lessons)


def _run_disabled(trades):
    """analyze_trades_batch with provider_override='disabled' — the exact
    mechanism the post_trade_ai_enabled gate switches on."""
    from ai import post_trade_analysis as pta
    calls = []

    def fake_raw_completion(*a, **kw):
        calls.append(1)
        return "[]"

    with cfg_ctx({"ai_provider": "openrouter"}), \
            patch.object(pta, "load_signal_context", return_value={}), \
            patch.object(pta, "load_msl_trajectory", return_value=None), \
            patch.object(pta, "analyse_trajectory", return_value={}), \
            patch.object(pta, "generate_rule_based_lesson",
                         side_effect=lambda *a: {
                             "scenario_type": "RULE_BASED_TEST",
                             "corrective_rule": "Rule: test"}), \
            patch.object(pta, "load_regime_at_entry", return_value={}), \
            patch.object(pta, "load_events_at_entry", return_value=""), \
            patch.object(pta, "load_existing_lessons", return_value=[]), \
            patch("ai.ai_router.raw_completion", side_effect=fake_raw_completion):
        lessons = pta.analyze_trades_batch(trades, sb=MagicMock(),
                                           provider_override="disabled")
    return lessons, calls


def test_disabled_provider_is_rule_based_with_zero_ai_calls():
    lessons, calls = _run_disabled([_trade(i) for i in range(3)])
    assert calls == [], (
        f"disabled provider must make zero AI calls, made {len(calls)}")
    assert len(lessons) == 3
    assert all(l["source"] == "RULE_BASED" for l in lessons)


def test_post_trade_ai_defaults_off():
    from ai import post_trade_analysis as pta
    with cfg_ctx({}):
        assert pta.post_trade_ai_enabled() is False


def test_post_trade_ai_explicit_on():
    from ai import post_trade_analysis as pta
    with cfg_ctx({"post_trade_ai_enabled": "true"}):
        assert pta.post_trade_ai_enabled() is True


TESTS = [
    ("code default is 10, not 1 (distinguishing case)",
     test_code_default_batch_size_is_10),
    ("explicit batch_size override is honoured",
     test_explicit_override_is_honoured),
    ("max_tokens scale with batch size (400/trade + 500)",
     test_max_tokens_scale_with_batch_size),
    ("array items map back to trades by symbol",
     test_array_items_map_to_trades_by_symbol),
    ("unusable item -> rule-based for that trade only",
     test_unusable_item_falls_back_to_rule_based_for_that_trade_only),
    ("unparseable batch -> rule-based, no trade lost",
     test_unparseable_whole_batch_falls_back_without_losing_trades),
    ("post_trade AI defaults OFF (quota stays with steps 23/24)",
     test_post_trade_ai_defaults_off),
    ("post_trade_ai_enabled=true is honoured",
     test_post_trade_ai_explicit_on),
    ("disabled provider -> rule-based, zero AI calls",
     test_disabled_provider_is_rule_based_with_zero_ai_calls),
]
