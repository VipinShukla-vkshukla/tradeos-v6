"""
TradeOS v7 — Phase 1: Global Cues Ingestion

Runs TWICE daily:
  EVENING (6 PM IST) — captures intraday USD/INR, Brent, US direction
                        for tonight's generate_signals + ai_enrich sector filters
  MORNING (8 AM IST) — captures true US closing prices (markets closed at 1:30 AM IST)
                        for pre-market Gift Nifty gap + Telegram brief

Change % logic:
  EVENING compares vs previous EVENING → intraday drift, valid for sector_impacts
  MORNING compares vs previous MORNING → true close-to-close, valid for Telegram brief

Data retention: APPEND (upsert on date+session)
"""
import sys
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
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

def _read_macro_indicator(sb, name: str, today_str: str) -> float | None:
    """
    Read today's value from macro_indicators (written by step 02).
    Returns None on cache miss so caller falls back to Yahoo.
    Uses actual schema: indicator_name / indicator_value / indicator_date.
    """
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
        if rows:
            return float(rows[0]["indicator_value"])
    except Exception as e:
        logger.debug(f"  macro_indicators read for {name} failed: {e}")
    return None

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
    inr_chg = usd_inr - usd_inr_prev  # positive = INR weakened
    if inr_chg > 0.5:
        return "POSITIVE_IT_PHARMA_NEGATIVE_IMPORTERS"
    elif inr_chg < -0.5:
        return "NEGATIVE_IT_POSITIVE_IMPORTERS"
    return "NEUTRAL"


def classify_global_bias(dow_chg_pct: float | None, nasdaq_chg_pct: float | None) -> str:
    if dow_chg_pct is None or nasdaq_chg_pct is None:
        return "MIXED"
    avg = (dow_chg_pct + nasdaq_chg_pct) / 2
    if avg > 1:
        return "RISK_ON"
    if avg < -1:
        return "RISK_OFF"
    return "MIXED"


