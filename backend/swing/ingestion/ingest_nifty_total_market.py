"""
TradeOS v7 — nifty_total_market weekly refresh

Stage D2b, 23-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D). `nifty_total_
market` was confirmed genuinely static — no freshness column existed at
all until migration 099 added `refreshed_at` alongside this module — kept
current only by someone re-importing NSE's own constituent CSVs by hand.
`intraday/scanner.py::unreferenced_candidates()` (Population B) depends on
it being current; so does `swing/compute/compute_indicators.py::fetch_
index_membership()`, which reads its `nifty_200`/`nifty_500` columns for
swing's own tagging — the reason this module writes carefully.

THREE CSVs, ONE TABLE. `ind_niftytotalmarket_list.csv` is the superset
(~752 rows: symbol, company name, industry, ISIN, series) and the only
REQUIRED fetch — every row this module writes comes from it. `ind_
nifty200list.csv` / `ind_nifty500list.csv` (200 / 500 rows) are read only
to recompute the two boolean membership columns for every row in that
superset. Each of the three is fetched and can fail INDEPENDENTLY:
Total Market failing aborts the whole run (nothing to upsert); either
index list failing means that ONE boolean column is left OUT of this
run's payload entirely, not written as True/False from stale/absent
data — Postgres/PostgREST leaves an omitted column untouched on
upsert, so a transient fetch failure degrades to "did not refresh that
flag this run", never "silently wiped it".

VERIFIED FETCHABLE, 23-Aug-2026, WHERE A PRIOR PASS ASSUMED OTHERWISE. An
earlier session flagged this URL as blocked/anti-bot after a single
WebFetch-tool timeout and left the refresher unbuilt on that basis. A
direct `requests.get()` with a plain browser User-Agent (no session
warmup, unlike ASM/GSM) returned 200 with real data on the first try.
Named here so the mistake is not repeated the next time this looks slow.

UPSERT ONLY, NEVER DELETE. A symbol present in the CURRENT table but
absent from a fresh Total Market fetch (dropped from the index, or a
transient omission) is left exactly as it is and reported as "stale" in
the log — this project's own swing-boundary caution: `nifty_total_market`
feeds a table (`stock_data_daily`, via `compute_indicators.py`) this
module has no business unilaterally shrinking the reach of.

    python -m swing.ingestion.ingest_nifty_total_market            live write
    DRY_RUN=1 python -m swing.ingestion.ingest_nifty_total_market  log only
"""
from __future__ import annotations

import csv
import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import requests
from config import get_supabase, is_kill_switch_active, logger

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

NIFTY_TOTAL_MARKET_URL = "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv"
NIFTY_200_URL          = "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv"
NIFTY_500_URL          = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"

# Same minimal-header approach ingest_asm_gsm.py already found necessary
# for nseindia.com — kept identical here for one less variable, even
# though this specific host answered a plain request with no session
# warmup at all when checked.
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_payload(total_market_rows: list[dict], nifty200_set: set[str],
                  nifty500_set: set[str], *, n200_ok: bool, n500_ok: bool,
                  refreshed_at: str) -> list[dict]:
    """
    PURE. One upsert row per Total Market CSV row, keyed on its own Symbol
    column, uppercased. `nifty_200`/`nifty_500` are set from membership in
    the two OTHER CSVs' symbol sets — but only when that fetch actually
    succeeded (`n200_ok`/`n500_ok`); otherwise the key is left OUT of the
    row entirely, not written False, so an upsert leaves the DB's existing
    value for that column untouched rather than overwriting it from a
    fetch that failed. A row with no Symbol is skipped, not written with
    an empty-string key.
    """
    out = []
    for r in total_market_rows:
        sym = (r.get("Symbol") or "").strip().upper()
        if not sym:
            continue
        row = {
            "symbol":       sym,
            "company_name": (r.get("Company Name") or "").strip() or None,
            "industry":     (r.get("Industry") or "").strip() or None,
            "isin":         (r.get("ISIN Code") or "").strip() or None,
            "series":       (r.get("Series") or "").strip() or None,
            "refreshed_at": refreshed_at,
        }
        if n200_ok:
            row["nifty_200"] = sym in nifty200_set
        if n500_ok:
            row["nifty_500"] = sym in nifty500_set
        out.append(row)
    return out


