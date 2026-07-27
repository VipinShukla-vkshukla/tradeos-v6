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
    exchange    0.00297% (NSE) on turnover
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


def _rates() -> dict:
    return {
        "brokerage_flat":   cfg_float("cost_brokerage_flat", 20.0),
        "brokerage_pct":    cfg_float("cost_brokerage_pct", 0.03),
        "stt_sell_pct":     cfg_float("cost_stt_sell_pct", 0.025),
        "exchange_pct":     cfg_float("cost_exchange_pct", 0.00297),
        "sebi_pct":         cfg_float("cost_sebi_pct", 0.0001),
        "stamp_buy_pct":    cfg_float("cost_stamp_buy_pct", 0.003),
        "gst_pct":          cfg_float("cost_gst_pct", 18.0),
        "slippage_bps":     cfg_float("cost_slippage_bps", 5.0),
    }


def round_trip(entry_price: float, qty: int, exit_price: float | None = None) -> RoundTrip:
    """
    Full cost of buying and selling `qty` shares.

    exit_price defaults to entry_price — costs are wanted BEFORE the trade, to
    decide whether to take it, and at that point the exit is unknown. Using the
    entry for both sides understates STT slightly on a winner, which is the safe
    direction for a go/no-go decision.
    """
    r = _rates()
    exit_price = exit_price or entry_price
    buy_val  = entry_price * qty
    sell_val = exit_price * qty
    turnover = buy_val + sell_val

    # Flat OR percentage, whichever is lower — per executed order, so twice.
    per_order = lambda v: min(r["brokerage_flat"], v * r["brokerage_pct"] / 100.0)
    brokerage = per_order(buy_val) + per_order(sell_val)

    stt      = sell_val * r["stt_sell_pct"] / 100.0
    exchange = turnover * r["exchange_pct"] / 100.0
    sebi     = turnover * r["sebi_pct"] / 100.0
    stamp    = buy_val  * r["stamp_buy_pct"] / 100.0
    gst      = (brokerage + exchange + sebi) * r["gst_pct"] / 100.0

    # Slippage is not a charge but it is a cost, and ignoring it is how a
    # backtest beats a live account. Applied to both legs.
    slippage = turnover * r["slippage_bps"] / 10000.0

    total = brokerage + stt + exchange + sebi + stamp + gst + slippage
    pct   = (total / buy_val * 100.0) if buy_val else 0.0

    return RoundTrip(
        turnover=round(turnover, 2), brokerage=round(brokerage, 2),
        stt=round(stt, 2), exchange=round(exchange, 2), sebi=round(sebi, 2),
        stamp=round(stamp, 2), gst=round(gst, 2),
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
    if gross_down > 0 and rt.pct_of_position > gross_down * 0.5:
        return False, (f"stop is {gross_down:.2f}% but the round trip costs "
                       f"{rt.pct_of_position:.2f}% — a stop-out loses "
                       f"{gross_down + rt.pct_of_position:.2f}%, not {gross_down:.2f}%")
    return True, (f"net {gross_up - rt.pct_of_position:.2f}% after "
                  f"{rt.pct_of_position:.2f}% costs")


def explain(entry_price: float, qty: int) -> str:
    """One line for an alert, so the cost is visible at the decision."""
    rt = round_trip(entry_price, qty)
    return (f"₹{entry_price * qty:,.0f} position · round trip ₹{rt.total:,.0f} "
            f"({rt.pct_of_position:.2f}%) · breakeven +{rt.breakeven_move_pct:.2f}%")
