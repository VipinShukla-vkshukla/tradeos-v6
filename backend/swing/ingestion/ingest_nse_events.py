"""
TradeOS v7 — Phase 1: NSE Corporate Events Ingestion
Fetches upcoming corporate filings (results, dividends, AGMs) from NSE.
Replaces the manual weekly update of 'Nifty Upcoming Events' Sheet tab.

Data retention: REPLACE weekly (old events purged after their date passes)
"""
import sys
import time
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger  # ✅ correct name

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Referer": "https://www.nseindia.com/",
}

# NSE corporate filings API
NSE_EVENTS_URL = "https://www.nseindia.com/api/event-calendar"

def fetch_nse_events(session: requests.Session, from_date: date, to_date: date) -> list[dict]:
    """Fetch corporate events from NSE API."""
    params = {
        "index": "equities",
        "fromDate": from_date.strftime("%d-%m-%Y"),
        "toDate":   to_date.strftime("%d-%m-%Y"),
    }
    try:
        resp = session.get(NSE_EVENTS_URL, params=params,
                          headers=NSE_HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        logger.warning(f"NSE events fetch failed: {e}")
        return []

def parse_events(raw_events: list, today: date) -> list[dict]:
    """Parse raw NSE events into DB rows."""
    rows = []
    for ev in raw_events:
        try:
            symbol   = str(ev.get("symbol", "") or "").strip().upper()
            company  = str(ev.get("company", "") or "").strip()
            purpose  = str(ev.get("purpose", "") or "").strip()
            details  = str(ev.get("bm_desc", "") or ev.get("description", "") or "").strip()[:500]
            date_str = str(ev.get("date", "") or "").strip()

            if not symbol or not date_str:
                continue

            # Parse event date — try each format in order; date.fromisoformat
            # is equivalent to strptime(..., "%Y-%m-%d") so there's no need
            # for a separate code path for it (the previous ternary here
            # always routed any 10-char dashed string to fromisoformat
            # regardless of which fmt the loop was on, so DD-MM-YYYY dates
            # never actually reached the strptime branch that handles them).
            for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    ev_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                continue

            days_to = (ev_date - today).days
            rows.append({
                "symbol":        symbol,
                "company_name":  company,
                "purpose":       purpose,
                "details":       details,
                "event_date":    ev_date.isoformat(),
                "days_to_event": days_to,
                "source":        "NSE_API",
            })
        except Exception as e:
            logger.debug(f"Skipping event row: {e}")

    return rows

def main():
    if is_kill_switch_active():                                              # ✅ correct call
        logger.warning("⛔ Kill switch active — NSE Events ingestion skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info("NSE Events Ingestion starting")
    sb = get_supabase()
    today = today_ist()

    session = requests.Session()
    try:
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=10)
        time.sleep(1)
    except Exception:
        pass

    # Fetch next 90 days of events
    to_date = today + timedelta(days=90)
    raw = fetch_nse_events(session, today, to_date)
    logger.info(f"Fetched {len(raw)} raw events from NSE")

    rows = parse_events(raw, today)
    if not rows:
        logger.warning("No events parsed — NSE may be unavailable")
        return {"status": "no_data"}

    # REPLACE: delete expired events (past date), upsert current+future
    try:
        # Purge events older than today
        sb.table("nifty_upcoming_events") \
          .delete().lt("event_date", today.isoformat()).execute()

        # Deduplicate within the fetched batch — NSE API can return the same
        # event multiple times, causing ON CONFLICT to affect the same row twice
        seen_keys: set = set()
        deduped: list  = []
        for r in rows:
            key = (r["symbol"], r["purpose"], r["event_date"])
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(r)
        logger.info(f"  Deduped: {len(rows)} → {len(deduped)} events")

        # Upsert new events
        for i in range(0, len(deduped), 100):
            batch = deduped[i:i+100]
            sb.table("nifty_upcoming_events").upsert(
                batch, on_conflict="symbol,purpose,event_date"
            ).execute()

        logger.success(f"NSE events saved: {len(deduped)} events (next 90 days, {len(rows)-len(deduped)} dupes dropped)")
        return {"status": "ok", "events": len(deduped)}
    except Exception as e:
        logger.error(f"Failed to save events: {e}")
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    main()