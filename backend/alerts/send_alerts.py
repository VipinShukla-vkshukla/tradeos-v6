"""
TradeOS v6 — Send Alerts v5
============================
Pipeline position: final step — after step 19 (ai_decision_engine).

WHAT CHANGED FROM v4 → v5:

PATCH 1 — entry_readiness import
  Optional import of analysis.entry_readiness.compute_entry_readiness.
  Graceful fallback if module not present (_READINESS_AVAILABLE = False).

PATCH 2 — STOP_BUFFER constant
  Reads stop_buffer_pct from config; defaults to 0.03 (3%).
  Used by TIER_1 block and TOMORROW'S GTT ORDERS section.

PATCH 3 — build_position_block: compressed rationale (evening)
  Removed static 130-char entry thesis.
  Added 85-char compressed rationale sourced from entry_thesis_map
  OR today's signal_log.ai_conviction_reason — whichever is available.
  Morning (brief=True) path is unchanged.

PATCH 4 — build_evening: sb=None signature
  Allows passing Supabase client for entry_readiness enrichment.

PATCH 5 — build_evening: restored echo at 300 chars with 📖 label
  Previously 200 chars, no icon. Now 📖 prefixed, 300 chars, esc()-wrapped.

PATCH 6 — build_evening: MACRO + FII FLOW section (new, before market intel)
  Shows FII 5-session sector flow (buying/selling) and regulatory alerts
  in a dedicated top-level section. Complements build_regime_header which
  already shows global indices — no duplication.
  Market Intelligence section simplified to summary + echo only.

PATCH 7 — build_evening: single guidance line
  Priority cascade: capital_deployment_narrative → new_positions_guidance →
  _ai_note. Sectors compressed to ▲/▼ inline. One text line max.

PATCH 8 — build_evening: TIER_1 compressed (3 lines) + readiness
  Line 1: symbol · zone range · dist · T1 · SL · R:R · readiness score
  Line 2: entry_note · invalidation · readiness breakdown
  Line 3: 💬 thesis + catalyst (evening learning layer)
  Optional compute_entry_readiness enrichment when sb is available.

PATCH 9 — build_evening: TIER_2 compressed (2 lines)
  Line 1: symbol · zone range · dist
  Line 2: entry_note · invalidation

PATCH 10 — build_evening: TOMORROW'S GTT ORDERS section
  Appended before return. Shows Entry / SL / T1 / RR for all TIER_1 picks
  with vol_ratio + delivery_pct reference (sourced from signal_log).
  signal_log select updated to include vol_ratio, delivery_pct.

PATCH 11 — build_morning: sb=None signature
  Mirrors build_evening for entry_readiness enrichment at morning runtime.

PATCH 12 — build_morning: single guidance line
  Same priority cascade as Patch 7. Sectors inline.

PATCH 13 — build_morning: TIER_1 live zone status + readiness
  Line 1: symbol · zone_status (live via yfinance from readiness, or
           computed from dist_entry_pct) · readiness score
  Line 2: Entry / SL / T1 / RR · timing window
  Line 3: entry_note / invalidation

PATCH 14 — build_morning: TIER_2 compact (2 lines)
  Inline zone computation replaces zone_line() call.

PATCH 15 — main(): pass sb to both builders
  build_morning(data, sb=sb) and build_evening(data, sb=sb).

UNCHANGED from v4:
  zone_line() — simplified to plain text (no <b> tags) to be safe with esc().
  Sector concentration warnings and correlation groups — already full detail in v4.
  build_compact(), send_signal_with_keyboard() — no changes.
  load_data() — only signal_log select gains vol_ratio, delivery_pct columns.

DATA SOURCES (all bound to confirmed schema):
  ai_context         → __FINAL_PICKS__ (conviction_reason + strategy_validation)
                        __MARKET_INTEL__ (conviction_reason + ai_note)
  signal_log         → all signals for signal_date
                        signal types: EXIT, MARKET_TOP_PICK, BUY_CANDIDATE,
                        REENTRY_SETUP, STAGED_ENTRY, WATCH, AVOID_ENTRY_EVENT
  open_positions     → full schema including target_1/2/3, high_water_mark,
                        locked_profit, partial_bookings, original_qty, current_qty
  master_shortlist   → entry zones, expected_r, dist_entry_pct
  market_regime      → regime, nifty, vix, breadth, A/D, PCR
  fii_dii_flow       → fii_net, fii_net_5d, fii_net_20d, fii_flag
  global_cues        → gift_nifty, gap_signal, global indices, crude, INR
  nifty_upcoming_events → symbol, purpose, details, event_date, days_to_event
  lessons            → 7-day AI + rule counts
  data_anomalies     → ERROR-level alerts

MESSAGE STRUCTURE:
  EVENING:
    Header: regime + FII + macro snapshot (build_regime_header)
    Section 0: MACRO + FII FLOW (FII sector flow + regulatory alerts) [NEW v5]
    Section 1: Market Intelligence (summary + echo)
    Section 2: Portfolio Health Snapshot
    Section 3: Open Positions (full lifecycle)
    Section 4: EXIT signals
    Section 5: TIER_1 — Act Now (3-line compact + readiness)
    Section 6: TIER_2 — Watch for Trigger (2-line compact)
    Section 7: TIER_3 — Monitor
    Section 8: Near Miss
    Section 9: Sector Warnings + Correlation Groups (full detail)
    Section 10: Upcoming Events
    Footer: GTT Orders + lesson count

  MORNING:
    Header: regime + Gift Nifty gap
    Gap Risk Alert: SL breach risk positions
    Position Pulse: brief position status
    EXIT Today
    TIER_1 Watchlist (live zone status + entry levels)
    TIER_2 Intraday Triggers (2-line compact)
    Today's Events + Next 3 days events

  AFTERNOON:
    Header: date + time
    TIER_1 zone status only (live yfinance prices)
    IN ZONE / APPROACHING / ABOVE / MISSED per pick
    Timing note for 1:30–2:30 PM entry window

  COMPACT:
    One-screen mobile summary
"""

import sys
import json
import time
from datetime import timedelta
from pathlib import Path
import html

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import (
    get_supabase, today_ist, is_kill_switch_active,
    cfg, cfg_bool, DRY_RUN,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)

# ── PATCH 1: entry_readiness import ───────────────────────────────────────
try:
    from analysis.entry_readiness import compute_entry_readiness
    _READINESS_AVAILABLE = True
except ImportError:
    _READINESS_AVAILABLE = False

MESSAGE_STYLE = cfg("telegram_message_style", "structured")

# ── PATCH 2: STOP_BUFFER constant ─────────────────────────────────────────
# Used by TIER_1 zone computation and TOMORROW'S GTT ORDERS section.
try:
    STOP_BUFFER: float = float(cfg("stop_buffer_pct", "0.03"))
except Exception:
    STOP_BUFFER: float = 0.03

# All signal types that represent actionable entry candidates
# MARKET_TOP_PICK added in v4 — confirmed 63 live records, was missing from v3
ENTRY_TYPES = {
    "BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
    "REENTRY_SETUP", "STAGED_ENTRY", "MARKET_TOP_PICK",
}

def esc(text: str) -> str:
    """
    Escape HTML special characters in AI-generated free text.
    Prevents Telegram from misinterpreting < > & as HTML tags.
    Only apply to dynamic content — NOT to your own <b> <i> tags.
    """
    if not text:
        return ""
    return html.escape(str(text), quote=False)

# ── Telegram sender ────────────────────────────────────────────────────────

_TELEGRAM_LIMIT = 4096   # Telegram's hard per-message limit

