"""
TradeOS v7 — Phase 2: Market News Ingestion
Runs as step 01 — first step of evening pipeline, before fetch_chartink.
Scrapes 8 sources to populate market_news table so
market_intelligence_engine.py has structured news context.

Sources (all free, no API key, all URLs verified 2026-03-29):
    1. NSE circulars (Google News) — ASM/GSM/FO/index changes via targeted query
    2. RBI press release RSS       — rate decisions, NBFC directives, liquidity ops
    3. NSE bulk + block deals      — institutional buys/sells (session-gated, best-effort)
    4. Economic Times RSS          — PLI, capex, promoter commentary, analyst upgrades
    5. Google News RSS             — sector-level queries covering all NSE 500 sectors
    6. SEBI RSS                    — F&O changes, surveillance, investor protection
    7. Investing.com Commodities   — crude oil, metals, agri — directly impacts NSE sectors
    8. Investing.com Economy/Forex — Fed/FOMC, DXY, global macro affecting FII flows

International coverage rationale:
    - Crude oil (news_11): OMCs, paint, tyre, aviation stocks move 2-5% on big crude moves
    - Fed/FOMC (news_14): FII outflows trigger Nifty gap-opens; IT sector mirrors Nasdaq
    - Forex/DXY (news_1):  Rupee depreciation affects importers vs exporters asymmetrically
    - China macro:         Metals sector (steel, aluminium, copper) strongly correlated

Dedup strategy: upsert on (source, headline) — date is NOT part of the key.
Same article never re-inserts regardless of when the script runs.
Required one-time migration:
    ALTER TABLE market_news DROP CONSTRAINT IF EXISTS market_news_news_date_source_headline_key;
    ALTER TABLE market_news ADD CONSTRAINT market_news_source_headline_key UNIQUE (source, headline);

Non-fatal — pipeline continues if any or all sources fail.
No BeautifulSoup dependency — uses stdlib xml.etree only.
"""
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from datetime import date

import requests
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg_bool

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

# ── Sector Google News queries (Source 5) ─────────────────────────────────
SECTOR_QUERIES = [
    "India banking NBFC financial sector NSE",
    "India industrials manufacturing capital goods NSE",
    "India metals mining steel aluminium NSE",
    "India IT technology software exports NSE",
    "India energy oil gas refining power NSE",
    "USFDA warning letter India pharma NSE import alert",
]

# ── NSE circular Google News queries (Source 1) ───────────────────────────
# NSE's own API (/api/latest-circular, /api/corporateAnnouncementsEquity)
# requires a live Akamai-validated browser session and returns 404 without it.
# Google News aggregates ET/Mint/MoneyControl coverage within ~1h of NSE release.
NSE_CIRCULAR_QUERIES = [
    "NSE India ASM GSM surveillance list 2026",
    "NSE India F&O ban futures options ban 2026",
    "NSE circular margin hike index constituent change 2026",
]


# ── Helpers ────────────────────────────────────────────────────────────────

def _parse_rss_date(raw: str) -> str | None:
    """
    Parse RSS pubDate → ISO date string (YYYY-MM-DD).
    Returns None if unparseable so callers fall back to today_ist().
    """
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d %b %Y %H:%M:%S +0000",
        "%Y-%m-%d %H:%M:%S",      # investing.com format: "2026-03-29 13:00:32"
    ):
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _is_recent(date_str: str | None, days: int = 3) -> bool:
    """Return True if date_str is within the last N days or is None (unknown date, keep it)."""
    if date_str is None:
        return True
    try:
        item_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        return (today_ist() - item_date).days <= days
    except ValueError:
        return True

def _strip_cdata(xml_text: str | bytes) -> str:
    if isinstance(xml_text, bytes):
        xml_text = xml_text.lstrip(b"\xef\xbb\xbf").decode("utf-8", errors="replace")
    text = xml_text.lstrip("\ufeff")
    # Replace CDATA blocks with escaped plain text so inner HTML doesn't break XML
    def _escape_cdata(m: re.Match) -> str:
        inner = m.group(1)
        inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return inner
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", _escape_cdata, text, flags=re.DOTALL)
    return text


def _parse_rss(raw_text: str) -> ET.Element | None:
    """Parse RSS XML safely, returns root element or None on failure."""
    try:
        cleaned = _strip_cdata(raw_text)
        return ET.fromstring(cleaned)
    except ET.ParseError as e:
        logger.warning(f"RSS XML parse failed: {e}")
        return None


