"""
TradeOS v6 — Position Manager
================================
Manages open/closed positions via Telegram inline buttons and dashboard.
Integrates into your existing send_alerts.py.

FLOW:
  1. generate_signals.py creates a signal in signal_log
  2. send_alerts.py calls add_position_buttons() to attach inline keyboard
  3. User taps [✅ Entered at ₹X] on Telegram
  4. Telegram webhook → /telegram/callback endpoint → creates open_position row
  5. User can update actual price if it differs from signal price
  6. User taps [🔴 Exit] → creates closed_position row with actual P&L

PRICE UPDATE:
  Signal price comes from signal_log.current_price (close at signal date).
  Actual entry price often differs (next day open, limit order fill, etc.).
  Telegram: after [✅ Entered], bot asks "Actual entry price? (Reply or skip)"
  Dashboard: editable price field in open_positions table.
  Both update entry_price_actual. Brain uses entry_price_actual for P&L when available.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")


def _tg_api(method: str, data: dict) -> dict:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    body = json.dumps(data).encode()
    req  = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        logger.warning(f"  Telegram API error ({method}): {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# INLINE KEYBOARD BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_signal_keyboard(signal_id: int, symbol: str,
                           signal_price: float) -> dict:
    """
    Build Telegram inline keyboard for a signal alert.
    Attach to signal alert messages in send_alerts.py.
    """
    return {
        "inline_keyboard": [[
            {
                "text":          f"✅ Entered at ₹{signal_price:.0f}",
                "callback_data": json.dumps({
                    "action":       "enter",
                    "signal_id":    signal_id,
                    "symbol":       symbol,
                    "signal_price": signal_price,
                }),
            },
            {
                "text":          "❌ Skip",
                "callback_data": json.dumps({
                    "action":    "skip",
                    "signal_id": signal_id,
                }),
            },
        ], [
            {
                "text":          "👁 Watching",
                "callback_data": json.dumps({
                    "action":    "watch",
                    "signal_id": signal_id,
                    "symbol":    symbol,
                }),
            },
            {
                "text":          "💰 Update Price",
                "callback_data": json.dumps({
                    "action":    "update_price",
                    "signal_id": signal_id,
                    "symbol":    symbol,
                }),
            },
        ]]
    }


def build_open_position_keyboard(position_id: int, symbol: str) -> dict:
    """
    Build inline keyboard for open position alerts.
    Shown in morning digest for each open position.
    """
    return {
        "inline_keyboard": [[
            {
                "text":          "🔴 Exit",
                "callback_data": json.dumps({
                    "action":      "exit",
                    "position_id": position_id,
                    "symbol":      symbol,
                }),
            },
            {
                "text":          "💰 Update Price",
                "callback_data": json.dumps({
                    "action":      "update_price",
                    "position_id": position_id,
                    "symbol":      symbol,
                }),
            },
        ], [
            {
                "text":          "✂️ Partial Exit",
                "callback_data": json.dumps({
                    "action":      "partial_exit",
                    "position_id": position_id,
                    "symbol":      symbol,
                }),
            },
            {
                "text":          "📝 Add Note",
                "callback_data": json.dumps({
                    "action":      "add_note",
                    "position_id": position_id,
                }),
            },
        ]]
    }


# ─────────────────────────────────────────────────────────────────────────────
# POSITION OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────

def open_position(signal_id: int, symbol: str, entry_price: float,
                  actual_price: float = None, telegram_msg_id: str = None) -> Optional[int]:
    """
    Create an open_position row from a signal.
    Returns the new position ID.
    """
    sb = get_supabase()

    # Load signal details
    rows = (sb.table("signal_log")
              .select("*")
              .eq("id", signal_id)
              .execute().data)
    if not rows:
        logger.error(f"Signal {signal_id} not found")
        return None

    sig = rows[0]

    row = {
        "entry_date":          str(date.today()),
        "symbol":              symbol,
        "company_name":        sig.get("company_name"),
        "sector":              sig.get("sector"),
        "strategy":            sig.get("signal_type") or sig.get("signal_subtype"),
        "signal_id":           signal_id,
        "entry_price":         float(sig.get("current_price") or entry_price),
        "entry_price_actual":  float(actual_price) if actual_price else None,
        "stop_loss":           _compute_stop(sig),
        "target_price":        _compute_target(sig),
        "regime_at_entry":     sig.get("regime") or sig.get("active_regime"),
        "ai_conviction":       sig.get("ai_conviction"),
        "telegram_msg_id":     telegram_msg_id,
        "status":              "OPEN",
    }

    result = sb.table("open_positions").insert(row).execute()
    if not result.data:
        return None

    pos_id = result.data[0]["id"]
    logger.info(f"  Opened position {pos_id}: {symbol} @ ₹{entry_price}")
    return pos_id


def update_entry_price(position_id: int = None, signal_id: int = None,
                        actual_price: float = None) -> bool:
    """
    Update the actual entry price for an open position.
    Called when user reports actual fill price differs from signal price.
    Works by position_id (from open_positions) or signal_id.
    """
    if actual_price is None:
        return False
    sb = get_supabase()

    if position_id:
        sb.table("open_positions").update({
            "entry_price_actual": actual_price,
            "notes":              f"Price updated to ₹{actual_price:.2f} via Telegram",
        }).eq("id", position_id).execute()
        logger.info(f"  Updated position {position_id} actual price: ₹{actual_price}")
        return True

    if signal_id:
        rows = (sb.table("open_positions")
                  .select("id")
                  .eq("signal_id", signal_id)
                  .eq("status", "OPEN")
                  .execute().data)
        if rows:
            sb.table("open_positions").update({
                "entry_price_actual": actual_price,
            }).eq("id", rows[0]["id"]).execute()
            logger.info(f"  Updated signal {signal_id} position actual price: ₹{actual_price}")
            return True
    return False


def close_position(position_id: int, exit_price: float,
                   exit_reason: str = "MANUAL",
                   quantity: int = None) -> Optional[int]:
    """
    Close an open position → creates closed_position row with auto-computed P&L.
    P&L uses entry_price_actual if available, otherwise entry_price.
    """
    sb = get_supabase()

    rows = (sb.table("open_positions")
              .select("*")
              .eq("id", position_id)
              .execute().data)
    if not rows:
        logger.error(f"Position {position_id} not found")
        return None

    pos          = rows[0]
    actual_entry = pos.get("entry_price_actual") or pos.get("entry_price")

    closed_row = {
        "open_position_id": position_id,
        "symbol":           pos["symbol"],
        "company_name":     pos.get("company_name"),
        "sector":           pos.get("sector"),
        "strategy":         pos.get("strategy"),
        "signal_id":        pos.get("signal_id"),
        "entry_date":       pos["entry_date"],
        "exit_date":        str(date.today()),
        "entry_price":      float(actual_entry),
        "exit_price":       float(exit_price),
        "quantity":         quantity or pos.get("quantity") or 1,
        "exit_reason":      exit_reason,
        "regime_at_entry":  pos.get("regime_at_entry"),
        "ai_conviction":    pos.get("ai_conviction"),
    }

    result = sb.table("closed_positions").insert(closed_row).execute()
    if not result.data:
        return None

    closed_id = result.data[0]["id"]

    # Update open_position status
    sb.table("open_positions").update({
        "status": "CLOSED",
    }).eq("id", position_id).execute()

    pnl = round(((exit_price - float(actual_entry)) / float(actual_entry)) * 100, 2)
    logger.info(f"  Closed position {position_id}: {pos['symbol']} P&L={pnl:+.1f}%")
    return closed_id


def _compute_stop(sig: dict) -> Optional[float]:
    """Estimate stop loss from signal data."""
    cp   = sig.get("current_price") or sig.get("kite_price")
    atr  = sig.get("atr_pct")
    sma50 = sig.get("sma_50") or sig.get("dist_sma50")
    if cp and atr:
        return round(float(cp) * (1 - float(atr) * 1.5 / 100), 2)
    return None


def _compute_target(sig: dict) -> Optional[float]:
    """Estimate target from signal data."""
    cp  = sig.get("current_price") or sig.get("kite_price")
    rr  = sig.get("expected_r_msl")
    atr = sig.get("atr_pct")
    if cp and rr:
        return round(float(cp) * (1 + float(rr) / 100), 2)
    if cp and atr:
        return round(float(cp) * (1 + float(atr) * 3 / 100), 2)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────────────────────

def handle_callback(callback_data_str: str, message_id: str = None,
                     chat_id: str = None) -> str:
    """
    Process a Telegram inline button callback.
    Returns reply text to send back to user.

    Integrate into your FastAPI webhook:
      @app.post("/telegram/callback")
      async def telegram_callback(update: dict):
          cq = update.get("callback_query", {})
          reply = handle_callback(
              cq.get("data",""),
              message_id=cq.get("message",{}).get("message_id"),
              chat_id=cq.get("message",{}).get("chat",{}).get("id"),
          )
          # answer the callback query
          _tg_api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": reply})
    """
    try:
        data = json.loads(callback_data_str)
    except Exception:
        return "Invalid callback data"

    action = data.get("action", "")

    if action == "enter":
        pos_id = open_position(
            signal_id=data["signal_id"],
            symbol=data["symbol"],
            entry_price=data["signal_price"],
            telegram_msg_id=str(message_id),
        )
        if pos_id:
            # Ask for actual price
            _tg_api("sendMessage", {
                "chat_id":    chat_id or TELEGRAM_CHAT_ID,
                "text":       (
                    f"✅ Position opened: *{data['symbol']}*\n"
                    f"Signal price: ₹{data['signal_price']:.0f}\n\n"
                    f"Reply with your actual fill price (or type 'skip'):"
                ),
                "parse_mode": "Markdown",
                "reply_markup": json.dumps({"inline_keyboard": [[
                    {"text": "Skip (use signal price)", "callback_data": json.dumps({
                        "action": "skip_price", "position_id": pos_id
                    })}
                ]]}),
            })
            return f"Entered {data['symbol']}"
        return "Failed to create position"

    elif action == "update_price":
        # Prompt for price input
        target = data.get("position_id") or data.get("signal_id")
        _tg_api("sendMessage", {
            "chat_id":  chat_id or TELEGRAM_CHAT_ID,
            "text":     f"💰 Enter actual price for *{data['symbol']}*:",
            "parse_mode": "Markdown",
        })
        # In production: store pending_price_update in Redis/DB keyed by chat_id
        # then handle next text message as the price input
        return "Enter actual price in next message"

    elif action == "exit":
        # Prompt for exit price
        _tg_api("sendMessage", {
            "chat_id":  chat_id or TELEGRAM_CHAT_ID,
            "text":     f"🔴 Enter exit price for *{data['symbol']}*:",
            "parse_mode": "Markdown",
        })
        return "Enter exit price"

    elif action == "watch":
        return f"Added {data['symbol']} to watchlist"

    elif action == "skip":
        return "Signal skipped"

    return "Action processed"


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION PATCH FOR send_alerts.py
# ─────────────────────────────────────────────────────────────────────────────
"""
ADD TO YOUR send_alerts.py — in the function that sends individual signal alerts:

