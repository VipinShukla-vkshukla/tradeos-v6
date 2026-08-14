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

REGIME SYMMETRY  (`risk_regime_scales_target`, shipped OFF)
-----------------------------------------------------------
`regime_k` scaled the STOP and not the TARGET. Both are ATR distances and they
are the two sides of one ratio, so a knob whose stated purpose is volatility
became a knob on reward-per-unit-risk:

    planned R = target_atr_mult / (stop_atr_mult * regime_k) = 3.0 / (1.5 * k)

    TRENDING 2.1050 · RISK ON 2.0000 · NEUTRAL 1.9048 · RECOVERING 1.7391 ·
    RISK OFF 1.6000

R therefore SHRANK as conditions worsened — more risk per share for identical
reward, exactly when the market is least likely to pay for it — and the 2R
design point was reachable only in RISK ON, a regime this book has never traded
(all 1000 plans in the 28-Jul→13-Aug window read NEUTRAL). 1.9048R was not
drift or a distribution; it was that constant, on 685 of 995 plans.

With the switch on, `regime_k` multiplies the target distance too **on the ATR
branch only**, and planned R becomes `target_atr_mult / stop_atr_mult` = 2.0 in
every regime. Scaling it on the STRUCTURE branch as well was the obvious
alternative and is wrong: a structural stop is a PRICE, so `regime_k` never
touched its risk, and scaling only its target would raise R with no offsetting
change in risk — a free 5% on 310 of 995 plans. The k is applied to the target
exactly where it was applied to the stop, which is what "symmetric" means here.

THE COST MODEL  (`risk_min_planned_r_enabled`, shipped OFF)
------------------------------------------------------------
This module used to import `dataclasses` and nothing else. It set a target with
no knowledge of the friction that target has to clear, while every other gate in
the system priced its own. Now it sizes the plan by the production rule, prices
that clip's round trip, and reports `friction_r` / `required_rr` on every plan
whether or not the floor is armed.

The basis is LEDGER (`entry_leg + exit_leg`, statutory only), not GATE
(`round_trip`, which adds 5 bps of slippage per leg). The two differ by a
constant +0.100pp of position — 1.10–1.17x on CNC clips, 1.94x on MIS — and the
reason to pick the ledger here is the CLAUDE.md rule that a gate and the thing
it gates must be the SAME QUANTITY. `planned_target` is compared against
REALISED R by `expectancy_ledger`, `weekly_review` and every prior built from
`closed_positions`, and all of those price friction statutorily because slippage
is already inside the fill price on both books. Charging it here as well would
be a double count against the very number this floor is meant to protect.

`required_rr` is the break-even planned R at the design hit rate,
`(1 - h + friction_r) / h` — the identity `tools/unit_economics.py` uses, so the
two are directly comparable. Note what it assumes and what the book does: it
assumes winners pay exactly `rr` and losers exactly 1R, and of the 10 closed
swing trades with a full planned geometry, ONE reached its planned target and
NONE reached its planned stop. This floor is therefore a PLANNING discipline —
"do not write down a plan that cannot pay for itself" — not a prediction of
realised expectancy, which the exit ladder decides. See FINDINGS F-2.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


# ── Defaults. Every one is overridable from system_config via load_risk_params. ──
DEFAULT_STOP_ATR_MULT   = 1.5    # initial stop distance in ATRs below the anchor
DEFAULT_TARGET_ATR_MULT = 3.0    # primary target distance in ATRs above the anchor
DEFAULT_MAX_RISK_PCT    = 8.0    # reject a setup whose stop is >8% away — too wide to size sanely
DEFAULT_MIN_RISK_PCT    = 1.5    # floor: a stop closer than this is inside daily noise

# Both fixes ship INERT. Arming either changes what the account does with money
# and is a separate decision on separate evidence.
DEFAULT_REGIME_SCALES_TARGET = False
DEFAULT_MIN_PLANNED_R_ENABLED = False
DEFAULT_PLAN_HIT_RATE = 0.40     # the design hit rate the break-even is taken at
DEFAULT_PLAN_R_MARGIN = 0.0      # extra R demanded ABOVE break-even
DEFAULT_PLAN_PRODUCT  = "CNC"    # the swing book is delivery, never MIS
DEFAULT_PLAN_CAPITAL  = 0.0      # 0 -> derive from the live swing sleeve

# Regime scales the stop. Wider stops in stressed markets avoid being shaken out
# by volatility expansion; tighter stops in trending markets keep R:R attractive.
# It scales the TARGET by the same factor when `risk_regime_scales_target` is on
# — see REGIME SYMMETRY above for why that is the same statement, not a second
# knob.
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

    # ── Cost awareness. Reported on every plan; only ENFORCED when the floor
    # is armed. Defaults keep every existing construction site valid. ──
    regime_k:    float = 1.0    # the volatility factor the regime applied
    clip_qty:    int   = 0      # shares the production sizing rule would buy
    clip_value:  float = 0.0    # that clip in rupees
    friction_r:  float = 0.0    # round trip on that clip, in R at THIS stop
    required_rr: float = 0.0    # planned R at which expectancy is zero
    cost_basis:  str   = "none" # "ledger" | "unfunded" | "unavailable" | "none"

    def as_dict(self) -> dict:
        return asdict(self)


