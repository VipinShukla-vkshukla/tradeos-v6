"""
TradeOS v6 — Independent Scanner v2.0 (Layer B)
================================================
WHY THIS SCRIPT STILL EXISTS after screen_stocks.py:
  screen_stocks writes to msl_computed/master_shortlist (stock SELECTION).
  This script writes to scanner_signals (generate_signals CROSS-REFERENCE).
  generate_signals._apply_scanner_crossref() reads scanner_signals to award
  +5 score_adjusted bonus to any stock confirmed by BOTH the rule engine AND
  this scanner. That cross-ref bonus is the unique value of this script.

  These two scripts are COMPLEMENTARY, not redundant:
    screen_stocks → msl_computed → compute_msl → master_shortlist → generate_signals
    independent_scanner → scanner_signals → generate_signals (+5 score_adjusted bonus)

PATTERNS v2 (upgraded to screen_stocks-level sophistication):
  1. VOLUME_BURST        — 3-7% single-session move + vol>2x + delivery>45% + above_sma50
  2. RS_LEADER_BREAKOUT  — rs>5 + consol<10% + approaching 30d high + above_sma50
  3. INSTITUTIONAL_COIL  — delivery>60% + volume_expanding(20d>50d) + consol<8% (quiet accumulation)
  4. TREND_PULLBACK      — RSI 42-54 + above_sma50 + macd turning + dist_sma50 < 5%
  5. DELIVERY_CONFIRM    — delivery>65% + vol>1.8 + above_sma50 + adx directional

All patterns now use consistent hard gates that align with compute_msl v2 logic.
Regime-awareness added: TREND_PULLBACK disabled in RISK OFF (bounces fail in bear markets).

Data retention: APPEND (new scanner signals added daily, never deleted)
"""
import sys, json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, logger

# ── Pattern thresholds ─────────────────────────────────────────────
THRESHOLDS = {
    "VOLUME_BURST": {
        "min_pct_change":    3.0,    # single-session move %
        "max_pct_change":   12.0,    # cap at 12% (circuits/news spikes excluded)
        "min_vol_ratio":     2.0,    # volume must be 2x+ average
        "min_delivery_pct": 45.0,    # institutional, not just intraday speculation
        "require_above_sma50": True,
    },
    "RS_LEADER_BREAKOUT": {
        "min_rs_vs_nifty":   5.0,    # outperforming Nifty by 5%+
        "max_consol_range": 10.0,    # must be in tight consolidation
        "max_pct_to_30d_high": 6.0,  # within 6% of 30d high (approaching resistance)
        "require_above_sma50": True,
        "min_rsi_daily":    48.0,    # trend must be positive
    },
    "INSTITUTIONAL_COIL": {
        "min_delivery_pct": 60.0,    # strong institutional signal
        "min_vol_ratio":     1.2,    # above average (vol_expanding catches the trend)
        "max_consol_range":  8.0,    # must be tight (coiling, not choppy)
        "require_above_sma50": True,
        "min_rsi_daily":    46.0,
    },
    "TREND_PULLBACK": {
        "min_rsi_daily":    42.0,    # not oversold
        "max_rsi_daily":    54.0,    # not yet recovered (entry opportunity)
        "max_dist_sma50":    5.0,    # within 5% of SMA50 (not broken down)
        "require_above_sma50": True,
        "min_rsi_weekly":   50.0,    # weekly trend intact
        "disabled_in_risk_off": True,
    },
    "DELIVERY_CONFIRM": {
        "min_delivery_pct": 65.0,    # strong institutional delivery
        "min_vol_ratio":     1.8,    # volume confirming
        "require_above_sma50": True,
        "min_adx":          15.0,    # some directional trend
        "require_di_plus":  True,    # di_plus > di_minus (buyers in control)
    },
}


def to_f(v) -> float:
    try: return float(v or 0)
    except: return 0.0


