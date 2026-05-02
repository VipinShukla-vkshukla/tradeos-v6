"""
TradeOS v6 — Send Alerts v3
============================
Pipeline position: final step — after step 19 (ai_decision_engine).

WHAT CHANGED FROM v2 → v3:
  - Primary data source is now ai_context.__FINAL_PICKS__ (step 19 output)
    Previously: read signal_log directly and applied AI map separately.
    Now: __FINAL_PICKS__ has TIER_1/2/3, allocation %, thesis, correlation
    warnings, sector exposure warnings, portfolio guidance — all pre-computed.

  - TIER system replaces signal_type sorting
    TIER_1 = act now | TIER_2 = watch for trigger | TIER_3 = monitor
    Signal quality is now the AI's tier decision, not a human-defined sort.

  - WATCH signals that AI upgraded are now shown
    Step 19 evaluates WATCH signals and flags upgrade candidates.
    These appear as a separate "💡 NEAR MISS" section.

  - Sector exposure warnings shown
    If step 19 says "4 PSU bank candidates — cap to 1", you see that alert.

  - Correlation groups shown
    "RRKABEL + POLYCAB move together — take best 1" shown explicitly.

  - Full thesis shown — no truncation
    The send_message() already handles splitting for Telegram's 4096 char limit.

  - Portfolio guidance from step 19 shown
    Sizing guidance, deployment narrative, sector overweight/underweight.

  - Graceful fallback to signal_log if __FINAL_PICKS__ not available
    (e.g. step 19 failed or AI unavailable) — v2 behaviour preserved.

  - Morning brief shows today's TIER_1 pipeline preview based on yesterday's
    final picks + what changed overnight (Gift Nifty, global cues).

MESSAGE STRUCTURE (evening digest):
  Header: regime + FII + macro snapshot
  Section 1: 📊 Market Intelligence (step 18 summary + echo comparison)
  Section 2: ⭐ TIER_1 — Act Now  (full detail: thesis, entry, invalidation, allocation)
  Section 3: 🔭 TIER_2 — Watch  (trigger condition, entry note)
  Section 4: 💡 Near Miss  (WATCH signals AI flagged as upgrade candidates)
  Section 5: ⚠️ Sector Warnings + Correlation Groups
  Section 6: 📂 Open Positions  (P&L, SL proximity, action, lifecycle)
  Section 7: 🔴 EXIT / ➕ ADD signals
  Footer: Portfolio guidance + lesson count
"""

import sys
import json
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import (
    get_supabase, today_ist, is_kill_switch_active,
    cfg, cfg_bool, DRY_RUN,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)

MESSAGE_STYLE = cfg("telegram_message_style", "structured")


# ── Telegram sender ────────────────────────────────────────────────────────

def send_message(text: str) -> bool:
    import requests
    token   = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        logger.warning("Telegram credentials not set")
        return False

    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}

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

def tier_icon(t: str) -> str:
    return {"TIER_1": "⭐", "TIER_2": "🔭", "TIER_3": "👁"}.get((t or "").upper(), "•")

def regime_icon(r: str) -> str:
    return {"TRENDING": "🚀", "NEUTRAL": "⚖️", "CAUTION": "⚠️",
            "RISK OFF": "🔴", "RISK ON": "🚀", "BULLISH": "📈"}.get((r or "").upper(), "❓")

def fii_icon(f: str) -> str:
    return {"BUYING": "🟢", "SELLING": "🔴", "NEUTRAL": "⚪"}.get((f or "").upper(), "⚪")

def fmt_price(v) -> str:
    if v is None: return "—"
    try: return f"₹{float(v):,.0f}"
    except: return str(v)

def fmt_pct(v) -> str:
    if v is None: return "—"
    try: return f"{float(v):+.1f}%"
    except: return str(v)

def fmt_chg(v, d=2) -> str:
    if v is None: return ""
    try: return f"{float(v):+.{d}f}%"
    except: return str(v)

