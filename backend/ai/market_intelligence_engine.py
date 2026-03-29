"""
TradeOS v6 — Phase 2: Market Intelligence Engine
Runs as step 11a — after generate_shortlist (step 10), before send_alerts (step 12).

TWO-PASS EXECUTION:
  Pass 1 (~45s):  Assembles full market context from Supabase + market_news scraped today.
  Pass 2 (~90s):  Fetches stock-specific news for top 12 MSL candidates using 4 sources:
                  NSE corporate announcements, NSE bulk/block deals per symbol,
                  ET RSS filtered by company name, Google News RSS per company.

SINGLE AI CALL (web_search enabled via tools parameter):
  Answers 5 structured questions and returns a single JSON response:
    Q1: Market tone + position sizing guidance
    Q2: Commodity/macro sector headwind/tailwind (per sector, with duration estimate)
    Q3: Regulatory/policy news impact on specific held stocks or candidates
    Q4: FII flow intelligence + 5-session outlook
    Q5: Top 3 actionable candidates with thesis, entry trigger, invalidation

WRITES:
  lessons     — 1-3 rows, source="AI:market_intel", forward-looking daily context
  signal_log  — top 3 candidates, signal_type="MARKET_TOP_PICK"
  ai_context  — 1 row, symbol="__MARKET_INTEL__", full JSON in conviction_reason

Non-fatal throughout — any failure returns gracefully without stopping pipeline.
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
from config import get_supabase, today_ist, is_kill_switch_active, cfg

DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")



def get_shortlist_candidates(sb, today_str):
    import json

    try:
        # STEP 1: Fetch AI shortlist
        row = sb.table("ai_context") \
            .select("ai_note") \
            .eq("date", today_str) \
            .eq("symbol", "__SHORTLIST__") \
            .limit(1) \
            .execute().data

        if row and row[0].get("ai_note"):
            shortlist = json.loads(row[0]["ai_note"])

            symbols = [x["symbol"] for x in shortlist]
            rank_map = {x["symbol"]: x for x in shortlist}

            # STEP 2: Fetch MSL rows
            msl_rows = sb.table("master_shortlist") \
                .select("*") \
                .eq("date", today_str) \
                .in_("symbol", symbols) \
                .execute().data

            # STEP 3: Merge AI + MSL
            enriched = []
            for r in msl_rows:
                sym = r.get("symbol")
                ai = rank_map.get(sym, {})

                r["ai_rank"] = ai.get("rank")
                r["ai_action"] = ai.get("action")
                r["ai_reason"] = ai.get("reason")
                r["ai_risk"] = ai.get("risk")

                enriched.append(r)

            # STEP 4: Sort by AI rank
            enriched.sort(key=lambda x: x.get("ai_rank", 999))

            print("✅ Using AI shortlist")
            return enriched[:12]

    except Exception as e:
        print(f"⚠️ AI shortlist failed: {e}")

    # FALLBACK (your existing logic)
    print("⚠️ Falling back to master_shortlist")

    fallback = sb.table("master_shortlist") \
        .select("*") \
        .eq("date", today_str) \
        .order("final_score", desc=True) \
        .limit(12) \
        .execute().data

    for r in fallback:
        r["ai_rank"] = None
        r["ai_action"] = None
        r["ai_reason"] = None
        r["ai_risk"] = None

    return fallback


# ── Pass 2: Stock-specific news ────────────────────────────────────────────

def _fetch_nse_announcements(symbol: str, session: requests.Session) -> list[str]:
    """NSE corporate announcements for a specific symbol."""
    headlines = []
    try:
        time.sleep(0.3)
        resp = session.get(
            f"https://www.nseindia.com/api/corp-info?symbol={symbol}&type=announcements",
            headers=HEADERS, timeout=6
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in (data.get("data") or [])[:4]:
                desc = item.get("desc") or item.get("subject") or ""
                if desc:
                    headlines.append(f"[NSE] {desc[:200]}")
    except Exception:
        pass
    return headlines


def _fetch_nse_bulk_deals_symbol(symbol: str, session: requests.Session) -> list[str]:
    """NSE bulk deals for a specific symbol (institutional activity)."""
    headlines = []
    try:
        resp = session.get(
            f"https://www.nseindia.com/api/bulk-deals?symbol={symbol}",
            headers=HEADERS, timeout=6
        )
        if resp.status_code == 200:
            deals = resp.json().get("data") or []
            for d in deals[:3]:
                entity = d.get("clientName") or d.get("client") or ""
                side   = d.get("buySell") or ""
                qty    = d.get("qty") or 0
                price  = d.get("price") or 0
                if entity:
                    headlines.append(
                        f"[Bulk] {entity} {side}: {int(qty):,} @ ₹{float(price):.0f}"
                    )
    except Exception:
        pass
    return headlines


def _fetch_google_news_symbol(company_name: str) -> list[str]:
    """Google News RSS for a specific company (aggregates 200+ sources)."""
    headlines = []
    try:
        query   = company_name.replace(" ", "+") + "+NSE+India"
        url     = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        resp    = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            root = ET.fromstring(resp.text)
            for item in root.iter("item"):
                title = item.findtext("title") or ""
                # Remove source suffix
                title = re.sub(r"\s*-\s*[^-]+$", "", title).strip()
                if title and len(title) > 10:
                    headlines.append(f"[News] {title[:200]}")
                if len(headlines) >= 3:
                    break
    except Exception:
        pass
    return headlines


def fetch_stock_intel(symbol: str, company_name: str, nse_session: requests.Session) -> dict:
    """
    Aggregate stock-specific intelligence from 3 sources.
    Returns a dict with headlines list and institutional_buying flag.
    """
    headlines: list[str] = []
    headlines.extend(_fetch_nse_announcements(symbol, nse_session))
    headlines.extend(_fetch_nse_bulk_deals_symbol(symbol, nse_session))
    headlines.extend(_fetch_google_news_symbol(company_name or symbol))

    institutional_buying = any(
        "[bulk]" in h.lower() and any(
            k in h.lower() for k in ("buy", "b", "purchase")
        )
        for h in headlines
    )
    return {
        "symbol":              symbol,
        "company_name":        company_name,
        "headlines":           headlines[:10],
        "institutional_buying": institutional_buying,
    }


# ── Context assembly (Pass 1) ──────────────────────────────────────────────

def build_market_context(sb, today_str: str) -> dict:
    """
    Assemble full market context from all Supabase tables.
    Returns a structured dict used in the AI prompt.
    """

    # Market regime
    regime_rows = sb.table("market_regime").select(
        "regime,india_vix,nifty_price,avg_sector_breadth,date"
    ).order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # FII/DII flows
    fii_rows = sb.table("fii_dii_flow").select(
        "fii_net,fii_net_5d,fii_net_20d,fii_flag,dii_net,date"
    ).order("date", desc=True).limit(1).execute().data
    fii = fii_rows[0] if fii_rows else {}

    # Global cues
    cues_rows = sb.table("global_cues").select("*").order("date", desc=True).limit(1).execute().data
    cues = cues_rows[0] if cues_rows else {}

    # Sector strength — top 5 by rank
    sector_rows = sb.table("sector_strength").select(
        "sector,rank,sector_state,avg_rsi_daily"
    ).eq("date", today_str).order("rank").limit(5).execute().data
    if not sector_rows:
        sector_rows = sb.table("sector_strength").select(
            "sector,rank,sector_state,avg_rsi_daily"
        ).order("date", desc=True).limit(5).execute().data

    # Industry strength — top 5
    ind_rows = sb.table("industry_strength").select(
        "industry,rank,industry_state,avg_rsi_daily"
    ).eq("date", today_str).order("rank").limit(5).execute().data

    # Market news (scraped today by step 00a)
    news_rows = sb.table("market_news").select(
        "headline,source,category,impact_type,parsed_symbols"
    ).eq("news_date", today_str).order("id", desc=True).limit(30).execute().data

    # MSL top 12 candidates by score
    msl_rows = get_shortlist_candidates(sb, today_str)

    # Open positions (for Q3 regulatory check)
    pos_rows = sb.table("open_positions").select(
        "symbol,sector,pnl_pct,active_sl"
    ).eq("status", "ACTIVE").execute().data

    # Active lessons (top 5 by times_applied)
    lesson_rows = sb.table("lessons").select(
        "corrective_rule,scenario_type,times_applied"
    ).eq("is_active", True).order("times_applied", desc=True).limit(5).execute().data

    # Upcoming events (next 14 days)
    cutoff = str(today_ist() + timedelta(days=14))
    event_rows = sb.table("event_calendar").select(
        "event_name,event_type,affected_sectors,event_bias,event_intensity"
    ).eq("is_active", True).lte("start_date", cutoff).execute().data

    return {
        "regime":     regime,
        "fii":        fii,
        "cues":       cues,
        "sectors":    sector_rows,
        "industries": ind_rows,
        "news":       news_rows,
        "msl":        msl_rows,
        "positions":  pos_rows,
        "lessons":    lesson_rows,
        "events":     event_rows,
    }


# ── Prompt builder ─────────────────────────────────────────────────────────

def build_prompt(ctx: dict, stock_intel: list[dict]) -> str:
    """Build the full 5-question prompt with all context."""
    r   = ctx["regime"]
    fii = ctx["fii"]
    c   = ctx["cues"]

    lines = [
        f"TODAY: {today_ist()}",
        f"MARKET REGIME: {r.get('regime','?')} | Nifty: {r.get('nifty_price','?')} | VIX: {r.get('india_vix','?')} | Breadth: {r.get('avg_sector_breadth','?')}%",
        "",
        "FII/DII FLOWS:",
        f"  Today: FII ₹{fii.get('fii_net',0):+,.0f} Cr | DII ₹{fii.get('dii_net',0):+,.0f} Cr",
        f"  5-day FII net: ₹{float(fii.get('fii_net_5d') or 0):+,.0f} Cr | 20-day: ₹{float(fii.get('fii_net_20d') or 0):+,.0f} Cr | Flag: {fii.get('fii_flag','?')}",
        "",
        "GLOBAL MACRO:",
        f"  Gift Nifty: {c.get('gift_nifty','?')} ({c.get('gift_nifty_chg_pct',0):+.2f}%) | Gap: {c.get('gap_signal','?')}",
        f"  DOW: {c.get('us_dow_chg_pct',0):+.2f}% | S&P: {c.get('sp500_chg_pct',0):+.2f}% | NQ: {c.get('us_nasdaq_chg_pct',0):+.2f}%",
        f"  Brent Crude: ${c.get('brent_crude','?')} ({c.get('brent_chg_pct',0):+.2f}%)",
        f"  Gold: ${c.get('gold_price','?')} | USD/INR: ₹{c.get('usd_inr','?')} ({c.get('usd_inr_chg_pct',0):+.3f}%)",
        "",
        "TOP SECTORS (by rank):",
    ]
    for s in ctx["sectors"]:
        lines.append(f"  #{s.get('rank','?')} {s.get('sector','?')} — {s.get('sector_state','?')} | RSI: {s.get('avg_rsi_daily','?')}")

    lines.append("\nREGULATORY & POLICY NEWS (last 24h):")
    for n in ctx["news"][:15]:
        lines.append(f"  [{n.get('source','?')}] {n.get('headline','')[:120]}")

    lines.append("\nUPCOMING EVENTS (next 14 days):")
    for e in ctx["events"][:8]:
        lines.append(f"  {e.get('event_name','?')} | {e.get('event_type','?')} | Sectors: {e.get('affected_sectors','?')} | Bias: {e.get('event_bias','?')}")

    lines.append("\nOPEN POSITIONS (check for regulatory alerts):")
    for p in ctx["positions"]:
        lines.append(f"  {p.get('symbol','?')} | {p.get('sector','?')} | P&L: {float(p.get('pnl_pct') or 0):+.1f}%")

    lines.append("\nACTIVE LESSONS (top 5 by use):")
    for l in ctx["lessons"]:
        lines.append(f"  [{l.get('scenario_type','?')}] {l.get('corrective_rule','')[:150]}")

    lines.append("\nTOP 12 MSL CANDIDATES (by score):")
    for m in ctx["msl"]:
        lines.append(
            f"  {m.get('symbol','?')} | {m.get('sector','?')} | Score:{m.get('final_score',0):.0f} | "
            f"Lifecycle:{m.get('lifecycle','?')} | Validity:{m.get('validity_score','?')} | "
            f"ExpR:{m.get('expected_r','?')} | Trend:{m.get('trend_maturity','?')} | "
            f"Zone:₹{m.get('entry_zone_low','?')}-{m.get('entry_zone_high','?')} CMP:₹{m.get('current_price','?')}"
        )

    if stock_intel:
        lines.append("\nSTOCK-SPECIFIC NEWS (top 12 candidates):")
        for si in stock_intel:
            if si["headlines"]:
                lines.append(f"  {si['symbol']} ({si.get('company_name','')}):")
                for h in si["headlines"][:4]:
                    lines.append(f"    {h}")
                if si["institutional_buying"]:
                    lines.append(f"    ★ INSTITUTIONAL BUYING confirmed via bulk deals")

    lines.append("""
