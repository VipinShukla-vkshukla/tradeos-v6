"""
TradeOS v6 — AI Router
Routes to configured provider with automatic fallback chain.
Switch provider from frontend Settings — no code changes needed.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import cfg, AI_KEYS, AZURE_ENDPOINT, AZURE_DEPLOYMENT
from ai.providers.base_provider import ConvictionResult, UNKNOWN_RESULT


def _get_provider(name: str):
    from ai.providers.claude_provider   import ClaudeProvider
    from ai.providers.openai_provider   import OpenAIProvider
    from ai.providers.gemini_provider   import GeminiProvider
    from ai.providers.deepseek_provider import DeepSeekProvider
    from ai.providers.grok_provider     import GrokProvider
    from ai.providers.copilot_provider  import CopilotProvider
    from ai.providers.ml_provider       import MLProvider

    providers = {
        "claude":   lambda: ClaudeProvider(AI_KEYS["claude"]),
        "openai":   lambda: OpenAIProvider(AI_KEYS["openai"]),
        "gemini":   lambda: GeminiProvider(AI_KEYS["gemini"]),
        "deepseek": lambda: DeepSeekProvider(AI_KEYS["deepseek"]),
        "grok":     lambda: GrokProvider(AI_KEYS["grok"]),
        "copilot":  lambda: CopilotProvider(AI_KEYS["copilot"], AZURE_ENDPOINT, AZURE_DEPLOYMENT),
        "ml":       lambda: MLProvider(),
    }
    factory = providers.get(name)
    return factory() if factory else None


def analyze(stock_data: dict, context: dict) -> ConvictionResult:
    """
    Route to configured provider. Falls back to ML → scraping → UNKNOWN.
    Provider set in system_config.ai_provider.
    """
    provider_name = cfg("ai_provider", "disabled").lower()

    if provider_name == "disabled":
        return UNKNOWN_RESULT

    # Try primary provider
    if provider_name != "ml":
        provider = _get_provider(provider_name)
        if provider and provider.is_available():
            result = provider.analyze_signal(stock_data, context)
            if result.conviction != "UNKNOWN":
                return result
            logger.warning(f"Primary provider {provider_name} returned UNKNOWN — trying fallback")

    # ML fallback
    if cfg("ai_fallback_ml", "true").lower() == "true":
        from ai.providers.ml_provider import MLProvider
        ml = MLProvider()
        if ml.is_available():
            logger.debug(f"Using ML fallback for {stock_data.get('symbol')}")
            result = ml.analyze_signal(stock_data, context)
            result.fallback_used = True
            if result.conviction != "UNKNOWN":
                return result

    # Scraping fallback
    if cfg("ai_fallback_scraping", "true").lower() == "true":
        try:
            from ai.fallback.web_scraper     import get_news_for_symbol
            from ai.fallback.sentiment_scorer import score_news_list
            sym   = stock_data.get("symbol", "")
            news  = get_news_for_symbol(sym)
            score = score_news_list(news)
            sentiment = score["overall"]
            conviction = "HIGH" if sentiment == "POSITIVE" else ("LOW" if sentiment == "NEGATIVE" else "MEDIUM")
            return ConvictionResult(
                conviction=conviction,
                conviction_reason=f"Web scraping: {score['count']} headlines, sentiment={sentiment}",
                risks=["⚠️ Scraping fallback only — no technical analysis"] +
                      (["Upcoming results/event detected"] if score["risk_events"] else []),
                catalyst="Based on recent news headlines",
                suggested_action="WAIT",
                strategy_validation="Manual verification recommended",
                conflicts="NONE",
                ai_note=f"Scraped {score['count']} headlines, net sentiment score: {score['score']:.2f}",
                provider="web_scraping",
                fallback_used=True,
                confidence=0.3,
            )
        except Exception as e:
            logger.warning(f"Scraping fallback failed: {e}")

    return UNKNOWN_RESULT


def raw_completion(prompt: str, max_tokens: int = 1500) -> str:
    provider_name = cfg("ai_provider", "disabled").lower()
    if provider_name in ("disabled", "ml"):
        raise RuntimeError(
            "No LLM provider available for raw_completion "
            "(ai_provider is 'disabled' or 'ml')"
        )

    provider = _get_provider(provider_name)
    if not provider or not provider.is_available():
        raise RuntimeError(
            f"Provider '{provider_name}' is not available — check API key in .env"
        )

    # ── Claude ────────────────────────────────────────────────────────────
    if provider_name == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=provider.api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    # ── Gemini ────────────────────────────────────────────────────────────
    if provider_name == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=provider.api_key)
        model_name = getattr(provider, "model", None) or "gemini-2.0-flash"
        model_obj = genai.GenerativeModel(model_name)
        resp = model_obj.generate_content(prompt)
        return resp.text.strip()

    # ── OpenAI-compatible: openai / deepseek / grok / copilot ────────────
    # DeepSeek and Grok use the OpenAI SDK but point to different base URLs.
    # Copilot sets base_url via its constructor from AZURE_ENDPOINT.
    # OpenAI uses base_url=None which defaults to api.openai.com.
    if provider_name in ("openai", "deepseek", "grok", "copilot"):
        import openai
        _base_urls = {
            "deepseek": "https://api.deepseek.com",
            "grok":     "https://api.x.ai/v1",
        }
        _models = {
            "openai":   "gpt-4o-mini",
            "deepseek": "deepseek-chat",
            "grok":     "grok-2-latest",
            "copilot":  "gpt-4o",
        }
        client = openai.OpenAI(
            api_key=provider.api_key,
            base_url=getattr(provider, "base_url", None) or _base_urls.get(provider_name),
        )
        model = getattr(provider, "model", None) or _models.get(provider_name, "gpt-4o-mini")
        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip()

    raise RuntimeError(f"raw_completion not implemented for provider: {provider_name}")


def is_ai_available() -> bool:
    """
    Returns True if a non-ML LLM provider is configured and has an API key.
    Used by evolution_tracker.py to decide whether to attempt proposal generation.
    """
    provider_name = cfg("ai_provider", "disabled").lower()
    if provider_name in ("disabled", "ml"):
        return False
    provider = _get_provider(provider_name)
    return bool(provider and provider.is_available())