"""
TradeOS v6 — Telegram Alerts (Phase 1)
Sends daily signal summary via Telegram
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, cfg_bool, today_ist


def send_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


def main():
    if not cfg_bool("telegram_alerts_enabled", False):
        logger.info("Telegram alerts disabled — skipping")
        return {}

    from config import get_supabase
    sb    = get_supabase()
    today = str(today_ist())

    signals = sb.table("signal_log").select("*").eq("date", today).execute().data
    regime  = sb.table("market_regime").select("regime,nifty_price,india_vix").eq("date", today).execute().data
    regime_row = regime[0] if regime else {}

    buys   = [s for s in signals if s["signal_type"] == "BUY_CANDIDATE"]
    exits  = [s for s in signals if s["signal_type"] == "EXIT"]
    adds   = [s for s in signals if s["signal_type"] == "ADD"]

    lines = [
        f"<b>TradeOS v6 — {today}</b>",
        f"Regime: <b>{regime_row.get('regime','?')}</b> | Nifty: {regime_row.get('nifty_price','?')} | VIX: {regime_row.get('india_vix','?')}",
        "",
        f"🟡 <b>BUY CANDIDATES ({len(buys)})</b>",
    ]
    for s in sorted(buys, key=lambda x: x.get("score", 0), reverse=True)[:5]:
        warn = " ⚠️RISK OFF" if s.get("regime_warning") else ""
        lines.append(f"  {s['symbol']} [{s['strategy']}] Score:{s['score']:.0f}{warn}")

    if exits:
        lines.extend(["", f"🔴 <b>EXIT SIGNALS ({len(exits)})</b>"])
        for s in exits:
            lines.append(f"  {s['symbol']} [{s['strategy']}]")

    if adds:
        lines.extend(["", f"🟢 <b>ADD SIGNALS ({len(adds)})</b>"])
        for s in adds:
            lines.append(f"  {s['symbol']} [{s['strategy']}]")

    msg = "\n".join(lines)
    send_message(msg)
    logger.info(f"✓ Telegram alert sent: {len(buys)} buys, {len(exits)} exits, {len(adds)} adds")
    return {"sent": True}


if __name__ == "__main__":
    main()