def check_volume_burst(s: dict) -> tuple:
    t = THRESHOLDS["VOLUME_BURST"]
    pct_chg  = to_f(s.get("pct_change"))
    vol_r    = to_f(s.get("vol_ratio"))
    delivery = to_f(s.get("delivery_pct"))
    above_50 = bool(s.get("above_sma50"))
    consol   = to_f(s.get("consol_range") or 999)

    ok = (t["min_pct_change"] <= pct_chg <= t["max_pct_change"]
          and vol_r >= t["min_vol_ratio"]
          and delivery >= t["min_delivery_pct"]
          and (not t["require_above_sma50"] or above_50))

    # Confidence: higher for tighter consolidation breakout + stronger delivery
    if ok:
        conf = 0.0
        conf += min(0.30, pct_chg / 20)        # move size contribution
        conf += min(0.25, vol_r / 8)            # volume contribution
        conf += min(0.25, delivery / 100)       # delivery contribution
        conf += 0.20 if consol < 8 else 0.10   # breakout from base bonus
    else:
        conf = 0.0

    det = {"pct_change": pct_chg, "vol_ratio": vol_r, "delivery_pct": delivery,
           "consol_range": consol, "above_sma50": above_50}
    return ok, round(min(conf, 1.0), 3), det


def check_rs_leader_breakout(s: dict) -> tuple:
    t        = THRESHOLDS["RS_LEADER_BREAKOUT"]
    rs       = to_f(s.get("rs_vs_nifty"))
    consol   = to_f(s.get("consol_range") or 999)
    high_30d = to_f(s.get("high_30d"))
    close    = to_f(s.get("close") or s.get("current_price"))
    above_50 = bool(s.get("above_sma50"))
    rsi_d    = to_f(s.get("rsi_daily"))

    pct_to_high = ((high_30d - close) / high_30d * 100) if (high_30d > 0 and close > 0) else 999

    ok = (rs >= t["min_rs_vs_nifty"]
          and consol <= t["max_consol_range"]
          and pct_to_high <= t["max_pct_to_30d_high"]
          and (not t["require_above_sma50"] or above_50)
          and rsi_d >= t["min_rsi_daily"])

    if ok:
        conf = min(0.4, rs / 30)               # RS quality
        conf += 0.25 if consol < 5 else (0.15 if consol < 8 else 0.08)
        conf += 0.25 if pct_to_high < 2 else (0.15 if pct_to_high < 4 else 0.08)
        conf += 0.10 if rsi_d > 55 else 0.05
    else:
        conf = 0.0

    det = {"rs_vs_nifty": rs, "consol_range": consol,
           "pct_to_30d_high": round(pct_to_high, 2), "rsi_daily": rsi_d}
    return ok, round(min(conf, 1.0), 3), det


def check_institutional_coil(s: dict) -> tuple:
    """
    Detects quiet institutional accumulation in a tight consolidation.
    Fires 2-5 sessions BEFORE a breakout — the highest-value early signal.
    Volume expansion (20d avg > 50d avg) is the key differentiator from random tight ranges.
    """
    t        = THRESHOLDS["INSTITUTIONAL_COIL"]
    delivery = to_f(s.get("delivery_pct"))
    vol_r    = to_f(s.get("vol_ratio"))
    consol   = to_f(s.get("consol_range") or 999)
    above_50 = bool(s.get("above_sma50"))
    rsi_d    = to_f(s.get("rsi_daily"))
    vol_20d  = to_f(s.get("avg_vol_20d"))
    vol_50d  = to_f(s.get("avg_vol_50d"))

    # Volume expanding = sustained accumulation (not just today's spike)
    vol_expanding = (vol_20d > vol_50d * 1.08) if (vol_20d > 0 and vol_50d > 0) else False

    ok = (delivery >= t["min_delivery_pct"]
          and vol_r >= t["min_vol_ratio"]
          and consol <= t["max_consol_range"]
          and (not t["require_above_sma50"] or above_50)
          and rsi_d >= t["min_rsi_daily"])

    if ok:
        conf = min(0.40, delivery / 100)        # delivery is primary signal
        conf += 0.25 if vol_expanding else 0.10  # sustained > single-day
        conf += 0.20 if consol < 5 else (0.12 if consol < 7 else 0.06)
        conf += 0.15 if rsi_d >= 52 else 0.05
    else:
        conf = 0.0

    det = {"delivery_pct": delivery, "vol_ratio": vol_r, "consol_range": consol,
           "vol_expanding": vol_expanding, "rsi_daily": rsi_d}
    return ok, round(min(conf, 1.0), 3), det


