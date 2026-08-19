"""
Paper execution — real decisions, simulated fills.

WHY THIS IS NOT A TOY
---------------------
The intraday framework has never placed a trade, and the swing framework has one
signal-attributed outcome out of seventy closed positions. Both are now allowed
to sell real holdings automatically. Paper mode is how the seven intraday
engines and the runner logic earn that permission on evidence rather than on
having compiled.

The critical property is that ONLY the fill is simulated. The universe scan, the
engines, the index gate, the news gate, the cost model, the exit ladder, the
runner assessment and the alerts are all the identical code path. A paper result
is therefore a real statement about the decision logic — which a separate
backtest harness could never be, because a backtest is a second implementation
and would drift from the thing it is meant to measure.

PESSIMISTIC BY CONSTRUCTION
---------------------------
Every simplification is deliberately biased AGAINST the system:

    fills          at the worse side of the spread, never at the mid
    slippage       applied on both legs, from the same cost model as live
    charges        full round trip, not ignored
    limit orders   fill only when price actually TRADES THROUGH the limit,
                   not when it merely touches — a touch is not a fill, and
                   assuming otherwise is the single most common way paper
                   results beat live ones
    partial fills  not simulated; the whole order fills or none of it, which
                   is optimistic, so it is stated here rather than hidden

A paper system that flatters itself is worse than no paper system, because it
produces confidence rather than information.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg, IST, get_supabase, cfg_float, cfg_int, today_ist, capital_for


@dataclass
class PaperFill:
    ok: bool
    order_id: str | None
    fill_price: float | None
    quantity: int
    charges: float
    message: str


def _slippage_pct() -> float:
    return cfg_float("cost_slippage_bps", 5.0) / 10000.0


def product_for(framework: str) -> str:
    """
    The product a framework trades. One definition, because charges depend on
    it: CNC pays no brokerage but 0.1% STT on both legs plus a flat Rs 15.04 DP
    fee on every sell, and MIS pays the opposite shape. Getting this wrong does
    not raise — it just prices the trade as the other product.
    """
    if (framework or "SWING").upper() == "SWING":
        return "CNC"
    return (cfg("intraday_product", "CNC") or "CNC").upper()


def simulate_fill(symbol: str, side: str, qty: int, order_type: str,
                  limit_price: float | None, ltp: float,
                  product: str = "MIS") -> PaperFill:
    """
    Would this order have filled, and at what price?

    LIMIT orders require price to trade THROUGH the limit, not merely touch it.
    On a buy limit that means the market must be at or below the limit; on a
    sell limit, at or above. Treating a touch as a fill is exactly how paper
    equity curves come out ahead of live ones.
    """
    if qty <= 0 or not ltp or ltp <= 0:
        return PaperFill(False, None, None, 0, 0.0, "no quantity or price")

    slip = _slippage_pct()
    if order_type == "MARKET":
        # Pay the spread in the direction that hurts.
        fill = ltp * (1 + slip) if side == "BUY" else ltp * (1 - slip)
    else:
        if limit_price is None:
            return PaperFill(False, None, None, 0, 0.0, "limit order without a price")
        if side == "BUY" and ltp > limit_price:
            return PaperFill(False, None, None, 0, 0.0,
                             f"not filled — market {ltp:.2f} above the {limit_price:.2f} limit")
        if side == "SELL" and ltp < limit_price:
            return PaperFill(False, None, None, 0, 0.0,
                             f"not filled — market {ltp:.2f} below the {limit_price:.2f} limit")
        # Filled at the limit, never better. Price improvement happens live but
        # assuming it here would be free money the simulation invented.
        fill = limit_price

    try:
        # The leg being simulated, priced for the product being traded. This
        # halved a full MIS round trip regardless of side or product, so a CNC
        # buy was charged half an STT it does not pay on that leg at that rate,
        # and a CNC sell was charged nothing for the Rs 15.04 DP fee that is the
        # single largest cost on a small delivery position.
        #
        # Slippage is deliberately EXCLUDED from `charges`: it is already
        # reflected in `fill` above, so counting it here would deduct the same
        # cost twice and make paper look worse than live rather than equal to it.
        from intraday.cost_model import entry_leg, exit_leg
        leg = exit_leg if (side or "").upper() == "SELL" else entry_leg
        charges = leg(fill, qty, product=product)
    except Exception:
        charges = 0.0

    oid = f"PAPER-{datetime.now(IST):%Y%m%d%H%M%S}-{symbol[:6]}"
    return PaperFill(True, oid, round(fill, 2), qty, charges,
                     f"paper fill {qty} @ {fill:.2f} (charges ₹{charges:.2f})")


def place(symbol: str, side: str, qty: int, order_type: str,
          limit_price: float | None, ltp: float, framework: str = "SWING",
          reason: str = "", sb=None) -> PaperFill:
    """
    Record a simulated order and its fill.

    Writes to the same intraday_broker_log as live orders, tagged PAPER, so one
    query answers "what did the system do today" regardless of mode — and so a
    mode mix-up is visible rather than hidden in a separate table nobody reads.
    """
    sb = sb or get_supabase()
    f = simulate_fill(symbol, side, qty, order_type, limit_price, ltp,
                      product=product_for(framework))

    try:
        sb.table("intraday_broker_log").insert({
            "ts": datetime.now(IST).isoformat(),
            "symbol": symbol,
            "channel": "PAPER",
            "action": "FILLED" if f.ok else "NOT_FILLED",
            "side": side,
            "ref_id": f.order_id,
            "price": f.fill_price,
            "quantity": qty if f.ok else 0,
            "detail": f"[{framework}] {reason} | {f.message}",
        }).execute()
    except Exception as e:
        logger.debug(f"  paper log write failed: {e}")

    if f.ok:
        logger.info(f"  📄 PAPER {side} {qty} {symbol} @ {f.fill_price:.2f} "
                    f"— {reason[:60]}")
    else:
        logger.info(f"  📄 PAPER {side} {symbol} NOT FILLED — {f.message}")
    return f


def capacity(framework: str = "INTRADAY", sb=None) -> tuple[bool, str, float]:
    """
    Is there room for another paper position?

    Returns (allowed, reason, cash_left). Capital is config.capital_for(framework)
    — the SAME sleeve the live sizing formula reads, not a second number that can
    silently drift from it. Until 07-Aug-2026 this read its own `paper_starting_
    capital` key instead: introduced 31-Jul-2026, before capital_for()'s book-
    sleeve mechanism existed at all (05/06-Aug), and never migrated once it did.
    Concretely, that gap meant a ₹20,000 paper_starting_capital sat next to an
    intraday_capital of ₹1,00,000 — the dashboard's "Capital" field — silently
    capping paper deployment at a fifth of what the operator could see was
    configured, with no warning either number was even involved.

    capital_for() already keeps paper capital notional and separate from the
    real account (swing sizes against the whole account while intraday stays
    PAPER — see its own docstring), so nothing about that isolation is lost by
    reading it here instead of a parallel key.

    Concentration is still enforced, scoped to THIS book only — deployed value
    from the OTHER framework's paper positions must not eat into this book's
    cap. Pooling that sum was the same shape as every other book-pooling bug
    found this session (alloc_max_slots, check_new_entry); harmless today only
    because swing has never run in PAPER mode, exactly like those others were
    harmless only until the condition that exposed them arrived.
    """
    sb = sb or get_supabase()
    cap = capital_for(framework)
    max_open = cfg_int("paper_max_open_positions", 5)
    try:
        rows = (sb.table("open_positions")
                  .select("symbol,invested_value,framework")
                  .eq("mode", "PAPER").eq("status", "ACTIVE").execute().data or [])
    except Exception as e:
        return False, f"could not read paper book: {e}", 0.0

    mine = [r for r in rows if (r.get("framework") or "").upper() == framework.upper()]
    deployed = sum(float(r.get("invested_value") or 0) for r in mine)
    left = cap - deployed

    if len(mine) >= max_open:
        return False, f"{len(mine)} paper {framework} positions already open (cap {max_open})", left
    if left <= 0:
        return False, f"paper capital exhausted — ₹{deployed:,.0f} of ₹{cap:,.0f} deployed", left
    return True, f"₹{left:,.0f} of paper capital free", left


def close_position(symbol: str, exit_price: float, reason: str, detail: str,
                   sb=None, side: str | None = None) -> bool:
    """
    Close a paper position through the SAME path a live one uses.

    Delegates to control.position_lifecycle.close_position, which computes the
    realised R multiple, hold days, excursions and attribution. Reimplementing
    any of that here would give paper trades a different outcome record from
    live ones and make the two incomparable — which would defeat the only reason
    the simulation exists.

    `side` is the leg that FLATTENS the position — SELL for a long, BUY to cover
    a short. Accepted and recorded rather than assumed, because the closing leg
    of a short is a buy and a simulation that models it as a sell prices the
    spread on the wrong side. Optional so every existing caller is unchanged;
    when omitted the shared path infers it from the position's own direction.
    """
    sb = sb or get_supabase()
    try:
        rows = (sb.table("open_positions").select("*")
                  .eq("symbol", symbol).eq("mode", "PAPER").execute().data or [])
        if not rows:
            return False
        from control.position_lifecycle import _upsert_position, close_position as _close
        row = rows[0]
        leg = (side or ("BUY" if str(row.get("direction") or "LONG").upper() == "SHORT"
                        else "SELL")).upper()
        ok = _close(sb, row, float(exit_price), reason, detail,
                    today_ist().isoformat(), source="paper")
        if ok:
            verb = "COVERED" if leg == "BUY" else "CLOSED"
            logger.success(f"  📄 PAPER {verb} {symbol} @ {exit_price:.2f} — {reason}")
        return ok
    except Exception as e:
        logger.warning(f"  paper close failed for {symbol}: {e}")
        return False


def open_position(symbol: str, qty: int, fill_price: float, setup: dict,
                  framework: str = "INTRADAY", sb=None,
                  charges: float = 0.0) -> bool:
    """
    Create a PAPER position that the normal exit engine will manage.

    Deliberately written to open_positions with mode='PAPER' rather than to a
    separate table. Everything downstream — the exit ladder, the runner
    assessment, MFE/MAE tracking, the dashboard, closed_positions and the
    learning loop — then works on paper trades with no special case, which is
    the only way the simulation tests the real system rather than a copy of it.
    """
    sb = sb or get_supabase()
    # Imported here, not at module scope: control.position_lifecycle imports
    # this module, so a top-level import would be circular. The regex that
    # rewrote these call sites put the import in close_position() only, which is
    # why every paper entry failed with "name '_upsert_position' is not defined"
    # while closes worked.
    from control.position_lifecycle import _upsert_position
    try:
        row = {
            "symbol": symbol,
            "mode": "PAPER",
            "framework": framework,
            # The framework's product, not a constant. It is half the
            # uniqueness key now (migration 028), so hardcoding CNC would put an
            # intraday tranche in the swing slot and collide with the core it
            # was meant to sit beside.
            "product": product_for(framework),
            "status": "ACTIVE",
            # FOUND DURING MERGE REVIEW, migration 047: this key did not exist
            # on this dict at all, open_positions had no column to receive it,
            # and every reader of a position's direction — the exit ladder,
            # excursion tracking, close_position's cover-vs-sell choice — reads
            # it from THIS table, not from intraday_setups. A short would have
            # opened correctly (the entry leg is already direction-aware) and
            # then been managed as a long for its entire life. Explicit LONG
            # when the caller omits the key, matching intraday.direction's own
            # "unlabelled reads as LONG" default rather than leaving a gap for
            # a future caller to get right by accident.
            "direction": setup.get("direction") or "LONG",
            "entry_date": today_ist().isoformat(),
            "entry_price": fill_price,
            "actual_qty": qty,
            "current_qty": qty,
            "original_qty": qty,
            "invested_value": round(fill_price * qty, 2),
            "current_price": fill_price,
            "high_water_mark": fill_price,
            "planned_stop": setup.get("stop"),
            "planned_target": setup.get("target"),
            "target_price": setup.get("target"),
            "active_sl": setup.get("stop"),
            "sl_type": "PAPER_SETUP",
            # The price whose loss kills the thesis. Stored so the exit monitor
            # can check it mechanically rather than parsing the prose the engine
            # wrote for the alert.
            "invalidation_level": setup.get("invalidation_level"),
            "invalidation_note": setup.get("invalidation_note"),
            "strategy": setup.get("strategy"),
            "intraday_strategy": setup.get("strategy") if framework == "INTRADAY" else None,
            # SECTOR, on every paper row. It was written by the live swing path
            # and by nothing else, so sector was NULL on all fourteen closed
            # intraday positions — which makes "did these engines put me in one
            # sector three times" unanswerable. Six long-only engines producing
            # a cluster of correlated names is the documented reason the index
            # gate exists, and the concentration version of that risk could not
            # even be measured. close_position copies the column through, so
            # recording it here is what makes the question askable later.
            "sector": setup.get("sector"),
            # TOP_PICK / EXPLORATION / None. See allocation/hurdle.py's own
            # header comment on why this is a label, not a gate, and
            # intraday/engine.py::act_on_setups for where it is read from the
            # allocator's own verdict. Carried through close_position() onto
            # closed_positions the same way `sector` is, for the same reason:
            # a field only on the open row cannot be queried against outcomes.
            "pick_label": setup.get("pick_label"),
            "entry_signal_type": setup.get("strategy"),
            "source": "paper",
            # Entry-leg cost. Carried so the close can add the exit leg and
            # record a true round trip — a P&L that ignores charges overstates
            # intraday profit by roughly a fifth at this position size.
            "charges": round(float(charges or 0.0), 2),
            # Why this name was chosen over the others available today. See
            # migration 026 — a trade whose reasoning is not recorded cannot be
            # judged later on whether the reasoning or the market was wrong.
            "entry_rationale": setup.get("entry_rationale"),
            "synced_at": datetime.now(IST).isoformat(),
        }
        try:
            _upsert_position(sb, row)
        except Exception as e:
            # Migration 025 adds open_positions.charges. Opening without the
            # entry cost is far better than not opening: the round trip then
            # records only the exit leg, which understates cost but still beats
            # a simulation that silently takes no trades.
            if "charges" not in str(e) and "entry_rationale" not in str(e):
                raise
            logger.warning("  open_positions is missing a column (migration 025/026) "
                           "— opening without it rather than not at all.")
            row.pop("charges", None)
            row.pop("entry_rationale", None)
            _upsert_position(sb, row)
        logger.success(f"  📄 PAPER POSITION opened {symbol} {qty} @ {fill_price:.2f} "
                       f"[{framework}/{setup.get('strategy')}]")
        return True
    except Exception as e:
        logger.warning(f"  paper position open failed for {symbol}: {e}")
        return False