def load_risk_params() -> dict:
    """
    Read risk parameters from system_config so the Brain can calibrate them
    without a code change. Import is local to keep this module usable in
    isolation (tests, notebooks) without a live Supabase connection.
    """
    fallback = {
        "stop_atr_mult":        DEFAULT_STOP_ATR_MULT,
        "target_atr_mult":      DEFAULT_TARGET_ATR_MULT,
        "max_risk_pct":         DEFAULT_MAX_RISK_PCT,
        "min_risk_pct":         DEFAULT_MIN_RISK_PCT,
        "regime_scales_target": DEFAULT_REGIME_SCALES_TARGET,
        "min_planned_r":        DEFAULT_MIN_PLANNED_R_ENABLED,
        "plan_hit_rate":        DEFAULT_PLAN_HIT_RATE,
        "plan_r_margin":        DEFAULT_PLAN_R_MARGIN,
        "plan_product":         DEFAULT_PLAN_PRODUCT,
        "plan_capital":         DEFAULT_PLAN_CAPITAL,
        "risk_pct_per_trade":   1.0,
        "max_position_pct":     20.0,
    }
    try:
        from config import cfg, cfg_bool, cfg_float
        return {
            "stop_atr_mult":   cfg_float("risk_stop_atr_mult",   DEFAULT_STOP_ATR_MULT),
            "target_atr_mult": cfg_float("risk_target_atr_mult", DEFAULT_TARGET_ATR_MULT),
            "max_risk_pct":    cfg_float("risk_max_risk_pct",    DEFAULT_MAX_RISK_PCT),
            "min_risk_pct":    cfg_float("risk_min_risk_pct",    DEFAULT_MIN_RISK_PCT),
            "regime_scales_target": cfg_bool("risk_regime_scales_target",
                                             DEFAULT_REGIME_SCALES_TARGET),
            "min_planned_r":   cfg_bool("risk_min_planned_r_enabled",
                                        DEFAULT_MIN_PLANNED_R_ENABLED),
            "plan_hit_rate":   cfg_float("risk_plan_hit_rate", DEFAULT_PLAN_HIT_RATE),
            "plan_r_margin":   cfg_float("risk_plan_r_margin", DEFAULT_PLAN_R_MARGIN),
            "plan_product":    cfg("risk_plan_product", DEFAULT_PLAN_PRODUCT) or DEFAULT_PLAN_PRODUCT,
            "plan_capital":    cfg_float("risk_plan_capital", DEFAULT_PLAN_CAPITAL),
            # The SAME two keys portfolio_constraints sizes with. Read here, not
            # redefined here — a second copy of the sizing rule is how the plan
            # and the position it becomes would drift apart.
            "risk_pct_per_trade": cfg_float("risk_pct_per_trade", 1.0),
            "max_position_pct":   cfg_float("max_position_pct", 20.0),
        }
    except Exception:
        return fallback


def _plan_capital(p: dict) -> float:
    """
    What the planner sizes against. `risk_plan_capital` overrides; otherwise the
    live swing sleeve, which is the whole account while intraday is PAPER.
    """
    explicit = float(p.get("plan_capital") or 0.0)
    if explicit > 0:
        return explicit
    try:
        from config import capital_for
        return float(capital_for("SWING"))
    except Exception:
        return 0.0


