"""Claude (Anthropic) Provider"""
import json
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT
import re

class ClaudeProvider(BaseProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def is_available(self) -> bool:
        return bool(self.api_key)

    def _client(self):
        if not self._client:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=self.api_key)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}]
            )
            raw = msg.content[0].text.strip()
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                raise ValueError(f"No JSON in response: {raw[:100]}")
            data = json.loads(json_match.group())

            return ConvictionResult(
                conviction=data.get("conviction", "MEDIUM"),
                conviction_reason=data.get("conviction_reason", ""),
                risks=data.get("risks", []),
                catalyst=data.get("catalyst", ""),
                suggested_action=data.get("suggested_action", "WAIT"),
                strategy_validation=data.get("strategy_validation", ""),
                conflicts=data.get("conflicts", "NONE"),
                ai_note=data.get("ai_note", ""),
                provider="claude",
                confidence=float(data.get("confidence", 0.5)),
            )
        except Exception as e:
            from loguru import logger
            logger.warning(f"Claude provider error: {e}")
            return UNKNOWN_RESULT
