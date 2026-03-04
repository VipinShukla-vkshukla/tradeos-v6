"""DeepSeek Provider (OpenAI-compatible API)"""
import json
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT

class DeepSeekProvider(BaseProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def is_available(self) -> bool:
        return bool(self.api_key)

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com")
            resp = client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=500,
                messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}]
            )
            data = json.loads(resp.choices[0].message.content.strip())
            return ConvictionResult(provider="deepseek", **{k: data.get(k,"") for k in
                ["conviction","conviction_reason","catalyst","suggested_action",
                 "strategy_validation","conflicts","ai_note"]},
                risks=data.get("risks",[]), confidence=float(data.get("confidence",0.5)))
        except Exception as e:
            from loguru import logger; logger.warning(f"DeepSeek error: {e}")
            return UNKNOWN_RESULT
