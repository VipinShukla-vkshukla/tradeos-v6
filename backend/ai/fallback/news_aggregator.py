"""
TradeOS v7 — AI Fallback: News Aggregator
Combines multiple scraped sources for a stock.
Used when AI provider is unavailable or budget exceeded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import logger

from ai.fallback.web_scraper import scrape_stock_news
from ai.fallback.sentiment_scorer import score_sentiment, extract_key_events


def aggregate_news(symbol: str, sector: str = "") -> dict:
    """
    Aggregate news from multiple sources for a symbol.
    Returns: {sentiment, articles, key_events, confidence}
    """
    try:
        articles = scrape_stock_news(symbol, sector=sector)
        if not articles:
            return {
                "sentiment":   "NEUTRAL",
                "articles":    [],
                "key_events":  [],
                "confidence":  0.0,
                "source":      "scraping",
            }

        sentiment  = score_sentiment(articles)
        key_events = extract_key_events(articles)

        # Confidence based on article count
        confidence = min(0.8, len(articles) * 0.15)

        return {
            "sentiment":   sentiment,
            "articles":    articles[:5],    # top 5 headlines
            "key_events":  key_events,
            "confidence":  confidence,
            "source":      "scraping",
        }
    except Exception as e:
        logger.debug(f"News aggregation failed for {symbol}: {e}")
        return {
            "sentiment":  "NEUTRAL",
            "articles":   [],
            "key_events": [],
            "confidence": 0.0,
            "source":     "error",
        }