def days_held(entry_date_str) -> str:
    if not entry_date_str: return ""
    try:
        from datetime import date
        d = (today_ist() - date.fromisoformat(str(entry_date_str)[:10])).days
        return f"{d}d"
    except: return ""

def sl_proximity_pct(cp, sl) -> float | None:
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


# ── Data loader ────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """
    Primary source: ai_context.__FINAL_PICKS__ (step 19 output)
    Secondary source: ai_context.__MARKET_INTEL__ (step 18 output)
    Supplementary: signal_log, open_positions, market_regime, fii_dii_flow, global_cues
    Fallback: if __FINAL_PICKS__ not found, use signal_log directly (v2 behaviour)
    """
    # ── Determine last trading day ──
    try:
        holidays = {r["date"][:10] for r in sb.table("nse_holidays").select("date").execute().data}
    except Exception:
        holidays = set()
    signal_date = today
    d = today_ist()
    for _ in range(5):
        if d.weekday() < 5 and str(d) not in holidays:
            signal_date = str(d)
            break
        d -= timedelta(days=1)

    # ── Primary: __FINAL_PICKS__ ──
    final_picks_data = None
    try:
        fp_rows = (
            sb.table("ai_context")
              .select("conviction_reason,ai_note,conviction,suggested_action")
              .eq("date", signal_date)
              .eq("symbol", "__FINAL_PICKS__")
              .limit(1).execute().data
        )
        if fp_rows and fp_rows[0].get("conviction_reason"):
            final_picks_data = json.loads(fp_rows[0]["conviction_reason"])
            final_picks_data["_sizing"]   = fp_rows[0].get("suggested_action")
            final_picks_data["_ai_note"]  = fp_rows[0].get("ai_note", "")
    except Exception as e:
        logger.warning(f"__FINAL_PICKS__ load failed: {e}")

    # ── Secondary: __MARKET_INTEL__ ──
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
            r = mi_rows[0]
            full = json.loads(r.get("conviction_reason") or "{}")
            market_intel = {
                "summary":       r.get("ai_note", "")[:300],
                "sizing":        r.get("suggested_action"),
                "fii_bias":      (full.get("fii_outlook") or {}).get("5session_bias"),
                "fii_sectors":   (full.get("fii_outlook") or {}).get("favoured_sectors", []),
                "fii_exit":      (full.get("fii_outlook") or {}).get("exit_sectors", []),
                "echo":          (full.get("market_tone") or {}).get("echo_comparison", ""),
                "alerts":        [a for a in (full.get("regulatory_alerts") or [])
                                  if a.get("action") not in ("NO_ACTION", None)][:3],
            }
    except Exception as e:
        logger.warning(f"__MARKET_INTEL__ load failed: {e}")

    # ── Signal log (for EXIT/ADD/HOLD signals + fallback) ──
    signals = (
        sb.table("signal_log")
          .select("*")
          .eq("date", signal_date)
          .execute().data
    )

    # ── Open positions ──
    open_pos = (
        sb.table("open_positions")
          .select("*")
          .eq("status", "ACTIVE")
          .execute().data
    )

    # ── Regime ──
    regime_rows = (
        sb.table("market_regime")
          .select("regime,predicted_regime,regime_confidence,nifty_price,"
                  "india_vix,avg_sector_breadth,nifty_1d_chg_pct,above_200dma_pct,"
                  "advance_decline_ratio,nifty_pcr")
          .eq("date", signal_date).execute().data
    )
    if not regime_rows:
        regime_rows = (
            sb.table("market_regime")
              .select("regime,predicted_regime,regime_confidence,nifty_price,"
                      "india_vix,avg_sector_breadth,nifty_1d_chg_pct,above_200dma_pct,"
                      "advance_decline_ratio,nifty_pcr")
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
          .select("gift_nifty,gift_nifty_chg_pct,gap_signal,brent_crude,brent_chg_pct,"
                  "gold_price,usd_inr,us_dow_chg_pct,sp500_chg_pct,us_nasdaq_chg_pct")
          .eq("session", "EVENING").order("date", desc=True).limit(1).execute().data
    )
    cues = cues_rows[0] if cues_rows else {}

    # ── Lesson counts ──
    lesson_rows = (
        sb.table("lessons")
          .select("source")
          .gte("date", str(today_ist() - timedelta(days=7)))
          .execute().data
    )
    lessons_ai = sum(1 for r in lesson_rows if "AI:" in (r.get("source") or ""))
    lessons_rb = sum(1 for r in lesson_rows if "RULE" in (r.get("source") or "").upper())

    # ── MSL map for entry zones ──
    all_symbols = list({s.get("symbol") for s in signals if s.get("symbol")})
    msl_map = {}
    if all_symbols:
        msl_rows = (
            sb.table("master_shortlist")
              .select("symbol,entry_zone_low,entry_zone_high,current_price,lifecycle,dist_entry_pct")
              .eq("date", signal_date)
              .in_("symbol", all_symbols[:100])
              .execute().data
        )
        msl_map = {r["symbol"]: r for r in msl_rows}

    # Position event risk (next 5 days)
    pos_symbols = [p.get("symbol") for p in (open_pos or []) if p.get("symbol")]
    pos_events  = []
    if pos_symbols:
        try:
            _ts = str(today_ist()); _tc = str(today_ist() + timedelta(days=5))
            pev = sb.table("nifty_upcoming_events").select("symbol,purpose,event_date") \
                    .in_("symbol", pos_symbols).gte("event_date", _ts).lte("event_date", _tc).execute().data or []
            for e in pev:
                pos_events.append({"symbol": e["symbol"], "event_type": e.get("purpose",""), "event_date": e["event_date"]})
        except Exception: pass

    # Data anomalies
    anomalies = []
    try:
        anomalies = sb.table("data_anomalies").select("check_name,message,severity") \
                      .eq("date", today).eq("severity", "ERROR").execute().data or []
    except Exception: pass
    
    return {
        "signal_date":       signal_date,
        "final_picks":       final_picks_data,   # None if step 19 didn't run
        "market_intel":      market_intel,
        "signals":           signals,
        "open_pos":          open_pos,
        "regime":            regime,
        "fii":               fii,
        "cues":              cues,
        "msl_map":           msl_map,
        "lessons_ai":        lessons_ai,
        "lessons_rb":        lessons_rb,
        "pos_events":        pos_events, 
        "anomalies":         anomalies,
    }


# ── Regime header (shared) ─────────────────────────────────────────────────

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
        f"{r_icon} <b>{r_label}</b>" +
        (f" <i>({r_conf:.0%} conf)</i>" if r_conf else "") +
        (f"  Nifty: <b>{fmt_price(nifty)}</b>" if nifty else "") +
        (f" {fmt_chg(chg_1d)}" if chg_1d is not None else ""),
    ]

    details = []
    if vix:     details.append(f"VIX:{vix}")
    if breadth: details.append(f"Brdth:{breadth:.0f}%")
    if a200:    details.append(f"Ab200:{a200:.0f}%")
    if adr:     details.append(f"A/D:{adr:.1f}")
    if pcr:     details.append(f"PCR:{pcr}")
    if details: lines.append(f"  <i>{' · '.join(details)}</i>")

    if cues.get("gift_nifty"):
        g = cues.get("gift_nifty")
        gc = cues.get("gift_nifty_chg_pct", 0)
        gap = cues.get("gap_signal", "")
        ico = "📈" if float(gc or 0) > 0 else "📉"
        lines.append(f"  {ico} Gift Nifty: <b>{fmt_price(g)}</b> ({fmt_chg(gc)}) [{gap}]")

    global_parts = []
    if cues.get("us_dow_chg_pct"):     global_parts.append(f"DOW:{fmt_chg(cues['us_dow_chg_pct'])}")
    if cues.get("sp500_chg_pct"):      global_parts.append(f"S&P:{fmt_chg(cues['sp500_chg_pct'])}")
    if cues.get("us_nasdaq_chg_pct"):  global_parts.append(f"NQ:{fmt_chg(cues['us_nasdaq_chg_pct'])}")
    if cues.get("brent_crude"):        global_parts.append(f"Brent:${float(cues['brent_crude']):.0f}({fmt_chg(cues.get('brent_chg_pct'))})")
    if cues.get("usd_inr"):            global_parts.append(f"₹{float(cues['usd_inr']):.1f}/$")
    if global_parts:
        lines.append(f"  🌐 {' · '.join(global_parts)}")

    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(f"  {fii_line}")

    return lines


# ── Entry zone helper ──────────────────────────────────────────────────────

def zone_line(symbol: str, msl_map: dict) -> str:
    m = msl_map.get(symbol, {})
    lo, hi, cp = m.get("entry_zone_low"), m.get("entry_zone_high"), m.get("current_price")
    lc, dist = m.get("lifecycle", ""), m.get("dist_entry_pct")
    if not lo:
        return ""
    z  = f"{fmt_price(lo)}"
    if hi: z += f"–{fmt_price(hi)}"
    cp_str   = f"  CMP:{fmt_price(cp)}" if cp else ""
    dist_str = f"  Dist:{float(dist):+.1f}%" if dist is not None else ""
    lc_str   = f"  [{lc}]" if lc else ""
    return f"Zone: <b>{z}</b>{cp_str}{dist_str}{lc_str}"


# ── Main message builder ───────────────────────────────────────────────────

def build_evening(data: dict) -> str:
    """
    Full evening digest. Primary source: __FINAL_PICKS__ from step 19.
    Falls back gracefully to signal_log if step 19 didn't run.
    """
    fp        = data.get("final_picks")       # None if step 19 didn't run
    mi        = data.get("market_intel", {})
    signals   = data["signals"]
    open_pos  = data["open_pos"]
    regime    = data["regime"]
    fii       = data["fii"]
    cues      = data["cues"]
    msl_map   = data["msl_map"]
    date_str  = data["signal_date"]

    lines = [f"<b>📊 TradeOS · {date_str}</b>", ""]
    lines += build_regime_header(regime, fii, cues)
    if data.get("anomalies"):
        lines.append("")
        lines.append("🚨 <b>DATA ALERTS</b>")
        for a in data["anomalies"][:3]:
            lines.append(f"  ⚠️ {a.get('check_name','?')}: {a.get('message','')[:80]}")
    lines.append("")


    
    # ── Section 1: Market Intelligence summary ──
    if mi.get("summary"):
        lines.append("═══ <b>📡 MARKET INTELLIGENCE</b> ═══")
        lines.append(f"  {mi['summary']}")
        if mi.get("echo"):
            lines.append(f"  <i>Echo: {mi['echo'][:200]}</i>")

        if mi.get("fii_bias"):
            icon = fii_icon(mi["fii_bias"])
            lines.append(
                f"  {icon} FII 5-session: <b>{mi['fii_bias']}</b>"
                + (f"  Buying: {', '.join(mi.get('fii_sectors',[])[:3])}" if mi.get("fii_sectors") else "")
                + (f"  Selling: {', '.join(mi.get('fii_exit',[])[:2])}" if mi.get("fii_exit") else "")
            )

        if mi.get("alerts"):
            for alert in mi["alerts"]:
                urgency = alert.get("urgency", "LOW")
                ico     = "🚨" if urgency == "IMMEDIATE" else "⚠️"
                lines.append(
                    f"  {ico} [{urgency}] {alert.get('action','')} — "
                    f"{alert.get('news_item','')[:100]}"
                    + (f" | {alert.get('affected_symbols',[])} " if alert.get("affected_symbols") else "")
                )
        lines.append("")

    # ── If step 19 ran: use TIER structure from __FINAL_PICKS__ ──
    if fp and fp.get("ranked_candidates"):
        ranked     = fp["ranked_candidates"]
        tier1      = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2      = [r for r in ranked if r.get("tier") == "TIER_2"]
        tier3      = [r for r in ranked if r.get("tier") == "TIER_3"]
        guidance   = fp.get("portfolio_guidance", {})
        warnings   = fp.get("sector_exposure_warnings", [])
        corr_groups = fp.get("correlation_groups", [])

        # ── Portfolio sizing guidance ──
        sizing = guidance.get("position_sizing_override") or fp.get("_sizing", "?")
        lines.append(
            f"💼 <b>SIZING: {sizing}</b>"
            + (f"  <i>{guidance.get('new_positions_guidance','')[:100]}</i>" if guidance.get("new_positions_guidance") else "")
        )
        if guidance.get("capital_deployment_narrative"):
            lines.append(f"  <i>{guidance['capital_deployment_narrative'][:200]}</i>")
        if guidance.get("sectors_to_overweight"):
            lines.append(f"  ▲ Overweight: {', '.join(guidance['sectors_to_overweight'][:4])}")
        if guidance.get("sectors_to_underweight"):
            lines.append(f"  ▼ Underweight: {', '.join(guidance['sectors_to_underweight'][:4])}")
        lines.append("")

        # ── TIER_1: Act Now ──
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
                    lines.append(f"  💬 {c['thesis']}")
                if c.get("entry_note"):
                    lines.append(f"  📍 Entry: {c['entry_note']}")
                if c.get("invalidation"):
                    lines.append(f"  ❌ Invalid if: {c['invalidation']}")
                if c.get("catalyst"):
                    lines.append(f"  💡 Catalyst: {c['catalyst'][:100]}")
                if c.get("risks"):
                    risk_str = " · ".join(str(r) for r in (c["risks"] or [])[:2])
                    if risk_str:
                        lines.append(f"  ⚠️ Risks: {risk_str}")
                ez = zone_line(c["symbol"], msl_map)
                if ez:
                    lines.append(f"  {ez}")
                if c.get("lessons_applied"):
                    lessons_str = " · ".join((c["lessons_applied"] or [])[:2])
                    if lessons_str:
                        lines.append(f"  📚 Rules applied: <i>{lessons_str[:120]}</i>")
            lines.append("")

        # ── TIER_2: Watch for trigger ──
        if tier2:
            lines.append(f"🔭 <b>TIER 2 — WATCH FOR TRIGGER ({len(tier2)})</b>")
            for c in tier2:
                c_ico  = conviction_icon(c.get("conviction"))
                action = c.get("action", "?")
                lines.append(
                    f"\n  {c_ico} <b>{c['symbol']}</b>  [{action}]"
                )
                if c.get("thesis"):
                    lines.append(f"  💬 {c['thesis'][:150]}")
                if c.get("entry_note"):
                    lines.append(f"  📍 Trigger: {c['entry_note']}")
                if c.get("invalidation"):
                    lines.append(f"  ❌ Invalid if: {c['invalidation']}")
                ez = zone_line(c["symbol"], msl_map)
                if ez:
                    lines.append(f"  {ez}")
            lines.append("")

        # ── TIER_3: Monitor ──
        if tier3:
            lines.append(f"👁 <b>TIER 3 — MONITOR ({len(tier3)})</b>")
            t3_str = " · ".join(
                f"{c['symbol']}({conviction_icon(c.get('conviction'))})"
                for c in tier3
            )
            lines.append(f"  {t3_str}")
            lines.append("")

        # Near-miss upgrades flagged by step 18
        near_miss_sigs = [s for s in data["signals"]
                          if s.get("signal_type") == "WATCH"
                          and "NEAR_MISS_UPGRADE" in (s.get("ai_note") or "")]
        if near_miss_sigs:
            lines.append(f"💡 <b>NEAR MISS — FLAGGED BY AI ({len(near_miss_sigs)})</b>")
            lines.append("  <i>Didn't technically qualify — but AI sees a macro/news tailwind today</i>")
            for s in near_miss_sigs:
                note_raw = (s.get("ai_note") or "").replace("[NEAR_MISS_UPGRADE:", "").split("]")
                flag   = note_raw[0] if note_raw else "?"
                reason = note_raw[1].strip() if len(note_raw) > 1 else ""
                ez     = zone_line(s["symbol"], data["msl_map"])
                lines.append(f"\n  💡 <b>{s['symbol']}</b> [{flag}]  {s.get('sector','?')}")
                if reason: lines.append(f"  {reason[:160]}")
                if ez:     lines.append(f"  {ez}")
            lines.append("")
            
        # ── Sector exposure warnings ──
        if warnings:
            lines.append("⚠️ <b>SECTOR CONCENTRATION WARNINGS</b>")
            for w in warnings:
                lines.append(
                    f"  {w.get('sector','?')}: {w.get('candidate_count','?')} candidates "
                    f"(already hold {w.get('already_held',0)}) — {w.get('recommendation','')[:120]}"
                    f"  → Allow only: <b>{w.get('allow_count',1)}</b>"
                )
            lines.append("")

        # ── Correlation groups ──
        if corr_groups:
            lines.append("⚡ <b>CORRELATION GROUPS</b>")
            for g in corr_groups:
                lines.append(
                    f"  [{g.get('group_label','?')}] {g.get('symbols',[])} "
                    f"— {g.get('recommendation','')[:120]}"
                )
            lines.append("")

    else:
        # ── FALLBACK: Step 19 didn't run — use signal_log directly (v2 behaviour) ──
        lines.append("<i>⚠️ Step 19 AI picks not available — showing raw signals</i>")
        ENTRY_TYPES = {"BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP",
                       "REENTRY_SETUP", "STAGED_ENTRY"}
        buys = sorted(
            [s for s in signals if s["signal_type"] in ENTRY_TYPES],
            key=lambda x: x.get("score_adjusted") or x.get("score") or 0, reverse=True
        )
        if buys:
            lines.append(f"\n🎯 <b>SIGNALS ({len(buys)})</b>")
            for s in buys:
                c_ico = conviction_icon(s.get("ai_conviction"))
                lines.append(
                    f"\n  {c_ico} <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                    f"Score:<b>{s.get('score_adjusted', s.get('score',0)):.0f}</b> "
                    f"→ {s.get('ai_suggested_action','?')}"
                )
                if s.get("ai_conviction_reason"):
                    lines.append(f"  💬 {s['ai_conviction_reason'][:150]}")
                ez = zone_line(s["symbol"], msl_map)
                if ez: lines.append(f"  {ez}")
        lines.append("")

    # ── Open positions ──
    if open_pos:
        lines.append(f"<b>📂 OPEN POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl    = float(p.get("pnl_pct") or 0)
            cp     = float(p.get("current_price") or 0)
            sl     = float(p.get("active_sl") or 0)
            action = p.get("action_required") or "HOLD"
            ev     = p.get("event_risk") or ""
            ico    = "📈" if pnl >= 0 else "📉"
            prox   = sl_proximity_pct(cp, sl)
            sl_w   = "  🚨<b>Near SL!</b>" if prox is not None and prox <= 3.0 else ""

            # Get signal for this position
            sig = next((s for s in signals if s.get("symbol") == p.get("symbol")), {})
            pos_signal = sig.get("signal_type", "HOLD")
            pos_reason = sig.get("filter_reason") or sig.get("ai_conviction_reason") or ""

            lines.append(
                f"\n  {ico} <b>{p['symbol']}</b> [{p.get('strategy','?')}]"
                f"  P&L: <b>{fmt_pct(pnl)}</b>  Held: {days_held(p.get('entry_date'))}{sl_w}"
            )
            lines.append(
                f"  → Signal: <b>{pos_signal}</b>  Action: <b>{action}</b>"
                + (f"  SL:{fmt_price(sl)}" if sl else "")
                + (f"  CMP:{fmt_price(cp)}" if cp else "")
            )
            if pos_reason:
                lines.append(f"  <i>{pos_reason[:120]}</i>")
            if ev:
                lines.append(f"  ⚠️ Event: {ev}")
        lines.append("")
    pos_events = data.get("pos_events", [])
    if pos_events:
        lines.append("📅 <b>POSITION EVENT RISK (next 5 days)</b>")
        for e in pos_events[:5]:
            lines.append(f"  ⚠️ <b>{e['symbol']}</b>: {e.get('event_type','')} on {str(e.get('event_date',''))[:10]}")
        lines.append("")

    # ── Exit signals ──
    exits = [s for s in signals if s.get("signal_type") == "EXIT"]
    adds  = [s for s in signals if s.get("signal_type") == "ADD"]

    if exits:
        lines.append(f"🔴 <b>EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            reason = s.get("filter_reason") or s.get("ai_conviction_reason") or ""
            lines.append(
                f"  <b>{s['symbol']}</b>"
                + (f"  <i>— {reason[:100]}</i>" if reason else "")
            )
        lines.append("")

    if adds:
        lines.append(f"🟢 <b>ADD TO POSITION ({len(adds)})</b>")
        for s in adds:
            reason = s.get("ai_conviction_reason") or s.get("filter_reason") or ""
            lines.append(
                f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score:{s.get('score_adjusted', s.get('score',0)):.0f}"
                + (f"  <i>{reason[:80]}</i>" if reason else "")
            )
        lines.append("")

    # ── Footer ──
    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(fii_line)

    t1c = len(fp.get("ranked_candidates", [])) if fp else 0
    lines.append(
        f"<i>━━━ {t1c} ranked · {len(exits)} exit · {len(adds)} add · "
        f"{len(open_pos)} open  "
        f"| Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule ━━━</i>"
    )
    return "\n".join(lines)


def build_morning(data: dict) -> str:
    """
    Morning brief: yesterday's picks with overnight context.
    Shows what TIER_1 picks are still valid + Gift Nifty gap.
    """
    fp      = data.get("final_picks")
    regime  = data["regime"]
    fii     = data["fii"]
    cues    = data["cues"]
    date_str = data["signal_date"]
    msl_map = data["msl_map"]

    lines = [f"<b>🌅 TradeOS Morning · {date_str}</b>", ""]
    lines += build_regime_header(regime, fii, cues)
    lines.append("")

    if fp and fp.get("ranked_candidates"):
        ranked = fp["ranked_candidates"]
        tier1  = [r for r in ranked if r.get("tier") == "TIER_1"]
        guidance = fp.get("portfolio_guidance", {})
        sizing   = guidance.get("position_sizing_override") or fp.get("_sizing", "?")

        lines.append(f"💼 <b>TODAY'S APPROACH: {sizing}</b>")
        if guidance.get("new_positions_guidance"):
            lines.append(f"  <i>{guidance['new_positions_guidance'][:150]}</i>")
        lines.append("")

        if tier1:
            lines.append(f"⭐ <b>WATCHLIST ({len(tier1)} TIER_1 picks)</b>")
            for c in tier1:
                c_ico  = conviction_icon(c.get("conviction"))
                action = c.get("action", "?")
                alloc  = float(c.get("suggested_allocation_pct") or 0)
                lines.append(
                    f"  {c_ico} <b>{c['symbol']}</b>  [{action}]"
                    + (f"  {alloc:.0f}%" if alloc else "")
                )
                if c.get("entry_note"):
                    lines.append(f"     📍 {c['entry_note']}")
                ez = zone_line(c["symbol"], msl_map)
                if ez:
                    lines.append(f"     {ez}")
        lines.append("")

    # Open positions needing attention
    exits = [s for s in data["signals"] if s.get("signal_type") == "EXIT"]
    if exits:
        lines.append("🔴 <b>EXIT TODAY:</b> " + " · ".join(s["symbol"] for s in exits))
        lines.append("")

    adds = [s for s in data["signals"] if s.get("signal_type") == "ADD"]
    if adds:
        lines.append("🟢 <b>ADD TODAY:</b> " + " · ".join(s["symbol"] for s in adds))

    return "\n".join(lines)


def build_compact(data: dict) -> str:
    """Ultra-compact summary for quick mobile check."""
    fp       = data.get("final_picks")
    regime   = data["regime"]
    fii      = data["fii"]
    open_pos = data["open_pos"]
    date_str = data["signal_date"]

    r_label = regime.get("predicted_regime") or regime.get("regime", "?")
    nifty   = regime.get("nifty_price")
    fii_flag = fii.get("fii_flag", "?")

    lines = [f"<b>📊 {date_str}</b>  {regime_icon(r_label)}{r_label}  Nifty:{fmt_price(nifty)}  {fii_icon(fii_flag)}FII:{fii_flag}", ""]

    if fp and fp.get("ranked_candidates"):
        ranked = fp["ranked_candidates"]
        tier1  = [r for r in ranked if r.get("tier") == "TIER_1"]
        tier2  = [r for r in ranked if r.get("tier") == "TIER_2"]
        sizing = (fp.get("portfolio_guidance") or {}).get("position_sizing_override") or fp.get("_sizing", "?")

        lines.append(f"💼 {sizing}")
        if tier1:
            lines.append(f"\n⭐ <b>ACT NOW</b>")
            for c in tier1:
                lines.append(
                    f"  {conviction_icon(c.get('conviction'))} <b>{c['symbol']}</b> "
                    f"[{c.get('action','?')}] {float(c.get('suggested_allocation_pct',0)):.0f}%"
                )
        if tier2:
            t2str = " · ".join(c["symbol"] for c in tier2[:5])
            lines.append(f"\n🔭 <b>WATCH:</b> {t2str}")
    else:
        ENTRY_TYPES = {"BUY_CANDIDATE", "PRIME_SETUP", "BREAKOUT_SETUP", "REENTRY_SETUP"}
        buys = [s for s in data["signals"] if s.get("signal_type") in ENTRY_TYPES]
        if buys:
            lines.append(f"🎯 <b>BUYS ({len(buys)})</b>")
            for s in buys[:5]:
                lines.append(f"  {conviction_icon(s.get('ai_conviction'))} <b>{s['symbol']}</b> {s.get('score_adjusted', s.get('score',0)):.0f}pt")

    if open_pos:
        lines.append(f"\n📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl  = float(p.get("pnl_pct") or 0)
            ico  = "📈" if pnl >= 0 else "📉"
            prox = sl_proximity_pct(p.get("current_price"), p.get("active_sl"))
            sl_w = " 🚨" if prox is not None and prox <= 3.0 else ""
            lines.append(f"  {ico}{sl_w} <b>{p['symbol']}</b> {fmt_pct(pnl)} {days_held(p.get('entry_date'))}")

    exits = [s for s in data["signals"] if s.get("signal_type") == "EXIT"]
    adds  = [s for s in data["signals"] if s.get("signal_type") == "ADD"]
    if exits: lines.append("\n🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))
    if adds:  lines.append("🟢 <b>ADD:</b> "   + " · ".join(s["symbol"] for s in adds))

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    import os
    logger.info(f"send_alerts v3 | argv={sys.argv}")
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
    logger.info(
        f"Data loaded: step19={'✅' if has_fp else '❌ fallback'} | "
        f"{len(data['signals'])} signals | {len(data['open_pos'])} positions"
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

    fp = data.get("final_picks") or {}
    ranked = fp.get("ranked_candidates") or []
    tier1 = len([r for r in ranked if r.get("tier") == "TIER_1"])
    tier2 = len([r for r in ranked if r.get("tier") == "TIER_2"])
    exits = len([s for s in data["signals"] if s.get("signal_type") == "EXIT"])
    adds  = len([s for s in data["signals"] if s.get("signal_type") == "ADD"])

    if success:
        logger.success(
            f"Telegram ({mode}) sent: T1:{tier1} T2:{tier2} exit:{exits} add:{adds} "
            f"pos:{len(data['open_pos'])} | step19:{'ok' if has_fp else 'fallback'}"
        )
    return {"sent": success, "mode": mode, "tier1": tier1, "tier2": tier2, "step19": has_fp}


if __name__ == "__main__":
    main()