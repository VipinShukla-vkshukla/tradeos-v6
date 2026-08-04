"""
TradeOS v7 — Unified Risk Model
================================
Single source of truth for stop, target, R-multiple and position size.

WHY THIS MODULE EXISTS
----------------------
Before this, three modules each built their own stop/target and disagreed:

  compute_msl.compute_expected_r()
      target = ez_mid + 3.0 * ATR
      stop   = ez_low - 1.5 * ATR
      -> stores only the resulting R-MULTIPLE, throwing the prices away.

  generate_signals Gate 4C
      reads that R-multiple back, then RECONSTRUCTS prices with a completely
      different stop:
          stop   = entry * (1 - 0.03)      # flat 3%, no ATR anywhere
          target = entry + (entry*0.03) * expected_r

  brain/position_manager._compute_stop()
      stop = cp * (1 - atr_pct * 1.5 / 100)   # a third model again

For a 3% ATR stock with a 1.2-ATR-wide zone, model 1 puts the target ~10.8%
above entry; model 2 puts it ~4.3% above. Model 2 wins, because it is the one
the entry gate uses. So the gate compared current price against a target that
was 2.5x too close, and any stock that had ticked up even 2% off the zone low
computed an implied R:R below 0.5 and was demoted to WATCH.

That is not a theory. On 2026-07-24, 17 of 27 WATCH signals carried
filter_reason `insufficient_rr_*` with values of 0.05x, 0.10x, 0.27x, 0.30x,
0.38x, 0.41x, 0.43x, 0.47x — and all 11 signals that DID pass had subtype
`in_zone`, i.e. only stocks sitting exactly inside the entry zone could ever
clear the gate. The system was structurally incapable of entering a stock that
had started to move.

THE FIX
-------
Compute levels ONCE, in one place, and pass the LEVELS around rather than an
R-multiple that has to be re-expanded against an assumed stop. compute_msl
persists planned_stop / planned_target; generate_signals reads them.

RE-ANCHORING
------------
The stop is a property of the SETUP (structure), not of what you paid. If price
runs up before you enter, your stop does not move up with it — your risk grows
and your R:R shrinks. That is the correct, honest penalty for chasing, and it
is what makes `min_rr` a meaningful discipline rather than an arbitrary wall.

Worked example — ATR 3%, zone 100.00-103.60:
    stop = 95.50, target = 110.80
    enter 100.00 -> risk 4.50, reward 10.80, R:R 2.40   take
    enter 103.00 -> risk 7.50, reward  7.80, R:R 1.04   marginal, allowed
    enter 105.00 -> risk 9.50, reward  5.80, R:R 0.61   blocked
Under the old model, entering at 103.00 scored 0.43 and was blocked — which is
why almost nothing ever qualified.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


# ── Defaults. Every one is overridable from system_config via load_risk_params. ──
DEFAULT_STOP_ATR_MULT   = 1.5    # initial stop distance in ATRs below the anchor
DEFAULT_TARGET_ATR_MULT = 3.0    # primary target distance in ATRs above the anchor
DEFAULT_MAX_RISK_PCT    = 8.0    # reject a setup whose stop is >8% away — too wide to size sanely
DEFAULT_MIN_RISK_PCT    = 1.5    # floor: a stop closer than this is inside daily noise

# Regime scales the stop. Wider stops in stressed markets avoid being shaken out
# by volatility expansion; tighter stops in trending markets keep R:R attractive.
REGIME_STOP_MULT = {
    "TRENDING":   0.95,
    "RISK ON":    1.00,
    "NEUTRAL":    1.05,
    "RECOVERING": 1.15,
    "RISK OFF":   1.25,
}


@dataclass(frozen=True)
class TradeLevels:
    """Everything downstream needs to enter, size, stop and target a trade."""
    entry:            float
    stop:             float
    target:           float
    risk_per_share:   float
    reward_per_share: float
    rr:               float          # reward / risk at THIS entry price
    risk_pct:         float          # stop distance as % of entry
    target_pct:       float          # target distance as % of entry
    anchor:           float          # the setup price the stop was derived from
    stop_source:      str            # "atr" | "structure" | "atr_capped"
    valid:            bool
    reject_reason:    str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def load_risk_params() -> dict:
    """
    Read risk parameters from system_config so the Brain can calibrate them
    without a code change. Import is local to keep this module usable in
    isolation (tests, notebooks) without a live Supabase connection.
    """
    try:
        from config import cfg_float
        return {
            "stop_atr_mult":   cfg_float("risk_stop_atr_mult",   DEFAULT_STOP_ATR_MULT),
            "target_atr_mult": cfg_float("risk_target_atr_mult", DEFAULT_TARGET_ATR_MULT),
            "max_risk_pct":    cfg_float("risk_max_risk_pct",    DEFAULT_MAX_RISK_PCT),
            "min_risk_pct":    cfg_float("risk_min_risk_pct",    DEFAULT_MIN_RISK_PCT),
        }
    except Exception:
        return {
            "stop_atr_mult":   DEFAULT_STOP_ATR_MULT,
            "target_atr_mult": DEFAULT_TARGET_ATR_MULT,
            "max_risk_pct":    DEFAULT_MAX_RISK_PCT,
            "min_risk_pct":    DEFAULT_MIN_RISK_PCT,
        }


def compute_trade_levels(
    entry_price:    float,
    atr_abs:        float,
    *,
    anchor_price:   float | None = None,
    structure_stop: float | None = None,
    regime:         str   = "NEUTRAL",
    params:         dict  | None = None,
) -> TradeLevels:
    """
    Build stop / target / R:R for one setup.

    entry_price     Price the trade would actually be entered at. Use the entry
                    zone low when planning, the live price when evaluating a
                    chase.
    atr_abs         ATR in RUPEES, not percent. Callers holding atr_pct must
                    convert: atr_abs = price * atr_pct / 100.
    anchor_price    The setup price the stop hangs off — normally entry_zone_low.
                    Defaults to entry_price. Keeping this separate from
                    entry_price is what makes chasing cost R:R instead of
                    silently sliding the stop up with the price.
    structure_stop  Optional structural level (supertrend, SMA50, swing low).
                    Used when it sits ABOVE the ATR stop, i.e. is tighter and
                    more meaningful, but never closer than min_risk_pct.
    regime          Scales the stop via REGIME_STOP_MULT.
    """
    p = params or load_risk_params()

    if not entry_price or entry_price <= 0:
        return _invalid(entry_price, "invalid_entry_price")
    if not atr_abs or atr_abs <= 0:
        return _invalid(entry_price, "missing_atr")

    anchor    = anchor_price if (anchor_price and anchor_price > 0) else entry_price
    regime_k  = REGIME_STOP_MULT.get((regime or "NEUTRAL").upper(), 1.0)

    # ── Stop ──────────────────────────────────────────────────────────────────
    atr_stop    = anchor - (p["stop_atr_mult"] * regime_k * atr_abs)
    stop        = atr_stop
    stop_source = "atr"

    # A structural level tighter than the ATR stop is preferred — it is where
    # the thesis actually breaks. Ignore it if it is so tight that normal daily
    # movement would trigger it.
    if structure_stop and structure_stop > 0:
        min_gap = entry_price * (p["min_risk_pct"] / 100.0)
        if atr_stop < structure_stop < (entry_price - min_gap):
            stop        = structure_stop
            stop_source = "structure"

    if stop <= 0:
        return _invalid(entry_price, "stop_below_zero")

    risk = entry_price - stop
    if risk <= 0:
        # Price is already at or below the stop — there is no trade here.
        return _invalid(entry_price, "price_at_or_below_stop")

    risk_pct = risk / entry_price * 100.0

    # ── Target ────────────────────────────────────────────────────────────────
    # Anchored to the SETUP, not to the (possibly chased) entry price. This is
    # the whole point: a fixed objective the price is travelling toward. Chasing
    # eats into the remaining distance instead of inflating the target with it.
    target = anchor + (p["target_atr_mult"] * atr_abs)

    reward = target - entry_price
    if reward <= 0:
        return _invalid(entry_price, "target_already_reached", stop=stop,
                        target=target, anchor=anchor)

    rr = reward / risk

    # ── Validity ──────────────────────────────────────────────────────────────
    if risk_pct > p["max_risk_pct"]:
        return _invalid(entry_price, f"risk_too_wide_{risk_pct:.1f}pct",
                        stop=stop, target=target, anchor=anchor)

    return TradeLevels(
        entry            = round(entry_price, 2),
        stop             = round(stop, 2),
        target           = round(target, 2),
        risk_per_share   = round(risk, 2),
        reward_per_share = round(reward, 2),
        rr               = round(rr, 3),
        risk_pct         = round(risk_pct, 2),
        target_pct       = round(reward / entry_price * 100.0, 2),
        anchor           = round(anchor, 2),
        stop_source      = stop_source,
        valid            = True,
    )


def _invalid(entry, reason, stop=0.0, target=0.0, anchor=0.0) -> TradeLevels:
    return TradeLevels(
        entry=round(entry or 0, 2), stop=round(stop, 2), target=round(target, 2),
        risk_per_share=0.0, reward_per_share=0.0, rr=0.0,
        risk_pct=0.0, target_pct=0.0, anchor=round(anchor, 2),
        stop_source="none", valid=False, reject_reason=reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionSize:
    quantity:       int
    invested_value: float
    risk_amount:    float      # rupees at risk if the stop is hit
    risk_pct_of_capital: float
    capped_by:      str        # "risk" | "max_position" | "capital" | "none"


def compute_position_size(
    levels:         TradeLevels,
    total_capital:  float,
    *,
    risk_pct_per_trade: float = 1.0,
    max_position_pct:   float = 20.0,
    available_capital:  float | None = None,
) -> PositionSize:
    """
    Risk-parity sizing: every trade risks the same PERCENTAGE OF CAPITAL,
    so a wide-stop setup gets fewer shares than a tight-stop one.

    This is the piece that makes expectancy arithmetic work. Sizing by rupee
    value instead (equal position sizes) means a 9%-stop trade loses 3x what a
    3%-stop trade loses, and the average loss drifts up independently of any
    signal quality — which is consistent with the observed -4.05% average loss
    against a +3.89% average win.
    """
    if not levels.valid or levels.risk_per_share <= 0:
        return PositionSize(0, 0.0, 0.0, 0.0, "none")

    capital   = total_capital or 0.0
    available = available_capital if available_capital is not None else capital
    if capital <= 0:
        return PositionSize(0, 0.0, 0.0, 0.0, "capital")

    risk_budget = capital * (risk_pct_per_trade / 100.0)
    qty_by_risk = int(risk_budget // levels.risk_per_share)

    max_value    = capital * (max_position_pct / 100.0)
    qty_by_value = int(max_value // levels.entry)
    qty_by_avail = int(available // levels.entry) if levels.entry > 0 else 0

    qty = max(0, min(qty_by_risk, qty_by_value, qty_by_avail))
    if qty == 0:
        return PositionSize(0, 0.0, 0.0, 0.0, "capital")

    capped_by = ("risk"         if qty == qty_by_risk  else
                 "max_position" if qty == qty_by_value else "capital")

    invested = qty * levels.entry
    risk_amt = qty * levels.risk_per_share
    return PositionSize(
        quantity            = qty,
        invested_value      = round(invested, 2),
        risk_amount         = round(risk_amt, 2),
        risk_pct_of_capital = round(risk_amt / capital * 100.0, 3),
        capped_by           = capped_by,
    )
