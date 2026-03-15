"""
TradeOS v6 — Phase 1: Independent Scanner (Layer B)
Scans STOCK_DATA for 5 patterns independent of CTL/SBS/TPO rules.
Provides a second opinion that validates or challenges Sheet signals.

Patterns:
  1. VOLUME_SURGE      — vol_ratio > 2.0 + price breaks 20D high
  2. RS_BREAKOUT       — stock beats Nifty by >5% in last 20D while Nifty is flat
  3. POST_CONSOLIDATION — consol_range < 5% for signal, now expanding with volume
  4. MEAN_REVERSION    — RSI 40-45 on stock with 6M return > 20% (dip in uptrend)
  5. DELIVERY_SURGE    — delivery_pct > 70% on a volume breakout day

Data retention: APPEND (new scanner signals added daily, never deleted)
"""
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger

# ── Pattern thresholds (all tunable) ─────────────────────────
THRESHOLDS = {
    "VOLUME_SURGE": {
        "min_vol_ratio":    2.0,
        "require_price_hi": True,    # price > 20D high
    },
    "RS_BREAKOUT": {
        "min_stock_ret_20d":    5.0,   # % above Nifty in 20D
        "max_nifty_ret_20d":    2.0,   # Nifty must be flat or down
    },
    "POST_CONSOLIDATION": {
        "max_consol_range":  5.0,      # tight consolidation
        "min_vol_ratio":     1.5,      # volume expansion
        "breakout_setup":    True,     # flag must be set
    },
    "MEAN_REVERSION": {
        "min_rsi_daily":     40.0,
        "max_rsi_daily":     48.0,
        "min_ret_6m":        15.0,     # must be in strong uptrend
        "min_dist_sma50":    -5.0,     # not more than 5% below SMA50
    },
    "DELIVERY_SURGE": {
        "min_delivery_pct":  70.0,
        "min_vol_ratio":     1.8,
    },
}

def to_f(v) -> float:
    try: return float(v or 0)
    except: return 0.0

def check_volume_surge(s: dict) -> tuple[bool, float, dict]:
    t = THRESHOLDS["VOLUME_SURGE"]
    vol_ratio = to_f(s.get("vol_ratio"))
    high_30d  = to_f(s.get("high_30d"))
    close     = to_f(s.get("close"))
    ok = vol_ratio >= t["min_vol_ratio"] and (not t["require_price_hi"] or close >= high_30d * 0.99)
    conf = min(1.0, vol_ratio / 4.0) if ok else 0.0
    return ok, conf, {"vol_ratio": vol_ratio, "close": close, "high_30d": high_30d}

def check_rs_breakout(s: dict, nifty_ret_20d: float) -> tuple[bool, float, dict]:
    t = THRESHOLDS["RS_BREAKOUT"]
    ret_1m = to_f(s.get("ret_1m"))  # proxy for 20D return
    stock_excess = ret_1m - nifty_ret_20d
    ok = stock_excess >= t["min_stock_ret_20d"] and nifty_ret_20d <= t["max_nifty_ret_20d"]
    conf = min(1.0, stock_excess / 15.0) if ok else 0.0
    return ok, conf, {"stock_ret_1m": ret_1m, "nifty_ret_20d": nifty_ret_20d, "excess": stock_excess}

def check_post_consol(s: dict) -> tuple[bool, float, dict]:
    t = THRESHOLDS["POST_CONSOLIDATION"]
    consol  = to_f(s.get("consol_range"))
    vol     = to_f(s.get("vol_ratio"))
    bk_flag = bool(s.get("breakout_setup"))
    ok = consol <= t["max_consol_range"] and vol >= t["min_vol_ratio"] and bk_flag
    conf = min(1.0, vol / 3.0 * (1 - consol / 10)) if ok else 0.0
    return ok, conf, {"consol_range": consol, "vol_ratio": vol, "breakout_setup": bk_flag}

