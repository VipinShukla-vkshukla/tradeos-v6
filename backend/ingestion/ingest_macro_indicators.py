"""
TradeOS v6 — Phase 2: Macro Indicators Ingestion (SG5)
Runs as step 00b — after 00a_market_news, before fetch_chartink.

WHY THIS EXISTS:
market_intelligence_engine currently has narrative context (ET RSS headlines)
but no structured macro data. AI cannot quantify risk when it knows "RBI
held rates" vs "RBI held rates at 6.5% while CPI is at 6.2% above 6% target
with IIP contracting -1.2%" — the second is a risk-sizing statement, not
just a fact. This script supplies that structured context.

SCHEDULE: Weekly is sufficient — CPI/GDP are monthly releases, IIP is monthly.
Run daily (harmless, upsert pattern) but meaningful new data arrives ~monthly.

SOURCES (all free, no API key):
  1. RBI DBIE API     — CPI YoY, WPI YoY, repo rate, reverse repo
  2. MOSPI (via HTML) — GDP QoQ/YoY, IIP YoY growth
  3. Yahoo Finance    — US 10-yr Treasury yield (TNX) as fallback for SG1
                      — Silver price (SI=F) as fallback for SG2

Non-fatal throughout — pipeline continues if any source fails.
"""
import os
import re
import sys
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# RBI DBIE series codes (free REST API)
RBI_DBIE_BASE = "https://dbie.rbi.org.in/DBIE/dbie.rbi?site=publications"
RBI_SERIES = {
    "REPO_RATE":    "https://dbie.rbi.org.in/api/series/S782",     # Repo rate
    "REV_REPO":     "https://dbie.rbi.org.in/api/series/S784",     # Reverse repo
    "CRR":          "https://dbie.rbi.org.in/api/series/S786",     # CRR
}

# Yahoo Finance endpoints (free JSON, no auth)
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


# ── Source 1: RBI DBIE ────────────────────────────────────────────────────

def fetch_rbi_rates() -> list[dict]:
    """Fetch RBI policy rates from DBIE API."""
    results = []
    for name, url in RBI_SERIES.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            # DBIE returns {data: [{date: ..., value: ...}]}
            series_data = data.get("data") or data.get("series") or []
            if series_data:
                # Take most recent observation
                latest = series_data[-1] if isinstance(series_data, list) else series_data
                val = latest.get("value") or latest.get("obs_value")
                obs_date = latest.get("date") or latest.get("time_period")
                if val and obs_date:
                    results.append({
                        "indicator_name":  name,
                        "indicator_value": float(str(val).replace(",", "")),
                        "indicator_date":  str(obs_date)[:10],
                        "source":          "RBI_DBIE",
                        "release_date":    str(today_ist()),
                    })
        except Exception as e:
            logger.debug(f"RBI DBIE {name} failed (non-fatal): {e}")
    return results


# ── Source 2: MOSPI (GDP + IIP via gov.in RSS) ───────────────────────────

