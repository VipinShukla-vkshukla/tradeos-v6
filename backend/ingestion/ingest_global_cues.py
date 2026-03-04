"""
TradeOS v6 — Phase 1: Global Cues Ingestion
Fetches pre-market (8 AM) and post-market (6 PM) global signals:
  Gift Nifty, USD/INR, Brent Crude, Gold, Dow, Nasdaq

Used by signal engine to apply sector-specific impact filters.
Data retention: APPEND (upsert on date+time_slot)
"""
import sys
import time
import requests
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, now_ist, check_kill_switch, logger

def fetch_from_yahoo(ticker: str, session: requests.Session) -> float | None:
    """Fetch latest price from Yahoo Finance API (free, no auth)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    try:
        resp = session.get(url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(price)
    except Exception as e:
        logger.debug(f"Yahoo fetch failed for {ticker}: {e}")
        return None

def fetch_gift_nifty(session: requests.Session) -> tuple[float | None, float | None]:
    """Fetch Gift Nifty price and change."""
    price = fetch_from_yahoo("NIFTYBEES.NS", session)  # proxy
    # Try SGX Nifty via alternative
    gift = fetch_from_yahoo("^NSEI", session)
    return gift, price

def compute_gap(gift_nifty: float | None, nifty_last: float | None) -> float | None:
    """Estimated gap-up/down percentage."""
    if gift_nifty and nifty_last and nifty_last > 0:
        return round((gift_nifty - nifty_last) / nifty_last * 100, 2)
    return None

def classify_crude_impact(crude: float | None, crude_prev: float | None) -> str:
    """Classify crude oil move impact on Indian sectors."""
    if not crude or not crude_prev or crude_prev == 0:
        return "NEUTRAL"
    chg_pct = (crude - crude_prev) / crude_prev * 100
    if chg_pct > 3:
        return "POSITIVE_ENERGY_NEGATIVE_DOWNSTREAM"   # energy stocks up, paint/tyre down
    elif chg_pct < -3:
        return "NEGATIVE_ENERGY_POSITIVE_DOWNSTREAM"
    return "NEUTRAL"

def classify_inr_impact(usd_inr: float | None, usd_inr_prev: float | None) -> str:
    """Classify INR move impact on Indian sectors."""
    if not usd_inr or not usd_inr_prev or usd_inr_prev == 0:
        return "NEUTRAL"
    inr_chg = usd_inr - usd_inr_prev  # positive = INR weakened
    if inr_chg > 0.5:
        return "POSITIVE_IT_PHARMA_NEGATIVE_IMPORTERS"   # IT/pharma exports gain, oil importers lose
    elif inr_chg < -0.5:
        return "NEGATIVE_IT_POSITIVE_IMPORTERS"
    return "NEUTRAL"

def classify_global_bias(dow_chg_pct: float | None, nasdaq_chg_pct: float | None) -> str:
    if dow_chg_pct is None or nasdaq_chg_pct is None:
        return "MIXED"
    avg = (dow_chg_pct + nasdaq_chg_pct) / 2
    if avg > 1:    return "RISK_ON"
    if avg < -1:   return "RISK_OFF"
    return "MIXED"

def main(time_slot: str = "EVENING"):
    """time_slot: MORNING (8AM) or EVENING (6PM)"""
    check_kill_switch()
    logger.info(f"Global Cues Ingestion — {time_slot}")
    sb = get_supabase()
    today = today_ist()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # Fetch all data
    gift_nifty = fetch_from_yahoo("^NSEI", session)
    usd_inr    = fetch_from_yahoo("USDINR=X", session)
    brent      = fetch_from_yahoo("BZ=F", session)
    gold       = fetch_from_yahoo("GC=F", session)
    dow        = fetch_from_yahoo("^DJI", session)
    nasdaq     = fetch_from_yahoo("^IXIC", session)

    # Get previous day values for change calculations
    try:
        prev_rows = sb.table("global_cues").select("*") \
            .eq("time_slot", "EVENING") \
            .order("date", desc=True).limit(1).execute().data
        prev = prev_rows[0] if prev_rows else {}
    except Exception:
        prev = {}

    crude_prev = float(prev.get("brent_crude", 0) or 0) or None
    inr_prev   = float(prev.get("usd_inr", 0) or 0) or None
    dow_prev   = float(prev.get("dow_jones", 0) or 0) or None
    nas_prev   = float(prev.get("nasdaq", 0) or 0) or None

    # Compute impacts
    gift_chg    = None
    try:
        nifty_today = sb.table("market_regime").select("nifty_price") \
            .order("date", desc=True).limit(1).execute().data
        nifty_price = float(nifty_today[0]["nifty_price"]) if nifty_today else None
        gift_chg = compute_gap(gift_nifty, nifty_price)
    except Exception:
        pass

    dow_chg_pct  = ((dow - dow_prev)     / dow_prev * 100)  if dow and dow_prev else None
    nas_chg_pct  = ((nasdaq - nas_prev)  / nas_prev * 100)  if nasdaq and nas_prev else None

    row = {
        "date":           today.isoformat(),
        "time_slot":      time_slot,
        "gift_nifty":     gift_nifty,
        "gift_nifty_chg": gift_chg,
        "usd_inr":        usd_inr,
        "brent_crude":    brent,
        "gold_usd":       gold,
        "dow_jones":      dow,
        "nasdaq":         nasdaq,
        "gap_predicted":  gift_chg,
        "crude_impact":   classify_crude_impact(brent, crude_prev),
        "inr_impact":     classify_inr_impact(usd_inr, inr_prev),
        "global_bias":    classify_global_bias(dow_chg_pct, nas_chg_pct),
    }

    # APPEND: upsert on date+time_slot
    sb.table("global_cues").upsert(row, on_conflict="date,time_slot").execute()
    logger.success(
        f"Global cues saved: Gift={gift_nifty or 'N/A'} | "
        f"USD/INR={usd_inr or 'N/A'} | Crude={brent or 'N/A'} | "
        f"Bias={row['global_bias']}"
    )
    return row

if __name__ == "__main__":
    import sys
    slot = sys.argv[1].upper() if len(sys.argv) > 1 else "EVENING"
    main(slot)