def _nse_session() -> requests.Session:
    """
    Shared NSE session factory — warms Akamai cookies via homepage hit.
    sleep(3) is intentional: Akamai needs time to validate the session.
    NSE returns empty/500 (not 401) without valid cookies.
    """
    session = requests.Session()
    try:
        resp = session.get("https://www.nseindia.com", headers=HEADERS, timeout=15)
        session.cookies.update(resp.cookies)
        time.sleep(3)
    except Exception as e:
        logger.warning(f"NSE session warmup failed (continuing): {e}")
    return session


# ── Source 1: NSE Circulars via Google News ───────────────────────────────

def fetch_nse_circulars() -> list[dict]:
    """
    NSE regulatory circulars via targeted Google News RSS queries.
    Replaces direct NSE API call which requires live browser session.
    """
    results = []
    for query in NSE_CIRCULAR_QUERIES:
        try:
            encoded = query.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"NSE circular query HTTP {resp.status_code}: {query}")
                continue
            root = _parse_rss(resp.content)
            if root is None:
                continue
            count = 0
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
                if not title or len(title) < 15:
                    continue
                impact = _classify_nse_circular(title)
                results.append({
                    "headline":    title[:500],
                    "source":      "NSE_CIRCULAR",
                    "category":    "DOMESTIC_REGULATORY",
                    "impact_type": impact,
                    "magnitude":   _classify_nse_circular_magnitude(impact),
                    "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                    "raw_url":     item.findtext("link") or "",
                })
                count += 1
                if count >= 3:
                    break
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"NSE circular query failed '{query}': {e}")
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
    if any(k in h for k in ("TRADING HALT", "SUSPENSION", "SUSPENDED", "HALT IN TRADING")):
        return "TRADING_HALT"
    if any(k in h for k in ("DELISTING", "DELIST", "VOLUNTARY DELISTING", "COMPULSORY DELISTING")):
        return "DELISTING"
    return "REGULATORY"


def _classify_nse_circular_magnitude(impact_type: str) -> str:
    if impact_type in ("TRADING_HALT", "DELISTING"):
        return "HIGH"
    if impact_type in ("ASM_CHANGE", "FO_CHANGE"):
        return "MEDIUM"
    return "LOW"


# ── Source 2: RBI RSS ──────────────────────────────────────────────────────

def fetch_rbi_rss() -> list[dict]:
    """
    RBI press releases RSS.
    URL verified 2026-03-29: https://www.rbi.org.in/pressreleases_rss.xml
    Quirks handled by _strip_cdata(): UTF-8 BOM prefix + CDATA wrappers.
    """
    results = []
    try:
        resp = requests.get(
            "https://www.rbi.org.in/pressreleases_rss.xml",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"RBI RSS returned HTTP {resp.status_code}")
            return results
        root = _parse_rss(resp.content.decode("utf-8-sig", errors="replace"))
        if root is None:
            return results
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            results.append({
                "headline":    title[:500],
                "source":      "RBI",
                "category":    "CENTRAL_BANK",
                "impact_type": "RATE_DECISION" if any(
                    k in title.upper() for k in ("REPO", "RATE", "POLICY", "MPC", "CRR", "SLR")
                ) else "POLICY",
                "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                "raw_url":     item.findtext("link") or "",
            })
            if len(results) >= 8:
                break
    except Exception as e:
        logger.warning(f"RBI RSS fetch failed (non-fatal): {e}")
    return results


# ── Source 3: NSE Bulk + Block Deals ──────────────────────────────────────

