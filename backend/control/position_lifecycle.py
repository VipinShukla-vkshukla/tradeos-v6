"""
TradeOS v6 — Position Lifecycle Manager
========================================
The missing link between signals, the broker, and outcomes.

WHAT IT REPLACES
  brain/position_manager.py  — deleted. It wrote columns that did not exist
                               (id, entry_price_actual, stop_loss,
                               regime_at_entry, telegram_msg_id, quantity,
                               open_position_id), used status='OPEN'/'CLOSED'
                               while every reader filters status='ACTIVE', and
                               read signal_log.current_price which was not a
                               column. Its Telegram keyboards had no receiver.

RESPONSIBILITIES
  1. RECONCILE   Kite holdings -> open_positions. Opens rows for new holdings,
                 closes rows for vanished ones, adjusts quantity on partial
                 fills. Kite is authoritative; every divergence is logged to
                 position_reconcile_log rather than silently absorbed.
  2. ATTRIBUTE   Links each position to the signal that produced it
                 (signal_id, signal_date, entry_signal_type) and freezes the
                 trade plan at entry. closed_positions.signal_date was NULL on
                 all 70 historical rows, which is the sole reason
                 signal_outcomes holds 2 records and the Brain has learned
                 nothing.
  3. MANAGE      Applies the exit policy: partial booking at N×R, stop to
                 breakeven, delayed trailing, hard target, time stop.
  4. CLOSE       Writes closed_positions with a machine-generated exit_reason,
                 realised R-multiple, hold days and excursion stats.

EXIT POLICY, AND WHY IT IS SHAPED THIS WAY
------------------------------------------
Measured over the 70 realised trades:
    in-trade MFE +3.40%  vs  in-trade MAE -3.43%     (roughly symmetric)
    capture ratio (realised / in-trade MFE): median 3%
    46% of trades never achieved even +2% at any point
    exit_reason 'TRAIL SL HIT' = 37/70, average P&L -0.40%

Two conclusions drove the design:

  a) Trailing from entry is what compressed the average winner to +3.89%.
     A stop trailing 6% below the high-water mark from day one gets hit during
     ordinary retracement long before a 1-3 week thesis plays out. Trailing is
     now DELAYED until the position is up exit_trail_after_r (2R).

  b) Exits are NOT the main problem and this policy is not asked to rescue bad
     entries. Simulation showed every exit variant is negative over all 70
     trades, but on the subset whose entries worked (n=44) the EXISTING policy
     already returns +1.26%. So the policy targets a modest, evidence-backed
     improvement (+1.50% on that subset, +2.81% on trades reaching +5%) and
     leaves the heavy lifting to the entry gate fix in generate_signals.

Everything is expressed in R (multiples of initial risk), not fixed percent, so
a 2.5% ATR stock and a 6% ATR stock are managed on the same logic.

SAFETY
  Never places or cancels an order. Order placement stays in
  control/execution_engine.py behind its own autonomy-phase gate. This module
  only records state and raises alerts.
"""

from __future__ import annotations

import sys
import json
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, today_ist, IST, DRY_RUN,
    is_kill_switch_active, cfg, cfg_bool, cfg_float, cfg_int,
    TOTAL_CAPITAL,
)


# ─────────────────────────────────────────────────────────────────────────────
# EXIT POLICY
# ─────────────────────────────────────────────────────────────────────────────

def load_exit_policy() -> dict:
    """
    NOTE ON trail_r (was trail_pct).

    The trail was originally a PERCENTAGE below the high-water mark. That does
    not compose with R-based targets and rendered the trailing branch
    unreachable. Worked through: entry 100, planned stop 95 (risk 5%), so
    1R=+5% and the 3R target sits at 115. A 10% trail gives:
        HWM 110 (2.0R) -> trail 99.00   below the breakeven stop of 100 — ignored
        HWM 114 (2.8R) -> trail 102.60  only 0.5R locked
        HWM 115 (3.0R) -> hard target fires first
    The trail never bound, at any price, for any stock whose risk was under
    ~10%. Expressing it in R fixes that and makes it volatility-adaptive:
        HWM 110 (2.0R) -> trail 102.50  0.5R locked
        HWM 113 (2.6R) -> trail 105.50  1.1R locked
    A percentage trail also silently means "tighter for volatile stocks, looser
    for calm ones", which is backwards.
    """
    return {
        "partial_book_r":    cfg_float("exit_partial_book_r",    1.5),
        "partial_book_pct":  cfg_float("exit_partial_book_pct",  50.0),
        "move_to_breakeven": cfg_bool("exit_move_to_breakeven",  True),
        "trail_r":           cfg_float("exit_trail_r",           1.5),
        "trail_after_r":     cfg_float("exit_trail_after_r",     2.0),
        "target_r":          cfg_float("exit_target_r",          3.0),
        "time_stop_days":    cfg_int("exit_time_stop_days",      15),
        "time_stop_min_r":   cfg_float("exit_time_stop_min_r",   0.5),
    }


def _sell_price_today(symbol: str) -> float | None:
    """
    Average price of today's SELL trades for a symbol, from the broker.

    The realised price of a partial is a fact the broker knows and we should
    never guess: using the current LTP instead would record a profit that was
    never earned, on the exact trades the exit policy is judged by. LTP is only
    a last resort, and the caller logs when it falls back.

    Returns None when the broker is unreachable or has no matching fill.
    """
    try:
        from kite import kite_client
        trades = kite_client.fetch_trades() or []
    except Exception:
        return None

    today = datetime.now(IST).date().isoformat()
    qty = value = 0.0
    for t in trades:
        if (t.get("symbol") or t.get("tradingsymbol")) != symbol:
            continue
        if (t.get("transaction_type") or "").upper() != "SELL":
            continue
        ts = str(t.get("timestamp") or t.get("fill_timestamp") or "")
        if ts and not ts.startswith(today):
            continue
        q = float(t.get("quantity") or 0)
        p = float(t.get("average_price") or t.get("price") or 0)
        if q and p:
            qty += q
            value += q * p
    return round(value / qty, 2) if qty else None


