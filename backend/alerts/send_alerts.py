"""
TradeOS v6 — Send Alerts v2
============================
Changes vs v1 — fixing the core swing trading output gap:

  FIX (critical): PRE_BREAKOUT_WATCH and STAGED_ENTRY signals were NEVER shown.
  All 3 builders filtered only "BUY_CANDIDATE" signal_type. This meant the
  system's primary swing advantage — advance notice 2-5 sessions before a move —
  was computed but never delivered to you.

  NEW in evening digest (build_structured / build_compact):
    Section: "🔍 ADVANCE NOTICE" showing PRE_BREAKOUT_WATCH + STAGED_ENTRY
    with signal_type label and timing context:
      PRE_BREAKOUT_WATCH → "2-5 sessions ahead"
      STAGED_ENTRY       → "1-3 sessions ahead"

  NEW in regime header:
    + Market breadth % (above_200dma_pct from market_regime)
    + Regime confidence (regime_confidence from market_regime)
    + FII 20d net trend (fii_net_20d from fii_dii_flow)
    + Predicted regime source label (ML vs manual)

  NEW in morning brief:
    + Advance notice signals shown in Section 2 (before BUY_CANDIDATEs)

  All other logic identical to v1. Drop-in replacement.
"""
import sys, time
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, today_ist, is_kill_switch_active,
    cfg, cfg_bool, DRY_RUN
)

MESSAGE_STYLE = cfg("telegram_message_style", "structured")  # compact | structured


# ── Telegram sender ───────────────────────────────────────────────────────────

def send_message(text: str) -> bool:
    import os, requests
    token   = os.environ.get("TELEGRAM_TOKEN") or cfg("telegram_token")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg("telegram_chat_id")
    if not token or not chat_id:
        logger.warning("Telegram credentials not set — skipping alert")
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
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 30))
                logger.warning(f"Telegram rate limit — waiting {retry_after}s")
                time.sleep(retry_after)
                continue
            # Split long messages
            if resp.status_code == 400 and len(text) > 4000:
                mid  = len(text) // 2
                send_message(text[:mid])
                return send_message(text[mid:])
            logger.error(f"Telegram error {resp.status_code}: {resp.text[:200]}")
            return False
        except Exception as e:
            wait = [5, 15, 30][attempt]
            logger.warning(f"Telegram attempt {attempt+1} failed: {e} — retrying in {wait}s")
            time.sleep(wait)
    return False


# ── Formatters ────────────────────────────────────────────────────────────────