def fetch_nse_bulk_deals() -> list[dict]:
    """
    NSE bulk/block deals — institutional activity.
    Endpoint: snapshot-capital-market-largedeal (as of 2026-03-29)
    Keys: BULK_DEALS_DATA, BLOCK_DEALS_DATA
    """
    results = []
    try:
        session = _nse_session()
        try:
            resp = session.get(
                "https://www.nseindia.com/api/snapshot-capital-market-largedeal",
                headers={
                    **HEADERS,
                    "Referer": "https://www.nseindia.com/report-detail/display-bulk-and-block-deals",
                },
                timeout=15,
            )
            if resp.status_code != 200 or not resp.content:
                logger.warning(f"NSE bulk/block deals HTTP {resp.status_code} — endpoint may have changed")
            else:
                data = resp.json()
                structured = []
                for deal_type, key in (("bulk", "BULK_DEALS_DATA"), ("block", "BLOCK_DEALS_DATA")):
                    # NO [:15] SLICE.
                    #
                    # It used to keep the first fifteen deals per type. NSE
                    # reports well over thirty on a busy session, and the tail is
                    # where mid-cap accumulation lives — precisely the population
                    # an accumulation-confirmed engine exists to find. The cap
                    # was invisible: the alert looked complete because fifteen
                    # deals is more than anyone reads.
                    #
                    # Alert volume is bounded downstream by relevance, not here
                    # by an arbitrary count.
                    for d in (data.get(key) or []):
                        symbol = str(d.get("symbol") or "")
                        entity = str(d.get("clientName") or "")
                        side   = str(d.get("buySell") or "")
                        qty    = d.get("qty") or 0
                        price  = d.get("watp") or 0
                        if not symbol or not entity:
                            continue

                        # KEEP THE NUMBERS.
                        #
                        # The headline below formats side, quantity and price
                        # into a sentence, which is right for an alert and
                        # useless for an engine — "net institutional buying over
                        # twenty sessions" cannot be asked of prose. Same fields,
                        # kept as numbers, written alongside. The headline row is
                        # unchanged, so alerts behave exactly as before.
                        try:
                            q = float(qty or 0)
                            p = float(price or 0)
                            structured.append({
                                "deal_date":   (d.get("date") or today_ist().isoformat())[:10],
                                "symbol":      symbol.upper().strip(),
                                "deal_type":   deal_type.upper(),
                                "client_name": entity.strip()[:200],
                                "side":        "BUY" if side.upper().startswith("B") else "SELL",
                                "quantity":    q,
                                "price":       p or None,
                                "value_cr":    round(q * p / 1e7, 4) if (q and p) else None,
                            })
                        except (TypeError, ValueError):
                            pass

                        results.append({
                            "headline":       f"{entity} {side.upper()} {symbol}: {int(qty):,} shares @ ₹{float(price):.0f}"[:500],
                            "source":         "NSE_BULK_DEAL",
                            "category":       "CORPORATE",
                            "impact_type":    "BULK_DEAL",
                            "news_date":      None,
                            "parsed_symbols": [symbol],
                            "raw_url":        "",
                        })
                _persist_bulk_deals(structured)
        except Exception as e:
            logger.warning(f"NSE bulk/block deals fetch failed: {e}")
    except Exception as e:
        logger.warning(f"NSE bulk/block deals session failed (non-fatal): {e}")
    return results


def _persist_bulk_deals(rows: list[dict]) -> int:
    """
    Write structured deals, ignoring ones already recorded.

    Upsert on the natural key rather than insert, because the evening pipeline
    can legitimately run twice and this table is summed. A double-counted
    17-million-share block would not look like an error — it would look like
    twice the institutional conviction, which is the worst possible way for a
    duplicate to present itself.

    Non-fatal throughout: the headline row is the alert path and must never fail
    because a structured write did.
    """
    if not rows:
        return 0
    if not cfg_bool("bulk_deal_capture_enabled", False):
        return 0
    try:
        from config import get_supabase
        get_supabase().table("bulk_deals").upsert(
            rows, on_conflict="deal_date,symbol,client_name,side,quantity"
        ).execute()
        logger.info(f"  bulk_deals: captured {len(rows)} structured deal(s)")
        return len(rows)
    except Exception as e:
        # A missing table means migration 038 has not been applied. Say which,
        # rather than logging a bare PostgREST error nobody can act on.
        logger.warning(f"  bulk_deals capture skipped ({e}) — "
                       f"apply migration 038 if this persists")
        return 0


# ── Source 4: Economic Times Markets RSS ──────────────────────────────────

