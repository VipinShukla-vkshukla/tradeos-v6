"""
TradeOS v6 — Phase 1: Global Cues Ingestion
Fetches pre-market (8 AM) and post-market (6 PM) global signals:
  Gift Nifty, USD/INR, Brent Crude, Gold, Dow, Nasdaq

Used by signal engine to apply sector-specific impact filters.
Data retention: APPEND (upsert on date+session)
"""
import sys
import requests
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger


def fetch_from_yahoo(ticker: str, session: requests.Session) -> float | None:
    """Fetch latest price from Yahoo Finance API (free, no auth)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    try:
        resp = session.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(price)
    except Exception as e:
        logger.debug(f"Yahoo fetch failed for {ticker}: {e}")
        return None


def compute_gap_signal(gift_nifty: float | None, nifty_last: float | None) -> tuple[float | None, str]:
    """Returns (gap_chg_pct, gap_signal_text)."""
    if gift_nifty and nifty_last and nifty_last > 0:
        pct = round((gift_nifty - nifty_last) / nifty_last * 100, 2)
        if pct > 0.3:
            label = f"GAP_UP {pct:+.2f}%"
        elif pct < -0.3:
            label = f"GAP_DOWN {pct:+.2f}%"
        else:
            label = f"FLAT {pct:+.2f}%"
        return pct, label
    return None, "UNKNOWN"


def classify_crude_impact(crude: float | None, crude_prev: float | None) -> str:
    if not crude or not crude_prev or crude_prev == 0:
        return "NEUTRAL"
    chg_pct = (crude - crude_prev) / crude_prev * 100
    if chg_pct > 3:
        return "POSITIVE_ENERGY_NEGATIVE_DOWNSTREAM"
    elif chg_pct < -3:
        return "NEGATIVE_ENERGY_POSITIVE_DOWNSTREAM"
    return "NEUTRAL"


def classify_inr_impact(usd_inr: float | None, usd_inr_prev: float | None) -> str:
    if not usd_inr or not usd_inr_prev or usd_inr_prev == 0:
        return "NEUTRAL"
    inr_chg = usd_inr - usd_inr_prev   # positive = INR weakened
    if inr_chg > 0.5:
        return "POSITIVE_IT_PHARMA_NEGATIVE_IMPORTERS"
    elif inr_chg < -0.5:
        return "NEGATIVE_IT_POSITIVE_IMPORTERS"
    return "NEUTRAL"


def classify_global_bias(dow_chg_pct: float | None, nasdaq_chg_pct: float | None) -> str:
    if dow_chg_pct is None or nasdaq_chg_pct is None:
        return "MIXED"
    avg = (dow_chg_pct + nasdaq_chg_pct) / 2
    if avg > 1:   return "RISK_ON"
    if avg < -1:  return "RISK_OFF"
    return "MIXED"


def main(time_slot: str = "EVENING"):
    """time_slot: MORNING (8AM) or EVENING (6PM)"""
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — Global Cues ingestion skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Global Cues Ingestion — {time_slot}")
    sb = get_supabase()
    today = today_ist()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # ── Fetch all prices ──────────────────────────────────────────
    gift_nifty = fetch_from_yahoo("^NSEI", session)
    usd_inr    = fetch_from_yahoo("USDINR=X", session)
    brent      = fetch_from_yahoo("BZ=F", session)
    gold       = fetch_from_yahoo("GC=F", session)
    dow        = fetch_from_yahoo("^DJI", session)
    nasdaq     = fetch_from_yahoo("^IXIC", session)
    sp500      = fetch_from_yahoo("^GSPC", session)

    # ── Previous evening row for change % calculations ────────────
    try:
        prev_rows = sb.table("global_cues").select("*") \
            .eq("session", "EVENING") \
            .order("date", desc=True).limit(1).execute().data
        prev = prev_rows[0] if prev_rows else {}
    except Exception:
        prev = {}

    crude_prev = float(prev.get("brent_crude", 0) or 0) or None
    inr_prev   = float(prev.get("usd_inr", 0) or 0) or None
    dow_prev   = float(prev.get("us_dow_close", 0) or 0) or None       # ✅ correct col name
    nas_prev   = float(prev.get("us_nasdaq_close", 0) or 0) or None    # ✅ correct col name
    sp5_prev   = float(prev.get("sp500_close", 0) or 0) or None

    # ── Change percentages ────────────────────────────────────────
    usd_inr_chg_pct = round((usd_inr - inr_prev)  / inr_prev  * 100, 4) if usd_inr and inr_prev else None
    brent_chg_pct   = round((brent   - crude_prev) / crude_prev * 100, 4) if brent and crude_prev else None
    dow_chg_pct     = round((dow     - dow_prev)   / dow_prev  * 100, 4) if dow and dow_prev else None
    nas_chg_pct     = round((nasdaq  - nas_prev)   / nas_prev  * 100, 4) if nasdaq and nas_prev else None
    sp500_chg_pct    = round((sp500   - sp5_prev)   / sp5_prev  * 100, 4) if sp500 and sp5_prev else None

    # ── Gift Nifty gap ────────────────────────────────────────────
    gift_nifty_chg_pct, gap_signal = None, "UNKNOWN"
    try:
        nifty_row = sb.table("market_regime").select("nifty_price") \
            .order("date", desc=True).limit(1).execute().data
        nifty_price = float(nifty_row[0]["nifty_price"]) if nifty_row else None
        gift_nifty_chg_pct, gap_signal = compute_gap_signal(gift_nifty, nifty_price)
    except Exception:
        pass

    # ── sector_impacts → single JSONB (matches DB schema) ─────────
    sector_impacts = {
        "crude_impact": classify_crude_impact(brent, crude_prev),
        "inr_impact":   classify_inr_impact(usd_inr, inr_prev),
        "global_bias":  classify_global_bias(dow_chg_pct, nas_chg_pct),
    }

    row = {
        "date":               today.isoformat(),
        "session":            time_slot,            # ✅ was time_slot (key name)
        "gift_nifty":         gift_nifty,
        "gift_nifty_chg_pct": gift_nifty_chg_pct,  # ✅ was gift_nifty_chg
        "gap_signal":         gap_signal,           # ✅ was gap_predicted (float) → now text
        "usd_inr":            usd_inr,
        "usd_inr_chg_pct":    usd_inr_chg_pct,     # ✅ was missing entirely
        "brent_crude":        brent,
        "brent_chg_pct":      brent_chg_pct,        # ✅ was missing entirely
        "gold_price":         gold,                 # ✅ was gold_usd
        "us_dow_close":       dow,                  # ✅ was dow_jones
        "us_nasdaq_close":    nasdaq,               # ✅ was nasdaq
        "us_dow_chg_pct":     dow_chg_pct,
        "us_nasdaq_chg_pct":  nas_chg_pct,
        "sp500_close":        sp500,
        "sp500_chg_pct":      sp500_chg_pct,
        "sector_impacts":     sector_impacts,       # ✅ was 3 flat cols → 1 jsonb
    }

    # APPEND: upsert on date+session (matches DB primary key)
    sb.table("global_cues").upsert(row, on_conflict="date,session").execute()  # ✅ was date,time_slot
    logger.success(
        f"Global cues saved: Gift={gift_nifty or 'N/A'} | "
        f"USD/INR={usd_inr or 'N/A'} | Crude={brent or 'N/A'} | "
        f"DOW={dow or 'N/A'} ({dow_chg_pct:+.2f}%)" if dow_chg_pct else f"DOW={dow or 'N/A'} | "
        f"S&P={sp500 or 'N/A'} | Gap={gap_signal} | Bias={sector_impacts['global_bias']}"
    )
    return row


if __name__ == "__main__":
    slot = sys.argv[1].upper() if len(sys.argv) > 1 else "EVENING"
    main(slot)