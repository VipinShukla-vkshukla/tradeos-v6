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
