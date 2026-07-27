"""
TradeOS v6 — Step 18: AI Market Intelligence Engine
=====================================================
Pipeline position: after step 17 (post_trade), before step 19 (ai_decision_engine).

PURPOSE:
  Build a full market intelligence context from all enriched data sources,
  call AI once, and produce:
    1. Per-candidate sentiment_modifier written back to signal_log.score_adjusted
       (step 19 reads the adjusted scores — this is the news-to-score feedback loop)
    2. Market overlay JSON stored in ai_context.__MARKET_INTEL__
       (consumed by step 19 as its market context)
    3. 1-3 forward-looking lessons written to lessons table

WHAT CHANGED FROM market_intelligence_engine.py:
  - Candidates sourced from signal_log (post-step-15 gate-filtered) NOT raw MSL
    signal_log decides WHO qualifies; master_shortlist JOIN provides full picture
  - master_shortlist JOIN added: entry zones, current_price, convergence_pts,
    fundamental_quality, market_cap, st_cushion_pct, bb_width_pct
  - fv_low/fv_high/dist_fv_pct removed (duplicates of entry_zone fields)
  - macro_indicators added as source (CPI, WPI, IIP, rate decisions)
  - Historical echoes: last 5 days of __MARKET_INTEL__ from ai_context
    lets AI compare its prior calls to today — "VIX flagged 3d ago, now down 12%"
  - All sector_strength ranks included (was top 5 only)
  - global_cues.sector_impacts JSONB properly unpacked (was raw string before)
  - Lesson confidence scores (times_worked/times_applied) included in prompt
    so AI knows which rules are battle-tested vs hypothetical
  - AI now outputs sentiment_modifier per candidate — written to signal_log
  - __SHORTLIST__ from ai_context entirely removed (circular dependency)
  - No signal_log writes for top-3 picks — that is step 19's job

SELF-IMPROVEMENT MECHANISM:
  - Reads lessons ordered by confidence (times_worked/times_applied)
  - Writes new lessons with source=AI:market_intel
  - Step 17 (post_trade) increments times_worked/times_applied on lesson usage
  - Over time, lessons with low accuracy are auto-retired via is_active=False
  - Historical echoes allow AI to validate its own prior market calls

WRITES:
  signal_log     — score_adjusted updated with sentiment_modifier per candidate
  ai_context     — symbol=__MARKET_INTEL__, full analysis JSON
  lessons        — 1-3 forward-looking rules (source=AI:market_intel)
"""

import os
import re
import sys
import json
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import requests
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, is_kill_switch_active, cfg,cfg_float, AI_KEYS, get_trade_date, get_step_window

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_CLAUDE_MODEL   = "claude-sonnet-4-20250514"

_SYSTEM_PROMPT = (
    "You are a senior Indian equity portfolio manager specialising in NSE 500 swing trading. "
    "You have 20 years of experience reading macro flows, sector rotations, and FII patterns. "
    "Analyse the daily market intelligence packet and produce precise, actionable output. "
    "Be specific about sectors, timeframes, and price levels. "
    "When you see historical echoes of your prior calls, explicitly compare them to today's data. "
    "Output ONLY valid JSON — no preamble, no markdown, no explanation outside the JSON."
)

# ── Helpers ────────────────────────────────────────────────────────────────

def _pcr_label(pcr: float | None) -> str:
    """
    NSE Nifty PCR contrarian interpretation.
    High PCR = heavy put buying = fear positioning = contrarian bullish.
    Low PCR = heavy call buying = greed positioning = contrarian bearish.
    Thresholds configurable via system_config; defaults match standard NSE convention.
    """
    if pcr is None:
        return "no data"
    extreme_high = cfg_float("pcr_extreme_high", 1.3)
    mild_high    = cfg_float("pcr_mild_high",    1.0)
    mild_low     = cfg_float("pcr_mild_low",     0.8)
    if pcr > extreme_high:
        return f"{pcr} (EXTREME FEAR — contrarian bullish)"
    if pcr > mild_high:
        return f"{pcr} (mild bearish positioning)"
    if pcr >= mild_low:
        return f"{pcr} (neutral)"
    return f"{pcr} (EXTREME GREED — contrarian bearish, caution on new longs)"

def _lesson_confidence(lesson: dict) -> float:
    applied = lesson.get("times_applied") or 0
    worked  = lesson.get("times_worked")  or 0
    if applied < 3:
        return round(lesson.get("confidence") or 0.5, 2)  # not enough data, use stored
    return round(worked / applied, 2)


# ── Pass 1: Context assembly ───────────────────────────────────────────────