def evaluate_exit(pos: dict, ltp: float, sessions_held: int, policy: dict) -> dict:
    """
    Decide what should happen to one open position at the current price.

    Returns {action, reason, detail, new_sl, book_qty}. Pure — no I/O, no
    mutation — so it is directly testable and the caller owns all writes.

    action is one of:
        HOLD          nothing to do
        BOOK_PARTIAL  first target reached, take exit_partial_book_pct off
        TRAIL_SL      ratchet the stop up
        EXIT_TARGET   hard target reached
        EXIT_STOP     stop breached
        EXIT_TIME     time stop, position going nowhere
    """
    entry = float(pos.get("entry_price") or 0)
    stop0 = float(pos.get("planned_stop") or 0)
    sl    = float(pos.get("active_sl") or stop0 or 0)

    if not entry or not ltp:
        return {"action": "HOLD", "reason": "missing_price", "detail": "",
                "new_sl": None, "book_qty": 0}

    # Initial risk per share. Everything below is measured against it.
    risk = entry - stop0 if stop0 and stop0 < entry else None
    if not risk or risk <= 0:
        # No usable planned_stop (a pre-migration or manually created row).
        # Fall back to percent-based management so the position is not
        # unmanaged, but say so in the detail string.
        risk = entry * 0.05
        fallback = True
    else:
        fallback = False

    gain_r   = (ltp - entry) / risk
    gain_pct = (ltp - entry) / entry * 100.0

    # ── 1. Stop breach — always checked first ────────────────────────────────
    if sl and ltp <= sl:
        return {
            "action": "EXIT_STOP",
            "reason": "STOP_LOSS_HIT" if not pos.get("trail_activated") else "TRAIL_SL_HIT",
            "detail": f"ltp {ltp:.2f} <= sl {sl:.2f} ({gain_pct:+.2f}%, {gain_r:+.2f}R)",
            "new_sl": None, "book_qty": 0,
        }

    # ── 2. Hard target — EXIT, or run it ─────────────────────────────────────
    # The old rule exited unconditionally at 3R, which treats every trade the
    # same at exactly the moment they stop being the same. Over the 70 realised
    # trades the median capture ratio was 3% — nearly all of the favourable
    # excursion given back — and cutting winners at a fixed multiple guarantees
    # that never improves.
    #
    # A position here has already booked half at 1.5R with its stop at or above
    # breakeven, so running risks open PROFIT, never capital. That asymmetry is
    # what makes the discretion safe; the loss-side rules below stay strict and
    # unconditional.
    if gain_r >= policy["target_r"]:
        sig = (policy.get("_trend_ctx") or {}).get(pos.get("symbol")) or {}
        try:
            from control.exit_rules import assess_trend, target_decision
            tq = assess_trend(sig, pos)
            return target_decision(pos, gain_r, tq, policy)
        except Exception as e:
            # A failure to assess must not become a failure to bank a 3R gain.
            logger.warning(f"  trend assessment failed for {pos.get('symbol')}: {e}")
            return {
                "action": "EXIT_TARGET", "reason": "TARGET_HIT",
                "detail": f"{gain_r:.2f}R >= {policy['target_r']}R ({gain_pct:+.2f}%)",
                "new_sl": None, "book_qty": 0,
            }

    # ── 2b. Thesis broken while in profit ────────────────────────────────────
    # The trail is a price mechanism and only reacts after the damage. This
    # reacts to the reason while there is still profit to protect.
    if gain_r >= 1.0:
        sig = (policy.get("_trend_ctx") or {}).get(pos.get("symbol")) or {}
        if sig:
            try:
                from control.exit_rules import assess_trend, deterioration_check
                d = deterioration_check(pos, gain_r, assess_trend(sig, pos))
                if d:
                    return d
            except Exception as e:
                logger.debug(f"  deterioration check skipped: {e}")

    # ── 3. Partial booking at the first meaningful multiple ──────────────────
    already_booked = int(pos.get("partial_booked_qty") or 0) > 0
    current_qty    = int(pos.get("current_qty") or pos.get("actual_qty") or 0)
    if not already_booked and gain_r >= policy["partial_book_r"] and current_qty > 1:
        book_qty = max(1, int(current_qty * policy["partial_book_pct"] / 100.0))
        return {
            "action": "BOOK_PARTIAL",
            "reason": "PARTIAL_TARGET",
            "detail": (f"{gain_r:.2f}R >= {policy['partial_book_r']}R — book "
                       f"{book_qty}/{current_qty} @ {ltp:.2f} ({gain_pct:+.2f}%)"),
            "new_sl": entry if policy["move_to_breakeven"] else None,
            "book_qty": book_qty,
        }

    # ── 4. Trailing — DELAYED until trail_after_r ────────────────────────────
    # Trailing from entry is what produced the -0.40% average on 37 TRAIL SL HIT
    # exits: an ordinary retracement takes out the stop before the 1-3 week
    # thesis resolves.
    if gain_r >= policy["trail_after_r"]:
        hwm      = max(float(pos.get("high_water_mark") or entry), ltp)
        # Trail a fixed number of R below the high-water mark, NOT a percentage
        # (see load_exit_policy for why percentage trailing never bound).
        trail_sl = hwm - policy["trail_r"] * risk
        # Never let the trail loosen an existing stop.
        if trail_sl > sl:
            locked_r = (trail_sl - entry) / risk
            return {
                "action": "TRAIL_SL",
                "reason": "TRAIL_UPDATE",
                "detail": (f"{gain_r:.2f}R >= {policy['trail_after_r']}R — "
                           f"sl {sl:.2f} -> {trail_sl:.2f} "
                           f"(hwm {hwm:.2f}, locks {locked_r:+.2f}R)"),
                "new_sl": round(trail_sl, 2), "book_qty": 0,
            }

    # ── 5. Time stop — only on positions that are NOT working ────────────────
    if (sessions_held >= policy["time_stop_days"]
            and gain_r < policy["time_stop_min_r"]):
        return {
            "action": "EXIT_TIME",
            "reason": "TIME_EXIT",
            "detail": (f"{sessions_held} sessions held, {gain_r:+.2f}R "
                       f"< {policy['time_stop_min_r']}R — capital better deployed"),
            "new_sl": None, "book_qty": 0,
        }

    return {
        "action": "HOLD", "reason": "holding",
        "detail": (f"{gain_r:+.2f}R ({gain_pct:+.2f}%)"
                   + (" [no planned_stop — 5% fallback risk]" if fallback else "")),
        "new_sl": None, "book_qty": 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL ATTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def find_originating_signal(sb, symbol: str, entry_date: str,
                            lookback_days: int = 10) -> dict:
    """
    Locate the signal that produced this entry.

    Searches backwards from entry_date for the most recent actionable signal on
    the symbol. Falls back to any signal (including WATCH) so that a
    discretionary entry still records what the system was saying at the time —
    which is itself valuable: it tells you whether your manual overrides beat
    the system.

    Populating this is the entire reason signal_outcomes exists. All 70
    historical closed_positions rows have signal_date NULL, so no outcome has
    ever been attributable to a signal.
    """
    try:
        start = str(date.fromisoformat(str(entry_date)[:10]) - timedelta(days=lookback_days))
    except Exception:
        return {}

    base = ("id,date,symbol,signal_type,signal_subtype,strategy,sector,"
            "company_name,score_adjusted,regime,sector_rank_at_entry,"
            "entry_timing_type,lifecycle,expected_r_msl,"
            "planned_stop,planned_target,planned_risk_pct,ai_conviction")

    for types in (["PRIME_SETUP", "BREAKOUT_SETUP", "REENTRY_SETUP",
                   "BUY_CANDIDATE", "STAGED_ENTRY", "MOMENTUM_CONTINUATION"], None):
        try:
            q = (sb.table("signal_log").select(base)
                   .eq("symbol", symbol)
                   .gte("date", start).lte("date", str(entry_date)[:10]))
            if types:
                q = q.in_("signal_type", types)
            rows = q.order("date", desc=True).limit(1).execute().data
            if rows:
                return rows[0]
        except Exception as e:
            logger.debug(f"  signal lookup failed for {symbol}: {e}")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILIATION
# ─────────────────────────────────────────────────────────────────────────────

def _log_reconcile(sb, trade_date, symbol, action, **kw):
    if DRY_RUN:
        return
    try:
        sb.table("position_reconcile_log").insert({
            "trade_date": trade_date, "symbol": symbol, "action": action,
            "db_qty":  kw.get("db_qty"),  "broker_qty":   kw.get("broker_qty"),
            "db_price": kw.get("db_price"), "broker_price": kw.get("broker_price"),
            "detail":  kw.get("detail", ""), "resolved": kw.get("resolved", True),
        }).execute()
    except Exception as e:
        logger.debug(f"  reconcile log write failed: {e}")


def open_position_from_holding(sb, holding: dict, trade_date: str) -> bool:
    """Create an open_positions row for a Kite holding the DB does not know about."""
    sym   = holding["symbol"]
    qty   = holding["total_quantity"]
    entry = holding["average_price"]

    sig = find_originating_signal(sb, sym, trade_date)

    # Trade plan: prefer the levels the signal was validated against. Only if
    # the signal has none (pre-migration row, or a discretionary entry with no
    # signal at all) do we derive a stop from live ATR.
    planned_stop   = sig.get("planned_stop")
    planned_target = sig.get("planned_target")

    if not planned_stop:
        try:
            from analysis.risk_model import compute_trade_levels
            sd = (sb.table("stock_data_daily")
                    .select("atr_14,supertrend").eq("symbol", sym)
                    .order("date", desc=True).limit(1).execute().data)
            atr = float(sd[0].get("atr_14") or 0) if sd else 0
            if atr > 0:
                lv = compute_trade_levels(
                    entry_price=entry, atr_abs=atr, anchor_price=entry,
                    structure_stop=float(sd[0].get("supertrend") or 0) or None,
                    regime=sig.get("regime") or "NEUTRAL")
                if lv.valid:
                    planned_stop, planned_target = lv.stop, lv.target
        except Exception as e:
            logger.warning(f"  {sym}: could not derive trade plan: {e}")

    row = {
        "symbol":            sym,
        "company_name":      sig.get("company_name"),
        "sector":            sig.get("sector"),
        "strategy":          sig.get("strategy") or sig.get("signal_type"),
        "entry_date":        trade_date,
        "entry_price":       entry,
        "kite_avg_price":    entry,
        "actual_qty":        qty,
        "original_qty":      qty,
        "current_qty":       qty,
        "kite_qty":          qty,
        "invested_value":    round(qty * entry, 2),
        "current_price":     holding.get("last_price") or entry,
        "high_water_mark":   max(entry, holding.get("last_price") or entry),
        "status":            "ACTIVE",
        "source":            "kite",
        # Frozen trade plan
        "planned_stop":      planned_stop,
        "planned_target":    planned_target,
        "active_sl":         planned_stop,
        "target_price":      planned_target,
        "sl_type":           "ATR_PLANNED" if planned_stop else None,
        "expected_r_at_entry": sig.get("expected_r_msl"),
        # Attribution — the whole point
        "signal_id":         sig.get("id"),
        "signal_date":       sig.get("date"),
        "signal_subtype":    sig.get("signal_subtype"),
        "entry_signal_type": sig.get("signal_type"),
        "entry_timing_type": sig.get("entry_timing_type"),
        "lifecycle_at_entry": sig.get("lifecycle"),
        "regime_at_entry":   sig.get("regime"),
        "sector_rank_at_entry": sig.get("sector_rank_at_entry"),
        "max_favorable_excursion": 0.0,
        "max_adverse_excursion":   0.0,
        "reconcile_status":  "SYNCED",
        "last_reconciled_at": datetime.now(IST).isoformat(),
        "synced_at":         datetime.now(IST).isoformat(),
    }

    if DRY_RUN:
        logger.info(f"[DRY RUN] would OPEN {sym} qty={qty} @ {entry:.2f} "
                    f"sl={planned_stop} tgt={planned_target} signal={sig.get('id')}")
        return True

    try:
        sb.table("open_positions").upsert(row, on_conflict="symbol").execute()
        logger.success(
            f"  OPENED {sym:<12} qty={qty:<5} @ Rs.{entry:.2f}  "
            f"sl={planned_stop or '-'}  tgt={planned_target or '-'}  "
            f"signal={'#' + str(sig.get('id')) if sig.get('id') else 'NONE (discretionary)'}"
        )
        _log_reconcile(sb, trade_date, sym, "OPENED", broker_qty=qty,
                       broker_price=entry,
                       detail=f"signal_id={sig.get('id')} type={sig.get('signal_type')}")
        return True
    except Exception as e:
        logger.error(f"  {sym}: open_positions insert failed: {e}")
        return False


def close_position(sb, pos: dict, exit_price: float, exit_reason: str,
                   detail: str, trade_date: str, source: str = "kite") -> bool:
    """Move an open position into closed_positions with full outcome metrics."""
    sym   = pos["symbol"]
    entry = float(pos.get("entry_price") or 0)
    qty   = int(pos.get("current_qty") or pos.get("actual_qty") or 0)
    if not entry or not exit_price:
        logger.warning(f"  {sym}: cannot close — entry={entry} exit={exit_price}")
        return False

    # Partial bookings already realised are folded into total P&L so the
    # recorded outcome reflects the whole trade, not just the final tranche.
    booked_qty   = int(pos.get("partial_booked_qty") or 0)
    booked_price = float(pos.get("partial_booked_price") or 0)
    booked_pnl   = (booked_price - entry) * booked_qty if booked_qty and booked_price else 0.0

    final_pnl    = (exit_price - entry) * qty
    # GROSS, deliberately. 70 trades closed before charges were recorded and
    # their costs cannot be reconstructed; redefining this column would make old
    # and new rows incomparable while looking identical. Charges are recorded
    # separately and netted where they are known.
    realized_pnl = round(final_pnl + booked_pnl, 2)

    # Round trip = the entry leg already carried on the position, plus the exit
    # leg priced now. Slippage is excluded on both: for paper it is inside the
    # fill price, for live it is inside the actual traded price.
    entry_charges = float(pos.get("charges") or 0.0)
    exit_charges = 0.0
    try:
        from intraday.cost_model import round_trip
        rt = round_trip(float(exit_price), int(qty))
        statutory = rt.brokerage + rt.stt + rt.exchange + rt.sebi + rt.stamp + rt.gst
        exit_charges = round(statutory / 2.0, 2)
    except Exception:
        exit_charges = 0.0
    total_charges = round(entry_charges + exit_charges, 2) if (entry_charges or exit_charges) else None
    total_qty    = qty + booked_qty
    invested     = entry * total_qty
    pnl_pct      = round(realized_pnl / invested * 100, 3) if invested else 0.0

    stop0 = float(pos.get("planned_stop") or 0)
    risk  = entry - stop0 if stop0 and stop0 < entry else None
    r_mult = round((realized_pnl / total_qty) / risk, 3) if (risk and total_qty) else None

    hold_days = None
    try:
        hold_days = (date.fromisoformat(trade_date)
                     - date.fromisoformat(str(pos["entry_date"])[:10])).days
    except Exception:
        pass

    closed = {
        "symbol":           sym,
        "company_name":     pos.get("company_name"),
        "sector":           pos.get("sector"),
        "strategy":         pos.get("strategy"),
        # Carry mode and framework through. Both columns DEFAULT to LIVE/SWING,
        # so omitting them silently recorded every paper intraday trade as a
        # live swing one — contaminating the real performance record with
        # simulated results, which is the one thing paper mode must never do.
        "mode":             pos.get("mode") or "LIVE",
        "framework":        pos.get("framework") or "SWING",
        "product":          pos.get("product") or "CNC",
        "intraday_strategy": pos.get("intraday_strategy"),
        "entry_date":       pos.get("entry_date"),
        "entry_price":      entry,
        "actual_qty":       total_qty,
        "proposed_qty":     pos.get("proposed_qty"),
        "invested_value":   round(invested, 2),
        "exit_date":        trade_date,
        "exit_price":       exit_price,
        "exit_value":       round(exit_price * qty + booked_price * booked_qty, 2),
        "realized_pnl":     realized_pnl,
        # Round-trip cost. NULL rather than 0.0 when unknown, because "no
        # charges" and "charges not recorded" must not read the same — a zero
        # here would silently claim a free trade.
        "charges":          total_charges,
        "pnl_pct":          pnl_pct,
        "exit_reason":      exit_reason,
        "exit_reason_detail": detail,
        "hold_days":        hold_days,
        "r_multiple":       r_mult,
        "high_water_mark":  pos.get("high_water_mark"),
        "max_favorable_excursion": pos.get("max_favorable_excursion"),
        "max_adverse_excursion":   pos.get("max_adverse_excursion"),
        # Attribution — carried through so post_trade_analysis and
        # performance_tracker can finally join outcomes back to signals.
        "signal_id":        pos.get("signal_id"),
        "signal_date":      pos.get("signal_date"),
        "entry_signal_type": pos.get("entry_signal_type"),
        "entry_timing_type": pos.get("entry_timing_type"),
        "lifecycle_at_entry": pos.get("lifecycle_at_entry"),
        "regime_at_entry":  pos.get("regime_at_entry"),
        "sector_rank_at_entry": pos.get("sector_rank_at_entry"),
        "reentry_mode":     pos.get("reentry_mode"),
        "expected_r_at_entry":     pos.get("expected_r_at_entry"),
        "planned_stop_at_entry":   pos.get("planned_stop"),
        "planned_target_at_entry": pos.get("planned_target"),
        "partial_bookings": pos.get("partial_bookings"),
        "source":           source,
    }

    if DRY_RUN:
        logger.info(f"[DRY RUN] would CLOSE {sym} @ {exit_price:.2f} "
                    f"{pnl_pct:+.2f}% ({r_mult:+.2f}R) — {exit_reason}")
        return True

    try:
        try:
            sb.table("closed_positions").insert(closed).execute()
        except Exception as e:
            # Migration 025 adds closed_positions.charges. If the code is ahead
            # of the schema, close the trade WITHOUT the cost figure rather than
            # not at all: a position that cannot be recorded as closed stays
            # open forever in the book, and losing one optional column is
            # trivially recoverable next to that.
            if "charges" not in str(e):
                raise
            logger.warning("  closed_positions.charges is missing — apply "
                           "migration 025. Closing without the cost figure.")
            sb.table("closed_positions").insert(
                {k: v for k, v in closed.items() if k != "charges"}).execute()
        sb.table("open_positions").delete().eq("symbol", sym).execute()
        icon = "🟢" if realized_pnl > 0 else "🔴"
        logger.success(
            f"  {icon} CLOSED {sym:<12} @ Rs.{exit_price:.2f}  "
            f"{pnl_pct:+.2f}%  ({r_mult if r_mult is not None else '?'}R)  "
            f"{hold_days}d  — {exit_reason}"
        )
        _log_reconcile(sb, trade_date, sym, "CLOSED", db_qty=qty,
                       broker_qty=0, broker_price=exit_price, detail=detail)
        return True
    except Exception as e:
        logger.error(f"  {sym}: close failed: {e}")
        return False


def reconcile_with_broker(sb, trade_date: str) -> dict:
    """
    Kite holdings are the truth. Bring open_positions into agreement.

    Four divergence classes:
      in Kite, not in DB      -> OPENED        (entry made outside the framework,
                                                or the framework's own fill)
      in DB, not in Kite      -> CLOSED        (position was sold)
      qty(Kite) < qty(DB)     -> QTY_REDUCED   (partial exit)
      qty(Kite) > qty(DB)     -> QTY_INCREASED (added to the position)
    """
    from kite.kite_client import fetch_holdings, is_available

    if not is_available():
        logger.info("  Kite unavailable — skipping broker reconciliation "
                    "(Telegram-reported positions remain authoritative)")
        return {"status": "no_broker", "opened": 0, "closed": 0, "adjusted": 0}

    holdings = fetch_holdings()
    hold_map = {h["symbol"]: h for h in holdings}

    # PAPER positions are excluded, and this is not a refinement.
    #
    # Broker holdings are the truth for positions the broker HAS. A simulated
    # position is deliberately absent from Kite, so the "in DB, not in Kite ->
    # CLOSED" rule reads every paper trade as sold and closes it at its entry
    # price for a flat 0.00% — silently destroying the open paper book on the
    # first reconcile after it was created. Three intraday paper positions were
    # wiped exactly this way before the filter existed.
    #
    # Paper positions are closed by the exit engine and by square_off_paper(),
    # both of which know they are simulated.
    db_rows = [r for r in ((sb.table("open_positions").select("*")
                              .eq("status", "ACTIVE").execute().data) or [])
               if (r.get("mode") or "LIVE").upper() != "PAPER"]

    # A CNC BUY FILLED TODAY IS NOT IN holdings() YET.
    #
    # Kite moves a delivery purchase into holdings on T+1. Until then it lives
    # only in positions(), so "in DB, not in Kite" is true of every position the
    # system opened this morning — and this function would close all of them as
    # BROKER_EXIT at their entry price, minutes after buying them.
    #
    # CIPLA, ETERNAL and GABRIEL were bought at 14:28 today and appear in
    # positions() with product=CNC while holdings() shows only BHEL. Reconciling
    # without this would have wiped three real positions and left the account
    # holding stock the book had no record of.
    try:
        from kite.kite_client import fetch_positions
        day_qty = {}
        for p in (fetch_positions().get("net") or []):
            q = int(p.get("quantity") or 0)
            if q > 0:
                day_qty[p.get("tradingsymbol")] = q
    except Exception as e:
        # Cannot see today's fills -> cannot safely decide anything is gone.
        logger.warning(f"  reconcile: day positions unavailable ({e}) — "
                       f"skipping to avoid closing settled-tomorrow buys")
        return {"status": "no_day_positions", "opened": 0, "closed": 0, "adjusted": 0}

    for sym, q in day_qty.items():
        if sym not in hold_map:
            # Same shape fetch_holdings() returns, total_quantity included —
            # callers below read that key and a partial dict would raise here
            # rather than at the boundary where it was built.
            hold_map[sym] = {"symbol": sym, "quantity": q, "t1_quantity": 0,
                             "total_quantity": q, "average_price": 0.0,
                             "last_price": 0.0, "product": "CNC",
                             "pnl": 0.0, "close_price": 0.0, "day_change_pct": 0.0}
    db_map  = {r["symbol"]: r for r in db_rows}

    logger.info(f"  Reconcile: broker={len(hold_map)} holdings | db={len(db_map)} open")

    opened = closed = adjusted = 0

    # ── In Kite, not in DB ───────────────────────────────────────────────────
    for sym, h in hold_map.items():
        if sym not in db_map:
            if open_position_from_holding(sb, h, trade_date):
                opened += 1

    # ── In DB, not in Kite -> sold ───────────────────────────────────────────
    if cfg_bool("position_auto_close", True):
        from kite.kite_client import fetch_trades
        trades = fetch_trades()
        sell_map = {t["symbol"]: t for t in trades if t["transaction_type"] == "SELL"}

        for sym, pos in db_map.items():
            if sym in hold_map:
                continue
            # Exact fill price when the sale happened today; otherwise fall back
            # to the last known price and say so, rather than inventing one.
            sale = sell_map.get(sym)
            if sale:
                exit_price, detail = sale["price"], f"Kite SELL fill at {sale['timestamp']}"
            else:
                exit_price = float(pos.get("current_price") or pos.get("entry_price") or 0)
                detail = ("no same-day SELL trade found — exit price approximated "
                          "from last known price; Kite exposes trades for the "
                          "current day only")
            if exit_price:
                if close_position(sb, pos, exit_price,
                                  pos.get("exit_signal") or "BROKER_EXIT",
                                  detail, trade_date):
                    closed += 1

    # ── Backfill the trade plan on pre-existing positions ────────────────────
    # A position that already existed in open_positions (entered manually, or
    # created before migration 003) never passes through
    # open_position_from_holding, so planned_stop / planned_target stay NULL.
    # evaluate_exit then falls back to a generic 5%-of-entry risk assumption:
    # the position is nominally managed but every R multiple, the partial-book
    # trigger and the trail are computed off a number nobody chose.
    # Observed live: both open positions had sl/tgt/R/signal_id all NULL after
    # a successful reconcile.
    for sym, pos in db_map.items():
        if pos.get("planned_stop"):
            continue
        h = hold_map.get(sym)
        entry = float(pos.get("entry_price") or (h or {}).get("average_price") or 0)
        if not entry:
            continue
        sig = find_originating_signal(sb, sym, str(pos.get("entry_date") or trade_date))
        stop, target = sig.get("planned_stop"), sig.get("planned_target")
        if not stop:
            try:
                from analysis.risk_model import compute_trade_levels
                sd = (sb.table("stock_data_daily")
                        .select("atr_14,supertrend").eq("symbol", sym)
                        .order("date", desc=True).limit(1).execute().data)
                atr = float(sd[0].get("atr_14") or 0) if sd else 0
                if atr > 0:
                    lv = compute_trade_levels(
                        entry_price=entry, atr_abs=atr, anchor_price=entry,
                        structure_stop=float(sd[0].get("supertrend") or 0) or None,
                        regime=sig.get("regime") or "NEUTRAL")
                    if lv.valid:
                        stop, target = lv.stop, lv.target
            except Exception as e:
                logger.warning(f"  {sym}: trade-plan backfill failed: {e}")
        if not stop:
            continue

        patch = {
            "planned_stop":   stop,
            "planned_target": target,
            "target_price":   pos.get("target_price") or target,
            "sl_type":        pos.get("sl_type") or "ATR_BACKFILLED",
            # Only seed active_sl if nothing is protecting the position yet —
            # never loosen a stop the operator has already tightened.
            "active_sl":      pos.get("active_sl") or stop,
            "signal_id":      pos.get("signal_id")   or sig.get("id"),
            "signal_date":    pos.get("signal_date") or sig.get("date"),
            "entry_signal_type":  pos.get("entry_signal_type")  or sig.get("signal_type"),
            "regime_at_entry":    pos.get("regime_at_entry")    or sig.get("regime"),
            "high_water_mark":    pos.get("high_water_mark")    or entry,
        }
        if DRY_RUN:
            logger.info(f"[DRY RUN] would backfill plan for {sym}: sl={stop} tgt={target}")
        else:
            try:
                sb.table("open_positions").update(patch).eq("symbol", sym).execute()
                logger.success(
                    f"  PLAN BACKFILLED {sym:<12} entry={entry:.2f} "
                    f"sl={stop} tgt={target} "
                    f"signal={'#' + str(sig.get('id')) if sig.get('id') else 'none'}"
                )
                _log_reconcile(sb, trade_date, sym, "PLAN_BACKFILL",
                               db_price=entry,
                               detail=f"stop={stop} target={target}")
            except Exception as e:
                logger.warning(f"  {sym}: plan backfill write failed: {e}")

    # ── Backfill identity fields from the originating signal ─────────────────
    # Deliberately a SEPARATE pass, not part of the block above. That one exits
    # early on `if pos.get("planned_stop"): continue`, so the moment a position
    # has a stop it can never gain anything else — PPLPHARMA carried
    # signal_id=5038 and strategy=None indefinitely, and the digest rendered it
    # as "PPLPHARMA [None] [None]" beside "BHEL [CTL+MOM+SEC]".
    #
    # Cosmetic in the alert, but not only cosmetic: strategy is what attributes
    # a realised outcome back to the engine that produced it. A position that
    # closes with strategy NULL cannot be credited to CTL, SBS or anything else,
    # so the engine scoring never learns from it.
    # Candidate fields, intersected with what open_positions ACTUALLY has.
    # db_map rows come from select("*"), so their keys are the live schema —
    # deriving the list from them beats hardcoding it, which is how `industry`
    # (a signal_log column that open_positions does not have) got in here and
    # made PostgREST reject the whole update.
    _IDENTITY_CANDIDATES = ("strategy", "signal_subtype", "sector", "company_name")
    _sample = next(iter(db_map.values()), {})
    _IDENTITY = tuple(f for f in _IDENTITY_CANDIDATES if f in _sample)
    for sym, pos in db_map.items():
        missing = [f for f in _IDENTITY if not pos.get(f)]
        if not missing or not pos.get("signal_id"):
            continue
        try:
            sig_rows = (sb.table("signal_log").select(",".join(("id",) + _IDENTITY))
                          .eq("id", pos["signal_id"]).limit(1).execute().data) or []
            if not sig_rows:
                continue
            patch = {f: sig_rows[0].get(f) for f in missing if sig_rows[0].get(f)}
            if not patch:
                continue
            if DRY_RUN:
                logger.info(f"[DRY RUN] would backfill identity for {sym}: {patch}")
                continue
            sb.table("open_positions").update(patch).eq("symbol", sym).execute()
            logger.success(f"  IDENTITY BACKFILLED {sym:<12} "
                           + ", ".join(f"{k}={v}" for k, v in patch.items())
                           + f" (from signal #{pos['signal_id']})")
        except Exception as e:
            logger.warning(f"  {sym}: identity backfill failed: {e}")

    # ── Quantity drift ───────────────────────────────────────────────────────
    for sym, pos in db_map.items():
        h = hold_map.get(sym)
        if not h:
            continue
        db_qty, br_qty = int(pos.get("current_qty") or 0), h["total_quantity"]
        if db_qty == br_qty:
            # Agreement is a RESULT and has to be recorded as one. Skipping the
            # write left whatever drift status was set on the last mismatch
            # sitting on the row forever, so a position that reconciled cleanly
            # weeks ago still reported "QTY_INCREASED — Kite shows 15, book
            # shows 15" on the dashboard: a broker alarm contradicted by the
            # very numbers printed next to it.
            if pos.get("reconcile_status") != "MATCHED" and not DRY_RUN:
                try:
                    sb.table("open_positions").update({
                        "reconcile_status":   "MATCHED",
                        "last_reconciled_at": datetime.now(IST).isoformat(),
                    }).eq("symbol", sym).execute()
                except Exception as e:
                    logger.warning(f"  {sym}: reconcile clear failed: {e}")
            continue
        action = "QTY_REDUCED" if br_qty < db_qty else "QTY_INCREASED"

        patch = {
            "current_qty":    br_qty,
            "kite_qty":       br_qty,
            "actual_qty":     br_qty,
            "kite_avg_price": h["average_price"],
            "invested_value": round(br_qty * h["average_price"], 2),
            "reconcile_status": action,
            "last_reconciled_at": datetime.now(IST).isoformat(),
        }

        # A reduction IS the partial the exit engine asked for, arriving after
        # you executed it. Nothing recorded it: BOOK_PARTIAL wrote only the
        # instruction, and this branch only shrank the quantity. But
        # close_position READS partial_booked_qty/price to fold realised partial
        # profit into the final outcome — so booking 7 of 15 into a winner and
        # closing the rest recorded the P&L of 8 shares and silently discarded
        # the profit on the 7. The learning loop then studied a smaller win than
        # actually happened, on the trades the policy is designed to produce.
        if action == "QTY_REDUCED":
            sold = db_qty - br_qty
            prior_qty = int(pos.get("partial_booked_qty") or 0)
            # Broker fill first; the holding's last_price only as a fallback,
            # which the else-branch below reports rather than hides.
            price = _sell_price_today(sym) or float(h.get("last_price") or 0) or None
            if price:
                prior_price = float(pos.get("partial_booked_price") or 0)
                # Weighted average across multiple partials, so a second
                # reduction does not overwrite the first one's price.
                total_qty = prior_qty + sold
                patch["partial_booked_qty"] = total_qty
                patch["partial_booked_price"] = round(
                    ((prior_price * prior_qty) + (price * sold)) / total_qty, 2)
                patch["partial_booked_at"] = datetime.now(IST).isoformat()
                logger.info(f"  PARTIAL RECORDED {sym}: {sold} sh @ ~{price:.2f} "
                            f"(cumulative {total_qty})")
            else:
                logger.warning(
                    f"  {sym}: {sold} sh left the account but no sell price could be "
                    f"determined — partial profit will be missing from the closed "
                    f"outcome. Check Kite trades for today."
                )

        if not DRY_RUN:
            try:
                sb.table("open_positions").update(patch).eq("symbol", sym).execute()
            except Exception as e:
                logger.warning(f"  {sym}: qty sync failed: {e}")
                continue
        logger.info(f"  {action}: {sym} db={db_qty} -> broker={br_qty}")
        _log_reconcile(sb, trade_date, sym, action, db_qty=db_qty,
                       broker_qty=br_qty, broker_price=h["average_price"],
                       detail=f"quantity drift resolved in favour of broker")
        adjusted += 1

    return {"status": "ok", "opened": opened, "closed": closed, "adjusted": adjusted}


# ─────────────────────────────────────────────────────────────────────────────
# LIVE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _sessions_held(sb, entry_date: str, trade_date: str) -> int:
    """Trading sessions between two dates — calendar days badly overstate a
    time stop across weekends and NSE holidays."""
    try:
        rows = (sb.table("market_regime").select("date")
                  .gt("date", str(entry_date)[:10]).lte("date", trade_date)
                  .execute().data)
        return len(rows)
    except Exception:
        try:
            return (date.fromisoformat(trade_date)
                    - date.fromisoformat(str(entry_date)[:10])).days
        except Exception:
            return 0


def manage_open_positions(sb, trade_date: str, require_live: bool = False) -> dict:
    """
    Apply the exit policy to every open position at the current price.

    require_live=True (intraday runs) refuses to proceed on stale prices.

    WHY THAT MATTERS. The EOD fallback below reads stock_data_daily for
    trade_date. During market hours on a trading day, bhavcopy has not been
    written yet, so get_trade_date resolves to the PREVIOUS session and those
    "closes" are yesterday's. Evaluating stops against yesterday's close at
    11:00 is not a degraded check, it is a wrong one: it would miss a real
    intraday breach and re-fire yesterday's alerts every 30 minutes.
    The fallback is correct for the evening pipeline, where trade_date IS the
    session that just closed. It is never correct intraday.
    """
    # Kite market-data endpoints (ltp/quote/ohlc) require a PAID Kite Connect
    # subscription that this account does not have — verified directly:
    # profile/holdings/positions/margins all succeed, ltp/quote/ohlc return
    # "Insufficient permission for that call."
    #
    # Using fetch_ltp alone therefore returned {} on every cycle, and with
    # require_live=True the monitor skipped every single run — stop monitoring
    # would have stayed silently dead. The shared helper falls back to
    # yfinance (~15 min delayed) and reports which source it used, so a
    # delayed price is never presented as real-time.
    from analysis.trade_decision import fetch_live_prices

    policy = load_exit_policy()
    # Trend evidence for every managed symbol, fetched ONCE. Passed through the
    # policy dict so evaluate_exit stays pure — it does no I/O and can still be
    # tested against a recorded row.
    try:
        from control.exit_rules import load_signal_context
        policy["_trend_ctx"] = load_signal_context(
            sb, [r.get("symbol") for r in rows if r.get("symbol")])
    except Exception as e:
        logger.debug(f"  trend context unavailable: {e}")
        policy["_trend_ctx"] = {}
    rows = (sb.table("open_positions").select("*")
              .eq("status", "ACTIVE").execute().data) or []

    # Do not apply the SWING exit policy to INTRADAY positions.
    #
    # This engine books a partial at 1.5R, trails after 2R, targets 3R and time
    # stops after fifteen SESSIONS. An intraday position must be flat the same
    # day — often within the hour — so inheriting those rules would quietly
    # carry a four-hour thesis for three weeks. Nothing would error; the
    # position would simply be managed by rules never meant for it.
    if not cfg_bool("swing_manages_intraday_rows", False):
        intraday = [r for r in rows if (r.get("framework") or "SWING").upper() == "INTRADAY"]
        if intraday:
            logger.info(f"  skipping {len(intraday)} INTRADAY position(s) — "
                        f"managed by intraday/engine.py, not the swing exit policy: "
                        f"{', '.join(r['symbol'] for r in intraday)}")
        rows = [r for r in rows if (r.get("framework") or "SWING").upper() != "INTRADAY"]

    if not rows:
        return {"status": "ok", "managed": 0, "actions": []}

    symbols = [r["symbol"] for r in rows if r.get("symbol")]
    prices, price_source = fetch_live_prices(symbols)
    if prices:
        logger.info(f"  Live prices for {len(prices)}/{len(symbols)} via {price_source}"
                    + ("  ⚠️ ~15 min delayed" if price_source == "yfinance" else ""))

    if not prices and require_live:
        logger.warning(
            "  No live broker prices and require_live=True — skipping. "
            "Falling back to stock_data_daily intraday would evaluate stops "
            "against the PREVIOUS session's close (bhavcopy is not written "
            "until ~18:00 IST), producing missed breaches and repeated alerts. "
            "Refresh the Kite session: python -m kite.token_manager --login-url"
        )
        return {"status": "no_live_prices", "managed": 0, "actions": []}

    if not prices:
        # Fall back to the last daily close so the policy still runs (and the
        # evening pipeline still manages positions) when the broker is down.
        # Safe there: trade_date IS the session that just closed.
        try:
            latest = (sb.table("stock_data_daily").select("symbol,close")
                        .eq("date", trade_date).in_("symbol", symbols).execute().data)
            prices = {r["symbol"]: float(r["close"]) for r in latest if r.get("close")}
            logger.info(f"  Using EOD closes for {len(prices)} symbols (no live broker feed)")
        except Exception as e:
            logger.warning(f"  Could not load fallback prices: {e}")
            return {"status": "no_prices", "managed": 0, "actions": []}

    actions = []
    for pos in rows:
        sym = pos["symbol"]
        ltp = prices.get(sym)
        if not ltp:
            continue

        entry    = float(pos.get("entry_price") or 0)
        held     = _sessions_held(sb, pos.get("entry_date"), trade_date)
        decision = evaluate_exit(pos, ltp, held, policy)

        # Live metrics, refreshed every run
        hwm      = max(float(pos.get("high_water_mark") or entry), ltp)
        mfe      = max(float(pos.get("max_favorable_excursion") or 0), (ltp - entry) / entry * 100)
        mae      = min(float(pos.get("max_adverse_excursion")  or 0), (ltp - entry) / entry * 100)
        stop0    = float(pos.get("planned_stop") or 0)
        risk     = entry - stop0 if stop0 and stop0 < entry else None
        r_now    = round((ltp - entry) / risk, 3) if risk else None
        qty      = int(pos.get("current_qty") or pos.get("actual_qty") or 0)

        update = {
            "current_price":     ltp,
            "current_value":     round(ltp * qty, 2),
            "unrealized_pnl":    round((ltp - entry) * qty, 2),
            "pnl_pct":           round((ltp - entry) / entry * 100, 3) if entry else None,
            "high_water_mark":   round(hwm, 2),
            "max_favorable_excursion": round(mfe, 3),
            "max_adverse_excursion":   round(mae, 3),
            "r_multiple_current":      r_now,
            "last_reconciled_at": datetime.now(IST).isoformat(),
        }

        act = decision["action"]
        if act in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME"):
            # RECORD ONLY. This module never places orders — it raises the
            # alert and waits for the sale to appear in Kite, at which point
            # reconcile_with_broker writes the closed_positions row.
            update["exit_signal"]     = decision["reason"]
            update["action_required"] = f"EXIT — {decision['detail']}"
        elif act == "TRAIL_SL":
            update["active_sl"]       = decision["new_sl"]
            update["trail_activated"] = True
        elif act == "RUN":
            # Converted to a runner: keep the position, ratchet the stop, and
            # record that the hard target has been consciously passed so the
            # next cycle does not re-ask the same question from scratch.
            if decision.get("new_sl"):
                update["active_sl"] = decision["new_sl"]
                update["trail_activated"] = True
            update["action_required"] = f"RUNNER — {decision['detail']}"
        elif act == "EXIT_DETERIORATION":
            update["exit_signal"] = decision["reason"]
            update["action_required"] = f"EXIT — {decision['detail']}"
        elif act == "BOOK_PARTIAL":
            update["action_required"] = f"BOOK {decision['book_qty']} — {decision['detail']}"
            if decision["new_sl"]:
                update["active_sl"]       = decision["new_sl"]
                update["breakeven_moved"] = True

        if not DRY_RUN:
            try:
                sb.table("open_positions").update(update).eq("symbol", sym).execute()
            except Exception as e:
                logger.warning(f"  {sym}: update failed: {e}")

        # Phase 3: place the order the alert describes, for SWING positions.
        # Gated per framework via execution/gates — being comfortable
        # automating intraday exits says nothing about swing ones, so one
        # switch would force a decision there is no reason to make.
        if act in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME",
                   "EXIT_DETERIORATION", "BOOK_PARTIAL"):
            try:
                from execution.gates import auto_exit_enabled
                fw = (pos.get("framework") or "SWING").upper()
                if auto_exit_enabled(fw):
                    from execution.order_manager import OrderRequest, place
                    qty = int(decision.get("book_qty") or 0) or int(
                        pos.get("current_qty") or pos.get("actual_qty") or 0)
                    if qty > 0 and ltp:
                        slip = cfg_int("exit_slip_bps", 30) / 10000.0
                        place(OrderRequest(
                            sym, "SELL", qty, "LIMIT", round(ltp * (1 - slip), 1),
                            reason=f"{act}: {decision['detail']}"), sb)
            except Exception as e:
                logger.warning(f"  {sym}: auto-exit failed, alert still sent — {e}")

        if act != "HOLD":
            actions.append({"symbol": sym, "action": act,
                            "reason": decision["reason"], "detail": decision["detail"],
                            "ltp": ltp, "r": r_now})
            logger.info(f"  {act:<13} {sym:<12} {decision['detail']}")
        else:
            logger.debug(f"  HOLD          {sym:<12} {decision['detail']}")

    return {"status": "ok", "managed": len(rows), "actions": actions}


