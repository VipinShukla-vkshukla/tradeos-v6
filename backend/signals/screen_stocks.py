"""
TradeOS v6 — Dynamic Stock Screener v1.0
==========================================
Pipeline position: [10.6] — after compute_indicators, before compute_msl

REGIME ALIGNMENT UPDATE (v1.0 → v1.1)
======================================
screen_stocks now fully reflects the 5-state regime model from compute_regime v2.0.

States: TRENDING | RISK ON | NEUTRAL | RECOVERING | RISK OFF

CHANGES FROM v1.0:
  1. run_tpo()              — RECOVERING gets its own RSI window (40–53).
                              Was falling through to NEUTRAL default, which is wrong.
  2. run_reversal_setup()   — RECOVERING is now a primary mode (recovering_mode=True).
                              RVS is the most valuable engine in RECOVERING — stocks
                              bouncing from lows is exactly what RECOVERING means.
                              Threshold and gates tuned separately from risk_off_mode.
  3. run_mom_continuation() — RISK OFF: skip entirely (momentum in bear = dangerous).
                              RECOVERING: run with stricter gates (higher ADX floor,
                              delivery required, rsi_w floor raised).
  4. run_sector_rotation()  — RISK OFF: skip entirely.
                              RECOVERING: restrict to top 3 sectors only (not top 5).
  5. aggregate_and_rank()   — RECOVERING: no hard block (unlike RISK OFF), but engine
                              scores are multiplied by REGIME_ENGINE_WEIGHTS before
                              composite scoring. IAD/RSB/RVS boosted; CTL/MOM/VBD
                              reduced. RISK OFF: same hard block as before, but also
                              applies engine weights so quality signals still surface
                              for open_syms / force_include.
  6. REGIME_ENGINE_WEIGHTS  — New constant. Per-regime multipliers applied to each
                              engine's score during aggregation. Encodes the logic:
                                TRENDING   → CTL + MOM lead
                                RISK ON    → CTL + SBS + RSB lead
                                NEUTRAL    → balanced (all 1.0x)
                                RECOVERING → IAD + RSB + RVS lead; CTL + MOM reduced
                                RISK OFF   → RVS + IAD lead; CTL + MOM + VBD penalised

PURPOSE
  This script REPLACES the role of ingest_sheets.py for MASTER_SHORTLIST
  population (over time, via transition modes). It screens all 500 stocks
  in stock_data_daily through multiple strategy engines and proprietary
  scanners, then writes the top 30 or all qualified symbols to msl_computed
  (shadow mode) or master_shortlist (hybrid/full mode).

  The Google Sheet MSL tab becomes a manual override layer only —
  any symbol the user manually adds with force_include=True will be kept
  regardless of screener output.

STRATEGY ENGINES (evolved from your Sheet logic):
  1. CTL  — Core Trend Leaders (evolved: adds ADX, MACD, VWAP checks)
  2. SBS  — Structural Breakout Swing (evolved: BB squeeze, delivery trend)
  3. TPO  — Trend Pullback Opportunities (evolved: regime-aware RSI window)
  4. EAP  — Event-Accelerated Plays (evolved: programmatic event calendar)

PROPRIETARY SCANNERS (new, data-driven):
  5. VBD  — Velocity Burst Detector (single-day 3-6% move + institutional confirm)
  6. IAD  — Institutional Accumulation (quiet delivery + RS + volume expansion)
  7. RSB  — Relative Strength Breakout (RS leader coiling near resistance)
  8. MOM  — Momentum Continuation (EXPANSION phase + accelerating + near zone)
  9. RVS  — Reversal Setup (SMA50 bounce + RSI turning up from 45-52)
  10. SEC  — Sector Rotation (fresh sector momentum + early movers in new leaders)

SELECTION LOGIC:
  Each engine produces a {symbol: engine_score} dict.
  Scores are multiplied by REGIME_ENGINE_WEIGHTS[regime][engine] before aggregation.
  Symbols are aggregated with a CONVERGENCE BONUS when multiple engines agree.
  Top 30 by composite_score after sector diversification are selected.
  Hard blocks: ASM/GSM/FO_BAN excluded. Regime filter applied.

TRANSITION MODES (system_config: screener_mode):
  shadow  → writes to msl_computed only (master_shortlist untouched)
  hybrid  → writes to msl_computed AND updates master_shortlist (merged with Sheet)
  full    → writes only to master_shortlist (Sheet MSL tab is manual override only)
"""

import sys, os, json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import get_supabase, today_ist, IST, cfg, cfg_bool, is_kill_switch_active, DRY_RUN

# ── Config defaults (overridable via system_config) ────────────────────────────
MAX_SYMBOLS          = 30     # top N to write to master_shortlist
MAX_PER_SECTOR       = 5      # sector diversification cap
MIN_MARKET_CAP       = 300    # Cr — minimum for any strategy
CONVERGENCE_BONUS    = 8      # pts per additional engine confirming a symbol
WRITE_ALL_QUALIFIED  = True   # True = write all to msl_computed | False = write only top MAX_SYMBOLS


# ─────────────────────────────────────────────────────────────────────────────
# REGIME ENGINE WEIGHTS — NEW v1.1
# ─────────────────────────────────────────────────────────────────────────────

# Per-regime multipliers applied to each engine's raw score during aggregation.
# Encodes which engines are most reliable/relevant in each market state.
#
# Design rationale:
#   TRENDING:   Trend-following engines (CTL, MOM) at their most reliable.
#               Reversal setups (RVS) less relevant — few dips to buy.
#   RISK ON:    Broad participation. SBS/RSB also strong. Balance preferred.
#   NEUTRAL:    No edge in any direction. All engines equally weighted (1.0x).
#   RECOVERING: Coming out of bear. Accumulation (IAD), RS leaders (RSB), and
#               reversal setups (RVS) are the highest-value signals.
#               CTL/MOM/VBD penalised — trend not yet confirmed.
#   RISK OFF:   Bear market. Only highest-conviction quality signals surface.
#               RVS/IAD still valid for open_syms and force_include.
#               MOM/CTL/VBD heavily penalised — chasing in bear = capital destruction.
#
REGIME_ENGINE_WEIGHTS = {
    "TRENDING":   {"CTL": 1.2, "MOM": 1.2, "SBS": 1.1, "VBD": 1.0,
                   "IAD": 1.0, "RSB": 1.0, "TPO": 1.0, "RVS": 0.7, "SEC": 1.0},
    "RISK ON":    {"CTL": 1.1, "MOM": 1.1, "SBS": 1.1, "VBD": 1.0,
                   "IAD": 1.0, "RSB": 1.1, "TPO": 1.0, "RVS": 0.9, "SEC": 1.1},
    "NEUTRAL":    {"CTL": 1.0, "MOM": 1.0, "SBS": 1.0, "VBD": 1.0,
                   "IAD": 1.0, "RSB": 1.0, "TPO": 1.0, "RVS": 1.0, "SEC": 1.0},
    "RECOVERING": {"CTL": 0.8, "MOM": 0.7, "SBS": 0.9, "VBD": 0.8,
                   "IAD": 1.3, "RSB": 1.3, "TPO": 0.9, "RVS": 1.5, "SEC": 1.2},
    "RISK OFF":   {"CTL": 0.6, "MOM": 0.5, "SBS": 0.7, "VBD": 0.6,
                   "IAD": 1.2, "RSB": 1.2, "TPO": 0.7, "RVS": 1.4, "SEC": 1.0},
}

# ─────────────────────────────────────────────────────────────────────────────
# TRADING DAY RESOLVER
# ─────────────────────────────────────────────────────────────────────────────

def resolve_last_trading_day(sb, reference_date: str, max_lookback: int = 10) -> str:
    """
    Walk backwards from reference_date to find the last day that:
      - Is not Saturday/Sunday
      - Is not in nse_holidays table
      - Has actual data in stock_data_daily
    """
    holiday_rows = sb.table("nse_holidays").select("date").execute().data
    holidays     = {row["date"] for row in holiday_rows}
    candidate    = datetime.strptime(reference_date, "%Y-%m-%d").date()

    for _ in range(max_lookback):
        date_str = str(candidate)
        if candidate.weekday() in (5, 6):
            logger.debug(f"  {date_str} is a weekend — stepping back")
            candidate -= timedelta(days=1)
            continue
        if date_str in holidays:
            logger.debug(f"  {date_str} is an NSE holiday — stepping back")
            candidate -= timedelta(days=1)
            continue
        probe = (sb.table("stock_data_daily")
                   .select("date")
                   .eq("date", date_str)
                   .limit(1)
                   .execute().data)
        if probe:
            if date_str != reference_date:
                logger.info(f"  Resolved trading date: {reference_date} → {date_str}")
            return date_str
        logger.debug(f"  No stock_data_daily for {date_str} — stepping back")
        candidate -= timedelta(days=1)

    raise RuntimeError(
        f"No trading day with data found within {max_lookback} days before {reference_date}"
    )

