"""
What a round trip actually costs, and therefore what move is worth taking.

THIS IS THE MOST IMPORTANT MODULE IN THE INTRADAY SUBSYSTEM
-----------------------------------------------------------
Swing trading can be sloppy about costs: a 6% target absorbs 0.2% of charges
without noticing. Intraday cannot. The moves being harvested are 0.3% to 1.5%,
and charges land on both legs of every one of them.

Measured against the real Zerodha equity-intraday schedule (see the rates
below), the round trip is close to a CONSTANT 0.21% of position value:

    ₹  5,000 position  ->  0.21% round trip
    ₹ 20,000 position  ->  0.21% round trip
    ₹1,00,000 position ->  0.18% round trip

The flatness surprises people, including me before measuring it. Brokerage is
"₹20 or 0.03% per order, whichever is LOWER", so small positions pay the
percentage and only positions above ~₹66,000 per leg reach the ₹20 cap and start
amortising. Position size is therefore NOT the lever — the target is.

The consequence is a hard design constraint on which strategies are viable:

    0.30% target  ->  not tradeable at ANY size (costs take over 30% of it)
    0.50% target  ->  needs ~₹3.5L per position to keep 70% — impossible here
    0.70%+ target ->  viable

So at this account's size, intraday scalping is arithmetically excluded, and
only setups with a realistic ~0.7% or better move are worth taking. Setups whose
target does not clear the round trip by the keep-ratio are rejected outright
rather than shown with an optimistic R:R that quietly ignores charges.

CHARGES MODELLED (Zerodha equity intraday, MIS)
-----------------------------------------------
    brokerage   ₹20 or 0.03% per executed order, whichever is LOWER
    STT         0.025% on the SELL side only
    exchange    0.00307% (NSE) on turnover — BSE is 0.00375%
    SEBI        0.0001% on turnover
    stamp duty  0.003% on the BUY side only
    GST         18% on (brokerage + exchange + SEBI)

Rates live in system_config so they can be corrected without a deploy — they do
change, and a stale rate here silently biases every sizing decision.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg_float


@dataclass(frozen=True)
class RoundTrip:
    turnover: float            # buy value + sell value
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float
    total: float
    pct_of_position: float     # total cost as % of the entry value
    breakeven_move_pct: float  # move needed just to get back to flat
    dp: float = 0.0            # depository fee, delivery sells only


def _rates(product: str = "MIS") -> dict:
    """
    Charges for ONE product. MIS and CNC are not the same trade financially.

    This module was written for the intraday book and every rate in it is an
    MIS rate. The swing book then reused it — close_position, paper_broker and
    weekly_review all call round_trip — so every CNC trade was priced as if it
    were intraday. Four of the seven lines are wrong in that direction, and the
    error is one-sided: delivery is DEARER, so swing costs were understated on
    every trade, on entry sizing and on the recorded outcome alike.

    What actually differs at Zerodha:

      brokerage   equity DELIVERY is free; MIS is Rs 20 or 0.03%, lower of.
      STT         delivery 0.1% on BOTH legs; MIS 0.025% on the sell only.
                  This is the big one — 8x the rate and twice the sides.
      stamp       delivery 0.015% on buy; MIS 0.003%.
      DP          Rs 15.04 per scrip per SELL day, delivery only. Flat, so it
                  hurts small positions most: on a Rs 2,000 position it is
                  0.75% by itself, more than the entire modelled round trip.

    Exchange, SEBI and GST are the same for both.

    The MIS defaults are unchanged, so the intraday cost gate — which is
    calibrated on them and has 460 scored outcomes behind it — behaves exactly
    as before.
    """
    delivery = (product or "MIS").upper() in ("CNC", "NRML", "DELIVERY")
    if delivery:
        return {
            "brokerage_flat":   cfg_float("cost_cnc_brokerage_flat", 0.0),
            "brokerage_pct":    cfg_float("cost_cnc_brokerage_pct", 0.0),
            "stt_buy_pct":      cfg_float("cost_cnc_stt_buy_pct", 0.1),
            "stt_sell_pct":     cfg_float("cost_cnc_stt_sell_pct", 0.1),
            "exchange_pct":     cfg_float("cost_exchange_pct", 0.00307),
            "sebi_pct":         cfg_float("cost_sebi_pct", 0.0001),
            "stamp_buy_pct":    cfg_float("cost_cnc_stamp_buy_pct", 0.015),
            "gst_pct":          cfg_float("cost_gst_pct", 18.0),
            "slippage_bps":     cfg_float("cost_slippage_bps", 5.0),
            "dp_per_sell":      cfg_float("cost_dp_per_sell", 15.04),
        }
    return {
        "brokerage_flat":   cfg_float("cost_brokerage_flat", 20.0),
        "brokerage_pct":    cfg_float("cost_brokerage_pct", 0.03),
        "stt_buy_pct":      cfg_float("cost_stt_buy_pct", 0.0),
        "stt_sell_pct":     cfg_float("cost_stt_sell_pct", 0.025),
        "exchange_pct":     cfg_float("cost_exchange_pct", 0.00307),
        "sebi_pct":         cfg_float("cost_sebi_pct", 0.0001),
        "stamp_buy_pct":    cfg_float("cost_stamp_buy_pct", 0.003),
        "gst_pct":          cfg_float("cost_gst_pct", 18.0),
        "slippage_bps":     cfg_float("cost_slippage_bps", 5.0),
        "dp_per_sell":      0.0,
    }


def round_trip(entry_price: float, qty: int, exit_price: float | None = None,
               product: str = "MIS") -> RoundTrip:
    """
    Full cost of buying and selling `qty` shares.

    exit_price defaults to entry_price — costs are wanted BEFORE the trade, to
    decide whether to take it, and at that point the exit is unknown. Using the
    entry for both sides understates STT slightly on a winner, which is the safe
    direction for a go/no-go decision.
    """
    r = _rates(product)
    exit_price = exit_price or entry_price
    buy_val  = entry_price * qty
    sell_val = exit_price * qty
    turnover = buy_val + sell_val

    # Flat OR percentage, whichever is lower — per executed order, so twice.
    # For delivery both are zero, and min(0, x) is 0, so this is free as it
    # should be rather than needing a special case.
    per_order = lambda v: min(r["brokerage_flat"], v * r["brokerage_pct"] / 100.0)
    brokerage = per_order(buy_val) + per_order(sell_val)

    # Delivery pays STT on BOTH legs. MIS pays it on the sell only, and its
    # stt_buy_pct is 0, so one expression serves both.
    stt      = (sell_val * r["stt_sell_pct"] / 100.0
                + buy_val * r["stt_buy_pct"] / 100.0)
    exchange = turnover * r["exchange_pct"] / 100.0
    sebi     = turnover * r["sebi_pct"] / 100.0
    stamp    = buy_val  * r["stamp_buy_pct"] / 100.0
    gst      = (brokerage + exchange + sebi) * r["gst_pct"] / 100.0

    # Slippage is not a charge but it is a cost, and ignoring it is how a
    # backtest beats a live account. Applied to both legs.
    slippage = turnover * r["slippage_bps"] / 10000.0

    # DP is a FLAT rupee fee per scrip per selling day, delivery only. Being
    # flat it does not scale with position size, so it dominates small ones:
    # Rs 15.04 on a Rs 2,000 position is 0.75% before a single statutory charge
    # is counted. On a Rs 20,000 account taking Rs 2,000-4,000 positions this is
    # the largest single cost line, and it was absent entirely.
    dp       = r["dp_per_sell"] if qty > 0 else 0.0

    total = brokerage + stt + exchange + sebi + stamp + gst + slippage + dp
    pct   = (total / buy_val * 100.0) if buy_val else 0.0

    return RoundTrip(
        turnover=round(turnover, 2), brokerage=round(brokerage, 2),
        stt=round(stt, 2), exchange=round(exchange, 2), sebi=round(sebi, 2),
        stamp=round(stamp, 2), gst=round(gst, 2), dp=round(dp, 2),
        total=round(total + 0.0, 2), pct_of_position=round(pct, 4),
        breakeven_move_pct=round(pct, 4),
    )


def min_viable_position(entry_price: float, target_move_pct: float,
                        keep_ratio: float | None = None) -> int:
    """
    Smallest position for which `target_move_pct` still leaves a real profit.

    keep_ratio is the fraction of the gross move you insist on keeping after
    costs. At 0.70, a 0.5% target must cost no more than 0.15% round trip —
    which on flat brokerage implies a floor on position value.

    Returns 0 when NO size clears the bar, which happens when the target is
    simply too small to trade at this price. That is a real answer and the
    caller should reject the setup rather than shrink it.
    """
    keep = keep_ratio if keep_ratio is not None else cfg_float("intraday_cost_keep_ratio", 0.70)
    if entry_price <= 0 or target_move_pct <= 0:
        return 0
    max_cost_pct = target_move_pct * (1.0 - keep)

    # Cost% falls monotonically with size (flat brokerage amortises), so walk up
    # until it clears. Capped well above anything this account can take.
    qty = 1
    cap = int(2_000_000 / entry_price) + 1
    while qty <= cap:
        if round_trip(entry_price, qty).pct_of_position <= max_cost_pct:
            return qty
        qty = max(qty + 1, int(qty * 1.25))
    return 0


def is_worth_taking(entry_price: float, qty: int, target_price: float,
                    stop_price: float) -> tuple[bool, str]:
    """
    Does this setup survive its own costs?

    Checks the two things that kill small intraday trades:
      1. the target does not clear the round trip by the required margin
      2. the stop is TIGHTER than the round trip, so a stop-out costs more in
         charges than in price — the trade cannot lose small, only badly

    (2) is the subtler one and it is why very tight intraday stops on small
    positions are a trap: a 0.15% stop on a position whose round trip is 0.3%
    means every loss is triple the risk you sized for.
    """
    if qty <= 0 or entry_price <= 0:
        return False, "no position"
    rt = round_trip(entry_price, qty)
    gross_up   = (target_price - entry_price) / entry_price * 100.0
    gross_down = (entry_price - stop_price) / entry_price * 100.0

    keep = cfg_float("intraday_cost_keep_ratio", 0.70)
    if gross_up <= 0:
        return False, "target is at or below entry"
    if rt.pct_of_position > gross_up * (1 - keep):
        net = gross_up - rt.pct_of_position
        return False, (f"costs {rt.pct_of_position:.2f}% eat a {gross_up:.2f}% target "
                       f"— only {net:.2f}% net, under the {keep:.0%} keep-ratio")
    # The stop must be a MULTIPLE of the friction, not merely larger than it.
    #
    # This was 0.5 — reject only if costs exceed half the stop. On 30 Jul that
    # let through M&MFIN with a 0.44% stop against 0.21% costs (0.21 vs a 0.22
    # threshold, passing by a hundredth) and ATHERENERG at 0.52%. Both were
    # stopped out as SETUP_INVALIDATED inside 0.4% of entry, for -0.81R and
    # -0.80R. The one winner that day, GODFRYPHLP, had the widest stop at 0.66%.
    #
    # A stop only twice the round trip is inside ordinary noise: the position
    # has to survive a 0.21% tax before the thesis gets any room at all, so a
    # 0.44% stop is really a 0.23% thesis. At 0.35 the stop must be ~3x the
    # friction, which on that day rejected both losers and kept the winner.
    #
    # Three trades is not evidence, and the ratio is configurable for that
    # reason. The principle does not depend on the sample: a stop that noise can
    # reach is not a stop, it is a fee.
    ratio = cfg_float("intraday_cost_stop_ratio", 0.35)
    if gross_down > 0 and rt.pct_of_position > gross_down * ratio:
        need = rt.pct_of_position / ratio
        return False, (f"stop is {gross_down:.2f}% but the round trip costs "
                       f"{rt.pct_of_position:.2f}% — needs a {need:.2f}% stop to "
                       f"give the thesis room; a stop-out here loses "
                       f"{gross_down + rt.pct_of_position:.2f}%")
    return True, (f"net {gross_up - rt.pct_of_position:.2f}% after "
                  f"{rt.pct_of_position:.2f}% costs")


def explain(entry_price: float, qty: int) -> str:
    """One line for an alert, so the cost is visible at the decision."""
    rt = round_trip(entry_price, qty)
    return (f"₹{entry_price * qty:,.0f} position · round trip ₹{rt.total:,.0f} "
            f"({rt.pct_of_position:.2f}%) · breakeven +{rt.breakeven_move_pct:.2f}%")


def exit_leg(exit_price: float, qty: int, product: str = "MIS") -> float:
    """
    Charges owed on the SELL leg alone.

    close_position needed this and did not have it, so it computed the full
    round trip and halved the statutory total. Halving is wrong in both
    directions at once and does not cancel:

      · STT is a SELL-side charge (for MIS entirely, for delivery on both legs
        but at the same rate). Halving the round-trip figure charges the exit
        only half of what it actually owes.
      · Stamp duty is a BUY-side charge. Halving the round-trip figure charges
        the exit for half a stamp duty it does not owe at all.
      · DP is a sell-side fee. Half of it is not a thing that exists.

    On a Rs 2,900 CNC exit the halving approach recorded about Rs 1.60 against
    a true cost near Rs 18.50. Every closed swing row understates its charges,
    which means realized_pnl net of charges is overstated, which means the
    weekly review scores every engine slightly better than it earned.
    """
    r = _rates(product)
    sell_val = exit_price * qty
    brokerage = min(r["brokerage_flat"], sell_val * r["brokerage_pct"] / 100.0)
    stt       = sell_val * r["stt_sell_pct"] / 100.0
    exchange  = sell_val * r["exchange_pct"] / 100.0
    sebi      = sell_val * r["sebi_pct"] / 100.0
    gst       = (brokerage + exchange + sebi) * r["gst_pct"] / 100.0
    dp        = r["dp_per_sell"] if qty > 0 else 0.0
    return round(brokerage + stt + exchange + sebi + gst + dp, 2)


def entry_leg(entry_price: float, qty: int, product: str = "MIS") -> float:
    """
    Charges owed on the BUY leg alone.

    The counterpart to exit_leg, and split out for the same reason: stamp duty
    is buy-side, DP is sell-side, and STT differs by product. Half a round trip
    charges each leg a blend of the other's costs.
    """
    r = _rates(product)
    buy_val = entry_price * qty
    brokerage = min(r["brokerage_flat"], buy_val * r["brokerage_pct"] / 100.0)
    stt       = buy_val * r["stt_buy_pct"] / 100.0
    exchange  = buy_val * r["exchange_pct"] / 100.0
    sebi      = buy_val * r["sebi_pct"] / 100.0
    stamp     = buy_val * r["stamp_buy_pct"] / 100.0
    gst       = (brokerage + exchange + sebi) * r["gst_pct"] / 100.0
    return round(brokerage + stt + exchange + sebi + stamp + gst, 2)
