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
    """
    Fetch RBI policy rates from Wikipedia's RBI page.
    Wikipedia's rates table is static HTML — no JS rendering, no auth.
    Updated within days of each MPC decision.
    URL verified working 2026-03-29.
    """
    results = []
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/Reserve_Bank_of_India",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            logger.debug(f"Wikipedia RBI page returned HTTP {resp.status_code}")
            return results

        idx = resp.text.find("Policy repo rate")
        if idx == -1:
            logger.debug("Wikipedia RBI: rates table not found in page")
            return results

        section = resp.text[idx:idx + 1200]
        rate_map = {
            "REPO_RATE":    r"Policy repo rate</td>\s*<td>([\d.]+)%",
            "REV_REPO":     r"Reverse repo rate</td>\s*<td>([\d.]+)%",
            "CRR":          r"Cash reserve ratio \(CRR\)</td>\s*<td>([\d.]+)%",
            "SLR":          r"Statutory liquidity ratio \(SLR\)</td>\s*<td>([\d.]+)%",
            "MSF_RATE":     r"Marginal standing facility rate</td>\s*<td>([\d.]+)%",
        }
        obs_date = str(today_ist())
        for name, pattern in rate_map.items():
            m = re.search(pattern, section)
            if m:
                results.append({
                    "indicator_name":  name,
                    "indicator_value": float(m.group(1)),
                    "indicator_date":  obs_date,
                    "source":          "WIKIPEDIA_RBI",
                    "release_date":    obs_date,
                })
    except Exception as e:
        logger.debug(f"Wikipedia RBI rates fetch failed (non-fatal): {e}")
    return results


# ── Source 2: MOSPI (GDP + IIP via gov.in RSS) ───────────────────────────

def fetch_mospi_data() -> list[dict]:
    """
    Fetch GDP and IIP from TradingEconomics meta description tags.
    Values are embedded in <meta name="description"> — no JS needed.
    URLs verified working 2026-03-29.
    """
    results = []
    indicators = [
        ("https://tradingeconomics.com/india/gdp-growth-annual", "GDP_YOY",
         r"(?:GDP|gross domestic product)[^\d]*?([\d\.]+)\s*percent", ),
        ("https://tradingeconomics.com/india/industrial-production", "IIP_YOY",
         r"(?:industrial production|IIP)[^\d]*?([+-]?[\d\.]+)\s*percent",),
        ("https://tradingeconomics.com/india/wholesale-prices", "WPI_YOY",
         r"(?:wholesale price|WPI)[^\d]*?([+-]?[\d\.]+)\s*percent",),
    ]
    for url, name, pattern in indicators:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                logger.debug(f"TradingEconomics {name} HTTP {resp.status_code}")
                continue
            meta = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]{0,400})"', resp.text)
            if not meta:
                logger.debug(f"TradingEconomics {name}: no meta description found")
                continue
            desc = meta.group(1)
            m = re.search(pattern, desc, re.IGNORECASE)
            if m:
                results.append({
                    "indicator_name":  name,
                    "indicator_value": float(m.group(1)),
                    "indicator_date":  str(today_ist()),
                    "source":          "TRADING_ECONOMICS",
                    "release_date":    str(today_ist()),
                })
                logger.debug(f"TradingEconomics {name}: {m.group(1)}% — from: {desc[:80]}")
        except Exception as e:
            logger.debug(f"TradingEconomics {name} failed (non-fatal): {e}")
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

def fetch_india_vix() -> dict | None:
    """
    Fetch India VIX from Yahoo Finance (^INDIAVIX).
    NSE publishes VIX in real-time; Yahoo reflects it with ~15min delay.
    This replaces the Sheet as the VIX source.
    """
    return fetch_yahoo_finance("%5EINDIAVIX", "INDIA_VIX")

