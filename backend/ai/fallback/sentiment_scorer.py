"""
TradeOS v7 — Rule-Based Sentiment Scorer
Scores news headlines without any AI/LLM.
Free, deterministic.
"""
import re

POSITIVE_WORDS = {
    "profit", "growth", "record", "beat", "strong", "bullish", "buy", "upgrade",
    "outperform", "rally", "surge", "gain", "positive", "expansion", "order", "win",
    "acquisition", "dividend", "bonus", "buyback", "recommended", "overweight",
}
NEGATIVE_WORDS = {
    "loss", "fall", "decline", "weak", "bearish", "sell", "downgrade", "underperform",
    "crash", "drop", "miss", "negative", "concern", "delay", "fraud", "probe",
    "investigation", "penalty", "fine", "default", "debt", "downward",
}
RISK_WORDS = {
    "results", "earnings", "quarter", "dividend", "board meeting", "agm",
    "fund raising", "rights issue", "merger", "acquisition", "delisting",
}


def score_headline(headline: str) -> dict:
    words = re.findall(r"\w+", headline.lower())
    word_set = set(words)
    pos = len(word_set & POSITIVE_WORDS)
    neg = len(word_set & NEGATIVE_WORDS)
    risk_evt = bool(word_set & RISK_WORDS)
    if pos > neg:
        sentiment = "POSITIVE"
        score = min(1.0, pos * 0.2)
    elif neg > pos:
        sentiment = "NEGATIVE"
        score = min(1.0, neg * 0.2)
    else:
        sentiment = "NEUTRAL"
        score = 0.0
    return {"sentiment": sentiment, "score": score, "has_risk_event": risk_evt, "headline": headline}


def score_news_list(news_items: list[dict]) -> dict:
    if not news_items:
        return {"overall": "NEUTRAL", "score": 0.0, "risk_events": False, "count": 0}
    scores = [score_headline(n.get("headline", "")) for n in news_items]
    pos = sum(1 for s in scores if s["sentiment"] == "POSITIVE")
    neg = sum(1 for s in scores if s["sentiment"] == "NEGATIVE")
    risk = any(s["has_risk_event"] for s in scores)
    net = pos - neg
    if net > 1:
        overall = "POSITIVE"
    elif net < -1:
        overall = "NEGATIVE"
    else:
        overall = "NEUTRAL"
    return {"overall": overall, "score": net / max(len(scores), 1),
            "risk_events": risk, "count": len(scores), "details": scores}