def main(time_slot: str = "EVENING"):
    """
    time_slot: MORNING (8 AM IST) or EVENING (6 PM IST)

    MORNING → prev lookup against previous MORNING row (true close-to-close %)
    EVENING → prev lookup against previous EVENING row (intraday drift, enough for sector_impacts)
    """
    if is_kill_switch_active():
        logger.warning("⛔ Kill switch active — Global Cues ingestion skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    time_slot = time_slot.upper()
    if time_slot not in ("MORNING", "EVENING"):
        logger.error(f"Invalid time_slot '{time_slot}' — must be MORNING or EVENING")
        return {"status": "error", "reason": "invalid_time_slot"}

    logger.info(f"Global Cues Ingestion — {time_slot}")
    sb = get_supabase()
    today = today_ist()
    today_str = today.isoformat()

    logger.info(f"  date={today_str} | session={time_slot}")

    http = requests.Session()
    http.headers.update({"User-Agent": "Mozilla/5.0"})

    # ── Fetch all prices ──────────────────────────────────────────
    gift_nifty = fetch_from_yahoo("^NSEI",    http)
    usd_inr    = fetch_from_yahoo("USDINR=X", http)
    brent      = fetch_from_yahoo("BZ=F",     http)
    gold       = fetch_from_yahoo("GC=F",     http)
    dow        = fetch_from_yahoo("^DJI",     http)
    nasdaq     = fetch_from_yahoo("^IXIC",    http)
    sp500      = fetch_from_yahoo("^GSPC",    http)
    # SG1: US 10-year Treasury yield — primary driver of FII EM outflows.
    # A yield spike from 4.2% to 4.8% predicts FII selling 1-2 days before flows show in data.
    # ^TNX quotes yield in % (e.g. 4.52 = 4.52%). Change in basis points = chg_pct * 100.
    # US 10-yr: step 02 already fetched and wrote this to macro_indicators.
    # Read from DB to avoid a duplicate Yahoo request. Fall back only on miss.
    _today_str = today.isoformat()
    us_10yr = _read_macro_indicator(sb, "US_10YR_YIELD", today_str)
    if us_10yr is None:
        us_10yr = fetch_from_yahoo("^TNX", http)
        logger.debug("  us_10yr: macro_indicators miss — fetched from Yahoo")
    else:
        logger.debug(f"  us_10yr: macro_indicators hit — {us_10yr}")

    # SG2: Silver price — same pattern
    silver = _read_macro_indicator(sb, "SILVER_PRICE", today_str)
    if silver is None:
        silver = fetch_from_yahoo("SI=F", http)
        logger.debug("  silver: macro_indicators miss — fetched from Yahoo")
    else:
        logger.debug(f"  silver: macro_indicators hit — {silver}")

    # ── Previous row — same session type for apples-to-apples change % ──
    # MORNING vs MORNING = true US close-to-close (US closed at 1:30 AM IST)
    # EVENING vs EVENING = intraday drift (sufficient for sector_impacts thresholds)
    try:
        prev_rows = (
            sb.table("global_cues")
            .select("*")
            .eq("session", time_slot)          # ← KEY FIX: was hardcoded "EVENING"
            .order("date", desc=True)
            .limit(1)
            .execute()
            .data
        )
        prev = prev_rows[0] if prev_rows else {}
    except Exception:
        prev = {}

    crude_prev  = float(prev.get("brent_crude",    0) or 0) or None
    inr_prev    = float(prev.get("usd_inr",        0) or 0) or None
    dow_prev    = float(prev.get("us_dow_close",   0) or 0) or None
    nas_prev    = float(prev.get("us_nasdaq_close",0) or 0) or None
    sp5_prev    = float(prev.get("sp500_close",    0) or 0) or None
    us10yr_prev = float(prev.get("us_10yr_yield",  0) or 0) or None  # SG1
    silver_prev = float(prev.get("silver_price",   0) or 0) or None  # SG2

    # ── Change percentages ────────────────────────────────────────
    usd_inr_chg_pct = round((usd_inr - inr_prev)   / inr_prev   * 100, 4) if usd_inr and inr_prev   else None
    brent_chg_pct   = round((brent   - crude_prev)  / crude_prev * 100, 4) if brent   and crude_prev else None
    dow_chg_pct     = round((dow     - dow_prev)    / dow_prev   * 100, 4) if dow     and dow_prev   else None
    nas_chg_pct     = round((nasdaq  - nas_prev)    / nas_prev   * 100, 4) if nasdaq  and nas_prev   else None
    sp500_chg_pct   = round((sp500   - sp5_prev)    / sp5_prev   * 100, 4) if sp500   and sp5_prev   else None
    # SG1: US 10-yr change in basis points (yield moves quoted in bps, not pct)
    # e.g. yield 4.52→4.68 = +16 bps — that's what triggers FII EM outflows
    us_10yr_chg_bps = round((us_10yr - us10yr_prev) * 100, 1) if us_10yr and us10yr_prev else None
    # SG2: Silver change in pct (same pattern as gold/crude)
    silver_chg_pct  = round((silver  - silver_prev) / silver_prev * 100, 4) if silver and silver_prev else None

    # ── Gift Nifty gap vs previous Nifty close ────────────────────
    # Only meaningful for MORNING (pre-market gap signal)
    # EVENING: still computed but gap_signal will be less actionable
    gift_nifty_chg_pct, gap_signal = None, "UNKNOWN"
    try:
        nifty_row = (
            sb.table("market_regime")
            .select("nifty_price")
            .order("date", desc=True)
            .limit(1)
            .execute()
            .data
        )
        nifty_price = float(nifty_row[0]["nifty_price"]) if nifty_row else None
        gift_nifty_chg_pct, gap_signal = compute_gap_signal(gift_nifty, nifty_price)
    except Exception:
        pass

    # ── Sector impacts JSONB ──────────────────────────────────────
    sector_impacts = {
        "crude_impact":  classify_crude_impact(brent, crude_prev),
        "inr_impact":    classify_inr_impact(usd_inr, inr_prev),
        "global_bias":   classify_global_bias(dow_chg_pct, nas_chg_pct),
        # SG1: bond yield signal — high yield = FII selling pressure on India
        "us10yr_signal": (
            "RATE_PRESSURE_HIGH" if us_10yr_chg_bps and us_10yr_chg_bps > 10
            else "RATE_PRESSURE_LOW" if us_10yr_chg_bps and us_10yr_chg_bps < -10
            else "NEUTRAL"
        ),
        # SG2: precious metals signal
        "silver_signal": (
            "PRECIOUS_METALS_RALLY" if silver_chg_pct and silver_chg_pct > 2
            else "PRECIOUS_METALS_SELL" if silver_chg_pct and silver_chg_pct < -2
            else "NEUTRAL"
        ),
    }

    row = {
        "date":               today.isoformat(),
        "session":            time_slot,
        "gift_nifty":         gift_nifty,
        "gift_nifty_chg_pct": gift_nifty_chg_pct,
        "gap_signal":         gap_signal,
        "usd_inr":            usd_inr,
        "usd_inr_chg_pct":    usd_inr_chg_pct,
        "brent_crude":        brent,
        "brent_chg_pct":      brent_chg_pct,
        "gold_price":         gold,
        "us_dow_close":       dow,
        "us_nasdaq_close":    nasdaq,
        "us_dow_chg_pct":     dow_chg_pct,
        "us_nasdaq_chg_pct":  nas_chg_pct,
        "sp500_close":        sp500,
        "sp500_chg_pct":      sp500_chg_pct,
        "sector_impacts":     sector_impacts,
        # SG1: US 10-year Treasury yield
        "us_10yr_yield":      us_10yr,
        "us_10yr_chg_bps":    us_10yr_chg_bps,
        # SG2: Silver price
        "silver_price":       silver,
        "silver_chg_pct":     silver_chg_pct,
    }

    # APPEND: upsert on date+session composite key
    sb.table("global_cues").upsert(row, on_conflict="date,session").execute()

    # ── Log summary (fixed f-string — was broken in original) ─────
    dow_str = f"DOW={dow or 'N/A'} ({dow_chg_pct:+.2f}%)" if dow_chg_pct is not None else f"DOW={dow or 'N/A'}"
    logger.success(
        f"[{time_slot}] Global cues saved: "
        f"Supabase Written date={today_str} | "
        f"Gift={gift_nifty or 'N/A'} | "
        f"USD/INR={usd_inr or 'N/A'} | "
        f"Crude={brent or 'N/A'} | "
        f"{dow_str} | "
        f"S&P={sp500 or 'N/A'} | "
        f"Gap={gap_signal} | "
        f"Bias={sector_impacts['global_bias']}"
    )
    return row


if __name__ == "__main__":
    slot = sys.argv[1].upper() if len(sys.argv) > 1 else "EVENING"
    main(slot)