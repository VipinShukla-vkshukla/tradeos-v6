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
from execution.gates import (orders_enabled, is_market_open, max_order_value,
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
# symbol:side -> the configuration error that makes retrying pointless.
# Session-scoped: a restart re-tries, because that is when you would have fixed
# whatever was wrong at the broker.
_blocked: dict[str, str] = {}


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

    # A configuration rejection cannot be retried into success, so it is checked
    # BEFORE any broker call — an order that cannot be placed should not cost a
    # holdings fetch and a margins fetch every cycle to discover that again.
    key = f"{req.symbol}:{req.side}"
    if key in _blocked:
        return OrderResult(False, None,
                           f"blocked for this session — {_blocked[key][:140]}",
                           "BROKER_CONFIG")

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
    last = _recent.get(key)
    if last and datetime.now(IST) - last < timedelta(minutes=5):
        return OrderResult(False, None,
                           f"an identical {req.side} for {req.symbol} was placed "
                           f"{(datetime.now(IST) - last).seconds}s ago",
                           "DUPLICATE")

    return OrderResult(True, None, f"cleared: {req.side} {req.quantity} {req.symbol} @ ~₹{px:,.2f}")


def place(req: OrderRequest, sb=None, notifier=None,
          framework: str = "SWING") -> OrderResult:
    """
    Preflight, then send — to the broker or to the simulator. Logged either way.

    PAPER mode routes here too, and deliberately still runs the full preflight
    first. Simulating an order that the live rails would have refused would make
    paper results describe a system you are not allowed to run, which is worse
    than useless: it would build confidence in decisions that can never execute.
    """
    sb = sb or get_supabase()
    pre = preflight(req, sb)
    if not pre.ok:
        logger.warning(f"  order BLOCKED [{pre.blocked_by}] {req.side} {req.quantity} "
                       f"{req.symbol}: {pre.message}")
        _log(sb, req, "BLOCKED", None, pre.message)
        return pre

    # ── PAPER: simulate the fill, skip the broker ───────────────────────────
    from execution.gates import is_paper
    if is_paper(framework):
        from execution import paper_broker
        px = req.price
        if px is None:
            try:
                from kite import kite_client
                px = (kite_client.fetch_ltp([req.symbol]) or {}).get(req.symbol)
            except Exception:
                px = None
        if not px:
            return OrderResult(False, None, "no price to simulate against", "NO_PRICE")
        f = paper_broker.place(req.symbol, req.side, req.quantity, req.order_type,
                               req.price, float(px), framework, req.reason, sb)
        _recent[f"{req.symbol}:{req.side}"] = datetime.now(IST)
        if notifier and f.ok:
            from intraday.notifier import Action
            notifier.send(Action(
                req.symbol, f"PAPER_{req.side}",
                f"[PAPER] {req.side} {req.quantity} @ ₹{f.fill_price:,.2f}",
                f"{req.reason}\nSimulated — no real order was placed. "
                f"Charges ₹{f.charges:.2f}.",
                ltp=f.fill_price, urgency="INFO"), force=True)
        return OrderResult(f.ok, f.order_id, f.message,
                           None if f.ok else "PAPER_NOT_FILLED")

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
        msg = str(e)
        key = f"{req.symbol}:{req.side}"

        # Record the ATTEMPT, not just the success. Without this the duplicate
        # guard never engaged on failures, so a rejected order retried every
        # 15-second cycle forever — observed live: the same PPLPHARMA sell
        # failing at 09:46:30, 09:46:47 and 09:47:03 with an identical error,
        # hammering the API and burying every other line in the log.
        _recent[key] = datetime.now(IST)

        # Some rejections are CONFIGURATION, not conditions. Retrying an IP
        # allowlist error at any interval is pointless — it cannot succeed until
        # a human changes something at the broker — so it is latched off for the
        # session and reported once, loudly, instead of once every cycle.
        permanent = any(s in msg.lower() for s in (
            "no ips configured", "allowed ips", "static ip",
            "insufficient permission", "api_key", "access_token",
            "not authorised", "not authorized",
        ))
        if permanent:
            _blocked[key] = msg
            logger.error(
                f"  order PERMANENTLY BLOCKED for {req.symbol} {req.side} — {msg[:160]}\n"
                f"      This cannot succeed by retrying. Nothing further will be "
                f"attempted for this symbol until the daemon restarts."
            )
        else:
            logger.error(f"  order FAILED {req.side} {req.quantity} {req.symbol}: {msg[:200]}")

        _log(sb, req, "BLOCKED_PERMANENT" if permanent else "FAILED", None, msg[:300])
        return OrderResult(False, None, msg,
                           "BROKER_CONFIG" if permanent else "BROKER_ERROR")


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
