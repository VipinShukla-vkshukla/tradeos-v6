"""Ollama Local Provider — Completely Free, Unlimited, Runs on Your Daemon.

Runs models locally on the same server as the pipeline. No API keys, no credits,
no rate limits. Requires Ollama running on the daemon server.

Setup on daemon:
    curl -fsSL https://ollama.ai/install.sh | sh
    ollama serve &
    ollama pull llama3.2:3b    # 2GB RAM - fast
    ollama pull mistral:7b     # 4GB RAM - better quality

Environment:
    OLLAMA_BASE_URL=http://localhost:11434   # Default
    OLLAMA_MODEL=llama3.2:3b                  # Default model
"""
import json
import re
import os
from openai import OpenAI
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT


class OllamaProvider(BaseProvider):
    """
    Local Ollama provider using OpenAI-compatible API.
    No API key needed - runs on your daemon server.
    """
    
    # Ordered model chain - first available wins
    MODELS = [
        "llama3.2:3b",      # 2GB RAM - fast, good for analysis
        "mistral:7b",       # 4GB RAM - better reasoning
        "qwen2.5:7b",       # 4GB RAM - good for coding/analysis
        "llama3.1:8b",      # 5GB RAM - strong general purpose
    ]

    def __init__(self, api_key: str = ""):
        """api_key is ignored - kept for interface compatibility."""
        self.base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.default_model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        # Ensure default model is first in chain
        self.MODELS = [self.default_model] + [m for m in self.MODELS if m != self.default_model]

    def is_available(self) -> bool:
        """Check if Ollama server is reachable."""
        try:
            import requests
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def analyze_signal(self, stock_data, context) -> ConvictionResult:
        last_error = None
        
        for model in self.MODELS:
            try:
                client = OpenAI(
                    api_key="ollama",  # Dummy key - Ollama ignores it
                    base_url=f"{self.base_url}/v1"
                )
                resp = client.chat.completions.create(
                    model=model,
                    max_tokens=800,  # Slightly higher for local models
                    messages=[{"role": "user", "content": self.build_prompt(stock_data, context)}],
                )
                raw = (resp.choices[0].message.content or "").strip()
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if not m:
                    raise ValueError(f"No JSON in response: {raw[:100]}")
                data = json.loads(m.group())
                
                return ConvictionResult(
                    provider=f"ollama:{model}",
                    **{k: data.get(k, "") for k in
                       ["conviction", "conviction_reason", "catalyst", "suggested_action",
                        "strategy_validation", "conflicts", "ai_note"]},
                    risks=data.get("risks", []),
                    confidence=float(data.get("confidence", 0.5)),
                )
            except Exception as e:
                last_error = e
                from loguru import logger
                logger.warning(f"Ollama model {model} failed: {e}")
        
        # If all models fail, log the last error
        if last_error:
            from loguru import logger
            logger.error(f"All Ollama models failed: {last_error}")
        return UNKNOWN_RESULT

    def build_prompt(self, stock_data, context) -> str:
        """Build the analysis prompt - same format as other providers."""
        return f"""You are a professional equity analyst. Analyze this stock signal and output ONLY valid JSON.

STOCK DATA:
- Symbol: {stock_data.get('symbol', 'UNKNOWN')}
- Close: ₹{stock_data.get('close', 'N/A')}
- RSI Daily: {stock_data.get('rsi_daily', 'N/A')}
- RSI Weekly: {stock_data.get('rsi_weekly', 'N/A')}
- 1M Return: {stock_data.get('ret_1m', 'N/A')}%
- RS vs Nifty: {stock_data.get('rs_vs_nifty', 'N/A')}%
- Above SMA50: {stock_data.get('above_sma50', 'N/A')}
- Sector: {stock_data.get('sector', 'N/A')}
- Sector Rank: {stock_data.get('sector_rank', 'N/A')}
- Delivery %: {stock_data.get('delivery_pct', 'N/A')}%
- Volume Ratio: {stock_data.get('volume_ratio', 'N/A')}

CONTEXT:
- Regime: {context.get('regime', 'NEUTRAL')}
- FII Flag: {context.get('fii_flag', 'N/A')}
- Market Breadth: {context.get('breadth_pct', 'N/A')}%
- Events (next 5d): {context.get('event_summary', 'None')}
- Historical Echoes: {context.get('echo_summary', 'None')}

OUTPUT ONLY THIS JSON (no extra text):
{{
  "conviction": "HIGH|MEDIUM|LOW",
  "conviction_reason": "Brief reason (max 2 sentences)",
  "catalyst": "Key catalyst or null",
  "suggested_action": "ENTER|WAIT|AVOID",
  "strategy_validation": "Matches CTL/SBS/etc or conflicts",
  "conflicts": "Any rule conflicts or null",
  "ai_note": "Historical context or null",
  "risks": ["risk1", "risk2"],
  "confidence": 0.0-1.0
}}"""