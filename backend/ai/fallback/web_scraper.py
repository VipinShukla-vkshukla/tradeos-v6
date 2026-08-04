"""
TradeOS v7 — Web Scraping Fallback
Scrapes NSE/BSE/Moneycontrol for news sentiment.
Free, no API key needed.
"""
import re, time, requests
from datetime import date
from loguru import logger

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def scrape_nse_announcements(symbol: str) -> list[dict]:
    """Fetch corporate filings from NSE for a symbol."""
    results = []
    try:
        url = f"https://www.nseindia.com/api/corp-info?symbol={symbol}&type=announcements"
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=5)
        resp = session.get(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("data", [])[:5]:
                results.append({
                    "source": "NSE",
                    "headline": item.get("desc", ""),
                    "date": item.get("an_dt", ""),
                    "type": item.get("attchmntType", ""),
                })
    except Exception as e:
        logger.debug(f"NSE announcement scrape failed for {symbol}: {e}")
    return results


def scrape_moneycontrol_news(symbol: str) -> list[dict]:
    """Scrape Moneycontrol news headlines for a symbol."""
    results = []
    try:
        url = f"https://www.moneycontrol.com/stocks/cptmarket/compsearchnew.php?search_data={symbol}&cid=&mbsearch_str=&type_search=News"
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.select("li.clearfix a")[:5]:
                results.append({
                    "source": "Moneycontrol",
                    "headline": a.get_text(strip=True),
                    "url": a.get("href", ""),
                })
    except Exception as e:
        logger.debug(f"Moneycontrol scrape failed for {symbol}: {e}")
    return results


def get_news_for_symbol(symbol: str) -> list[dict]:
    """Aggregate news from all free sources."""
    news = []
    news.extend(scrape_nse_announcements(symbol))
    news.extend(scrape_moneycontrol_news(symbol))
    return news[:10]