def check_mean_reversion(s: dict) -> tuple[bool, float, dict]:
    t = THRESHOLDS["MEAN_REVERSION"]
    rsi   = to_f(s.get("rsi_daily"))
    ret6m = to_f(s.get("ret_6m"))
    dist  = to_f(s.get("dist_sma50"))
    ok = (t["min_rsi_daily"] <= rsi <= t["max_rsi_daily"] and
          ret6m >= t["min_ret_6m"] and dist >= t["min_dist_sma50"])
    conf = 0.6 + (t["max_rsi_daily"] - rsi) / 20 if ok else 0.0
    return ok, min(0.9, conf), {"rsi_daily": rsi, "ret_6m": ret6m, "dist_sma50": dist}

def check_delivery_surge(s: dict) -> tuple[bool, float, dict]:
    t = THRESHOLDS["DELIVERY_SURGE"]
    deliv = to_f(s.get("delivery_pct"))
    vol   = to_f(s.get("vol_ratio"))
    ok = deliv >= t["min_delivery_pct"] and vol >= t["min_vol_ratio"]
    conf = min(1.0, deliv / 100 * vol / 3) if ok else 0.0
    return ok, conf, {"delivery_pct": deliv, "vol_ratio": vol}

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — independent_scanner skipped")
        return {"status": "skipped", "reason": "kill_switch"}
    logger.info("Independent Scanner starting")
    sb = get_supabase()
    today = today_ist()

    # Load today's stock data
    stocks = sb.table("stock_data_daily").select(
        "symbol,close,high_30d,vol_ratio,consol_range,breakout_setup,"
        "rsi_daily,ret_1m,ret_6m,dist_sma50,delivery_pct,sector"
    ).eq("date", today.isoformat()).execute().data

    if not stocks:
        logger.warning("No stock data for today — run ingest_sheets first")
        return {"status": "no_data"}

    # Get Nifty 20D return proxy from regime table
    regime_rows = sb.table("market_regime").select("nifty_price") \
        .order("date", desc=True).limit(22).execute().data
    nifty_ret_20d = 0.0
    if len(regime_rows) >= 20:
        p_now  = float(regime_rows[0].get("nifty_price") or 0)
        p_20d  = float(regime_rows[19].get("nifty_price") or 0)
        if p_20d > 0:
            nifty_ret_20d = (p_now - p_20d) / p_20d * 100

    # Load existing MSL symbols for cross-reference
    msl_rows = sb.table("master_shortlist").select("symbol") \
        .eq("date", today.isoformat()).execute().data
    msl_symbols = {r["symbol"] for r in msl_rows}

    hits = []
    for s in stocks:
        sym = s.get("symbol", "")
        patterns = []

        ok, conf, det = check_volume_surge(s)
        if ok: patterns.append(("VOLUME_SURGE", conf, det))

        ok, conf, det = check_rs_breakout(s, nifty_ret_20d)
        if ok: patterns.append(("RS_BREAKOUT", conf, det))

        ok, conf, det = check_post_consol(s)
        if ok: patterns.append(("POST_CONSOLIDATION", conf, det))

        ok, conf, det = check_mean_reversion(s)
        if ok: patterns.append(("MEAN_REVERSION", conf, det))

        ok, conf, det = check_delivery_surge(s)
        if ok: patterns.append(("DELIVERY_SURGE", conf, det))

        for pattern, confidence, details in patterns:
            hits.append({
                "date":       today.isoformat(),
                "symbol":     sym,
                "pattern":    pattern,
                "confidence": round(confidence, 3),
                "details":    details,
                "also_in_msl": sym in msl_symbols,
            })

    if hits:
        # APPEND: insert new scanner signals
        for i in range(0, len(hits), 200):
            sb.table("scanner_signals").insert(hits[i:i+200]).execute()

    unique_syms = len({h["symbol"] for h in hits})
    logger.success(f"Scanner: {len(hits)} pattern hits across {unique_syms} stocks")
    return {"status": "ok", "hits": len(hits), "unique_stocks": unique_syms}

if __name__ == "__main__":
    main()