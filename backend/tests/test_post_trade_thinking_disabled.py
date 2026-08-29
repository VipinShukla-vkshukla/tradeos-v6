"""
`ai/post_trade_analysis.py::_call_provider()`'s deepseek branch builds its
own `openai.OpenAI` client instead of routing through `ai_router.py`, and
never got that module's own fix for this exact model: deepseek-v4-flash is
a reasoning model whose `max_tokens` budgets reasoning AND output
together, reasoning spent first — `ai_router.raw_completion()`'s own
docstring documents a real incident (a 20,000-token budget burned entirely
on discarded reasoning, zero output). This call site's budget is only 800
tokens, a far smaller cushion, and nothing here even reads
`reasoning_content` — there is nothing to lose by disabling it, matching
`ai_router.py`'s own precedent exactly.

Found 29-Aug-2026 auditing every AI call site for cost efficiency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests import cfg_ctx


def _fake_openai_response(text="ok"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


def test_thinking_disabled_by_default():
    from ai.post_trade_analysis import _call_provider
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_openai_response()
    with cfg_ctx({}), patch("openai.OpenAI", return_value=fake_client):
        _call_provider("deepseek", {"deepseek": "fake-key"}, "prompt")
    _, kwargs = fake_client.chat.completions.create.call_args
    assert kwargs.get("extra_body") == {"thinking": {"type": "disabled"}}, (
        "deepseek call did not disable thinking — the reasoning-budget "
        "leak this fix closes is back")


def test_thinking_stays_on_when_explicitly_armed():
    from ai.post_trade_analysis import _call_provider
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_openai_response()
    with cfg_ctx({"ai_thinking_enabled": "true"}), \
         patch("openai.OpenAI", return_value=fake_client):
        _call_provider("deepseek", {"deepseek": "fake-key"}, "prompt")
    _, kwargs = fake_client.chat.completions.create.call_args
    assert "extra_body" not in kwargs, (
        "ai_thinking_enabled=true must be a real switch, not overridden")


def test_non_deepseek_provider_is_unaffected():
    """This fix must not add extra_body for a provider that doesn't
    support this parameter — openai/grok/copilot all share the deepseek
    branch's sibling code, untouched by this fix."""
    from ai.post_trade_analysis import _call_provider
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = _fake_openai_response()
    with cfg_ctx({}), patch("openai.OpenAI", return_value=fake_client):
        _call_provider("openai", {"openai": "fake-key"}, "prompt")
    _, kwargs = fake_client.chat.completions.create.call_args
    assert "extra_body" not in kwargs


TESTS = [
    ("deepseek thinking disabled by default", test_thinking_disabled_by_default),
    ("ai_thinking_enabled=true keeps thinking on",
     test_thinking_stays_on_when_explicitly_armed),
    ("non-deepseek provider unaffected", test_non_deepseek_provider_is_unaffected),
]
