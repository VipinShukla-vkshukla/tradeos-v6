"""
TradeOS v7 — NSE IPO archive refresh

Stage D2f, 24-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D). Replaces the
raw_prices-based recency heuristic (F-58, migration 101) the operator
told this session to scrap — "you cannot use raw prices count to
identify the new listings, it has n number of different records...
unnecessarily complicating the things" — with NSE's own authoritative
IPO archive.

`https://www.nseindia.com/api/public-past-issues?index=equity` — a real
JSON API behind NSE's "All Upcoming Issues" page, verified live 24-Aug:
1,411 records back to 2003-01-02, every one carrying the actual NSE
tradingsymbol directly (no fuzzy company-name matching needed, unlike
groww.in/ipo, which was checked first and found to expose company names
only, no symbol at all).

Session-warmup pattern copied from ingest_asm_gsm.py's nse_session() —
proven against this exact host already; not reinvented here.

    python -m swing.ingestion.ingest_ipo_listings            live write
    DRY_RUN=1 python -m swing.ingestion.ingest_ipo_listings  log only
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import requests
from config import get_supabase, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

NSE_HOMEPAGE      = "https://www.nseindia.com"
NSE_IPO_API       = "https://www.nseindia.com/api/public-past-issues?index=equity"
NSE_IPO_PAGE      = "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"

NSE_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}

_PRICE_RANGE_RE = re.compile(r"Rs\.?\s*([\d,.]+)\s*to\s*Rs\.?\s*([\d,.]+)", re.IGNORECASE)


def _nse_session() -> requests.Session:
    """Same warmup ingest_asm_gsm.py already uses successfully against
    this host: a homepage GET for cookies before the real request."""
    session = requests.Session()
    try:
        resp = session.get(NSE_HOMEPAGE, headers=NSE_HEADERS, timeout=15)
        session.cookies.update(resp.cookies)
        time.sleep(1)
    except Exception as e:
        logger.warning(f"NSE session warmup failed (continuing): {e}")
    return session


def fetch_ipo_archive() -> list[dict] | None:
    """Returns None on fetch failure (caller must NOT treat as 'zero IPOs')."""
    session = _nse_session()
    try:
        resp = session.get(NSE_IPO_API, headers={**NSE_HEADERS, "Referer": NSE_IPO_PAGE},
                           timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else None
    except Exception as e:
        logger.warning(f"NSE IPO archive fetch failed: {e}")
        return None


def _parse_date(raw: str | None) -> str | None:
    if not raw or raw.strip() == "-":
        return None
    try:
        return datetime.strptime(raw.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def _parse_price(raw: str | None) -> float | None:
    if not raw:
        return None
    raw = raw.strip().replace(",", "")
    if not raw or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_price_range(raw: str | None) -> tuple[float | None, float | None]:
    if not raw:
        return None, None
    m = _PRICE_RANGE_RE.search(raw)
    if not m:
        return None, None
    try:
        return float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
    except ValueError:
        return None, None


def build_rows(raw: list[dict], refreshed_at: str) -> list[dict]:
    """
    PURE. NSE's own JSON records -> ipo_listings upsert rows. A record
    with no `symbol` is skipped, not written with an empty-string key.

    DEDUPED ON SYMBOL, FIRST OCCURRENCE WINS. NSE's archive carries one
    row per BOND/NCD TRANCHE, not per company — a single issuer can
    repeat under the same bare symbol many times (IBULHSG: 13 rows, all
    listing_date 09-APR-2024, confirmed live 24-Aug-2026 — genuinely
    identical, not conflicting data). `symbol` is this table's own
    PRIMARY KEY (one row per symbol, matching every other reference
    table in this project), so a raw upsert of the unmodified feed
    raises Postgres error 21000 ("cannot affect row a second time")
    before it ever writes anything. The API returns newest-first, so
    keeping the first occurrence keeps the most recent tranche when
    dates genuinely differ; harmless when they do not.
    """
    out = []
    seen: set[str] = set()
    for r in raw:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        low, high = _parse_price_range(r.get("priceRange"))
        out.append({
            "symbol":            sym,
            "company_name":      (r.get("company") or "").strip() or None,
            "security_type":     (r.get("securityType") or "").strip() or None,
            "issue_price":       _parse_price(r.get("issuePrice")),
            "price_range_low":   low,
            "price_range_high":  high,
            "issue_start_date":  _parse_date(r.get("ipoStartDate")),
            "issue_end_date":    _parse_date(r.get("ipoEndDate")),
            "listing_date":      _parse_date(r.get("listingDate")),
            "source":            "NSE",
            "refreshed_at":      refreshed_at,
        })
    return out


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_ipo_listings skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"NSE IPO archive refresh starting {'[DRY RUN]' if DRY_RUN else ''}")

    raw = fetch_ipo_archive()
    if raw is None:
        logger.warning("NSE IPO archive fetch failed — nothing to refresh, keeping existing data")
        return {"status": "no_data"}

    refreshed_at = datetime.now(timezone.utc).isoformat()
    rows = build_rows(raw, refreshed_at)
    logger.info(f"  NSE IPO archive: {len(raw)} records -> {len(rows)} rows to upsert")

    if DRY_RUN:
        listed = [r for r in rows if r["listing_date"]]
        logger.info(f"[DRY RUN] Would upsert {len(rows)} rows ({len(listed)} with a real listing_date)")
        for r in rows[:5]:
            logger.info(f"  {r}")
        return {"status": "dry_run", "total": len(rows)}

    sb = get_supabase()
    try:
        for i in range(0, len(rows), 300):
            sb.table("ipo_listings").upsert(rows[i:i + 300], on_conflict="symbol").execute()
    except Exception as e:
        logger.error(f"Failed to write ipo_listings: {e}")
        return {"status": "write_failed", "error": str(e)}

    logger.info(f"ipo_listings refresh complete: {len(rows)} rows written")
    return {"status": "ok", "total": len(rows)}


if __name__ == "__main__":
    result = main()
    logger.info(f"Result: {result}")
