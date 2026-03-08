"""
TradeOS v6 — Telegram Alerts (Phase 1)
Merges signal_log + ai_context for rich AI-powered daily digest.
Two formats: compact (10-second scan) | structured (full reasoning)
Switch: change MESSAGE_STYLE below
"""
import sys
from pathlib import Path
from datetime import timedelta, date as _date
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, cfg_bool, today_ist

# ── Switch format here ─────────────────────────────────────────────────────
# Options: "compact" | "structured"
MESSAGE_STYLE = "structured"


# ── Helpers ────────────────────────────────────────────────────────────────

def send_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False
    try:
        import requests
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
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

def chg_icon(v) -> str:
    try: return "▲" if float(v) >= 0 else "▼"
    except: return ""

def fmt_chg(v) -> str:
    try: return f"{abs(float(v)):.2f}%"
    except: return "?"

def fmt_price(v) -> str:
    try: return f"{float(v):,.0f}"
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
    return truncate(str(risks), 70)


# ── Data loading ────────────────────────────────────────────────────────────

def load_data(sb, today: str) -> dict:

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

    # Global cues — morning session preferred, fallback to latest
    cues_rows = sb.table("global_cues").select("*").order("date", desc=True).limit(2).execute().data
    cues = next((r for r in cues_rows if r.get("session") == "MORNING"), None)
    if not cues and cues_rows:
        cues = cues_rows[0]

    # Open positions
    open_pos = sb.table("open_positions").select(
        "symbol,strategy,pnl_pct,action_required,entry_date,active_sl,current_price,event_risk,sector"
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
    """Merge signal_log row with ai_context row — ai_context fields take priority."""
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


# ── COMPACT FORMAT ─────────────────────────────────────────────────────────
# One line per stock — 10-second scan

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

    # Market snapshot
    lines.append(
        f"{r_ico} <b>{regime_name}</b>  "
        f"Nifty {fmt_price(regime.get('nifty_price'))}  "
        f"VIX {fmt_price(regime.get('india_vix'))}"
    )

    # Global cues one-liner
    if cues:
        parts = []
        if cues.get("gift_nifty"):
            parts.append(f"Gift {fmt_price(cues['gift_nifty'])} {chg_icon(cues.get('gift_nifty_chg_pct'))}{fmt_chg(cues.get('gift_nifty_chg_pct'))}")
        if cues.get("us_dow_close"):
            parts.append(f"DOW {fmt_price(cues['us_dow_close'])}")
        if cues.get("us_nasdaq_close"):
            parts.append(f"NQ {fmt_price(cues['us_nasdaq_close'])}")
        if cues.get("brent_crude"):
            parts.append(f"Crude {fmt_price(cues['brent_crude'])} {chg_icon(cues.get('brent_chg_pct'))}{fmt_chg(cues.get('brent_chg_pct'))}")
        if cues.get("usd_inr"):
            parts.append(f"₹{fmt_price(cues['usd_inr'])}/USD")
        if cues.get("gap_signal"):
            parts.append(f"Gap:{cues['gap_signal']}")
        if parts:
            lines.append("🌐 " + "  ".join(parts))

    lines.append("")

    # Buy candidates
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

    # Open positions
    if open_pos:
        lines.append("")
        lines.append(f"📂 <b>POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl    = float(p.get("pnl_pct") or 0)
            action = p.get("action_required") or "HOLD"
            ico    = "📈" if pnl >= 0 else "📉"
            ev     = "⚠️" if p.get("event_risk") else ""
            lines.append(
                f"  {ico}{ev} <b>{p['symbol']}</b> {pnl:+.1f}% "
                f"{days_held(p.get('entry_date'))} → <b>{action}</b>"
            )

    # Exits / Adds
    if exits or adds:
        lines.append("")
        if exits:
            lines.append("🔴 <b>EXIT:</b> " + " · ".join(s["symbol"] for s in exits))
        if adds:
            lines.append("🟢 <b>ADD:</b> " + " · ".join(s["symbol"] for s in adds))

    lines += ["", f"<i>Lessons 7d: {data['lessons_ai']} AI · {data['lessons_rb']} rule-based</i>"]
    return "\n".join(lines)


# ── STRUCTURED FORMAT ──────────────────────────────────────────────────────
# Full AI reasoning per stock — evening review

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

    # ── MARKET CONTEXT ──────────────────────────────────────────
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

    # ── BUY CANDIDATES ─────────────────────────────────────────
    lines.append(f"<b>🎯 BUY CANDIDATES ({len(buys)})</b>")

    if buys:
        for s in buys[:5]:
            conv      = s.get("ai_conviction") or "—"
            c_ico     = conviction_icon(conv)
            reason    = s.get("ai_conviction_reason") or ""
            risk1     = top_risk(s.get("ai_risks"))
            catalyst  = truncate(s.get("ai_catalyst") or "", 70)
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

            # Line 1 — signal identity
            lines.append(
                f"\n  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score: <b>{s.get('score',0):.0f}</b>{warn}{asm}"
            )
            # Line 2 — AI verdict + action
            lines.append(
                f"  {c_ico} <b>{conv}</b>{conf_str} → <b>{action}</b>"
                f"  <i>{provider}{fallback}</i>"
            )
            # Line 3 — Why (most critical for decision)
            if reason:
                lines.append(f"  💬 {(reason)}")
            # Line 4 — Top risk
            if risk1:
                lines.append(f"  ⚠️ Risk: {risk1}")
            # Line 5 — Catalyst if meaningful
            if catalyst and catalyst.lower() not in ("none", "no catalyst", "no immediate catalyst", ""):
                lines.append(f"  💡 Catalyst: {catalyst}")
            # Line 6 — Conflicts (critical — shows AI disagrees with rule engine)
            if conflicts and conflicts.upper() not in ("NONE", ""):
                lines.append(f"  ⚡ Conflict: {(conflicts)}")
            # Line 7 — Industry context
            if ind_st or ind_rk:
                lines.append(f"  🏭 Industry: {ind_st}  Rank #{ind_rk}")
    else:
        lines.append("  — no setups today")

    lines.append("")

    # ── OPEN POSITIONS ─────────────────────────────────────────
    if open_pos:
        lines.append(f"<b>📂 OPEN POSITIONS ({len(open_pos)})</b>")
        for p in open_pos:
            pnl    = float(p.get("pnl_pct") or 0)
            cp     = float(p.get("current_price") or 0)
            sl     = float(p.get("active_sl") or 0)
            action = p.get("action_required") or "HOLD"
            ev     = p.get("event_risk") or ""
            ico    = "📈" if pnl >= 0 else "📉"

            lines.append(
                f"\n  {ico} <b>{p['symbol']}</b> [{p.get('strategy','?')}]  "
                f"P&L: <b>{pnl:+.1f}%</b>  Held: {days_held(p.get('entry_date'))}"
            )
            lines.append(
                f"  → Action: <b>{action}</b>"
                + (f"  SL: ₹{sl:.0f}" if sl else "")
                + (f"  CMP: ₹{cp:.0f}" if cp else "")
            )
            if ev:
                lines.append(f"  ⚠️ Event risk: {ev}")
        lines.append("")

    # ── EXIT SIGNALS ───────────────────────────────────────────
    if exits:
        lines.append(f"<b>🔴 EXIT SIGNALS ({len(exits)})</b>")
        for s in exits:
            c_ico  = conviction_icon(s.get("ai_conviction"))
            reason = truncate(s.get("ai_conviction_reason") or "", 70)
            lines.append(
                f"  {c_ico} <b>{s['symbol']}</b> [{s.get('strategy','?')}]"
                + (f"\n  💬 {reason}" if reason else "")
            )
        lines.append("")

    # ── ADD SIGNALS ────────────────────────────────────────────
    if adds:
        lines.append(f"<b>🟢 ADD TO POSITION ({len(adds)})</b>")
        for s in adds:
            lines.append(
                f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
                f"Score: {s.get('score',0):.0f}"
            )
        lines.append("")

    # ── FOOTER ────────────────────────────────────────────────
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

    from config import get_supabase
    sb    = get_supabase()
    today = str(today_ist())
    data  = load_data(sb, today)

    msg = build_compact(data) if MESSAGE_STYLE == "compact" else build_structured(data)

    success = send_message(msg)

    buys  = len([s for s in data["signals"] if s["signal_type"] == "BUY_CANDIDATE"])
    exits = len([s for s in data["signals"] if s["signal_type"] == "EXIT"])
    adds  = len([s for s in data["signals"] if s["signal_type"] == "ADD"])

    if success:
        logger.success(
            f"Telegram ({MESSAGE_STYLE}) sent: {buys} buys · {exits} exits · "
            f"{adds} adds · {len(data['open_pos'])} open positions"
        )
    return {"sent": success, "style": MESSAGE_STYLE, "buys": buys, "exits": exits}


if __name__ == "__main__":
    main()