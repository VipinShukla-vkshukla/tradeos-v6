"""
An exit that does not fill is not an exit.

WHY THIS MODULE EXISTS — 18-Aug-2026
------------------------------------
GABRIEL, 17-Aug-2026, reconstructed from the broker log and Kite order history:

    09:15:13  EXIT_STALL fires. SELL LIMIT 2 @ 1460.60, priced at
              ltp * (1 - exit_slip_bps) — a 0.30% buffer on a stock whose
              average daily range is 4.7%.
    09:16:01  gtt_manager.sync() cancels the protective GTT. The position had
              moved to status CLOSING, dropped out of the ACTIVE list sync()
              builds `held` from, and so looked like a GTT resting against
              stock that is no longer held.
    09:16 -   The order rests, unfilled, above a falling market. Nothing
    11:48     reprices it. tools.health's `pending` check watches unfilled
              ENTRIES only, so nothing reports it either. For two and a half
              hours the position has no stop of any kind.
    11:48:42  The OPERATOR repriced it by hand to 1432.60.
    11:48:51  Filled, at 1.92% below the price the exit was decided at.

Without that manual intervention the limit would still be resting and the
position would still be open and unprotected. That is what this module closes,
and it applied to every LIVE swing exit in the book, not to one trade.

THREE FAULTS, THREE FIXES
-------------------------
1. A FIXED BASIS-POINT BUFFER IS NOT A PRICE. Thirty basis points under the
   last trade is inside the noise of a 4.7%-ATR name, and the move that
   triggers an exit is usually larger than the buffer — so the limit is behind
   the market the moment it arrives. `exit_limit_price` scales the buffer with
   the name's own volatility and keeps exit_slip_bps as a floor.

2. NOBODY WATCHED THE ORDER AFTER IT WAS SENT. `reprice_stale_exits` runs on
   the slow timer, walks an unfilled SELL to the current market, and converts
   it to MARKET after `exit_order_max_repricings` attempts. An exit is a
   decision that the position should be closed; the price is a preference, and
   a preference must not be able to veto the decision indefinitely.

3. THE STOP WAS RELEASED BEFORE THE REPLACEMENT EXISTED. Protection is now
   released on FILL, never on placement — see `symbols_with_open_exit`, which
   gtt_manager.sync() consults before treating a GTT as orphaned.

Everything here reads BROKER TRUTH rather than the book. A position's status
column says what this system intended; kite.orders() says what is actually
resting, and only the second one can be sold against.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config import IST, cfg_bool, cfg_float, cfg_int


# Kite statuses that mean "still working at the exchange". Anything not in here
# is terminal (COMPLETE, CANCELLED, REJECTED) and is not ours to chase.
OPEN_STATUSES = ("OPEN", "TRIGGER PENDING", "OPEN PENDING", "MODIFY PENDING",
                 "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING")


def exit_limit_price(ltp: float, atr_pct: float | None) -> float:
    """
    Where to rest a SELL so that it actually fills, given how much this name moves.

    Pure, so tools.verify can assert the shape of it without a broker.

    The buffer is the LARGER of the flat basis-point setting and a fraction of
    the name's daily ATR. On GABRIEL (ATR 4.68%) that is 1.17% rather than
    0.30% — a limit at ₹1,447.9 instead of ₹1,460.6, and the 09:15 tape traded
    through ₹1,447 inside the first minute. On a 1.5%-ATR large cap the ATR
    term is 0.375% and the flat floor barely binds. The point is not that the
    buffer is wider; it is that the buffer tracks the thing it exists to
    absorb.
    """
    ltp = float(ltp or 0)
    if ltp <= 0:
        return 0.0
    flat = cfg_float("exit_slip_bps", 30.0) / 10000.0
    frac = cfg_float("exit_slip_atr_frac", 0.25)
    atr = max(0.0, float(atr_pct or 0.0)) / 100.0
    return round(ltp * (1 - max(flat, frac * atr)), 1)


def symbols_with_open_exit(kite=None) -> set | None:
    """
    Every symbol carrying a SELL order that is still working at the broker.

    gtt_manager.sync() calls this before cancelling a GTT it believes to be
    orphaned. A position whose exit order has been PLACED but not FILLED is
    still held — the shares are still in the account — and dropping its stop in
    that window is exactly what left GABRIEL unprotected on 17-Aug.

    Returns None, never an empty set, when the broker cannot be reached. The
    caller must treat that as "do not cancel anything this cycle": the cost of
    over-keeping a GTT is a duplicate that sync's own duplicate branch cleans
    up next pass, and the cost of over-cancelling one is an unprotected live
    position. Those are not comparable, so the ambiguous case resolves toward
    keeping protection.
    """
    try:
        if kite is None:
            from kite import kite_client
            kite = kite_client.get_kite()
        if not kite:
            return None
        out = set()
        for o in (kite.orders() or []):
            if (o.get("transaction_type") or "").upper() != "SELL":
                continue
            if (o.get("status") or "").upper() in OPEN_STATUSES:
                sym = o.get("tradingsymbol")
                if sym:
                    out.add(sym)
        return out
    except Exception as e:
        logger.warning(f"  exit orders: could not read open orders — {e}")
        return None


def stale_exits(orders: list, now, max_age_s: float) -> list:
    """
    The open SELL orders that have been resting longer than `max_age_s`.

    Pure — it takes the order list rather than fetching one — so the age
    arithmetic and the status filter are testable with no broker and no clock.
    """
    out = []
    for o in orders or []:
        if (o.get("transaction_type") or "").upper() != "SELL":
            continue
        if (o.get("status") or "").upper() not in OPEN_STATUSES:
            continue
        ts = o.get("order_timestamp")
        if ts is None:
            continue
        try:
            age = (now - ts).total_seconds()
        except TypeError:
            # Kite hands back a tz-naive timestamp; `now` is tz-aware IST.
            age = (now.replace(tzinfo=None) - ts).total_seconds()
        if age >= max_age_s:
            out.append(dict(o, _age_s=age))
    return out


def reprice_stale_exits(sb=None, prices: dict | None = None) -> dict:
    """
    Walk unfilled SELL exits forward until they fill.

    Called on the SLOW timer, not per cycle: a modify is a broker write, and
    repricing every fifteen seconds would burn rate limit and churn the order
    book without improving the odds of a fill.

    THE ESCALATION IS THE POINT. Each pass moves the limit to the current
    market less the volatility-scaled buffer. After `exit_order_max_repricings`
    passes the order is cancelled and re-sent as MARKET, because by then the
    evidence is that this name is not coming back to any limit worth naming,
    and continuing to ask turns a decision to exit into a decision to hold.

    OFF BY DEFAULT (`exit_order_reprice_enabled`) — it places and cancels real
    orders. Never touches a BUY: an unfilled entry that expires is a trade not
    taken, which is a normal outcome and is already watched by tools.health's
    `pending` check.
    """
    res = {"seen": 0, "repriced": 0, "marketed": 0, "errors": 0}
    if not cfg_bool("exit_order_reprice_enabled", False):
        return res
    try:
        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            return res
        orders = kite.orders() or []
    except Exception as e:
        logger.warning(f"  exit reprice: could not read orders — {e}")
        res["errors"] += 1
        return res

    now = datetime.now(IST)
    max_age = cfg_float("exit_order_reprice_after_s", 60.0)
    max_tries = cfg_int("exit_order_max_repricings", 3)

    for o in stale_exits(orders, now, max_age):
        res["seen"] += 1
        sym = o.get("tradingsymbol")
        oid = str(o.get("order_id") or "")
        qty = int(o.get("pending_quantity") or o.get("quantity") or 0)
        if not sym or not oid or qty <= 0:
            continue

        # HOW MANY TIMES HAS THIS ORDER ALREADY BEEN WALKED? Counted from the
        # broker's own history rather than from a dict in this process, so the
        # escalation survives a restart — which matters, because a daemon
        # restart is precisely when an exit is most likely to be left stranded.
        tries = _repricings_so_far(kite, oid)

        ltp = (prices or {}).get(sym)
        if not ltp:
            try:
                q = kite.ltp([f"NSE:{sym}"]) or {}
                ltp = float((q.get(f"NSE:{sym}") or {}).get("last_price") or 0)
            except Exception:
                ltp = 0
        if not ltp:
            continue

        if tries >= max_tries:
            _escalate_to_market(kite, sb, sym, oid, qty, ltp, o, tries, res)
            continue

        new_px = exit_limit_price(float(ltp), atr_pct_for(sb, sym))
        # Four paise is not worth a broker write; it is the same reasoning
        # gtt_resync_min_pct applies to resting stops.
        if new_px <= 0 or abs(new_px - float(o.get("price") or 0)) < 0.05:
            continue
        try:
            kite.modify_order(variety=kite.VARIETY_REGULAR, order_id=oid,
                              order_type=kite.ORDER_TYPE_LIMIT,
                              quantity=qty, price=new_px)
            res["repriced"] += 1
            logger.info(
                f"  exit: {sym} unfilled {o['_age_s']:.0f}s at ₹{o.get('price')} "
                f"— repriced to ₹{new_px:.2f} (attempt {tries + 1} of {max_tries})")
            _log(sb, sym, "EXIT_REPRICED", oid, new_px, qty,
                 f"attempt {tries + 1}, age {o['_age_s']:.0f}s")
        except Exception as e:
            res["errors"] += 1
            logger.error(f"  exit: {sym} reprice FAILED — {e}")
            _log(sb, sym, "EXIT_REPRICE_FAILED", oid, new_px, qty, str(e)[:250])

    return res


def _escalate_to_market(kite, sb, sym, oid, qty, ltp, order, tries, res) -> None:
    """Stop asking and cross the spread. Cancel first — never two live sells."""
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=oid)
    except Exception as e:
        # If the cancel fails the original may still fill, and placing a second
        # order would sell twice. Refuse to escalate rather than risk it.
        res["errors"] += 1
        logger.error(f"  exit: {sym} cancel before MARKET failed, NOT escalating — {e}")
        _log(sb, sym, "EXIT_MARKET_ABORTED", oid, ltp, qty, str(e)[:250])
        return
    try:
        new_id = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange="NSE", tradingsymbol=sym,
            transaction_type=kite.TRANSACTION_TYPE_SELL, quantity=qty,
            product=kite.PRODUCT_CNC, order_type=kite.ORDER_TYPE_MARKET,
            tag="tradeos")
        res["marketed"] += 1
        logger.warning(
            f"  exit: {sym} unfilled after {tries} repricing(s) and "
            f"{order['_age_s'] / 60:.0f} min — sent MARKET (id {new_id}). An exit "
            f"that will not fill at a limit is still an exit")
        _log(sb, sym, "EXIT_MARKET", new_id, ltp, qty,
             f"escalated after {tries} repricings, age {order['_age_s']:.0f}s")
    except Exception as e:
        res["errors"] += 1
        logger.error(f"  exit: {sym} MARKET escalation FAILED after cancel — {e}")
        _log(sb, sym, "EXIT_MARKET_FAILED", None, ltp, qty, str(e)[:250])


def _repricings_so_far(kite, order_id: str) -> int:
    """Modifications already applied to this order, from the broker's history."""
    try:
        return sum(1 for h in (kite.order_history(order_id) or [])
                   if "MODIFY" in (h.get("status") or "").upper())
    except Exception:
        # Unknown history counts as zero attempts, which errs toward repricing
        # rather than toward an unnecessary market order.
        return 0


def atr_pct_for(sb, symbol: str) -> float | None:
    """Daily ATR% for the buffer. None when unknown, so the flat floor binds."""
    try:
        if sb is None:
            from config import get_supabase
            sb = get_supabase()
        r = (sb.table("stock_data_daily").select("atr_pct")
               .eq("symbol", symbol).order("date", desc=True)
               .limit(1).execute().data or [])
        return float(r[0]["atr_pct"]) if r and r[0].get("atr_pct") else None
    except Exception:
        return None


def _log(sb, symbol, action, ref_id, price, qty, detail) -> None:
    """Every broker write recorded, successes and failures — the rule elsewhere too."""
    try:
        if sb is None:
            from config import get_supabase
            sb = get_supabase()
        sb.table("intraday_broker_log").insert({
            "ts": datetime.now(IST).isoformat(), "symbol": symbol,
            "channel": "ORDER", "action": action, "side": "SELL",
            "ref_id": str(ref_id) if ref_id else None,
            "price": float(price) if price else None,
            "quantity": int(qty) if qty else None,
            "detail": detail, "framework": "SWING",
        }).execute()
    except Exception:
        pass