# ─────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """Load all required tables in bulk — no per-symbol calls."""

    # stock_data_daily — all 500 stocks
    stock_rows = sb.table("stock_data_daily").select("*").eq("date", today).execute().data
    if not stock_rows:
        latest = sb.table("stock_data_daily").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            fb = latest[0]["date"]
            stock_rows = sb.table("stock_data_daily").select("*").eq("date", fb).execute().data
            logger.warning(f"stock_data_daily: falling back to {fb}")
    stock_map = {r["symbol"]: r for r in (stock_rows or [])}

    # sector_strength
    sector_rows = sb.table("sector_strength").select("sector,rank").eq("date", today).execute().data
    if not sector_rows:
        sector_rows = sb.table("sector_strength").select("sector,rank").order("date", desc=True).limit(50).execute().data
    sector_rank = {r["sector"]: r["rank"] for r in sector_rows if r.get("rank")}

    # market_regime
    regime_rows = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # safety_lists (ASM/FO_BAN)
    safety_rows = sb.table("safety_lists").select("symbol,list_type").execute().data
    asm_set    = {r["symbol"] for r in safety_rows if r["list_type"] in ("ASM", "GSM", "ASM_SHORTTERM")}
    fo_ban_set = {r["symbol"] for r in safety_rows if r["list_type"] == "FO_BAN"}

    # event_calendar
    event_rows = sb.table("event_calendar").select("*").eq("is_active", True).execute().data

    # fii_dii_flow (latest)
    fii_rows = sb.table("fii_dii_flow").select("fii_flag,fii_net_20d,fii_net_5d").order("date", desc=True).limit(1).execute().data
    fii = fii_rows[0] if fii_rows else {}

    # existing master_shortlist for manual override detection (Disabled for time being until ingest sheet dependency is fully removed)
    #msl_rows = sb.table("master_shortlist").select("symbol,force_include,notes").eq("date", today).execute().data
    #force_include = {r["symbol"] for r in (msl_rows or []) if r.get("force_include")}

    #(Temp for time being until ingest sheet dependency is fully removed)
    msl_rows = sb.table("master_shortlist")\
        .select("symbol,force_include,compute_source")\
        .eq("date", today).execute().data
    force_include = {r["symbol"] for r in (msl_rows or []) if r.get("force_include")}

    # ── INGEST_SHEETS MERGE (remove this block when ingest_sheets is retired) ──
    _sheet_syms = {r["symbol"] for r in (msl_rows or []) if r.get("compute_source") == "ingest_sheets"}
    if _sheet_syms:
        force_include |= _sheet_syms
        logger.info(f"  ingest_sheets symbols merged into force_include: {len(_sheet_syms)}")
    # ── END INGEST_SHEETS MERGE ─────────────────────────────────────────────────
    #(Temp for time being until ingest sheet dependency is fully removed)

    # open_positions (don't re-add already-held stocks as new entries)
    open_rows = sb.table("open_positions").select("symbol").eq("status", "ACTIVE").execute().data
    open_syms = {r["symbol"] for r in (open_rows or [])}

    logger.info(
        f"  Loaded: {len(stock_map)} stocks | {len(sector_rank)} sectors | "
        f"regime={regime.get('regime','?')} | asm={len(asm_set)} | "
        f"fii={fii.get('fii_flag','?')} | forced={len(force_include)}"
    )
    return {
        "stock_map":    stock_map,
        "sector_rank":  sector_rank,
        "regime":       regime,
        "asm_set":      asm_set,
        "fo_ban_set":   fo_ban_set,
        "event_rows":   event_rows,
        "fii":          fii,
        "force_include": force_include,
        "open_syms":    open_syms,
    }


def resolve_regime(regime: dict) -> str:
    from datetime import datetime, timezone
    manual    = regime.get("regime", "NEUTRAL")
    predicted = regime.get("predicted_regime")
    pred_at   = regime.get("regime_predicted_at")
    if predicted and pred_at:
        try:
            if isinstance(pred_at, str):
                pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
            else:
                pred_dt = pred_at
            if pred_dt.tzinfo is None:
                pred_dt = pred_dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - pred_dt).total_seconds() < 86400:
                return predicted
        except Exception:
            pass
    return manual


def get_event_action(symbol: str, sector: str, events: list, buffer_days: int = 2) -> str:
    """Returns AVOID_ENTRY, PRIORITISE, or NO_CHANGE based on event calendar."""
    from datetime import date as _date
    HIGH   = {"RESULTS","EARNINGS","QUARTERLY_RESULTS","BOARD_MEETING","FINANCIAL RESULTS"}
    today  = today_ist()
    for ev in events:
        if not ev.get("is_active"): continue
        ev_sym  = ev.get("symbol", "")
        sectors = str(ev.get("affected_sectors", "")).lower()
        relevant = (ev_sym == symbol) or (sector and sector.lower() in sectors)
        if not relevant: continue
        sd_str = ev.get("start_date") or ev.get("event_date")
        if not sd_str: continue
        try:
            sd   = _date.fromisoformat(str(sd_str)[:10])
            days = (sd - today).days
        except Exception:
            continue
        raw = (str(ev.get("event_type","")) + " " + str(ev.get("purpose",""))).upper()
        if any(k in raw for k in HIGH):
            if 0 <= days <= buffer_days:  return "AVOID_ENTRY"
            if -buffer_days <= days < 0:  return "PRIORITISE"
    return "NO_CHANGE"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY ENGINES
# ─────────────────────────────────────────────────────────────────────────────

def run_ctl(stock_map, sector_rank, cfg_ctl):
    results = {}
    max_sector_rank = cfg_ctl.get("max_sector_rank", 4)
    min_monthly_rsi = cfg_ctl.get("min_monthly_rsi", 58)
    min_weekly_rsi  = cfg_ctl.get("min_weekly_rsi", 58)
    min_6m_return   = cfg_ctl.get("min_6m_return", 0)
    max_atr_pct     = cfg_ctl.get("max_atr_pct", 4.5)
    min_market_cap  = cfg_ctl.get("min_market_cap", 300)

    for sym, s in stock_map.items():
        sector = s.get("sector", "")

        if sector and sector_rank.get(sector, 99) > max_sector_rank:   continue
        if (s.get("rsi_monthly") or 0) < min_monthly_rsi:              continue
        if (s.get("rsi_weekly")  or 0) < min_weekly_rsi:               continue
        if (s.get("ret_6m") or -999) < min_6m_return:                  continue
        if (s.get("atr_pct") or 999) > max_atr_pct:                    continue
        if (s.get("market_cap") or 0) < min_market_cap:                continue
        if not s.get("above_sma50"):                                    continue

        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        if adx < 12 or di_plus <= di_min:                               continue

        score  = 0.0
        rsi_m  = float(s.get("rsi_monthly") or 0)
        rsi_w  = float(s.get("rsi_weekly")  or 0)
        ret_6m = float(s.get("ret_6m") or 0)
        vol_r  = float(s.get("vol_ratio") or 0)
        s_rank = sector_rank.get(sector, 10)
        macd_h = float(s.get("macd_hist") or 0)
        sma_cross = bool(s.get("sma50_gt_200"))

        score += max(0, (5 - s_rank)) * 6

        if 60 <= rsi_m <= 72:   score += 20
        elif rsi_m > 72:         score += 10
        elif rsi_m >= 58:        score += 12

        if rsi_w >= 62:          score += 15
        elif rsi_w >= 58:        score += 10

        if 15 <= ret_6m <= 50:   score += 15
        elif ret_6m > 0:         score += 8
        elif ret_6m > 50:        score += 5

        if adx >= 28:            score += 10
        elif adx >= 20:          score += 6
        elif adx >= 12:          score += 2

        if vol_r >= 1.5:         score += 8
        elif vol_r >= 1.1:       score += 4

        if sma_cross:            score += 15

        if macd_h > 0.5:         score += 12
        elif macd_h > 0:         score += 8
        elif macd_h > -0.3:      score += 4
        elif macd_h < -1.0:      score -= 6

        results[sym] = round(score, 1)

    logger.info(f"  CTL: {len(results)} candidates")
    return results