def _fetch_csv(url: str, label: str) -> list[dict] | None:
    """Returns None on fetch failure (caller must NOT treat as 'empty list')."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(10, 30))
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            logger.warning(f"{label}: fetched OK but parsed zero rows — treating as failure")
            return None
        return rows
    except Exception as e:
        logger.warning(f"{label} fetch failed: {e}")
        return None


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — ingest_nifty_total_market skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"nifty_total_market refresh starting {'[DRY RUN]' if DRY_RUN else ''}")

    total_market_rows = _fetch_csv(NIFTY_TOTAL_MARKET_URL, "Nifty Total Market")
    if total_market_rows is None:
        logger.warning("Total Market fetch failed — nothing to refresh, keeping existing data")
        return {"status": "no_data"}

    nifty200_rows = _fetch_csv(NIFTY_200_URL, "Nifty 200")
    nifty500_rows = _fetch_csv(NIFTY_500_URL, "Nifty 500")
    n200_ok, n500_ok = nifty200_rows is not None, nifty500_rows is not None
    nifty200_set = {r["Symbol"].strip().upper() for r in (nifty200_rows or []) if r.get("Symbol")}
    nifty500_set = {r["Symbol"].strip().upper() for r in (nifty500_rows or []) if r.get("Symbol")}

    logger.info(
        f"Total Market: {len(total_market_rows)} | "
        f"Nifty 200: {len(nifty200_set)}{'' if n200_ok else ' (FETCH FAILED, flag not refreshed this run)'} | "
        f"Nifty 500: {len(nifty500_set)}{'' if n500_ok else ' (FETCH FAILED, flag not refreshed this run)'}"
    )

    payload = build_payload(total_market_rows, nifty200_set, nifty500_set,
                            n200_ok=n200_ok, n500_ok=n500_ok,
                            refreshed_at=_now_utc())

    sb = get_supabase()
    try:
        existing = {r["symbol"] for r in
                   (sb.table("nifty_total_market").select("symbol").execute().data or [])}
    except Exception as e:
        logger.warning(f"  could not read existing nifty_total_market for diff reporting: {e}")
        existing = set()

    new_symbols = {r["symbol"] for r in payload} - existing
    stale_symbols = existing - {r["symbol"] for r in payload}
    logger.info(
        f"  {len(new_symbols)} new symbol(s), "
        f"{len(payload) - len(new_symbols)} refreshed, "
        f"{len(stale_symbols)} existing symbol(s) not in this fetch "
        f"(left untouched, not deleted)"
    )

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would upsert {len(payload)} rows "
                     f"(nifty_200 {'refreshed' if n200_ok else 'unchanged'}, "
                     f"nifty_500 {'refreshed' if n500_ok else 'unchanged'})")
        for r in payload[:5]:
            logger.info(f"  {r}")
        return {"status": "dry_run", "total": len(payload), "new": len(new_symbols),
               "stale": len(stale_symbols), "n200_ok": n200_ok, "n500_ok": n500_ok}

    try:
        for i in range(0, len(payload), 300):
            sb.table("nifty_total_market").upsert(
                payload[i:i + 300], on_conflict="symbol").execute()
    except Exception as e:
        logger.error(f"Failed to write nifty_total_market: {e}")
        return {"status": "write_failed", "error": str(e)}

    logger.info(f"nifty_total_market refresh complete: {len(payload)} rows written")
    return {"status": "ok", "total": len(payload), "new": len(new_symbols),
           "stale": len(stale_symbols), "n200_ok": n200_ok, "n500_ok": n500_ok}


if __name__ == "__main__":
    result = main()
    logger.info(f"Result: {result}")
