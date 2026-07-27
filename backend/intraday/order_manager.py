"""
Phase 3 — automatic order placement.

SHIPPED OFF BY DEFAULT, AND THAT IS NOT A FORMALITY
---------------------------------------------------
orders_enabled() requires the phase to be >= 3.0 AND an explicit boolean AND
DRY_RUN to be false. Three switches because each fails differently: the phase is
a deliberate promotion, the boolean is a fast off-switch that does not require
re-reasoning about phases, and DRY_RUN keeps the idiom used everywhere else in
this codebase working here, where it matters most.

WHAT MAKES THIS SAFE ENOUGH TO EXIST
------------------------------------
Every order passes preflight() before it is sent. Preflight is deliberately
paranoid and checks things that "cannot" be wrong, because the cost of being
wrong is real money and the cost of a redundant check is microseconds:

  · kill switch, phase, DRY_RUN
  · market actually open (not a stale clock, not a holiday)
  · per-order rupee cap, independent of what sizing computed
  · daily order count and daily notional caps
  · SELL never exceeds the quantity actually held at the broker
  · BUY never exceeds available cash
  · no duplicate order for the same symbol and side within a cooldown

The caps are the important part. Sizing bugs are the failure mode that empties
an account, and every sizing input here — capital, risk percent, ATR — has been
wrong at some point in this project's history. A hard rupee ceiling is the one
control that does not depend on any of them being right.

EXITS ARE PREFERRED OVER ENTRIES
--------------------------------
If the daily order budget is nearly spent, an exit still goes through while an
entry does not. Being unable to reduce risk is a strictly worse failure than
being unable to add it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, is_kill_switch_active, DRY_RUN
from intraday.config import (orders_enabled, is_market_open, max_order_value,
                             max_orders_per_day, max_notional_per_day)


@dataclass
class OrderRequest:
    symbol: str
    side: str            # BUY | SELL
    quantity: int
    order_type: str = "LIMIT"     # LIMIT | MARKET
    price: float | None = None
    reason: str = ""
    signal_id: int | None = None


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    message: str
    blocked_by: str | None = None


_recent: dict[str, datetime] = {}


def _today_totals(sb) -> tuple[int, float]:
    """Orders placed and notional committed today — the daily budget spent."""
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (sb.table("intraday_broker_log").select("price,quantity,action")
                  .eq("channel", "ORDER").gte("ts", start.isoformat())
                  .execute().data or [])
        placed = [r for r in rows if r.get("action") == "PLACED"]
        notional = sum(float(r.get("price") or 0) * int(r.get("quantity") or 0) for r in placed)
        return len(placed), notional
    except Exception as e:
        # Unknown spend must not read as zero spend. Report the budget as fully
        # consumed so preflight blocks rather than permits.
        logger.warning(f"  order: could not read today's totals — {e}")
        return max_orders_per_day(), max_notional_per_day()


def preflight(req: OrderRequest, sb=None) -> OrderResult:
    """Every reason this order must not be sent. Cheap, and checked in order of severity."""
    sb = sb or get_supabase()

    if is_kill_switch_active():
        return OrderResult(False, None, "master kill switch is active", "KILL_SWITCH")
    if DRY_RUN:
        return OrderResult(False, None, "DRY_RUN is set", "DRY_RUN")
    if not orders_enabled():
        return OrderResult(False, None,
                           "Phase 3 order placement is not enabled "
                           "(intraday_autonomy_phase >= 3.0 AND intraday_orders_enabled)",
                           "PHASE")
    if not is_market_open():
        return OrderResult(False, None, "market is closed", "MARKET_CLOSED")
    if req.quantity <= 0:
        return OrderResult(False, None, f"quantity {req.quantity} is not positive", "QUANTITY")

    px = req.price
    if px is None:
        try:
            from kite import kite_client
            px = (kite_client.fetch_ltp([req.symbol]) or {}).get(req.symbol)
        except Exception:
            px = None
    if not px:
        return OrderResult(False, None, "no price available to value the order", "NO_PRICE")

    value = px * req.quantity
    if value > max_order_value():
        return OrderResult(False, None,
                           f"order value ₹{value:,.0f} exceeds the ₹{max_order_value():,.0f} per-order cap",
                           "ORDER_CAP")

    n_today, notional_today = _today_totals(sb)
    # Exits are allowed to consume the last of the budget; entries are not.
    if req.side == "BUY":
        if n_today >= max_orders_per_day():
            return OrderResult(False, None,
                               f"{n_today} orders already placed today (cap {max_orders_per_day()})",
                               "DAILY_COUNT")
        if notional_today + value > max_notional_per_day():
            return OrderResult(False, None,
                               f"₹{notional_today:,.0f} committed today; this order would breach "
                               f"the ₹{max_notional_per_day():,.0f} daily notional cap",
                               "DAILY_NOTIONAL")

    # Broker-truth checks.
    try:
        from kite import kite_client
        if req.side == "SELL":
            holdings = {h["symbol"]: h for h in (kite_client.fetch_holdings() or [])}
            h = holdings.get(req.symbol)
            held = (int(h.get("quantity") or 0) + int(h.get("t1_quantity") or 0)) if h else 0
            if req.quantity > held:
                return OrderResult(False, None,
                                   f"cannot sell {req.quantity} — broker shows {held} held",
                                   "INSUFFICIENT_HOLDING")
        else:
            cash = float((kite_client.fetch_margins() or {}).get("available_cash") or 0)
            if value > cash:
                return OrderResult(False, None,
                                   f"order value ₹{value:,.0f} exceeds available cash ₹{cash:,.0f}",
                                   "INSUFFICIENT_CASH")
    except Exception as e:
        return OrderResult(False, None, f"broker state unavailable: {e}", "BROKER_UNAVAILABLE")

    # Duplicate guard — a loop that re-decides every 15s must not be able to
    # send the same order repeatedly while the first is still being filled.
    key = f"{req.symbol}:{req.side}"
    last = _recent.get(key)
    if last and datetime.now(IST) - last < timedelta(minutes=5):
        return OrderResult(False, None,
                           f"an identical {req.side} for {req.symbol} was placed "
                           f"{(datetime.now(IST) - last).seconds}s ago",
                           "DUPLICATE")

    return OrderResult(True, None, f"cleared: {req.side} {req.quantity} {req.symbol} @ ~₹{px:,.2f}")


def place(req: OrderRequest, sb=None, notifier=None) -> OrderResult:
    """Preflight, then send. Logged either way."""
    sb = sb or get_supabase()
    pre = preflight(req, sb)
    if not pre.ok:
        logger.warning(f"  order BLOCKED [{pre.blocked_by}] {req.side} {req.quantity} "
                       f"{req.symbol}: {pre.message}")
        _log(sb, req, "BLOCKED", None, pre.message)
        return pre

    try:
        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            return OrderResult(False, None, "no broker session", "BROKER_UNAVAILABLE")

        params = {
            "variety":          kite.VARIETY_REGULAR,
            "exchange":         "NSE",
            "tradingsymbol":    req.symbol,
            "transaction_type": (kite.TRANSACTION_TYPE_BUY if req.side == "BUY"
                                 else kite.TRANSACTION_TYPE_SELL),
            "quantity":         int(req.quantity),
            "product":          kite.PRODUCT_CNC,
            "order_type":       (kite.ORDER_TYPE_LIMIT if req.order_type == "LIMIT"
                                 else kite.ORDER_TYPE_MARKET),
            "tag":              "tradeos",
        }
        if req.order_type == "LIMIT":
            params["price"] = round(float(req.price), 1)

        order_id = kite.place_order(**params)
        _recent[f"{req.symbol}:{req.side}"] = datetime.now(IST)
        logger.success(f"  order PLACED {req.side} {req.quantity} {req.symbol} (id {order_id})")
        _log(sb, req, "PLACED", str(order_id), req.reason)

        if notifier:
            from intraday.notifier import Action
            notifier.send(Action(
                req.symbol, f"ORDER_{req.side}",
                f"{req.side} {req.quantity} @ ₹{req.price:,.2f}" if req.price
                else f"{req.side} {req.quantity} at market",
                f"{req.reason}\nOrder id {order_id}. Verify in Kite.",
                ltp=req.price, urgency="CRITICAL"), force=True)

        return OrderResult(True, str(order_id), "placed")

    except Exception as e:
        logger.error(f"  order FAILED {req.side} {req.quantity} {req.symbol}: {e}")
        _log(sb, req, "FAILED", None, str(e)[:300])
        return OrderResult(False, None, str(e), "BROKER_ERROR")


def _log(sb, req: OrderRequest, action: str, order_id: str | None, detail: str) -> None:
    try:
        sb.table("intraday_broker_log").insert({
            "ts":       datetime.now(IST).isoformat(),
            "symbol":   req.symbol,
            "channel":  "ORDER",
            "action":   action,
            "side":     req.side,
            "ref_id":   order_id,
            "price":    req.price,
            "quantity": req.quantity,
            "detail":   detail,
        }).execute()
    except Exception as e:
        logger.debug(f"  order log failed: {e}")