def plan_clip(entry_price: float, risk_per_share: float, capital: float,
              *, risk_pct_per_trade: float, max_position_pct: float
              ) -> tuple[int, str]:
    """
    Shares the sizing rule would buy for this plan on an EMPTY book, and which
    of the two terms bound.

    These are the two capital terms of `portfolio_constraints.enforce()` — the
    risk budget and the max-position ceiling. The sector and cash terms there
    need live book state the planner does not have, and both only ever REDUCE
    the quantity, so this is an upper bound on the real clip and therefore a
    LOWER bound on friction per R. Erring toward less friction is the permissive
    direction for a gate that can refuse a trade.

    `compute_position_size` below calls this rather than restating it. Two
    copies of a sizing rule is how a plan and the position it becomes drift
    apart, and the friction this module reports is only meaningful if it was
    computed on the clip the account will actually take.
    """
    if entry_price <= 0 or risk_per_share <= 0 or capital <= 0:
        return 0, "capital"
    qty_by_risk  = int((capital * risk_pct_per_trade / 100.0) // risk_per_share)
    qty_by_value = int((capital * max_position_pct / 100.0) // entry_price)
    if qty_by_risk <= qty_by_value:
        return max(0, qty_by_risk), "risk"
    return max(0, qty_by_value), "max_position"


def _statutory_round_trip(entry_price: float, qty: int, product: str) -> float:
    """
    Both legs' charges on the LEDGER basis — statutory only, no slippage.

    Split out as a named module attribute so a test can replace it and prove the
    floor stays permissive when the charge schedule cannot be read at all.
    """
    from intraday.cost_model import entry_leg, exit_leg
    return (entry_leg(entry_price, qty, product=product)
            + exit_leg(entry_price, qty, product=product))


def required_planned_r(friction_r: float, hit_rate: float) -> float:
    """
    Planned R at which expectancy is zero, given friction charged every trade:

        h * R - (1 - h) - friction = 0   ->   R = (1 - h + friction) / h

    The same identity `tools/unit_economics.py:114` uses, so a number here is
    directly comparable to one printed there.
    """
    if hit_rate <= 0:
        return 0.0
    return (1.0 - hit_rate + friction_r) / hit_rate


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
    #
    # `regime_k` applies to the target exactly where it applied to the stop —
    # the ATR branch. On the structure branch the stop is a price the regime
    # never scaled, so scaling its target would move R without moving risk. See
    # REGIME SYMMETRY in the module docstring.
    target_k = regime_k if (p["regime_scales_target"] and stop_source == "atr") else 1.0
    target   = anchor + (p["target_atr_mult"] * target_k * atr_abs)

    reward = target - entry_price
    if reward <= 0:
        return _invalid(entry_price, "target_already_reached", stop=stop,
                        target=target, anchor=anchor)

    rr = reward / risk

    # ── Validity ──────────────────────────────────────────────────────────────
    if risk_pct > p["max_risk_pct"]:
        return _invalid(entry_price, f"risk_too_wide_{risk_pct:.1f}pct",
                        stop=stop, target=target, anchor=anchor)

    # ── Cost ──────────────────────────────────────────────────────────────────
    # What this plan's own clip pays to open and close, expressed in units of
    # this plan's own risk. Computed and REPORTED unconditionally; enforced only
    # behind `risk_min_planned_r_enabled`.
    clip_qty, friction_r, cost_basis = _plan_friction(entry_price, risk, p)
    # Rounded FIRST, then required_rr derived from the rounded figure, so the
    # two numbers a human reads reconcile through the identity rather than
    # disagreeing in the fourth decimal.
    friction_r  = round(friction_r, 4)
    required_rr = (round(required_planned_r(friction_r, p["plan_hit_rate"]), 3)
                   if cost_basis == "ledger" else 0.0)

    levels = TradeLevels(
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
        regime_k         = regime_k,
        clip_qty         = clip_qty,
        clip_value       = round(clip_qty * entry_price, 2),
        friction_r       = friction_r,
        required_rr      = required_rr,
        cost_basis       = cost_basis,
    )

    # A plan whose clip could not be sized, or whose charge schedule could not
    # be read, has NO opinion about cost — which must not be spelled the same
    # way as "measured and found wanting". Refusing to fund a share is
    # `portfolio_constraints`' job and it names that reason itself; pre-empting
    # it here would hide the real one behind a cost verdict never computed.
    if p["min_planned_r"] and cost_basis == "ledger":
        bar = required_rr + p["plan_r_margin"]
        if levels.rr < bar:
            return _invalid(
                entry_price,
                f"below_min_planned_r_{levels.rr:.3f}_needs_{bar:.3f}",
                stop=stop, target=target, anchor=anchor)

    return levels


def _plan_friction(entry_price: float, risk_per_share: float,
                   p: dict) -> tuple[int, float, str]:
    """Clip, friction in R, and which basis produced it."""
    qty, _binding = plan_clip(entry_price, risk_per_share, _plan_capital(p),
                              risk_pct_per_trade = p["risk_pct_per_trade"],
                              max_position_pct   = p["max_position_pct"])
    if qty <= 0:
        return 0, 0.0, "unfunded"
    try:
        cost = _statutory_round_trip(entry_price, qty, p["plan_product"])
    except Exception:
        return qty, 0.0, "unavailable"
    risk_rupees = qty * risk_per_share
    if risk_rupees <= 0:
        return qty, 0.0, "unfunded"
    return qty, cost / risk_rupees, "ledger"


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

    # The two capital terms come from plan_clip so the planner and the sizer
    # cannot disagree about what this trade's clip is.
    qty_plan, binding = plan_clip(
        levels.entry, levels.risk_per_share, capital,
        risk_pct_per_trade = risk_pct_per_trade,
        max_position_pct   = max_position_pct)
    qty_by_avail = int(available // levels.entry) if levels.entry > 0 else 0

    qty = max(0, min(qty_plan, qty_by_avail))
    if qty == 0:
        return PositionSize(0, 0.0, 0.0, 0.0, "capital")

    capped_by = binding if qty == qty_plan else "capital"

    invested = qty * levels.entry
    risk_amt = qty * levels.risk_per_share
    return PositionSize(
        quantity            = qty,
        invested_value      = round(invested, 2),
        risk_amount         = round(risk_amt, 2),
        risk_pct_of_capital = round(risk_amt / capital * 100.0, 3),
        capped_by           = capped_by,
    )