def check_trend_pullback(s: dict, regime: str) -> tuple:
    """
    RSI dipping to 42-54 in a confirmed uptrend — the 'buy the dip' setup.
    Requires MACD histogram turning (not necessarily positive yet).
    DISABLED in RISK OFF — bounces fail frequently in bear markets.
    """
    t = THRESHOLDS["TREND_PULLBACK"]
    if t.get("disabled_in_risk_off") and regime == "RISK OFF":
        return False, 0.0, {"disabled": "RISK OFF regime"}

    rsi_d    = to_f(s.get("rsi_daily"))
    rsi_w    = to_f(s.get("rsi_weekly"))
    above_50 = bool(s.get("above_sma50"))
    dist_50  = to_f(s.get("dist_sma50"))
    macd_h   = to_f(s.get("macd_hist"))
    sma200   = bool(s.get("sma50_gt_200"))

    ok = (t["min_rsi_daily"] <= rsi_d <= t["max_rsi_daily"]
          and (not t["require_above_sma50"] or above_50)
          and abs(dist_50) <= t["max_dist_sma50"]
          and rsi_w >= t["min_rsi_weekly"]
          and sma200)  # golden cross required — ensures quality base

    if ok:
        # MACD turning up (even if still negative) = early reversal
        macd_conf = 0.25 if macd_h > 0 else (0.18 if macd_h > -0.3 else (0.10 if macd_h > -1 else 0.04))
        # RSI sweet spot: 46-52 is ideal for entry
        rsi_conf  = 0.25 if 46 <= rsi_d <= 52 else (0.18 if rsi_d < 46 else 0.12)
        dist_conf = 0.25 if abs(dist_50) < 2 else (0.18 if abs(dist_50) < 3.5 else 0.10)
        conf = macd_conf + rsi_conf + dist_conf + (0.10 if rsi_w >= 55 else 0.04)
    else:
        conf = 0.0

    det = {"rsi_daily": rsi_d, "rsi_weekly": rsi_w, "dist_sma50": dist_50,
           "macd_hist": macd_h, "sma50_gt_200": sma200}
    return ok, round(min(conf, 1.0), 3), det