from position_manager import build_signal_keyboard

# After building your signal message text:
keyboard = build_signal_keyboard(
    signal_id=signal["id"],
    symbol=signal["symbol"],
    signal_price=float(signal.get("current_price") or 0),
)

# When sending the Telegram message, add reply_markup:
_tg_api("sendMessage", {
    "chat_id":      TELEGRAM_CHAT_ID,
    "text":         your_message_text,
    "parse_mode":   "Markdown",
    "reply_markup": json.dumps(keyboard),
})

ADD TO YOUR FastAPI app (or create a simple webhook handler):

@app.post("/telegram/webhook")
async def telegram_webhook(update: dict):
    # Handle button callbacks
    if "callback_query" in update:
        cq     = update["callback_query"]
        reply  = handle_callback(
            cq.get("data",""),
            message_id=str(cq.get("message",{}).get("message_id","")),
            chat_id=str(cq.get("message",{}).get("chat",{}).get("id","")),
        )
        _tg_api("answerCallbackQuery", {
            "callback_query_id": cq["id"],
            "text": reply,
        })

    # Handle text replies (for price input)
    elif "message" in update:
        msg  = update["message"]
        text = msg.get("text","").strip()
        # Check if this is a price reply (numeric)
        try:
            price = float(text.replace("₹","").replace(",",""))
            # Look up pending price update for this chat
            # (implement with Redis or a pending_updates DB table)
            # update_entry_price(position_id=pending_id, actual_price=price)
        except ValueError:
            pass

    return {"ok": True}
"""