def fetch_et_rss() -> list[dict]:
    """ET Markets RSS. URL verified working in production logs."""
    results = []
    try:
        resp = requests.get(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            headers=HEADERS, timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"ET RSS returned HTTP {resp.status_code}")
            return results
        root = _parse_rss(resp.text)
        if root is None:
            return results
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            results.append({
                "headline":    title[:500],
                "source":      "ET_MARKETS",
                "category":    _classify_et_category(title),
                "impact_type": _classify_et_impact(title),
                "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                "raw_url":     item.findtext("link") or "",
            })
            if len(results) >= 12:
                break
    except Exception as e:
        logger.warning(f"ET RSS fetch failed (non-fatal): {e}")
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
    """Google News RSS — aggregates ET, Mint, MoneyControl, BS across NSE sectors."""
    results = []
    for query in SECTOR_QUERIES:
        try:
            encoded = query.replace(" ", "+")
            url = f"https://news.google.com/rss/search?q={encoded}&hl=en-IN&gl=IN&ceid=IN:en"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            root = _parse_rss(resp.text)
            if root is None:
                continue
            count = 0
            for item in root.iter("item"):
                title = re.sub(r"\s*-\s*[^-]+$", "", (item.findtext("title") or "").strip()).strip()
                if not title or len(title) < 15:
                    continue
                results.append({
                    "headline":    title[:500],
                    "source":      "GOOGLE_NEWS",
                    "category":    "DOMESTIC_POLICY",
                    "impact_type": "MACRO",
                    "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                    "raw_url":     item.findtext("link") or "",
                })
                count += 1
                if count >= 3:
                    break
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"Google News sector RSS failed '{query}': {e}")
    return results


# ── Source 6: SEBI RSS ─────────────────────────────────────────────────────

def fetch_sebi_circulars() -> list[dict]:
    """
    SEBI official RSS.
    URL verified 2026-03-29: https://www.sebi.gov.in/sebirss.xml
    Covers circulars, orders, press releases. No BeautifulSoup needed.
    """
    results = []
    try:
        resp = requests.get(
            "https://www.sebi.gov.in/sebirss.xml",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"SEBI RSS returned HTTP {resp.status_code}")
            return results
        root = _parse_rss(resp.text)
        if root is None:
            return results
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title or len(title) < 10:
                continue
            results.append({
                "headline":    title[:500],
                "source":      "SEBI",
                "category":    "DOMESTIC_REGULATORY",
                "impact_type": "REGULATORY",
                "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                "raw_url":     item.findtext("link") or "",
            })
            if len(results) >= 8:
                break
    except Exception as e:
        logger.warning(f"SEBI RSS fetch failed (non-fatal): {e}")
    return results


# ── Source 7: Investing.com Commodities RSS ────────────────────────────────

def fetch_investing_commodities() -> list[dict]:
    """
    Investing.com commodities news RSS.
    URL verified 2026-03-29: https://investing.com/rss/news_11.rss
    Covers crude, Brent, gold, base metals, agri.
    NSE sector impact: OMCs, paint, tyre, aviation (crude); mining, jewellery (metals).
    Note: Reuters stopped RSS in 2020 — investing.com carries Reuters wire via licensing.
    """
    results = []
    try:
        resp = requests.get(
            "https://investing.com/rss/news_11.rss",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            logger.warning(f"Investing.com commodities RSS HTTP {resp.status_code}")
            return results
        root = _parse_rss(resp.text)
        if root is None:
            return results
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            results.append({
                "headline":    title[:500],
                "source":      "INVESTING_COMMODITIES",
                "category":    "INTERNATIONAL",
                "impact_type": _classify_commodity_impact(title),
                "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                "raw_url":     item.findtext("link") or "",
            })
            if len(results) >= 10:
                break
    except Exception as e:
        logger.warning(f"Investing.com commodities RSS failed (non-fatal): {e}")
    return results


def _classify_commodity_impact(title: str) -> str:
    t = title.upper()
    if any(k in t for k in ("CRUDE", "OIL", "BRENT", "WTI", "OPEC")):
        return "CRUDE_MOVE"
    if any(k in t for k in ("GOLD", "SILVER", "PRECIOUS")):
        return "PRECIOUS_METALS"
    if any(k in t for k in ("STEEL", "ALUMINIUM", "COPPER", "IRON", "ZINC", "NICKEL")):
        return "BASE_METALS"
    return "COMMODITY"


# ── Source 8: Investing.com Economy + Forex RSS ────────────────────────────

def fetch_investing_macro() -> list[dict]:
    """
    Investing.com economy (news_14) + forex (news_1) RSS.
    Both URLs verified 2026-03-29.
    Economy: Fed/FOMC, US CPI, GDP, tariffs — drive FII flow into/out of India.
    Forex:   DXY, USD/INR — importers vs exporters diverge on rupee moves.
    Filtered via _is_macro_relevant() to only keep India-impacting headlines.
    """
    results = []
    feeds = [
        ("https://investing.com/rss/news_14.rss", "MACRO"),   # economy
        ("https://investing.com/rss/news_1.rss",  "FOREX"),   # forex / DXY
    ]
    for url, default_impact in feeds:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"Investing.com macro RSS HTTP {resp.status_code}: {url}")
                continue
            root = _parse_rss(resp.text)
            if root is None:
                continue
            count = 0
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                if not title or not _is_macro_relevant(title):
                    continue
                results.append({
                    "headline":    title[:500],
                    "source":      "INVESTING_MACRO",
                    "category":    "INTERNATIONAL",
                    "impact_type": _classify_macro_impact(title, default_impact),
                    "news_date":   _parse_rss_date(item.findtext("pubDate") or ""),
                    "raw_url":     item.findtext("link") or "",
                })
                count += 1
                if count >= 6:
                    break
        except Exception as e:
            logger.warning(f"Investing.com macro RSS failed {url} (non-fatal): {e}")
    return results