def fetch_mospi_data() -> list[dict]:
    """
    Attempt to fetch GDP/IIP from MOSPI press releases.
    These are published as PDFs/HTML — we parse the RSS for release dates
    and extract numbers from the headline text.
    """
    results = []
    try:
        import xml.etree.ElementTree as ET
        resp = requests.get(
            "https://www.mospi.gov.in/rss/releases",
            headers=HEADERS, timeout=12
        )
        if resp.status_code != 200:
            return results

        root = ET.fromstring(resp.text)
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            pub_date = item.findtext("pubDate") or ""

            # Extract GDP figure
            gdp_match = re.search(r"GDP.*?(\d+\.?\d*)\s*%", title, re.IGNORECASE)
            if gdp_match:
                results.append({
                    "indicator_name":  "GDP_YOY",
                    "indicator_value": float(gdp_match.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "MOSPI",
                    "release_date":    str(today_ist()),
                })

            # Extract IIP figure
            iip_match = re.search(r"IIP.*?([+-]?\d+\.?\d*)\s*%", title, re.IGNORECASE)
            if iip_match:
                results.append({
                    "indicator_name":  "IIP_YOY",
                    "indicator_value": float(iip_match.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "MOSPI",
                    "release_date":    str(today_ist()),
                })

            # Extract CPI from MOSPI (MoSPI also publishes CPI)
            cpi_match = re.search(r"CPI.*?(\d+\.?\d*)\s*%", title, re.IGNORECASE)
            if cpi_match:
                results.append({
                    "indicator_name":  "CPI_YOY",
                    "indicator_value": float(cpi_match.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "MOSPI",
                    "release_date":    str(today_ist()),
                })

            if len(results) >= 6:
                break
    except Exception as e:
        logger.debug(f"MOSPI fetch failed (non-fatal): {e}")
    return results


# ── Source 3: Yahoo Finance (US 10-yr + Silver — SG1/SG2 backup) ─────────

def fetch_yahoo_finance(symbol: str, indicator_name: str) -> dict | None:
    """
    Fetch latest price from Yahoo Finance.
    Used as backup for US 10-yr yield and silver if ingest_global_cues
    does not yet have them (pre-SG1/SG2 code update window).
    """
    try:
        url = f"{YAHOO_BASE}/{symbol}?interval=1d&range=2d"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        result = data.get("chart", {}).get("result", [{}])[0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        prev  = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price:
            change = ((price - prev) / prev * 100) if prev else 0
            return {
                "indicator_name":  indicator_name,
                "indicator_value": round(float(price), 4),
                "indicator_date":  str(today_ist()),
                "previous_value":  round(float(prev), 4) if prev else None,
                "change_bps":      round(change * 100, 1),  # bps for rates, pct*100 for prices
                "source":          "YAHOO_FINANCE",
                "release_date":    str(today_ist()),
            }
    except Exception as e:
        logger.debug(f"Yahoo Finance {symbol} failed (non-fatal): {e}")
    return None


# ── Source 4: ET RSS — CPI/WPI from headlines ────────────────────────────

def fetch_cpi_wpi_from_et() -> list[dict]:
    """
    Parse ET Markets RSS for CPI/WPI release data.
    ET publishes structured data releases within 30 mins of announcement.
    """
    results = []
    try:
        import xml.etree.ElementTree as ET
        resp = requests.get(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return results
        root = ET.fromstring(resp.text)
        for item in root.iter("item"):
            title = item.findtext("title") or ""

            # CPI: "India CPI inflation rises to 5.8% in November"
            cpi = re.search(r"CPI\s+(?:inflation|data).*?(\d+\.?\d*)\s*%", title, re.IGNORECASE)
            if cpi:
                results.append({
                    "indicator_name":  "CPI_YOY",
                    "indicator_value": float(cpi.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "ET_RSS",
                    "release_date":    str(today_ist()),
                })

            # WPI
            wpi = re.search(r"WPI\s+(?:inflation|data).*?([+-]?\d+\.?\d*)\s*%", title, re.IGNORECASE)
            if wpi:
                results.append({
                    "indicator_name":  "WPI_YOY",
                    "indicator_value": float(wpi.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "ET_RSS",
                    "release_date":    str(today_ist()),
                })

            if len(results) >= 4:
                break
    except Exception as e:
        logger.debug(f"ET CPI/WPI fetch failed (non-fatal): {e}")
    return results


# ── Write to macro_indicators ─────────────────────────────────────────────

def write_macro_indicators(sb, rows: list[dict]) -> int:
    """Upsert macro indicator rows. Returns count written."""
    if not rows or DRY_RUN:
        if DRY_RUN:
            logger.info(f"[DRY RUN] Would write {len(rows)} macro_indicators rows:")
            for r in rows[:8]:
                logger.info(f"  {r['indicator_name']}: {r['indicator_value']} ({r['source']})")
        return len(rows) if DRY_RUN else 0

    written = 0
    # Deduplicate by indicator_name (keep highest-confidence source)
    source_priority = {"RBI_DBIE": 4, "MOSPI": 3, "ET_RSS": 2, "YAHOO_FINANCE": 1}
    seen: dict[str, dict] = {}
    for row in rows:
        name = row["indicator_name"]
        pri  = source_priority.get(row.get("source", ""), 0)
        if name not in seen or pri > source_priority.get(seen[name].get("source", ""), 0):
            seen[name] = row

    for row in seen.values():
        try:
            sb.table("macro_indicators").upsert(
                row, on_conflict="indicator_date,indicator_name"
            ).execute()
            written += 1
        except Exception as e:
            logger.warning(f"macro_indicators write failed for {row.get('indicator_name')}: {e}")
    return written


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_macro_indicators skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Macro Indicators Ingestion starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb  = get_supabase()
    all_rows: list[dict] = []

    # Source 1: RBI policy rates
    try:
        rbi_rows = fetch_rbi_rates()
        all_rows.extend(rbi_rows)
        logger.info(f"  RBI DBIE: {len(rbi_rows)} indicators")
    except Exception as e:
        logger.warning(f"  RBI DBIE: FAILED (non-fatal) — {e}")

    # Source 2: MOSPI GDP + IIP
    try:
        mospi_rows = fetch_mospi_data()
        all_rows.extend(mospi_rows)
        logger.info(f"  MOSPI: {len(mospi_rows)} indicators")
    except Exception as e:
        logger.warning(f"  MOSPI: FAILED (non-fatal) — {e}")

    # Source 3: Yahoo Finance — US 10-yr (SG1 backup) + Silver (SG2 backup)
    for symbol, name in [("%5ETNX", "US_10YR_YIELD"), ("SI%3DF", "SILVER_PRICE")]:
        try:
            row = fetch_yahoo_finance(symbol, name)
            if row:
                all_rows.append(row)
                logger.info(f"  Yahoo {name}: {row['indicator_value']}")
        except Exception as e:
            logger.warning(f"  Yahoo {name}: FAILED (non-fatal) — {e}")

    # Source 4: ET RSS CPI/WPI
    try:
        et_rows = fetch_cpi_wpi_from_et()
        all_rows.extend(et_rows)
        logger.info(f"  ET RSS CPI/WPI: {len(et_rows)} indicators")
    except Exception as e:
        logger.warning(f"  ET RSS: FAILED (non-fatal) — {e}")

    written = write_macro_indicators(sb, all_rows)

    logger.success(f"Macro Indicators done: {len(all_rows)} fetched → {written} written")
    return {"status": "ok", "fetched": len(all_rows), "written": written}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Macro Indicators Ingestion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())
