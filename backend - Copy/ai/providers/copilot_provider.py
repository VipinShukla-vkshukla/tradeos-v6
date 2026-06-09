"""Microsoft Copilot / Azure OpenAI Provider"""
import json, os
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT

class CopilotProvider(BaseProvider):
    def __init__(self, api_key: str, endpoint: str = "", deployment: str = "gpt-4o"):
        self.api_key    = api_key
        self.endpoint   = endpoint or os.getenv("AZURE_OPENAI_ENDPOINT", "")
        self.deployment = deployment

    def is_available(self) -> bool:
        return bool(self.api_key and self.endpoint)

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        try:
            from openai import AzureOpenAI
            client = AzureOpenAI(
                api_key=self.api_key,
                api_version="2024-02-01",
                azure_endpoint=self.endpoint
            )
            resp = client.chat.completions.create(
                model=self.deployment,
                max_tokens=500,
                messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}]
            )
            data = json.loads(resp.choices[0].message.content.strip())
            return ConvictionResult(provider="copilot", **{k: data.get(k,"") for k in
                ["conviction","conviction_reason","catalyst","suggested_action",
                 "strategy_validation","conflicts","ai_note"]},
                risks=data.get("risks",[]), confidence=float(data.get("confidence",0.5)))
        except Exception as e:
            from loguru import logger; logger.warning(f"Copilot error: {e}")
            return UNKNOWN_RESULT