def build_market_context(sb, window_start, window_end_trade, window_end_calendar, last_td):
    """
    Full market context. Candidates from signal_log (gate-filtered by step 15)
    joined with master_shortlist for complete picture.
    """

    # ── Market regime ──
    regime = (sb.table("market_regime")
                .select("date,regime,predicted_regime,regime_confidence,regime_score,"
                        "nifty_price,nifty_50dma,nifty_200dma,nifty_weekly_rsi,"
                        "india_vix,vix_5d_delta,avg_sector_breadth,"
                        "nifty_1d_chg_pct,nifty_5d_chg_pct,nifty_20d_chg_pct,"
                        "advance_decline_ratio,above_200dma_pct,nifty_pcr,"
                        "banknifty_price,banknifty_weekly_rsi,macro_boost")
                .order("date", desc=True).limit(1).execute().data)
    regime = regime[0] if regime else {}

    # ── FII/DII ──
    fii = (sb.table("fii_dii_flow")
             .select("date,fii_net,dii_net,fii_net_5d,fii_net_10d,fii_net_20d,"
                     "fii_flag,dii_net_5d,dii_net_20d,dii_flag")
             .order("date", desc=True).limit(1).execute().data)
    fii = fii[0] if fii else {}

    # ── Global cues — sector_impacts JSONB unpacked ──
    cues_rows = (sb.table("global_cues").select("*")
            .gte("date", window_start)
            .lte("date", window_end_calendar)
                .order("date", desc=True)
                .execute().data)

    if not cues_rows:
        cues_rows = (sb.table("global_cues").select("*")
                    .order("date", desc=True).limit(1).execute().data)

    # Use latest as primary cue, keep others for prompt context
    cues = cues_rows[0] if cues_rows else {}
    cues_history = cues_rows[1:] if len(cues_rows) > 1 else []  # extra sessions for prompt
    sector_impacts = cues.pop("sector_impacts", {}) or {}
    logger.info(f"  global_cues: {len(cues_rows)} sessions in window | primary={cues.get('date','?')}")

    # ── Macro indicators — last 10 across all indicators ──
    macro_rows = (sb.table("macro_indicators")
                    .select("indicator_date,indicator_name,indicator_value,"
                            "previous_value,change_bps,source")
                    .gte("indicator_date", window_start)
                    .lte("indicator_date", window_end_calendar)
                    .order("indicator_date", desc=True)
                    .limit(15).execute().data)

    # Fallback: if no new macro in window, grab last 5 for context
    if not macro_rows:
        macro_rows = (sb.table("macro_indicators")
                        .select("indicator_date,indicator_name,indicator_value,"
                                "previous_value,change_bps,source")
                        .order("indicator_date", desc=True).limit(5).execute().data)
        logger.info("  macro_indicators: no new data in window — using last 5 for context")
    else:
        logger.info(
            f"  macro_indicators: {len(macro_rows)} new prints | "
            f"window {window_start} → {window_end_calendar}"
        )
        
    # ── Sector strength — ALL sectors ranked ──
    sectors = (sb.table("sector_strength")
                 .select("sector,rank,top4_flag,sector_state,avg_rsi_daily,"
                         "avg_rsi_weekly,avg_rsi_monthly,avg_ret_6m,"
                         "breadth_sma50,fii_flow_sector")
                 .eq("date", last_td).order("rank").execute().data)
    if not sectors:
        sectors = (sb.table("sector_strength")
                     .select("sector,rank,top4_flag,sector_state,avg_rsi_daily,"
                             "avg_rsi_weekly,avg_rsi_monthly,avg_ret_6m,"
                             "breadth_sma50,fii_flow_sector")
                     .order("date", desc=True).limit(25).execute().data)

    # ── Industry strength ──
    industries = (sb.table("industry_strength")
                    .select("industry,rank,top5_flag,industry_state,"
                            "avg_rsi_daily,avg_rsi_weekly,avg_ret_6m")
                    .eq("date", last_td).order("rank").limit(15).execute().data)

    # ── Market news — window query: captures weekend + holiday news ──
    _news_start = window_start
    _news_end   = window_end_calendar
    news = (sb.table("market_news")
            .select("headline,source,category,impact_type,magnitude,parsed_symbols,news_date")
            .gte("news_date", _news_start)
            .lte("news_date", _news_end)
            .order("news_date", desc=True)
            .limit(60)                          # increase cap — more days = more news
            .execute().data)
    logger.info(f"  market_news: {len(news)} items | window {_news_start} → {_news_end}")

    # ── Gate-filtered candidates from signal_log ──
    sig_rows = (sb.table("signal_log")
                  .select("symbol,company_name,sector,industry,signal_type,signal_subtype,"
                          "score,score_adjusted,momentum_state,momentum_phase,velocity_state,"
                          "trend_maturity,lifecycle,struct_edge,entry_timing_type,"
                          "holding_score,momentum_score,institutional_score,"
                          "breakout_readiness,risk_score,"
                          "bb_squeeze,bb_context,vwap_alignment,macd_direction,"
                          "weekly_structure,psar_dual_confirmed,ha_signal,"
                          "rsi_daily,rsi_weekly,vol_ratio,atr_pct,ret_6m,rs_vs_nifty,"
                          "validity_score,expected_r_msl,days_to_trigger_est,"
                          "fii_flag,sector_rank_at_entry,industry_rank,industry_top5,"
                          "eap_action,in_rule_engine")
                  .eq("date", last_td)
                  .in_("signal_type", ["PRIME_SETUP","BREAKOUT_SETUP","REENTRY_SETUP",
                                       "BUY_CANDIDATE","STAGED_ENTRY","MOMENTUM_CONTINUATION"])
                  .order("score_adjusted", desc=True)
                  .limit(30).execute().data)

    if not sig_rows:
        logger.warning(
            f"Zero buy candidates for {last_td} — promoting top near-miss WATCH signals "
            f"as provisional candidates for AI analysis"
        )
        sig_rows = (
            sb.table("signal_log")
            .select("symbol,company_name,sector,industry,signal_type,signal_subtype,"
                    "score,score_adjusted,momentum_state,momentum_phase,velocity_state,"
                    "trend_maturity,lifecycle,struct_edge,entry_timing_type,"
                    "holding_score,momentum_score,institutional_score,"
                    "breakout_readiness,risk_score,bb_squeeze,bb_context,vwap_alignment,"
                    "macd_direction,weekly_structure,psar_dual_confirmed,ha_signal,"
                    "rsi_daily,rsi_weekly,vol_ratio,atr_pct,ret_6m,rs_vs_nifty,"
                    "validity_score,expected_r_msl,days_to_trigger_est,"
                    "fii_flag,sector_rank_at_entry,industry_rank,industry_top5,"
                    "eap_action,in_rule_engine,near_miss_data,filter_reason")
            .eq("date", last_td)
            .eq("signal_type", "WATCH")
            .not_.is_("near_miss_data", "null")
            .order("score_adjusted", desc=True)
            .limit(20).execute().data
        )
        # Tag for prompt so AI knows these are near-miss promotions, not gate-passed signals
        for r in sig_rows:
            r["_promoted_from_watch"] = True
    
    # ── JOIN with master_shortlist for fields not in signal_log ──
    if sig_rows:
        symbols = [r["symbol"] for r in sig_rows]
        msl_rows = (sb.table("master_shortlist")
                      .select("symbol,current_price,entry_zone_low,entry_zone_high,"
                              "dist_entry_pct,convergence_pts,engines_count,"
                              "fundamental_quality,market_cap,st_cushion_pct,"
                              "bb_width_pct,bb_position_pct,dist_vwap_20d_pct,"
                              "ma_alignment_score,stoch_context,persistent_phase,"
                              "reentry_mode,position_state,suggested,"
                              "dist_sma50,ret_3m,ret_12m,low_52w,pct_change,"
                              "low_30d,consol_range")
                      .eq("date", last_td)
                      .in_("symbol", symbols)
                      .execute().data)
        msl_map = {r["symbol"]: r for r in msl_rows}

        candidates = []
        for sig in sig_rows:
            sym = sig["symbol"]
            msl = msl_map.get(sym, {})
            merged = {**sig}
            # Add MSL fields not in signal_log
            merged["current_price"]    = msl.get("current_price")
            merged["entry_zone_low"]   = msl.get("entry_zone_low")
            merged["entry_zone_high"]  = msl.get("entry_zone_high")
            merged["dist_entry_pct"]   = msl.get("dist_entry_pct")
            merged["convergence_pts"]  = msl.get("convergence_pts")
            merged["engines_count"]    = msl.get("engines_count")
            merged["fundamental_quality"] = msl.get("fundamental_quality")
            merged["market_cap"]       = msl.get("market_cap")
            merged["st_cushion_pct"]   = msl.get("st_cushion_pct")
            merged["bb_width_pct"]     = msl.get("bb_width_pct")
            merged["bb_position_pct"]  = msl.get("bb_position_pct")
            merged["ma_alignment_score"] = msl.get("ma_alignment_score")
            merged["stoch_context"]    = msl.get("stoch_context")
            merged["dist_sma50"]       = msl.get("dist_sma50")
            merged["ret_3m"]           = msl.get("ret_3m")
            merged["ret_12m"]          = msl.get("ret_12m")
            merged["low_52w"]          = msl.get("low_52w")
            merged["pct_change"]       = msl.get("pct_change")
            merged["low_30d"]          = msl.get("low_30d")
            merged["consol_range"]     = msl.get("consol_range")
            candidates.append(merged)
    else:
        candidates = []

    # ── Open positions ──
    positions = (sb.table("open_positions")
                   .select("symbol,sector,strategy,invested_value,pnl_pct,active_sl")
                   .eq("status", "ACTIVE").execute().data)

    # ── Lessons ordered by confidence (battle-tested rules first) ──
    lesson_rows = (sb.table("lessons")
                     .select("id,corrective_rule,scenario_type,impacted_sector,"
                             "times_applied,times_worked,confidence,source,observation")
                     .eq("is_active", True)
                     .order("times_applied", desc=True).limit(10).execute().data)
    # Compute live confidence and sort
    for l in lesson_rows:
        l["live_confidence"] = _lesson_confidence(l)
    lesson_rows.sort(key=lambda x: x["live_confidence"], reverse=True)

    # ── WATCH signals with near_miss_data ──
    watch_rows = (sb.table("signal_log")
                    .select("symbol,company_name,sector,score,score_adjusted,"
                            "momentum_state,lifecycle,risk_score,fii_flag,"
                            "sector_rank_at_entry,filter_reason,near_miss_data")
                    .eq("date", last_td)
                    .eq("signal_type", "WATCH")
                    .not_.is_("near_miss_data", "null")
                    .order("score_adjusted", desc=True)
                    .limit(15).execute().data)
    watch_candidates = []
    if watch_rows:
        watch_msl = (sb.table("master_shortlist")
                       .select("symbol,current_price,entry_zone_low,entry_zone_high,dist_entry_pct")
                       .eq("date", last_td)
                       .in_("symbol", [r["symbol"] for r in watch_rows])
                       .execute().data)
        wm = {r["symbol"]: r for r in watch_msl}
        for w in watch_rows:
            w["current_price"]   = wm.get(w["symbol"], {}).get("current_price")
            w["entry_zone_low"]  = wm.get(w["symbol"], {}).get("entry_zone_low")
            w["entry_zone_high"] = wm.get(w["symbol"], {}).get("entry_zone_high")
            w["dist_entry_pct"]  = wm.get(w["symbol"], {}).get("dist_entry_pct")
            watch_candidates.append(w)

    # ── Events ──
    cutoff = str(today_ist() + timedelta(days=14))
    events = (sb.table("event_calendar")
                .select("event_name,event_type,affected_sectors,event_bias,"
                        "event_intensity,start_date")
                .eq("is_active", True)
                .lte("start_date", cutoff)
                .order("start_date").execute().data)

    # ── Historical echoes: last 5 days of __MARKET_INTEL__ ──
    # Lets AI compare prior calls to today — self-improving context
    echo_rows = (sb.table("ai_context")
                   .select("date,ai_note,conviction,suggested_action,conviction_reason")
                   .eq("symbol", "__MARKET_INTEL__")
                   .order("date", desc=True).limit(5).execute().data)
    echoes = []
    for row in echo_rows:
        try:
            full = json.loads(row.get("conviction_reason") or "{}")
            echoes.append({
                "date":          row["date"],
                "sizing":        row.get("suggested_action"),
                "summary":       row.get("ai_note", "")[:200],
                "fii_bias":      (full.get("fii_outlook") or {}).get("5session_bias"),
                "top_sectors":   (full.get("fii_outlook") or {}).get("favoured_sectors", []),
                "macro_drivers": [(m.get("driver") or "") for m in
                                  (full.get("macro_sector_impacts") or [])[:2]],
            })
        except Exception:
            echoes.append({"date": row["date"], "summary": row.get("ai_note", "")[:200]})

    return {
        "regime":         regime,
        "fii":            fii,
        "cues":           cues,
        "cues_history":     cues_history,
        "sector_impacts": sector_impacts,
        "macro":          macro_rows,
        "sectors":        sectors,
        "industries":     industries,
        "news":           news,
        "candidates":     candidates,
        "positions":      positions,
        "lessons":        lesson_rows,
        "events":         events,
        "echoes":         echoes,
        "watch_candidates": watch_candidates,
    }


