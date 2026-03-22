"""
TradeOS v6 — Phase 2: Market News Ingestion
Runs as step 00a — first step of evening pipeline, before fetch_chartink.
Scrapes 6 free sources to populate market_news table so
market_intelligence_engine.py has structured news context.

Sources (all free, no API key):
    1. NSE latest circulars    — ASM/GSM/FO changes, index changes, margin hikes
    2. RBI press release RSS   — rate decisions, NBFC directives, liquidity ops
    3. NSE bulk + block deals  — institutional buys/sells with entity name + qty
    4. Economic Times RSS      — PLI, capex, promoter commentary, analyst upgrades
    5. Google News RSS         — sector-level queries covering all NSE 500 sectors
    6. SEBI circulars          — F&O changes, surveillance, investor protection

Non-fatal — pipeline continues if any or all sources fail.
Uses requests + BeautifulSoup (both already in requirements.txt).
"""
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Sector-level Google News queries — covers all major NSE 500 sectors
SECTOR_QUERIES = [
    "India banking NBFC financial sector NSE",
    "India industrials manufacturing capital goods NSE",
    "India metals mining steel aluminium NSE",
    "India IT technology software exports NSE",
    "India energy oil gas refining power NSE",
    # SG10: Dedicated pharma regulatory query — FDA/USFDA actions affect pharma stocks 3-8%
    # typically published in ET/Mint within 6-8h of FDA action, well within 6 PM pipeline window
    "USFDA warning letter India pharma NSE import alert",
]


# ── Source 1: NSE Circulars ────────────────────────────────────────────────

