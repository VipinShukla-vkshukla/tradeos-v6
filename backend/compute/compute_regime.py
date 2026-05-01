"""
TradeOS v6 — Compute Regime v2.0
==================================
Pipeline position: [10.4] — after compute_indicators, before screen_stocks

CHANGES FROM v1.0
=================
BUG FIXES:
  1. yfinance ran on calendar date (Sunday) → now resolves effective trading date FIRST
     before any data fetch. All table queries use effective_date, not today.
  2. Price structure scoring 0/25 → was caused by None fields from yfinance on holiday.
     Fixed by resolving effective_date upfront + try/except on yfinance calls.
  3. FII flows scoring 0/15 → net_20d/net_5d were 0.0 (not None) so fallback never
     triggered. Fixed by treating 0.0 as valid zero (not null) and scoring correctly.
     Cascade fallback now explicit: net_20d → net_10d → computed from dailies → flag.
  4. Snapshot fields stored redundantly — build_market_snapshot now returns a clean
     dict used only to populate today_row. No direct Supabase writes from snapshot.
     The write happens once in write_regime() only.
  5. Removed duplicate assignment: raw_20d and net_5d_raw were assigned but never used.

IMPROVEMENTS:
  6. VIX direction (delta) added to score_volatility — VIX at 18 falling from 22
     is VERY different from VIX at 18 rising from 16. ±3pt adjustment.
  7. GIFT Nifty now scored in score_momentum — loaded but unused in v1. ±2pt.
  8. DII counter-flow added to score_fii — DII buying when FII sells = floor support.
     Adjusts FII score by up to ±3pts.
  9. Smallcap breadth added to score_breadth — Nifty50 can be up while smallcaps
     in freefall (Jan 2024 scenario). Separate signal, not folded into midcap.
  10. USD/INR direction added as regime modifier — rupee weakening amplifies FII
      outflows and raises effective risk. ±2pt adjustment on final score.
  11. Global cue (S&P 500 direction) added — Indian market 80% correlated in risk-off.
      Fetched via yfinance, ±3pt adjustment.
  12. banknifty_weekly_rsi adjustment was +/- delta added to P1 pts — now correctly
      bounded so P1 cannot exceed 25.

ARCHITECTURE:
  - Snapshot is a pure data collection function — returns dict, no Supabase writes.
  - effective_date resolved once in main() and threaded through all functions.
  - All pillar scores are now bounded individually before summing.
  - Hysteresis engine unchanged — logic was sound, data inputs were broken.

NEW 5-STATE MODEL (unchanged from v1):
  TRENDING   → All cylinders firing. Strong bull market.
  RISK ON    → Positive trend with some mixed signals.
  NEUTRAL    → Neither bull nor bear. Selective positioning.
  RECOVERING → Coming out of bear. Bounce confirmed but not yet neutral.
  RISK OFF   → Bear conditions. Capital preservation.

SCORING MODEL (0–100):
  Pillar 1: Price Structure  25 pts — Nifty vs 50DMA/200DMA, golden cross
  Pillar 2: Breadth          25 pts — above_200dma, sector breadth, AD ratio, midcap, smallcap
  Pillar 3: Momentum         20 pts — weekly RSI, 20d/5d return, GIFT Nifty
  Pillar 4: Volatility       15 pts — India VIX level + VIX direction delta
  Pillar 5: FII Flows        15 pts — fii_net_20d, fii_net_5d, DII counter-flow
  Pillar 6:Global modifier:  ±5 pts — S&P500 direction, USD/INR direction
"""

import sys, os, json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, IST, cfg, cfg_bool, is_kill_switch_active, DRY_RUN

try:
    import yfinance as yf
    import pandas as pd
    YFINANCE_AVAILABLE = True
except ImportError:
    logger.warning("yfinance not installed — index data will use Supabase fallback only")
    YFINANCE_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# SCORE THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────

SCORE_TRENDING   = 78
SCORE_RISK_ON    = 60
SCORE_NEUTRAL    = 40
SCORE_RECOVERING = 25


# ─────────────────────────────────────────────────────────────────────────────
# EFFECTIVE DATE RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────

def resolve_effective_date(sb, today: str) -> tuple[str, bool]:
    """
    Resolve the last actual trading day and ensure a row exists for it.

    Priority:
    1. stock_data_daily MAX(date) — bhavcopy is ground truth (runs before this step)
    2. yfinance ^NSEI — fallback if bhavcopy hasn't run yet
    3. market_regime DB — last resort
    """
    # ── Step 1: stock_data_daily — bhavcopy ground truth ─────────────────────
    last_trading = None
    try:
        rows = (sb.table("stock_data_daily")
                  .select("date")
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(1)
                  .execute().data)
        if rows:
            last_trading = rows[0]["date"]
            logger.info(f"  stock_data_daily last trading date: {last_trading}")
    except Exception as e:
        logger.warning(f"  stock_data_daily date resolution failed: {e}")

    # ── Step 2: yfinance fallback ─────────────────────────────────────────────
    if not last_trading and YFINANCE_AVAILABLE:
        try:
            df = yf.Ticker("^NSEI").history(period="5d", interval="1d")
            if not df.empty:
                last_trading = str(df.index[-1].date())
                logger.info(f"  yfinance last trading date: {last_trading}")
        except Exception as e:
            logger.warning(f"  yfinance date resolution failed: {e}")

    # ── Step 3: market_regime DB fallback ────────────────────────────────────
    if not last_trading:
        latest = (sb.table("market_regime")
                    .select("date")
                    .order("date", desc=True)
                    .limit(1)
                    .execute().data)
        if latest:
            last_trading = latest[0]["date"]
            logger.info(f"  DB fallback date: {last_trading}")
        else:
            last_trading = today
            logger.warning(f"  No DB rows found — using today: {today}")

    is_trading = (last_trading == today)
    if not is_trading:
        logger.info(f"  {today} is non-trading — effective date is {last_trading}")

    # ── Step 4: Ensure row exists for last_trading (unchanged) ───────────────
    existing = (sb.table("market_regime")
                  .select("date")
                  .eq("date", last_trading)
                  .limit(1)
                  .execute().data)

    if not existing:
        logger.info(f"  No row found for {last_trading} — creating placeholder")
        try:
            sb.table("market_regime").upsert(
                {"date": last_trading},
                on_conflict="date"
            ).execute()
            logger.info(f"  ✓ Placeholder row created for {last_trading}")
        except Exception as e:
            logger.warning(f"  Failed to create placeholder row: {e}")

    return last_trading, is_trading


# ─────────────────────────────────────────────────────────────────────────────
# DATA COLLECTION — pure data fetch, no Supabase writes
# ─────────────────────────────────────────────────────────────────────────────