def send_action_alerts(actions: list[dict]):
    """Telegram alert for anything needing a decision. Silent when nothing does."""
    if not actions:
        return
    urgent = [a for a in actions
              if a["action"] in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME",
                                 "EXIT_DETERIORATION", "BOOK_PARTIAL")]
    if not urgent:
        return
    icons = {"EXIT_STOP": "🔴", "EXIT_TARGET": "🎯", "EXIT_TIME": "⏰",
             "EXIT_DETERIORATION": "⚠️", "BOOK_PARTIAL": "💰", "RUN": "🏃"}
    lines = ["<b>⚡ Position Actions Required</b>", ""]
    for a in urgent:
        lines.append(f"{icons.get(a['action'], '•')} <b>{a['symbol']}</b> — {a['action']}")
        lines.append(f"    LTP Rs.{a['ltp']:.2f}"
                     + (f" | {a['r']:+.2f}R" if a.get("r") is not None else ""))
        lines.append(f"    <i>{a['detail']}</i>")
    lines.append("\n<i>Execute in Kite. Positions reconcile automatically on the next run.</i>")
    try:
        from alerts.send_alerts import send_message
        send_message("\n".join(lines), subject_suffix="Position Actions")
    except Exception as e:
        logger.warning(f"  Position alert send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main(reconcile: bool = True, manage: bool = True,
         require_live: bool = False, alert: bool = True) -> dict:
    """
    alert=False suppresses the standalone "Position Actions Required" message.

    Set by run_pipeline for the evening run, where send_alerts renders the very
    same instruction inside the digest's OPEN POSITIONS block a few steps later
    — so one BOOK_PARTIAL on one position produced two notifications describing
    it in different words. Duplicate alerts for a single fact are worse than one
    alert: they make you check whether something happened twice.

    It stays True for the intraday and ad-hoc paths, which have no digest
    following them and would otherwise report nothing at all.
    """
    if is_kill_switch_active():
        logger.warning("Kill switch active — position_lifecycle skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb = get_supabase()
    from config import get_trade_date
    trade_date = get_trade_date(sb, mode="auto", caller="position_lifecycle")

    logger.info("=" * 66)
    logger.info(f"Position Lifecycle | {trade_date}" + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 66)

    result = {"trade_date": trade_date}

    if reconcile and cfg_bool("position_reconcile_enabled", True):
        result["reconcile"] = reconcile_with_broker(sb, trade_date)

    # Capital is checked here because this is the one component that runs
    # several times a day AND already holds a broker session. Sizing errors are
    # silent until an order is rejected mid-session, so the comparison wants to
    # be in front of you before the next entry, not after.
    try:
        from control.capital_check import log_capital_status
        result["capital"] = log_capital_status(sb)
    except Exception as e:
        logger.debug(f"  capital check skipped: {e}")

    if manage:
        managed = manage_open_positions(sb, trade_date, require_live=require_live)
        result["manage"] = managed
        if alert:
            send_action_alerts(managed.get("actions", []))
        elif managed.get("actions"):
            logger.info(f"  {len(managed['actions'])} action(s) — alerting suppressed; "
                        f"the evening digest carries them")

    logger.success(f"Position lifecycle complete: {result}")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v6 — Position Lifecycle Manager")
    ap.add_argument("--dry-run",           action="store_true")
    ap.add_argument("--reconcile-only",    action="store_true")
    ap.add_argument("--manage-only",       action="store_true")
    ap.add_argument("--require-live",      action="store_true",
                    help="Intraday mode: skip rather than fall back to the "
                         "previous session's closes when the broker feed is down.")
    args = ap.parse_args()
    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"
    print(main(reconcile=not args.manage_only,
               manage=not args.reconcile_only,
               require_live=args.require_live))
