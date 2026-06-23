"""
analysis/entry_readiness.py

Computes a per-candidate entry readiness score (0–100) using 6 factors:
  1. AI conviction (HIGH / MEDIUM / LOW)                  weight 20%
  2. Final score from master_shortlist                    weight 20%
  3. Zone proximity (dist_entry_pct)                      weight 20%
  4. Volume health (vol_ratio / live yfinance volume)     weight 20%
  5. Delivery % from stock_data_daily (yesterday)         weight 10%
  6. Regime alignment (active_regime from master_shortlist) weight 10%

Called from send_alerts.py for TIER_1 candidates.
  Evening  → use_live=False  (market closed, pipeline data only)
  Morning  → use_live=True   (yfinance 15-min delayed price + volume)

Adds to each candidate dict:
  readiness_score      int 0–100
  readiness_label      str  STRONG SETUP / CONDITIONAL / WAIT / SKIP TODAY
  readiness_icon       str  🟢 / 🟡 / 🟠 / 🔴
  readiness_breakdown  str  compact factor summary for Telegram
  zone_status          str  ✅ IN ZONE / ⚠️ APPROACHING / ❌ MISSED  (live only)
  timing_note          str  entry window recommendation
  gtt_entry            float  zone_low  (exact GTT limit price)
  gtt_sl               float  zone_low × (1 − stop_buffer)
  gtt_t1               float  zone_low × (1 + stop_buffer × expected_r)
  rr_ratio             float  (T1 − entry) / (entry − SL)
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── yfinance (optional dep) ──────────────────────────────────────────────────
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logger.warning("entry_readiness: yfinance not installed — live data disabled")

# ── stop buffer (read from system_config if available) ───────────────────────
try:
    from config import cfg
    STOP_BUFFER: float = float(cfg("stop_buffer_pct", "0.03"))
except Exception:
    STOP_BUFFER: float = 0.03

# ── Factor weight config ─────────────────────────────────────────────────────
WEIGHTS = {
    "conviction":  0.20,
    "final_score": 0.20,
    "zone_prox":   0.20,
    "volume":      0.20,
    "delivery":    0.10,
    "regime":      0.10,
}

CONVICTION_SCORE: dict[str, float] = {"HIGH": 100.0, "MEDIUM": 65.0, "LOW": 30.0}
REGIME_SCORE: dict[str, float] = {
    "TRENDING":   100.0,
    "RISK ON":     90.0,
    "RECOVERING":  75.0,
    "NEUTRAL":     50.0,
    "RISK OFF":    15.0,
}

NSE_SESSION_START_MINS: int = 9 * 60 + 15   # 9:15 AM IST
NSE_SESSION_TOTAL_MINS: int = 375            # 9:15 → 15:30


# ── Scoring helpers ──────────────────────────────────────────────────────────

def _score_zone_proximity(dist_entry_pct: Optional[float]) -> float:
    """
    Signed dist_entry_pct from master_shortlist:
      negative = price below zone (approaching from below) — still enterable
      positive = price above zone (entry missed)            — penalise harder
    """
    if dist_entry_pct is None:
        return 50.0
    d = float(dist_entry_pct)
    if d <= 0:
        # Price at or below zone — approaching from below (good)
        ad = abs(d)
        if ad <= 0.5: return 100.0
        if ad <= 1.5: return 90.0
        if ad <= 3.0: return 70.0
        if ad <= 5.0: return 45.0
        return max(0.0, 45.0 - (ad - 5.0) * 5.0)
    else:
        # Price above zone — entry window passed (penalise more steeply)
        if d <= 0.5: return 90.0    # barely above — still might pull back
        if d <= 1.5: return 60.0
        if d <= 3.0: return 30.0
        if d <= 5.0: return 10.0
        return 0.0                  # 5%+ above zone = skip


def _score_volume(vol_ratio: Optional[float]) -> float:
    """100 = 2× average, 0 = no volume."""
    if vol_ratio is None:
        return 50.0
    v = float(vol_ratio)
    if v >= 2.0: return 100.0
    if v >= 1.5: return 90.0
    if v >= 1.2: return 80.0
    if v >= 1.0: return 65.0
    if v >= 0.8: return 45.0
    return max(0.0, v * 45.0)


def _score_delivery(delivery_pct: Optional[float]) -> float:
    """100 = ≥65% delivery, 0 = no data."""
    if delivery_pct is None:
        return 50.0
    d = float(delivery_pct)
    if d >= 65.0: return 100.0
    if d >= 50.0: return 85.0
    if d >= 40.0: return 70.0
    if d >= 30.0: return 55.0
    return max(0.0, d * 1.5)


# ── yfinance live fetch ──────────────────────────────────────────────────────

def fetch_live_data(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch live NSE price + today's volume via yfinance (15-min delayed).
    Returns: symbol → {price, volume_today, high, low, open, elapsed_pct}
    Falls back to empty dict per symbol on any error.
    """
    if not _YF_AVAILABLE or not symbols:
        return {}

    now       = datetime.now()
    cur_m     = now.hour * 60 + now.minute
    elapsed   = min(1.0, max(0.01,
                   (cur_m - NSE_SESSION_START_MINS) / NSE_SESSION_TOTAL_MINS))
    result: dict[str, dict] = {}

    try:
        ns_syms  = [f"{s}.NS" for s in symbols]
        tickers  = yf.Tickers(" ".join(ns_syms))
        for sym in symbols:
            try:
                t  = tickers.tickers.get(f"{sym}.NS")
                fi = t.fast_info if t else None
                if fi is None:
                    result[sym] = {"elapsed_pct": elapsed}
                    continue

                vol = 0
                try:
                    hist = t.history(period="1d", interval="1d")
                    vol  = int(hist["Volume"].iloc[-1]) if not hist.empty else 0
                except Exception:
                    pass

                result[sym] = {
                    "price":        float(fi.last_price or 0),
                    "volume_today": vol,
                    "high":         float(fi.day_high or 0),
                    "low":          float(fi.day_low  or 0),
                    "open":         float(fi.open     or 0),
                    "elapsed_pct":  elapsed,
                }
            except Exception as e:
                logger.debug(f"yfinance {sym}: {e}")
                result[sym] = {"elapsed_pct": elapsed}
    except Exception as e:
        logger.warning(f"entry_readiness yfinance batch failed: {e}")

    return result


