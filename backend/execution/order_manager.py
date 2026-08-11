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
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (IST, TOTAL_CAPITAL, get_supabase, is_kill_switch_active,
                    DRY_RUN)
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

# The same, for causes that are not about a symbol AT ALL.
#
# An IP allowlist rejection, a bad api_key, a dead access_token — none of these
# know what a symbol is. They kill EVERY order on the account, both books,
# entries and exits alike. Latching them per (symbol, side) meant the account
# was dead while the system reported one stuck name: on 2026-08-06 a KIMS
# giveback exit was rejected for the IP, latched as "KIMS:SELL", and the log
# said "nothing further will be attempted for this symbol" — true of the latch,
# and a serious understatement of the situation. Every subsequent symbol would
# have paid its own rejected round trip to learn the same thing.
_blocked_account: str | None = None

# Causes that are properties of the ACCOUNT or the APP, never of a symbol.
_ACCOUNT_WIDE = (
    "no ips configured", "allowed ips", "static ip",
    "insufficient permission", "api_key", "access_token",
    "not authorised", "not authorized",
)


def _product(kite, framework: str):
    """
    CNC or MIS, and only INTRADAY may be MIS.

    intraday_product existed in system_config and nothing read it — every order,
    swing and intraday alike, went out CNC. That is the safe direction to be
    wrong in (CNC is delivery: no leverage, no broker-forced square-off), but a
    switch that reads as configured and is not is exactly what this audit
    exists to catch.

    Swing is never MIS regardless of the setting: a swing position is meant to
    be held for days, and MIS would have the broker liquidate it at 15:20 on the
    day it was opened.
    """
    if (framework or "SWING").upper() != "INTRADAY":
        return kite.PRODUCT_CNC
    from config import cfg
    want = (cfg("intraday_product", "CNC") or "CNC").strip().upper()
    if want == "MIS":
        return kite.PRODUCT_MIS
    if want not in ("CNC", "MIS"):
        logger.warning(f"  intraday_product is '{want}', which is neither CNC nor "
                       f"MIS — using CNC")
    return kite.PRODUCT_CNC


def _today_totals(sb, framework: str = "SWING") -> tuple[int, float, float]:
    """
    Orders placed and notional committed today: (count, framework spend, all spend).

    Scoped per framework because the caps are per framework — counting swing
    orders against the intraday budget would have either framework exhaust the
    other's allowance. The third figure is the combined spend, because both
    books draw on ONE account: two frameworks each permitted their own daily
    notional can together commit more than the account holds.
    """
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = (sb.table("intraday_broker_log").select("price,quantity,action,framework,side")
                  .eq("channel", "ORDER").gte("ts", start.isoformat())
                  .execute().data or [])
        placed = [r for r in rows if r.get("action") == "PLACED"]
        fw = (framework or "SWING").upper()

        def _val(r):
            return float(r.get("price") or 0) * int(r.get("quantity") or 0)

        # Rows written before the framework column existed are NULL. Counting
        # them against whichever framework is asking is the conservative read:
        # it can only make preflight stricter, never permit more.
        mine = [r for r in placed if (r.get("framework") or fw).upper() == fw]

        # BUY ONLY, and that is the whole point of these numbers.
        #
        # Both figures gate entries: how many positions may be opened today, and
        # how much capital may be committed. A SELL does neither — it closes
        # exposure and returns cash — so counting sells against them meant a
        # normal day's exits exhausted the entry budget before any entry was
        # considered. On 30 Jul three PPLPHARMA partial-book sells filled a cap
        # of two, and auto-entry could not have fired for the rest of the day no
        # matter what the pipeline found.
        #
        # Exits were already exempt from being BLOCKED by the cap. They were not
        # exempt from CONSUMING it, which is the same bug wearing the opposite
        # face.
        buys     = [r for r in mine   if (r.get("side") or "").upper() == "BUY"]
        buys_all = [r for r in placed if (r.get("side") or "").upper() == "BUY"]
        return len(buys), sum(_val(r) for r in buys), sum(_val(r) for r in buys_all)
    except Exception as e:
        # Unknown spend must not read as zero spend. Report the budget as fully
        # consumed so preflight blocks rather than permits.
        logger.warning(f"  order: could not read today's totals — {e}")
        return (max_orders_per_day(framework), max_notional_per_day(framework),
                float(TOTAL_CAPITAL))


