"""OpenAI (ChatGPT) Provider"""
import json
import re
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT

class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def is_available(self) -> bool:
        return bool(self.api_key)

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=500,
                messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}]
            )
            raw = resp.choices[0].message.content.strip()
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                raise ValueError(f"No JSON in response: {raw[:100]}")
            data = json.loads(json_match.group())
            
            return ConvictionResult(provider="openai", **{k: data.get(k, "") for k in
                ["conviction","conviction_reason","catalyst","suggested_action",
                 "strategy_validation","conflicts","ai_note"]},
                risks=data.get("risks",[]), confidence=float(data.get("confidence",0.5)))
        except Exception as e:
            from loguru import logger; logger.warning(f"OpenAI error: {e}")
            return UNKNOWN_RESULT
