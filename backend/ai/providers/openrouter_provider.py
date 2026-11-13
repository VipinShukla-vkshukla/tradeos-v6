"""OpenRouter provider (OpenAI-compatible).

Tries an ordered list of models and auto-switches to the next one if the
current model errors out or returns UNKNOWN. Falls back to free models so
TradeOS never hard-fails on AI when paid providers are exhausted.
"""
import json
import re
from openai import OpenAI
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT


class OpenRouterProvider(BaseProvider):
    # Ordered model chain: first available model wins; on failure try the next.
    MODELS = [
        "deepseek/deepseek-chat",          # deepseek via OpenRouter
        "deepseek/deepseek-coder",         # deepseek backup variant
        "meta-llama/llama-3.1-8b-instruct:free",   # free fallback
    ]

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1"

    def is_available(self) -> bool:
        return bool(self.api_key)

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        last_error = None
        for model in self.MODELS:
            try:
                client = OpenAI(api_key=self.api_key, base_url=self.base_url)
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=500,
                    messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}],
                    extra_headers={
                        "HTTP-Referer": "https://localhost",
                        "X-Title": "TradeOS",
                    },
                )
                raw = (resp.choices[0].message.content or "").strip()
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    raise ValueError(f"No JSON in response: {raw[:100]}")
                data = json.loads(m.group())
                return ConvictionResult(
                    provider="openrouter",
                    model=model,
                    **{k: data.get(k, "") for k in
                       ["conviction", "conviction_reason", "catalyst", "suggested_action",
                        "strategy_validation", "conflicts", "ai_note"]},
                    risks=data.get("risks", []),
                    confidence=float(data.get("confidence", 0.5)),
                )
            except Exception as e:
                last_error = e
                from loguru import logger
                logger.warning(f"OpenRouter model {model} failed: {e}")
        return UNKNOWN_RESULT