def fetch_nifty_pcr(sb) -> dict | None:
    """
    Fetch Nifty Put/Call ratio from NSE option chain API.
    Falls back to last stored value if market is closed or OI is zero.
    """
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
        time.sleep(1)
        resp = session.get(
            "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
            headers=HEADERS,
            timeout=15
        )
        if resp.status_code != 200:
            logger.debug(f"NSE PCR API returned HTTP {resp.status_code}")
            return _load_last_pcr(sb)

        data     = resp.json()
        filtered = data.get("filtered", {})
        put_oi   = filtered.get("PE", {}).get("totOI", 0)
        call_oi  = filtered.get("CE", {}).get("totOI", 0)
        logger.debug(f"raw OI → put={put_oi} call={call_oi}")

        # Zero OI = market closed or no data — don't store, use last value
        if call_oi == 0 or put_oi == 0:
            logger.info("  Nifty PCR: zero OI detected (market closed) — using last stored value")
            return _load_last_pcr(sb)

        # Sanity check — PCR outside 0.3–3.0 is almost certainly bad data
        pcr = round(put_oi / call_oi, 3)
        if not (0.3 <= pcr <= 3.0):
            logger.warning(f"  Nifty PCR: value {pcr} outside valid range — using last stored value")
            return _load_last_pcr(sb)

        logger.debug(f"  NSE PCR: put_oi={put_oi} call_oi={call_oi} pcr={pcr}")
        return {
            "indicator_name":  "NIFTY_PCR",
            "indicator_value": pcr,
            "indicator_date":  str(today_ist()),
            "source":          "NSE_OPTION_CHAIN",
            "release_date":    str(today_ist()),
        }

    except Exception as e:
        logger.debug(f"NSE PCR fetch failed (non-fatal): {e}")
        return _load_last_pcr(sb)


def _load_last_pcr(sb) -> dict | None:
    try:
        rows = (sb.table("macro_indicators")
                  .select("indicator_value,indicator_date")
                  .eq("indicator_name", "NIFTY_PCR")
                  .gt("indicator_value", 0)
                  .order("indicator_date", desc=True)
                  .limit(1)
                  .execute().data)
        if rows:
            val  = float(rows[0]["indicator_value"])
            date = rows[0]["indicator_date"]
            logger.info(f"  Nifty PCR: using last stored value {val} from {date}")
            return {
                "indicator_name":  "NIFTY_PCR",
                "indicator_value": val,
                "indicator_date":  str(today_ist()),
                "source":          "NSE_OPTION_CHAIN_CACHED",
                "release_date":    date,
                "is_cached":       True,
            }
    except Exception as e:
        logger.warning(f"  Nifty PCR DB fallback failed: {e}")

    # ── NEW: neutral default when DB is also empty ──────────────────────────
    default_pcr = 1.0   # neutral default — cfg_float not imported in this module
    logger.warning(f"  Nifty PCR: no cached value found — using neutral default {default_pcr}")
    return {
        "indicator_name":  "NIFTY_PCR",
        "indicator_value": default_pcr,
        "indicator_date":  str(today_ist()),
        "source":          "DEFAULT_NEUTRAL",
        "release_date":    str(today_ist()),
        "is_cached":       True,
    }

# ── Source 4: ET RSS — CPI/WPI from headlines ────────────────────────────