def run_sbs(stock_map: dict, sector_rank: dict, cfg_sbs: dict) -> dict:
    """
    Structural Breakout Swing — EVOLVED from Sheet.
    Original: sector rank, daily/weekly RSI, ATR, vol ratio, consolidation, market cap.
    Added: BB squeeze detection, delivery trend (institutional in tight range),
           PSAR confirmation, proximity to 30d high (approach to resistance).
    """
    results = {}
    max_sector_rank = cfg_sbs.get("max_sector_rank", 7)
    min_daily_rsi   = cfg_sbs.get("min_daily_rsi", 52)
    min_weekly_rsi  = cfg_sbs.get("min_weekly_rsi", 54)
    max_atr_pct     = cfg_sbs.get("max_atr_pct", 5.5)
    min_vol_ratio   = cfg_sbs.get("min_vol_ratio", 1.1)
    max_consol      = cfg_sbs.get("max_consol_pct", 15)
    min_market_cap  = cfg_sbs.get("min_market_cap", 300)

    for sym, s in stock_map.items():
        sector = s.get("sector", "")

        if sector and sector_rank.get(sector, 99) > max_sector_rank:    continue
        if (s.get("rsi_daily") or 0) < min_daily_rsi:                   continue
        if (s.get("rsi_weekly") or 0) < min_weekly_rsi:                 continue
        if (s.get("atr_pct") or 999) > max_atr_pct:                     continue
        if (s.get("vol_ratio") or 0) < min_vol_ratio:                   continue
        if (s.get("market_cap") or 0) < min_market_cap:                 continue

        consol  = float(s.get("consol_range") or 999)
        bb_up   = float(s.get("bb_upper") or 0)
        bb_lo   = float(s.get("bb_lower") or 0)
        close   = float(s.get("close") or s.get("current_price") or 0)
        bb_width_pct = ((bb_up - bb_lo) / close * 100) if (close > 0 and bb_up > bb_lo) else 999

        tight_consol = consol <= max_consol
        bb_squeeze   = bb_width_pct < 7.0
        if not (tight_consol or bb_squeeze):                             continue

        wk_hh = bool(s.get("wk_hi_high"))
        wk_hl = bool(s.get("wk_hi_low"))
        above_50_sbs = bool(s.get("above_sma50"))
        if not above_50_sbs:                                        continue
        if not (wk_hh or wk_hl):                                   continue

        score = 0.0
        rsi_d    = float(s.get("rsi_daily") or 0)
        rsi_w    = float(s.get("rsi_weekly") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        delivery = float(s.get("delivery_pct") or 0)
        high_30d = float(s.get("high_30d") or 0)
        s_rank   = sector_rank.get(sector, 10)
        adx      = float(s.get("adx") or 0)

        score += max(0, (8 - s_rank)) * 4
        if 55 <= rsi_d <= 68:    score += 18
        elif 52 <= rsi_d < 55:   score += 12
        elif rsi_d > 68:         score += 6
        if rsi_w >= 58:          score += 12
        elif rsi_w >= 54:        score += 7
        if bb_squeeze:           score += 15
        if bb_width_pct < 4.5:  score += 8
        if consol < 6:           score += 10
        elif consol < 10:        score += 6
        if vol_r >= 2.0:         score += 10
        elif vol_r >= 1.4:       score += 6
        if delivery >= 55:       score += 10
        elif delivery >= 42:     score += 5
        if high_30d > 0 and close > 0:
            pct_away = (high_30d - close) / high_30d * 100
            if pct_away < 2:     score += 10
            elif pct_away < 5:   score += 6
            elif pct_away < 8:   score += 3
        if wk_hh and wk_hl:     score += 8
        elif wk_hl:              score += 4
        if bool(s.get("breakout_setup")): score += 5
        if bool(s.get("bk_trigger")):     score += 5

        results[sym] = round(score, 1)

    logger.info(f"  SBS: {len(results)} candidates")
    return results


def run_tpo(ctl_results: dict, stock_map: dict, cfg_tpo: dict, regime_name: str) -> dict:
    """
    Trend Pullback Opportunities — EVOLVED from Sheet.
    Original: works only on CTL stocks, daily RSI 42-55, dist from SMA50 <= 3%, ATR <= 4%.
    Added: regime-aware RSI window (in bull markets, pullbacks to 44 are fine),
           MACD histogram turning point detection.

    REGIME ALIGNMENT v1.1:
      TRENDING/RISK ON → RSI 44–58  (shallow pullbacks acceptable in strong market)
      NEUTRAL          → RSI 42–55  (config default, balanced)
      RECOVERING       → RSI 40–53  (NEW) deeper pullbacks before trust is rebuilt;
                                    entry earlier gives more room before resistance.
      RISK OFF         → RSI 38–50  (need deep pullback to justify any new entry)
    """
    results = {}

    # ── Regime-aware RSI window — all 5 states now handled explicitly ─────────
    if regime_name in ("TRENDING", "RISK ON"):
        rsi_min, rsi_max = 44, 58
    elif regime_name == "RECOVERING":
        # NEW v1.1: RECOVERING sits between NEUTRAL and RISK OFF.
        # Market bouncing from lows means quality pullbacks go deeper before
        # recovering — buying too early risks catching a dead-cat bounce.
        # The 40–53 window reflects that trust has not yet been fully restored.
        rsi_min, rsi_max = 40, 53
    elif regime_name == "RISK OFF":
        rsi_min, rsi_max = 38, 50
    else:
        # NEUTRAL — config default
        rsi_min, rsi_max = cfg_tpo.get("min_rsi", 42), cfg_tpo.get("max_rsi", 55)

    max_dist_sma50 = cfg_tpo.get("max_dist_sma50", 4)
    max_atr_pct    = cfg_tpo.get("max_atr_pct", 4)
    max_ctl_rank   = cfg_tpo.get("max_ctl_rank", 15)

    for sym in ctl_results:
        s = stock_map.get(sym, {})
        rsi_d = float(s.get("rsi_daily") or 0)

        if not (rsi_min <= rsi_d <= rsi_max):                 continue
        if abs(float(s.get("dist_sma50") or 999)) > max_dist_sma50: continue
        if (s.get("atr_pct") or 999) > max_atr_pct:          continue
        if not s.get("above_sma50"):                          continue

        rsi_w = float(s.get("rsi_weekly") or 0)
        if rsi_w < 50:                                        continue

        score = 0.0
        dist_50  = abs(float(s.get("dist_sma50") or 0))
        macd_h   = float(s.get("macd_hist") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        delivery = float(s.get("delivery_pct") or 0)

        if 46 <= rsi_d <= 53:    score += 20
        elif 44 <= rsi_d < 46:   score += 14
        elif rsi_d <= 58:        score += 10

        if dist_50 < 1.5:        score += 15
        elif dist_50 < 3:        score += 10
        elif dist_50 < 4:        score += 5

        if macd_h > 0:           score += 10
        elif macd_h > -0.5:      score += 8
        elif macd_h > -1:        score += 4

        if rsi_w >= 58:          score += 10
        elif rsi_w >= 52:        score += 6

        if vol_r >= 1.3 and delivery >= 50:   score += 12
        elif vol_r >= 1.1 or delivery >= 45:  score += 6

        ctl_score = ctl_results.get(sym, 0)
        score += ctl_score * 0.3

        results[sym] = round(score, 1)

    logger.info(f"  TPO: {len(results)} candidates")
    return results


def run_eap_overlay(candidates: dict, stock_map: dict, event_rows: list, cfg_eap: dict) -> dict:
    """
    Event-Accelerated Plays — EVOLVED overlay.
    Returns adjusted {symbol: score_delta} — applied on top of base scores.
    """
    buffer_days  = int(cfg_eap.get("pre_event_days", 2))
    post_boost   = float(cfg_eap.get("post_event_aggression", 2))
    pre_penalty  = float(cfg_eap.get("pre_event_aggression", 1))

    adjustments = {}
    for sym in candidates:
        s      = stock_map.get(sym, {})
        sector = s.get("sector", "")
        action = get_event_action(sym, sector, event_rows, buffer_days)
        if action == "AVOID_ENTRY":
            adjustments[sym] = -pre_penalty * 15
        elif action == "PRIORITISE":
            adjustments[sym] = post_boost * 10
        else:
            adjustments[sym] = 0

    return adjustments


# ─────────────────────────────────────────────────────────────────────────────
# PROPRIETARY SCANNERS
# ─────────────────────────────────────────────────────────────────────────────

def run_vbd(stock_map: dict, sector_rank: dict) -> dict:
    """
    Velocity Burst Detector — single-session 3-6% move + institutional confirm.
    No regime gating here — regime weight in REGIME_ENGINE_WEIGHTS reduces impact
    in RECOVERING/RISK OFF rather than eliminating it entirely.
    """
    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        pct_chg = float(s.get("pct_change") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        consol  = float(s.get("consol_range") or 999)
        above_50= bool(s.get("above_sma50"))
        adx     = float(s.get("adx") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        if pct_chg < 3.0:             continue
        if pct_chg > 12.0:            continue
        if vol_r < 1.8:               continue
        if delivery < 35:             continue
        if market_cap < MIN_MARKET_CAP: continue
        if not above_50:              continue
        if s_rank > 10:               continue

        score = 0.0
        if 4 <= pct_chg <= 7:         score += 25
        elif pct_chg > 7:             score += 15
        elif pct_chg >= 3:            score += 18
        if vol_r >= 5:                score += 25
        elif vol_r >= 3:              score += 20
        elif vol_r >= 2:              score += 12
        if delivery >= 65:            score += 20
        elif delivery >= 50:          score += 14
        elif delivery >= 38:          score += 7
        if consol < 6:               score += 15
        elif consol < 10:            score += 8
        if adx >= 25:                 score += 10
        elif adx >= 18:               score += 5
        score += max(0, (6 - s_rank)) * 2

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  VBD: {len(results)} candidates")
    return results


def run_iad(stock_map: dict, sector_rank: dict) -> dict:
    """
    Institutional Accumulation Detector — quiet delivery + RS + volume expansion.
    Most valuable scanner in RECOVERING — institutions accumulate before price moves.
    Engine weight boosted to 1.3x in RECOVERING and 1.2x in RISK OFF via REGIME_ENGINE_WEIGHTS.
    """
    results = {}
    for sym, s in stock_map.items():
        sector   = s.get("sector", "")
        delivery = float(s.get("delivery_pct") or 0)
        vol_r    = float(s.get("vol_ratio") or 0)
        rs       = float(s.get("rs_vs_nifty") or 0)
        consol   = float(s.get("consol_range") or 999)
        above_50 = bool(s.get("above_sma50"))
        rsi_d    = float(s.get("rsi_daily") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank   = sector_rank.get(sector, 99)

        vol_20d = float(s.get("avg_vol_20d") or 0)
        vol_50d = float(s.get("avg_vol_50d") or 0)
        vol_expanding = vol_20d > vol_50d * 1.10 if (vol_20d > 0 and vol_50d > 0) else False

        if delivery < 50:                         continue
        if vol_r < 1.2:                           continue
        if market_cap < MIN_MARKET_CAP:           continue
        if not above_50:                          continue
        if consol > 20:                           continue
        if s_rank > 12:                           continue

        score = 0.0

        if delivery >= 70:                        score += 35
        elif delivery >= 62:                      score += 27
        elif delivery >= 55:                      score += 18
        elif delivery >= 50:                      score += 10
        elif delivery >= 42:                      score += 5
        elif delivery >= 38:                      score += 2

        if rs > 20:                              score += 22
        elif rs > 12:                            score += 17
        elif rs > 8:                             score += 13
        elif rs > 3:                             score += 8
        elif rs > 0:                             score += 4

        if vol_expanding:                        score += 15
        if vol_r >= 2.0:                         score += 10
        elif vol_r >= 1.5:                       score += 6

        if consol < 7:                           score += 15
        elif consol < 12:                        score += 8

        if 50 <= rsi_d <= 65:                    score += 10
        elif rsi_d > 65:                         score += 4

        score += max(0, (6 - s_rank)) * 2

        min_score = 55 if delivery >= 50 else 68

        if score >= min_score:
            results[sym] = round(score, 1)

    logger.info(f"  IAD: {len(results)} candidates")
    return results


def run_rsb(stock_map: dict, sector_rank: dict) -> dict:
    """
    Relative Strength Breakout — RS leader coiling near resistance.
    Engine weight boosted in RECOVERING (1.3x) — RS leaders are first movers
    when market confirms recovery. Engine weight boosted in RISK OFF (1.2x)
    as RS leaders hold up best and recover fastest.
    """
    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rs      = float(s.get("rs_vs_nifty") or 0)
        ret_1m  = float(s.get("ret_1m") or 0)
        ret_3m  = float(s.get("ret_3m") or 0)
        consol  = float(s.get("consol_range") or 999)
        high_30d= float(s.get("high_30d") or 0)
        close   = float(s.get("close") or s.get("current_price") or 0)
        above_50= bool(s.get("above_sma50"))
        rsi_d   = float(s.get("rsi_daily") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        if rs < 4:                                continue
        if consol > 12:                           continue
        if not above_50:                          continue
        if market_cap < MIN_MARKET_CAP:           continue
        if rsi_d < 48:                            continue
        if s_rank > 10:                           continue

        score = 0.0
        if rs > 20:                              score += 28
        elif rs > 12:                            score += 22
        elif rs > 6:                             score += 15
        elif rs > 4:                             score += 8
        if ret_1m > 0 and ret_3m > 0:           score += 18
        elif ret_1m > 0:                         score += 9
        if consol < 5:                           score += 18
        elif consol < 8:                         score += 12
        elif consol < 12:                        score += 6
        if high_30d > 0 and close > 0:
            pct_away = (high_30d - close) / high_30d * 100
            if pct_away < 1.5:                   score += 18
            elif pct_away < 3.5:                 score += 12
            elif pct_away < 6:                   score += 6
        if 52 <= rsi_d <= 68:                   score += 12
        elif rsi_d > 68:                         score += 5
        score += max(0, (6 - s_rank)) * 2

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  RSB: {len(results)} candidates")
    return results


def run_mom_continuation(stock_map: dict, sector_rank: dict, regime_name: str) -> dict:
    """
    Momentum Continuation — EXPANSION phase + accelerating velocity.

    REGIME ALIGNMENT v1.1:
      RISK OFF   → Skip entirely. Chasing momentum in a bear market is the
                   fastest path to large drawdowns. Engine weight (0.5x) isn't
                   enough — better to produce zero candidates in RISK OFF.
      RECOVERING → Run with stricter gates: higher ADX floor (22 vs 18),
                   delivery required (≥42), rsi_w floor raised (57 vs 54).
                   We want only the clearest momentum signals — stocks where
                   institutions are clearly buying the recovery, not just
                   bouncing randomly.
      All others → Standard gates unchanged.
    """
    # ── RISK OFF: skip entirely — NEW v1.1 ────────────────────────────────────
    if regime_name == "RISK OFF":
        logger.info("  MOM: skipped — RISK OFF regime (momentum chasing in bear = high risk)")
        return {}

    # ── RECOVERING: stricter gate thresholds — NEW v1.1 ──────────────────────
    recovering_mode = (regime_name == "RECOVERING")
    adx_floor    = 22 if recovering_mode else 18    # stronger trend required in recovery
    rsi_w_floor  = 57 if recovering_mode else 54    # weekly must be clearly supportive
    delivery_min = 42 if recovering_mode else 0     # institutions must be behind the move

    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        macd_h  = float(s.get("macd_hist") or 0)
        above_50= bool(s.get("above_sma50"))
        sma200  = bool(s.get("sma50_gt_200"))
        above_st= bool(s.get("above_st"))
        ret_1m  = float(s.get("ret_1m") or 0)
        ret_3m  = float(s.get("ret_3m") or 0)
        wk_hh   = bool(s.get("wk_hi_high"))
        wk_hl   = bool(s.get("wk_hi_low"))
        dist_50 = float(s.get("dist_sma50") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # Hard gates for EXPANSION phase detection (regime-aware where noted)
        if not (above_50 and sma200 and above_st):    continue
        if adx < adx_floor or di_plus <= di_min:      continue   # regime-aware floor
        if rsi_d < 52 or rsi_d > 74:                 continue
        if rsi_w < rsi_w_floor:                       continue   # regime-aware floor
        if macd_h <= 0:                              continue
        if not (wk_hh or wk_hl):                     continue
        if market_cap < MIN_MARKET_CAP:               continue
        if s_rank > 10:                               continue
        # RECOVERING: require delivery — random bounce without institutional conviction
        if recovering_mode and delivery < delivery_min: continue   # NEW v1.1

        score = 0.0
        monthly_pace = ret_3m / 3 if ret_3m > 0 else 0
        accelerating = ret_1m > monthly_pace * 1.2 if monthly_pace > 0 else ret_1m > 3

        if adx >= 30:            score += 20
        elif adx >= 22:          score += 14
        elif adx >= 18:          score += 8

        if 57 <= rsi_d <= 68:   score += 18
        elif 52 <= rsi_d < 57:  score += 12
        elif rsi_d > 68:         score += 7

        if rsi_w >= 62:          score += 12
        elif rsi_w >= 56:        score += 7

        if accelerating:         score += 12

        if wk_hh and wk_hl:     score += 10
        elif wk_hl:              score += 5

        if delivery >= 55:       score += 10
        elif delivery >= 42:     score += 5

        if dist_50 < 5:          score += 8
        elif dist_50 < 10:       score += 4

        score += max(0, (6 - s_rank)) * 3

        if sma200:               score += 12
        if above_st:             score += 8

        min_score = 45 if (sma200 and above_st) else (52 if (sma200 or above_st) else 60)

        # RECOVERING: raise bar — only the clearest momentum signals should qualify
        if recovering_mode:
            min_score = max(min_score, 55)   # NEW v1.1

        if score >= min_score:
            results[sym] = round(score, 1)

    logger.info(f"  MOM: {len(results)} candidates"
                + (" [RECOVERING strict mode]" if recovering_mode else ""))
    return results


def run_reversal_setup(stock_map, sector_rank, regime_name):
    """
    Reversal Setup — SMA50 bounce + RSI turning up from oversold.

    REGIME ALIGNMENT v1.1:
      RECOVERING  → Primary mode (NEW). This is the BEST regime for RVS.
                    Market-wide recovery means individual stock reversals have
                    a macro tailwind. Lower score threshold (50 vs 55 base),
                    sector rank relaxed to 10, and gate checks slightly looser.
      RISK OFF    → Strict mode (unchanged from v1.0). Bounces fail frequently
                    in confirmed bear markets. Tighter gates, higher threshold.
      All others  → Standard mode. Score threshold 55.

    Key insight: RISK OFF and RECOVERING are NOT the same. RECOVERING has
    confirmed breadth improvement + VIX declining + Nifty bounced 3%+ from lows
    (those are the hysteresis conditions in compute_regime). RVS in RECOVERING
    is backed by macro confirmation; RVS in RISK OFF is a bet against the trend.
    """
    recovering_mode = (regime_name == "RECOVERING")   # NEW v1.1
    risk_off_mode   = (regime_name == "RISK OFF")

    if recovering_mode:
        logger.info("  RVS: RECOVERING mode — primary engine, relaxed gates")
    elif risk_off_mode:
        logger.info("  RVS: RISK OFF strict mode")

    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        rsi_m   = float(s.get("rsi_monthly") or 0)
        above_50= bool(s.get("above_sma50"))
        sma200  = bool(s.get("sma50_gt_200"))
        dist_50 = float(s.get("dist_sma50") or 0)
        ret_6m  = float(s.get("ret_6m") or 0)
        macd_h  = float(s.get("macd_hist") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        market_cap = float(s.get("market_cap") or 0)
        s_rank  = sector_rank.get(sector, 99)

        # ── Hard gates ──────────────────────────────────────────────────────
        if rsi_d > 54:                           continue
        if rsi_d < 36:                           continue
        if rsi_m < 48:                           continue
        if not sma200:                           continue
        if ret_6m < 0:                           continue
        if dist_50 < -10:                        continue
        if market_cap < MIN_MARKET_CAP:          continue

        # ── Regime-aware sector gate — NEW v1.1 ──────────────────────────
        # RECOVERING: relax to 10 — pharma, FMCG, IT (ranked 6-9) are exactly
        #             where RVS bounces are most reliable during recovery.
        # RISK OFF:   relax to 10 — same logic, slightly more selective.
        # Standard:   9 — balanced quality filter.
        if recovering_mode:
            max_sector = 10
        elif risk_off_mode:
            max_sector = 10
        else:
            max_sector = 9

        if s_rank > max_sector:                  continue

        # RISK OFF strict mode: require conviction
        if risk_off_mode and vol_r < 1.15 and delivery < 40:  continue

        # RECOVERING: require some volume confirmation (not as strict as RISK OFF)
        # Random bounces in RECOVERING are less likely, but still need some signal
        if recovering_mode and vol_r < 1.0 and delivery < 30:  continue   # NEW v1.1

        score = 0.0

        if 44 <= rsi_d <= 52:                    score += 25
        elif rsi_d < 44:                         score += 15

        if macd_h > 0:                           score += 18
        elif macd_h > -0.3:                      score += 12
        elif macd_h > -1:                        score += 6

        abs_dist = abs(dist_50)
        if abs_dist < 2:                         score += 18
        elif abs_dist < 5:                       score += 12
        elif abs_dist < 8:                       score += 6

        if rsi_m >= 58:                          score += 12
        elif rsi_m >= 50:                        score += 7

        if vol_r >= 1.5 and delivery >= 45:      score += 15
        elif vol_r >= 1.2 or delivery >= 40:     score += 7

        if rsi_w >= 48:                          score += 8

        score += max(0, (5 - s_rank)) * 4

        # ── Score threshold by regime — NEW v1.1 ─────────────────────────
        # RECOVERING: lower threshold — macro tailwind makes reversals more reliable.
        # RISK OFF:   higher threshold — bounces fail frequently, need strong signal.
        # Standard:   55 (unchanged from v1.0).
        if recovering_mode:
            min_score = 50   # NEW: macro-confirmed recovery = higher base probability
        elif risk_off_mode:
            min_score = 60
        else:
            min_score = 55

        if score >= min_score:
            results[sym] = round(score, 1)

    logger.info(f"  RVS: {len(results)} candidates")
    return results


def run_sector_rotation(stock_map: dict, sector_rank: dict, regime_name: str) -> dict:
    """
    Sector Rotation — early movers in freshly leading sectors.

    REGIME ALIGNMENT v1.1:
      RISK OFF    → Skip entirely (NEW). Sector rotation plays require market
                    participation. In RISK OFF, sector rotation is mostly
                    defensive repositioning, not a return-generating opportunity.
                    These setups fail in bear markets as even top-ranked sectors
                    come under pressure.
      RECOVERING  → Focus on top 3 sectors only (was top 5). Early recovery
                    sector leadership is narrow — only the clearest 2-3 sectors
                    drive the initial bounce. Casting wider nets in RECOVERING
                    picks up false-start sectors.
      All others  → Top 5 sectors as before (standard mode).
    """
    # ── RISK OFF: skip entirely — NEW v1.1 ────────────────────────────────────
    if regime_name == "RISK OFF":
        logger.info("  SEC: skipped — RISK OFF regime (sector rotation requires market participation)")
        return {}

    # ── RECOVERING: tighter sector filter — NEW v1.1 ─────────────────────────
    if regime_name == "RECOVERING":
        top_n = 3   # only the clearest sector leaders in recovery
        logger.info("  SEC: RECOVERING mode — restricting to top 3 sectors")
    else:
        top_n = 5   # standard

    top_sectors = {s for s, r in sector_rank.items() if r <= top_n}

    results = {}
    for sym, s in stock_map.items():
        sector  = s.get("sector", "")
        if sector not in top_sectors:            continue

        s_rank  = sector_rank.get(sector, 99)
        rsi_d   = float(s.get("rsi_daily") or 0)
        rsi_w   = float(s.get("rsi_weekly") or 0)
        delivery= float(s.get("delivery_pct") or 0)
        vol_r   = float(s.get("vol_ratio") or 0)
        above_50= bool(s.get("above_sma50"))
        adx     = float(s.get("adx") or 0)
        di_plus = float(s.get("di_plus") or 0)
        di_min  = float(s.get("di_minus") or 0)
        ret_1m  = float(s.get("ret_1m") or 0)
        market_cap = float(s.get("market_cap") or 0)

        if rsi_d < 48 or rsi_d > 72:            continue
        if not above_50:                         continue
        if market_cap < MIN_MARKET_CAP:          continue
        if di_plus <= di_min:                   continue

        score = 0.0
        if s_rank == 1:                          score += 20
        elif s_rank == 2:                        score += 16
        elif s_rank <= 4:                        score += 10
        elif s_rank <= 6:                        score += 5
        if 52 <= rsi_d <= 64:                   score += 20
        elif 48 <= rsi_d < 52:                  score += 12
        elif rsi_d > 64:                         score += 8
        if adx >= 22:                            score += 12
        elif adx >= 17:                          score += 7
        if vol_r >= 1.5 and delivery >= 48:      score += 18
        elif vol_r >= 1.3 or delivery >= 42:     score += 10
        if ret_1m > 5:                           score += 10
        elif ret_1m > 0:                         score += 5
        if rsi_w >= 55:                          score += 10
        elif rsi_w >= 50:                        score += 5

        if score >= 55:
            results[sym] = round(score, 1)

    logger.info(f"  SEC: {len(results)} candidates"
                + (" [top 3 sectors only]" if regime_name == "RECOVERING" else ""))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION & SELECTION
# ─────────────────────────────────────────────────────────────────────────────

def aggregate_and_rank(
    engine_results: dict,
    stock_map: dict,
    sector_rank: dict,
    asm_set: set,
    fo_ban_set: set,
    eap_adjustments: dict,
    regime_name: str,
    open_syms: set,
    force_include: set,
    today: str,
    max_symbols: int = 30,
    max_per_sector: int = 5,
    allow_risk_off: bool = False,
) -> tuple:

    """
    Aggregate all engine scores, apply REGIME_ENGINE_WEIGHTS, convergence bonus,
    EAP overlay, sector diversification, and return top max_symbols candidates.

    REGIME ALIGNMENT v1.1:
      Engine scores are multiplied by REGIME_ENGINE_WEIGHTS[regime][engine]
      BEFORE base_score calculation. This means:
        - In RECOVERING, an IAD score of 70 becomes 70 * 1.3 = 91 before averaging.
        - In RECOVERING, a MOM score of 70 becomes 70 * 0.7 = 49 before averaging.
      This naturally surfaces the right engine mix per regime without hard blocking.

      RECOVERING treatment: No hard block (unlike RISK OFF). All candidates
      pass through but regime-weighted scores reshuffle the ranking significantly.
      Practical effect: IAD/RSB/RVS candidates rank higher; CTL/MOM/VBD lower.

      RISK OFF treatment: Hard block for new entries (unchanged from v1.0).
      open_syms and force_include bypass the block but still get regime-weighted
      scoring (IAD/RVS boosted so quality bounce setups surface for review).
    """

    # ── Get regime engine weights (default to NEUTRAL if unknown) ─────────────
    weights = REGIME_ENGINE_WEIGHTS.get(regime_name, REGIME_ENGINE_WEIGHTS["NEUTRAL"])
    logger.info(
        f"  Regime weights ({regime_name}): "
        + " | ".join(f"{e}={w}x" for e, w in sorted(weights.items()) if w != 1.0)
        + (" (all 1.0x)" if all(w == 1.0 for w in weights.values()) else "")
    )

    # ── Collect all symbols with regime-weighted scores ───────────────────────
    all_syms: dict = defaultdict(lambda: {"engines": [], "scores": [], "raw": 0})
    for engine, results in engine_results.items():
        # ── INGEST_SHEETS MERGE (remove this block when ingest_sheets is retired) ──
        for sym in force_include:
            if sym not in all_syms and sym in stock_map:
                all_syms[sym]  # defaultdict seeds empty engines/scores; composite=0, force_include sorts first
        # ── END INGEST_SHEETS MERGE ─────────────────────────────────────────────────
        engine_weight = weights.get(engine, 1.0)
        for sym, score in results.items():
            weighted_score = round(score * engine_weight, 1)
            all_syms[sym]["engines"].append(engine)
            all_syms[sym]["scores"].append(weighted_score)

    # ── Hard exclusions ───────────────────────────────────────────────────────
    blocked_by_regime = set()
    regime_strict = (regime_name == "RISK OFF") and not allow_risk_off
    if regime_name == "RISK OFF" and allow_risk_off:
        logger.warning("  RISK OFF regime detected — screener_allow_risk_off=True, proceeding with reduced universe")

    candidates = []
    for sym, data in all_syms.items():
        s = stock_map.get(sym, {})

        if sym in asm_set or sym in fo_ban_set:          continue
        if (s.get("market_cap") or 0) < MIN_MARKET_CAP:  continue

        if regime_strict and sym not in open_syms and sym not in force_include:
            blocked_by_regime.add(sym)
            continue

        # Base score = weighted max × 0.6 + weighted avg × 0.4
        base_score = (
            max(data["scores"]) * 0.6 + (sum(data["scores"]) / len(data["scores"])) * 0.4
            if data["scores"] else 0
        )

        n_engines = len(data["engines"])
        avg_score = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0
        quality_multiplier = min(avg_score / 60, 1.5)
        convergence = (n_engines - 1) * CONVERGENCE_BONUS * quality_multiplier

        eap_adj = eap_adjustments.get(sym, 0)

        composite = base_score + convergence + eap_adj

        if composite < 0:                                 continue

        sector    = (s.get("sector") or "").lower()
        s_rank    = sector_rank.get(s.get("sector",""), 99)
        in_pos    = sym in open_syms
        forced    = sym in force_include

        candidates.append({
            "symbol":          sym,
            "company_name":    s.get("company_name"),
            "sector":          s.get("sector", ""),
            "strategy_source": "+".join(sorted(data["engines"])),
            "current_price":   s.get("close") or s.get("current_price"),
            "market_cap":      s.get("market_cap"),
            "composite_score": round(composite, 1),
            "base_score":      round(base_score, 1),
            "convergence_pts": convergence,
            "eap_adjustment":  eap_adj,
            "engines_count":   n_engines,
            "engines_list":    ",".join(sorted(data["engines"])),
            "sector_rank":     s_rank,
            "in_position":     in_pos,
            "force_include":   forced,
            "engine_scores_json": json.dumps({
                e: round(sc, 1)
                for e, sc in zip(data["engines"], data["scores"])
            }),
            "date":            today,
        })

    candidates.sort(key=lambda x: (x["force_include"], x["composite_score"]), reverse=True)

    sector_counts: dict = defaultdict(int)
    selected = []
    overflow = []

    for c in candidates:
        sector = c["sector"].lower()
        if c["force_include"]:
            selected.append(c)
        elif sector_counts[sector] < max_per_sector:
            selected.append(c)
            sector_counts[sector] += 1
        else:
            overflow.append(c)

    remaining = max_symbols - len(selected)
    if remaining > 0:
        selected.extend(overflow[:remaining])
        selected.sort(key=lambda x: x["composite_score"], reverse=True)

    def find_natural_cutoff(candidates, hard_max=30, min_gap=10):
        for i in range(min(hard_max, len(candidates) - 1)):
            gap = candidates[i]["composite_score"] - candidates[i+1]["composite_score"]
            if gap >= min_gap and i >= 20:
                return i + 1
        return hard_max

    cutoff = find_natural_cutoff(selected, hard_max=max_symbols)
    final = selected[:cutoff]

    if blocked_by_regime:
        logger.warning(f"  {len(blocked_by_regime)} symbols blocked by RISK OFF regime")

    logger.info(
        f"  Aggregated: {len(all_syms)} total unique | "
        f"{len(candidates)} after hard filters | {len(final)} final"
    )
    return final, candidates


# ─────────────────────────────────────────────────────────────────────────────
# WRITE STRATEGY
# ─────────────────────────────────────────────────────────────────────────────

def write_screener_results(sb, candidates: list, all_qualified: list, mode: str, today: str, write_all: bool = True):
    """
    shadow  → msl_computed only (master_shortlist untouched)
    hybrid  → msl_computed + update master_shortlist (merge with Sheet)
    full    → master_shortlist only (screener is the source of truth)
    """
    to_write = all_qualified if write_all else candidates

    if DRY_RUN:
        logger.info(
            f"[DRY RUN] Would write {len(to_write)} to msl_computed | "
            f"{len(candidates)} to master_shortlist | mode={mode}"
        )
        for c in candidates[:5]:
            logger.info(
                f"  #{candidates.index(c)+1} {c['symbol']:<12} "
                f"score={c['composite_score']:>5}  engines={c['engines_list']:<25} "
                f"sector={c['sector']}"
            )
        return

    if mode in ("shadow", "hybrid"):
        rows = []
        for rank, c in enumerate(to_write, 1):
            rows.append({
                "date":               today,
                "symbol":             c["symbol"],
                "company_name":       c.get("company_name"),
                "sector":             c.get("sector"),
                "strategy_source":    c.get("strategy_source"),
                "current_price":      c.get("current_price"),
                "composite_score":    c.get("composite_score"),
                "base_score":         c.get("base_score"),
                "priority_rank":      rank,
                "engines_list":       c.get("engines_list"),
                "convergence_pts":    c.get("convergence_pts"),
                "eap_adjustment":     c.get("eap_adjustment"),
                "sector_rank":        c.get("sector_rank"),
                "in_position":        c.get("in_position"),
                "engine_scores_json": c.get("engine_scores_json"),
                "screener_source":    "screen_stocks_v1",
                "computed_at":        datetime.now(IST).isoformat(),
            })
        for i in range(0, len(rows), 50):
            sb.table("msl_computed").upsert(rows[i:i+50], on_conflict="date,symbol").execute()
        logger.success(f"✓ {len(rows)} symbols → msl_computed")

    if mode in ("hybrid", "full"):
        msl_rows = []

        for rank, c in enumerate(candidates, 1):

            row = dict(c)   # copies ALL computed fields from candidate

            # ---- Add / rename fields needed by master_shortlist ----
            row["date"] = today
            row["final_score"] = c.get("composite_score")
            row["base_rank"] = rank
            row["priority_rank"] = rank
            row["position_state"] = "WATCHING"
            row["in_position"] = c.get("in_position", False)

            # metadata
            row["compute_source"] = "screen_stocks"
            row["screener_source"] = "screen_stocks_v1"
            row["computed_at"] = datetime.now(IST).isoformat()

            msl_rows.append(row)

        for i in range(0, len(msl_rows), 50):
            sb.table("master_shortlist") \
            .upsert(msl_rows[i:i+50], on_conflict="date,symbol") \
            .execute()

        if mode == "full":
            selected_syms = {c["symbol"] for c in candidates}
            existing = sb.table("master_shortlist") \
                        .select("symbol") \
                        .eq("date", today) \
                        .eq("compute_source", "screen_stocks")\
                        .execute().data

            stale = {r["symbol"] for r in existing} - selected_syms

            if stale:
                sb.table("master_shortlist") \
                .delete() \
                .eq("date", today) \
                .eq("compute_source", "screen_stocks")\
                .in_("symbol", list(stale)) \
                .execute()

                logger.info(f"  Removed {len(stale)} stale symbols from master_shortlist")

        logger.success(f"✓ {len(msl_rows)} symbols → master_shortlist")


def check_eap_revival(sb, stock_map: dict, today: str):
    """
    After daily write, finds stocks that were EAP-penalized yesterday
    but are showing strong post-event price action today.
    """
    from datetime import date, timedelta
    yesterday = str(date.fromisoformat(today) - timedelta(days=1))

    try:
        penalized = (
            sb.table("msl_computed")
            .select("symbol,eap_adjustment,composite_score,priority_rank")
            .eq("date", yesterday)
            .lt("eap_adjustment", -10)
            .execute()
            .data
        )
    except Exception as e:
        logger.warning(f"  EAP revival check failed (yesterday fetch): {e}")
        return

    if not penalized:
        logger.info("  EAP revival: no penalized stocks found yesterday — skipping")
        return

    revivals = []
    for row in penalized:
        sym      = row["symbol"]
        s        = stock_map.get(sym, {})
        pct_chg  = float(s.get("pct_change")   or 0)
        delivery = float(s.get("delivery_pct") or 0)
        vol_r    = float(s.get("vol_ratio")    or 0)

        if pct_chg >= 3.0 and delivery >= 45 and vol_r >= 1.3:
            revivals.append(sym)
            logger.info(
                f"  EAP REVIVAL: {sym:<12} "
                f"pct_chg={pct_chg:+.1f}%  "
                f"delivery={delivery:.0f}%  "
                f"vol_r={vol_r:.1f}x  "
                f"(was penalized {row['eap_adjustment']:.0f}pts yesterday)"
            )

    if not revivals:
        logger.info(f"  EAP revival: checked {len(penalized)} penalized stocks — none qualify today")
        return

    if DRY_RUN:
        logger.info(f"[DRY RUN] Would tag {len(revivals)} EAP revival stocks in msl_computed")
        return

    tagged = 0

    target_tables = ["msl_computed", "master_shortlist"]

    for sym in revivals:
        for tbl in target_tables:
            try:
                sb.table(tbl) \
                .update({"eap_revival": True}) \
                .eq("date", today) \
                .eq("symbol", sym) \
                .execute()

            except Exception as e:
                logger.warning(f"  EAP revival tag failed for {sym} in {tbl}: {e}")

        tagged += 1

    logger.success(f"  EAP revival: tagged {tagged}/{len(revivals)} stocks in msl_computed")


def log_screener_outcomes(sb, today: str, hold_days_list: list = None):
    """
    Runs daily at end of pipeline. Computes returns for hold periods
    and writes to screener_performance for engine attribution analysis.
    """
    from datetime import date, timedelta

    if hold_days_list is None:
        hold_days_list = [5, 10, 15]

    try:
        price_rows = (
            sb.table("stock_data_daily")
            .select("symbol,close")
            .eq("date", today)
            .execute()
            .data
        )
        today_prices = {r["symbol"]: float(r["close"] or 0) for r in price_rows}
    except Exception as e:
        logger.warning(f"  Outcomes: could not load today prices: {e}")
        return

    if not today_prices:
        logger.info("  Outcomes: no price data for today — skipping")
        return

    all_outcomes = []

    for hold_days in hold_days_list:
        past_date = str(date.fromisoformat(today) - timedelta(days=hold_days))

        try:
            past_rows = (
                sb.table("msl_computed")
                .select("symbol,composite_score,engines_list,current_price,priority_rank")
                .eq("date", past_date)
                .execute()
                .data
            )
        except Exception as e:
            logger.warning(f"  Outcomes: could not fetch {past_date}: {e}")
            continue

        if not past_rows:
            logger.info(f"  Outcomes: no msl_computed data for {past_date} — skipping {hold_days}d")
            continue

        period_count = 0
        for row in past_rows:
            sym     = row["symbol"]
            entry   = float(row.get("current_price") or 0)
            current = today_prices.get(sym, 0)

            if entry <= 0 or current <= 0:
                continue

            all_outcomes.append({
                "symbol":           sym,
                "entry_date":       past_date,
                "exit_date":        today,
                "hold_days":        hold_days,
                "entry_price":      round(entry, 2),
                "exit_price":       round(current, 2),
                "pct_return":       round((current - entry) / entry * 100, 2),
                "composite_score":  row.get("composite_score"),
                "priority_rank":    row.get("priority_rank"),
                "engines_list":     row.get("engines_list"),
            })
            period_count += 1

        logger.info(f"  Outcomes: {hold_days}d period — {period_count} stocks from {past_date}")

    if not all_outcomes:
        logger.info("  Outcomes: nothing to log (no past data aligns with today)")
        return

    winners = [o for o in all_outcomes if o["pct_return"] > 0]

    if DRY_RUN:
        logger.info(
            f"[DRY RUN] Would log {len(all_outcomes)} outcomes | "
            f"{len(winners)} positive ({len(winners)/len(all_outcomes)*100:.0f}% hit rate)"
        )
        return

    for i in range(0, len(all_outcomes), 50):
        try:
            sb.table("screener_performance")\
                .upsert(all_outcomes[i:i+50], on_conflict="symbol,entry_date,hold_days")\
                .execute()
        except Exception as e:
            logger.warning(f"  Outcomes: batch write failed: {e}")

    logger.success(
        f"  Outcomes logged: {len(all_outcomes)} records | "
        f"{len(winners)}/{len(all_outcomes)} positive "
        f"({len(winners)/len(all_outcomes)*100:.0f}% hit rate)"
    )


# ─────────────────────────────────────────────────────────────
# ADD THIS FUNCTION ABOVE main()
# ─────────────────────────────────────────────────────────────
def export_screener_excel(final, all_qualified, today):
    """
    Export screener output to Excel
    """

    export_dir = Path("exports")
    export_dir.mkdir(exist_ok=True)

    file_path = export_dir / f"screener_output_{today}.xlsx"

    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:

        # Top Selected Stocks
        if final:
            df1 = pd.DataFrame(final)
            df1.to_excel(writer, sheet_name="Final_Selected", index=False)

        # All Qualified Stocks
        if all_qualified:
            df2 = pd.DataFrame(all_qualified)
            df2.to_excel(writer, sheet_name="All_Qualified", index=False)

        # Auto column width
        for sheet in writer.sheets.values():
            for column_cells in sheet.columns:
                max_len = max(len(str(cell.value)) if cell.value else 0 for cell in column_cells)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max_len + 2, 35)

    logger.success(f"✓ Excel exported: {file_path}")

def validate_master_shortlist_completeness(sb, today):
    """
    Checks if rows written to master_shortlist have NULL gaps
    """
    logger.info("Checking master_shortlist completeness...")

    rows = (
        sb.table("master_shortlist")
        .select("*")
        .eq("date", today)
        .execute()
        .data
    )

    if not rows:
        logger.warning("No rows found in master_shortlist")
        return

    total_rows = len(rows)

    # Collect all columns
    all_cols = set()
    for r in rows:
        all_cols.update(r.keys())

    gap_report = {}

    for col in sorted(all_cols):
        null_count = sum(1 for r in rows if r.get(col) is None)

        if null_count > 0:
            gap_report[col] = null_count

    logger.info("=" * 70)
    logger.info("MASTER_SHORTLIST COMPLETENESS REPORT")
    logger.info("=" * 70)

    for col, cnt in gap_report.items():
        pct = round(cnt / total_rows * 100, 1)
        logger.warning(f"{col:<25} NULL in {cnt}/{total_rows} rows ({pct}%)")

    logger.info("=" * 70)

    fully_blank = [c for c, v in gap_report.items() if v == total_rows]

    if fully_blank:
        logger.error("Columns never populated by script:")
        for c in fully_blank:
            logger.error(f"   -> {c}")

    logger.success("Completeness check finished.")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — screen_stocks skipped")
        return {"status": "skipped"}

    sb    = get_supabase()
    today = str(today_ist())
    try:
        today = resolve_last_trading_day(sb, today)
    except RuntimeError as exc:
        logger.error(str(exc))
        return {"status": "no_data"}
    mode  = cfg("screener_mode", "shadow")
    if os.getenv("SCREENER_MODE_OVERRIDE"):
        mode = os.getenv("SCREENER_MODE_OVERRIDE")

    max_symbols         = int(cfg("screener_max_symbols", str(MAX_SYMBOLS)))
    max_sector          = int(cfg("screener_max_per_sector", str(MAX_PER_SECTOR)))
    allow_risk_off      = cfg_bool("screener_allow_risk_off", False)
    write_all_qualified = cfg_bool("screener_write_all_qualified", WRITE_ALL_QUALIFIED)

    logger.info("=" * 65)
    logger.info(f"STEP: Screen Stocks v1.1 | {today} | mode={mode.upper()}"
                + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 65)

    logger.info("Pass 1: loading data...")
    data = load_data(sb, today)
    if not data or not data.get("stock_map"):
        logger.error("No stock data — aborting")
        return {"status": "no_data"}

    stock_map   = data["stock_map"]
    sector_rank = data["sector_rank"]
    regime_name = resolve_regime(data["regime"])
    asm_set     = data["asm_set"]
    fo_ban_set  = data["fo_ban_set"]
    event_rows  = data["event_rows"]
    force_include = data["force_include"]
    open_syms   = data["open_syms"]

    strat_cfg = {}
    try:
        from config import get_strategy_config
        strat_cfg = get_strategy_config()
    except Exception:
        pass

    def _parse(key): return (
        json.loads(strat_cfg[key]) if isinstance(strat_cfg.get(key), str)
        else strat_cfg.get(key, {})
    )
    cfg_ctl = _parse("CTL")
    cfg_sbs = _parse("SBS")
    cfg_tpo = _parse("TPO")
    cfg_eap = _parse("EAP")

    # ── Run engines ───────────────────────────────────────────────────────────
    logger.info(f"Pass 2: running engines (regime={regime_name})...")

    ctl_results = run_ctl(stock_map, sector_rank, cfg_ctl)
    sbs_results = run_sbs(stock_map, sector_rank, cfg_sbs)
    tpo_results = run_tpo(ctl_results, stock_map, cfg_tpo, regime_name)
    vbd_results = run_vbd(stock_map, sector_rank)
    iad_results = run_iad(stock_map, sector_rank)
    rsb_results = run_rsb(stock_map, sector_rank)
    # ── Regime-aware engines (pass regime_name directly) ─────────────────────
    mom_results = run_mom_continuation(stock_map, sector_rank, regime_name)
    rvs_results = run_reversal_setup(stock_map, sector_rank, regime_name)
    sec_results = run_sector_rotation(stock_map, sector_rank, regime_name)

    all_candidates = (
        set(ctl_results) | set(sbs_results) | set(tpo_results) |
        set(vbd_results) | set(iad_results) | set(rsb_results) |
        set(mom_results) | set(rvs_results) | set(sec_results)
    )

    eap_adjustments = run_eap_overlay(
        {sym: 1 for sym in all_candidates}, stock_map, event_rows, cfg_eap
    )

    engine_results = {
        "CTL": ctl_results, "SBS": sbs_results, "TPO": tpo_results,
        "VBD": vbd_results, "IAD": iad_results, "RSB": rsb_results,
        "MOM": mom_results, "RVS": rvs_results, "SEC": sec_results,
    }

    logger.info(
        f"  Engine totals: CTL={len(ctl_results)} SBS={len(sbs_results)} "
        f"TPO={len(tpo_results)} VBD={len(vbd_results)} IAD={len(iad_results)} "
        f"RSB={len(rsb_results)} MOM={len(mom_results)} RVS={len(rvs_results)} "
        f"SEC={len(sec_results)} | Total unique={len(all_candidates)}"
    )

    # ── Aggregate ─────────────────────────────────────────────────────────────
    logger.info("Pass 3: aggregating and ranking...")
    final, all_qualified = aggregate_and_rank(
        engine_results, stock_map, sector_rank,
        asm_set, fo_ban_set, eap_adjustments,
        regime_name, open_syms, force_include, today,
        max_symbols=max_symbols, max_per_sector=max_sector,
        allow_risk_off=allow_risk_off,
    )

    if not final:
        logger.error("No candidates survived selection")
        return {"status": "no_candidates"}

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info(f"\n  ═══ TOP {len(final)} SCREENED SYMBOLS ({regime_name}) ═══")
    for i, c in enumerate(final, 1):
        tag = "📂" if c.get("in_position") else "  "
        logger.info(
            f"  {tag} #{i:>2} {c['symbol']:<12} "
            f"score={c['composite_score']:>5}  "
            f"engines={c['engines_list']:<28} "
            f"sector={c['sector']:<20} "
            f"rank={c['sector_rank']}"
        )

    from collections import Counter
    sector_dist = Counter(c["sector"] for c in final)
    engine_dist = Counter()
    for c in final:
        for e in c["engines_list"].split(","):
            engine_dist[e] += 1

    multi_engine = [c for c in final if c["engines_count"] >= 2]
    logger.info(f"  Multi-engine confirmed: {len(multi_engine)}/{len(final)}")
    logger.info(f"  Sector distribution: {dict(sector_dist)}")
    logger.info(f"  Engine hits: {dict(engine_dist)}")

    # ── Write ─────────────────────────────────────────────────────────────────
    logger.info(f"Pass 4: writing ({mode} mode)...")
    write_screener_results(sb, final, all_qualified, mode, today, write_all_qualified)
    #export_screener_excel(final, all_qualified, today)---To enable Excel export, uncomment this line and ensure openpyxl is installed.
    if mode in ("hybrid", "full"):
        validate_master_shortlist_completeness(sb, today)
    check_eap_revival(sb, stock_map, today)
    log_screener_outcomes(sb, today)

    return {
        "status":        "ok",
        "selected":      len(final),
        "total_written": len(all_qualified) if write_all_qualified else len(final),
        "mode":          mode,
        "regime":        regime_name,
        "multi_engine":  len(multi_engine),
        "engines_used":  list(engine_dist.keys()),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Screen Stocks v1.1")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--mode",     choices=["shadow","hybrid","full"])
    parser.add_argument("--max",      type=int, default=30, help="Max symbols to select")
    args = parser.parse_args()
    if args.dry_run: os.environ["DRY_RUN"] = "True"
    if args.mode:    os.environ["SCREENER_MODE_OVERRIDE"] = args.mode
    print(main())