def _is_macro_relevant(title: str) -> bool:
    """Only keep headlines that materially move Indian markets."""
    t = title.upper()
    return any(k in t for k in (
        "FED", "FOMC", "RATE", "INFLATION", "CPI", "GDP", "TARIFF",
        "CHINA", "DOLLAR", "DXY", "USD", "RUPEE", "INR", "INTEREST",
        "RECESSION", "IMF", "WORLD BANK", "TREASURY", "YIELD",
        "OPEC", "CRUDE", "OIL", "EMERGING MARKET",
    ))


def _classify_macro_impact(title: str, default: str) -> str:
    t = title.upper()
    if any(k in t for k in ("FED", "FOMC", "INTEREST RATE", "RATE DECISION")):
        return "FED_DECISION"
    if any(k in t for k in ("CPI", "INFLATION", "PCE")):
        return "INFLATION_DATA"
    if any(k in t for k in ("GDP", "RECESSION", "GROWTH")):
        return "GROWTH_DATA"
    if any(k in t for k in ("TARIFF", "TRADE WAR", "SANCTION")):
        return "TRADE_POLICY"
    if any(k in t for k in ("DXY", "DOLLAR", "USD", "RUPEE", "INR")):
        return "FOREX"
    return default


# ── Write to market_news ───────────────────────────────────────────────────

def write_market_news(sb, items: list[dict], today_str: str) -> int:
    """
    Upsert to market_news on conflict key (source, headline).
    news_date uses actual RSS pubDate where available, today_ist() as fallback.
    """
    if not items:
        return 0

    rows = []
    seen: set[str] = set()
    for item in items:
        h = item.get("headline", "").strip()
        if not h or h in seen:
            continue
        if not _is_recent(item.get("news_date")):
            continue
        seen.add(h)
        rows.append({
            "news_date":      item.get("news_date") or today_str,
            "headline":       h,
            "source":         item.get("source", "UNKNOWN"),
            "category":       item.get("category", "DOMESTIC_POLICY"),
            "impact_type":    item.get("impact_type", "MACRO"),
            "magnitude":      item.get("magnitude") or None,
            "parsed_sectors": None,
            "parsed_symbols": item.get("parsed_symbols"),
            "raw_url":        item.get("raw_url") or None,
        })

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would write {len(rows)} market_news rows")
        for r in rows[:5]:
            logger.info(f"  [{r['source']}] {r['news_date']} | {r['headline'][:80]}")
        return len(rows)

    written = 0
    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        try:
            sb.table("market_news").upsert(
                batch, on_conflict="source,headline"
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
        ("NSE_CIRCULAR",          fetch_nse_circulars),
        ("RBI",                   fetch_rbi_rss),
        ("NSE_BULK_DEAL",         fetch_nse_bulk_deals),
        ("ET_MARKETS",            fetch_et_rss),
        ("GOOGLE_NEWS",           fetch_google_news_sectors),
        ("SEBI",                  fetch_sebi_circulars),
        ("INVESTING_COMMODITIES", fetch_investing_commodities),
        ("INVESTING_MACRO",       fetch_investing_macro),
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
    logger.info(f"  news_date written to Supabase: {today_str}")
    return {
        "status":  "ok",
        "scraped": len(all_items),
        "written": written,
        "sources": source_counts,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v7 — Market News Ingestion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())