def fetch_cpi_wpi_from_et() -> list[dict]:
    """
    Fetch CPI from TradingEconomics meta description.
    ET RSS only has CPI on release day (~12th of month) — TradingEconomics
    always has the latest published value.
    """
    results = []
    try:
        resp = requests.get(
            "https://tradingeconomics.com/india/inflation-cpi",
            headers=HEADERS, timeout=15,
        )
        if resp.status_code != 200:
            logger.debug(f"TradingEconomics CPI HTTP {resp.status_code}")
            return results
        meta = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]{0,400})"', resp.text)
        if not meta:
            logger.debug("TradingEconomics CPI: no meta description found")
            return results
        desc = meta.group(1)
        # e.g. "Inflation Rate in India increased to 3.21 percent in February from 2.74 percent"
        current = re.search(r"to\s+([+-]?[\d\.]+)\s+percent", desc, re.IGNORECASE)
        previous = re.search(r"from\s+([+-]?[\d\.]+)\s+percent", desc, re.IGNORECASE)
        if current:
            results.append({
                "indicator_name":  "CPI_YOY",
                "indicator_value": float(current.group(1)),
                "indicator_date":  str(today_ist()),
                "source":          "TRADING_ECONOMICS",
                "release_date":    str(today_ist()),
            })
        if previous:
            results.append({
                "indicator_name":  "CPI_YOY_PREV",
                "indicator_value": float(previous.group(1)),
                "indicator_date":  str(today_ist()),
                "source":          "TRADING_ECONOMICS",
                "release_date":    str(today_ist()),
            })
    except Exception as e:
        logger.debug(f"TradingEconomics CPI fetch failed (non-fatal): {e}")
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
    source_priority = {"RBI_DBIE": 4, "MOSPI": 3, "NSE_OPTION_CHAIN": 3, "ET_RSS": 2, "YAHOO_FINANCE": 1}
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


def _already_written_today(sb, name: str, today_str: str) -> bool:
    """True if today's value already exists in macro_indicators (re-run guard)."""
    try:
        rows = (
            sb.table("macro_indicators")
              .select("indicator_value")
              .eq("indicator_name", name)
              .eq("indicator_date", today_str)
              .not_.is_("indicator_value", "null")
              .limit(1)
              .execute().data
        )
        return bool(rows)
    except Exception:
        return False

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_macro_indicators skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Macro Indicators Ingestion starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb  = get_supabase()
    _today_str = str(today_ist())
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

    # Source 3: Yahoo Finance — US 10-yr + Silver + India VIX
    # Skip if today's value already exists (re-run or late-in-day execution).
    _today_str = str(today_ist())
    for symbol, name in [("%5ETNX", "US_10YR_YIELD"), ("SI%3DF", "SILVER_PRICE")]:
        if _already_written_today(sb, name, _today_str):
            logger.info(f"  Yahoo {name}: already written today — skipping")
            continue
        try:
            row = fetch_yahoo_finance(symbol, name)
            if row:
                all_rows.append(row)
                logger.info(f"  Yahoo {name}: {row['indicator_value']}")
        except Exception as e:
            logger.warning(f"  Yahoo {name}: FAILED (non-fatal) — {e}")

    try:
        if _already_written_today(sb, "INDIA_VIX", _today_str):
            logger.info("  India VIX: already written today — skipping")
        else:
            vix_row = fetch_india_vix()
            if vix_row:
                all_rows.append(vix_row)
                logger.info(f"  India VIX: {vix_row['indicator_value']}")
    except Exception as e:
        logger.warning(f"  India VIX: FAILED (non-fatal) — {e}")

    # ADD THIS BLOCK right after ↑
    try:
        pcr_row = fetch_nifty_pcr(sb)    # ← pass sb
        if pcr_row:
            all_rows.append(pcr_row)
            logger.info(f"  Nifty PCR: {pcr_row['indicator_value']} (source={pcr_row['source']})")
        else:
            logger.warning("  Nifty PCR: no data and no cached value available")
    except Exception as e:
        logger.warning(f"  Nifty PCR: FAILED (non-fatal) — {e}")

    # Source 4: ET RSS CPI/WPI
    try:
        et_rows = fetch_cpi_wpi_from_et()
        all_rows.extend(et_rows)
        logger.info(f"  ET RSS CPI/WPI: {len(et_rows)} indicators")
    except Exception as e:
        logger.warning(f"  ET RSS: FAILED (non-fatal) — {e}")

    written = write_macro_indicators(sb, all_rows)

    logger.success(
        f"Macro Indicators done: {len(all_rows)} fetched → {written} written | "
        f"indicator_date written to Supabase: {_today_str}"
    )
    return {"status": "ok", "fetched": len(all_rows), "written": written}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Macro Indicators Ingestion")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())