"""
Phase 2.5 — broker-side stops (GTT).

THE PROBLEM THIS SOLVES
-----------------------
Every stop in this system has so far been notional: a number in a Postgres row
that only becomes an action if a Python process happens to be running, holds a
valid broker session, and gets a price. Until today that process ran ONCE a day,
at 13:00. A stop breached at 09:31 was acted on nearly six hours later.

A GTT is an order resting at Zerodha. It fires whether or not this laptop is on,
whether or not the token expired, whether or not the daemon crashed at 09:20. It
converts the stop from an intention into a commitment.

WHY THE ENGINE STILL RUNS
-------------------------
A GTT trigger is a static price. It cannot express "trail 1.5R below the
high-water mark", which is where most of the exit edge lives. So the two are
layered deliberately:

  GTT     the hard floor — a safety net that survives everything
  engine  the smart part — ratchets the stop up, books partials, times out

The engine moves the stop; this module makes the broker agree. If they ever
disagree the BROKER wins for safety and the divergence is reported, because the
resting order is the one that will actually execute.

WHY NOT OCO (stop AND target in one GTT)
----------------------------------------
An OCO would also take profit automatically, which sounds strictly better. It is
not, for this strategy: the exit policy books HALF at 1.5R and lets the rest
run, and a two-leg GTT cannot express a partial. Placing an OCO target would
either exit the whole position early or fight the engine's partial. The stop is
the leg worth resting at the broker; the target is the leg worth managing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_float, get_supabase
from execution.gates import gtt_enabled, is_market_open


@dataclass
class GttState:
    symbol: str
    gtt_id: int | None
    trigger: float | None
    quantity: int | None
    status: str | None


def _kite():
    from kite import kite_client
    return kite_client.get_kite()


def list_gtts() -> dict[str, list[GttState]] | None:
    """
    Active stop GTTs, keyed by symbol, each list sorted tightest-stop-first.
    None means the fetch could not be trusted — no session, or the call
    failed — and callers MUST treat that as "unknown", never as "zero GTTs
    exist". Collapsing unknown into empty is exactly what let a list-call
    timeout re-place a stop that was already resting: on 2026-08-06 six
    positions each grew a second live SELL GTT because this returned {}
    instead of admitting it couldn't see anything, and every one of those
    six placements then went through because the KITE SESSION ITSELF was
    fine — only the one read had timed out.

    A symbol can map to more than one entry. Kite has no uniqueness
    constraint on GTTs, so a re-placed stop rests ALONGSIDE the old one, not
    in place of it. sync() cancels everything after the first as a duplicate.
    """
    kite = _kite()
    if not kite:
        return None
    try:
        raw = kite.get_gtts() or []
    except Exception as e:
        logger.warning(f"  gtt: list failed — {e}")
        return None

    out: dict[str, list[GttState]] = {}
    for g in raw:
        try:
            if (g.get("status") or "").lower() != "active":
                continue
            cond = g.get("condition") or {}
            sym = cond.get("tradingsymbol")
            trig = (cond.get("trigger_values") or [None])[0]
            orders = g.get("orders") or []
            qty = int(orders[0].get("quantity")) if orders else None
            # SELL only — a BUY GTT is an entry trigger, not a stop, and
            # treating one as a stop would cancel the wrong order.
            if orders and (orders[0].get("transaction_type") or "").upper() != "SELL":
                continue
            if sym:
                out.setdefault(sym, []).append(
                    GttState(sym, int(g.get("id")), float(trig) if trig else None,
                             qty, g.get("status")))
        except Exception:
            continue

    # Tightest trigger first — same rule as the ratchet below: of two stops
    # resting on one position, the safer one (higher, for a SELL) is the one
    # to treat as canonical. Newest id breaks a tie between equal triggers.
    for sym in out:
        out[sym].sort(key=lambda s: (s.trigger if s.trigger is not None else float("-inf"),
                                     s.gtt_id or 0), reverse=True)
    return out


def place_stop(symbol: str, qty: int, trigger: float, last_price: float) -> int | None:
    """
    Rest a SELL stop at the broker. Returns the GTT id.

    The limit price sits a configurable margin BELOW the trigger. A pure limit
    at the trigger price frequently does not fill in a fast move — which is
    exactly the move a stop exists for — leaving you holding a position you
    believed was protected.
    """
    if not gtt_enabled():
        logger.debug(f"  gtt: disabled — would place {symbol} stop @ {trigger}")
        return None
    kite = _kite()
    if not kite or qty <= 0 or trigger <= 0:
        return None

    slip = cfg_float("gtt_limit_slippage_pct", 0.5) / 100.0
    limit = round(trigger * (1 - slip), 1)
    try:
        resp = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange="NSE",
            trigger_values=[round(trigger, 1)],
            last_price=round(last_price, 1),
            orders=[{
                "transaction_type": kite.TRANSACTION_TYPE_SELL,
                "quantity":         int(qty),
                "order_type":       kite.ORDER_TYPE_LIMIT,
                "product":          kite.PRODUCT_CNC,
                "price":            limit,
            }],
        )
        gid = int(resp.get("trigger_id")) if isinstance(resp, dict) else int(resp)
        logger.success(f"  gtt: placed {symbol} SELL {qty} trigger ₹{trigger:.2f} limit ₹{limit:.2f} (id {gid})")
        _log(symbol, "PLACED", gid, trigger, qty)
        return gid
    except Exception as e:
        logger.error(f"  gtt: place failed for {symbol} — {e}")
        _log(symbol, "PLACE_FAILED", None, trigger, qty, detail=str(e)[:300])
        return None


def modify_stop(gtt_id: int, symbol: str, qty: int, trigger: float, last_price: float) -> bool:
    if not gtt_enabled():
        return False
    kite = _kite()
    if not kite:
        return False
    slip = cfg_float("gtt_limit_slippage_pct", 0.5) / 100.0
    limit = round(trigger * (1 - slip), 1)
    try:
        kite.modify_gtt(
            trigger_id=gtt_id,
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange="NSE",
            trigger_values=[round(trigger, 1)],
            last_price=round(last_price, 1),
            orders=[{
                "transaction_type": kite.TRANSACTION_TYPE_SELL,
                "quantity":         int(qty),
                "order_type":       kite.ORDER_TYPE_LIMIT,
                "product":          kite.PRODUCT_CNC,
                "price":            limit,
            }],
        )
        logger.success(f"  gtt: {symbol} trigger -> ₹{trigger:.2f} (id {gtt_id})")
        _log(symbol, "MODIFIED", gtt_id, trigger, qty)
        return True
    except Exception as e:
        logger.error(f"  gtt: modify failed for {symbol} — {e}")
        _log(symbol, "MODIFY_FAILED", gtt_id, trigger, qty, detail=str(e)[:300])
        return False


def cancel_stop(gtt_id: int, symbol: str) -> bool:
    if not gtt_enabled():
        return False
    kite = _kite()
    if not kite:
        return False
    try:
        kite.delete_gtt(trigger_id=gtt_id)
        logger.info(f"  gtt: cancelled {symbol} (id {gtt_id})")
        _log(symbol, "CANCELLED", gtt_id, None, None)
        return True
    except Exception as e:
        logger.warning(f"  gtt: cancel failed for {symbol} — {e}")
        return False


def sync(positions: list[dict], prices: dict[str, float], notifier=None) -> dict:
    """
    Make the broker's resting stops match the engine's intended stops.

    Called on a slow timer, not per tick: a GTT modify is a broker write, and
    re-placing it because the intended stop moved by four paise would burn rate
    limit and clutter the order book for no protective benefit. Only a move
    larger than gtt_resync_min_pct is worth acting on.

    Stops are only ever RATCHETED UP here. If the engine's intended stop is
    below what is already resting, the resting one is left alone — loosening a
    stop that is already protecting a position is never an improvement, and a
    bug that computes a lower stop should not be able to widen real risk.

    Product stays CNC here even when intraday_product is MIS: Kite GTTs are
    CNC/NRML only. If you switch intraday to MIS, intraday stops must come from
    the live exit loop, not from a resting GTT — see _product() in
    order_manager.py.
    """
    result = {"placed": 0, "modified": 0, "cancelled": 0, "skipped": 0, "errors": 0}
    if not gtt_enabled():
        result["skipped"] = len(positions)
        return result
    if not is_market_open():
        # GTTs can be placed outside market hours, but a stop derived from a
        # stale last_price is not one worth resting. Wait for live prices.
        result["skipped"] = len(positions)
        return result

    # A GTT is a REAL resting sell order at Zerodha. A paper position is stock
    # the simulation only pretends to hold, so resting a stop for one would put
    # a live sell order against shares that either do not exist — or, far worse,
    # do exist because another framework holds the same symbol for real, in
    # which case a simulated stop could sell a genuine holding.
    #
    # Paper stops are enforced by the exit engine in-process, which is the
    # correct simulation of a broker stop and costs nothing at the broker.
    # CNC only. Kite GTTs are CNC/NRML — an MIS tranche cannot have one, and
    # does not need one: the broker force-squares MIS around 15:20, which is the
    # same protection (survives this process dying) by another mechanism.
    real = [p for p in positions
            if (p.get("mode") or "LIVE").upper() != "PAPER"
            and (p.get("product") or "CNC").upper() != "MIS"]
    if len(real) != len(positions):
        n = len(positions) - len(real)
        logger.debug(f"  gtt: {n} paper position(s) excluded — simulated stops "
                     f"stay in-process")
        result["skipped"] += n
    positions = real

    existing = list_gtts()
    if existing is None:
        # Cannot confirm what is already resting, so nothing this cycle is
        # safe — not a placement (might duplicate), not a cancel (might drop
        # real protection). Skip the whole cycle; sync() is called again on
        # the next resync tick and tries fresh. See list_gtts()'s docstring
        # for the 2026-08-06 incident this replaced.
        logger.warning(f"  gtt: sync skipped — could not confirm what is already "
                       f"resting, not safe to place or cancel this cycle "
                       f"({len(positions)} position(s) deferred)")
        result["skipped"] = len(positions)
        result["errors"] += 1
        return result

    min_move = cfg_float("gtt_resync_min_pct", 0.4) / 100.0

    for p in positions:
        sym = p.get("symbol")
        qty = int(p.get("current_qty") or p.get("actual_qty") or 0)
        want = p.get("active_sl") or p.get("planned_stop")
        ltp = prices.get(sym) or p.get("current_price")
        if not sym or qty <= 0 or not want or not ltp:
            result["skipped"] += 1
            continue
        want = float(want)

        # A stop above the market would trigger instantly on placement, turning
        # a protective order into an immediate market exit.
        if want >= float(ltp):
            logger.warning(f"  gtt: {sym} intended stop ₹{want:.2f} is at/above LTP ₹{ltp:.2f} "
                           f"— not resting an order that would fire on arrival")
            result["skipped"] += 1
            continue

        cur_list = existing.get(sym) or []
        cur = cur_list[0] if cur_list else None

        # More than one active SELL GTT for a symbol we still hold is always
        # a duplicate — Kite has no uniqueness constraint, so a re-placed
        # stop rests ALONGSIDE the old one instead of replacing it. Keep the
        # tightest (cur, index 0 after the sort in list_gtts()) and cancel
        # the rest, regardless of whether cur itself needs any change below.
        for extra in cur_list[1:]:
            if extra.gtt_id and cancel_stop(extra.gtt_id, sym):
                result["cancelled"] += 1
                logger.warning(f"  gtt: {sym} had a duplicate stop resting "
                               f"(id {extra.gtt_id}, trigger ₹{extra.trigger}) — cancelled")

        if cur is None:
            gid = place_stop(sym, qty, want, float(ltp))
            if gid:
                result["placed"] += 1
                if notifier:
                    from intraday.notifier import Action
                    notifier.send(Action(sym, "GTT_PLACED",
                                         f"Broker stop resting at ₹{want:.2f} for {qty} sh",
                                         "This stop now survives the daemon being off.",
                                         ltp=float(ltp), urgency="INFO"))
            else:
                result["errors"] += 1
            continue

        needs_qty_fix = cur.quantity is not None and cur.quantity != qty
        moved_up = cur.trigger is not None and want > cur.trigger * (1 + min_move)

        if moved_up or needs_qty_fix:
            if modify_stop(cur.gtt_id, sym, qty, want, float(ltp)):
                result["modified"] += 1
                if notifier and moved_up:
                    from intraday.notifier import Action
                    notifier.send(Action(sym, "GTT_RAISED",
                                         f"Broker stop raised ₹{cur.trigger:.2f} → ₹{want:.2f}",
                                         f"{qty} sh protected. Risk reduced at the broker, not just in the book.",
                                         ltp=float(ltp), urgency="INFO"))
            else:
                result["errors"] += 1
        else:
            result["skipped"] += 1

    # A GTT resting for a symbol no longer held is a live sell order against
    # stock that is not there — it must not be allowed to linger.
    held = {p.get("symbol") for p in positions}
    for sym, gs in existing.items():
        if sym not in held:
            for g in gs:
                if g.gtt_id and cancel_stop(g.gtt_id, sym):
                    result["cancelled"] += 1

    return result


def _log(symbol: str, action: str, gtt_id: int | None,
         trigger: float | None, qty: int | None, detail: str | None = None) -> None:
    """Audit trail. Every broker write is recorded, successes and failures."""
    try:
        get_supabase().table("intraday_broker_log").insert({
            "ts":       datetime.now(IST).isoformat(),
            "symbol":   symbol,
            "channel":  "GTT",
            "action":   action,
            "ref_id":   str(gtt_id) if gtt_id else None,
            "price":    trigger,
            "quantity": qty,
            "detail":   detail,
        }).execute()
    except Exception as e:
        logger.debug(f"  gtt log failed: {e}")
