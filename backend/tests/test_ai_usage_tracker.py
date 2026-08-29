"""
Real token/cost visibility for AI calls — 29-Aug-2026, migration 127.

Before this: the three highest-volume AI call sites (evening decision
engine's batches, evening market-intel call, intraday's advisor firing up
to ~75x/trading day) reported zero token or cost usage anywhere in the
codebase. This module makes every call measurable without changing what
any of them decide — `_extract_usage` is pure and provider-shape-aware
(OpenAI-compatible clients, Claude's SDK, and Gemini's SDK all shape
`resp.usage` differently), and `log_usage` must NEVER raise, since a
logging failure breaking the AI call it observes would be strictly worse
than not observing it at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ai.usage_tracker import _extract_usage, log_usage


def _openai_style_resp(prompt_tokens=100, completion_tokens=50, finish_reason="stop"):
    resp = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.choices = [MagicMock()]
    resp.choices[0].finish_reason = finish_reason
    return resp


def _claude_style_resp(input_tokens=80, output_tokens=40, stop_reason="end_turn"):
    resp = MagicMock()
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.stop_reason = stop_reason
    return resp


def _gemini_style_resp(prompt_tokens=60, candidate_tokens=30):
    resp = MagicMock()
    resp.usage_metadata.prompt_token_count = prompt_tokens
    resp.usage_metadata.candidates_token_count = candidate_tokens
    return resp


def test_extract_usage_openai_compatible():
    pt, ct, fr = _extract_usage(_openai_style_resp(120, 60, "stop"), "deepseek")
    assert (pt, ct, fr) == (120, 60, "stop")


def test_extract_usage_flags_truncation():
    pt, ct, fr = _extract_usage(_openai_style_resp(finish_reason="length"), "openai")
    assert fr == "length", "a truncated response must be visible as such"


def test_extract_usage_claude_shape():
    pt, ct, fr = _extract_usage(_claude_style_resp(80, 40, "end_turn"), "claude")
    assert (pt, ct, fr) == (80, 40, "end_turn")


def test_extract_usage_gemini_shape():
    pt, ct, fr = _extract_usage(_gemini_style_resp(60, 30), "gemini")
    assert (pt, ct) == (60, 30)


def test_extract_usage_never_raises_on_malformed_response():
    """A response object missing the expected attributes entirely (a
    provider SDK version bump, a mocked test double, anything) must
    degrade to (None, None, None), never raise — this function's whole
    job is to be safe to call from inside a hot path."""
    class Empty:
        pass
    pt, ct, fr = _extract_usage(Empty(), "deepseek")
    assert (pt, ct, fr) == (None, None, None)

    pt, ct, fr = _extract_usage(None, "claude")
    assert (pt, ct, fr) == (None, None, None)


def test_log_usage_writes_expected_row():
    fake_sb = MagicMock()
    log_usage("ai_decision_engine", "deepseek", "deepseek-v4-flash",
             _openai_style_resp(100, 50, "stop"), framework="SWING", sb=fake_sb)
    fake_sb.table.assert_called_once_with("ai_usage_log")
    row = fake_sb.table.return_value.insert.call_args[0][0]
    assert row["call_site"] == "ai_decision_engine"
    assert row["framework"] == "SWING"
    assert row["provider"] == "deepseek"
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50
    assert row["total_tokens"] == 150
    assert row["finish_reason"] == "stop"


def test_log_usage_never_raises_when_the_write_fails():
    """The whole point of best-effort logging: a broken table, a network
    blip, a bad row shape — none of it may propagate to the caller, which
    is in the middle of handling a real AI response."""
    fake_sb = MagicMock()
    fake_sb.table.side_effect = RuntimeError("table does not exist")
    log_usage("ai_decision_engine", "deepseek", "deepseek-v4-flash",
             _openai_style_resp(), sb=fake_sb)   # must not raise


def test_log_usage_handles_missing_usage_gracefully():
    """A resp with no usable usage data at all still writes a row (all
    token fields null) rather than skipping the call-site record
    entirely — knowing a call happened, even without its cost, is still
    worth having."""
    fake_sb = MagicMock()
    log_usage("intraday_ai_advisor", "deepseek", "deepseek-v4-flash",
             object(), framework="INTRADAY", sb=fake_sb)
    row = fake_sb.table.return_value.insert.call_args[0][0]
    assert row["prompt_tokens"] is None
    assert row["total_tokens"] is None
    assert row["call_site"] == "intraday_ai_advisor"


TESTS = [
    ("extract usage — openai-compatible shape", test_extract_usage_openai_compatible),
    ("extract usage — flags truncation via finish_reason",
     test_extract_usage_flags_truncation),
    ("extract usage — claude shape", test_extract_usage_claude_shape),
    ("extract usage — gemini shape", test_extract_usage_gemini_shape),
    ("extract usage — never raises on malformed response",
     test_extract_usage_never_raises_on_malformed_response),
    ("log_usage — writes the expected row", test_log_usage_writes_expected_row),
    ("log_usage — never raises when the write fails",
     test_log_usage_never_raises_when_the_write_fails),
    ("log_usage — missing usage still logs the call",
     test_log_usage_handles_missing_usage_gracefully),
]
