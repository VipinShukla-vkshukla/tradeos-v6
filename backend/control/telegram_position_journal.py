"""
TradeOS v6 — Telegram Position Journal
=======================================
Manual/discretionary fallback for recording entries and exits, for trades made
outside the framework or before the Kite reconciler next runs.

REPLACES brain/position_manager.py, which was non-functional:
  - inserted columns that do not exist on open_positions (id,
    entry_price_actual, stop_loss, regime_at_entry, telegram_msg_id, quantity)
    and on closed_positions (open_position_id, quantity)
  - wrote status='OPEN'/'CLOSED' while every reader in the codebase filters
    status='ACTIVE'
  - read signal_log.current_price, which was not a column and was never written
    by generate_signals, so entry_price resolved to 0
  - documented a FastAPI webhook at /telegram/callback that exists nowhere in
    the repo, so every inline button it attached was inert

WHY POLLING, NOT A WEBHOOK
  A webhook needs a public HTTPS endpoint running continuously. This system has
  no server — it is cron-job.org triggering GitHub Actions, which are ephemeral.
  Telegram's getUpdates long-poll needs no hosting: each scheduled run drains
  everything since the last processed update_id and exits. The offset is kept in
  system_config so no update is processed twice.

PRECEDENCE
  Kite is authoritative. Rows created here are marked source='telegram' and are
  overwritten by reconcile_with_broker when the broker disagrees on quantity or
  average price. This journal exists so a position is never invisible, not to
  compete with the broker record.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, today_ist, IST, DRY_RUN,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
)

OFFSET_KEY = "telegram_update_offset"


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM TRANSPORT
# ─────────────────────────────────────────────────────────────────────────────

def _tg_api(method: str, data: dict | None = None, timeout: int = 15) -> dict:
    if not TELEGRAM_TOKEN:
        return {}
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        if data:
            req = urllib.request.Request(
                url, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning(f"  Telegram {method} failed: {e}")
        return {}


def _get_offset(sb) -> int:
    try:
        rows = sb.table("system_config").select("value").eq("key", OFFSET_KEY).execute().data
        return int(rows[0]["value"]) if rows and rows[0].get("value") else 0
    except Exception:
        return 0


def _set_offset(sb, offset: int) -> None:
    if DRY_RUN:
        return
    try:
        existing = sb.table("system_config").select("key").eq("key", OFFSET_KEY).execute().data
        if existing:
            sb.table("system_config").update({"value": str(offset)}).eq("key", OFFSET_KEY).execute()
        else:
            sb.table("system_config").insert({
                "key": OFFSET_KEY, "value": str(offset),
                "description": "Highest processed Telegram update_id — prevents reprocessing",
            }).execute()
    except Exception as e:
        logger.warning(f"  Could not persist Telegram offset: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────────────────────────────────────
# Telegram caps callback_data at 64 BYTES. The old implementation packed a full
# JSON object with signal_id, symbol and a float price into it, which silently
# exceeded the cap for most symbols and produced buttons Telegram rejected.
# Compact pipe-delimited form instead.

def build_signal_keyboard(signal_id: int, symbol: str, price: float) -> dict:
    return {"inline_keyboard": [[
        {"text": f"✅ Bought @ ₹{price:.0f}", "callback_data": f"e|{signal_id}|{symbol}"},
        {"text": "❌ Skip",                   "callback_data": f"s|{signal_id}"},
    ]]}


def build_position_keyboard(symbol: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "🔴 Exited",  "callback_data": f"x|{symbol}"},
        {"text": "✂️ Partial", "callback_data": f"p|{symbol}"},
    ]]}


# ─────────────────────────────────────────────────────────────────────────────
# ACTIONS
# ─────────────────────────────────────────────────────────────────────────────

def record_entry(sb, symbol: str, signal_id: int | None,
                 price: float, qty: int) -> bool:
    """
    Record a manual entry. Schema-correct, unlike the module this replaces,
    and it reuses position_lifecycle's attribution + trade-plan logic so a
    Telegram-recorded position carries the same stop, target and signal link
    as a Kite-reconciled one.
    """
    from control.position_lifecycle import find_originating_signal
    from analysis.risk_model import compute_trade_levels

    trade_date = str(today_ist())
    sig = {}
    if signal_id:
        rows = sb.table("signal_log").select("*").eq("id", signal_id).execute().data
        sig = rows[0] if rows else {}
    if not sig:
        sig = find_originating_signal(sb, symbol, trade_date)

    planned_stop   = sig.get("planned_stop")
    planned_target = sig.get("planned_target")
    if not planned_stop:
        sd = (sb.table("stock_data_daily").select("atr_14,supertrend")
                .eq("symbol", symbol).order("date", desc=True).limit(1).execute().data)
        atr = float(sd[0].get("atr_14") or 0) if sd else 0
        if atr > 0:
            lv = compute_trade_levels(
                entry_price=price, atr_abs=atr, anchor_price=price,
                structure_stop=float(sd[0].get("supertrend") or 0) or None,
                regime=sig.get("regime") or "NEUTRAL")
            if lv.valid:
                planned_stop, planned_target = lv.stop, lv.target

    row = {
        "symbol":          symbol,
        "company_name":    sig.get("company_name"),
        "sector":          sig.get("sector"),
        "strategy":        sig.get("strategy") or sig.get("signal_type"),
        "entry_date":      trade_date,
        "entry_price":     price,
        "actual_qty":      qty,
        "original_qty":    qty,
        "current_qty":     qty,
        "invested_value":  round(price * qty, 2),
        "current_price":   price,
        "high_water_mark": price,
        "status":          "ACTIVE",          # matches what every reader filters on
        "source":          "telegram",
        "planned_stop":    planned_stop,
        "planned_target":  planned_target,
        "active_sl":       planned_stop,
        "target_price":    planned_target,
        "expected_r_at_entry":   sig.get("expected_r_msl"),
        "signal_id":       sig.get("id"),
        "signal_date":     sig.get("date"),
        "signal_subtype":  sig.get("signal_subtype"),
        "entry_signal_type": sig.get("signal_type"),
        "entry_timing_type": sig.get("entry_timing_type"),
        "lifecycle_at_entry": sig.get("lifecycle"),
        "regime_at_entry": sig.get("regime"),
        "sector_rank_at_entry": sig.get("sector_rank_at_entry"),
        "max_favorable_excursion": 0.0,
        "max_adverse_excursion":   0.0,
        "reconcile_status": "TELEGRAM_PENDING",
        "synced_at":       datetime.now(IST).isoformat(),
    }
    if DRY_RUN:
        logger.info(f"[DRY RUN] would record entry {symbol} {qty}@{price}")
        return True
    try:
        sb.table("open_positions").upsert(row, on_conflict="symbol").execute()
        logger.success(f"  Entry recorded: {symbol} {qty} @ Rs.{price:.2f} "
                       f"(sl={planned_stop} tgt={planned_target})")
        return True
    except Exception as e:
        logger.error(f"  Entry record failed for {symbol}: {e}")
        return False


def record_exit(sb, symbol: str, price: float, reason: str = "MANUAL_EXIT") -> bool:
    from control.position_lifecycle import close_position
    rows = (sb.table("open_positions").select("*")
              .eq("symbol", symbol).eq("status", "ACTIVE").execute().data)
    if not rows:
        logger.warning(f"  No active position for {symbol}")
        return False
    return close_position(sb, rows[0], price, reason,
                          "recorded via Telegram", str(today_ist()), source="telegram")


# ─────────────────────────────────────────────────────────────────────────────
# UPDATE PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

_PENDING_KEY = "telegram_pending_action"


def _set_pending(sb, payload: dict | None):
    val = json.dumps(payload) if payload else ""
    if DRY_RUN:
        return
    try:
        existing = sb.table("system_config").select("key").eq("key", _PENDING_KEY).execute().data
        if existing:
            sb.table("system_config").update({"value": val}).eq("key", _PENDING_KEY).execute()
        else:
            sb.table("system_config").insert({
                "key": _PENDING_KEY, "value": val,
                "description": "Awaiting a numeric reply (price/qty) for a Telegram action",
            }).execute()
    except Exception as e:
        logger.warning(f"  pending-action write failed: {e}")


def _get_pending(sb) -> dict | None:
    try:
        rows = (sb.table("system_config").select("value")
                  .eq("key", _PENDING_KEY).execute().data)
        if rows and rows[0].get("value"):
            return json.loads(rows[0]["value"])
    except Exception:
        pass
    return None


def _handle_callback(sb, cq: dict) -> str:
    data = cq.get("data", "")
    parts = data.split("|")
    kind = parts[0] if parts else ""

    if kind == "s":
        return "Skipped."

    if kind == "e" and len(parts) >= 3:
        signal_id, symbol = parts[1], parts[2]
        _set_pending(sb, {"action": "entry", "symbol": symbol, "signal_id": signal_id})
        _tg_api("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": (f"📝 <b>{symbol}</b> — reply with <code>price qty</code>\n"
                     f"e.g. <code>1450.50 20</code>"),
            "parse_mode": "HTML",
        })
        return f"Enter fill for {symbol}"

    if kind == "x" and len(parts) >= 2:
        symbol = parts[1]
        _set_pending(sb, {"action": "exit", "symbol": symbol})
        _tg_api("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"🔴 <b>{symbol}</b> — reply with the exit price",
            "parse_mode": "HTML",
        })
        return f"Enter exit price for {symbol}"

    return "OK"


def _handle_text(sb, text: str) -> str | None:
    pending = _get_pending(sb)
    if not pending:
        return None

    nums = []
    for tok in text.replace(",", " ").replace("₹", " ").split():
        try:
            nums.append(float(tok))
        except ValueError:
            pass
    if not nums:
        return None

    action, symbol = pending.get("action"), pending.get("symbol")
    if action == "entry":
        price = nums[0]
        qty   = int(nums[1]) if len(nums) > 1 else 0
        if qty <= 0:
            return "Need both price and quantity, e.g. `1450.50 20`"
        ok = record_entry(sb, symbol, pending.get("signal_id"), price, qty)
        _set_pending(sb, None)
        return (f"✅ Recorded {symbol}: {qty} @ ₹{price:.2f}" if ok
                else f"❌ Could not record {symbol}")

    if action == "exit":
        ok = record_exit(sb, symbol, nums[0])
        _set_pending(sb, None)
        return (f"✅ Closed {symbol} @ ₹{nums[0]:.2f}" if ok
                else f"❌ No active position for {symbol}")
    return None


def poll_and_process() -> dict:
    """
    Drain every Telegram update since the last processed one.

    Safe to call from any scheduled job. Idempotent via the persisted offset:
    a run that crashes mid-batch reprocesses only the unacknowledged tail.
    """
    if not TELEGRAM_TOKEN:
        return {"status": "no_token", "processed": 0}

    sb     = get_supabase()
    offset = _get_offset(sb)

    resp = _tg_api("getUpdates", {"offset": offset + 1, "timeout": 0, "limit": 100})
    updates = resp.get("result", []) or []
    if not updates:
        return {"status": "ok", "processed": 0}

    processed, max_id = 0, offset
    for u in updates:
        max_id = max(max_id, int(u.get("update_id", 0)))
        try:
            if "callback_query" in u:
                cq    = u["callback_query"]
                reply = _handle_callback(sb, cq)
                _tg_api("answerCallbackQuery",
                        {"callback_query_id": cq["id"], "text": reply})
                processed += 1
            elif "message" in u and u["message"].get("text"):
                reply = _handle_text(sb, u["message"]["text"].strip())
                if reply:
                    _tg_api("sendMessage", {
                        "chat_id": u["message"]["chat"]["id"],
                        "text": reply, "parse_mode": "Markdown",
                    })
                    processed += 1
        except Exception as e:
            logger.warning(f"  Update {u.get('update_id')} failed: {e}")

    _set_offset(sb, max_id)
    logger.info(f"  Telegram journal: {processed} action(s) from {len(updates)} update(s)")
    return {"status": "ok", "processed": processed, "offset": max_id}


def main():
    return poll_and_process()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v6 — Telegram position journal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"
    print(main())
