"""
TradeOS v6 — Send Alerts v4
============================
Pipeline position: final step — after step 19 (ai_decision_engine).

WHAT CHANGED FROM v3 → v4:

FIX 1 — portfolio_guidance truncation (primary bug)
  Step 19 now writes ranked_candidates → conviction_reason,
  and portfolio_guidance/warnings/correlations → strategy_validation.
  load_data() merges both columns back into final_picks_data.
  Result: portfolio_guidance, sector_exposure_warnings, correlation_groups
  are now always present in the alert output.

FIX 2 — MARKET_TOP_PICK added to ENTRY_TYPES fallback
  Signal type MARKET_TOP_PICK has 63 live records and was silently excluded
  from the v3 fallback display.

FIX 3 — ADD signal removed
  signal_log has no ADD signal type in production. Replaced with
  action_required from open_positions (which does have ADD/TRAIL_SL/etc).

FIX 4 — TIER_2 shown in morning brief
  Was silently dropped in v3. Now shown as "INTRADAY TRIGGERS" section.

FIX 5 — _ai_note (portfolio narrative) shown in morning brief
  The step 19 ai_note column holds the capital deployment narrative.
  Now surfaced in both morning and evening.

NEW — Portfolio Health Snapshot (evening)
  Section before open positions showing: total invested, unrealized P&L,
  locked profit, winner/loser ratio, near-SL count, target hits, partial exits.
  Computed from open_positions columns: invested_value, unrealized_pnl,
  locked_profit, pnl_pct, active_sl, current_price, target_hit,
  original_qty, current_qty.

NEW — Full position lifecycle display (evening + morning)
  Each open position now shows:
  - Entry price vs CMP vs SL (% away) vs HWM
  - T1/T2/T3 target progress (✅ if CMP ≥ target, else % remaining)
  - Locked profit (₹)
  - Partial booking history from partial_bookings JSONB + qty comparison
  - action_required + exit_signal from open_positions
  - Original entry thesis from signal_log (signal_date JOIN)
  - lifecycle stage

NEW — Gap risk check in morning brief
  Estimates open price = CMP × (1 + gift_nifty_chg_pct/100).
  Flags SL breach risk or dangerously-close positions before market open.

NEW — nifty_upcoming_events with details column
  Events now show purpose + details (e.g. "Quarterly Results — Q4FY26")
  and are sourced for both open positions and TIER_1/TIER_2 watchlist stocks.

NEW — Today's event calendar in morning brief
  Events happening today are shown at top of morning brief.

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
    Header: regime + FII + macro snapshot
    Section 1: Market Intelligence (step 18 summary + FII bias + alerts)
    Section 2: Portfolio Health Snapshot (new)
    Section 3: Open Positions (full lifecycle — entry/SL/HWM/targets/thesis)
    Section 4: EXIT signals
    Section 5: TIER_1 — Act Now
    Section 6: TIER_2 — Watch for Trigger
    Section 7: TIER_3 — Monitor
    Section 8: Near Miss (WATCH signals AI flagged)
    Section 9: Sector Warnings + Correlation Groups
    Section 10: Upcoming Events (7 days, positions + watchlist)
    Footer: portfolio guidance + lesson count

  MORNING:
    Header: regime + Gift Nifty gap
    Gap Risk Alert: SL breach risk positions
    Position Pulse: brief position status + gap risk per position
    EXIT Today
    TIER_1 Watchlist (entry levels, invalidation)
    TIER_2 Intraday Triggers (new — was missing in v3)
    Today's Events + Next 3 days events

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

MESSAGE_STYLE = cfg("telegram_message_style", "structured")

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

def send_message(text: str) -> bool:
    import requests
    token   = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not set")
        return False

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
            if resp.status_code == 400 and len(text) > 4000:
                mid = len(text) // 2
                send_message(text[:mid])
                return send_message(text[mid:])
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

    This fixes the silent truncation bug where portfolio_guidance was
    never stored because conviction_reason[:8000] ran out of space
    before reaching it.
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
            # Same fix as step 19 — verify signal_log actually has data
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
                signal_date = str(d)   # probe failed, trust the date
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
                # Primary payload: {"ranked_candidates": [...]}
                final_picks_data = json.loads(row["conviction_reason"])
                # Guidance payload: {portfolio_guidance, warnings, correlations}
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
                ][:3],
            }
    except Exception as e:
        logger.warning(f"__MARKET_INTEL__ load failed: {e}")

    # ── Signal log — all types for signal_date ──
    signals = (
        sb.table("signal_log")
          .select(
              "symbol,sector,signal_type,signal_subtype,strategy,"
              "score,score_adjusted,filter_reason,"
              "ai_conviction,ai_conviction_reason,ai_note,ai_suggested_action,"
              "holding_score,momentum_state,lifecycle,fii_flag,"
              "sector_rank_at_entry,days_to_trigger_est,near_miss_data,eap_action"
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
    # Used in evening brief to show "is original thesis still valid?"
    entry_thesis_map: dict[str, str] = {}
    pos_symbols = [p.get("symbol") for p in open_pos if p.get("symbol")]
    signal_dates_needed = list({
        str(p.get("signal_date") or "")
        for p in open_pos if p.get("signal_date")
    })
    for sd in signal_dates_needed[:5]:      # cap to 5 lookups
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
    # Derived from open_positions schema — no new tables needed
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


# ── Entry zone line (new candidates) ──────────────────────────────────────

def zone_line(symbol: str, msl_map: dict) -> str:
    m = msl_map.get(symbol, {})
    lo   = m.get("entry_zone_low")
    hi   = m.get("entry_zone_high")
    cp   = m.get("current_price")
    lc   = m.get("lifecycle", "")
    dist = m.get("dist_entry_pct")
    er   = m.get("expected_r")
    if not lo:
        return ""
    z = fmt_price(lo)
    if hi: z += f"–{fmt_price(hi)}"
    parts = [f"Zone: <b>{z}</b>"]
    if cp:   parts.append(f"CMP:{fmt_price(cp)}")
    if dist is not None: parts.append(f"Dist:{float(dist):+.1f}%")
    if er:   parts.append(f"ExpR:{float(er):.1f}x")
    if lc:   parts.append(f"[{lc}]")
    return "  ".join(parts)


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
    brief=False → evening mode: full detail including thesis, partial bookings
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
        # Morning mode ends here — no thesis, no partial detail
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

    # ── Original entry thesis (evening) ──
    thesis = entry_thesis_map.get(sym, "")
    if thesis:
        # Strip the tier prefix added by step 19
        t = thesis.split("| Entry:")[0].strip()
        if "] " in t:
            t = t.split("] ", 1)[-1]
        lines.append(f"  💬 <i>{t[:130]}</i>")

    return lines


# ── Evening digest ─────────────────────────────────────────────────────────

def build_evening(data: dict) -> str:
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
        for a in data["anomalies"][:3]:
            lines.append(f"  ⚠️ {a.get('check_name','?')}: {a.get('message','')[:80]}")
    lines.append("")

    # ── Section 1: Market Intelligence ──
    if mi.get("summary"):
        lines.append("═══ <b>📡 MARKET INTELLIGENCE</b> ═══")
        lines.append(f"  {mi['summary']}")
        if mi.get("echo"):
            lines.append(f"  <i>Echo: {mi['echo'][:200]}</i>")
        if mi.get("fii_bias"):
            ico = fii_icon(mi["fii_bias"])
            lines.append(
                f"  {ico} FII 5-session: <b>{mi['fii_bias']}</b>"
                + (f"  Buying: {', '.join(mi.get('fii_sectors', [])[:3])}" if mi.get("fii_sectors") else "")
                + (f"  Selling: {', '.join(mi.get('fii_exit', [])[:2])}" if mi.get("fii_exit") else "")
            )
        for alert in (mi.get("alerts") or []):
            urgency = alert.get("urgency", "LOW")
            ico     = "🚨" if urgency == "IMMEDIATE" else "⚠️"
            lines.append(
                f"  {ico} [{urgency}] {alert.get('action', '')} — "
                f" {esc(alert.get('news_item', '')[:100])}"
                + (f" | {alert.get('affected_symbols', [])}" if alert.get("affected_symbols") else "")
            )
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
                + (f"  <i>— {esc(reason[:100])}</i>" if reason else "")
            )
        lines.append("")

    # ── AVOID_ENTRY_EVENT warnings (signal_type confirmed in prod) ──
    avoid_events = [s for s in signals if s.get("signal_type") == "AVOID_ENTRY_EVENT"]
    if avoid_events:
        lines.append("⚠️ <b>AVOID ENTRY — EVENT RISK</b>")
        for s in avoid_events:
            lines.append(
                f"  🚫 <b>{s['symbol']}</b> [{s.get('sector','?')}]"
                + (f" — {esc(s.get('filter_reason','')[:80])}" if s.get("filter_reason") else "")
            )
        lines.append("")

    # ── Sections 5–8: Tier structure from step 19 ──
    if fp and fp.get("ranked_candidates"):
        ranked      = fp["ranked_candidates"]
        tier1       = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2       = [r for r in ranked if r.get("tier") == "TIER_2"]
        tier3       = [r for r in ranked if r.get("tier") == "TIER_3"]
        guidance    = fp.get("portfolio_guidance", {})     # now always populated (v4 fix)
        warnings    = fp.get("sector_exposure_warnings", [])
        corr_groups = fp.get("correlation_groups", [])

        # Portfolio sizing guidance — includes capital deployment narrative
        sizing = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        lines.append(f"💼 <b>SIZING: {sizing}</b>")
        if guidance.get("new_positions_guidance"):
            lines.append(f"  <i>{esc(guidance.get('new_positions_guidance'))[:120]}</i>")
        if guidance.get("capital_deployment_narrative"):
            lines.append(f"  <i>{esc(guidance.get('capital_deployment_narrative'))[:200]}</i>")
        if fp.get("_ai_note"):
            lines.append(f"  <i>{esc(fp.get('_ai_note'))[:200]}</i>")
        if guidance.get("sectors_to_overweight"):
            lines.append(f"  ▲ Overweight: {', '.join(guidance['sectors_to_overweight'][:4])}")
        if guidance.get("sectors_to_underweight"):
            lines.append(f"  ▼ Underweight: {', '.join(guidance['sectors_to_underweight'][:4])}")
        lines.append("")

        # TIER_1: Act Now
        if tier1:
            lines.append(f"⭐ <b>TIER 1 — ACT NOW ({len(tier1)})</b>")
            for c in tier1:
                c_ico  = conviction_icon(c.get("conviction"))
                conf   = float(c.get("confidence") or 0)
                alloc  = float(c.get("suggested_allocation_pct") or 0)
                action = c.get("action", "?")
                corr   = c.get("correlation_group") or ""
                lines.append(
                    f"\n  {c_ico} <b>{c['symbol']}</b>  [{action}]"
                    + (f"  <b>{alloc:.0f}%</b>" if alloc else "")
                    + (f"  conf:{conf:.0%}" if conf else "")
                    + (f"  📎{corr}" if corr else "")
                )
                if c.get("thesis"):
                    lines.append(f"  💬 {esc(c['thesis'])}")
                if c.get("entry_note"):
                    lines.append(f"  📍 Entry: {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    lines.append(f"  ❌ Invalid if: {esc(c['invalidation'])}")
                if c.get("catalyst"):
                    lines.append(f"  💡 {esc(c['catalyst'][:100])}")
                if c.get("risks"):
                    risk_str = " · ".join(esc(str(r)) for r in (c["risks"] or [])[:2])
                    if risk_str:
                        lines.append(f"  ⚠️ {risk_str}")
                if c.get("lessons_applied"):
                    ls = " · ".join(esc(l) for l in (c["lessons_applied"] or [])[:2])
                    if ls:
                        lines.append(f"  📚 <i>{ls[:120]}</i>")
            lines.append("")

        # TIER_2: Watch for trigger
        if tier2:
            lines.append(f"🔭 <b>TIER 2 — WATCH FOR TRIGGER ({len(tier2)})</b>")
            for c in tier2:
                c_ico = conviction_icon(c.get("conviction"))
                lines.append(f"\n  {c_ico} <b>{c['symbol']}</b>  [{c.get('action', '?')}]")
                if c.get("thesis"):
                    lines.append(f"  💬 {esc(c['thesis'][:150])}")
                if c.get("entry_note"):
                    lines.append(f"  📍 Trigger: {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    lines.append(f"  ❌ Invalid if: {esc(c['invalidation'])}")
                ez = zone_line(c["symbol"], msl_map)
                if ez: lines.append(f"  {ez}")
            lines.append("")

        # TIER_3: Monitor
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
                if reason: lines.append(f"  {esc(reason[:160])}")
                if ez:     lines.append(f"  {esc(ez)}")
            lines.append("")

        # Sector exposure warnings (now always loaded — v4 fix)
        if warnings:
            lines.append("⚠️ <b>SECTOR CONCENTRATION WARNINGS</b>")
            for w in warnings:
                lines.append(
                    f"  {esc(w.get('sector', '?'))}: {esc(w.get('candidate_count', '?'))} candidates "
                    f"(hold {w.get('already_held', 0)}) — {esc(w.get('recommendation', '')[:100])}"
                    f"  → Allow: <b>{esc(w.get('allow_count', 1))}</b>"
                )
            lines.append("")

        # Correlation groups (now always loaded — v4 fix)
        if corr_groups:
            lines.append("⚡ <b>CORRELATION GROUPS</b>")
            for g in corr_groups:
                lines.append(
                    f"  [{g.get('group_label', '?')}] {g.get('symbols', [])} "
                    f"— {esc(g.get('recommendation', '')[:100])}"
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
                    lines.append(f"  💬 {s['ai_conviction_reason'][:150]}")
                ez = zone_line(s["symbol"], msl_map)
                if ez: lines.append(f"  {ez}")
        lines.append("")

    # ── Section 9: Upcoming events (7 days, positions + watchlist) ──
    upcoming = data.get("upcoming_events", [])
    if upcoming:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>UPCOMING EVENTS — 7 DAYS</b>")
        for e in upcoming[:8]:
            sym     = e.get("symbol", "?")
            tag     = "📂" if sym in pos_sym_set else "🔭"
            d_to    = e.get("days_to_event")
            dt      = str(e.get("event_date", ""))[:10]
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")[:60]
            d_str   = f"({d_to}d)" if d_to is not None else f"({dt})"
            lines.append(
                f"  {tag} <b>{sym}</b>: {purpose} {d_str}"
                + (f" — {detail}" if detail else "")
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

def build_morning(data: dict) -> str:
    """
    Morning brief: execution-focused, gap-risk-aware.
    Answers: What do I action at market open? What are my position risks?
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
    today_events = [e for e in upcoming if str(e.get("event_date", ""))[:10] == today_str]
    if today_events:
        pos_sym_set = {p.get("symbol") for p in open_pos}
        lines.append("📅 <b>TODAY'S EVENTS — ACTION REQUIRED</b>")
        for e in today_events:
            tag     = "📂" if e.get("symbol") in pos_sym_set else "🔭"
            purpose = e.get("purpose", "")
            detail  = (e.get("details") or "")[:60]
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
                + (f" — {esc(reason[:80])}" if reason else "")
            )
        lines.append("")

    # ── TIER_1 + TIER_2 watchlist ──
    if fp and fp.get("ranked_candidates"):
        ranked   = fp["ranked_candidates"]
        tier1    = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2    = [r for r in ranked if r.get("tier") == "TIER_2"]
        guidance = fp.get("portfolio_guidance", {})
        sizing   = guidance.get("position_sizing_override") or fp.get("_sizing", "?")

        lines.append(f"💼 <b>TODAY'S APPROACH: {sizing}</b>")
        if guidance.get("new_positions_guidance"):
            lines.append(f"  <i>{esc(guidance['new_positions_guidance'][:150])}</i>")
        # Capital deployment narrative — now always present (v4 fix)
        if guidance.get("capital_deployment_narrative"):
            lines.append(f"  <i>{esc(guidance['capital_deployment_narrative'][:150])}</i>")
        elif fp.get("_ai_note"):
            lines.append(f"  <i>{esc(fp['_ai_note'][:200])}</i>")
        if guidance.get("sectors_to_overweight"):
            lines.append(f"  ▲ {', '.join(guidance['sectors_to_overweight'][:4])}")
        lines.append("")

        # TIER_1: act now
        if tier1:
            lines.append(f"⭐ <b>WATCHLIST — TIER_1 ({len(tier1)} picks)</b>")
            for c in tier1:
                c_ico  = conviction_icon(c.get("conviction"))
                action = c.get("action", "?")
                alloc  = float(c.get("suggested_allocation_pct") or 0)
                conf   = float(c.get("confidence") or 0)
                lines.append(
                    f"  {c_ico} <b>{c['symbol']}</b>  [{action}]"
                    + (f"  <b>{alloc:.0f}%</b>" if alloc else "")
                    + (f"  conf:{conf:.0%}" if conf else "")
                )
                if c.get("entry_note"):
                    lines.append(f"    📍 {esc(c['entry_note'])}")
                if c.get("invalidation"):
                    lines.append(f"    ❌ Skip if: {esc(c['invalidation'])}")
                ez = zone_line(c["symbol"], msl_map)
                if ez: lines.append(f"    {esc(ez)}")
            lines.append("")

        # TIER_2: intraday triggers — was missing in v3, now shown in morning
        if tier2:
            lines.append(f"🔭 <b>INTRADAY TRIGGERS — TIER_2 ({len(tier2)})</b>")
            for c in tier2:
                c_ico = conviction_icon(c.get("conviction"))
                lines.append(f"  {c_ico} <b>{c['symbol']}</b>  [{c.get('action', '?')}]")
                if c.get("entry_note"):
                    lines.append(f"    📍 {esc(c['entry_note'])}")
                ez = zone_line(c["symbol"], msl_map)
                if ez: lines.append(f"    {esc(ez)}")
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


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    logger.info(f"send_alerts v4 | argv={sys.argv}")
    if is_kill_switch_active():
        logger.warning("Kill switch — send_alerts skipped")
        return {"status": "skipped"}

    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info("Telegram alerts disabled")
        return {}

    if "--position-risk" in sys.argv:
        return {"status": "skipped", "reason": "covered by sl_monitor"}

    is_morning = "--morning" in sys.argv
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
    )

    if is_morning:
        msg  = build_morning(data)
        mode = "morning"
    elif MESSAGE_STYLE == "compact":
        msg  = build_compact(data)
        mode = "compact"
    else:
        msg  = build_evening(data)
        mode = "structured"

    success = send_message(msg)

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