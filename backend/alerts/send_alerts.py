"""
TradeOS v6 — Telegram Alerts (Phase 1)
Two modes:
  Evening (default) — full signal digest after 6 PM pipeline
  Morning           — pre-market brief at 7 AM before market opens

Usage:
  python alerts/send_alerts.py            → evening digest
  python alerts/send_alerts.py --morning  → morning pre-market brief

Switch evening format: change MESSAGE_STYLE below
"""
import sys
from pathlib import Path
from datetime import timedelta, date as _date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, cfg_bool, today_ist

# ── Evening format switch ──────────────────────────────────────────────────
# Options: "compact" | "structured"
MESSAGE_STYLE = "structured"


# ── Helpers ────────────────────────────────────────────────────────────────

def send_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False

    MAX_LEN = 4000  # safe buffer below 4096

    # Split on double newlines to avoid breaking mid-line
    chunks, current = [], ""
    for para in text.split("\n\n"):
        block = para + "\n\n"
        if len(current) + len(block) > MAX_LEN:
            chunks.append(current.strip())
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current.strip())

    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        for chunk in chunks:
            resp = requests.post(url, json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       chunk,
                "parse_mode": "HTML"
            })
            if not resp.ok:
                logger.error(f"Telegram API error: {resp.status_code} — {resp.text[:200]}")
                return False
        return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def conviction_icon(c: str) -> str:
    return {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(str(c or "").upper(), "⚪")

def regime_icon(r: str) -> str:
    return {"RISK ON": "🟢", "NEUTRAL": "🟡", "RISK OFF": "🔴"}.get(str(r or ""), "⚪")

def gap_icon(gap_signal: str) -> str:
    s = str(gap_signal or "").upper()
    if "STRONG_UP" in s:   return "🚀"
    if "UP" in s:          return "📈"
    if "STRONG_DOWN" in s: return "🔻"
    if "DOWN" in s:        return "📉"
    return "➡️"

def chg_icon(v) -> str:
    try: return "▲" if float(v) >= 0 else "▼"
    except: return ""

def fmt_chg(v, decimals=2) -> str:
    try: return f"{abs(float(v)):.{decimals}f}%"
    except: return "?"

def fmt_price(v) -> str:
    try: return f"{float(v):,.0f}"
    except: return "?"

def fmt_pct(v) -> str:
    try: return f"{float(v):+.1f}%"
    except: return "?"

def days_held(entry_date_str) -> str:
    try:
        d = _date.fromisoformat(str(entry_date_str))
        return f"{(today_ist() - d).days}d"
    except:
        return "?"

def truncate(text, n=80) -> str:
    if not text: return ""
    s = str(text)
    return s[:n] + "…" if len(s) > n else s

def top_risk(risks) -> str:
    if not risks: return ""
    if isinstance(risks, list) and risks:
        return (str(risks[0]))
    return (str(risks),)

def sl_proximity_pct(current_price, active_sl) -> float | None:
    """How far current price is above SL as a percentage."""
    try:
        cp = float(current_price or 0)
        sl = float(active_sl or 0)
        if cp > 0 and sl > 0:
            return ((cp - sl) / cp) * 100
    except:
        pass
    return None


# ── Data loading ────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:
    """Common data loader used by both morning and evening."""

    # Signals — with weekend/holiday fallback
    signals = sb.table("signal_log").select("*").eq("date", today).execute().data
    signal_date = today
    if not signals:
        latest = sb.table("signal_log").select("date").order("date", desc=True).limit(1).execute().data
        if latest:
            signal_date = latest[0]["date"]
            signals = sb.table("signal_log").select("*").eq("date", signal_date).execute().data

    # ai_context — keyed by symbol, prefer signal_date then latest available
    ai_rows = sb.table("ai_context").select("*").eq("date", signal_date).execute().data
    if not ai_rows:
        ai_rows = sb.table("ai_context").select("*").order("date", desc=True).limit(50).execute().data
    ai_map = {r["symbol"]: r for r in (ai_rows or [])}

    # Regime — last available
    regime_rows = sb.table("market_regime").select(
        "regime,nifty_price,india_vix,date"
    ).order("date", desc=True).limit(1).execute().data
    regime = regime_rows[0] if regime_rows else {}

    # Global cues — MORNING session preferred, fallback to latest
    cues_rows = sb.table("global_cues").select("*").order("date", desc=True).limit(2).execute().data
    cues = next((r for r in cues_rows if r.get("session") == "MORNING"), None)
    if not cues and cues_rows:
        cues = cues_rows[0]

    # Open positions
    open_pos = sb.table("open_positions").select(
        "symbol,strategy,pnl_pct,action_required,entry_date,"
        "active_sl,current_price,event_risk,sector,locked_profit"
    ).eq("status", "ACTIVE").execute().data

    # Lessons last 7 days
    week_ago = str(today_ist() - timedelta(days=7))
    lessons  = sb.table("lessons").select("id,source").gte("date", week_ago).execute().data

    return {
        "signals":     signals,
        "signal_date": signal_date,
        "ai_map":      ai_map,
        "regime":      regime,
        "cues":        cues or {},
        "open_pos":    sorted(open_pos or [], key=lambda x: float(x.get("pnl_pct") or 0), reverse=True),
        "lessons_ai":  len([l for l in lessons if str(l.get("source","")).startswith("AI:")]),
        "lessons_rb":  len([l for l in lessons if l.get("source") == "RULE_BASED"]),
    }


def enrich_signal(sig: dict, ai_map: dict) -> dict:
    """Merge signal_log row with ai_context — ai_context fields take priority."""
    ai = ai_map.get(sig["symbol"], {})
    return {**sig, **{
        "ai_conviction":        ai.get("conviction")        or sig.get("ai_conviction"),
        "ai_conviction_reason": ai.get("conviction_reason") or sig.get("ai_conviction_reason"),
        "ai_risks":             ai.get("risks")             or [],
        "ai_catalyst":          ai.get("catalyst")          or "",
        "ai_suggested_action":  ai.get("suggested_action")  or sig.get("ai_suggested_action"),
        "ai_conflicts":         ai.get("conflicts")         or "",
        "ai_note":              ai.get("ai_note")           or sig.get("ai_note"),
        "ai_provider":          ai.get("provider")          or sig.get("ai_provider"),
        "ai_confidence":        ai.get("confidence")        or sig.get("ai_confidence"),
        "ai_fallback_used":     ai.get("fallback_used")     or sig.get("ai_fallback_used"),
    }}


# ══════════════════════════════════════════════════════════════════════════════
# MORNING BRIEF
# Purpose: Pre-market temperature before 9:15 AM open
# Content:
#   1. Global overnight cues — Gift Nifty gap, DOW, NQ, Crude, Gold, USD/INR
#   2. Previous close context — Nifty, VIX, regime
#   3. BUY_CANDIDATEs from yesterday — should you enter today given the gap?
#   4. Open positions — SL proximity warning based on overnight moves
# ══════════════════════════════════════════════════════════════════════════════

def build_morning(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data["cues"]
    open_pos = data["open_pos"]
    today    = data["signal_date"]

    buys = sorted(
        [enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "BUY_CANDIDATE"],
        key=lambda x: x.get("score", 0), reverse=True
    )

    # ── Gap assessment ─────────────────────────────────────────
    gift       = cues.get("gift_nifty")
    gift_chg   = cues.get("gift_nifty_chg_pct")
    gap_signal = cues.get("gap_signal", "")
    g_ico      = gap_icon(gap_signal)
    prev_nifty = regime.get("nifty_price")

    # Implied gap: Gift Nifty vs prev Nifty close
    implied_gap_pts = None
    implied_gap_pct = None
    if gift and prev_nifty:
        try:
            implied_gap_pts = float(gift) - float(prev_nifty)
            implied_gap_pct = (implied_gap_pts / float(prev_nifty)) * 100
        except:
            pass

    lines = [
        f"<b>☀️ TradeOS · Morning Brief · {today_ist()}</b>",
        "<i>Pre-market — markets open at 9:15 AM IST</i>",
        "",
    ]

    # ── SECTION 1: GLOBAL OVERNIGHT ────────────────────────────
    lines.append("<b>🌍 OVERNIGHT GLOBAL CUES</b>")

    # ── Line 1: Nifty prev close + Gift Nifty implied open + gap ──
    if prev_nifty and gift:
        gap_str = (
            f"  {g_ico} <b>{implied_gap_pts:+.0f} pts ({implied_gap_pct:+.2f}%)</b>"
            if implied_gap_pts is not None else ""
        )
        gift_chg_str = (
            f"  Gift chg: {chg_icon(gift_chg)}{fmt_chg(gift_chg)}"
            if gift_chg else ""
        )
        lines.append(
            f"  📊 Nifty prev close: <b>{fmt_price(prev_nifty)}</b>  "
            f"→  Gift Nifty: <b>{fmt_price(gift)}</b>"
            f"{gift_chg_str}{gap_str}"
        )
    elif prev_nifty:
        lines.append(
            f"  📊 Nifty prev close: <b>{fmt_price(prev_nifty)}</b>  "
            f"(Gift Nifty unavailable)"
        )
    elif gift:
        lines.append(
            f"  🎁 Gift Nifty: <b>{fmt_price(gift)}</b>  "
            f"{chg_icon(gift_chg)}{fmt_chg(gift_chg)}"
        )

    # ── Line 2: Gap signal + Regime + VIX ─────────────────────
    gap_label = f"{g_ico} <b>{gap_signal}</b>  " if gap_signal else ""
    vix_val   = regime.get("india_vix")
    vix_warn  = "  ⚠️ Elevated VIX" if vix_val and float(vix_val or 0) > 18 else ""
    lines.append(
        f"  {gap_label}"
        f"{regime_icon(regime.get('regime',''))} Regime: <b>{regime.get('regime','?')}</b>  "
        f"VIX: <b>{fmt_price(vix_val)}</b>{vix_warn}"
    )

    # ── Line 3: US markets ─────────────────────────────────────────────
    dow       = cues.get("us_dow_close")
    dow_chg   = cues.get("us_dow_chg_pct")
    nasdaq    = cues.get("us_nasdaq_close")
    nas_chg   = cues.get("us_nasdaq_chg_pct")
    sp500     = cues.get("sp500_close")
    sp500_chg = cues.get("sp500_chg_pct")
    if dow or nasdaq or sp500:
        parts = []
        if dow:   parts.append(f"DOW <b>{fmt_price(dow)}</b> {chg_icon(dow_chg)}{fmt_chg(dow_chg)}")
        if sp500: parts.append(f"S&P <b>{fmt_price(sp500)}</b> {chg_icon(sp500_chg)}{fmt_chg(sp500_chg)}")
        if nasdaq:parts.append(f"NQ <b>{fmt_price(nasdaq)}</b> {chg_icon(nas_chg)}{fmt_chg(nas_chg)}")
        lines.append("  🇺🇸 " + "  ".join(parts))

    # ── Line 4: Commodities ────────────────────────────────────
    crude     = cues.get("brent_crude")
    crude_chg = cues.get("brent_chg_pct")
    gold      = cues.get("gold_price")
    if crude or gold:
        parts = []
        if crude: parts.append(
            f"Crude <b>{fmt_price(crude)}</b> {chg_icon(crude_chg)}{fmt_chg(crude_chg)}"
        )
        if gold: parts.append(f"Gold <b>{fmt_price(gold)}</b>")
        lines.append("  🛢️ " + "  ".join(parts))

    # ── Line 5: USD/INR ───────────────────────────────────────
    usd     = cues.get("usd_inr")
    usd_chg = cues.get("usd_inr_chg_pct")
    if usd:
        rupee_warn = "  ⚠️ Rupee pressure" if usd_chg and float(usd_chg or 0) > 0.3 else ""
        lines.append(
            f"  💱 USD/INR: <b>₹{fmt_price(usd)}</b>  "
            f"{chg_icon(usd_chg)}{fmt_chg(usd_chg)}{rupee_warn}"
        )

    lines.append("")

    # ── SECTION 2: ENTRY DECISION — BUY CANDIDATES ─────────────
    lines.append(f"<b>🎯 ENTRY WATCH — BUY CANDIDATES ({len(buys)})</b>")

    if buys:
        # Gap context for entry decisions
        gap_favourable = gap_signal and any(
            x in str(gap_signal).upper() for x in ["UP", "FLAT"]
        )
        gap_unfavourable = gap_signal and "DOWN" in str(gap_signal).upper()

        if gap_unfavourable:
            lines.append(
                "  ⚠️ <b>Gap DOWN detected</b> — consider waiting for gap fill "
                "before entering BUY_CANDIDATEs"
            )
        elif gap_favourable:
            lines.append(
                "  ✅ Gap supports entry — monitor for confirmed breakout above "
                "entry zone in first 30 min"
            )

        lines.append("")

        for s in buys[:5]:
            conv     = s.get("ai_conviction") or "—"
            c_ico    = conviction_icon(conv)
            action   = s.get("ai_suggested_action") or "—"
            reason   = (s.get("ai_conviction_reason") or "")
            risk1    = top_risk(s.get("ai_risks"))
            conflicts = s.get("ai_conflicts") or ""
            conf     = s.get("ai_confidence")
            conf_str = f" {float(conf):.0%}" if conf else ""
            warn     = " ⚠️RISK OFF" if s.get("regime_warning") else ""
            asm      = " 🚫ASM" if s.get("asm_flag") else ""

            # Entry go/no-go based on gap + AI
            if gap_unfavourable and conv == "HIGH":
                entry_call = "⏳ Wait for gap fill"
            elif gap_unfavourable:
                entry_call = "❌ Skip today"
            elif conv == "HIGH" and action == "ENTER":
                entry_call = "✅ Watch for entry"
            elif conv == "MEDIUM":
                entry_call = "👀 Monitor closely"
            else:
                entry_call = "⏭️ Pass today"

            lines.append(
                f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score:{s.get('score',0):.0f}  "
                f"{c_ico} {conv}{conf_str}{warn}{asm}"
            )
            lines.append(f"  → {entry_call}")
            if reason:
                lines.append(f"  💬 {reason}")
            if risk1:
                lines.append(f"  ⚠️ {risk1}")
            if conflicts and conflicts.upper() not in ("NONE", ""):
                lines.append(f"  ⚡ {(conflicts)}")
            lines.append("")

    else:
        lines.append("  — no buy candidates from yesterday's pipeline")
        lines.append("")

    # ── SECTION 3: OPEN POSITIONS — SL WATCH ──────────────────
    if open_pos:
        lines.append(f"<b>📂 OPEN POSITIONS — SL WATCH ({len(open_pos)})</b>")

        at_risk   = []
        healthy   = []
        for p in open_pos:
            prox = sl_proximity_pct(p.get("current_price"), p.get("active_sl"))
            if prox is not None and prox <= 3.0:
                at_risk.append((p, prox))
            else:
                healthy.append((p, prox))

        # At-risk positions first — need immediate attention
        if at_risk:
            lines.append("  🔴 <b>NEAR SL — Act if market opens weak:</b>")
            for p, prox in sorted(at_risk, key=lambda x: x[1]):
                pnl    = float(p.get("pnl_pct") or 0)
                action = p.get("action_required") or "HOLD"
                ev     = " ⚠️event" if p.get("event_risk") else ""
                lines.append(
                    f"    🚨 <b>{p['symbol']}</b> [{p.get('strategy','?')}]  "
                    f"P&L:{fmt_pct(pnl)}  SL gap: <b>{prox:.1f}%</b>  "
                    f"→ {action}{ev}"
                )
            lines.append("")

        # Healthy positions — quick status
        if healthy:
            lines.append("  🟢 <b>Healthy positions:</b>")
            for p, prox in sorted(healthy, key=lambda x: float(x[0].get("pnl_pct") or 0), reverse=True):
                pnl    = float(p.get("pnl_pct") or 0)
                action = p.get("action_required") or "HOLD"
                prox_str = f"  SL gap:{prox:.1f}%" if prox is not None else ""
                locked   = float(p.get("locked_profit") or 0)
                lock_str = f"  Locked:₹{locked:.0f}" if locked > 0 else ""
                ico      = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"    {ico} <b>{p['symbol']}</b>  "
                    f"P&L:<b>{fmt_pct(pnl)}</b>  "
                    f"{days_held(p.get('entry_date'))}"
                    f"{prox_str}{lock_str}  → {action}"
                )

        lines.append("")

    # ── FOOTER ────────────────────────────────────────────────
    lines.append(
        f"<i>━━━ Data: global_cues MORNING session · "
        f"Signals from {data['signal_date']} · "
        f"{len(open_pos)} open positions ━━━</i>"
    )


    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# EVENING DIGEST — COMPACT
# ══════════════════════════════════════════════════════════════════════════════

def build_compact(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data["cues"]
    open_pos = data["open_pos"]

    buys  = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "BUY_CANDIDATE"],
                   key=lambda x: x.get("score", 0), reverse=True)
    exits = [enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "EXIT"]
    adds  = [s for s in signals if s["signal_type"] == "ADD"]

    regime_name = regime.get("regime", "UNKNOWN")
    r_ico = regime_icon(regime_name)

    lines = [f"<b>📊 TradeOS · {data['signal_date']}</b>", ""]

    lines.append(
        f"{r_ico} <b>{regime_name}</b>  "
        f"Nifty {fmt_price(regime.get('nifty_price'))}  "
        f"VIX {fmt_price(regime.get('india_vix'))}"
    )

    if cues:
        parts = []
        if cues.get("gift_nifty"):
            parts.append(
                f"Gift {fmt_price(cues['gift_nifty'])} "
                f"{chg_icon(cues.get('gift_nifty_chg_pct'))}"
                f"{fmt_chg(cues.get('gift_nifty_chg_pct'))}"
            )
        if cues.get("us_dow_close"):   parts.append(f"DOW {fmt_price(cues['us_dow_close'])}")
        if cues.get("us_nasdaq_close"):parts.append(f"NQ {fmt_price(cues['us_nasdaq_close'])}")
        if cues.get("brent_crude"):
            parts.append(
                f"Crude {fmt_price(cues['brent_crude'])} "
                f"{chg_icon(cues.get('brent_chg_pct'))}"
                f"{fmt_chg(cues.get('brent_chg_pct'))}"
            )
        if cues.get("usd_inr"):        parts.append(f"₹{fmt_price(cues['usd_inr'])}/USD")
        if cues.get("gap_signal"):     parts.append(f"Gap:{cues['gap_signal']}")
        if parts:
            lines.append("🌐 " + "  ".join(parts))

    lines.append("")
    lines.append(f"🎯 <b>BUYS ({len(buys)})</b>")
    for s in buys[:5]:
        c_ico  = conviction_icon(s.get("ai_conviction"))
        action = s.get("ai_suggested_action") or "—"
        conf   = f" {float(s.get('ai_confidence') or 0):.0%}" if s.get("ai_confidence") else ""
        warn   = "⚠️" if s.get("regime_warning") else ""
        asm    = "🚫" if s.get("asm_flag") else ""
        lines.append(
            f"  {c_ico}{warn}{asm} <b>{s['symbol']}</b> "
            f"[{s.get('strategy','?')}] {s.get('score',0):.0f}pt"
            f"{conf} → {action}"
        )
    if not buys:
        lines.append("  — no setups today")

    if open_pos:
        lines.append("")
        lines.append(f"📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl    = float(p.get("pnl_pct") or 0)
            action = p.get("action_required") or "HOLD"
            ico    = "📈" if pnl >= 0 else "📉"
            ev     = "⚠️" if p.get("event_risk") else ""
            prox   = sl_proximity_pct(p.get("current_price"), p.get("active_sl"))
            sl_warn = " 🚨SL" if prox is not None and prox <= 3.0 else ""
            lines.append(
                f"  {ico}{ev}{sl_warn} <b>{p['symbol']}</b> {fmt_pct(pnl)} "
                f"{days_held(p.get('entry_date'))} → <b>{action}</b>"
            )

    if exits or adds:
        lines.append("")
        if exits:
            lines.append("🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))
        if adds:
            lines.append("🟢 <b>ADD:</b> " + " · ".join(s["symbol"] for s in adds))

    lines += ["", f"<i>Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule-based</i>"]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# EVENING DIGEST — STRUCTURED
# ══════════════════════════════════════════════════════════════════════════════

def build_structured(data: dict) -> str:
    signals  = data["signals"]
    ai_map   = data["ai_map"]
    regime   = data["regime"]
    cues     = data["cues"]
    open_pos = data["open_pos"]

    buys  = sorted([enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "BUY_CANDIDATE"],
                   key=lambda x: x.get("score", 0), reverse=True)
    exits = [enrich_signal(s, ai_map) for s in signals if s["signal_type"] == "EXIT"]
    adds  = [s for s in signals if s["signal_type"] == "ADD"]

    regime_name = regime.get("regime", "UNKNOWN")
    r_ico = regime_icon(regime_name)

    lines = [f"<b>━━━ 📊 TradeOS v6 · {data['signal_date']} ━━━</b>", ""]

    # Market context
    lines.append("<b>🌍 MARKET CONTEXT</b>")
    lines.append(
        f"  {r_ico} Regime: <b>{regime_name}</b>  "
        f"Nifty: <b>{fmt_price(regime.get('nifty_price'))}</b>  "
        f"VIX: <b>{fmt_price(regime.get('india_vix'))}</b>"
    )

    if cues:
        gift      = cues.get("gift_nifty")
        gift_chg  = cues.get("gift_nifty_chg_pct")
        dow       = cues.get("us_dow_close")
        nasdaq    = cues.get("us_nasdaq_close")
        crude     = cues.get("brent_crude")
        crude_chg = cues.get("brent_chg_pct")
        usd       = cues.get("usd_inr")
        usd_chg   = cues.get("usd_inr_chg_pct")
        gap       = cues.get("gap_signal", "")

        if gift:
            lines.append(
                f"  🎁 Gift Nifty: <b>{fmt_price(gift)}</b> "
                f"{chg_icon(gift_chg)}{fmt_chg(gift_chg)}"
                + (f"  Gap: <b>{gap}</b>" if gap else "")
            )
        if dow or nasdaq:
            parts = []
            if dow:    parts.append(f"DOW <b>{fmt_price(dow)}</b>")
            if nasdaq: parts.append(f"NQ <b>{fmt_price(nasdaq)}</b>")
            lines.append("  🇺🇸 " + "  ".join(parts))
        if crude or usd:
            parts = []
            if crude: parts.append(f"Crude <b>{fmt_price(crude)}</b> {chg_icon(crude_chg)}{fmt_chg(crude_chg)}")
            if usd:   parts.append(f"₹<b>{fmt_price(usd)}</b>/USD {chg_icon(usd_chg)}{fmt_chg(usd_chg)}")
            lines.append("  🛢️ " + "  ".join(parts))

    lines.append("")

    # Buy candidates
    lines.append(f"<b>🎯 BUY CANDIDATES ({len(buys)})</b>")
    if buys:
        for s in buys[:5]:
            conv      = s.get("ai_conviction") or "—"
            c_ico     = conviction_icon(conv)
            reason    = s.get("ai_conviction_reason") or ""
            risk1     = top_risk(s.get("ai_risks"))
            catalyst  = (s.get("ai_catalyst") or "")
            conflicts = s.get("ai_conflicts") or ""
            action    = s.get("ai_suggested_action") or "—"
            provider  = s.get("ai_provider") or ""
            confidence = s.get("ai_confidence")
            ind_st    = s.get("industry_state") or ""
            ind_rk    = s.get("industry_rank") or ""
            warn      = " ⚠️RISK OFF" if s.get("regime_warning") else ""
            asm       = " 🚫ASM" if s.get("asm_flag") else ""
            conf_str  = f" {float(confidence):.0%}" if confidence else ""
            fallback  = " (fallback)" if s.get("ai_fallback_used") else ""

            lines.append(
                f"\n  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score: <b>{s.get('score',0):.0f}</b>{warn}{asm}"
            )
            lines.append(
                f"  {c_ico} <b>{conv}</b>{conf_str} → <b>{action}</b>"
                f"  <i>{provider}{fallback}</i>"
            )
            if reason:
                lines.append(f"  💬 {(reason)}")
            if risk1:
                lines.append(f"  ⚠️ Risk: {risk1}")
            if catalyst and catalyst.lower() not in ("none", "no catalyst", "no immediate catalyst", ""):
                lines.append(f"  💡 Catalyst: {catalyst}")
            if conflicts and conflicts.upper() not in ("NONE", ""):
                lines.append(f"  ⚡ Conflict: {(conflicts,)}")
            if ind_st or ind_rk:
                lines.append(f"  🏭 Industry: {ind_st}  Rank #{ind_rk}")
    else:
        lines.append("  — no setups today")

    lines.append("")

    # Open positions
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
            if ev:
                lines.append(f"  ⚠️ Event risk: {ev}")
        lines.append("")

    # Exits
    if exits:
        lines.append(f"<b>🔴 EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            c_ico  = conviction_icon(s.get("ai_conviction"))
            reason = (s.get("ai_conviction_reason") or "")
            lines.append(
                f"  {c_ico} <b>{s['symbol']}</b> [{s.get('strategy','?')}]"
                + (f"\n  💬 {reason}" if reason else "")
            )
        lines.append("")

    # Adds
    if adds:
        lines.append(f"<b>🟢 ADD TO POSITION ({len(adds)})</b>")
        for s in adds:
            lines.append(
                f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score: {s.get('score',0):.0f}"
            )
        lines.append("")

    lines.append(
        f"<i>━━━ {len(buys)} buy · {len(exits)} exit · {len(adds)} add  "
        f"| Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule-based ━━━</i>"
    )

    return "\n".join(lines)


# ── MAIN ───────────────────────────────────────────────────────────────────

def main():
    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info("Telegram alerts disabled — skipping")
        return {}

    is_morning = "--morning" in sys.argv

    from config import get_supabase
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

    buys  = len([s for s in data["signals"] if s["signal_type"] == "BUY_CANDIDATE"])
    exits = len([s for s in data["signals"] if s["signal_type"] == "EXIT"])
    adds  = len([s for s in data["signals"] if s["signal_type"] == "ADD"])

    if success:
        logger.success(
            f"Telegram ({mode}) sent: {buys} buys · {exits} exits · "
            f"{adds} adds · {len(data['open_pos'])} open positions"
        )
    return {"sent": success, "mode": mode, "buys": buys, "exits": exits}


if __name__ == "__main__":
    main()