def parse_hhmm(s: str) -> time | None:
    """'10:30' -> time(10, 30). None on anything unparseable, so a bad
    config value degrades to "no cutoff" rather than a crash."""
    try:
        h, m = str(s).strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def entry_reserved(n_today: int, now: datetime, max_before_cutoff: int,
                   cutoff: time | None) -> tuple[bool, str]:
    """
    PURE — replaces entry_paced() (11-Aug-2026, same day it shipped).

    entry_paced() blocked any entry arriving within a fixed gap of the LAST
    one, regardless of quality. Checked against the actual 10-Aug sequence
    it was built from, that was wrong: AUBANK (09:36:24) and SCI (09:36:26)
    were not two candidates racing a stale counter — the allocator scored 8
    proposals together at 09:36:23 ("3 to take, 5 refused") and picked both
    of them, in ONE joint, opportunity-cost-aware decision, as two of that
    day's three best trades. A 20-minute gap would have let AUBANK through
    and then refused SCI for 20 minutes for no reason at all — overriding a
    decision the allocator had already made well, at zero benefit. (The
    third slot, VIJAYA, genuinely DID need to out-compete rivals across
    three separate allocator passes over the next ~40 seconds before
    winning it at 09:37:03 — the rising bar was already doing that job
    correctly on its own.)

    What actually went wrong on 10-Aug was not "two entries too close
    together" — it was that ALL THREE of the day's slots were spent on a
    single snapshot of the first ~21 minutes (itself mostly a symptom of
    the Kite token being stale until then), leaving nothing for the
    remaining ~5.5 hours regardless of what showed up. This targets THAT
    directly: a cap on how many of today's entries may land before a
    cutoff clock time, with NO restriction on how close together they are
    otherwise. Two, or three, genuinely good candidates that clear the
    allocator's bar in the same scoring pass — before OR after the cutoff
    — are taken together exactly as the allocator decided; only a FOURTH
    early one, arriving separately, would ever be refused.

    Returns (blocked, why). blocked is False whenever max_before_cutoff is
    0/None (reserve off) or cutoff is None (unparseable config, same fail-
    open-on-bad-config posture as elsewhere in this module) or `now` is
    already past the cutoff — the reserve has no opinion at all once the
    session has had a chance to show its character.
    """
    if not max_before_cutoff or max_before_cutoff <= 0 or cutoff is None:
        return False, ""
    if now.time() >= cutoff:
        return False, ""
    if n_today >= max_before_cutoff:
        return True, (f"{n_today} already taken before "
                      f"{cutoff.strftime('%H:%M')} — reserving the rest of "
                      f"the budget for later in the session")
    return False, ""