def conviction_icon(c: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get((c or "").upper(), "⚪")

def regime_icon(r: str) -> str:
    return {"TRENDING": "🚀", "NEUTRAL": "⚖️", "CAUTION": "⚠️",
            "RISK OFF": "🔴", "RISK ON": "🚀"}.get((r or "").upper(), "❓")

def gap_icon(gap_signal: str) -> str:
    return {"GAP_UP": "📈", "GAP_DOWN": "📉", "FLAT": "➡️"}.get((gap_signal or "").upper(), "")

def chg_icon(v) -> str:
    if v is None: return ""
    return "▲" if float(v) > 0 else ("▼" if float(v) < 0 else "→")

def fmt_chg(v, decimals=2) -> str:
    if v is None: return ""
    return f"{float(v):+.{decimals}f}%"

def fmt_price(v) -> str:
    if v is None: return "—"
    return f"{float(v):,.0f}"

def fmt_pct(v) -> str:
    if v is None: return "—"
    return f"{float(v):+.1f}%"

def days_held(entry_date_str) -> str:
    if not entry_date_str: return ""
    try:
        from datetime import date
        ed = date.fromisoformat(str(entry_date_str)[:10])
        d  = (today_ist() - ed).days
        return f"{d}d"
    except Exception:
        return ""

def truncate(text, n=80) -> str:
    if not text: return ""
    return str(text)[:n] + ("…" if len(str(text)) > n else "")

def top_risk(risks) -> str:
    if not risks: return ""
    if isinstance(risks, str):
        try: risks = __import__("json").loads(risks)
        except Exception: return truncate(risks, 60)
    if isinstance(risks, list) and risks:
        return truncate(str(risks[0]), 60)
    return ""

def sl_proximity_pct(current_price, active_sl) -> float | None:
    try:
        cp, sl = float(current_price), float(active_sl)
        if sl <= 0 or cp <= 0: return None
        return (cp - sl) / cp * 100
    except Exception:
        return None

def fii_footer_line(fii: dict) -> str:
    if not fii: return ""
    flag    = str(fii.get("fii_flag") or "").upper()
    net     = fii.get("fii_net")
    net_5d  = fii.get("fii_net_5d")
    net_20d = fii.get("fii_net_20d")  # v2: added 20d trend
    ico     = {"BULLISH": "🟢", "BEARISH": "🔴", "NEUTRAL": "⚪"}.get(flag, "⚪")
    parts   = [f"{ico} FII: <b>{flag}</b>"]
    if net      is not None: parts.append(f"Today ₹{float(net):.0f}Cr")
    if net_5d   is not None: parts.append(f"5d ₹{float(net_5d):.0f}Cr")
    if net_20d  is not None: parts.append(f"20d ₹{float(net_20d):.0f}Cr")  # v2
    return "  ".join(parts)

def entry_zone_line(symbol: str, msl_map: dict) -> str:
    msl    = msl_map.get(symbol, {})
    ez_lo  = msl.get("entry_zone_low")
    ez_hi  = msl.get("entry_zone_high")
    cp     = msl.get("current_price")
    lc     = msl.get("lifecycle", "")
    if ez_lo:
        zone = f"₹{float(ez_lo):.0f}"
        if ez_hi: zone += f"–{float(ez_hi):.0f}"
        cp_str = f"  CMP ₹{float(cp):.0f}" if cp else ""
        lc_str = f"  [{lc}]" if lc else ""
        return f"Entry zone: <b>{zone}</b>{cp_str}{lc_str}"
    return ""

def signal_type_label(signal_type: str) -> str:
    """Human-readable label + timing context for each signal type."""
    return {
        "BUY_CANDIDATE":        ("✅ Entry now",            ""),
        "PRE_BREAKOUT_WATCH":   ("🔭 Breakout watch",       "2–5 sessions ahead"),
        "STAGED_ENTRY":         ("📍 Approaching zone",     "1–3 sessions ahead"),
        "REENTRY_SETUP":        ("↩️ Re-entry setup",        "Pullback to support"),
        "PRIME_SETUP":          ("⭐ Prime setup",           "All timeframes aligned"),
        "EXIT":                 ("🚪 Exit signal",          ""),
        "ADD":                  ("➕ Add to position",      ""),
        "HOLD":                 ("✋ Hold",                  ""),
        "WATCH":                ("👁 Watching",              ""),
        "BLOCKED_ASM":          ("🚫 ASM blocked",          ""),
        "BUY_BLOCKED_REGIME":   ("⛔ Regime blocked",       ""),
        "AVOID_ENTRY_EVENT":    ("📅 Event — avoid entry",  ""),
    }.get(signal_type, (signal_type, ""))


# ── Data loader ───────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    # Signals with weekend/holiday fallback
    signals     = sb.table("signal_log").select("*").eq("date", today).execute().data
    signal_date = today
    if not signals:
        latest = sb.table("signal_log").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            signal_date = latest[0]["date"]
            signals = sb.table("signal_log").select("*").eq("date", signal_date).execute().data

    # ai_context
    ai_rows = sb.table("ai_context").select("*").eq("date", signal_date).execute().data
    if not ai_rows:
        ai_rows = sb.table("ai_context").select("*").order("date", desc=True).limit(50).execute().data
    ai_map = {r["symbol"]: r for r in (ai_rows or [])}

    # v2: load richer regime fields including predicted_regime, regime_confidence, breadth
    regime_rows = sb.table("market_regime").select(
        "regime,predicted_regime,regime_confidence,nifty_price,india_vix,"
        "above_200dma_pct,avg_sector_breadth,advance_decline_ratio,date"
    ).order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # Global cues
    cues_rows = sb.table("global_cues").select("*").order("date", desc=True).limit(2).execute().data
    cues = next((r for r in cues_rows if r.get("session") == "MORNING"), None)
    if not cues and cues_rows:
        cues = cues_rows[0]

    # Open positions
    open_pos = sb.table("open_positions").select(
        "symbol,strategy,pnl_pct,action_required,entry_date,"
        "active_sl,current_price,event_risk,sector,locked_profit"
    ).eq("status", "ACTIVE").execute().data

    # Lessons
    week_ago = str(today_ist() - timedelta(days=7))
    lessons  = sb.table("lessons").select("id,source").gte("date", week_ago).execute().data

    # v2: load fii_net_20d for footer and regime context
    fii_rows = sb.table("fii_dii_flow").select(
        "fii_net,fii_net_5d,fii_net_20d,fii_flag,date"
    ).order("date", desc=True).limit(1).execute().data
    fii = fii_rows[0] if fii_rows else {}

    # Master shortlist
    msl_rows = sb.table("master_shortlist").select(
        "symbol,entry_zone_low,entry_zone_high,current_price,lifecycle"
    ).eq("date", today).execute().data
    if not msl_rows:
        msl_rows = sb.table("master_shortlist").select(
            "symbol,entry_zone_low,entry_zone_high,current_price,lifecycle"
        ).order("date", desc=True).limit(200).execute().data
    msl_map = {r["symbol"]: r for r in (msl_rows or [])}

    # Position events for morning brief
    pos_symbols = [p["symbol"] for p in (open_pos or [])]
    pos_events  = []
    if pos_symbols:
        try:
            _today   = today_ist()
            _cutoff  = str(_today + timedelta(days=5))
            _today_s = str(_today)
            pev = sb.table("nifty_upcoming_events").select(
                "symbol,purpose,event_date"
            ).in_("symbol", pos_symbols).gte("event_date", _today_s).lte("event_date", _cutoff).execute().data or []
            sev = sb.table("event_calendar").select(
                "symbol,event_type,event_date,is_active"
            ).in_("symbol", pos_symbols).eq("is_active", True).gte("event_date", _today_s).lte("event_date", _cutoff).execute().data or []
            seen = set()
            for e in pev:
                k = (e["symbol"], e["event_date"])
                if k not in seen:
                    seen.add(k)
                    pos_events.append({"symbol": e["symbol"], "event_type": e.get("purpose",""), "event_date": e["event_date"]})
            for e in sev:
                k = (e["symbol"], e["event_date"])
                if k not in seen:
                    seen.add(k)
                    pos_events.append({"symbol": e["symbol"], "event_type": e.get("event_type",""), "event_date": e["event_date"]})
        except Exception as _e:
            logger.debug(f"Position events fetch failed: {_e}")

    # AG3: data_anomalies ERROR rows
    anomalies = []
    try:
        anomalies = (sb.table("data_anomalies")
                       .select("check_name,message,severity,affected")
                       .eq("date", today)
                       .eq("severity", "ERROR")
                       .execute().data or [])
    except Exception as _ae:
        logger.debug(f"data_anomalies fetch failed: {_ae}")

    return {
        "signals":     signals,
        "signal_date": signal_date,
        "ai_map":      ai_map,
        "regime":      regime,
        "cues":        cues or {},
        "open_pos":    sorted(open_pos or [], key=lambda x: float(x.get("pnl_pct") or 0), reverse=True),
        "lessons_ai":  len([l for l in lessons if str(l.get("source","")).startswith("AI:")]),
        "lessons_rb":  len([l for l in lessons if l.get("source") == "RULE_BASED"]),
        "fii":         fii,
        "msl_map":     msl_map,
        "pos_events":  pos_events,
        "anomalies":   anomalies,
    }


def enrich_signal(sig: dict, ai_map: dict) -> dict:
    merged = dict(sig)
    ai = ai_map.get(sig.get("symbol"), {})
    for field in ["ai_conviction","ai_conviction_reason","ai_suggested_action",
                  "ai_note","ai_provider","ai_fallback_used","ai_catalyst",
                  "ai_risks","ai_confidence","ai_strategy_validation","ai_conflicts"]:
        if ai.get(field) is not None:
            merged[field] = ai[field]
    return merged


# ── Regime header (v2 — richer) ───────────────────────────────────────────────

def build_regime_header(regime: dict, fii: dict) -> list:
    """v2: includes breadth, regime confidence, ML vs manual label, FII 20d."""
    lines      = []
    r_name     = regime.get("regime", "UNKNOWN")
    r_pred     = regime.get("predicted_regime")
    r_conf     = regime.get("regime_confidence")
    r_ico      = regime_icon(r_name)
    breadth    = regime.get("above_200dma_pct") or regime.get("avg_sector_breadth")
    ad_ratio   = regime.get("advance_decline_ratio")
    vix        = regime.get("india_vix")
    nifty      = regime.get("nifty_price")

    # Main regime line
    regime_display = r_name
    regime_source  = ""
    if r_pred and r_pred != r_name:
        regime_source = f"  <i>(ML: {r_pred} {f'{float(r_conf):.0%}' if r_conf else ''})</i>"
    elif r_pred == r_name and r_conf:
        regime_source = f"  <i>(ML {float(r_conf):.0%} confidence)</i>"

    vix_warn = "  ⚠️ Elevated" if vix and float(vix) > 18 else ""
    lines.append(
        f"{r_ico} Regime: <b>{regime_display}</b>{regime_source}  "
        f"Nifty: <b>{fmt_price(nifty)}</b>  "
        f"VIX: <b>{fmt_price(vix)}</b>{vix_warn}"
    )

    # Breadth + A/D line
    breadth_parts = []
    if breadth is not None:
        b = float(breadth)
        b_ico = "📈" if b >= 55 else ("📉" if b < 40 else "➡️")
        breadth_parts.append(f"{b_ico} Breadth: <b>{b:.0f}%</b> above 200dma")
    if ad_ratio is not None:
        breadth_parts.append(f"A/D: <b>{float(ad_ratio):.2f}</b>")
    if breadth_parts:
        lines.append("  " + "  ".join(breadth_parts))

    return lines


# ── Morning brief ─────────────────────────────────────────────────────────────

def build_morning(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data["cues"]
    open_pos = data["open_pos"]
    fii      = data.get("fii", {})

    # All entry-type signals sorted by score
    ADVANCE_TYPES = {"PRE_BREAKOUT_WATCH", "STAGED_ENTRY"}
    ENTRY_TYPES   = {"BUY_CANDIDATE", "PRIME_SETUP"}
    advance = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ADVANCE_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)
    buys    = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ENTRY_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)

    lines = [f"<b>☀️ TradeOS Morning Brief · {data['signal_date']}</b>", ""]

    # Section 0: data anomalies
    anomalies = data.get("anomalies", [])
    if anomalies:
        lines.append("🚨 <b>DATA ALERTS</b>")
        for a in anomalies[:3]:
            lines.append(f"  ⚠️ {a.get('check_name','?')}: {truncate(a.get('message',''),80)}")
        if len(anomalies) > 3:
            lines.append(f"  <i>…{len(anomalies) - 3} more — check data_anomalies table</i>")
        lines.append("")

    # Section 1: global cues (v2 — includes 10yr yield, silver)
    lines.append("🌍 <b>GLOBAL CUES</b>")
    lines += build_regime_header(regime, fii)

    gift = cues.get("gift_nifty"); gift_chg = cues.get("gift_nifty_chg_pct")
    if gift:
        lines.append(
            f"  🎁 Gift Nifty: <b>{fmt_price(gift)}</b> "
            f"{chg_icon(gift_chg)}{fmt_chg(gift_chg)}"
            + (f"  {gap_icon(cues.get('gap_signal'))} {cues.get('gap_signal','')}" if cues.get("gap_signal") else "")
        )
    us_parts = []
    if cues.get("us_dow_close"):    us_parts.append(f"DOW <b>{fmt_price(cues['us_dow_close'])}</b>")
    if cues.get("sp500_close"):     us_parts.append(f"S&P <b>{fmt_price(cues['sp500_close'])}</b>")
    if cues.get("us_nasdaq_close"): us_parts.append(f"NQ <b>{fmt_price(cues['us_nasdaq_close'])}</b>")
    if us_parts: lines.append("  🇺🇸 " + "  ".join(us_parts))
    comm_parts = []
    if cues.get("brent_crude"): comm_parts.append(f"Crude <b>{fmt_price(cues['brent_crude'])}</b> {fmt_chg(cues.get('brent_chg_pct'),1)}")
    if cues.get("gold_price"):  comm_parts.append(f"Gold <b>{fmt_price(cues['gold_price'])}</b>")
    if cues.get("silver_price"):comm_parts.append(f"Silver <b>{fmt_price(cues['silver_price'])}</b> {fmt_chg(cues.get('silver_chg_pct'),1)}")
    if comm_parts: lines.append("  🛢️ " + "  ".join(comm_parts))
    if cues.get("usd_inr"):
        lines.append(f"  💱 ₹{fmt_price(cues['usd_inr'])}/USD {fmt_chg(cues.get('usd_inr_chg_pct'),2)}")
    if cues.get("us_10yr_yield"):
        bps = cues.get("us_10yr_chg_bps")
        bps_str = f" ({bps:+.0f}bps)" if bps is not None else ""
        yield_warn = "  ⚠️ Yield spike" if bps and float(bps) > 10 else ""
        lines.append(f"  🏦 US 10yr: <b>{cues['us_10yr_yield']:.2f}%</b>{bps_str}{yield_warn}")
    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(f"  {fii_line}")
    lines.append("")

    # Section 2: advance notice (NEW v2)
    if advance:
        lines.append(f"🔍 <b>ADVANCE NOTICE ({len(advance)})</b>  <i>Set alerts — don't enter yet</i>")
        for s in advance[:4]:
            lbl, timing = signal_type_label(s["signal_type"])
            timing_str  = f"  <i>{timing}</i>" if timing else ""
            c_ico       = conviction_icon(s.get("ai_conviction"))
            ez          = entry_zone_line(s["symbol"], data["msl_map"])
            lines.append(f"\n  {lbl} <b>{s['symbol']}</b> [{s.get('sector','?')}] Score:{s.get('score',0):.0f}{timing_str}")
            if ez: lines.append(f"  📍 {ez}")
            if s.get("ai_conviction"): lines.append(f"  {c_ico} {s.get('ai_conviction')} — {truncate(s.get('ai_conviction_reason',''),70)}")
        lines.append("")

    # Section 3: entry-ready buys
    if buys:
        lines.append(f"🎯 <b>ENTRY READY ({len(buys)})</b>")
        for s in buys[:6]:
            c_ico  = conviction_icon(s.get("ai_conviction"))
            action = s.get("ai_suggested_action") or "—"
            warn   = " ⚠️" if s.get("regime_warning") else ""
            asm    = " 🚫ASM" if s.get("asm_flag") else ""
            ez     = entry_zone_line(s["symbol"], data["msl_map"])
            lines.append(
                f"\n  {c_ico}{warn}{asm} <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score:{s.get('score',0):.0f} → {action}"
            )
            if ez: lines.append(f"  📍 {ez}")
        lines.append("")

    # Section 4: open positions SL watch
    if open_pos:
        lines.append(f"📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl   = float(p.get("pnl_pct") or 0)
            prox  = sl_proximity_pct(p.get("current_price"), p.get("active_sl"))
            sl_w  = " 🚨SL!" if prox is not None and prox <= 3 else ""
            ico   = "📈" if pnl >= 0 else "📉"
            ev    = " ⚠️" if p.get("event_risk") else ""
            lines.append(
                f"  {ico}{ev}{sl_w} <b>{p['symbol']}</b> {fmt_pct(pnl)} "
                f"{days_held(p.get('entry_date'))} → {p.get('action_required','HOLD')}"
            )

    # Section 5: position event risk
    pos_events = data.get("pos_events", [])
    if pos_events:
        lines.append("")
        lines.append("📅 <b>POSITION EVENT RISK (5d)</b>")
        for e in pos_events[:5]:
            lines.append(f"  ⚠️ {e['symbol']}: {e.get('event_type','')} on {str(e.get('event_date',''))[:10]}")

    return "\n".join(lines)


# ── Evening digest (structured) ───────────────────────────────────────────────

def build_structured(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data["cues"]
    open_pos = data["open_pos"]
    fii      = data.get("fii", {})
    msl_map  = data.get("msl_map", {})

    ADVANCE_TYPES = {"PRE_BREAKOUT_WATCH", "STAGED_ENTRY"}
    ENTRY_TYPES   = {"BUY_CANDIDATE", "PRIME_SETUP", "REENTRY_SETUP"}
    advance = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ADVANCE_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)
    buys    = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ENTRY_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)
    exits   = [enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "EXIT"]
    adds    = [s for s in signals if s["signal_type"] == "ADD"]

    lines = [f"<b>━━━ 📊 TradeOS v6 · {data['signal_date']} ━━━</b>", ""]

    # Market context (v2 — richer)
    lines.append("<b>🌍 MARKET CONTEXT</b>")
    lines += build_regime_header(regime, fii)
    if cues:
        gift = cues.get("gift_nifty"); gift_chg = cues.get("gift_nifty_chg_pct")
        if gift:
            lines.append(
                f"  🎁 Gift Nifty: <b>{fmt_price(gift)}</b> "
                f"{chg_icon(gift_chg)}{fmt_chg(gift_chg)}"
            )
        us_parts = []
        if cues.get("us_dow_close"):    us_parts.append(f"DOW <b>{fmt_price(cues['us_dow_close'])}</b>")
        if cues.get("sp500_close"):     us_parts.append(f"S&P <b>{fmt_price(cues['sp500_close'])}</b>")
        if cues.get("us_nasdaq_close"): us_parts.append(f"NQ <b>{fmt_price(cues['us_nasdaq_close'])}</b>")
        if us_parts: lines.append("  🇺🇸 " + "  ".join(us_parts))
        if cues.get("brent_crude") or cues.get("usd_inr"):
            parts = []
            if cues.get("brent_crude"): parts.append(f"Crude <b>{fmt_price(cues['brent_crude'])}</b> {fmt_chg(cues.get('brent_chg_pct'),1)}")
            if cues.get("usd_inr"):     parts.append(f"₹<b>{fmt_price(cues['usd_inr'])}</b>/USD")
            if cues.get("silver_price"):parts.append(f"Silver <b>{fmt_price(cues['silver_price'])}</b>")
            lines.append("  🛢️ " + "  ".join(parts))
        if cues.get("us_10yr_yield"):
            bps = cues.get("us_10yr_chg_bps")
            bps_str   = f" ({float(bps):+.0f}bps)" if bps is not None else ""
            yield_warn = "  ⚠️ Yield spike — FII pressure" if bps and float(bps) > 10 else ""
            lines.append(f"  🏦 US 10yr: <b>{cues['us_10yr_yield']:.2f}%</b>{bps_str}{yield_warn}")
    lines.append("")

    # ADVANCE NOTICE section (NEW v2 — the core swing advantage)
    if advance:
        lines.append(f"<b>🔍 ADVANCE NOTICE ({len(advance)}) — Position Before The Move</b>")
        for s in advance[:5]:
            lbl, timing   = signal_type_label(s["signal_type"])
            timing_str    = f"  <i>{timing}</i>" if timing else ""
            c_ico         = conviction_icon(s.get("ai_conviction"))
            reason        = s.get("ai_conviction_reason") or ""
            risk1         = top_risk(s.get("ai_risks"))
            ez            = entry_zone_line(s["symbol"], msl_map)
            warn          = " ⚠️CAUTION" if s.get("regime_warning") else ""
            score_str     = f" Score:<b>{s.get('score',0):.0f}</b>"
            scanner_str   = " 🎯+scanner" if s.get("in_scanner") else ""
            lines.append(
                f"\n  {lbl}{timing_str}{warn}\n"
                f"  <b>{s['symbol']}</b> [{s.get('sector','?')}]{score_str}{scanner_str}"
            )
            if ez:     lines.append(f"  📍 {ez}")
            if reason: lines.append(f"  {c_ico} {truncate(reason, 90)}")
            if risk1:  lines.append(f"  ⚠️ Risk: {risk1}")
        lines.append("")

    # Entry-ready buys
    lines.append(f"<b>🎯 ENTRY READY ({len(buys)})</b>")
    if buys:
        for s in buys[:5]:
            conv      = s.get("ai_conviction") or "—"
            c_ico     = conviction_icon(conv)
            reason    = s.get("ai_conviction_reason") or ""
            risk1     = top_risk(s.get("ai_risks"))
            catalyst  = s.get("ai_catalyst") or ""
            action    = s.get("ai_suggested_action") or "—"
            provider  = s.get("ai_provider") or ""
            confidence = s.get("ai_confidence")
            ind_st    = s.get("industry_state") or ""
            warn      = " ⚠️RISK OFF" if s.get("regime_warning") else ""
            asm       = " 🚫ASM" if s.get("asm_flag") else ""
            conf_str  = f" {float(confidence):.0%}" if confidence else ""
            fallback  = " (fallback)" if s.get("ai_fallback_used") else ""
            scanner_s = " 🎯scanner" if s.get("in_scanner") else ""
            lines.append(
                f"\n  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score: <b>{s.get('score',0):.0f}</b>{warn}{asm}{scanner_s}"
            )
            lines.append(f"  {c_ico} <b>{conv}</b>{conf_str} → <b>{action}</b>  <i>{provider}{fallback}</i>")
            if reason:   lines.append(f"  💬 {truncate(reason, 90)}")
            if risk1:    lines.append(f"  ⚠️ Risk: {risk1}")
            if catalyst and catalyst.lower() not in ("none","no catalyst",""):
                lines.append(f"  💡 Catalyst: {truncate(catalyst, 80)}")
            if ind_st:   lines.append(f"  🏭 Industry: {ind_st}")
            ez = entry_zone_line(s["symbol"], msl_map)
            if ez:       lines.append(f"  📍 {ez}")
    else:
        lines.append("  — no entry-ready setups today")
    lines.append("")

    # Open positions
    if open_pos:
        lines.append(f"<b>📂 OPEN POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl   = float(p.get("pnl_pct") or 0)
            cp    = float(p.get("current_price") or 0)
            sl    = float(p.get("active_sl") or 0)
            action = p.get("action_required") or "HOLD"
            ev     = p.get("event_risk") or ""
            ico    = "📈" if pnl >= 0 else "📉"
            prox   = sl_proximity_pct(cp, sl)
            sl_warn = "  🚨 Near SL!" if prox is not None and prox <= 3.0 else ""
            lines.append(
                f"\n  {ico} <b>{p['symbol']}</b> [{p.get('strategy','?')}]  "
                f"P&L: <b>{fmt_pct(pnl)}</b>  Held: {days_held(p.get('entry_date'))}{sl_warn}"
            )
            lines.append(
                f"  → Action: <b>{action}</b>"
                + (f"  SL: ₹{sl:.0f}" if sl else "")
                + (f"  CMP: ₹{cp:.0f}" if cp else "")
            )
            if ev: lines.append(f"  ⚠️ Event risk: {ev}")
        lines.append("")

    # Exits
    if exits:
        lines.append(f"<b>🔴 EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            reason = truncate(s.get("ai_conviction_reason") or "", 80)
            lines.append(f"  <b>{s['symbol']}</b>" + (f"\n  💬 {reason}" if reason else ""))
        lines.append("")

    # Adds
    if adds:
        lines.append(f"<b>🟢 ADD TO POSITION ({len(adds)})</b>")
        for s in adds:
            lines.append(f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] Score:{s.get('score',0):.0f}")
        lines.append("")

    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(fii_line)
    lines.append(
        f"<i>━━━ {len(advance)} advance · {len(buys)} entry · "
        f"{len(exits)} exit · {len(adds)} add  "
        f"| Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule ━━━</i>"
    )
    return "\n".join(lines)


# ── Compact evening ───────────────────────────────────────────────────────────

def build_compact(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data.get("cues", {})
    open_pos = data["open_pos"]
    fii      = data.get("fii", {})

    ADVANCE_TYPES = {"PRE_BREAKOUT_WATCH", "STAGED_ENTRY"}
    ENTRY_TYPES   = {"BUY_CANDIDATE", "PRIME_SETUP", "REENTRY_SETUP"}
    advance = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ADVANCE_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)
    buys    = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] in ENTRY_TYPES],
                     key=lambda x: x.get("score", 0), reverse=True)
    exits   = [enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "EXIT"]
    adds    = [s for s in signals if s["signal_type"] == "ADD"]

    lines = [f"<b>📊 TradeOS · {data['signal_date']}</b>", ""]
    lines += build_regime_header(regime, fii)
    if cues.get("gift_nifty"):
        lines.append(
            f"🎁 Gift {fmt_price(cues['gift_nifty'])} "
            f"{chg_icon(cues.get('gift_nifty_chg_pct'))}{fmt_chg(cues.get('gift_nifty_chg_pct'))}"
        )
    lines.append("")

    if advance:
        lines.append(f"🔍 <b>WATCH ({len(advance)})</b>  <i>ahead-of-time</i>")
        for s in advance[:3]:
            lbl, timing = signal_type_label(s["signal_type"])
            lines.append(
                f"  {lbl} <b>{s['symbol']}</b> {s.get('score',0):.0f}pt"
                + (f"  <i>{timing}</i>" if timing else "")
            )
        lines.append("")

    lines.append(f"🎯 <b>BUYS ({len(buys)})</b>")
    for s in buys[:5]:
        c_ico  = conviction_icon(s.get("ai_conviction"))
        action = s.get("ai_suggested_action") or "—"
        conf   = f" {float(s.get('ai_confidence') or 0):.0%}" if s.get("ai_confidence") else ""
        warn   = "⚠️" if s.get("regime_warning") else ""
        asm    = "🚫" if s.get("asm_flag") else ""
        lines.append(
            f"  {c_ico}{warn}{asm} <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
            f"{s.get('score',0):.0f}pt{conf} → {action}"
        )
    if not buys: lines.append("  — no setups today")

    if open_pos:
        lines.append(f"\n📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl   = float(p.get("pnl_pct") or 0)
            action = p.get("action_required") or "HOLD"
            ico    = "📈" if pnl >= 0 else "📉"
            ev     = "⚠️" if p.get("event_risk") else ""
            prox   = sl_proximity_pct(p.get("current_price"), p.get("active_sl"))
            sl_w   = " 🚨SL" if prox is not None and prox <= 3.0 else ""
            lines.append(
                f"  {ico}{ev}{sl_w} <b>{p['symbol']}</b> {fmt_pct(pnl)} "
                f"{days_held(p.get('entry_date'))} → <b>{action}</b>"
            )

    if exits or adds:
        lines.append("")
        if exits: lines.append("🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))
        if adds:  lines.append("🟢 <b>ADD:</b> "  + " · ".join(s["symbol"] for s in adds))

    fii_line = fii_footer_line(fii)
    if fii_line: lines.append(f"\n{fii_line}")
    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — send_alerts skipped")
        return {"status": "skipped"}

    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info("Telegram alerts disabled")
        return {}

    if "--position-risk" in sys.argv:
        logger.info("--position-risk covered by morning brief + sl_monitor")
        return {"status": "skipped", "reason": "covered"}

    is_morning = "--morning" in sys.argv

    sb    = get_supabase()
    today = str(today_ist())
    data  = load_data(sb, today)

    if is_morning:
        msg  = build_morning(data)
        mode = "morning"
    elif MESSAGE_STYLE == "compact":
        msg  = build_compact(data)
        mode = "compact"
    else:
        msg  = build_structured(data)
        mode = "structured"

    success = send_message(msg)

    advance = len([s for s in data["signals"] if s["signal_type"] in {"PRE_BREAKOUT_WATCH","STAGED_ENTRY"}])
    buys    = len([s for s in data["signals"] if s["signal_type"] in {"BUY_CANDIDATE","PRIME_SETUP"}])
    exits   = len([s for s in data["signals"] if s["signal_type"] == "EXIT"])
    adds    = len([s for s in data["signals"] if s["signal_type"] == "ADD"])

    if success:
        logger.success(
            f"Telegram ({mode}) sent: {advance} advance notice · {buys} buys · "
            f"{exits} exits · {adds} adds · {len(data['open_pos'])} open positions"
        )
    return {"sent": success, "mode": mode, "advance": advance, "buys": buys}


if __name__ == "__main__":
    main()