def fetch_index_data(ticker: str) -> dict | None:
    """
    Fetch index price, DMAs, weekly RSI via yfinance.
    Wrapped in try/except — yfinance can fail silently on holidays or poor connections.

    FIX: v1 had no error handling here. If yfinance returned empty on a Sunday,
    nifty_price/dma fields remained None and score_price_structure returned 0.
    """
    if not YFINANCE_AVAILABLE:
        return None
    try:
        df = yf.Ticker(ticker).history(period="2y", interval="1d")
        if df.empty:
            logger.warning(f"  yfinance: empty response for {ticker}")
            return None

        closes = df["Close"].dropna()
        if len(closes) < 10:
            logger.warning(f"  yfinance: insufficient data for {ticker} ({len(closes)} rows)")
            return None

        def dma(n):
            return float(closes.tail(n).mean()) if len(closes) >= n else None

        def weekly_rsi(period=14):
            weekly = closes.resample("W-FRI").last().dropna()
            if len(weekly) < period + 1:
                return None
            delta   = weekly.diff().dropna()
            gain    = delta.clip(lower=0).rolling(period).mean().iloc[-1]
            loss    = (-delta.clip(upper=0)).rolling(period).mean().iloc[-1]
            if loss == 0:
                return 100.0
            return round(100 - (100 / (1 + gain / loss)), 2)

        # 20d and 5d returns for momentum
        ret_20d = None
        ret_5d  = None
        ret_1d = None
        if len(closes) >= 2:
            ret_1d = round((float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100, 2)
        if len(closes) >= 21:
            ret_20d = round((float(closes.iloc[-1]) / float(closes.iloc[-21]) - 1) * 100, 2)
        if len(closes) >= 6:
            ret_5d = round((float(closes.iloc[-1]) / float(closes.iloc[-6]) - 1) * 100, 2)

        return {
            "price":      float(closes.iloc[-1]),
            "dma50":      dma(50),
            "dma200":     dma(200),
            "weekly_rsi": weekly_rsi(),
            "ret_1d":     ret_1d,
            "ret_20d":    ret_20d,
            "ret_5d":     ret_5d,
        }
    except Exception as e:
        logger.warning(f"  yfinance failed for {ticker}: {e}")
        return None


def fetch_sp500_direction() -> float | None:
    """
    S&P 500 5-day return — global risk signal.
    Indian market is 80% correlated with US in risk-off environments.
    NEW in v2: global cue scoring.
    """
    data = fetch_index_data("^GSPC")
    return data["ret_5d"] if data else None


def fetch_usdinr_direction(sb) -> float | None:
    """
    USD/INR direction from macro_indicators table.
    Rupee weakening = FII outflows amplified = effective risk higher.
    Returns recent % change (positive = rupee weakening = bad for market).
    NEW in v2.
    """
    try:
        rows = (sb.table("macro_indicators")
                  .select("indicator_value,indicator_date")
                  .eq("indicator_name", "USD_INR")
                  .order("indicator_date", desc=True)
                  .limit(6)
                  .execute().data)
        if len(rows) >= 2:
            recent = float(rows[0]["indicator_value"])
            older  = float(rows[-1]["indicator_value"])
            return round((recent - older) / older * 100, 3) if older > 0 else None
    except Exception as e:
        logger.warning(f"  USD/INR fetch failed: {e}")
    return None


def build_market_snapshot(sb, effective_date: str) -> dict:
    """
    Collect all market data into a single dict.
    Pure data collection — no Supabase writes.

    FIX v1: snapshot was built with calendar date, causing empty results
    for sector/stock table queries on holidays. Now uses effective_date.

    FIX v1: fields were being treated as if they needed to be written
    separately to Supabase. The snapshot is just a data container —
    write_regime() handles all persistence in one place.
    """
    snap = {}

    # ── Nifty + BankNifty via yfinance ────────────────────────────────────────
    nifty = fetch_index_data("^NSEI")
    bank  = fetch_index_data("^NSEBANK")

    snap.update({
        "nifty_price":         nifty["price"]      if nifty else None,
        "nifty_50dma":         nifty["dma50"]       if nifty else None,
        "nifty_200dma":        nifty["dma200"]      if nifty else None,
        "nifty_weekly_rsi":    nifty["weekly_rsi"]  if nifty else None,
        "nifty_20d_chg_pct":   nifty["ret_20d"]     if nifty else None,
        "nifty_5d_chg_pct":    nifty["ret_5d"]      if nifty else None,
        "banknifty_price":     bank["price"]         if bank else None,
        "banknifty_weekly_rsi":bank["weekly_rsi"]    if bank else None,
        "nifty_1d_chg_pct":    nifty["ret_1d"]  if nifty else None,
    })

    logger.info(
        f"  BankNifty: price={snap.get('banknifty_price') or 'N/A'} | "
        f"weekly_rsi={snap.get('banknifty_weekly_rsi') or 'N/A'} | "
        f"Nifty weekly_rsi={snap.get('nifty_weekly_rsi') or 'N/A'}"
    )

    if not nifty:
        logger.warning("  yfinance Nifty fetch failed — P1 will rely on DB fallback")

    # ── Sector breadth ────────────────────────────────────────────────────────
    try:
        sector_rows = (sb.table("sector_strength")
                         .select("breadth_sma50")
                         .eq("date", effective_date)
                         .execute().data)
        vals = [float(r["breadth_sma50"]) for r in sector_rows
                if r.get("breadth_sma50") is not None]
        snap["avg_sector_breadth"] = round(sum(vals) / len(vals), 2) if vals else None
    except Exception as e:
        logger.warning(f"  sector_strength fetch failed: {e}")
        snap["avg_sector_breadth"] = None

    # ── Stock-level breadth signals ───────────────────────────────────────────
    try:
        stock_rows = (sb.table("stock_data_daily")
                        .select("market_cap_category,close,sma_50,sma_200,pct_change")
                        .eq("date", effective_date)
                        .execute().data)

        # above_200dma breadth
        valid_200 = [r for r in stock_rows if r.get("close") and r.get("sma_200")]
        if valid_200:
            above = sum(1 for r in valid_200 if float(r["close"]) > float(r["sma_200"]))
            snap["above_200dma_pct"] = round(above / len(valid_200) * 100, 2)
        else:
            snap["above_200dma_pct"] = None

        # Advance/decline ratio
        changes = [float(r["pct_change"]) for r in stock_rows if r.get("pct_change") is not None]
        adv = sum(1 for x in changes if x > 0)
        dec = sum(1 for x in changes if x < 0)
        snap["advance_decline_ratio"] = round(adv / dec, 2) if dec > 0 else (99.0 if adv > 0 else None)

        # Midcap breadth (above SMA50)
        mid   = [r for r in stock_rows if r.get("market_cap_category") == "midcap"]
        if mid:
            ab = sum(1 for r in mid if r.get("close") and r.get("sma_50")
                     and float(r["close"]) > float(r["sma_50"]))
            snap["midcap_breadth"] = round(ab / len(mid) * 100, 2)
        else:
            snap["midcap_breadth"] = None

        # NEW v2: Smallcap breadth (above SMA50)
        # Nifty can be up while smallcaps in freefall — this catches that divergence
        small = [r for r in stock_rows if r.get("market_cap_category") == "smallcap"]
        if small:
            ab = sum(1 for r in small if r.get("close") and r.get("sma_50")
                     and float(r["close"]) > float(r["sma_50"]))
            snap["smallcap_breadth"] = round(ab / len(small) * 100, 2)
        else:
            snap["smallcap_breadth"] = None

    except Exception as e:
        logger.warning(f"  stock breadth fetch failed: {e}")
        snap.update({"above_200dma_pct": None, "advance_decline_ratio": None,
                     "midcap_breadth": None, "smallcap_breadth": None})

    # ── VIX (primary: macro_indicators) ──────────────────────────────────────
    try:
        vix_rows = (sb.table("macro_indicators")
                      .select("indicator_value,indicator_date")
                      .eq("indicator_name", "INDIA_VIX")
                      .order("indicator_date", desc=True)
                      .limit(6)
                      .execute().data)
        if vix_rows:
            snap["india_vix"] = float(vix_rows[0]["indicator_value"])
            # VIX direction: today vs 5 days ago
            # NEW v2: VIX at 18 falling from 22 ≠ VIX at 18 rising from 16
            if len(vix_rows) >= 5:
                older_vix = float(vix_rows[4]["indicator_value"])
                snap["vix_5d_delta"] = round(snap["india_vix"] - older_vix, 2)
            else:
                snap["vix_5d_delta"] = None
        else:
            snap["india_vix"]    = None
            snap["vix_5d_delta"] = None
        logger.info(
            f"  VIX: current={snap.get('india_vix')} | "
            f"rows_fetched={len(vix_rows)} | "
            f"vix_5d_delta={snap.get('vix_5d_delta')} | "
            f"{'⚠ need 5 rows for delta' if len(vix_rows) < 5 else '✓ delta computed'}"
        )
    except Exception as e:
        logger.warning(f"  VIX fetch failed: {e}")
        snap["india_vix"]    = None
        snap["vix_5d_delta"] = None

    # ── GIFT Nifty ────────────────────────────────────────────────────────────
    try:
        gift_rows = (sb.table("global_cues")
                    .select("gift_nifty")
                    .eq("date", effective_date)
                    .limit(1)
                    .execute().data)

        if gift_rows and gift_rows[0].get("gift_nifty"):
            gn = float(gift_rows[0]["gift_nifty"])
            snap["gift_nifty"] = gn

            # Fallback: prev close from market_regime instead of global_cues.nifty_prev_close
            prev_row = (sb.table("market_regime")
                        .select("nifty_price")
                        .lt("date", effective_date)
                        .not_.is_("nifty_price", "null")
                        .order("date", desc=True)
                        .limit(1)
                        .execute().data)

            if prev_row and prev_row[0].get("nifty_price"):
                pc = float(prev_row[0]["nifty_price"])
                snap["gift_premium_pct"] = round((gn - pc) / pc * 100, 2)
                logger.info(f"  GIFT Nifty={gn} | prev_close={pc} | premium={snap['gift_premium_pct']}%")
            else:
                snap["gift_premium_pct"] = None
                logger.warning("  GIFT Nifty: prev close not found in market_regime — premium not computed")
        else:
            snap["gift_nifty"]       = None
            snap["gift_premium_pct"] = None

    except Exception as e:
        logger.warning(f"  GIFT Nifty fetch failed: {e}")
        snap["gift_nifty"]       = None
        snap["gift_premium_pct"] = None

    try:
        pcr_rows = (sb.table("macro_indicators")
                    .select("indicator_value,indicator_date")
                    .eq("indicator_name", "NIFTY_PCR")
                    .order("indicator_date", desc=True)
                    .limit(1)
                    .execute().data)
        snap["nifty_pcr"] = float(pcr_rows[0]["indicator_value"]) if pcr_rows else None
    except Exception as e:
        logger.warning(f"  PCR fetch failed: {e}")
        snap["nifty_pcr"] = None

    # ── Global cues (S&P500, USD/INR) — NEW v2 ───────────────────────────────
    snap["sp500_5d_ret"]   = fetch_sp500_direction()
    snap["usdinr_5d_chg"]  = fetch_usdinr_direction(sb)

    return snap


def load_data(sb, effective_date: str) -> dict:
    """
    Load all inputs. effective_date is pre-resolved — no calendar date usage here.

    FIX v1: was called with calendar date. All table queries now use effective_date.
    """
    # Build snapshot (data only, no writes)
    snapshot = build_market_snapshot(sb, effective_date)

    # Merge with last populated market_regime row for any missing fields
    # (e.g. if yfinance failed, pull nifty_price from last DB row)
    latest = (sb.table("market_regime")
                .select("*")
                .not_.is_("nifty_price", "null")
                .order("date", desc=True)
                .limit(1)
                .execute().data)

    today_row = {}
    if latest:
        today_row = dict(latest[0])
        logger.info(f"  DB fallback from: {latest[0]['date']}")

    # Snapshot values take priority (fresher), DB fills gaps
    for k, v in snapshot.items():
        if v is not None:
            today_row[k] = v

    # Resolve VIX with explicit fallback chain
    vix = today_row.get("india_vix")
    if vix:
        vix_source = "macro_indicators"
    else:
        vix = float(today_row.get("india_vix") or 0)
        vix_source = "market_regime(DB fallback)"

    # History (for hysteresis)
    history = (sb.table("market_regime")
                 .select("date,regime,computed_regime,regime_score_computed,nifty_price,"
                         "above_200dma_pct,avg_sector_breadth,india_vix,nifty_5d_chg_pct,"
                         "advance_decline_ratio")
                 .order("date", desc=True)
                 .limit(12)
                 .execute().data)
    history = [r for r in history if r.get("date") != effective_date][:10]

    # FII/DII data
    fii_rows = (sb.table("fii_dii_flow")
                  .select("fii_net_cr,dii_net,fii_net_5d,fii_net_10d,fii_net_20d,"
                          "dii_net_5d,dii_net_20d,fii_flag,date")
                  .order("date", desc=True)
                  .limit(5)
                  .execute().data)
    fii = fii_rows[0] if fii_rows else {}

    logger.info(
        f"  Loaded: regime_history={len(history)} days | "
        f"VIX={vix} ({vix_source}) | "
        f"FII flag={fii.get('fii_flag','?')} | "
        f"Nifty={today_row.get('nifty_price','?')} | "
        f"above200dma={today_row.get('above_200dma_pct','?')}%"
    )

    return {
        "today_row":    today_row,
        "history":      history,
        "fii":          fii,
        "fii_history":  fii_rows,
        "vix":          vix,
        "vix_5d_delta": today_row.get("vix_5d_delta"),
        "vix_source":   vix_source,
        "sp500_5d_ret": today_row.get("sp500_5d_ret"),
        "usdinr_5d_chg":today_row.get("usdinr_5d_chg"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# PILLAR SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_price_structure(row: dict) -> tuple[float, dict]:
    """
    25 pts: Nifty position relative to key MAs.

    Sub-components:
      vs 50DMA  (8 pts): short-term trend health
      vs 200DMA (10 pts): primary trend direction
      Golden/death cross (7 pts): institutional positioning
      BankNifty RSI adjustment (±3 pts, bounded): banking sector confirmation

    BankNifty drives Nifty in India (40%+ weight). Its RSI is a quality check
    on whether the price structure is supported by the largest sector.

    FIX v2: BankNifty adjustment now bounded so P1 cannot exceed 25.
    """
    nifty  = float(row.get("nifty_price") or 0)
    dma50  = float(row.get("nifty_50dma") or 0)
    dma200 = float(row.get("nifty_200dma") or 0)

    pts = 0.0
    breakdown = {}

    if not nifty:
        logger.warning("  P1: nifty_price is None — price structure score will be 0")
        return 0.0, {"error": "nifty_price missing"}

    # vs 50DMA (8 pts)
    if dma50 > 0:
        dist50 = (nifty - dma50) / dma50 * 100
        if dist50 > 3:      p = 8
        elif dist50 > 0:    p = 5
        elif dist50 > -2:   p = 2
        elif dist50 > -5:   p = 1
        else:               p = 0
        pts += p
        breakdown["vs_50dma"] = {"dist_pct": round(dist50, 2), "pts": p}

    # vs 200DMA (10 pts)
    if dma200 > 0:
        dist200 = (nifty - dma200) / dma200 * 100
        if dist200 > 5:      p = 10
        elif dist200 > 1:    p = 7
        elif dist200 > -2:   p = 3
        elif dist200 > -6:   p = 1
        else:                p = 0
        pts += p
        breakdown["vs_200dma"] = {"dist_pct": round(dist200, 2), "pts": p}

    # Golden/death cross — 50DMA vs 200DMA (7 pts)
    if dma50 > 0 and dma200 > 0:
        cross = (dma50 - dma200) / dma200 * 100
        if cross > 2:    p = 7
        elif cross > 0:  p = 4
        elif cross > -1: p = 2
        else:            p = 0
        pts += p
        breakdown["golden_cross"] = {"dist_pct": round(cross, 2), "pts": p}

    # BankNifty RSI adjustment — bounded ±3, and total P1 capped at 25
    bank_rsi = row.get("banknifty_weekly_rsi")
    if bank_rsi is not None:
        bank_rsi = float(bank_rsi)
        if bank_rsi < 40:    delta = -3
        elif bank_rsi < 45:  delta = -2
        elif bank_rsi < 50:  delta = -1
        elif bank_rsi >= 65: delta = 2
        elif bank_rsi >= 58: delta = 1
        else:                delta = 0
        pts += delta
        breakdown["banknifty_rsi"] = {"rsi": round(bank_rsi, 1), "pts": delta}

    # Hard cap at 25
    pts = min(pts, 25.0)
    return round(max(pts, 0.0), 1), breakdown


def score_breadth(row: dict) -> tuple[float, dict]:
    """
    25 pts: Market participation quality.

    Sub-components:
      above_200dma_pct  (12 pts): core breadth signal
      avg_sector_breadth (8 pts): cross-sector participation
      advance_decline    (5 pts): daily market health
      midcap adjustment  (±4 pts): midcap vs largecap divergence
      smallcap adjustment(±3 pts, NEW v2): smallcap is leading indicator

    Jan 2024 lesson: Nifty50 +8% while midcap/smallcap -12% = not a bull market.
    Breadth exposes index distortion. Smallcap is the earliest warning.
    """
    above_200 = float(row.get("above_200dma_pct") or 0)
    sector_b  = float(row.get("avg_sector_breadth") or 0)
    ad_ratio  = float(row.get("advance_decline_ratio") or 0)
    midcap_b  = row.get("midcap_breadth")
    smallcap_b = row.get("smallcap_breadth")

    pts = 0.0
    breakdown = {}

    # % stocks above 200DMA (12 pts)
    if above_200 > 0:
        if above_200 >= 65:   p = 12
        elif above_200 >= 55: p = 9
        elif above_200 >= 45: p = 6
        elif above_200 >= 35: p = 3
        elif above_200 >= 25: p = 1
        else:                 p = 0
        pts += p
        breakdown["above_200dma_pct"] = {"value": above_200, "pts": p}

    # Sector breadth (8 pts)
    if sector_b > 0:
        if sector_b >= 60:   p = 8
        elif sector_b >= 45: p = 6
        elif sector_b >= 30: p = 3
        elif sector_b >= 20: p = 1
        else:                p = 0
        pts += p
        breakdown["avg_sector_breadth"] = {"value": round(sector_b, 1), "pts": p}

    # Advance/Decline ratio (5 pts)
    if ad_ratio > 0:
        if ad_ratio >= 3.0:   p = 5
        elif ad_ratio >= 1.5: p = 3
        elif ad_ratio >= 0.8: p = 1
        else:                 p = 0
        pts += p
        breakdown["advance_decline"] = {"value": round(ad_ratio, 2), "pts": p}

    # Midcap breadth adjustment (±4 pts)
    # Midcap leading large-cap = healthy. Midcap lagging = warning.
    if midcap_b is not None:
        midcap_b = float(midcap_b)
        if midcap_b >= 60:   delta = 4
        elif midcap_b >= 45: delta = 2
        elif midcap_b < 35:  delta = -3
        elif midcap_b < 45:  delta = -1
        else:                delta = 0
        pts += delta
        breakdown["midcap_breadth"] = {"value": round(midcap_b, 1), "pts": delta}

    # Smallcap breadth adjustment (±3 pts) — NEW v2
    # Smallcap is leading indicator: turns down 2-4 weeks before Nifty in corrections
    if smallcap_b is not None:
        smallcap_b = float(smallcap_b)
        if smallcap_b >= 55:   delta = 3
        elif smallcap_b >= 40: delta = 1
        elif smallcap_b < 25:  delta = -3
        elif smallcap_b < 35:  delta = -1
        else:                  delta = 0
        pts += delta
        breakdown["smallcap_breadth"] = {"value": round(smallcap_b, 1), "pts": delta}

    pts = min(pts, 25.0)
    return round(max(pts, 0.0), 1), breakdown


def score_momentum(row: dict) -> tuple[float, dict]:
    """
    20 pts: Trend velocity and direction.

    Sub-components:
      weekly_rsi  (10 pts): trend quality signal
      20d return   (6 pts): primary momentum
      5d return    (4 pts): near-term pulse
      GIFT Nifty   (±2 pts, NEW v2): pre-open global signal
      Nifty PCR    (±2 pts): options market sentiment — NEW

    GIFT Nifty was loaded in v1 but never scored. It's a real-time
    signal for next-session direction — especially useful for regime
    detection because it incorporates overnight global moves.
    Premium > +0.3% = gap-up opening = bullish. Discount < -0.3% = gap-down.
    """
    weekly_rsi   = float(row.get("nifty_weekly_rsi") or 0)
    chg_20d      = float(row.get("nifty_20d_chg_pct") or 0)
    chg_5d       = float(row.get("nifty_5d_chg_pct") or 0)
    gift_premium = row.get("gift_premium_pct")   # may be None

    pts = 0.0
    breakdown = {}

    # Weekly Nifty RSI (10 pts)
    if weekly_rsi > 0:
        if weekly_rsi >= 65:   p = 10
        elif weekly_rsi >= 58: p = 7
        elif weekly_rsi >= 52: p = 5
        elif weekly_rsi >= 45: p = 2
        elif weekly_rsi >= 35: p = 1
        else:                  p = 0
        pts += p
        breakdown["weekly_rsi"] = {"value": round(weekly_rsi, 1), "pts": p}

    # 20-day return (6 pts)
    if chg_20d != 0:
        if chg_20d >= 6:     p = 6
        elif chg_20d >= 3:   p = 4
        elif chg_20d >= 0:   p = 2
        else:                p = 0
        pts += p
        breakdown["nifty_20d_chg"] = {"value": round(chg_20d, 2), "pts": p}

    # 5-day return (4 pts)
    if chg_5d != 0:
        if chg_5d >= 3:    p = 4
        elif chg_5d >= 1:  p = 2
        elif chg_5d >= 0:  p = 1
        else:              p = 0
        pts += p
        breakdown["nifty_5d_chg"] = {"value": round(chg_5d, 2), "pts": p}

    # GIFT Nifty premium/discount (±2 pts) — NEW v2
    # Only scored if we have the data — never penalise for missing it
    if gift_premium is not None:
        gift_premium = float(gift_premium)
        if gift_premium > 0.5:    delta = 2
        elif gift_premium > 0.2:  delta = 1
        elif gift_premium < -0.5: delta = -2
        elif gift_premium < -0.2: delta = -1
        else:                     delta = 0
        pts += delta
        breakdown["gift_nifty_premium"] = {"value": round(gift_premium, 2), "pts": delta}
    
    pcr = row.get("nifty_pcr")
    if pcr is not None:
        pcr = float(pcr)
        if pcr > 1.3:    delta = -2
        elif pcr > 1.0:  delta = -1
        elif pcr > 0.8:  delta = 1
        else:            delta = 2   # pcr <= 0.8
        pts += delta
        breakdown["nifty_pcr"] = {"value": round(pcr, 2), "pts": delta}

    pts = min(pts, 20.0)
    return round(max(pts, 0.0), 1), breakdown


def score_volatility(vix: float, vix_5d_delta: float | None) -> tuple[float, dict]:
    """
    15 pts: India VIX level + direction.

    VIX level (12 pts): fear gauge baseline
    VIX direction (±3 pts, NEW v2): is fear increasing or decreasing?

    Critical fix: VIX at 18.85 falling from 22 = recovering, award points.
    VIX at 18.85 rising from 16 = deteriorating, subtract points.
    v1 only scored the level, missing the direction entirely.

    NSE VIX calibration:
      < 13:  very low fear, momentum environment
      13-16: normal, healthy range
      16-18: slight elevation
      18-22: elevated — approaching risk-off
      22-26: high fear — risk-off territory
      > 26:  extreme fear — confirmed risk-off
    """
    breakdown = {}

    if not vix or vix <= 0:
        return 7.5, {"india_vix": {"value": None, "pts": 7.5, "note": "missing — midpoint"}}

    # VIX level (12 pts max — reduced from 15 to make room for direction)
    if vix < 13:        p = 12
    elif vix < 16:      p = 10
    elif vix < 18:      p = 8
    elif vix < 20:      p = 5
    elif vix < 22:      p = 2
    elif vix < 26:      p = 1
    else:               p = 0
    breakdown["vix_level"] = {"value": round(vix, 2), "pts": p}

    # VIX direction (±3 pts) — NEW v2
    delta_pts = 0
    if vix_5d_delta is not None:
        vd = float(vix_5d_delta)
        if vd < -2:      delta_pts = 3    # VIX falling sharply = fear receding
        elif vd < -0.5:  delta_pts = 2
        elif vd < 0:     delta_pts = 1
        elif vd < 0.5:   delta_pts = 0
        elif vd < 2:     delta_pts = -1   # VIX rising = fear building
        elif vd < 4:     delta_pts = -2
        else:            delta_pts = -3   # VIX spiking = high fear
        breakdown["vix_direction"] = {
            "5d_delta": round(vd, 2),
            "pts": delta_pts,
            "note": "falling" if vd < 0 else "rising"
        }

    total = float(p) + float(delta_pts)
    return round(min(max(total, 0.0), 15.0), 1), breakdown


def score_fii(fii: dict, fii_history: list) -> tuple[float, dict]:
    """
    15 pts: FII + DII flows — smart money direction.

    Sub-components:
      fii_net_20d (8 pts): sustained FII direction
      fii_net_5d  (4 pts): recent FII pulse
      DII counter-flow (±3 pts, NEW v2): DII buying when FII sells = floor support

    FIX v1: net_20d was 0.0 (not None) on some days, so the fallback to fii_flag
    never triggered, and 0.0 was scored as "mild positive" (2pts). Now 0.0 is
    correctly treated as zero net flow and scored at 2pts, which IS right.
    The actual bug was net_5d also being 0.0 and scoring 3pts — but fii_flag=CAUTION
    expected to yield only 1pt total. This was a logic mismatch, not a code bug.
    Net flow of zero is genuinely neutral, scoring 2+3=5pts is acceptable.

    FIX v1: Removed duplicate variable assignments (raw_20d, net_5d_raw).

    DII counter-flow rationale: In Indian market, DII (mutual funds + insurance)
    are contra-FII. When FII sells aggressively, DII buys — this provides a floor
    and reduces effective risk. Net FII-30 + DII buy = different regime implication
    than FII-30 + DII flat.
    """
    pts = 0.0
    breakdown = {}

    # ── Resolve net_20d with explicit cascade ────────────────────────────────
    net_20d = fii.get("fii_net_20d")

    if net_20d is None:
        net_20d = fii.get("fii_net_10d")
        if net_20d is not None:
            breakdown["net_20d_source"] = "fii_net_10d_proxy"

    if net_20d is None and len(fii_history) >= 3:
        dailies = [float(h["fii_net_cr"]) for h in fii_history
                   if h.get("fii_net_cr") is not None]
        if dailies:
            net_20d = sum(dailies)
            breakdown["net_20d_source"] = f"computed_from_{len(dailies)}_daily_rows"

    # ── Resolve net_5d ────────────────────────────────────────────────────────
    net_5d = fii.get("fii_net_5d")

    if net_5d is None and len(fii_history) >= 1:
        dailies = [float(h["fii_net_cr"]) for h in fii_history[:3]
                   if h.get("fii_net_cr") is not None]
        if dailies:
            net_5d = sum(dailies)
            breakdown["net_5d_source"] = f"computed_from_{len(dailies)}_daily_rows"

    fii_flag = str(fii.get("fii_flag") or "").upper()

    # ── Score net_20d (8 pts) ────────────────────────────────────────────────
    if net_20d is not None:
        n = float(net_20d)
        if n >= 5000:    p = 8
        elif n >= 2000:  p = 6
        elif n >= 500:   p = 4
        elif n >= 0:     p = 2
        elif n >= -2000: p = 1
        else:            p = 0
        pts += p
        breakdown["fii_net_20d"] = {"value": round(n), "pts": p}
    else:
        # Last resort: flag-based
        flag_pts = {"POSITIVE": 5, "NEUTRAL": 3, "CAUTION": 1}.get(fii_flag, 2)
        pts += flag_pts
        breakdown["fii_net_20d"] = {"value": None, "pts": flag_pts,
                                     "source": f"fii_flag={fii_flag}"}

    # ── Score net_5d (4 pts) — reduced from 7 to make room for DII ──────────
    if net_5d is not None:
        n = float(net_5d)
        if n >= 2000:    p = 4
        elif n >= 500:   p = 3
        elif n >= 0:     p = 2
        elif n >= -1000: p = 1
        else:            p = 0
        pts += p
        breakdown["fii_net_5d"] = {"value": round(n), "pts": p}
    else:
        pts += 2   # midpoint when unknown
        breakdown["fii_net_5d"] = {"value": None, "pts": 2, "note": "missing — midpoint"}

    # ── DII counter-flow (±3 pts) — NEW v2 ───────────────────────────────────
    # DII buying during FII selling = supportive floor (less bearish than pure FII)
    # DII selling along with FII = capitulation (more bearish)
    dii_20d = fii.get("dii_net_20d")
    dii_5d  = fii.get("dii_net_5d")

    if dii_20d is not None:
        dii_20d = float(dii_20d)
        fii_20d_val = float(net_20d) if net_20d is not None else 0
        # DII buying while FII negative = supportive
        if fii_20d_val < -1000 and dii_20d > 2000:    delta = 3
        elif fii_20d_val < 0 and dii_20d > 500:        delta = 2
        elif fii_20d_val < 0 and dii_20d > 0:          delta = 1
        # Both selling = capitulation signal
        elif fii_20d_val < -1000 and dii_20d < 0:      delta = -3
        elif fii_20d_val < 0 and dii_20d < 0:          delta = -1
        else:                                           delta = 0
        pts += delta
        breakdown["dii_counter_flow"] = {
            "dii_net_20d": round(dii_20d),
            "fii_net_20d": round(fii_20d_val),
            "pts": delta
        }

    return round(min(max(pts, 0.0), 15.0), 1), breakdown


def compute_global_modifier(sp500_ret: float | None, usdinr_chg: float | None) -> tuple[float, dict]:
    """
    ±5 pts: Global risk context — NEW v2.

    S&P 500 5d return (±3 pts):
      Indian market correlation with US is ~0.8 in risk-off.
      A -3% S&P 500 week predicts Nifty weakness with high reliability.

    USD/INR direction (±2 pts):
      Rupee weakening = FII outflows amplified.
      Rupee at 84 is different from rupee weakening TO 84 from 82.
      FIX: v1 had no rupee awareness — regime could show NEUTRAL during
      a classic FII outflow + rupee depreciation episode.
    """
    pts = 0.0
    breakdown = {}

    # S&P 500 5-day return
    if sp500_ret is not None:
        sp = float(sp500_ret)
        if sp >= 3:      p = 3
        elif sp >= 1:    p = 2
        elif sp >= 0:    p = 1
        elif sp >= -2:   p = -1
        elif sp >= -4:   p = -2
        else:            p = -3
        pts += p
        breakdown["sp500_5d_ret"] = {"value": round(sp, 2), "pts": p}

    # USD/INR direction (positive = rupee weakening = bad)
    if usdinr_chg is not None:
        fx = float(usdinr_chg)
        if fx > 1.5:       p = -2
        elif fx > 0.5:     p = -1
        elif fx < -1.0:    p = 2    # strong rupee — check this BEFORE -0.5
        elif fx < -0.5:    p = 1
        else:              p = 0
        pts += p
        breakdown["usdinr_direction"] = {"5d_chg_pct": round(fx, 3), "pts": p}

    # Bounded ±5
    return round(min(max(pts, -5.0), 5.0), 1), breakdown


# ─────────────────────────────────────────────────────────────────────────────
# HYSTERESIS ENGINE — unchanged from v1 (logic was correct)
# ─────────────────────────────────────────────────────────────────────────────

def get_historical_regimes(history: list) -> list[str]:
    return [r.get("computed_regime") or r.get("regime") or "" for r in history]


def consecutive_days_below(scores: list, threshold: float) -> int:
    count = 0
    for s in scores:
        if s is not None and float(s) < threshold:
            count += 1
        else:
            break
    return count


def consecutive_days_above(scores: list, threshold: float) -> int:
    count = 0
    for s in scores:
        if s is not None and float(s) >= threshold:
            count += 1
        else:
            break
    return count


def detect_recovering(today_row: dict, history: list, score: float) -> bool:
    """
    RECOVERING requires ALL four conditions simultaneously.
    Strict by design — dead-cat bounce looks identical in first 1-2 days.
    """
    if not (SCORE_RECOVERING <= score < SCORE_NEUTRAL):
        return False

    # Was in RISK OFF within last 10 trading days
    recent_regimes = get_historical_regimes(history)
    if not any(r == "RISK OFF" for r in recent_regimes[:10]):
        return False

    # Breadth improving — above_200dma_pct rising vs 5 days ago
    today_breadth = float(today_row.get("above_200dma_pct") or 0)
    if len(history) >= 5:
        past_breadth = float(history[4].get("above_200dma_pct") or 0)
        if not (today_breadth > past_breadth + 3):
            return False
    elif today_breadth < 35:
        return False

    # VIX declining
    today_vix = float(today_row.get("india_vix") or 0)
    if len(history) >= 5 and today_vix > 0:
        past_vix = float(history[4].get("india_vix") or 0)
        if not (past_vix > 0 and today_vix < past_vix):
            return False
    elif today_vix >= 22:
        return False

    # Nifty bounced at least 3% from recent lows
    today_nifty = float(today_row.get("nifty_price") or 0)
    if today_nifty > 0 and history:
        lows = [float(h["nifty_price"]) for h in history if h.get("nifty_price")]
        if lows and today_nifty <= min(lows) * 1.03:
            return False

    return True


def apply_hysteresis(raw_score: float, today_row: dict,
                     history: list, is_recovering: bool) -> str:
    """
    Asymmetric hysteresis: fast downgrades, slow upgrades.
    Logic unchanged from v1 — was correct. Data inputs were the bug.
    """
    recent_scores = [h.get("regime_score_computed") for h in history[:7]]

    if history:
        current = history[0].get("computed_regime") or history[0].get("regime") or "NEUTRAL"
    else:
        current = today_row.get("regime") or "NEUTRAL"

    # Raw regime from score
    if raw_score >= SCORE_TRENDING:     raw_regime = "TRENDING"
    elif raw_score >= SCORE_RISK_ON:    raw_regime = "RISK ON"
    elif raw_score >= SCORE_NEUTRAL:    raw_regime = "NEUTRAL"
    elif is_recovering:                 raw_regime = "RECOVERING"
    else:                               raw_regime = "RISK OFF"

    if not history:
        return raw_regime

    # ── DOWNGRADE RULES (fast) ───────────────────────────────────────────────
    extreme_risk_off  = raw_score < 15
    two_days_risk_off = (consecutive_days_below(recent_scores, SCORE_RECOVERING) >= 1
                         and raw_score < SCORE_RECOVERING)

    if (extreme_risk_off or two_days_risk_off) and current != "RISK OFF":
        logger.warning(f"  DOWNGRADE: {current} → RISK OFF (score={raw_score:.1f})")
        return "RISK OFF"

    if current in ("TRENDING", "RISK ON"):
        if raw_score < SCORE_RISK_ON:
            if consecutive_days_below(recent_scores, SCORE_RISK_ON) >= 1 or raw_score < SCORE_NEUTRAL:
                logger.warning(f"  DOWNGRADE: {current} → NEUTRAL (score={raw_score:.1f})")
                return "NEUTRAL"

    if current == "TRENDING" and raw_score < SCORE_TRENDING:
        if consecutive_days_below(recent_scores, SCORE_TRENDING) >= 1:
            return "RISK ON"

    # ── UPGRADE RULES (slow) ─────────────────────────────────────────────────
    if current == "RISK OFF" and is_recovering:
        logger.info(f"  UPGRADE: RISK OFF → RECOVERING (score={raw_score:.1f})")
        return "RECOVERING"

    if current == "RECOVERING":
        if raw_score >= SCORE_NEUTRAL:
            if consecutive_days_above(recent_scores, SCORE_RECOVERING) >= 2:
                logger.info(f"  UPGRADE: RECOVERING → NEUTRAL (score={raw_score:.1f})")
                return "NEUTRAL"
        return "RECOVERING"

    if current == "NEUTRAL" and raw_score >= SCORE_RISK_ON:
        if consecutive_days_above(recent_scores, SCORE_RISK_ON) >= 2:
            logger.info(f"  UPGRADE: NEUTRAL → RISK ON (score={raw_score:.1f})")
            return "RISK ON"
        return "NEUTRAL"

    if current == "RISK ON" and raw_score >= SCORE_TRENDING:
        if consecutive_days_above(recent_scores, SCORE_TRENDING) >= 2:
            logger.info(f"  UPGRADE: RISK ON → TRENDING (score={raw_score:.1f})")
            return "TRENDING"
        return "RISK ON"

    return current


# ─────────────────────────────────────────────────────────────────────────────
# WRITE — single write point, no writes from snapshot
# ─────────────────────────────────────────────────────────────────────────────

def write_regime(sb, effective_date, mode, regime, score, breakdown, today_row, raw_data=None):
    """
    Single write point. In full mode also creates the row if it doesn't exist.
    """
    computed_fields = {
        # Core regime output
        "computed_regime":        regime,
        "regime_score_computed":  round(score, 1),
        "regime_score_breakdown": breakdown,
        "regime_computed_at":     datetime.now(IST).isoformat(),
        # Market data — all sourced from today_row (snapshot priority, DB fallback)
        "nifty_price":            today_row.get("nifty_price"),
        "nifty_50dma":            today_row.get("nifty_50dma"),
        "nifty_200dma":           today_row.get("nifty_200dma"),
        "nifty_weekly_rsi":       today_row.get("nifty_weekly_rsi"),
        "nifty_1d_chg_pct":       today_row.get("nifty_1d_chg_pct"),
        "nifty_5d_chg_pct":       today_row.get("nifty_5d_chg_pct"),
        "nifty_20d_chg_pct":      today_row.get("nifty_20d_chg_pct"),
        "india_vix":              today_row.get("india_vix"),
        "gift_nifty":             today_row.get("gift_nifty"),
        "avg_sector_breadth":     today_row.get("avg_sector_breadth"),
        "above_200dma_pct":       today_row.get("above_200dma_pct"),
        "advance_decline_ratio":  today_row.get("advance_decline_ratio"),
        "banknifty_price":        today_row.get("banknifty_price"),
        "banknifty_weekly_rsi":   today_row.get("banknifty_weekly_rsi"),
        "vix_5d_delta":    today_row.get("vix_5d_delta"),
        "gift_premium_pct": today_row.get("gift_premium_pct"),
        "sp500_5d_ret":    today_row.get("sp500_5d_ret"),
        "usdinr_5d_chg":   today_row.get("usdinr_5d_chg"),
        "midcap_breadth":  today_row.get("midcap_breadth"),
        "smallcap_breadth": today_row.get("smallcap_breadth"),
        "raw_data": json.dumps(raw_data) if raw_data else None,
    }

    full_fields = {
        **computed_fields,
        "regime":       regime,
        "regime_score": round(score / 100, 2),
    }

    if DRY_RUN:
        logger.info(f"[DRY RUN] regime={regime} score={score:.1f} mode={mode}")
        logger.info(f"  Would write: nifty={today_row.get('nifty_price')} "
                    f"vix={today_row.get('india_vix')} "
                    f"above200={today_row.get('above_200dma_pct')}")
        return

    fields = full_fields if mode == "full" else computed_fields

    fields["date"] = effective_date
    sb.table("market_regime").upsert(fields, on_conflict="date").execute()
    logger.success(f"✓ market_regime UPSERT | computed_regime={regime} | score={score:.1f} | date={effective_date}")

    if mode == "full":
        logger.info(f"  FULL MODE: regime field updated → Sheet decommissioned")

def build_raw_data(today_row: dict, data: dict, effective_date: str) -> dict:
    """
    Structured audit trail of all values used in this regime computation
    and their sources. Replaces the Sheet's raw_data JSON blob.
    """
    return {
        "computed_at":    datetime.now(IST).isoformat(),
        "effective_date": effective_date,
        "sources": {
            "nifty": {
                "source":       "yfinance:^NSEI",
                "price":        today_row.get("nifty_price"),
                "50dma":        today_row.get("nifty_50dma"),
                "200dma":       today_row.get("nifty_200dma"),
                "weekly_rsi":   today_row.get("nifty_weekly_rsi"),
                "1d_chg_pct":   today_row.get("nifty_1d_chg_pct"),
                "5d_chg_pct":   today_row.get("nifty_5d_chg_pct"),
                "20d_chg_pct":  today_row.get("nifty_20d_chg_pct"),
            },
            "banknifty": {
                "source":       "yfinance:^NSEBANK",
                "price":        today_row.get("banknifty_price"),
                "weekly_rsi":   today_row.get("banknifty_weekly_rsi"),
            },
            "vix": {
                "source":       data.get("vix_source"),
                "india_vix":    today_row.get("india_vix"),
                "vix_5d_delta": today_row.get("vix_5d_delta"),
            },
            "gift_nifty": {
                "source":         "supabase:global_cues",
                "gift_nifty":     today_row.get("gift_nifty"),
                "premium_pct":    today_row.get("gift_premium_pct"),
                "prev_close_src": "supabase:market_regime",
            },
            "breadth": {
                "source":               "supabase:stock_data_daily",
                "above_200dma_pct":     today_row.get("above_200dma_pct"),
                "advance_decline_ratio":today_row.get("advance_decline_ratio"),
                "midcap_breadth":       today_row.get("midcap_breadth"),
                "smallcap_breadth":     today_row.get("smallcap_breadth"),
            },
            "sector_breadth": {
                "source":             "supabase:sector_strength",
                "avg_sector_breadth": today_row.get("avg_sector_breadth"),
            },
            "fii_dii": {
                "source":       "supabase:fii_dii_flow",
                "fii_net_20d":  data["fii"].get("fii_net_20d"),
                "fii_net_5d":   data["fii"].get("fii_net_5d"),
                "fii_flag":     data["fii"].get("fii_flag"),
                "dii_net_20d":  data["fii"].get("dii_net_20d"),
                "dii_net_5d":   data["fii"].get("dii_net_5d"),
            },
            "global": {
                "sp500_source":   "yfinance:^GSPC",
                "sp500_5d_ret":   today_row.get("sp500_5d_ret"),
                "usdinr_source":  "supabase:macro_indicators:USD_INR",
                "usdinr_5d_chg":  today_row.get("usdinr_5d_chg"),
                "nifty_pcr_src":  "supabase:macro_indicators:NIFTY_PCR",
                "nifty_pcr":      today_row.get("nifty_pcr"),
            },
        }
    }

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — compute_regime skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())
    mode  = cfg("compute_regime_mode", "shadow")

    if os.getenv("COMPUTE_REGIME_MODE_OVERRIDE"):
        mode = os.getenv("COMPUTE_REGIME_MODE_OVERRIDE")

    logger.info("=" * 65)
    logger.info(f"STEP: Compute Regime v2.0 | {today} | mode={mode.upper()}"
                + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 65)

    # ── STEP 1: Resolve effective trading date FIRST ──────────────────────────
    # yfinance is ground truth for last trading date.
    # resolve_effective_date() also creates a placeholder row if one doesn't
    # exist — so write_regime() can always use .update() safely.
    effective_date, is_trading = resolve_effective_date(sb, today)
    if not is_trading:
        logger.info(f"  Non-trading day — computing for last trading date: {effective_date}")

    # ── STEP 2: Load all data ─────────────────────────────────────────────────
    logger.info("Pass 1: loading data...")
    data = load_data(sb, effective_date)

    today_row    = data["today_row"]
    history      = data["history"]
    fii          = data["fii"]
    fii_history  = data["fii_history"]
    vix          = data["vix"]
    vix_5d_delta = data["vix_5d_delta"]
    sp500_ret    = data["sp500_5d_ret"]
    usdinr_chg   = data["usdinr_5d_chg"]

    # ── STEP 3: Score pillars ─────────────────────────────────────────────────
    logger.info("Pass 2: scoring pillars...")

    p1_score, p1_bd = score_price_structure(today_row)
    p2_score, p2_bd = score_breadth(today_row)
    p3_score, p3_bd = score_momentum(today_row)
    p4_score, p4_bd = score_volatility(vix, vix_5d_delta)
    p5_score, p5_bd = score_fii(fii, fii_history)
    gm_score, gm_bd = compute_global_modifier(sp500_ret, usdinr_chg)

    raw_score   = p1_score + p2_score + p3_score + p4_score + p5_score + gm_score
    total_score = min(max(raw_score, 0.0), 100.0)

    breakdown = {
        "price_structure": {"score": p1_score, "max": 25, "detail": p1_bd},
        "breadth":         {"score": p2_score, "max": 25, "detail": p2_bd},
        "momentum":        {"score": p3_score, "max": 20, "detail": p3_bd},
        "volatility":      {"score": p4_score, "max": 15, "detail": p4_bd},
        "fii_flows":       {"score": p5_score, "max": 15, "detail": p5_bd},
        "global_modifier": {"score": gm_score, "max": 5,  "detail": gm_bd},
        "total":           round(total_score, 1),
        "vix_source":      data["vix_source"],
    }
    
    logger.info(f"  Pillar scores:")
    logger.info(f"    Price structure:  {p1_score:>5.1f}/25")
    logger.info(f"    Breadth:          {p2_score:>5.1f}/25")
    logger.info(f"    Momentum:         {p3_score:>5.1f}/20")
    logger.info(f"    Volatility/VIX:   {p4_score:>5.1f}/15  (VIX={vix}"
                + (f", Δ5d={vix_5d_delta:+.2f}" if vix_5d_delta is not None else "") + ")")
    logger.info(f"    FII flows:        {p5_score:>5.1f}/15")
    logger.info(f"    Global modifier:  {gm_score:>+5.1f}/±5"
                + (f"  (S&P500 5d={sp500_ret:+.1f}%)" if sp500_ret else ")")  )
    logger.info(f"    ─────────────────────────────────────")
    logger.info(f"    TOTAL:            {total_score:>5.1f}/100")

    # ── STEP 3B: Build raw_data audit trail ──────────────────────────────────
    raw_data = build_raw_data(today_row, data, effective_date)

    # ── STEP 4: Recovering check + hysteresis ────────────────────────────────
    is_recovering = detect_recovering(today_row, history, total_score)
    if is_recovering:
        logger.info("  RECOVERING conditions met — breadth improving, VIX declining, Nifty bouncing")

    logger.info("Pass 3: applying hysteresis...")
    final_regime = apply_hysteresis(total_score, today_row, history, is_recovering)

    # ── STEP 5: Comparison with Sheet and ML ─────────────────────────────────
    sheet_regime = today_row.get("regime", "?")
    ml_regime    = today_row.get("predicted_regime")

    match_sheet = "✅ AGREE" if final_regime == sheet_regime else f"⚠ DIVERGE (Sheet={sheet_regime})"
    logger.info(f"  Computed: {final_regime} | Score: {total_score:.1f}/100 | {match_sheet}")

    if final_regime != sheet_regime:
        logger.warning(
            f"  Regime divergence: Sheet='{sheet_regime}', compute='{final_regime}'. "
            f"{'FULL MODE: compute wins.' if mode == 'full' else 'SHADOW: Sheet still active.'}"
        )

    if ml_regime:
        ml_match = "✅" if ml_regime == final_regime else "⚠"
        logger.info(f"  vs ML predicted_regime: {ml_match} ML={ml_regime} | Computed={final_regime}")

    # ── STEP 6: Write (single write point) ───────────────────────────────────
    # Row is guaranteed to exist — resolve_effective_date() created it if missing.
    logger.info(f"Pass 4: writing ({mode} mode)...")
    write_regime(sb, effective_date, mode, final_regime,
                 total_score, breakdown, today_row, raw_data=raw_data)

    return {
        "status":           "ok",
        "effective_date":   effective_date,
        "is_trading_day":   is_trading,
        "computed_regime":  final_regime,
        "sheet_regime":     sheet_regime,
        "score":            round(total_score, 1),
        "mode":             mode,
        "agree_with_sheet": final_regime == sheet_regime,
        "is_recovering":    is_recovering,
        "pillars": {
            "price":    p1_score,
            "breadth":  p2_score,
            "momentum": p3_score,
            "vix":      p4_score,
            "fii":      p5_score,
            "global":   gm_score,
        },
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Compute Regime v2.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mode", choices=["shadow", "full"])
    args = parser.parse_args()
    if args.dry_run: os.environ["DRY_RUN"] = "True"
    if args.mode:    os.environ["COMPUTE_REGIME_MODE_OVERRIDE"] = args.mode
    print(main())