def preflight(req: OrderRequest, sb=None, framework: str = "SWING") -> OrderResult:
    """
    Every reason this order must not be sent. Cheap, and checked in order of severity.

    framework decides WHICH caps apply. It used to be omitted here while place()
    accepted it, so every swing order was measured against the intraday rails —
    swing_max_order_value was configured, displayed on the panel, and never
    consulted by the code that blocks orders.
    """
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
    if _blocked_account:
        return OrderResult(False, None,
                           f"ALL orders blocked for this session — "
                           f"{_blocked_account[:140]}",
                           "BROKER_CONFIG")
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

    fw = (framework or "SWING").upper()
    value = px * req.quantity
    # The per-order cap guards against a SIZING bug, which is an entry concern:
    # an exit's quantity is not computed from capital, it is what you already
    # hold, and it is checked against broker holdings below. Applying the cap to
    # a SELL would mean a position that grew past the cap could not be closed —
    # being unable to reduce risk is a strictly worse failure than being unable
    # to add it, which is the rule this module opens with.
    if req.side == "BUY" and value > max_order_value(fw):
        return OrderResult(False, None,
                           f"order value ₹{value:,.0f} exceeds the ₹{max_order_value(fw):,.0f} "
                           f"per-order cap for {fw}",
                           "ORDER_CAP")

    n_today, notional_today, notional_all = _today_totals(sb, fw)
    # Exits are allowed to consume the last of the budget; entries are not.
    if req.side == "BUY":
        if n_today >= max_orders_per_day(fw):
            return OrderResult(False, None,
                               f"{n_today} {fw} orders already placed today "
                               f"(cap {max_orders_per_day(fw)})",
                               "DAILY_COUNT")
        if notional_today + value > max_notional_per_day(fw):
            return OrderResult(False, None,
                               f"₹{notional_today:,.0f} committed to {fw} today; this order "
                               f"would breach the ₹{max_notional_per_day(fw):,.0f} daily "
                               f"notional cap",
                               "DAILY_NOTIONAL")
        # Both books spend the same account. Without this, swing and intraday
        # each staying inside their own daily cap can still commit more in a day
        # than the account contains.
        if notional_all + value > TOTAL_CAPITAL:
            return OrderResult(False, None,
                               f"₹{notional_all:,.0f} already committed across both "
                               f"frameworks today; this order would exceed the "
                               f"₹{TOTAL_CAPITAL:,.0f} account",
                               "ACCOUNT_NOTIONAL")

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
            # An EMPTY margins dict means the fetch failed — a real one always
            # carries all four keys. Collapsing that into cash=0 produced
            # "order value ₹5,800 exceeds available cash ₹0", which reads as a
            # drained account and is really a 7-second read timeout: on
            # 2026-08-06 the margins call timed out 13 times in three minutes
            # during the same Kite blip that duplicated the GTTs. Refusing is
            # still correct — capital you cannot see is capital you cannot size
            # against — but it must say which of the two it is.
            m = kite_client.fetch_margins()
            if not m:
                return OrderResult(False, None,
                                   "could not read available cash from the broker — "
                                   "refusing to size an order against unknown capital. "
                                   "This is a broker/network failure, NOT a zero balance",
                                   "BROKER_UNAVAILABLE")
            cash = float(m.get("available_cash") or 0)
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
    pre = preflight(req, sb, framework)
    if not pre.ok:
        logger.warning(f"  order BLOCKED [{pre.blocked_by}] {req.side} {req.quantity} "
                       f"{req.symbol}: {pre.message}")
        _log(sb, req, "BLOCKED", None, pre.message, framework)
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
                ltp=f.fill_price, urgency="INFO",
                # This function ALREADY receives framework and uses it correctly
                # three lines above (is_paper(framework)) — it was just never
                # passed into the alert. Action.framework defaults to
                # "INTRADAY", so a paper SWING order (should that mode ever be
                # used) would silently alert to the intraday channel.
                framework=framework), force=True)
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
            "product":          _product(kite, framework),
            "order_type":       (kite.ORDER_TYPE_LIMIT if req.order_type == "LIMIT"
                                 else kite.ORDER_TYPE_MARKET),
            "tag":              "tradeos",
        }
        if req.order_type == "LIMIT":
            params["price"] = round(float(req.price), 1)

        order_id = kite.place_order(**params)
        _recent[f"{req.symbol}:{req.side}"] = datetime.now(IST)
        logger.success(f"  order PLACED {req.side} {req.quantity} {req.symbol} (id {order_id})")
        _log(sb, req, "PLACED", str(order_id), req.reason, framework)

        if notifier:
            from intraday.notifier import Action
            # THE LIVE-ORDER ALERT ROUTE, and it was carrying no framework at
            # all. Every real BUY/SELL — swing or intraday, CRITICAL urgency —
            # defaulted to Action.framework="INTRADAY" and went to the
            # intraday Discord/Telegram channel regardless of which book it
            # belonged to. TRAVELFOOD's SELL and GABRIEL's BUY on 2026-08-06
            # were both swing orders tagged "[INTRADAY]" this way. framework
            # is a parameter of THIS function already — preflight(), is_paper()
            # and _product() three lines above all use it correctly. It just
            # never reached the alert.
            notifier.send(Action(
                req.symbol, f"ORDER_{req.side}",
                f"{req.side} {req.quantity} @ ₹{req.price:,.2f}" if req.price
                else f"{req.side} {req.quantity} at market",
                f"{req.reason}\nOrder id {order_id}. Verify in Kite.",
                ltp=req.price, urgency="CRITICAL",
                framework=framework), force=True)

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
        global _blocked_account
        account_wide = any(s in msg.lower() for s in _ACCOUNT_WIDE)
        if account_wide:
            _blocked_account = msg
            _blocked[key] = msg
            n_live = _count_live_positions(sb)
            logger.error(
                f"  EVERY ORDER ON THIS ACCOUNT IS NOW BLOCKED — {msg[:160]}\n"
                f"      Rejected on {req.symbol} {req.side}, but this is an account-level\n"
                f"      condition: it is not about {req.symbol}. No order for ANY symbol,\n"
                f"      in EITHER book, can succeed until it is fixed at the broker —\n"
                f"      INCLUDING EXITS. {n_live} live position(s) can currently be\n"
                f"      protected only by their resting GTT stops.\n"
                f"      Retrying cannot fix this. Fix it, then restart the daemon."
            )
        else:
            logger.error(f"  order FAILED {req.side} {req.quantity} {req.symbol}: {msg[:200]}")
        permanent = account_wide

        _log(sb, req, "BLOCKED_PERMANENT" if permanent else "FAILED", None, msg[:300], framework)
        return OrderResult(False, None, msg,
                           "BROKER_CONFIG" if permanent else "BROKER_ERROR")


def _count_live_positions(sb) -> int:
    """
    How many real positions are exposed by an account-wide order block.

    Only used to make the block message concrete — "6 live position(s) can
    currently be protected only by their resting GTT stops" is a sentence an
    operator can act on; "orders are blocked" is not. Returns -1 rather than
    raising, because a failure to COUNT must never swallow the report of the
    block itself.
    """
    try:
        rows = (sb.table("open_positions").select("symbol,mode")
                  .eq("status", "ACTIVE").execute().data or [])
        return sum(1 for r in rows if (r.get("mode") or "LIVE").upper() != "PAPER")
    except Exception:
        return -1


def _log(sb, req: OrderRequest, action: str, order_id: str | None,
         detail: str, framework: str = "SWING") -> None:
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
            # Without this, today's spend cannot be attributed, and the
            # per-framework daily caps are unenforceable.
            "framework": (framework or "SWING").upper(),
        }).execute()
    except Exception as e:
        logger.debug(f"  order log failed: {e}")
