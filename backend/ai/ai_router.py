"""
TradeOS v7 — AI Router
Routes to configured provider with automatic fallback chain.
Switch provider from frontend Settings — no code changes needed.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import cfg, cfg_bool, AI_KEYS, AZURE_ENDPOINT, AZURE_DEPLOYMENT
from ai.providers.base_provider import ConvictionResult, UNKNOWN_RESULT


def _get_provider(name: str):
    from ai.providers.claude_provider   import ClaudeProvider
    from ai.providers.openai_provider   import OpenAIProvider
    from ai.providers.gemini_provider   import GeminiProvider
    from ai.providers.deepseek_provider import DeepSeekProvider
    from ai.providers.grok_provider     import GrokProvider
    from ai.providers.copilot_provider  import CopilotProvider
    from ai.providers.openrouter_provider import OpenRouterProvider
    from ai.providers.ml_provider       import MLProvider

    providers = {
        "claude":    lambda: ClaudeProvider(AI_KEYS["claude"]),
        "openai":    lambda: OpenAIProvider(AI_KEYS["openai"]),
        "gemini":    lambda: GeminiProvider(AI_KEYS["gemini"]),
        "deepseek":  lambda: DeepSeekProvider(AI_KEYS["deepseek"]),
        "grok":      lambda: GrokProvider(AI_KEYS["grok"]),
        "copilot":   lambda: CopilotProvider(AI_KEYS["copilot"], AZURE_ENDPOINT, AZURE_DEPLOYMENT),
        "openrouter":lambda: OpenRouterProvider(AI_KEYS["openrouter"]),
        "ml":        lambda: MLProvider(),
    }
    factory = providers.get(name)
    return factory() if factory else None


def _provider_chain() -> list[str]:
    """
    Ordered LLM provider chain: primary provider (from system_config.ai_provider)
    followed by ordered fallbacks (from system_config.ai_provider_fallback,
    comma-separated, defaults to "openrouter"). ML and disabled are skipped
    here — ML is handled as a separate fallback after all LLM providers fail.
    """
    primary = cfg("ai_provider", "disabled").lower()
    fallback_raw = cfg("ai_provider_fallback", "openrouter")
    fallback = [x.strip() for x in fallback_raw.split(",") if x.strip()]
    chain = []
    if primary != "disabled":
        chain.append(primary)
    chain.extend(p for p in fallback if p not in chain)
    return chain


def analyze(stock_data: dict, context: dict) -> ConvictionResult:
    """
    Route to configured provider with an ordered LLM fallback chain, then
    ML → scraping → UNKNOWN. Provider set in system_config.ai_provider;
    fallbacks set in system_config.ai_provider_fallback.
    """
    provider_name = cfg("ai_provider", "disabled").lower()

    if provider_name == "disabled":
        return UNKNOWN_RESULT

    # Try LLM providers in order (primary → fallbacks). Auto-switches on error
    # or UNKNOWN so a single exhausted/dead provider never kills the pipeline.
    for name in _provider_chain():
        if name == "ml":
            continue
        provider = _get_provider(name)
        if provider and provider.is_available():
            result = provider.analyze_signal(stock_data, context)
            if result.conviction != "UNKNOWN":
                model_tag = getattr(result, "model", name)
                result.provider = f"{name}:{model_tag}"
                return result
            logger.warning(f"LLM provider {name} returned UNKNOWN — trying next")

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


def raw_completion(prompt: str, max_tokens: int = 1500,
                   call_site: str = "unknown", framework: str | None = None) -> str:
    """
    `call_site`/`framework` exist only for `usage_tracker.log_usage()`'s
    sake — a label naming which caller this is, so the token/cost
    visibility built 29-Aug-2026 can attribute spend correctly. Every
    caller should pass its own name; a caller that doesn't gets logged as
    "unknown" rather than crashing, since this is observability, not a
    contract.

    Tries LLM providers in the configured chain (primary → fallbacks) and
    returns the first successful completion. Auto-switches to the next
    provider if one errors out.
    """
    provider_name = cfg("ai_provider", "disabled").lower()
    if provider_name in ("disabled", "ml"):
        raise RuntimeError(
            "No LLM provider available for raw_completion "
            "(ai_provider is 'disabled' or 'ml')"
        )

    last_error = None
    for name in _provider_chain():
        if name == "ml":
            continue
        provider = _get_provider(name)
        if not provider or not provider.is_available():
            continue
        try:
            return _raw_completion_for(name, provider, prompt, max_tokens,
                                       call_site, framework)
        except Exception as e:
            last_error = e
            logger.warning(f"raw_completion provider {name} failed — trying next: {e}")

    if last_error:
        raise RuntimeError(
            f"All LLM providers in chain failed — last error: {last_error}"
        )
    raise RuntimeError("No LLM provider available in chain")


def _raw_completion_for(name: str, provider, prompt: str, max_tokens: int,
                        call_site: str, framework: str | None) -> str:
    """Single-attempt completion for one OpenAI/Anthropic/Google provider."""
    from ai.usage_tracker import log_usage

    # ── Claude ────────────────────────────────────────────────────────────
    if name == "claude":
        import anthropic
        client = anthropic.Anthropic(api_key=provider.api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage(call_site, "claude", "claude-sonnet-4-20250514", msg, framework)
        return msg.content[0].text.strip()

    # ── Gemini ────────────────────────────────────────────────────────────
    if name == "gemini":
        import google.generativeai as genai
        genai.configure(api_key=provider.api_key)
        model_name = getattr(provider, "model", None) or "gemini-2.0-flash"
        model_obj = genai.GenerativeModel(model_name)
        resp = model_obj.generate_content(prompt)
        log_usage(call_site, "gemini", model_name, resp, framework)
        return resp.text.strip()

    # ── OpenAI-compatible: openai / deepseek / grok / copilot / openrouter ─
    # DeepSeek and Grok use the OpenAI SDK but point to different base URLs.
    # Copilot sets base_url via its constructor from AZURE_ENDPOINT.
    # OpenAI uses base_url=None which defaults to api.openai.com.
    # OpenRouter uses base_url https://openrouter.ai/api/v1.
    if name in ("openai", "deepseek", "grok", "copilot", "openrouter"):
        import openai
        _base_urls = {
            "deepseek":  "https://api.deepseek.com",
            "grok":      "https://api.x.ai/v1",
            "openrouter":"https://openrouter.ai/api/v1",
        }
        _models = {
            "openai":   "gpt-4o-mini",
            "deepseek": "deepseek-v4-flash",
            "grok":     "grok-2-latest",
            "copilot":  "gpt-4o",
            "openrouter": "deepseek/deepseek-chat",
        }
        client = openai.OpenAI(
            api_key=provider.api_key,
            base_url=getattr(provider, "base_url", None) or _base_urls.get(name),
        )
        model = getattr(provider, "model", None) or _models.get(name, "gpt-4o-mini")
        extra: dict = {}
        if name == "deepseek" and not cfg_bool("ai_thinking_enabled", False):
            extra["extra_body"] = {"thinking": {"type": "disabled"}}

        resp = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
        log_usage(call_site, name, model, resp, framework)
        choice = resp.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            used = getattr(getattr(resp, "usage", None), "completion_tokens", "?")
            logger.warning(
                f"  {name}/{model} stopped at the token limit "
                f"(finish_reason=length, completion_tokens={used}, "
                f"max_tokens={max_tokens}) — the response is incomplete. "
                f"Raising max_tokens only helps if the model honours it; "
                f"otherwise the request has to be split."
            )
        return (choice.message.content or "").strip()

    raise RuntimeError(f"raw_completion not implemented for provider: {name}")


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