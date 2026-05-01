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
from config import get_supabase, today_ist, is_kill_switch_active, cfg, AI_KEYS

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

def _last_trading_day(sb) -> str:
    try:
        holidays = {r["date"][:10] for r in sb.table("nse_holidays").select("date").execute().data}
    except Exception:
        holidays = set()
    d = today_ist()
    for _ in range(10):
        d -= timedelta(days=1)
        if d.weekday() < 5 and str(d) not in holidays:
            return str(d)
    return str(today_ist() - timedelta(days=1))


def _lesson_confidence(lesson: dict) -> float:
    applied = lesson.get("times_applied") or 0
    worked  = lesson.get("times_worked")  or 0
    if applied < 3:
        return round(lesson.get("confidence") or 0.5, 2)  # not enough data, use stored
    return round(worked / applied, 2)


# ── Pass 1: Context assembly ───────────────────────────────────────────────

def build_market_context(sb, today_str: str, last_td: str) -> dict:
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
    cues = (sb.table("global_cues").select("*")
              .eq("session", "EVENING").order("date", desc=True).limit(1).execute().data)
    if not cues:
        cues = (sb.table("global_cues").select("*").order("date", desc=True).limit(1).execute().data)
    cues = cues[0] if cues else {}
    sector_impacts = cues.pop("sector_impacts", {}) or {}

    # ── Macro indicators — last 10 across all indicators ──
    macro_rows = (sb.table("macro_indicators")
                    .select("indicator_date,indicator_name,indicator_value,"
                            "previous_value,change_bps,source")
                    .order("indicator_date", desc=True).limit(10).execute().data)

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

    # ── Market news ──
    news = (sb.table("market_news")
              .select("headline,source,category,impact_type,parsed_symbols")
              .eq("news_date", today_str).order("id", desc=True).limit(40).execute().data)

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
                          "eap_action,in_rule_engine,in_scanner")
                  .eq("date", last_td)
                  .in_("signal_type", ["PRIME_SETUP","BREAKOUT_SETUP","REENTRY_SETUP",
                                       "BUY_CANDIDATE","STAGED_ENTRY"])
                  .order("score_adjusted", desc=True)
                  .limit(30).execute().data)

    # ── JOIN with master_shortlist for fields not in signal_log ──
    if sig_rows:
        symbols = [r["symbol"] for r in sig_rows]
        msl_rows = (sb.table("master_shortlist")
                      .select("symbol,current_price,entry_zone_low,entry_zone_high,"
                              "dist_entry_pct,convergence_pts,engines_count,"
                              "fundamental_quality,market_cap,st_cushion_pct,"
                              "bb_width_pct,bb_position_pct,dist_vwap_20d_pct,"
                              "ma_alignment_score,stoch_context,persistent_phase,"
                              "reentry_mode,position_state,suggested")
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

def build_prompt(ctx: dict, stock_intel: list[dict]) -> str:
    r   = ctx["regime"]
    fii = ctx["fii"]
    c   = ctx["cues"]

    regime_label = r.get("predicted_regime") or r.get("regime", "UNKNOWN")
    regime_conf  = float(r.get("regime_confidence") or 0)

    lines = [
        f"DATE: {today_ist()}",
        "",
        "═══ MARKET REGIME ═══",
        f"  Regime: {regime_label} (confidence: {regime_conf:.0%}) | Score: {r.get('regime_score','?')}",
        f"  Nifty: ₹{r.get('nifty_price','?')} | 1d: {float(r.get('nifty_1d_chg_pct',0) or 0):+.2f}% | "
        f"5d: {float(r.get('nifty_5d_chg_pct',0) or 0):+.2f}% | 20d: {float(r.get('nifty_20d_chg_pct',0) or 0):+.2f}%",
        f"  50DMA: {r.get('nifty_50dma','?')} | 200DMA: {r.get('nifty_200dma','?')} | "
        f"Weekly RSI: {r.get('nifty_weekly_rsi','?')}",
        f"  VIX: {r.get('india_vix','?')} (5d Δ: {r.get('vix_5d_delta','?')}) | "
        f"A/D: {r.get('advance_decline_ratio','?')} | Above 200DMA: {r.get('above_200dma_pct','?')}%",
        f"  Breadth: {r.get('avg_sector_breadth','?')}% | PCR: {r.get('nifty_pcr','?')} | "
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
    ]

    if ctx["sector_impacts"]:
        lines.append(f"  Sector impacts (from global): {json.dumps(ctx['sector_impacts'])[:400]}")

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
        lines.append(f"  [{n.get('source','?')}] {n.get('headline','')[:130]}")

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

    lines += [
        "",
        "═══ VALIDATED SIGNAL CANDIDATES (post step-15 gate filtering) ═══",
        "  These stocks passed 11 technical gates: ATR-normalised zone, R:R viability,",
        "  structural breakdown check, risk_score, liquidity, lifecycle, momentum gates.",
        "  Signal quality: PRIME_SETUP > BREAKOUT_SETUP > REENTRY_SETUP > BUY_CANDIDATE",
        "  Your task: assess each against TODAY's macro/FII/news context. Output sentiment_modifier.",
    ]
    for m in ctx["candidates"]:
        lines.append(
            f"  {m.get('symbol','?')} | {m.get('sector','?')} | {m.get('signal_type','?')} | "
            f"Score:{float(m.get('score_adjusted',0) or 0):.1f} | "
            f"CMP:₹{m.get('current_price','?')} Zone:₹{m.get('entry_zone_low','?')}-{m.get('entry_zone_high','?')} "
            f"Dist:{m.get('dist_entry_pct','?')}% | "
            f"Lifecycle:{m.get('lifecycle','?')} ExpR:{m.get('expected_r_msl','?')}x | "
            f"Validity:{m.get('validity_score','?')} | "
            f"Engines:{m.get('engines_count','?')} Conv:{m.get('convergence_pts','?')} | "
            f"SectorRk:{m.get('sector_rank_at_entry','?')} IndTop5:{m.get('industry_top5','?')} | "
            f"FIIFlag:{m.get('fii_flag','?')} | ST:{m.get('st_cushion_pct','?')}% | "
            f"Trigger:{m.get('days_to_trigger_est','?')}d | "
            f"Quality:{m.get('fundamental_quality','?')} MCap:₹{m.get('market_cap','?')}Cr"
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
  "candidate_sentiment": [
    {
      "symbol": "NSE_SYMBOL",
      "sentiment_modifier": 0.05,
      "sentiment_reason": "one line — what drove this adjustment",
      "news_flag": "POSITIVE | NEGATIVE | NEUTRAL | INSTITUTIONAL_BUY | RESULTS_RISK"
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
                "model": _CLAUDE_MODEL, "max_tokens": 3000,
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


def call_ai(prompt: str) -> dict | None:
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
                full_text = raw_completion(f"{_SYSTEM_PROMPT}\n\n{prompt}", max_tokens=3000)
            except Exception as e:
                logger.warning(f"Claude fallback also failed: {e}")
                return None
    else:
        try:
            full_text = raw_completion(f"{_SYSTEM_PROMPT}\n\n{prompt}", max_tokens=3000)
        except Exception as e:
            logger.warning(f"AI call failed for provider={provider}: {e}")
            return None

    if not full_text:
        return None

    try:
        m = re.search(r"\{[\s\S]+\}", full_text)
        return json.loads(m.group()) if m else None
    except Exception as e:
        logger.warning(f"JSON parse failed: {e}")
        return None


# ── Writes ─────────────────────────────────────────────────────────────────

def apply_sentiment_modifiers(sb, result: dict, candidates: list[dict], date_str: str) -> int:
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

    logger.info(f"sentiment_modifier: {written} signal_log scores updated")
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
            "conviction_reason": json.dumps(result, ensure_ascii=False)[:8000],
            "risks":             [i.get("driver", "") for i in
                                  (result.get("macro_sector_impacts") or [])[:3]],
            "catalyst":          "; ".join(tone.get("setup_types_favoured") or []),
            "suggested_action":  tone.get("position_sizing_guidance", "REDUCED_25PCT"),
            "provider":          provider,
            "ai_note":           (tone.get("summary", "") + " | " +
                                  tone.get("echo_comparison", ""))[:500],
            "fallback_used":     False,
            "confidence":        0.85,
        }, on_conflict="date,symbol").execute()
        logger.info("ai_context __MARKET_INTEL__ written")
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

    sb        = get_supabase()
    today_str = str(today_ist())
    last_td   = _last_trading_day(sb)
    logger.info(f"Today: {today_str} | Last trading day: {last_td}")

    # Pass 1
    logger.info("Pass 1: building market context...")
    ctx = build_market_context(sb, today_str, last_td)
    logger.info(
        f"  {len(ctx['candidates'])} signal candidates | {len(ctx['news'])} news | "
        f"{len(ctx['sectors'])} sectors | {len(ctx['macro'])} macro indicators | "
        f"{len(ctx['echoes'])} historical echoes"
    )

    # Pass 2
    stock_intel: list[dict] = []
    if ctx["candidates"] and not DRY_RUN:
        logger.info(f"Pass 2: fetching news for {len(ctx['candidates'])} candidates...")
        sess = requests.Session()
        try:
            sess.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
            time.sleep(1)
        except Exception:
            pass
        for row in ctx["candidates"]:
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

    prompt = build_prompt(ctx, stock_intel)

    if DRY_RUN:
        logger.info(f"[DRY RUN] Prompt: {len(prompt)} chars")
        logger.info(f"[DRY RUN] Candidates: {[c.get('symbol') for c in ctx['candidates']]}")
        return {"status": "dry_run", "prompt_chars": len(prompt),
                "candidates": len(ctx["candidates"])}

    logger.info("Calling AI...")
    result = call_ai(prompt)

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