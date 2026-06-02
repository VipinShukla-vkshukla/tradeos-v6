"""
TradeOS v6 — Phase 3: Telegram Approval Bot
Sends signal alerts and waits for your approval before executing orders.
Commands: /approve <signal_id>, /reject <signal_id>, /status, /kill
"""
import sys
import json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, get_supabase, today_ist, logger

import requests

def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message to your Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured — skipping message")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": parse_mode,
        }, timeout=10)
        return resp.ok
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


def send_signal_alert(signal: dict) -> bool:
    """Format and send a BUY_CANDIDATE signal for approval."""
    regime_icon = "⚠️" if signal.get("regime_warning") else "✅"
    conviction_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(
        signal.get("ai_conviction", ""), "⬜"
    )
    eap_icon = {"PRIORITISE": "🚀", "AVOID_ENTRY": "🚫", "NO_CHANGE": ""}.get(
        signal.get("eap_action", ""), ""
    )

    msg = (
        f"<b>📊 TRADE SIGNAL — {signal.get('symbol')}</b>\n"
        f"{'─' * 30}\n"
        f"<b>Strategy:</b> {signal.get('strategy', 'CTL')}\n"
        f"<b>Score:</b> {signal.get('score', 0):.1f}\n"
        f"<b>Sector:</b> {signal.get('sector', '')}\n"
        f"<b>Price:</b> ₹{signal.get('current_price', 0)}\n"
        f"<b>Entry Zone:</b> ₹{signal.get('entry_zone_low', 0)} – ₹{signal.get('entry_zone_high', 0)}\n"
        f"<b>Lifecycle:</b> {signal.get('lifecycle', '')}\n"
        f"<b>Regime:</b> {regime_icon} {signal.get('regime', '')}\n"
    )
    if signal.get("ai_conviction"):
        msg += (
            f"\n{conviction_icon} <b>AI Conviction:</b> {signal.get('ai_conviction')}\n"
            f"<i>{signal.get('ai_conviction_reason', '')}</i>\n"
        )
    if signal.get("eap_action") and signal.get("eap_action") != "NO_CHANGE":
        msg += f"\n{eap_icon} <b>EAP:</b> {signal.get('eap_action')}\n"
    if signal.get("risks"):
        risks = signal.get("risks", [])
        if isinstance(risks, list):
            msg += f"\n⚠️ <b>Risks:</b> {', '.join(risks[:2])}\n"

    signal_id = signal.get("id", "")
    msg += f"\n<code>/approve {signal_id}</code>  |  <code>/reject {signal_id}</code>"

    return send_message(msg)


def send_daily_summary(signals: list) -> bool:
    """Send end-of-day summary of all signals."""
    buy_candidates = [s for s in signals if s.get("signal_type") == "BUY_CANDIDATE"]
    exits          = [s for s in signals if s.get("signal_type") == "EXIT"]
    adds           = [s for s in signals if s.get("signal_type") == "ADD"]

    sb  = get_supabase()
    regime_row = sb.table("market_regime").select("regime,nifty_price,india_vix") \
        .order("date", desc=True).limit(1).execute().data
    regime = regime_row[0] if regime_row else {}

    regime_emoji = {"RISK ON": "🟢", "NEUTRAL": "🟡", "RISK OFF": "🔴"}.get(
        regime.get("regime", ""), "⬜"
    )

    msg = (
        f"<b>🤖 TradeOS v6 — Daily Summary</b>\n"
        f"{today_ist().strftime('%d %b %Y')}\n"
        f"{'─' * 30}\n"
        f"{regime_emoji} <b>Regime:</b> {regime.get('regime', '?')} | "
        f"Nifty: {regime.get('nifty_price', '?')} | VIX: {regime.get('india_vix', '?')}\n\n"
    )

    if buy_candidates:
        msg += f"🟢 <b>BUY CANDIDATES ({len(buy_candidates)})</b>\n"
        for s in buy_candidates[:5]:
            warn = " ⚠️" if s.get("regime_warning") else ""
            ai   = f" [{s.get('ai_conviction','?')}]" if s.get("ai_conviction") else ""
            msg += f"  • {s['symbol']} | Score:{s.get('score',0):.0f} | {s.get('strategy','')}{ai}{warn}\n"

    if exits:
        msg += f"\n🔴 <b>EXIT SIGNALS ({len(exits)})</b>\n"
        for s in exits[:3]:
            msg += f"  • {s['symbol']} | {s.get('exit_reason', '')}\n"

    if adds:
        msg += f"\n🔵 <b>ADD SIGNALS ({len(adds)})</b>\n"
        for s in adds[:3]:
            msg += f"  • {s['symbol']}\n"

    if not buy_candidates and not exits and not adds:
        msg += "No actionable signals today.\n"

    return send_message(msg)


def process_telegram_commands():
    """
    Poll Telegram for /approve or /reject commands.
    Called from run_pipeline.py in Phase 3.
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        resp = requests.get(url, timeout=10)
        updates = resp.json().get("result", [])

        sb = get_supabase()
        for update in updates:
            text = update.get("message", {}).get("text", "")
            chat_id = str(update.get("message", {}).get("chat", {}).get("id", ""))

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue   # ignore messages from other chats

            if text.startswith("/approve "):
                signal_id = text.split(" ", 1)[1].strip()
                _handle_approval(signal_id, sb)
            elif text.startswith("/reject "):
                signal_id = text.split(" ", 1)[1].strip()
                _handle_rejection(signal_id, sb)
            elif text == "/kill":
                from config import set_system_config
                set_system_config("master_kill_switch", "true")
                send_message("🔴 <b>KILL SWITCH ACTIVATED</b> — All pipeline activity halted.")
                logger.critical("KILL SWITCH activated via Telegram")
            elif text == "/status":
                _send_status(sb)

    except Exception as e:
        logger.warning(f"Telegram command processing failed: {e}")


def _handle_approval(signal_id: str, sb) -> None:
    """Execute an approved signal."""
    try:
        signals = sb.table("signal_log").select("*").eq("id", signal_id).execute().data
        if not signals:
            send_message(f"Signal {signal_id} not found.")
            return
        signal = signals[0]
        from control.execution_engine import place_order
        result = place_order(signal, approved_by="TELEGRAM")
        if result["success"]:
            send_message(
                f"✅ <b>ORDER PLACED</b>\n"
                f"{signal['symbol']} | Qty:{result['qty']} | ₹{result['invested']:,.0f}\n"
                f"Stop: ₹{result['stop']}"
            )
        else:
            send_message(f"❌ Order failed: {result['error']}")
    except Exception as e:
        send_message(f"❌ Approval processing error: {e}")


def _handle_rejection(signal_id: str, sb) -> None:
    sb.table("signal_log").update({"outcome": "REJECTED"}).eq("id", signal_id).execute()
    send_message(f"Signal {signal_id} rejected.")


def _send_status(sb) -> None:
    positions = sb.table("open_positions").select("symbol,pnl_pct,lifecycle").execute().data
    total_pnl = sum(float(p.get("pnl_pct") or 0) for p in positions)
    msg = f"<b>Portfolio Status</b>\n{len(positions)} positions | Avg P&L: {total_pnl/len(positions)*100:.1f}%\n"
    for p in positions[:8]:
        pnl = float(p.get("pnl_pct") or 0) * 100
        icon = "🟢" if pnl >= 0 else "🔴"
        msg += f"{icon} {p['symbol']} | {pnl:+.1f}% | {p.get('lifecycle','')}\n"
    send_message(msg)