Answer these 5 questions. Output ONLY valid JSON — no preamble, no markdown:
{
  "market_tone": {
    "summary": "2 sentences max — dominant market dynamic and what it means for new entries",
    "setup_types_favoured": ["e.g. REENTRY_SETUP in defensive sectors"],
    "setup_types_to_avoid": ["e.g. PRE_BREAKOUT in commodity if crude falling"],
    "position_sizing_guidance": "FULL | REDUCED_25PCT | HALF | MINIMAL"
  },
  "macro_sector_impacts": [
    {
      "driver": "e.g. Brent crude +2.3%",
      "tailwind_sectors": ["exact sector names from NSE 500"],
      "headwind_sectors": ["exact sector names from NSE 500"],
      "magnitude": "HIGH | MEDIUM | LOW",
      "sessions": 3
    }
  ],
  "regulatory_alerts": [
    {
      "news_item": "headline",
      "affected_symbols": ["NSE_SYMBOL"],
      "affected_sectors": ["sector"],
      "action": "WATCH | EXIT | AVOID_NEW_ENTRY | NO_ACTION",
      "urgency": "IMMEDIATE | THIS_WEEK | LOW"
    }
  ],
  "fii_outlook": {
    "5session_bias": "BUYING | SELLING | NEUTRAL",
    "favoured_sectors": ["sector names"],
    "exit_sectors": ["sector names"],
    "confidence": "HIGH | MEDIUM | LOW",
    "key_signal_to_watch": "one specific data point that will confirm or deny this view"
  },
  "top_3_candidates": [
    {
      "rank": 1,
      "symbol": "NSE_SYMBOL",
      "thesis": "why this stock in these exact conditions — 2 sentences",
      "entry_trigger": "specific price level or volume event",
      "session_window": "enter within N sessions if trigger fires",
      "invalidation": "one condition that kills the setup",
      "macro_tailwind": "which macro driver from today supports this",
      "risk_flag": "main risk"
    }
  ],
  "lessons_generated": [
    {
      "scenario_type": "e.g. Market/FII-Caution or Commodity/Gold-Up",
      "observation": "pattern visible in today's data",
      "corrective_rule": "Rule: actionable instruction starting with Rule:",
      "applies_to_sectors": ["sector1", "sector2"]
    }
  ]
}""")

    return "\n".join(lines)


# ── AI call ────────────────────────────────────────────────────────────────

def call_ai(prompt: str) -> dict | None:
    """
    Single AI call with web_search tool enabled.
    Falls back to non-search call if web_search not available.
    Returns parsed JSON dict or None on failure.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — market_intelligence_engine skipped")
        return None

    payload = {
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 2000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [
            {
                "role":    "user",
                "content": (
                    "You are a senior Indian equity portfolio manager specialising in NSE 500 "
                    "swing trading. Analyse the daily market intelligence packet below and produce "
                    "precise, actionable output. Be specific about sectors and timeframes. "
                    "Search the web for any breaking news that may not be captured in the data.\n\n"
                    + prompt
                )
            }
        ],
    }

    try:
        resp = requests.post(
            ANTHROPIC_API_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        if resp.status_code != 200:
            logger.warning(f"AI call failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return None

        data    = resp.json()
        content = data.get("content", [])
        # Collect all text blocks (tool results + final text)
        text_parts = [b["text"] for b in content if b.get("type") == "text"]
        full_text  = "\n".join(text_parts)

        # Extract JSON from response
        json_match = re.search(r"\{[\s\S]+\}", full_text)
        if not json_match:
            logger.warning("AI response contained no JSON block")
            return None

        return json.loads(json_match.group())

    except json.JSONDecodeError as e:
        logger.warning(f"AI response JSON parse failed: {e}")
        return None
    except Exception as e:
        logger.warning(f"AI call exception: {e}")
        return None


# ── Write results ──────────────────────────────────────────────────────────

def write_lessons(sb, result: dict, today_str: str) -> int:
    """Write 1-3 forward-looking market context lessons."""
    lessons_raw = result.get("lessons_generated") or []
    if not lessons_raw:
        return 0
    written = 0
    for l in lessons_raw[:3]:
        rule = l.get("corrective_rule") or ""
        if not rule:
            continue
        try:
            sb.table("lessons").insert({
                "date":             today_str,
                "scenario_type":    l.get("scenario_type", "Market/Context"),
                "trigger_event":    "daily_market_intel",
                "linked_event_type": "MARKET_INTEL",
                "impacted_sector":  ", ".join(l.get("applies_to_sectors") or [])[:200],
                "scenario_context": l.get("observation", ""),
                "what_expected":    "Market intelligence forward-looking signal",
                "what_happened":    l.get("observation", ""),
                "what_failed":      "N/A — proactive lesson",
                "root_cause":       l.get("observation", ""),
                "corrective_rule":  rule,
                "source":           "AI:market_intel",
                "is_active":        True,
                "times_applied":    0,
                "times_worked":     0,
                "confidence":       0.8,
            }).execute()
            written += 1
        except Exception as e:
            logger.warning(f"Lesson write failed: {e}")
    return written


def write_top_candidates(sb, result: dict, today_str: str) -> int:
    """Write top 3 candidates as MARKET_TOP_PICK signals to signal_log."""
    candidates = result.get("top_3_candidates") or []
    if not candidates:
        return 0
    written = 0
    for c in candidates[:3]:
        symbol = c.get("symbol") or ""
        if not symbol:
            continue
        try:
            sb.table("signal_log").upsert({
                "date":           today_str,
                "symbol":         symbol,
                "signal_type":    "MARKET_TOP_PICK",
                "signal_subtype": "MARKET_INTEL",
                "position_state": "BUY_CANDIDATE",
                "score":          90 - ((c.get("rank", 1) - 1) * 5),   # 90/85/80
                "score_adjusted": 90 - ((c.get("rank", 1) - 1) * 5),
                "ai_conviction":  "HIGH",
                "ai_conviction_reason": c.get("thesis", ""),
                "ai_risks":       [c.get("risk_flag", "")],
                "ai_catalyst":    c.get("macro_tailwind", ""),
                "ai_suggested_action": "ENTER",
                "ai_note": (
                    f"Entry trigger: {c.get('entry_trigger','')} | "
                    f"Window: {c.get('session_window','')} | "
                    f"Invalidation: {c.get('invalidation','')}"
                ),
                "ai_provider":    "market_intelligence_engine",
                "regime":         result.get("market_tone", {}).get("summary", "")[:50],
                "sheet_conflict": False,
            }, on_conflict="date,symbol").execute()
            written += 1
        except Exception as e:
            logger.warning(f"signal_log MARKET_TOP_PICK write failed for {symbol}: {e}")
    return written


def write_ai_context(sb, result: dict, today_str: str):
    """Write full market intel JSON to ai_context as __MARKET_INTEL__ row."""
    try:
        tone = result.get("market_tone") or {}
        sb.table("ai_context").upsert({
            "date":              today_str,
            "symbol":            "__MARKET_INTEL__",
            "conviction":        tone.get("position_sizing_guidance", "REDUCED_25PCT"),
            "conviction_reason": json.dumps(result, ensure_ascii=False)[:8000],
            "risks":             [i.get("driver", "") for i in (result.get("macro_sector_impacts") or [])[:3]],
            "catalyst":          "; ".join(tone.get("setup_types_favoured") or []),
            "suggested_action":  tone.get("position_sizing_guidance", "REDUCED_25PCT"),
            "provider":          "market_intelligence_engine",
            "fallback_used":     False,
            "confidence":        0.85,
        }, on_conflict="date,symbol").execute()
    except Exception as e:
        logger.warning(f"ai_context __MARKET_INTEL__ write failed: {e}")

    # ── AG7 FIX: track market_intelligence_engine cost in ai_model_performance ──
    # Previously: this daily Anthropic API call (with web_search) was invisible to
    # evolution_tracker's provider performance analysis — could be spending ₹50/day
    # without knowing. Now writes a proxy row so evolution can track quality over time.
    # accuracy proxy: 1.0 if we got 3 top candidates (AI delivered useful output),
    #                 0.5 if fewer (partial result), 0.0 if empty (AI call failed).
    try:
        candidates = result.get("top_3_candidates") or []
        accuracy_proxy = 1.0 if len(candidates) >= 3 else (0.5 if candidates else 0.0)
        sb.table("ai_model_performance").insert({
            "date":           today_str,
            "provider":       "market_intelligence_engine",
            "model":          "claude-sonnet-4-20250514",
            "calls_today":    1,
            "cost_today":     0.0,      # web_search billing varies — placeholder for now
            "accuracy":       accuracy_proxy,
            "avg_confidence": 0.85,
            "fallback_used":  False,
        }).execute()
    except Exception as e:
        logger.debug(f"ai_model_performance write skipped (non-fatal): {e}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — market_intelligence_engine skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info(f"Market Intelligence Engine starting {'[DRY RUN]' if DRY_RUN else ''}")
    sb        = get_supabase()
    today_str = str(today_ist())

    # ── Pass 1: Assemble Supabase context ────────────────────────────────
    logger.info("Pass 1: assembling market context from Supabase...")
    ctx = build_market_context(sb, today_str)
    logger.info(
        f"  Context: {len(ctx['msl'])} MSL candidates | "
        f"{len(ctx['news'])} news items | {len(ctx['sectors'])} sectors"
    )

    # ── Pass 2: Stock-specific news for top 12 candidates ────────────────
    stock_intel: list[dict] = []
    if ctx["msl"] and not DRY_RUN:
        logger.info(f"Pass 2: fetching stock-specific news for {len(ctx['msl'])} candidates...")
        nse_session = requests.Session()
        try:
            # Warm NSE session
            nse_session.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
            time.sleep(1)
        except Exception:
            pass  # proceed without NSE session — Google News still works

        for msl_row in ctx["msl"]:
            sym   = msl_row.get("symbol") or ""
            cname = msl_row.get("company_name") or sym
            if not sym:
                continue
            try:
                si = fetch_stock_intel(sym, cname, nse_session)
                stock_intel.append(si)
                logger.debug(f"  {sym}: {len(si['headlines'])} headlines")
            except Exception as e:
                logger.debug(f"  {sym}: news fetch failed (non-fatal): {e}")
            time.sleep(0.2)

        logger.info(f"Pass 2 done: {sum(len(s['headlines']) for s in stock_intel)} headlines across {len(stock_intel)} stocks")

    # ── Build prompt and call AI ─────────────────────────────────────────
    logger.info("Building AI prompt...")
    prompt = build_prompt(ctx, stock_intel)

    if DRY_RUN:
        logger.info(f"[DRY RUN] Prompt length: {len(prompt)} chars. Skipping AI call.")
        logger.info(f"[DRY RUN] MSL candidates: {[m.get('symbol') for m in ctx['msl']]}")
        return {"status": "dry_run", "prompt_chars": len(prompt)}

    logger.info("Calling AI (web_search enabled)...")
    result = call_ai(prompt)

    if not result:
        logger.warning("AI call returned no result — market_intelligence_engine non-fatal exit")
        return {"status": "ai_failed", "lessons": 0, "signals": 0}

    # ── Write outputs ─────────────────────────────────────────────────────
    lessons_written  = write_lessons(sb, result, today_str)
    signals_written  = write_top_candidates(sb, result, today_str)
    write_ai_context(sb, result, today_str)

    # Log top 3 for visibility
    tone       = result.get("market_tone") or {}
    candidates = result.get("top_3_candidates") or []
    logger.success(
        f"Market Intelligence done: "
        f"{lessons_written} lessons | {signals_written} MARKET_TOP_PICK signals | "
        f"Sizing: {tone.get('position_sizing_guidance','?')} | "
        f"Top picks: {[c.get('symbol') for c in candidates]}"
    )
    return {
        "status":   "ok",
        "lessons":  lessons_written,
        "signals":  signals_written,
        "sizing":   tone.get("position_sizing_guidance"),
        "top_3":    [c.get("symbol") for c in candidates],
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TradeOS v6 — Market Intelligence Engine")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--symbol", help="Debug single stock news fetch")
    args = parser.parse_args()
    if args.dry_run:
        os.environ["DRY_RUN"] = "True"
    if args.symbol:
        # Debug mode: just fetch and print news for one symbol
        import requests as _req
        session = _req.Session()
        try:
            session.get("https://www.nseindia.com", headers=HEADERS, timeout=8)
        except Exception:
            pass
        intel = fetch_stock_intel(args.symbol, args.symbol, session)
        print(f"\nNews for {args.symbol}:")
        for h in intel["headlines"]:
            print(f"  {h}")
        print(f"Institutional buying: {intel['institutional_buying']}")
    else:
        print(main())