def _split_html_safe(text: str, limit: int = _TELEGRAM_LIMIT) -> list[str]:
    """
    Split a Telegram HTML message at newline boundaries so no HTML tag
    is ever sheared across a part boundary.

    Strategy:
      1. Walk backwards from `limit` chars to find the last newline.
      2. Repeat on the remainder until the whole message is consumed.

    If a single line exceeds `limit` (pathological), it's hard-split and
    may render broken — but that's a content problem, not a code problem.
    """
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)   # last newline before limit
        if cut <= 0:
            cut = limit                    # no newline found — hard cut
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def send_message(text: str) -> bool:
    import requests
    token   = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not set")
        return False

    # Split BEFORE sending — never rely on a 400 to trigger the split
    parts = _split_html_safe(text)
    if len(parts) > 1:
        results = [send_message(p) for p in parts]
        return all(results)

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 30))
                logger.warning(f"Telegram rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            logger.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            wait = [5, 15, 30][attempt]
            logger.warning(f"Telegram attempt {attempt+1} failed: {e} — retry in {wait}s")
            time.sleep(wait)
    return False


# ── Formatters ────────────────────────────────────────────────────────────

def conviction_icon(c: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get((c or "").upper(), "⚪")

def regime_icon(r: str) -> str:
    return {
        "TRENDING": "🚀", "NEUTRAL": "⚖️", "CAUTION": "⚠️",
        "RISK OFF": "🔴", "RISK ON": "🚀", "BULLISH": "📈",
    }.get((r or "").upper(), "❓")

def fii_icon(f: str) -> str:
    return {"BUYING": "🟢", "SELLING": "🔴", "NEUTRAL": "⚪"}.get((f or "").upper(), "⚪")

def action_icon(a: str) -> str:
    return {
        "TRAIL_SL": "📈", "PARTIAL_BOOK": "💰", "EXIT": "🔴",
        "HOLD": "🟡", "ADD": "🟢", "REDUCE": "🔽",
        "WATCH_CLOSELY": "👁", "BOOK_PARTIAL": "💰",
    }.get((a or "").upper().replace(" ", "_"), "⚪")

def fmt_price(v) -> str:
    if v is None: return "—"
    try:    return f"₹{float(v):,.0f}"
    except: return str(v)

def fmt_pct(v) -> str:
    if v is None: return "—"
    try:    return f"{float(v):+.1f}%"
    except: return str(v)

def fmt_chg(v, d: int = 2) -> str:
    if v is None: return ""
    try:    return f"{float(v):+.{d}f}%"
    except: return str(v)

def fmt_inr(v) -> str:
    """Compact Indian-format currency: Cr / L / ₹."""
    if v is None: return "—"
    try:
        val = float(v)
        if abs(val) >= 1e7:  return f"₹{val/1e7:.1f}Cr"
        elif abs(val) >= 1e5: return f"₹{val/1e5:.1f}L"
        else:                 return f"₹{val:,.0f}"
    except: return str(v)

def days_held(entry_date_str) -> str:
    if not entry_date_str: return ""
    try:
        from datetime import date
        d = (today_ist() - date.fromisoformat(str(entry_date_str)[:10])).days
        return f"{d}d"
    except: return ""

def sl_proximity_pct(cp, sl) -> "float | None":
    try:
        cp, sl = float(cp), float(sl)
        if sl <= 0 or cp <= 0: return None
        return (cp - sl) / cp * 100
    except: return None

def fii_footer_line(fii: dict) -> str:
    if not fii: return ""
    flag    = str(fii.get("fii_flag") or "").upper()
    net     = fii.get("fii_net")
    net_5d  = fii.get("fii_net_5d")
    net_20d = fii.get("fii_net_20d")
    ico     = fii_icon(flag)
    parts   = [f"{ico} FII: <b>{flag}</b>"]
    if net      is not None: parts.append(f"Today ₹{float(net):.0f}Cr")
    if net_5d   is not None: parts.append(f"5d ₹{float(net_5d):.0f}Cr")
    if net_20d  is not None: parts.append(f"20d ₹{float(net_20d):.0f}Cr")
    return "  ".join(parts)


# ── Position-specific helpers ──────────────────────────────────────────────

def target_progress(cp, t1, t2, t3) -> str:
    """
    Show T1/T2/T3 with hit status.
    ✅ = CMP has reached or passed target.
    (+X.Y%) = distance remaining to target.
    """
    try:    cp = float(cp or 0)
    except: return ""
    if cp <= 0: return ""
    parts = []
    for label, t in [("T1", t1), ("T2", t2), ("T3", t3)]:
        if t is None: continue
        try:    t = float(t)
        except: continue
        if t <= 0: continue
        pct = (t - cp) / cp * 100
        if pct <= 0:
            parts.append(f"{label}:{fmt_price(t)}✅")
        else:
            parts.append(f"{label}:{fmt_price(t)}({pct:+.1f}%)")
    return "  ".join(parts)

def partial_booking_summary(partial_bookings, orig_qty, curr_qty) -> str:
    """
    Summarise partial exit from:
      - original_qty vs current_qty difference (confirms partial happened)
      - partial_bookings JSONB array for price/qty/pnl detail
    """
    orig = int(orig_qty or 0)
    curr = int(curr_qty or 0)
    qty_line = ""
    if orig > 0 and curr < orig:
        pct_out  = (orig - curr) / orig * 100
        qty_line = f"{pct_out:.0f}% booked ({orig - curr}/{orig} sh)"

    if not partial_bookings:
        return qty_line

    try:
        bookings = (partial_bookings
                    if isinstance(partial_bookings, list)
                    else json.loads(partial_bookings))
        if not isinstance(bookings, list) or not bookings:
            return qty_line
        entries = []
        for b in bookings[-2:]:           # show last 2 bookings only
            if not isinstance(b, dict): continue
            price = b.get("price") or b.get("exit_price")
            pnl   = b.get("pnl_pct") or b.get("pnl")
            if price:
                s = f"@{fmt_price(price)}"
                if pnl: s += f"={fmt_pct(pnl)}"
                entries.append(s)
        detail = " ".join(entries)
        return f"{qty_line} {detail}".strip() if detail else qty_line
    except Exception:
        return qty_line


# ── Data loader ────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """
    Primary source: ai_context.__FINAL_PICKS__ (step 19 output)

    v4 FIX: Reads ranked_candidates from conviction_reason and
    portfolio_guidance/warnings/correlations from strategy_validation,
    then merges them back into a single final_picks_data dict.

    v5 CHANGE: signal_log select gains vol_ratio, delivery_pct columns
    (used by TOMORROW'S GTT ORDERS section).
    """
    # ── Trading date resolution ──
    try:
        holidays = {
            r["date"][:10]
            for r in sb.table("nse_holidays").select("date").execute().data
        }
    except Exception:
        holidays = set()
    signal_date = today
    d = today_ist()
    for _ in range(10):
        if d.weekday() < 5 and str(d) not in holidays:
            try:
                probe = (
                    sb.table("signal_log")
                    .select("date")
                    .eq("date", str(d))
                    .limit(1)
                    .execute().data
                )
                if probe:
                    signal_date = str(d)
                    if str(d) != today:
                        logger.info(
                            f"No signal_log data for {today} yet "
                            f"— using last available: {d}"
                        )
                    break
            except Exception:
                signal_date = str(d)
                break
        d -= timedelta(days=1)

    # ── __FINAL_PICKS__: merge conviction_reason + strategy_validation ──
    final_picks_data = None
    try:
        fp_rows = (
            sb.table("ai_context")
              .select("conviction_reason,strategy_validation,ai_note,"
                      "conviction,suggested_action")
              .eq("date", signal_date)
              .eq("symbol", "__FINAL_PICKS__")
              .limit(1).execute().data
        )
        if fp_rows:
            row = fp_rows[0]
            if row.get("conviction_reason"):
                final_picks_data = json.loads(row["conviction_reason"])
                if row.get("strategy_validation"):
                    try:
                        guidance_data = json.loads(row["strategy_validation"])
                        final_picks_data.update(guidance_data)
                    except Exception as e:
                        logger.warning(f"strategy_validation parse failed: {e}")
            final_picks_data = final_picks_data or {}
            final_picks_data["_sizing"]  = row.get("suggested_action")
            final_picks_data["_ai_note"] = row.get("ai_note", "")
    except Exception as e:
        logger.warning(f"__FINAL_PICKS__ load failed: {e}")

    # ── __MARKET_INTEL__ (step 18 summary) ──
    market_intel = {}
    try:
        mi_rows = (
            sb.table("ai_context")
              .select("conviction_reason,ai_note,suggested_action")
              .eq("date", signal_date)
              .eq("symbol", "__MARKET_INTEL__")
              .limit(1).execute().data
        )
        if mi_rows:
            r    = mi_rows[0]
            full = json.loads(r.get("conviction_reason") or "{}")
            market_intel = {
                "summary":     r.get("ai_note", "")[:300],
                "sizing":      r.get("suggested_action"),
                "fii_bias":    (full.get("fii_outlook") or {}).get("5session_bias"),
                "fii_sectors": (full.get("fii_outlook") or {}).get("favoured_sectors", []),
                "fii_exit":    (full.get("fii_outlook") or {}).get("exit_sectors", []),
                "echo":        (full.get("market_tone") or {}).get("echo_comparison", ""),
                "alerts":      [
                    a for a in (full.get("regulatory_alerts") or [])
                    if a.get("action") not in ("NO_ACTION", None)
                ][:5],
            }
    except Exception as e:
        logger.warning(f"__MARKET_INTEL__ load failed: {e}")

    # ── Signal log — all types for signal_date ──
    # v5: added vol_ratio, delivery_pct for TOMORROW'S GTT ORDERS reference line
    signals = (
        sb.table("signal_log")
          .select(
              "symbol,sector,signal_type,signal_subtype,strategy,"
              "score,score_adjusted,filter_reason,"
              "ai_conviction,ai_conviction_reason,ai_note,ai_suggested_action,"
              "holding_score,momentum_state,lifecycle,fii_flag,"
              "sector_rank_at_entry,days_to_trigger_est,near_miss_data,eap_action,"
              "vol_ratio,delivery_pct"
          )
          .eq("date", signal_date)
          .execute().data
    )

    # ── Open positions — full schema ──
    open_pos = (
        sb.table("open_positions")
          .select(
              "symbol,company_name,sector,strategy,"
              "entry_date,entry_price,current_price,current_value,"
              "invested_value,unrealized_pnl,pnl_pct,locked_profit,"
              "lifecycle,sl_type,initial_sl_atr,high_water_mark,active_sl,"
              "exit_signal,action_required,event_risk,upcoming_news,"
              "target_1,target_2,target_3,target_price,target_hit,"
              "trailing_sl_pct,signal_date,"
              "original_qty,current_qty,partial_bookings,status"
          )
          .eq("status", "ACTIVE")
          .execute().data
    )

    # ── Original entry thesis per position (signal_log JOIN via signal_date) ──
    entry_thesis_map: dict[str, str] = {}
    pos_symbols = [p.get("symbol") for p in open_pos if p.get("symbol")]
    signal_dates_needed = list({
        str(p.get("signal_date") or "")
        for p in open_pos if p.get("signal_date")
    })
    for sd in signal_dates_needed[:5]:
        if not sd: continue
        try:
            rows = (
                sb.table("signal_log")
                  .select("symbol,ai_conviction_reason")
                  .eq("date", sd)
                  .in_("symbol", pos_symbols)
                  .execute().data
            )
            for r in rows:
                sym = r.get("symbol")
                acr = r.get("ai_conviction_reason") or ""
                if sym and acr and sym not in entry_thesis_map:
                    entry_thesis_map[sym] = acr[:200]
        except Exception as e:
            logger.warning(f"Entry thesis lookup failed for {sd}: {e}")

    # ── Regime ──
    regime_rows = (
        sb.table("market_regime")
          .select(
              "regime,predicted_regime,regime_confidence,nifty_price,"
              "india_vix,avg_sector_breadth,nifty_1d_chg_pct,above_200dma_pct,"
              "advance_decline_ratio,nifty_pcr"
          )
          .eq("date", signal_date).execute().data
    )
    if not regime_rows:
        regime_rows = (
            sb.table("market_regime")
              .select(
                  "regime,predicted_regime,regime_confidence,nifty_price,"
                  "india_vix,avg_sector_breadth,nifty_1d_chg_pct,above_200dma_pct,"
                  "advance_decline_ratio,nifty_pcr"
              )
              .order("date", desc=True).limit(1).execute().data
        )
    regime = regime_rows[0] if regime_rows else {}

    # ── FII ──
    fii_rows = (
        sb.table("fii_dii_flow")
          .select("fii_net,fii_net_5d,fii_net_20d,fii_flag,dii_net,dii_flag")
          .order("date", desc=True).limit(1).execute().data
    )
    fii = fii_rows[0] if fii_rows else {}

    # ── Global cues ──
    cues_rows = (
        sb.table("global_cues")
          .select(
              "gift_nifty,gift_nifty_chg_pct,gap_signal,brent_crude,brent_chg_pct,"
              "gold_price,usd_inr,us_dow_chg_pct,sp500_chg_pct,us_nasdaq_chg_pct"
          )
          .eq("session", "EVENING").order("date", desc=True).limit(1).execute().data
    )
    cues = cues_rows[0] if cues_rows else {}

    # ── Lesson counts (7-day window) ──
    lesson_rows = (
        sb.table("lessons")
          .select("source")
          .gte("date", str(today_ist() - timedelta(days=7)))
          .execute().data
    )
    lessons_ai = sum(1 for r in lesson_rows if "AI:" in (r.get("source") or ""))
    lessons_rb = sum(1 for r in lesson_rows if "RULE" in (r.get("source") or "").upper())

    # ── MSL map — entry zones for new candidates ──
    all_symbols = list({s.get("symbol") for s in signals if s.get("symbol")})
    msl_map: dict[str, dict] = {}
    if all_symbols:
        msl_rows = (
            sb.table("master_shortlist")
              .select(
                  "symbol,entry_zone_low,entry_zone_high,current_price,"
                  "lifecycle,dist_entry_pct,expected_r,validity_score"
              )
              .eq("date", signal_date)
              .in_("symbol", all_symbols[:100])
              .execute().data
        )
        msl_map = {r["symbol"]: r for r in msl_rows}

    # ── Upcoming events: positions + TIER_1/2 watchlist (next 7 days) ──
    watchlist_symbols: list[str] = []
    if final_picks_data and final_picks_data.get("ranked_candidates"):
        watchlist_symbols = [
            r["symbol"] for r in final_picks_data["ranked_candidates"]
            if r.get("tier") in ("TIER_1", "TIER_2") and r.get("symbol")
        ]
    all_event_symbols = list(set(pos_symbols + watchlist_symbols))
    upcoming_events: list[dict] = []
    if all_event_symbols:
        try:
            ts = str(today_ist())
            tc = str(today_ist() + timedelta(days=7))
            upcoming_events = (
                sb.table("nifty_upcoming_events")
                  .select("symbol,purpose,details,event_date,days_to_event")
                  .in_("symbol", all_event_symbols)
                  .gte("event_date", ts)
                  .lte("event_date", tc)
                  .order("event_date")
                  .execute().data
            ) or []
        except Exception as e:
            logger.warning(f"Upcoming events load failed: {e}")

    # ── Data anomalies ──
    anomalies: list[dict] = []
    try:
        anomalies = (
            sb.table("data_anomalies")
              .select("check_name,message,severity")
              .eq("date", today)
              .eq("severity", "ERROR")
              .execute().data or []
        )
    except Exception:
        pass

    # ── Portfolio health snapshot ──
    portfolio_summary: dict = {}
    if open_pos:
        total_invested  = sum(float(p.get("invested_value")  or 0) for p in open_pos)
        total_pnl       = sum(float(p.get("unrealized_pnl")  or 0) for p in open_pos)
        total_locked    = sum(float(p.get("locked_profit")   or 0) for p in open_pos)
        pnl_pct_overall = (total_pnl / total_invested * 100) if total_invested > 0 else 0
        winners         = sum(1 for p in open_pos if float(p.get("pnl_pct") or 0) > 0)
        losers          = sum(1 for p in open_pos if float(p.get("pnl_pct") or 0) < 0)
        near_sl_count   = sum(
            1 for p in open_pos
            if (sl_proximity_pct(p.get("current_price"), p.get("active_sl")) or 99) <= 3.0
        )
        target_hit_count = sum(1 for p in open_pos if p.get("target_hit"))
        partial_done    = sum(
            1 for p in open_pos
            if int(p.get("current_qty") or 0) < int(p.get("original_qty") or 0)
        )
        portfolio_summary = {
            "total_invested":    total_invested,
            "total_pnl":         total_pnl,
            "pnl_pct_overall":   pnl_pct_overall,
            "total_locked":      total_locked,
            "winners":           winners,
            "losers":            losers,
            "near_sl_count":     near_sl_count,
            "target_hit_count":  target_hit_count,
            "partial_done":      partial_done,
        }

    return {
        "signal_date":       signal_date,
        "display_date":      today,
        "final_picks":       final_picks_data,
        "market_intel":      market_intel,
        "signals":           signals,
        "open_pos":          open_pos,
        "entry_thesis_map":  entry_thesis_map,
        "regime":            regime,
        "fii":               fii,
        "cues":              cues,
        "msl_map":           msl_map,
        "upcoming_events":   upcoming_events,
        "lessons_ai":        lessons_ai,
        "lessons_rb":        lessons_rb,
        "portfolio_summary": portfolio_summary,
        "anomalies":         anomalies,
    }


# ── Regime header ──────────────────────────────────────────────────────────

def build_regime_header(regime: dict, fii: dict, cues: dict) -> list[str]:
    r_label = regime.get("predicted_regime") or regime.get("regime", "?")
    r_conf  = float(regime.get("regime_confidence") or 0)
    r_icon  = regime_icon(r_label)
    nifty   = regime.get("nifty_price")
    vix     = regime.get("india_vix")
    breadth = regime.get("avg_sector_breadth")
    adr     = regime.get("advance_decline_ratio")
    chg_1d  = regime.get("nifty_1d_chg_pct")
    a200    = regime.get("above_200dma_pct")
    pcr     = regime.get("nifty_pcr")

    lines = [
        f"{r_icon} <b>{r_label}</b>"
        + (f" <i>({r_conf:.0%} conf)</i>" if r_conf else "")
        + (f"  Nifty: <b>{fmt_price(nifty)}</b>" if nifty else "")
        + (f" {fmt_chg(chg_1d)}" if chg_1d is not None else ""),
    ]

    details = []
    if vix:     details.append(f"VIX:{vix}")
    if breadth: details.append(f"Brdth:{breadth:.0f}%")
    if a200:    details.append(f"Ab200:{a200:.0f}%")
    if adr:     details.append(f"A/D:{adr:.1f}")
    if pcr:     details.append(f"PCR:{pcr}")
    if details: lines.append(f"  <i>{' · '.join(details)}</i>")

    if cues.get("gift_nifty"):
        g   = cues.get("gift_nifty")
        gc  = cues.get("gift_nifty_chg_pct", 0)
        gap = cues.get("gap_signal", "")
        ico = "📈" if float(gc or 0) > 0 else "📉"
        lines.append(f"  {ico} Gift Nifty: <b>{fmt_price(g)}</b> ({fmt_chg(gc)}) [{gap}]")

    global_parts = []
    if cues.get("us_dow_chg_pct"):    global_parts.append(f"DOW:{fmt_chg(cues['us_dow_chg_pct'])}")
    if cues.get("sp500_chg_pct"):     global_parts.append(f"S&P:{fmt_chg(cues['sp500_chg_pct'])}")
    if cues.get("us_nasdaq_chg_pct"): global_parts.append(f"NQ:{fmt_chg(cues['us_nasdaq_chg_pct'])}")
    if cues.get("brent_crude"):
        global_parts.append(
            f"Brent:${float(cues['brent_crude']):.0f}"
            f"({fmt_chg(cues.get('brent_chg_pct'))})"
        )
    if cues.get("usd_inr"): global_parts.append(f"₹{float(cues['usd_inr']):.1f}/$")
    if global_parts:
        lines.append(f"  🌐 {' · '.join(global_parts)}")

    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(f"  {fii_line}")

    return lines


# ── Entry zone line (fallback — near-miss + raw signal sections only) ─────
# PATCH: simplified to plain text (no <b> tags) so esc() callers are safe.
# TIER_1 and TIER_2 blocks now compute zone inline — this function is only
# used in near-miss and raw-signal fallback displays.

def zone_line(symbol: str, msl_map: dict) -> str:
    m    = msl_map.get(symbol, {})
    lo   = m.get("entry_zone_low")
    hi   = m.get("entry_zone_high")
    dist = m.get("dist_entry_pct")
    er   = m.get("expected_r")
    if not lo:
        return ""
    lo = float(lo)
    zh = float(hi) if hi else round(lo * 1.02, 0)
    dist_str = f" ({abs(float(dist)):.1f}%↓)" if dist is not None else ""
    er_str   = f" · ExpR:{float(er):.1f}×" if er else ""
    return f"₹{lo:,.0f}–₹{zh:,.0f}{dist_str}{er_str}"


# ── Open position block ────────────────────────────────────────────────────

def build_position_block(
    p: dict,
    sig_map: dict,
    entry_thesis_map: dict,
    cues: dict,
    include_gap_risk: bool = False,
    brief: bool = False,
) -> list[str]:
    """
    Full or brief lifecycle block for a single open position.

    brief=True  → morning mode: header + SL/target + gap risk + action
    brief=False → evening mode: full detail including rationale, partial bookings

    PATCH 3: entry thesis replaced with 85-char compressed rationale.
    Sources: entry_thesis_map (from original signal date) OR today's
    sig_map.ai_conviction_reason — whichever is available first.
    """
    sym    = p.get("symbol", "?")
    pnl    = float(p.get("pnl_pct")        or 0)
    cp     = float(p.get("current_price")  or 0)
    sl     = float(p.get("active_sl")      or 0)
    hwm    = float(p.get("high_water_mark") or 0)
    entry  = float(p.get("entry_price")    or 0)
    locked = float(p.get("locked_profit")  or 0)
    ico    = "📈" if pnl >= 0 else "📉"
    prox   = sl_proximity_pct(cp, sl)
    sl_w   = "  🚨<b>Near SL!</b>" if prox is not None and prox <= 3.0 else ""

    today_sig  = sig_map.get(sym, {})
    today_type = today_sig.get("signal_type", "—")
    action_req = p.get("action_required") or "HOLD"
    exit_sig   = p.get("exit_signal") or ""

    lines: list[str] = []

    # ── Header ──
    lines.append(
        f"\n  {ico} <b>{sym}</b> [{p.get('strategy', '?')}] [{p.get('lifecycle', '?')}]"
        f"  P&L: <b>{fmt_pct(pnl)}</b>  Held: {days_held(p.get('entry_date'))}{sl_w}"
    )

    # ── Price / SL / HWM ──
    price_parts = []
    if entry: price_parts.append(f"Entry:{fmt_price(entry)}")
    if cp:    price_parts.append(f"CMP:{fmt_price(cp)}")
    if sl:
        prox_str = f"({prox:+.1f}%)" if prox is not None else ""
        price_parts.append(f"SL:{fmt_price(sl)}{prox_str}")
    if hwm and hwm > cp: price_parts.append(f"HWM:{fmt_price(hwm)}")
    if locked > 0:       price_parts.append(f"🔒{fmt_inr(locked)}")
    if price_parts:
        lines.append(f"  {' · '.join(price_parts)}")

    # ── Gap risk (morning mode only) ──
    if include_gap_risk and cues.get("gift_nifty_chg_pct") and cp > 0 and sl > 0:
        gap_pct  = float(cues.get("gift_nifty_chg_pct") or 0)
        est_open = cp * (1 + gap_pct / 100)
        if est_open <= sl:
            lines.append(
                f"  🚨 <b>SL BREACH RISK AT OPEN</b> "
                f"EstOpen:{fmt_price(est_open)} vs SL:{fmt_price(sl)}"
            )
        elif sl_proximity_pct(est_open, sl) is not None and sl_proximity_pct(est_open, sl) <= 1.5:
            lines.append(
                f"  ⚠️ Gap narrows SL cushion  EstOpen:{fmt_price(est_open)}"
            )

    # ── Targets ──
    tp = target_progress(cp, p.get("target_1"), p.get("target_2"), p.get("target_3"))
    if tp:
        lines.append(f"  Targets: {tp}")

    # ── Action required ──
    a_ico = action_icon(action_req)
    action_line = f"  {a_ico} Action: <b>{action_req}</b>"
    if exit_sig:
        action_line += f"  ⚡Signal: <b>{exit_sig}</b>"
    elif today_type not in ("—", ""):
        action_line += f"  Signal: {today_type}"
    lines.append(action_line)

    if brief:
        # Morning mode ends here — no rationale, no partial detail
        event_risk = p.get("event_risk") or ""
        if event_risk:
            lines.append(f"  ⚠️ Event: {event_risk}")
        return lines

    # ── Partial bookings (evening only) ──
    pb = partial_booking_summary(
        p.get("partial_bookings"),
        p.get("original_qty"),
        p.get("current_qty"),
    )
    if pb:
        lines.append(f"  📊 Partial: {pb}")

    # ── Event risk / upcoming news (evening) ──
    event_risk    = p.get("event_risk") or ""
    upcoming_news = p.get("upcoming_news") or ""
    if event_risk:
        lines.append(f"  ⚠️ Event: {event_risk}")
    elif upcoming_news:
        lines.append(f"  📅 News: {upcoming_news[:80]}")

    # ── PATCH 3: Compressed rationale (evening only, max 85 chars) ──
    # Replaces the old 130-char static thesis block.
    # Sources: entry_thesis_map (original signal date) → today's ai_conviction_reason
    thesis_raw = (entry_thesis_map.get(sym, "")
                  or today_sig.get("ai_conviction_reason", ""))
    if thesis_raw:
        t = thesis_raw.split("| Entry:")[0].strip()
        if "] " in t:
            t = t.split("] ", 1)[-1]
        lines.append(f"   💬 {esc(t[:85])}")

    return lines


# ── Evening digest ─────────────────────────────────────────────────────────

# PATCH 4: sb=None added for entry_readiness enrichment
def build_evening(data: dict, sb=None) -> str:
    fp       = data.get("final_picks")
    mi       = data.get("market_intel", {})
    signals  = data["signals"]
    open_pos = data["open_pos"]
    regime   = data["regime"]
    fii      = data["fii"]
    cues     = data["cues"]
    msl_map  = data["msl_map"]
    ps       = data.get("portfolio_summary", {})
    eth_map  = data.get("entry_thesis_map", {})
    date_str = data["signal_date"]

    sig_map = {s.get("symbol"): s for s in signals}

    display = data.get("display_date", date_str)
    stale   = display != date_str
    lines = [
        f"<b>📊 TradeOS Evening · {display}</b>"
        + (f"  <i>(data: {date_str})</i>" if stale else ""),
        ""
    ]
    lines += build_regime_header(regime, fii, cues)

    # ── Anomalies ──
    if data.get("anomalies"):
        lines += ["", "🚨 <b>DATA ALERTS</b>"]
        for a in data["anomalies"]:
            lines.append(f"  ⚠️ {a.get('check_name','?')}: {a.get('message','')}")
    lines.append("")

    # ── PATCH 6: Section 0 — MACRO + FII FLOW ─────────────────────────────
    # FII sector-level flow and regulatory alerts — the "what's happening globally"
    # layer. Complements build_regime_header (which shows totals + global indices).
    # NOT duplicated: regime header shows net FII flow; this section shows
    # which sectors FII is accumulating/distributing and what events to watch.
    if mi.get("fii_bias") or mi.get("alerts"):
        lines.append("═══ <b>🌍 MACRO + FII FLOW</b> ═══")
        if mi.get("fii_bias"):
            _fii_line = f"  FII 5-sess: <b>{mi['fii_bias']}</b>"
            if mi.get("fii_sectors"):
                _fii_line += f"  ▲ Buying: {', '.join(mi['fii_sectors'])}"
            if mi.get("fii_exit"):
                _fii_line += f"  ▼ Selling: {', '.join(mi['fii_exit'])}"
            lines.append(_fii_line)
        for _alert in (mi.get("alerts") or []):
            _urgency = str(_alert.get("urgency_level") or _alert.get("urgency") or "INFO").upper()
            _ico     = {"HIGH": "🚨", "MEDIUM": "⚠️", "IMMEDIATE": "🚨"}.get(_urgency, "📌")
            _sym_tag = (
                f" [{', '.join(str(s) for s in _alert['affected_symbols'][:3])}]"
                if _alert.get("affected_symbols") else ""
            )
            lines.append(
                f"  {_ico} [{_urgency}] {esc(_alert.get('action', ''))} — "
                f"{esc(_alert.get('news_item', ''))}{_sym_tag}"
            )
        lines.append("")

    # ── Section 1: Market Intelligence ────────────────────────────────────
    # Scope: India market summary narrative + historical echo pattern.
    # FII bias and alerts now live in Section 0 above.
    if mi.get("summary"):
        lines.append("═══ <b>📡 MARKET INTELLIGENCE</b> ═══")
        lines.append(f"  {mi['summary']}")
        # PATCH 5: echo restored at 300 chars with 📖 icon and esc()
        if mi.get("echo"):
            lines.append(f"  📖 <i>{esc(mi['echo'])}</i>")
        lines.append("")

    # ── Section 2: Portfolio Health Snapshot ──
    if ps and open_pos:
        pnl_ico = "📈" if ps.get("pnl_pct_overall", 0) >= 0 else "📉"
        lines.append("═══ <b>💼 PORTFOLIO HEALTH</b> ═══")
        lines.append(
            f"  {len(open_pos)} positions  "
            f"Invested: <b>{fmt_inr(ps['total_invested'])}</b>  "
            f"{pnl_ico} P&L: <b>{fmt_inr(ps['total_pnl'])}</b> "
            f"({fmt_pct(ps['pnl_pct_overall'])})"
            + (f"  🔒 Locked: {fmt_inr(ps['total_locked'])}" if ps.get("total_locked", 0) > 0 else "")
        )
        status_parts = [f"✅ {ps['winners']} win  ❌ {ps['losers']} loss"]
        if ps.get("near_sl_count", 0) > 0:
            status_parts.append(f"🚨 {ps['near_sl_count']} near SL")
        if ps.get("target_hit_count", 0) > 0:
            status_parts.append(f"🎯 {ps['target_hit_count']} target hit")
        if ps.get("partial_done", 0) > 0:
            status_parts.append(f"📊 {ps['partial_done']} partially exited")
        lines.append(f"  {'  ·  '.join(status_parts)}")
        lines.append("")

    # ── Section 3: Open Positions (full lifecycle) ──
    if open_pos:
        lines.append(f"═══ <b>📂 OPEN POSITIONS ({len(open_pos)})</b> ═══")
        for p in open_pos:
            lines += build_position_block(
                p, sig_map, eth_map, cues,
                include_gap_risk=False, brief=False,
            )
        lines.append("")

    # ── Section 4: EXIT signals ──
    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append(f"🔴 <b>EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            reason = s.get("filter_reason") or s.get("ai_conviction_reason") or ""
            lines.append(
                f"  <b>{s['symbol']}</b>"
                + (f"  <i>— {esc(reason)}</i>" if reason else "")
            )
        lines.append("")

    # ── AVOID_ENTRY_EVENT warnings ──
    avoid_events = [s for s in signals if s.get("signal_type") == "AVOID_ENTRY_EVENT"]
    if avoid_events:
        lines.append("⚠️ <b>AVOID ENTRY — EVENT RISK</b>")
        for s in avoid_events:
            lines.append(
                f"  🚫 <b>{s['symbol']}</b> [{s.get('sector','?')}]"
                + (f" — {esc(s.get('filter_reason',''))}" if s.get("filter_reason") else "")
            )
        lines.append("")

    # ── Sections 5–9: Tier structure from step 19 ──
    if fp and fp.get("ranked_candidates"):
        ranked      = fp["ranked_candidates"]
        tier1       = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2       = [r for r in ranked if r.get("tier") == "TIER_2"]
        tier3       = [r for r in ranked if r.get("tier") == "TIER_3"]
        guidance    = fp.get("portfolio_guidance", {})
        warnings    = fp.get("sector_exposure_warnings", [])
        corr_groups = fp.get("correlation_groups", [])

        # ── PATCH 7: Single guidance line with priority cascade ──
        sizing     = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        _gtext     = (guidance.get("capital_deployment_narrative")
                      or guidance.get("new_positions_guidance")
                      or fp.get("_ai_note") or "")
        _sec_parts = []
        if guidance.get("sectors_to_overweight"):
            _sec_parts.append(f"▲ {'/'.join(guidance['sectors_to_overweight'])}")
        if guidance.get("sectors_to_underweight"):
            _sec_parts.append(f"▼ {'/'.join(guidance['sectors_to_underweight'])}")
        lines.append(
            f"💼 <b>SIZING: {sizing}</b>"
            + (f"  {'  '.join(_sec_parts)}" if _sec_parts else "")
        )
        if _gtext:
            lines.append(f"  <i>{esc(_gtext[:150])}</i>")
        lines.append("")

        # ── PATCH 8: TIER_1 — 3-line compact + readiness score ──────────
        if tier1:
            # Optional readiness enrichment (adds score, icon, breakdown,
            # readiness_label per pick). use_live=False = DB data only.
            if _READINESS_AVAILABLE and sb:
                try:
                    tier1 = compute_entry_readiness(
                        tier1, msl_map, sb=sb, use_live=False
                    )
                except Exception as e:
                    logger.warning(f"entry_readiness enrichment failed: {e}")

            lines.append(f"⭐ <b>TIER 1 — ACT NOW ({len(tier1)})</b>")
            for c in tier1:
                sym   = c.get("symbol", "?")
                conv  = (c.get("conviction") or "").upper()
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                er    = float(msl.get("expected_r")      or 2.0)
                sl_p  = round(zl * (1 - STOP_BUFFER), 0) if zl else None
                t1_p  = round(zl * (1 + STOP_BUFFER * er), 0) if zl else None
                rr    = (round((t1_p - zl) / (zl - sl_p), 1)
                         if t1_p and sl_p and zl and sl_p < zl else None)
                r_icon    = c.get("readiness_icon") or conviction_icon(conv)
                r_score   = c.get("readiness_score")
                r_label   = c.get("readiness_label", "")
                dist      = msl.get("dist_entry_pct")
                dist_str  = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""
                score_str = f"[{r_score}/100·{r_label}]" if r_score else ""

                # Line 1: symbol · zone · targets · SL · R:R · readiness
                lines.append(
                    f"\n  {r_icon} <b>{sym}</b> [{conv}]"
                    f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}"
                    f"  → T1₹{t1_p:,.0f} | SL₹{sl_p:,.0f} · RR {rr}×  {score_str}"
                )

                # Line 2: entry condition · invalidation · readiness breakdown
                parts2 = []
                if c.get("entry_note"):
                    parts2.append(esc(c["entry_note"]))
                if c.get("invalidation"):
                    parts2.append(f"❌ {esc(c['invalidation'])}")
                breakdown = c.get("readiness_breakdown", "")
                if breakdown:
                    parts2.append(f"<i>{breakdown}</i>")
                if parts2:
                    lines.append(f"   {' · '.join(parts2)}")

                # Line 3: WHY — thesis + catalyst (evening learning layer)
                thesis   = c.get("thesis") or c.get("ai_conviction_reason") or ""
                catalyst = c.get("catalyst") or ""
                if "] " in thesis:
                    thesis = thesis.split("] ", 1)[-1]
                thesis = thesis.split("| Entry:")[0].strip()
                if catalyst and len(thesis) < 70:
                    thesis += f" · 💡 {esc(catalyst)}"
                if thesis:
                    lines.append(f"   💬 {esc(thesis)}")

            lines.append("")

        # ── PATCH 9: TIER_2 — 2-line compact ─────────────────────────────
        if tier2:
            lines.append(f"🔭 <b>TIER 2 — WATCH FOR TRIGGER ({len(tier2)})</b>")
            for c in tier2:
                sym   = c.get("symbol", "?")
                conv  = (c.get("conviction") or "").upper()
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                dist  = msl.get("dist_entry_pct")
                dist_str = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""

                # Line 1: symbol · zone · dist
                lines.append(
                    f"\n  {conviction_icon(conv)} <b>{sym}</b> [{conv}]"
                    f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}"
                )

                # Line 2: entry condition · invalidation
                parts2 = []
                if c.get("entry_note"):
                    parts2.append(f"📍 {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    parts2.append(f"❌ {esc(c['invalidation'])}")
                if parts2:
                    lines.append(f"   {' · '.join(parts2)}")

            lines.append("")

        # TIER_3: Monitor (unchanged)
        if tier3:
            lines.append(f"👁 <b>TIER 3 — MONITOR ({len(tier3)})</b>")
            t3_str = " · ".join(
                f"{c['symbol']}({conviction_icon(c.get('conviction'))})"
                for c in tier3
            )
            lines.append(f"  {t3_str}")
            lines.append("")

        # Near-miss upgrades (WATCH signals flagged by step 18)
        near_miss = [
            s for s in signals
            if s.get("signal_type") == "WATCH"
            and "NEAR_MISS_UPGRADE" in (s.get("ai_note") or "")
        ]
        if near_miss:
            lines.append(f"💡 <b>NEAR MISS — AI FLAGGED ({len(near_miss)})</b>")
            lines.append("  <i>Didn't technically qualify — AI sees macro/news tailwind</i>")
            for s in near_miss:
                note_raw = (s.get("ai_note") or "").replace("[NEAR_MISS_UPGRADE:", "").split("]")
                flag   = note_raw[0] if note_raw else "?"
                reason = note_raw[1].strip() if len(note_raw) > 1 else ""
                ez     = zone_line(s["symbol"], msl_map)
                lines.append(f"\n  💡 <b>{s['symbol']}</b> [{flag}]  {s.get('sector', '?')}")
                if reason: lines.append(f"  {esc(reason)}")
                if ez:     lines.append(f"  {esc(ez)}")
            lines.append("")

        # Sector exposure warnings — full detail (learning layer, never compress)
        if warnings:
            lines.append("⚠️ <b>SECTOR CONCENTRATION</b>")
            for w in warnings:
                lines.append(
                    f"  {esc(w.get('sector', '?'))}: "
                    f"{w.get('candidate_count', '?')} candidates "
                    f"(holding {w.get('already_held', 0)}) — "
                    f"{esc(w.get('recommendation', ''))}"
                    f"  → Allow: <b>{w.get('allow_count', 1)}</b>"
                )
            lines.append("")

        # Correlation groups — full detail (portfolio construction learning)
        if corr_groups:
            lines.append("⚡ <b>CORRELATION GROUPS</b>")
            for g in corr_groups:
                lines.append(
                    f"  [{g.get('group_label', '?')}]"
                    f" {g.get('symbols', [])} — "
                    f"{esc(g.get('recommendation', ''))}"
                )
            lines.append("")

    else:
        # ── Fallback: step 19 didn't run — raw signal_log ──
        lines.append("<i>⚠️ Step 19 AI picks unavailable — showing raw signals</i>")
        buys = sorted(
            [s for s in signals if s.get("signal_type") in ENTRY_TYPES],
            key=lambda x: float(x.get("score_adjusted") or x.get("score") or 0),
            reverse=True,
        )
        if buys:
            lines.append(f"\n🎯 <b>RAW SIGNALS ({len(buys)})</b>")
            for s in buys:
                c_ico = conviction_icon(s.get("ai_conviction"))
                lines.append(
                    f"\n  {c_ico} <b>{s['symbol']}</b> [{s.get('strategy', '?')}] "
                    f"Score:<b>{float(s.get('score_adjusted') or s.get('score') or 0):.0f}</b>"
                )
                if s.get("ai_conviction_reason"):
                    lines.append(f"  💬 {s['ai_conviction_reason']}")
                ez = zone_line(s["symbol"], msl_map)
                if ez: lines.append(f"  {ez}")
        lines.append("")

    # ── Section 10: Upcoming events (7 days) ──
    upcoming = data.get("upcoming_events", [])
    if upcoming:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>UPCOMING EVENTS — 7 DAYS</b>")
        for e in upcoming[:8]:
            sym     = e.get("symbol", "?")
            tag     = "📂" if sym in pos_sym_set else "🔭"
            d_to    = e.get("days_to_event")
            dt      = str(e.get("event_date", ""))
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")
            d_str   = f"({d_to}d)" if d_to is not None else f"({dt})"
            lines.append(
                f"  {tag} <b>{sym}</b>: {purpose} {d_str}"
                + (f" — {detail}" if detail else "")
            )
        lines.append("")

    # ── PATCH 10: TOMORROW'S GTT ORDERS ──────────────────────────────────
    # Placed at the bottom so it's easy to find before placing orders at 9 AM.
    # vol_ratio + delivery_pct sourced from signal_log (added to select in v5).
    if fp and fp.get("ranked_candidates"):
        _t1_orders = [r for r in fp["ranked_candidates"] if r.get("tier") == "TIER_1"]
        if _t1_orders:
            lines.append("─" * 28)
            lines.append("📋 <b>TOMORROW'S GTT ORDERS</b>  <i>Place before 9:15 AM</i>")
            for _c in _t1_orders:
                _sym  = _c.get("symbol", "?")
                _msl  = msl_map.get(_sym, {})
                _zl   = float(_msl.get("entry_zone_low")  or 0)
                _er   = float(_msl.get("expected_r")      or 2.0)
                _sl   = round(_zl * (1 - STOP_BUFFER), 0) if _zl else None
                _t1   = round(_zl * (1 + STOP_BUFFER * _er), 0) if _zl else None
                _rr   = (round((_t1 - _zl) / (_zl - _sl), 1)
                         if _t1 and _sl and _zl and _sl < _zl else None)
                _sig  = sig_map.get(_sym, {})
                _vref = float(_sig.get("vol_ratio")    or 0)
                _dref = float(_sig.get("delivery_pct") or 0)
                lines.append(
                    f"  <b>{_sym}</b>  "
                    f"Entry₹{_zl:,.0f} | SL₹{_sl:,.0f} | T1₹{_t1:,.0f} | RR {_rr}×"
                )
                if _vref or _dref:
                    lines.append(
                        f"   <i>Ref: Vol {_vref:.1f}× · Del {_dref:.0f}%"
                        f" · Confirm Vol>1× at 9:45AM + price≥VWAP</i>"
                    )
            lines.append("")

    # ── Footer ──
    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(fii_line)

    ranked_total = len(fp.get("ranked_candidates", [])) if fp else 0
    lines.append(
        f"<i>━━━ {ranked_total} ranked · {len(exits)} exit · "
        f"{len(open_pos)} open  "
        f"| Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule ━━━</i>"
    )
    return "\n".join(lines)


# ── Morning brief ──────────────────────────────────────────────────────────

# PATCH 11: sb=None added for entry_readiness enrichment
def build_morning(data: dict, sb=None) -> str:
    """
    Morning brief: execution-focused, gap-risk-aware.
    Answers: What do I action at market open? What are my position risks?

    PATCH 11-14: sb param, guidance compression, TIER_1 live zone status,
    TIER_2 compact 2-line format.
    """
    fp       = data.get("final_picks")
    regime   = data["regime"]
    fii      = data["fii"]
    cues     = data["cues"]
    open_pos = data["open_pos"]
    signals  = data["signals"]
    msl_map  = data["msl_map"]
    eth_map  = data.get("entry_thesis_map", {})
    ps       = data.get("portfolio_summary", {})
    date_str = data["signal_date"]
    today_str = str(today_ist())

    sig_map = {s.get("symbol"): s for s in signals}

    display = data.get("display_date", date_str)
    stale   = display != date_str
    lines = [
        f"<b>🌅 TradeOS Morning · {display}</b>"
        + (f"  <i>(data: {date_str})</i>" if stale else ""),
        ""
    ]
    lines += build_regime_header(regime, fii, cues)
    lines.append("")

    # ── Today's events at the top — act before market opens ──
    upcoming = data.get("upcoming_events", [])
    today_events = [e for e in upcoming if str(e.get("event_date", "")) == today_str]
    if today_events:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>TODAY'S EVENTS — ACTION REQUIRED</b>")
        for e in today_events:
            tag     = "📂" if e.get("symbol") in pos_sym_set else "🔭"
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")
            lines.append(
                f"  {tag} <b>{e.get('symbol','?')}</b>: {purpose}"
                + (f" — {detail}" if detail else "")
            )
        lines.append("")

    # ── Gap risk summary (pre-market SL breach alert) ──
    gap_pct = float(cues.get("gift_nifty_chg_pct") or 0)
    if gap_pct != 0 and open_pos:
        breach_risk = []
        watch_risk  = []
        for p in open_pos:
            cp = float(p.get("current_price") or 0)
            sl = float(p.get("active_sl") or 0)
            if cp <= 0 or sl <= 0: continue
            est_open = cp * (1 + gap_pct / 100)
            prox_at_open = sl_proximity_pct(est_open, sl)
            if est_open <= sl:
                breach_risk.append(p.get("symbol", "?"))
            elif prox_at_open is not None and prox_at_open <= 2.0:
                watch_risk.append(p.get("symbol", "?"))
        if breach_risk:
            lines.append(
                f"🚨 <b>SL BREACH RISK AT OPEN: {', '.join(breach_risk)}</b>"
            )
            lines.append(
                f"  Estimated gap: {fmt_chg(gap_pct)}  "
                f"— Review these before placing orders!"
            )
            lines.append("")
        elif watch_risk:
            lines.append(
                f"⚠️ Gap narrows SL cushion: {', '.join(watch_risk)}  ({fmt_chg(gap_pct)} gap)"
            )
            lines.append("")

    # ── Open position pulse ──
    if open_pos:
        lines.append(f"═══ <b>📂 POSITION PULSE ({len(open_pos)})</b> ═══")
        if ps:
            lines.append(
                f"  Overall: <b>{fmt_pct(ps.get('pnl_pct_overall'))}</b>  "
                f"✅{ps.get('winners', 0)}  ❌{ps.get('losers', 0)}"
                + (f"  🚨{ps.get('near_sl_count', 0)} near SL" if ps.get("near_sl_count", 0) > 0 else "")
                + (f"  🎯{ps.get('target_hit_count', 0)} target hit" if ps.get("target_hit_count", 0) > 0 else "")
            )
        for p in open_pos:
            lines += build_position_block(
                p, sig_map, eth_map, cues,
                include_gap_risk=True, brief=True,
            )
        lines.append("")

    # ── EXIT today ──
    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append("🔴 <b>EXIT TODAY</b>")
        for s in exits:
            reason = s.get("filter_reason") or s.get("ai_conviction_reason") or ""
            lines.append(
                f"  <b>{s['symbol']}</b>"
                + (f" — {esc(reason)}" if reason else "")
            )
        lines.append("")

    # ── TIER_1 + TIER_2 watchlist ──
    if fp and fp.get("ranked_candidates"):
        ranked   = fp["ranked_candidates"]
        tier1    = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2    = [r for r in ranked if r.get("tier") == "TIER_2"]
        guidance = fp.get("portfolio_guidance", {})
        sizing   = guidance.get("position_sizing_override") or fp.get("_sizing", "?")

        # ── PATCH 12: Single morning guidance line ──
        _mgtext = (guidance.get("capital_deployment_narrative")
                   or guidance.get("new_positions_guidance")
                   or fp.get("_ai_note") or "")
        lines.append(
            f"💼 <b>TODAY'S APPROACH: {sizing}</b>"
            + (f"  ▲ {'/'.join(guidance['sectors_to_overweight'][:2])}"
               if guidance.get("sectors_to_overweight") else "")
        )
        if _mgtext:
            lines.append(f"  <i>{esc(_mgtext)}</i>")
        lines.append("")

        # ── PATCH 13: TIER_1 — live zone status + readiness ─────────────
        if tier1:
            # use_live=True → yfinance live price for zone status + volume check
            if _READINESS_AVAILABLE and sb:
                try:
                    tier1 = compute_entry_readiness(
                        tier1, msl_map, sb=sb, use_live=True
                    )
                except Exception as e:
                    logger.warning(f"morning readiness enrichment failed: {e}")

            lines.append(f"⭐ <b>WATCHLIST — TIER_1 ({len(tier1)} picks)</b>")
            for c in tier1:
                sym   = c.get("symbol", "?")
                conv  = (c.get("conviction") or "").upper()
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                er    = float(msl.get("expected_r")      or 2.0)
                sl_p  = round(zl * (1 - STOP_BUFFER), 0) if zl else None
                t1_p  = round(zl * (1 + STOP_BUFFER * er), 0) if zl else None
                rr    = (round((t1_p - zl) / (zl - sl_p), 1)
                         if t1_p and sl_p and zl and sl_p < zl else None)
                r_icon    = c.get("readiness_icon") or conviction_icon(conv)
                r_score   = c.get("readiness_score")
                score_str = f"[{r_score}/100]" if r_score else ""
                timing    = c.get("timing_note", "Verify zone manually")

                # Zone status: prefer live readiness output; fallback to dist_entry_pct
                z_status = c.get("zone_status")
                if not z_status:
                    dist = msl.get("dist_entry_pct")
                    if dist is not None:
                        d = float(dist)
                        if abs(d) <= 2:
                            z_status = f"📍 In Zone ({d:+.1f}%)"
                        elif d < 0:
                            z_status = f"⬇️ Approaching ({d:+.1f}%)"
                        else:
                            z_status = f"⬆️ Above Zone (+{d:.1f}%)"
                    else:
                        z_status = "📍 Verify zone"

                # Line 1: symbol · live zone status · readiness score
                lines.append(
                    f"\n  {r_icon} <b>{sym}</b> [{conv}]  {z_status}  {score_str}"
                )

                # Line 2: exact GTT levels · timing window
                lines.append(
                    f"   Entry₹{zl:,.0f}–₹{zh:,.0f}"
                    f" | SL₹{sl_p:,.0f} | T1₹{t1_p:,.0f} | RR {rr}×"
                    f" · <i>{timing}</i>"
                )

                # Line 3: entry condition / invalidation
                if c.get("entry_note"):
                    lines.append(f"   📍 {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    lines.append(f"   ❌ {esc(c['invalidation'])}")

            lines.append("")

        # ── PATCH 14: TIER_2 — 2-line compact ────────────────────────────
        if tier2:
            lines.append(f"🔭 <b>INTRADAY TRIGGERS — TIER_2 ({len(tier2)})</b>")
            for c in tier2:
                sym   = c.get("symbol", "?")
                conv  = (c.get("conviction") or "").upper()
                msl   = msl_map.get(sym, {})
                zl    = float(msl.get("entry_zone_low")  or 0)
                zh    = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
                dist  = msl.get("dist_entry_pct")
                dist_str = f"({abs(float(dist)):.1f}%↓)" if dist is not None else ""

                # Line 1: symbol · zone · dist
                lines.append(
                    f"  {conviction_icon(conv)} <b>{sym}</b> [{conv}]"
                    f"  ₹{zl:,.0f}–₹{zh:,.0f} {dist_str}"
                )

                # Line 2: entry condition only (no rationale in morning)
                if c.get("entry_note"):
                    lines.append(f"    📍 {esc(c['entry_note'])}")

            lines.append("")

    # ── Events in next 3 days (non-today) ──
    near_events = [
        e for e in upcoming
        if str(e.get("event_date", ""))[:10] != today_str
        and (e.get("days_to_event") or 99) <= 3
    ]
    if near_events:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>EVENTS IN NEXT 3 DAYS</b>")
        for e in near_events[:6]:
            tag     = "📂" if e.get("symbol") in pos_sym_set else "🔭"
            d_to    = e.get("days_to_event", "?")
            purpose = e.get("purpose", "")
            lines.append(
                f"  {tag} <b>{e.get('symbol', '?')}</b>: {purpose} ({d_to}d)"
            )

    return "\n".join(lines)

# ── Afternoon zone check ───────────────────────────────────────────────────

def build_afternoon(data: dict, sb=None) -> str:
    """
    Afternoon zone check: 1:00 PM IST
    Answers: Of last night's TIER_1 picks, which are at zone RIGHT NOW?
    Uses live yfinance prices via compute_entry_readiness(use_live=True).
    No position pulse, no global cues, no GTT section — zone status only.
    """
    from datetime import datetime as _dt
    fp      = data.get("final_picks")
    msl_map = data["msl_map"]
    now_str = _dt.now().strftime("%I:%M %p")
    date_str = data["signal_date"]

    lines = [
        f"<b>📊 AFTERNOON CONVICTION CHECK — {date_str}  |  TradeOS v6  |  {now_str}</b>",
        "═" * 35,
        "",
    ]

    if not fp or not fp.get("ranked_candidates"):
        lines.append("<i>⚠️ No ranked candidates — step 19 data missing</i>")
        return "\n".join(lines)

    tier1 = [r for r in fp["ranked_candidates"] if r.get("tier") == "TIER_1"]

    if not tier1:
        lines.append("<i>No TIER_1 picks for today</i>")
        return "\n".join(lines)

    # Enrich with live yfinance prices — _pre_market=False at 1PM so
    # timing_note automatically shows "1:30–2:30 PM · Limit at zone_low"
    if _READINESS_AVAILABLE and sb:
        try:
            tier1 = compute_entry_readiness(
                tier1, msl_map, sb=sb, use_live=True
            )
        except Exception as e:
            logger.warning(f"afternoon readiness enrichment failed: {e}")

    any_actionable = False

    for c in tier1:
        sym         = c.get("symbol", "?")
        conv        = (c.get("conviction") or "").upper()
        msl         = msl_map.get(sym, {})
        zl          = float(msl.get("entry_zone_low")  or 0)
        zh          = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))
        zone_status = c.get("zone_status", "")
        timing_note = c.get("timing_note", "")
        r_score     = c.get("readiness_score")
        score_str   = f"[{r_score}/100]" if r_score else ""

        if "IN ZONE" in zone_status:
            status_ico = "✅"
            any_actionable = True
        elif "APPROACHING" in zone_status:
            status_ico = "⬇️"
        elif "ABOVE ZONE" in zone_status:
            status_ico = "⚠️"
        else:
            status_ico = "❌"

        # Line 1: status · symbol · zone range · live price · score
        lines.append(
            f"{status_ico} <b>{sym}</b>  ₹{zl:,.0f}–₹{zh:,.0f}  —  {zone_status}  {score_str}"
        )

        # Line 2: timing note
        if timing_note:
            lines.append(f"   <i>{esc(timing_note)}</i>")

        # Line 3: vol + delivery breakdown — the decision layer
        breakdown = c.get("readiness_breakdown", "")
        if breakdown:
            lines.append(f"   {breakdown}")

        # Line 4: entry note if in zone, invalidation if missed
        if c.get("entry_note") and "IN ZONE" in zone_status:
            lines.append(f"   📍 {esc(c['entry_note'])}")
        if c.get("invalidation") and "MISSED" in zone_status:
            lines.append(f"   ❌ {esc(c['invalidation'][:60])}")

        lines.append("")

    if not any_actionable:
        lines.append(
            "<i>No TIER_1 picks in zone right now — monitor for 1:30–2:30 PM window</i>"
        )

    return "\n".join(lines)

# ── Compact (mobile one-screen) ────────────────────────────────────────────

def build_compact(data: dict) -> str:
    fp       = data.get("final_picks")
    regime   = data["regime"]
    fii      = data["fii"]
    open_pos = data["open_pos"]
    signals  = data["signals"]
    date_str = data["signal_date"]
    ps       = data.get("portfolio_summary", {})
    cues     = data["cues"]

    r_label  = regime.get("predicted_regime") or regime.get("regime", "?")
    nifty    = regime.get("nifty_price")
    fii_flag = fii.get("fii_flag", "?")
    gift_chg = cues.get("gift_nifty_chg_pct")

    lines = [
        f"<b>📊 {date_str}</b>  {regime_icon(r_label)}{r_label}  "
        f"Nifty:{fmt_price(nifty)}  {fii_icon(fii_flag)}FII:{fii_flag}"
        + (f"  Gift:{fmt_chg(gift_chg)}" if gift_chg else ""),
        "",
    ]

    if ps:
        lines.append(
            f"💼 P&L: <b>{fmt_pct(ps.get('pnl_pct_overall'))}</b>  "
            f"✅{ps.get('winners', 0)} ❌{ps.get('losers', 0)}"
            + (f"  🚨{ps.get('near_sl_count', 0)} SL" if ps.get("near_sl_count", 0) > 0 else "")
            + (f"  🎯{ps.get('target_hit_count', 0)}" if ps.get("target_hit_count", 0) > 0 else "")
        )

    if fp and fp.get("ranked_candidates"):
        ranked  = fp["ranked_candidates"]
        tier1   = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2   = [r for r in ranked if r.get("tier") == "TIER_2"]
        guidance = fp.get("portfolio_guidance", {})
        sizing  = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        lines.append(f"💼 {sizing}")
        if tier1:
            lines.append("\n⭐ <b>ACT NOW</b>")
            for c in tier1:
                lines.append(
                    f"  {conviction_icon(c.get('conviction'))} <b>{c['symbol']}</b> "
                    f"[{c.get('action', '?')}] {float(c.get('suggested_allocation_pct', 0)):.0f}%"
                )
        if tier2:
            t2str = " · ".join(c["symbol"] for c in tier2[:5])
            lines.append(f"\n🔭 <b>WATCH:</b> {t2str}")
    else:
        buys = sorted(
            [s for s in signals if s.get("signal_type") in ENTRY_TYPES],
            key=lambda x: float(x.get("score_adjusted") or x.get("score") or 0),
            reverse=True,
        )
        if buys:
            lines.append(f"🎯 <b>BUYS ({len(buys)})</b>")
            for s in buys[:5]:
                lines.append(
                    f"  {conviction_icon(s.get('ai_conviction'))} "
                    f"<b>{s['symbol']}</b> "
                    f"{float(s.get('score_adjusted') or s.get('score') or 0):.0f}pt"
                )

    if open_pos:
        lines.append(f"\n📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl  = float(p.get("pnl_pct") or 0)
            cp   = float(p.get("current_price") or 0)
            sl   = float(p.get("active_sl") or 0)
            ico  = "📈" if pnl >= 0 else "📉"
            prox = sl_proximity_pct(cp, sl)
            sl_w = " 🚨" if prox is not None and prox <= 3.0 else ""
            t_hit = " 🎯" if p.get("target_hit") else ""
            act   = p.get("action_required") or ""
            act_str = f"  [{act}]" if act and act != "HOLD" else ""
            lines.append(
                f"  {ico}{sl_w}{t_hit} <b>{p['symbol']}</b> "
                f"{fmt_pct(pnl)} {days_held(p.get('entry_date'))}{act_str}"
            )

    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append("\n🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))

    return "\n".join(lines)


def send_signal_with_keyboard(signal: dict) -> bool:
    """
    Sends an individual signal alert with inline confirmation keyboard.
    Called for each ENTRY-type signal after the main digest.
    Requires brain/position_manager to be present.
    """
    try:
        from brain.position_manager import build_signal_keyboard, _tg_api
    except ImportError:
        logger.debug("position_manager not available — skipping keyboard send")
        return False

    try:
        sym   = signal.get("symbol", "?")
        stype = signal.get("signal_type", "")
        price = float(signal.get("current_price") or signal.get("kite_price") or 0)
        score = float(signal.get("score_adjusted") or signal.get("score") or 0)

        text = (
            f"<b>{stype}: {sym}</b>\n"
            f"Price: ₹{price:.2f}  Score: {score:.0f}\n"
            f"Tap below to confirm entry or skip."
        )
        keyboard = build_signal_keyboard(
            signal_id=signal["id"],
            symbol=sym,
            signal_price=price,
        )
        _tg_api("sendMessage", {
            "chat_id":      TELEGRAM_CHAT_ID,
            "text":         text,
            "parse_mode":   "HTML",
            "reply_markup": json.dumps(keyboard),
        })
        return True
    except Exception as e:
        logger.warning(f"send_signal_with_keyboard failed for {signal.get('symbol')}: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    logger.info(f"send_alerts v5 | argv={sys.argv}")
    if is_kill_switch_active():
        logger.warning("Kill switch — send_alerts skipped")
        return {"status": "skipped"}

    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info("Telegram alerts disabled")
        return {}

    if "--position-risk" in sys.argv:
        return {"status": "skipped", "reason": "covered by sl_monitor"}

    is_morning   = "--morning"   in sys.argv
    is_afternoon = "--afternoon" in sys.argv
    sb    = get_supabase()
    today = str(today_ist())
    data  = load_data(sb, today)

    has_fp = bool(data.get("final_picks"))
    has_guidance = bool(
        (data.get("final_picks") or {}).get("portfolio_guidance")
    )
    logger.info(
        f"Data loaded: step19={'✅' if has_fp else '❌ fallback'} "
        f"| guidance={'✅' if has_guidance else '❌ missing'} "
        f"| {len(data['signals'])} signals | {len(data['open_pos'])} positions"
        f"| readiness={'✅' if _READINESS_AVAILABLE else '❌ unavailable'}"
    )

    # PATCH 15: pass sb to both builders for entry_readiness enrichment
    if is_morning:
        msg  = build_morning(data, sb=sb)
        mode = "morning"
    elif is_afternoon:
        msg  = build_afternoon(data, sb=sb)
        mode = "afternoon"
    elif MESSAGE_STYLE == "compact":
        msg  = build_compact(data)
        mode = "compact"
    else:
        msg  = build_evening(data, sb=sb)
        mode = "structured"

    success = send_message(msg)

    # Send per-signal keyboard messages for actionable entry signals
    if cfg_bool("telegram_signal_keyboards_enabled", False):
        entry_signals = [s for s in data["signals"] if s.get("signal_type") in ENTRY_TYPES
                         and s.get("id")]
        for sig in entry_signals:
            send_signal_with_keyboard(sig)

    fp     = data.get("final_picks") or {}
    ranked = fp.get("ranked_candidates") or []
    tier1  = len([r for r in ranked if r.get("tier") == "TIER_1"])
    tier2  = len([r for r in ranked if r.get("tier") == "TIER_2"])
    exits  = len([s for s in data["signals"] if s.get("signal_type") == "EXIT"])

    if success:
        logger.success(
            f"Telegram ({mode}) sent: T1:{tier1} T2:{tier2} exit:{exits} "
            f"pos:{len(data['open_pos'])} "
            f"| step19:{'ok' if has_fp else 'fallback'} "
            f"| guidance:{'ok' if has_guidance else 'missing'}"
        )

    # ── Write structured daily summary ──
    try:
        ranked   = (data.get("final_picks") or {}).get("ranked_candidates") or []
        _tier1   = len([r for r in ranked if r.get("tier") == "TIER_1"])
        _tier2   = len([r for r in ranked if r.get("tier") == "TIER_2"])
        _tier3   = len([r for r in ranked if r.get("tier") == "TIER_3"])

        _buy_raw  = sum(1 for s in data["signals"] if s.get("signal_type") in ENTRY_TYPES)
        _watch    = sum(1 for s in data["signals"] if s.get("signal_type") == "WATCH")
        _exit_raw = sum(1 for s in data["signals"] if s.get("signal_type") == "EXIT")

        _zero_reason = None
        if _buy_raw == 0:
            _zero_reason = "NO_RAW_SIGNALS"
        elif not has_fp:
            _zero_reason = "STEP19_MISSING"
        elif _tier1 == 0 and _tier2 == 0:
            _zero_reason = "AI_PASSED_ALL_TO_TIER3"

        sb.table("signal_daily_summary").upsert({
            "date":           today,
            "buy_raw":        _buy_raw,
            "watch_raw":      _watch,
            "exit_raw":       _exit_raw,
            "tier1":          _tier1,
            "tier2":          _tier2,
            "tier3":          _tier3,
            "open_positions": len(data["open_pos"]),
            "step19_ok":      has_fp,
            "guidance_ok":    has_guidance,
            "zero_reason":    _zero_reason,
            "sizing":         (data.get("final_picks") or {}).get("_sizing"),
            "ai_note":        ((data.get("final_picks") or {}).get("_ai_note") or ""),
        }).execute()
        logger.info(f"signal_daily_summary written: T1:{_tier1} T2:{_tier2} raw:{_buy_raw}")
    except Exception as e:
        logger.warning(f"signal_daily_summary write failed (non-fatal): {e}")

    return {
        "sent":     success,
        "mode":     mode,
        "tier1":    tier1,
        "tier2":    tier2,
        "step19":   has_fp,
        "guidance": has_guidance,
    }


if __name__ == "__main__":
    main()
