"""
Real token usage for every AI call — 29-Aug-2026. Migration 127.

Before this: the three highest-volume AI call sites (`ai_decision_engine`'s
evening batches, `market_intelligence_engine`'s evening call, intraday's
`ai_advisor` firing up to ~75x/trading day) reported zero cost or token
usage anywhere. The only budget mechanism in the codebase
(`post_trade_analysis.PROVIDER_COST_INR`) is a hardcoded per-call guess,
not derived from `resp.usage`, and scoped to the smallest, cheapest,
least-frequent path. This module is purely additive observability — it
changes no AI-call or trading behaviour, only makes what already happens
measurable.

`_extract_usage` is pure and provider-shape-aware, because `resp.usage`
is not the same shape across providers (OpenAI-compatible clients use
`prompt_tokens`/`completion_tokens`; Claude's SDK uses
`input_tokens`/`output_tokens`; Gemini's uses `usage_metadata`). Never
raises — an unrecognised or missing shape returns (None, None, None)
rather than crashing the call it is observing.
"""

from __future__ import annotations

from loguru import logger


def _extract_usage(resp, provider: str) -> tuple[int | None, int | None, str | None]:
    try:
        if provider == "claude":
            u = getattr(resp, "usage", None)
            return (getattr(u, "input_tokens", None),
                    getattr(u, "output_tokens", None),
                    getattr(resp, "stop_reason", None))
        if provider == "gemini":
            u = getattr(resp, "usage_metadata", None)
            return (getattr(u, "prompt_token_count", None),
                    getattr(u, "candidates_token_count", None),
                    None)
        # openai-compatible: openai, deepseek, grok, copilot
        u = getattr(resp, "usage", None)
        choices = getattr(resp, "choices", None) or []
        fr = getattr(choices[0], "finish_reason", None) if choices else None
        return (getattr(u, "prompt_tokens", None),
                getattr(u, "completion_tokens", None), fr)
    except Exception:
        return None, None, None


def log_usage(call_site: str, provider: str, model: str, resp,
              framework: str | None = None, sb=None) -> None:
    """
    Best-effort. A logging failure must never break the AI call it is
    observing — every path here is wrapped, warning only on failure.
    """
    try:
        pt, ct, fr = _extract_usage(resp, provider)
        row = {
            "call_site": call_site, "framework": framework,
            "provider": provider, "model": model,
            "prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": (pt + ct) if (pt is not None and ct is not None) else None,
            "finish_reason": fr,
        }
        if sb is None:
            from config import get_supabase
            sb = get_supabase()
        sb.table("ai_usage_log").insert(row).execute()
    except Exception as e:
        logger.warning(f"  ai usage log failed for {call_site} — {e}")