# ── Stock data daily fetch ───────────────────────────────────────────────────

def _load_sdd_map(sb, symbols: list[str]) -> dict[str, dict]:
    """Load latest stock_data_daily row per symbol for vol_ratio, delivery_pct, avg_vol_20d."""
    sdd_map: dict[str, dict] = {}
    if not sb or not symbols:
        return sdd_map
    try:
        rows = (
            sb.table("stock_data_daily")
              .select("symbol,vol_ratio,delivery_pct,avg_vol_20d,volume")
              .in_("symbol", symbols)
              .order("date", desc=True)
              .limit(len(symbols) * 3)
              .execute().data
        )
        seen: set[str] = set()
        for r in rows:
            s = r.get("symbol", "")
            if s and s not in seen:
                sdd_map[s] = r
                seen.add(s)
    except Exception as e:
        logger.warning(f"entry_readiness SDD fetch failed: {e}")
    return sdd_map


# ── Main public function ─────────────────────────────────────────────────────

def compute_entry_readiness(
    candidates: list[dict],
    msl_map: dict,
    sb=None,
    use_live: bool = True,
    sig_map: dict | None = None,
) -> list[dict]:
    """
    Enriches each candidate dict in-place with readiness fields.

    Args:
        candidates : list of ai_context ranked_candidate dicts (TIER_1 picks)
        msl_map    : symbol → master_shortlist row (already loaded by send_alerts.py)
        sb         : Supabase client (used to load stock_data_daily + optional yfinance)
        use_live   : True for morning alert (yfinance), False for evening (pipeline data only)

    Returns the same list, each dict enriched with readiness_* keys.
    """
    syms    = [c.get("symbol", "") for c in candidates]
    sdd_map = _load_sdd_map(sb, syms)
    live_data: dict[str, dict] = {}
    if use_live:
        live_data = fetch_live_data(syms)
    _now        = datetime.now()
    _now_min    = _now.hour * 60 + _now.minute
    _pre_market = _now_min < NSE_SESSION_START_MINS  # True before 9:15 AM IST

    for c in candidates:
        sym  = c.get("symbol", "")
        msl  = msl_map.get(sym, {})
        sdd  = sdd_map.get(sym, {})
        live = live_data.get(sym, {})

        # ── Factor 1: AI conviction ──────────────────────────────────────────
        conv    = (c.get("conviction") or msl.get("ai_conviction") or "").upper()
        f_conv  = CONVICTION_SCORE.get(conv, 50.0)

        # ── Factor 2: Final score ────────────────────────────────────────────
        fscore  = float(msl.get("final_score") or 0)
        f_score = min(100.0, max(0.0, fscore))

        # ── Factor 3: Zone proximity ─────────────────────────────────────────
        dist   = msl.get("dist_entry_pct")
        f_zone = _score_zone_proximity(dist)

        # ── Factor 4: Volume (live if available, else yesterday's vol_ratio) ─
        live_vol  = live.get("volume_today")
        elapsed   = live.get("elapsed_pct", 1.0)
        avg_vol   = int(sdd.get("avg_vol_20d") or 0)

        if live_vol and avg_vol and elapsed > 0.05:
            # Normalise live volume to expected full-day pace
            expected_by_now = avg_vol * elapsed
            live_ratio = live_vol / expected_by_now if expected_by_now > 0 else None
            f_vol = _score_volume(live_ratio)
            vol_display = live_ratio
        else:
            f_vol = _score_volume(sdd.get("vol_ratio"))
            vol_display = sdd.get("vol_ratio")

        # ── Factor 5: Delivery % ─────────────────────────────────────────────
        f_del       = _score_delivery(sdd.get("delivery_pct"))
        del_display = sdd.get("delivery_pct")

        # ── Factor 6: Regime alignment ───────────────────────────────────────
        regime_str = (msl.get("active_regime") or msl.get("regime") or "NEUTRAL").upper()
        f_regime   = REGIME_SCORE.get(regime_str, 50.0)

        # ── Weighted total ────────────────────────────────────────────────────
        raw = (
            f_conv   * WEIGHTS["conviction"]  +
            f_score  * WEIGHTS["final_score"] +
            f_zone   * WEIGHTS["zone_prox"]   +
            f_vol    * WEIGHTS["volume"]       +
            f_del    * WEIGHTS["delivery"]     +
            f_regime * WEIGHTS["regime"]
        )
        readiness = int(round(raw))

        if readiness >= 75:
            label, icon = "STRONG SETUP",  "🟢"
        elif readiness >= 60:
            label, icon = "CONDITIONAL",   "🟡"
        elif readiness >= 40:
            label, icon = "WAIT",          "🟠"
        else:
            label, icon = "SKIP TODAY",    "🔴"

        # ── Breakdown string for Telegram ────────────────────────────────────
        def _flag(score: float) -> str:
            return "✅" if score >= 70 else ("⚠️" if score >= 45 else "❌")

        _vol_is_live = bool(live_vol and avg_vol and elapsed > 0.05)
        vol_str  = f"{(vol_display or 0):.1f}×{'📡' if _vol_is_live else '📅'}"
        del_str  = f"{(del_display or 0):.0f}%"
        dist_str = f"{abs(float(dist or 0)):.1f}%↓"
        breakdown = (
            f"Conv {_flag(f_conv)} {conv} · "
            f"Zone {_flag(f_zone)} {dist_str} · "
            f"Vol {_flag(f_vol)} {vol_str} · "
            f"Del {_flag(f_del)} {del_str} · "
            f"Regime {_flag(f_regime)} {regime_str}"
        )

        # ── Zone status (live price vs zone bounds) ───────────────────────────
        zone_status = ""
        timing_note = ""
        live_price  = live.get("price")
        zl          = float(msl.get("entry_zone_low")  or 0)
        zh          = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))

        if live_price and zl:

            if live_price < zl * 0.995:
                zone_status = f"⬇️ APPROACHING (₹{live_price:,.0f}, {((zl - live_price) / zl * 100):.1f}% below zone)"
                timing_note = "Wait · Price below zone · Watch for gap-up at open" if _pre_market else "Wait · Price below zone"
            elif live_price <= zh:
                zone_status = f"✅ IN ZONE (₹{live_price:,.0f})"
                timing_note = "✅ GTT ready · Place limit at zone_low before 9:15 AM" if _pre_market else "1:30–2:30 PM · Limit at zone_low"
            elif live_price <= zh * 1.015:
                pct_above   = (live_price - zh) / zh * 100
                zone_status = f"⚠️ ABOVE ZONE (₹{live_price:,.0f}, +{pct_above:.1f}%)"
                timing_note = "Closed above zone · Confirm at open — pullback to zone needed" if _pre_market else "Watch for afternoon pullback to zone"
            else:
                pct_above   = (live_price - zh) / zh * 100
                zone_status = f"❌ MISSED (₹{live_price:,.0f}, +{pct_above:.1f}% above zone)"
                timing_note = "Skip today · Re-evaluate tomorrow"

        # ── GTT values ────────────────────────────────────────────────────────
        er      = float(msl.get("expected_r") or 2.0)
        gtt_entry = zl if zl else None
        gtt_sl    = round(zl * (1.0 - STOP_BUFFER), 2) if zl else None
        gtt_t1    = round(zl * (1.0 + STOP_BUFFER * er), 2) if zl else None
        rr_ratio  = (
            round((gtt_t1 - gtt_entry) / (gtt_entry - gtt_sl), 1)
            if gtt_t1 and gtt_sl and gtt_entry and gtt_sl < gtt_entry
            else None
        )

        # ── Enrich candidate in-place ─────────────────────────────────────────
        c.update({
            "readiness_score":     readiness,
            "readiness_label":     label,
            "readiness_icon":      icon,
            "readiness_breakdown": breakdown,
            "zone_status":         zone_status,
            "timing_note":         timing_note,
            "gtt_entry":           gtt_entry,
            "gtt_sl":              gtt_sl,
            "gtt_t1":              gtt_t1,
            "rr_ratio":            rr_ratio,
        })

    return candidates