def fetch_nse_circulars() -> list[dict]:
    """NSE regulatory circulars — surveillance, margin, index, FO changes."""
    results = []
    try:
        session = requests.Session()
        # Warm NSE session to get cookies
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
        time.sleep(1)
        resp = session.get(
            "https://www.nseindia.com/api/latest-circular",
            headers=HEADERS, timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in (data.get("data") or [])[:10]:
                headline = str(item.get("subject") or item.get("desc") or "")
                if not headline:
                    continue
                results.append({
                    "headline":    headline[:500],
                    "source":      "NSE_CIRCULAR",
                    "category":    "DOMESTIC_REGULATORY",
                    "impact_type": _classify_nse_circular(headline),
                    "magnitude":   _classify_nse_circular_magnitude(_classify_nse_circular(headline)),
                    "raw_url":     item.get("url", ""),
                })
    except Exception as e:
        logger.debug(f"NSE circulars fetch failed (non-fatal): {e}")
    return results


def _classify_nse_circular(headline: str) -> str:
    h = headline.upper()
    if any(k in h for k in ("ASM", "GSM", "SURVEILLANCE", "SHORTTERM")):
        return "ASM_CHANGE"
    if any(k in h for k in ("MARGIN", "EXPOSURE", "VAR")):
        return "MARGIN_CHANGE"
    if any(k in h for k in ("BAN", "F&O", "FUTURES", "OPTIONS")):
        return "FO_CHANGE"
    if any(k in h for k in ("INDEX", "NIFTY", "CONSTITUENT")):
        return "INDEX_CHANGE"
    # SG9: Trading halt and delisting detection
    if any(k in h for k in ("TRADING HALT", "SUSPENSION", "SUSPENDED", "HALT IN TRADING")):
        return "TRADING_HALT"
    if any(k in h for k in ("DELISTING", "DELIST", "VOLUNTARY DELISTING", "COMPULSORY DELISTING")):
        return "DELISTING"
    return "REGULATORY"


def _classify_nse_circular_magnitude(impact_type: str) -> str:
    """SG9: TRADING_HALT and DELISTING are always HIGH magnitude — directly affect held positions."""
    if impact_type in ("TRADING_HALT", "DELISTING"):
        return "HIGH"
    if impact_type in ("ASM_CHANGE", "FO_CHANGE"):
        return "MEDIUM"
    return "LOW"


# ── Source 2: RBI RSS ──────────────────────────────────────────────────────

def fetch_rbi_rss() -> list[dict]:
    """RBI press releases — rate decisions, policy, NBFC directives."""
    results = []
    try:
        resp = requests.get(
            "https://rbi.org.in/rss",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                if not title:
                    continue
                results.append({
                    "headline":    title[:500],
                    "source":      "RBI",
                    "category":    "CENTRAL_BANK",
                    "impact_type": "RATE_DECISION" if any(
                        k in title.upper() for k in
                        ("REPO", "RATE", "POLICY", "MPC", "CRR", "SLR")
                    ) else "POLICY",
                    "raw_url":     item.findtext("link") or "",
                })
                if len(results) >= 8:
                    break
    except Exception as e:
        logger.debug(f"RBI RSS fetch failed (non-fatal): {e}")
    return results


# ── Source 3: NSE Bulk + Block Deals ──────────────────────────────────────

def fetch_nse_bulk_deals() -> list[dict]:
    """NSE bulk deals — institutional buys/sells with entity name + qty."""
    results = []
    try:
        today_str = str(today_ist())
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
        time.sleep(0.5)

        for deal_type in ("bulk-deals", "block-deals"):
            try:
                resp = session.get(
                    f"https://www.nseindia.com/api/{deal_type}",
                    headers=HEADERS, timeout=8
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                deals = data.get("data") or (data if isinstance(data, list) else [])
                for d in deals[:15]:
                    symbol  = str(d.get("symbol") or d.get("scrip_cd") or "")
                    entity  = str(d.get("clientName") or d.get("client") or "")
                    side    = str(d.get("buySell") or d.get("buy_sell") or "")
                    qty     = d.get("qty") or d.get("quantity") or 0
                    price   = d.get("price") or 0
                    if not symbol or not entity:
                        continue
                    headline = (
                        f"{entity} {side.upper()} {symbol}: "
                        f"{int(qty):,} shares @ ₹{float(price):.0f}"
                    )
                    results.append({
                        "headline":    headline[:500],
                        "source":      "NSE_BULK_DEAL",
                        "category":    "CORPORATE",
                        "impact_type": "BULK_DEAL",
                        "parsed_symbols": [symbol],
                        "raw_url":     "",
                    })
            except Exception as e:
                logger.debug(f"NSE {deal_type} fetch failed: {e}")
    except Exception as e:
        logger.debug(f"NSE bulk/block deals fetch failed (non-fatal): {e}")
    return results


# ── Source 4: Economic Times Markets RSS ──────────────────────────────────

def fetch_et_rss() -> list[dict]:
    """ET Markets RSS — policy, PLI, promoter commentary, analyst calls."""
    results = []
    try:
        resp = requests.get(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                if not title:
                    continue
                results.append({
                    "headline":    title[:500],
                    "source":      "ET_MARKETS",
                    "category":    _classify_et_category(title),
                    "impact_type": _classify_et_impact(title),
                    "raw_url":     item.findtext("link") or "",
                })
                if len(results) >= 12:
                    break
    except Exception as e:
        logger.debug(f"ET RSS fetch failed (non-fatal): {e}")
    return results


def _classify_et_category(title: str) -> str:
    t = title.upper()
    if any(k in t for k in ("RBI", "SEBI", "GOVT", "BUDGET", "PLI", "POLICY")):
        return "DOMESTIC_POLICY"
    if any(k in t for k in ("FED", "FOMC", "ECB", "IMF", "CHINA", "US ", "GLOBAL")):
        return "INTERNATIONAL"
    return "CORPORATE"


def _classify_et_impact(title: str) -> str:
    t = title.upper()
    if any(k in t for k in ("RESULT", "QUARTER", "PROFIT", "REVENUE", "PAT", "EBITDA")):
        return "EARNINGS"
    if any(k in t for k in ("PLI", "SUBSIDY", "CAPEX", "ORDER", "CONTRACT", "WIN")):
        return "POLICY"
    if any(k in t for k in ("BUY", "UPGRADE", "TARGET", "OVERWEIGHT", "OUTPERFORM")):
        return "ANALYST_CALL"
    return "MACRO"


# ── Source 5: Google News RSS (sector-level) ───────────────────────────────

def fetch_google_news_sectors() -> list[dict]:
    """Google News RSS for each major sector — aggregates 200+ Indian financial sources."""
    results = []
    for query in SECTOR_QUERIES:
        try:
            encoded = query.replace(" ", "+")
            url = (
                f"https://news.google.com/rss/search"
                f"?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
            )
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.text)
            count = 0
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                # Clean Google News title format: "Headline - Source"
                title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
                if not title or len(title) < 15:
                    continue
                results.append({
                    "headline":    title[:500],
                    "source":      "GOOGLE_NEWS",
                    "category":    "DOMESTIC_POLICY",
                    "impact_type": "MACRO",
                    "raw_url":     item.findtext("link") or "",
                })
                count += 1
                if count >= 3:
                    break
            time.sleep(0.3)   # gentle rate limiting
        except Exception as e:
            logger.debug(f"Google News RSS fetch failed for '{query}' (non-fatal): {e}")
    return results


# ── Source 6: SEBI Circulars ───────────────────────────────────────────────

def fetch_sebi_circulars() -> list[dict]:
    """SEBI circulars — F&O, lot sizes, insider trading, investor protection."""
    results = []
    try:
        resp = requests.get(
            "https://www.sebi.gov.in/sebiweb/other/OtherAction.do?doRecent=yes&type=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.select("table tr")[:15]:
                cells = row.find_all("td")
                if len(cells) >= 2:
                    title = cells[-1].get_text(strip=True)
                    if title and len(title) > 10:
                        results.append({
                            "headline":    title[:500],
                            "source":      "SEBI",
                            "category":    "DOMESTIC_REGULATORY",
                            "impact_type": "REGULATORY",
                            "raw_url":     "",
                        })
                        if len(results) >= 6:
                            break
    except Exception as e:
        logger.debug(f"SEBI circulars fetch failed (non-fatal): {e}")
    return results


# ── Write to market_news ───────────────────────────────────────────────────

def write_market_news(sb, items: list[dict], today_str: str) -> int:
    """Upsert scraped items to market_news. Returns count written."""
    if not items:
        return 0

    rows = []
    seen = set()  # dedup by headline within this run
    for item in items:
        h = item.get("headline", "").strip()
        if not h or h in seen:
            continue
        seen.add(h)
        rows.append({
            "news_date":      today_str,
            "headline":       h,
            "source":         item.get("source", "UNKNOWN"),
            "category":       item.get("category", "DOMESTIC_POLICY"),
            "impact_type":    item.get("impact_type", "MACRO"),
            "parsed_sectors": None,       # AI derives in market_intelligence_engine
            "parsed_symbols": item.get("parsed_symbols"),
            "magnitude":      None,       # AI derives
            "raw_url":        item.get("raw_url") or None,
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(rows)} market_news rows")
        for r in rows[:5]:
            logger.info(f"  [{r['source']}] {r['headline'][:80]}")
        return len(rows)

    written = 0
    for i in range(0, len(rows), 100):
        batch = rows[i:i+100]
        try:
            # upsert with conflict on (news_date, source, headline)
            sb.table("market_news").upsert(
                batch, on_conflict="news_date,source,headline"
            ).execute()
            written += len(batch)
        except Exception as e:
            logger.warning(f"market_news batch write failed: {e}")
    return written


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_market_news skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Market News Ingestion starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb        = get_supabase()
    today_str = str(today_ist())

    all_items: list[dict] = []
    source_counts: dict[str, int] = {}

    sources = [
        ("NSE_CIRCULAR",   fetch_nse_circulars),
        ("RBI",            fetch_rbi_rss),
        ("NSE_BULK_DEAL",  fetch_nse_bulk_deals),
        ("ET_MARKETS",     fetch_et_rss),
        ("GOOGLE_NEWS",    fetch_google_news_sectors),
        ("SEBI",           fetch_sebi_circulars),
    ]

    for name, fn in sources:
        try:
            items = fn()
            source_counts[name] = len(items)
            all_items.extend(items)
            logger.info(f"  {name}: {len(items)} items")
        except Exception as e:
            source_counts[name] = 0
            logger.warning(f"  {name}: FAILED (non-fatal) — {e}")

    written = write_market_news(sb, all_items, today_str)

    logger.success(
        f"Market News done: {len(all_items)} scraped → {written} written | "
        f"Sources: {source_counts}"
    )
    return {
        "status":  "ok",
        "scraped": len(all_items),
        "written": written,
        "sources": source_counts,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Market News Ingestion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())