def check_delivery_confirm(s: dict) -> tuple:
    """
    High delivery on above-average volume WITH a directional trend.
    Key upgrade from v1: adds ADX directional filter (di_plus > di_minus).
    Prevents false signals from high delivery on a stock in a downtrend.
    """
    t        = THRESHOLDS["DELIVERY_CONFIRM"]
    delivery = to_f(s.get("delivery_pct"))
    vol_r    = to_f(s.get("vol_ratio"))
    above_50 = bool(s.get("above_sma50"))
    adx      = to_f(s.get("adx"))
    di_plus  = to_f(s.get("di_plus"))
    di_minus = to_f(s.get("di_minus"))

    ok = (delivery >= t["min_delivery_pct"]
          and vol_r >= t["min_vol_ratio"]
          and (not t["require_above_sma50"] or above_50)
          and adx >= t["min_adx"]
          and (not t["require_di_plus"] or di_plus > di_minus))

    if ok:
        conf  = min(0.40, delivery / 100)
        conf += min(0.30, vol_r / 5)
        conf += 0.20 if adx >= 25 else (0.12 if adx >= 18 else 0.06)
        conf += 0.10 if (di_plus - di_minus) >= 8 else 0.04
    else:
        conf = 0.0

    det = {"delivery_pct": delivery, "vol_ratio": vol_r, "adx": adx,
           "di_spread": round(di_plus - di_minus, 1), "above_sma50": above_50}
    return ok, round(min(conf, 1.0), 3), det


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — independent_scanner skipped")
        return {"status": "skipped"}

    logger.info("Independent Scanner v2 starting")
    sb    = get_supabase()
    today = today_ist()

    # Load today's stock data — all fields needed for v2 patterns
    stocks = sb.table("stock_data_daily").select(
        "symbol,close,high_30d,vol_ratio,avg_vol_20d,avg_vol_50d,"
        "consol_range,breakout_setup,rsi_daily,rsi_weekly,"
        "ret_1m,ret_6m,dist_sma50,delivery_pct,sector,"
        "above_sma50,sma50_gt_200,adx,di_plus,di_minus,"
        "macd_hist,pct_change,rs_vs_nifty"
    ).eq("date", today.isoformat()).execute().data

    if not stocks:
        logger.warning("No stock data for today")
        return {"status": "no_data"}

    # Get current regime for TREND_PULLBACK gate
    regime_rows = sb.table("market_regime").select("regime,predicted_regime").order("date", desc=True).limit(1).execute().data
    regime = "NEUTRAL"
    if regime_rows:
        r = regime_rows[0]
        regime = r.get("predicted_regime") or r.get("regime") or "NEUTRAL"

    # MSL cross-reference
    msl_rows = sb.table("master_shortlist").select("symbol").eq("date", today.isoformat()).execute().data
    msl_symbols = {r["symbol"] for r in msl_rows}

    logger.info(f"  Scanning {len(stocks)} stocks | regime={regime} | msl={len(msl_symbols)}")

    hits = []
    pattern_counts = {"VOLUME_BURST": 0, "RS_LEADER_BREAKOUT": 0,
                      "INSTITUTIONAL_COIL": 0, "TREND_PULLBACK": 0, "DELIVERY_CONFIRM": 0}

    for s in stocks:
        sym = s.get("symbol", "")
        market_cap = to_f(s.get("market_cap"))
        if market_cap > 0 and market_cap < 300:    # skip micro-caps
            continue

        patterns_found = []

        ok, conf, det = check_volume_burst(s)
        if ok: patterns_found.append(("VOLUME_BURST", conf, det))

        ok, conf, det = check_rs_leader_breakout(s)
        if ok: patterns_found.append(("RS_LEADER_BREAKOUT", conf, det))

        ok, conf, det = check_institutional_coil(s)
        if ok: patterns_found.append(("INSTITUTIONAL_COIL", conf, det))

        ok, conf, det = check_trend_pullback(s, regime)
        if ok: patterns_found.append(("TREND_PULLBACK", conf, det))

        ok, conf, det = check_delivery_confirm(s)
        if ok: patterns_found.append(("DELIVERY_CONFIRM", conf, det))

        for pattern, confidence, details in patterns_found:
            pattern_counts[pattern] += 1
            hits.append({
                "date":          today.isoformat(),
                "symbol":        sym,
                "pattern_type":  pattern,
                "confidence":    confidence,
                "details":       details,
                "in_msl":        sym in msl_symbols,
            })

    if hits:
        # APPEND: insert new scanner signals (never delete historical data)
        for i in range(0, len(hits), 200):
            sb.table("scanner_signals").insert(hits[i:i+200]).execute()

    unique_syms = len({h["symbol"] for h in hits})
    in_msl_hits = sum(1 for h in hits if h["in_msl"])

    logger.success(
        f"Scanner v2: {len(hits)} pattern hits | {unique_syms} unique stocks | "
        f"{in_msl_hits} also in MSL (will get +5 score_adjusted in generate_signals)"
    )
    logger.info(f"  Pattern breakdown: {pattern_counts}")

    # Log high-confidence hits that are in MSL (most actionable)
    high_conf_msl = [h for h in hits if h["in_msl"] and h["confidence"] >= 0.7]
    if high_conf_msl:
        syms = list({h["symbol"] for h in high_conf_msl})[:8]
        logger.info(f"  High-confidence MSL hits (conf≥0.70): {syms}")

    return {
        "status": "ok",
        "hits": len(hits),
        "unique_stocks": unique_syms,
        "in_msl": in_msl_hits,
        "pattern_counts": pattern_counts,
    }


if __name__ == "__main__":
    main()