# ── Pass 2: Stock-specific news ────────────────────────────────────────────

def _nse_announcements(symbol: str, session: requests.Session) -> list[str]:
    out = []
    try:
        time.sleep(0.3)
        r = session.get(
            f"https://www.nseindia.com/api/corp-info?symbol={symbol}&type=announcements",
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            for item in (r.json().get("data") or [])[:3]:
                desc = item.get("desc") or item.get("subject") or ""
                if desc:
                    out.append(f"[NSE] {desc[:180]}")
    except Exception:
        pass
    return out


def _nse_bulk_deals(symbol: str, session: requests.Session) -> list[str]:
    out = []
    try:
        r = session.get(
            f"https://www.nseindia.com/api/bulk-deals?symbol={symbol}",
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            for d in (r.json().get("data") or [])[:3]:
                entity = d.get("clientName") or d.get("client") or ""
                if entity:
                    out.append(
                        f"[Bulk] {entity} {d.get('buySell','')}: "
                        f"{int(d.get('qty', 0)):,} @ ₹{float(d.get('price', 0)):.0f}"
                    )
    except Exception:
        pass
    return out


def _google_news(company_name: str) -> list[str]:
    out = []
    try:
        q = company_name.replace(" ", "+") + "+NSE+India"
        r = requests.get(
            f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            for item in ET.fromstring(r.text).iter("item"):
                title = re.sub(r"\s*-\s*[^-]+$", "", item.findtext("title") or "").strip()
                if title and len(title) > 10:
                    out.append(f"[News] {title[:180]}")
                if len(out) >= 3:
                    break
    except Exception:
        pass
    return out


def fetch_stock_intel(symbol: str, company_name: str, session: requests.Session) -> dict:
    headlines = (
        _nse_announcements(symbol, session)
        + _nse_bulk_deals(symbol, session)
        + _google_news(company_name or symbol)
    )
    institutional_buying = any(
        "[bulk]" in h.lower() and any(k in h.lower() for k in ("buy", "b", "purchase"))
        for h in headlines
    )
    return {
        "symbol": symbol,
        "company_name": company_name,
        "headlines": headlines[:8],
        "institutional_buying": institutional_buying,
    }


# ── Prompt builder ─────────────────────────────────────────────────────────

def build_prompt(ctx: dict, stock_intel: list[dict], window_start: str, window_end: str) -> str:
    r   = ctx["regime"]
    fii = ctx["fii"]
    c   = ctx["cues"]
    sector_impacts = ctx.get("sector_impacts", {})

    regime_label = r.get("predicted_regime") or r.get("regime", "UNKNOWN")
    regime_conf  = float(r.get("regime_confidence") or 0)

    lines = [
        f"DATE: {today_ist()}",
        f"DATA WINDOW: {window_start} → {window_end}  "
        f"(captures all news/macro/cues since last run)",
        "",
        "",
        "═══ MARKET REGIME ═══",
        f"  Regime: {regime_label} (confidence: {regime_conf:.0%}) | Score: {r.get('regime_score','?')}",
        f"  Nifty: ₹{r.get('nifty_price','?')} | 1d: {float(r.get('nifty_1d_chg_pct',0) or 0):+.2f}% | "
        f"5d: {float(r.get('nifty_5d_chg_pct',0) or 0):+.2f}% | 20d: {float(r.get('nifty_20d_chg_pct',0) or 0):+.2f}%",
        f"  50DMA: {r.get('nifty_50dma','?')} | 200DMA: {r.get('nifty_200dma','?')} | "
        f"Weekly RSI: {r.get('nifty_weekly_rsi','?')}",
        f"  VIX: {r.get('india_vix','?')} (5d Δ: {r.get('vix_5d_delta','?')}) | "
        f"A/D: {r.get('advance_decline_ratio','?')} | Above 200DMA: {r.get('above_200dma_pct','?')}%",
        f"  Breadth: {r.get('avg_sector_breadth','?')}% | "
        f"PCR: {_pcr_label(r.get('nifty_pcr'))} | "
        f"Macro boost: {r.get('macro_boost','?')}",
        f"  BankNifty: {r.get('banknifty_price','?')} | BankNifty RSI-W: {r.get('banknifty_weekly_rsi','?')}",
        "",
        "═══ FII / DII FLOWS ═══",
        f"  Today  → FII: ₹{float(fii.get('fii_net',0) or 0):+,.0f}Cr | DII: ₹{float(fii.get('dii_net',0) or 0):+,.0f}Cr",
        f"  5-day  → FII: ₹{float(fii.get('fii_net_5d',0) or 0):+,.0f}Cr | DII: ₹{float(fii.get('dii_net_5d',0) or 0):+,.0f}Cr",
        f"  20-day → FII: ₹{float(fii.get('fii_net_20d',0) or 0):+,.0f}Cr | DII: ₹{float(fii.get('dii_net_20d',0) or 0):+,.0f}Cr",
        f"  Flags  → FII: {fii.get('fii_flag','?')} | DII: {fii.get('dii_flag','?')}",
        "",
        "═══ GLOBAL MACRO ═══",
        f"  Gift Nifty: {c.get('gift_nifty','?')} ({float(c.get('gift_nifty_chg_pct',0) or 0):+.2f}%) | Gap: {c.get('gap_signal','?')}",
        f"  DOW: {float(c.get('us_dow_chg_pct',0) or 0):+.2f}% | S&P: {float(c.get('sp500_chg_pct',0) or 0):+.2f}% | NQ: {float(c.get('us_nasdaq_chg_pct',0) or 0):+.2f}%",
        f"  Brent: ${c.get('brent_crude','?')} ({float(c.get('brent_chg_pct',0) or 0):+.2f}%) | "
        f"Gold: ${c.get('gold_price','?')} | USD/INR: ₹{c.get('usd_inr','?')} ({float(c.get('usd_inr_chg_pct',0) or 0):+.3f}%)",
        f"  US 10yr: {c.get('us_10yr_yield','?')}% ({float(c.get('us_10yr_chg_bps',0) or 0):+.0f}bps)"
        f" [{sector_impacts.get('us10yr_signal','?')}]",
    ]

    if ctx["sector_impacts"]:
        lines.append(f"  Sector impacts (from global): {json.dumps(ctx['sector_impacts'])}")

    # Prior sessions in this data window (e.g. Fri+Sat+Sun global cues all
    # loaded ahead of a Monday run) — previously loaded into ctx and then
    # silently dropped before build_prompt ever ran; the AI only ever saw
    # the single latest row, missing any reversal/continuation across the gap.
    if ctx.get("cues_history"):
        lines += ["", "═══ PRIOR SESSIONS IN WINDOW (oldest global_cues rows this run) ═══"]
        for ch in ctx["cues_history"][:4]:
            lines.append(
                f"  {ch.get('date','?')} | Gift Nifty: {float(ch.get('gift_nifty_chg_pct',0) or 0):+.2f}% "
                f"({ch.get('gap_signal','?')}) | DOW: {float(ch.get('us_dow_chg_pct',0) or 0):+.2f}% | "
                f"S&P: {float(ch.get('sp500_chg_pct',0) or 0):+.2f}% | "
                f"Brent: {float(ch.get('brent_chg_pct',0) or 0):+.2f}%"
            )
        lines.append(
            "  → Read alongside GLOBAL MACRO above: a reversal across these sessions "
            "(e.g. Friday risk-off unwound by Monday) matters more than either day alone."
        )

    if ctx["macro"]:
        lines += ["", "═══ MACRO INDICATORS (recent) ═══"]
        for m in ctx["macro"]:
            delta = f"Δ{float(m.get('change_bps',0) or 0):+.0f}bps" if m.get("change_bps") else ""
            lines.append(
                f"  {m.get('indicator_date','?')} | {m.get('indicator_name','?')}: "
                f"{m.get('indicator_value','?')} (prev: {m.get('previous_value','?')}) {delta}"
            )

    lines += ["", "═══ SECTOR RANKINGS (full) ═══"]
    for s in ctx["sectors"]:
        top      = "★" if s.get("top4_flag") else " "
        fii_flow = f"| FII:₹{float(s.get('fii_flow_sector',0) or 0):+,.0f}Cr" if s.get("fii_flow_sector") else ""
        lines.append(
            f"  {top} #{str(s.get('rank','?')):>2} {s.get('sector','?'):<32} "
            f"{s.get('sector_state','?'):<12} RSI-D:{s.get('avg_rsi_daily','?')} "
            f"RSI-W:{s.get('avg_rsi_weekly','?')} Ret6m:{s.get('avg_ret_6m','?')}% "
            f"Brdth:{s.get('breadth_sma50','?')}% {fii_flow}"
        )

    lines += ["", "═══ TOP INDUSTRIES ═══"]
    for ind in ctx["industries"][:12]:
        top = "★" if ind.get("top5_flag") else " "
        lines.append(
            f"  {top} #{str(ind.get('rank','?')):>2} {ind.get('industry','?'):<35} "
            f"{ind.get('industry_state','?'):<10} RSI-D:{ind.get('avg_rsi_daily','?')} "
            f"Ret6m:{ind.get('avg_ret_6m','?')}%"
        )

    lines += ["", "═══ MARKET NEWS (last 24h) ═══"]
    for n in ctx["news"][:25]:
        mag = n.get("magnitude") or ""
        mag_str = f"[{mag}] " if mag else ""
        lines.append(f"  [{n.get('source','?')}] {mag_str}{n.get('impact_type','?')} | {n.get('headline','')[:120]}")

    lines += ["", "═══ UPCOMING EVENTS (next 14 days) ═══"]
    for e in ctx["events"][:12]:
        lines.append(
            f"  {e.get('start_date','?')} | {e.get('event_type','?')} — "
            f"{e.get('event_name','?')} | Sectors: {e.get('affected_sectors','?')} | "
            f"Bias: {e.get('event_bias','?')} | Intensity: {e.get('event_intensity','?')}"
        )

    lines += ["", "═══ OPEN POSITIONS ═══"]
    for p in ctx["positions"]:
        lines.append(
            f"  {p.get('symbol','?')} | {p.get('sector','?')} | "
            f"P&L: {float(p.get('pnl_pct') or 0):+.1f}% | SL: {p.get('active_sl','?')}"
        )
    if not ctx["positions"]:
        lines.append("  None")

    lines += ["", "═══ ACTIVE LESSONS (by confidence: battle-tested first) ═══"]
    for l in ctx["lessons"]:
        conf = l.get("live_confidence", 0)
        applied = l.get("times_applied") or 0
        source  = l.get("source", "MANUAL")
        lines.append(
            f"  [{l.get('scenario_type','?')}] conf:{conf:.0%} ({applied} uses, src:{source}) "
            f"| {l.get('corrective_rule','')[:160]}"
        )

    if ctx["echoes"]:
        lines += ["", "═══ HISTORICAL ECHOES (your prior market calls — compare to today) ═══"]
        for e in ctx["echoes"]:
            lines.append(
                f"  {e.get('date','?')} | Sizing:{e.get('sizing','?')} | "
                f"FII bias:{e.get('fii_bias','?')} | {e.get('summary','')[:150]}"
            )
        lines.append(
            "  → Compare above to today. If conditions have improved, upgrade sizing. "
            "If deteriorated, downgrade. Reference specific metrics."
        )

    promoted = any(c.get("_promoted_from_watch") for c in ctx["candidates"])
    candidates_header = (
        "═══ NEAR-MISS WATCH SIGNALS PROMOTED (no gate-passed candidates today) ═══\n"
        "  Market is in a broad uptrend — these stocks passed scoring but were ATR-blocked.\n"
        "  Evaluate each against today's macro/FII context. If macro supports, flag in near_miss_upgrades."
        if promoted else
        "═══ VALIDATED SIGNAL CANDIDATES (post step-15 gate filtering) ═══\n"
        "  These stocks passed 11 technical gates: ATR-normalised zone, R:R viability,\n"
        "  structural breakdown check, risk_score, liquidity, lifecycle, momentum gates.\n"
        "  Signal quality: PRIME_SETUP > MOMENTUM_CONTINUATION > BREAKOUT_SETUP > REENTRY_SETUP > BUY_CANDIDATE\n"
        "  PCR guidance: EXTREME FEAR readings support upgrading sizing and favouring\n"
        "  pullback/reentry setups; EXTREME GREED readings warrant MINIMAL/HALF sizing\n"
        "  and favour setup_types_to_avoid for chase-style entries (BREAKOUT, MOMENTUM_CONTINUATION).\n"
        "  Factor this into position_sizing_guidance and setup_types_favoured/avoid below.\n"
        "  Your task: assess each against TODAY's macro/FII/news context. Output sentiment_modifier."
    )
    lines += ["", candidates_header]

    for m in ctx["candidates"]:
        # Line 1: identity, price, position context
        lines.append(
            f"  {m.get('symbol','?')} | {m.get('sector','?')} | "
            f"{m.get('signal_type','?')} | Score:{float(m.get('score_adjusted',0) or 0):.1f} | "
            f"CMP:₹{m.get('current_price','?')} "
            f"Zone:₹{m.get('entry_zone_low','?')}-{m.get('entry_zone_high','?')} "
            f"Dist:{m.get('dist_entry_pct','?')}% | "
            f"DaysTrig:{m.get('days_to_trigger_est','?')}d | "
            f"Lifecycle:{m.get('lifecycle','?')} ExpR:{m.get('expected_r_msl','?')}x | "
            f"Eng:{m.get('engines_count','?')} Conv:{m.get('convergence_pts','?')} | "
            f"SRk:{m.get('sector_rank_at_entry','?')} IndTop5:{m.get('industry_top5','?')} | "
            f"FII:{m.get('fii_flag','?')} MCap:₹{m.get('market_cap','?')}Cr "
            f"Qual:{m.get('fundamental_quality','?')}"
        )
        # Line 2: technicals now visible to AI
        _low52 = m.get('low_52w')
        _cmp   = m.get('current_price')
        _dist_52w_low = (
            round((float(_cmp) - float(_low52)) / float(_low52) * 100, 1)
            if _low52 and _cmp and float(_low52) > 0 else "?"
        )
        lines.append(
            f"    → Mom:{m.get('momentum_state','?')}/{m.get('momentum_phase','?')}"
            f"/{m.get('velocity_state','?')} "
            f"Struct:{m.get('struct_edge','?')} Entry:{m.get('entry_timing_type','?')} "
            f"Maturity:{m.get('trend_maturity','?')} | "
            f"RSI-D:{m.get('rsi_daily','?')} RSI-W:{m.get('rsi_weekly','?')} "
            f"ADX:{m.get('adx','?')} RSvN:{m.get('rs_vs_nifty','?')} "
            f"Ret3M:{m.get('ret_3m','?')}% Ret6M:{m.get('ret_6m','?')}% "
            f"Ret12M:{m.get('ret_12m','?')}% Vol:{m.get('vol_ratio','?')}x | "
            f"HoldSc:{m.get('holding_score','?')} Risk:{m.get('risk_score','?')} "
            f"BrkRdy:{m.get('breakout_readiness','?')} "
            f"MomSc:{m.get('momentum_score','?')} InstSc:{m.get('institutional_score','?')} | "
            f"ST:{m.get('st_cushion_pct','?')}% MACD:{m.get('macd_direction','?')} "
            f"BB:{m.get('bb_context','?')} WkStr:{m.get('weekly_structure','?')} "
            f"PSAR:{m.get('psar_dual_confirmed','?')} | "
            f"Dist50:{m.get('dist_sma50','?')}% From52wL:{_dist_52w_low}% "
            f"Consol:{m.get('consol_range','?')}%"
        )

    if stock_intel:
        lines += ["", "═══ STOCK-SPECIFIC NEWS ═══"]
        for si in stock_intel:
            if si["headlines"]:
                lines.append(f"  {si['symbol']} ({si.get('company_name','')}):")
                for h in si["headlines"][:4]:
                    lines.append(f"    {h}")
                if si["institutional_buying"]:
                    lines.append(f"    ★ INSTITUTIONAL BUYING confirmed via bulk deals")

    if ctx.get("watch_candidates"):
        lines += ["", "═══ NEAR-MISS WATCH SIGNALS (evaluate if today's context upgrades these) ═══",
                "  These stocks passed all MSL scoring but were blocked by a narrow gate.",
                "  Assess whether today's macro/sector/news context justifies upgrading any.",
                "  Output each in near_miss_upgrades[] if you recommend promotion."]
        for w in ctx["watch_candidates"]:
            nm = {}
            try:
                nm = json.loads(w.get("near_miss_data") or "{}")
            except Exception:
                pass
            lines.append(
                f"  {w.get('symbol','?')} | {w.get('sector','?')} | "
                f"Score:{float(w.get('score_adjusted',0) or 0):.1f} | "
                f"Block:{nm.get('blocked_by','?')} | "
                f"CMP:₹{w.get('current_price','?')} Zone:₹{w.get('entry_zone_low','?')}-{w.get('entry_zone_high','?')} | "
                f"FII:{w.get('fii_flag','?')} SRk:{w.get('sector_rank_at_entry','?')} | "
                f"Detail:{str({k:v for k,v in nm.items() if k != 'blocked_by'})[:120]}"
            )

    lines.append(r"""
                 
OUTPUT INSTRUCTIONS:
Return ONLY this exact JSON structure. All fields are required.
sentiment_modifier: -0.2 to +0.2 (positive = good news boost, negative = bad news drag, 0.0 = neutral)
Return a sentiment_modifier for EVERY symbol in the candidates list above.

{
  "market_tone": {
    "summary": "2 sentences — dominant market dynamic and what it means for new entries today",
    "echo_comparison": "Compare to your last call above — what changed and what it implies for sizing",
    "setup_types_favoured": ["specific signal subtypes that suit today's conditions"],
    "setup_types_to_avoid": ["signal subtypes that are wrong for today's conditions"],
    "position_sizing_guidance": "FULL | REDUCED_25PCT | HALF | MINIMAL"
  },
  "macro_sector_impacts": [
    {
      "driver": "specific macro driver e.g. Brent crude +2.3%",
      "tailwind_sectors": ["exact NSE sector names"],
      "headwind_sectors": ["exact NSE sector names"],
      "magnitude": "HIGH | MEDIUM | LOW",
      "sessions": 3
    }
  ],
  "regulatory_alerts": [
    {
      "news_item": "headline summary",
      "affected_symbols": ["NSE_SYMBOL"],
      "affected_sectors": ["sector name"],
      "action": "WATCH | EXIT | AVOID_NEW_ENTRY | NO_ACTION",
      "urgency": "IMMEDIATE | THIS_WEEK | LOW"
    }
  ],
  "fii_outlook": {
    "5session_bias": "BUYING | SELLING | NEUTRAL",
    "favoured_sectors": ["sector names FII money is rotating into"],
    "exit_sectors": ["sector names FII is actively selling"],
    "confidence": "HIGH | MEDIUM | LOW",
    "key_signal_to_watch": "one specific data point that will confirm or deny this view"
  },
  "dii_outlook": {
    "5session_signal": "STRONG_BUYING | BUYING | NEUTRAL | SELLING | STRONG_SELLING",
    "absorbing_fii_selling": true,
    "key_implication": "one line — floor signal (DII absorbing) or double pressure (both selling)"
  },
  "candidate_sentiment": [
    {
      "symbol": "NSE_SYMBOL",
      "sentiment_modifier": 0.05,
      "sentiment_reason": "one line — what drove this adjustment",
      "news_flag": "POSITIVE | NEGATIVE | NEUTRAL | INSTITUTIONAL_BUY | RESULTS_RISK"
    }
  ],
  "near_miss_upgrades": [
    {
      "symbol": "NSE_SYMBOL",
      "upgrade_reason": "why today macro/news changes the blocked condition",
      "original_block": "e.g. insufficient_rr_0.87x",
      "flag_for": "TIER_1 | TIER_2 | WATCH_CLOSELY"
    }
  ],               
  "lessons_generated": [
    {
      "scenario_type": "e.g. Market/FII-Caution or Commodity/Crude-Up",
      "observation": "specific pattern visible in today's data",
      "corrective_rule": "Rule: actionable instruction starting with Rule:",
      "applies_to_sectors": ["sector1", "sector2"]
    }
  ]
}""")

    return "\n".join(lines)


# ── AI call ────────────────────────────────────────────────────────────────

def _call_claude_websearch(prompt: str) -> str | None:
    api_key = AI_KEYS.get("claude", "")
    if not api_key:
        return None
    try:
        resp = requests.post(
            _CLAUDE_API_URL,
            headers={"Content-Type": "application/json", "x-api-key": api_key,
                     "anthropic-version": "2023-06-01"},
            json={
                "model": _CLAUDE_MODEL, "max_tokens": 8000,
                "system": _SYSTEM_PROMPT,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        if resp.status_code != 200:
            logger.warning(f"Claude web_search HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        parts = [b["text"] for b in resp.json().get("content", []) if b.get("type") == "text"]
        return "\n".join(parts) or None
    except Exception as e:
        logger.warning(f"Claude web_search exception: {e}")
        return None

_REQUIRED_KEYS = {
    "market_tone", "macro_sector_impacts", "regulatory_alerts",
    "fii_outlook", "dii_outlook", "candidate_sentiment",
}

def _validate_ai_result(result: dict, n_candidates: int) -> list[str]:
    """Returns list of issues. Empty = structurally complete."""
    issues = []
    for k in _REQUIRED_KEYS:
        if k not in result:
            issues.append(f"missing key '{k}'")
    got = len(result.get("candidate_sentiment") or [])
    if got < n_candidates:
        issues.append(f"candidate_sentiment truncated: {got}/{n_candidates} entries")
    if not (result.get("market_tone") or {}).get("position_sizing_guidance"):
        issues.append("market_tone.position_sizing_guidance missing")
    return issues

def call_ai(prompt: str, n_candidates: int = 0) -> dict | None:
    try:
        from ai.ai_router import is_ai_available, raw_completion
    except ImportError:
        from ai_router import is_ai_available, raw_completion

    if not is_ai_available():
        logger.warning("No AI provider configured — market_intel skipped")
        return None

    provider = cfg("ai_provider", "disabled").lower()
    full_text = None

    if provider == "claude":
        full_text = _call_claude_websearch(prompt)
        if not full_text:
            try:
                full_text = raw_completion(f"{_SYSTEM_PROMPT}\n\n{prompt}", max_tokens=8000)
            except Exception as e:
                logger.warning(f"Claude fallback also failed: {e}")
                return None
    else:
        try:
            full_text = raw_completion(f"{_SYSTEM_PROMPT}\n\n{prompt}", max_tokens=8000)
        except Exception as e:
            logger.warning(f"AI call failed for provider={provider}: {e}")
            return None

    if not full_text:
        return None

    try:
        m = re.search(r"\{[\s\S]+\}", full_text)
        if not m:
            return None
        raw = m.group()
        recovered = False
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parse failed: {e}")
            fixed = _close_truncated_json(raw)
            if not fixed:
                return None
            result = json.loads(fixed)
            recovered = True

        issues = _validate_ai_result(result, n_candidates)
        got = len(result.get("candidate_sentiment") or [])
        src = "recovered" if recovered else "direct"

        if issues:
            logger.warning(f"AI result incomplete ({src}): {issues}")
            if any("missing key" in i for i in issues):
                return None
        else:
            logger.info(
                f"AI result OK ({src}): {got}/{n_candidates} sentiment entries"
                f" | all required keys present"
            )

        return result
    except Exception as e:
        logger.warning(f"JSON parse failed (outer): {e}")
        return None

def _close_truncated_json(s: str) -> str | None:
    """Close unclosed brackets/braces in a truncated JSON response."""
    in_string = esc = False
    stack: list[str] = []
    last_val_end = 0
    for i, ch in enumerate(s):
        if esc:          esc = False;            continue
        if ch == "\\" and in_string: esc = True; continue
        if ch == '"' and not esc:    in_string = not in_string; continue
        if in_string:    continue
        if ch in "{[":   stack.append(ch)
        elif ch in "]}":
            if stack:    stack.pop()
            last_val_end = i + 1
    trimmed = s[:last_val_end].rstrip().rstrip(",")
    closing  = "".join("}" if c == "{" else "]" for c in reversed(stack))
    try:
        json.loads(trimmed + closing)
        return trimmed + closing
    except Exception:
        return None
    
# ── Writes ─────────────────────────────────────────────────────────────────

def apply_sentiment_modifiers(sb, result: dict, candidates: list[dict], date_str: str, n_candidates: int = 0) -> int:
    """
    Write sentiment_modifier back to signal_log.score_adjusted.
    This is the news-to-score feedback loop: step 19 reads the adjusted scores.
    Clamp final score_adjusted to 0-100.
    """
    sentiment_list = result.get("candidate_sentiment") or []
    if not sentiment_list:
        return 0

    sentiment_map = {s["symbol"]: s for s in sentiment_list}
    written = 0

    for cand in candidates:
        sym = cand["symbol"]
        s   = sentiment_map.get(sym)
        if not s:
            continue
        modifier = float(s.get("sentiment_modifier") or 0)
        if modifier == 0.0:
            continue  # no change needed

        current_score = float(cand.get("score_adjusted") or cand.get("score") or 0)
        # modifier is -0.2 to +0.2 — apply as percentage adjustment
        new_score = round(min(max(current_score * (1 + modifier), 0), 100), 2)

        try:
            sb.table("signal_log").update({
                "score_adjusted": new_score,
                "ai_note": f"[sentiment:{modifier:+.2f}] {s.get('sentiment_reason','')[:200]}",
            }).eq("date", date_str).eq("symbol", sym).execute()
            written += 1
        except Exception as e:
            logger.warning(f"sentiment_modifier write failed for {sym}: {e}")

    neutral = sum(1 for s in sentiment_list if float(s.get("sentiment_modifier") or 0) == 0.0)
    missing = max(0, n_candidates - len(sentiment_list))          # candidates AI never mentioned
    logger.info(
        f"sentiment_modifier: {written} scores updated | "
        f"{neutral} neutral/0.0 skipped | "
        f"{missing} never returned by AI"
        + (" ← possible truncation" if missing > 0 else "")
    )
    return written


def write_lessons(sb, result: dict, date_str: str) -> int:
    written = 0
    for l in (result.get("lessons_generated") or [])[:3]:
        rule = l.get("corrective_rule") or ""
        if not rule or not rule.strip():
            continue
        try:
            sb.table("lessons").insert({
                "date":              date_str,
                "scenario_type":     l.get("scenario_type", "Market/Context"),
                "trigger_event":     "daily_market_intel",
                "linked_event_type": "MARKET_INTEL",
                "impacted_sector":   ", ".join(l.get("applies_to_sectors") or [])[:200],
                "scenario_context":  l.get("observation", ""),
                "what_expected":     "Forward-looking market pattern",
                "what_happened":     l.get("observation", ""),
                "what_failed":       "N/A — proactive lesson",
                "root_cause":        l.get("observation", ""),
                "corrective_rule":   rule,
                "source":            "AI:market_intel",
                "is_active":         True,
                "times_applied":     0,
                "times_worked":      0,
                "confidence":        0.5,  # starts neutral, earns confidence via post_trade
            }).execute()
            written += 1
        except Exception as e:
            logger.warning(f"Lesson write failed: {e}")
    return written


def write_market_intel(sb, result: dict, date_str: str, provider: str):
    tone = result.get("market_tone") or {}
    try:
        sb.table("ai_context").upsert({
            "date":              date_str,
            "symbol":            "__MARKET_INTEL__",
            "conviction":        tone.get("position_sizing_guidance", "REDUCED_25PCT"),
            "conviction_reason": json.dumps(result, ensure_ascii=False),
            "risks":             [i.get("driver", "") for i in
                                  (result.get("macro_sector_impacts") or [])[:3]],
            "catalyst":          "; ".join(tone.get("setup_types_favoured") or []),
            "suggested_action":  tone.get("position_sizing_guidance", "REDUCED_25PCT"),
            "conflicts": json.dumps({
                "fii_exit_sectors":   (result.get("fii_outlook") or {}).get("exit_sectors", []),
                "setup_types_avoid":  tone.get("setup_types_to_avoid", []),
                "regulatory_alerts":  [
                    a for a in (result.get("regulatory_alerts") or [])
                    if a.get("action") not in ("NO_ACTION", None)
                ],
            }, ensure_ascii=False),
            "provider":          provider,
            "ai_note":           (tone.get("summary", "") + " | " +
                                  tone.get("echo_comparison", ""))[:500],
            "fallback_used":     False,
            "confidence":        0.85,
        }, on_conflict="date,symbol").execute()
        logger.info("ai_context __MARKET_INTEL__ written")
        # Write near_miss upgrade flags to signal_log.ai_note
        for upgrade in (result.get("near_miss_upgrades") or []):
            sym = upgrade.get("symbol")
            if not sym: continue
            try:
                sb.table("signal_log").update({
                    "ai_note": f"[NEAR_MISS_UPGRADE:{upgrade.get('flag_for','?')}] "
                            f"{upgrade.get('upgrade_reason','')[:200]}",
                }).eq("date", date_str).eq("symbol", sym).execute()
            except Exception as e:
                logger.warning(f"near_miss upgrade write failed for {sym}: {e}")
    except Exception as e:
        logger.warning(f"ai_context write failed: {e}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch — step 18 skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info("=" * 60)
    logger.info(f"STEP 18: AI Market Intelligence {'[DRY RUN]' if DRY_RUN else ''}")
    logger.info("=" * 60)

    sb           = get_supabase()
    last_td       = get_trade_date(sb, mode="auto", caller="18_ai_market_intel")
    window_start, window_end_trade, window_end_calendar = get_step_window(
        sb, "__MARKET_INTEL__", current_td=last_td
    )

    # Pass 1
    logger.info("Pass 1: building market context...")
    ctx = build_market_context(sb, window_start, window_end_trade, window_end_calendar, last_td)
    logger.info(
        f"  {len(ctx['candidates'])} signal candidates | {len(ctx['news'])} news | "
        f"{len(ctx['sectors'])} sectors | {len(ctx['macro'])} macro indicators | "
        f"{len(ctx['echoes'])} historical echoes"
    )

    # Pass 2
    stock_intel: list[dict] = []
    _intel_targets = ctx["candidates"] or ctx["watch_candidates"]
    if _intel_targets and not DRY_RUN:
        logger.info(f"Pass 2: fetching news for {len(_intel_targets)} candidates (BUY:{len(ctx['candidates'])} WATCH:{len(ctx['watch_candidates'])})...")
        sess = requests.Session()
        try:
            sess.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
            time.sleep(1)
        except Exception:
            pass
        for row in _intel_targets:
            sym = row.get("symbol") or ""
            if not sym:
                continue
            try:
                si = fetch_stock_intel(sym, row.get("company_name") or sym, sess)
                stock_intel.append(si)
            except Exception as e:
                logger.debug(f"News fetch failed for {sym}: {e}")
            time.sleep(0.2)
        total_headlines = sum(len(s["headlines"]) for s in stock_intel)
        logger.info(f"Pass 2 done: {total_headlines} headlines across {len(stock_intel)} stocks")

    prompt = build_prompt(ctx, stock_intel, window_start, window_end_calendar)

    if DRY_RUN:
        logger.info(f"[DRY RUN] Prompt: {len(prompt)} chars")
        logger.info(f"[DRY RUN] Candidates: {[c.get('symbol') for c in ctx['candidates']]}")
        return {"status": "dry_run", "prompt_chars": len(prompt),
                "candidates": len(ctx["candidates"])}

    logger.info("Calling AI...")
    result = call_ai(prompt, n_candidates=len(ctx["candidates"]))

    if not result:
        logger.warning("AI call returned nothing — step 18 non-fatal exit")
        return {"status": "ai_failed"}

    provider = cfg("ai_provider", "unknown")

    # Write 1: sentiment modifiers to signal_log (step 19 reads adjusted scores)
    sentiment_written = apply_sentiment_modifiers(sb, result, ctx["candidates"], last_td)

    # Write 2: lessons
    lessons_written = write_lessons(sb, result, last_td)

    # Write 3: full market intel to ai_context
    write_market_intel(sb, result, last_td, provider)

    tone = result.get("market_tone") or {}
    fii_outlook = result.get("fii_outlook") or {}

    logger.success(
        f"Step 18 done: {sentiment_written} scores updated | {lessons_written} lessons | "
        f"Sizing: {tone.get('position_sizing_guidance','?')} | "
        f"FII 5-session: {fii_outlook.get('5session_bias','?')} | "
        f"Echo: {tone.get('echo_comparison','N/A')[:80]}"
    )
    return {
        "status":             "ok",
        "sentiment_written":  sentiment_written,
        "lessons":            lessons_written,
        "sizing":             tone.get("position_sizing_guidance"),
        "fii_5session":       fii_outlook.get("5session_bias"),
    }


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="TradeOS v6 — Step 18: Market Intelligence")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    print(main())