"""
TradeOS v7 — Portfolio Construction Constraints
================================================
Decides whether a NEW entry is admissible given what is already held, and
sizes it.

WHY THIS IS SEPARATE FROM THE SCREENER
--------------------------------------
Three different questions were previously answered by one blunt instrument
(`max_per_sector` on a 30-name shortlist):

  1. What should I RESEARCH?   -> breadth is free; wider is strictly better.
                                  Handled by the sector floor in screen_stocks.
  2. What should I BUY today?  -> quality first. A count cap here would refuse
                                  the third good setup in a genuinely leading
                                  sector, which for a 1-3 week momentum system
                                  is refusing the actual edge.
  3. What may I HOLD at once?  -> this module. The binding constraint.

Concentration risk is RUPEES AT RISK, not a count of tickers. Three positions
of Rs.15,000 in one sector is a smaller bet than one of Rs.60,000, yet a count
cap treats the first as three times worse. Everything below is therefore
measured against capital.

WHY INDUSTRY, NOT ONLY SECTOR
-----------------------------
`sector` in this dataset is coarse — 'financials' spans 70 stocks covering
banks, NBFCs and insurers. `industry` is the honest correlation unit (83 groups
over the same universe, populated 499/499). Two stocks in the same industry are
close to one position held twice; two in the same sector may not be correlated
at all. So the sector limit is capital-based and generous, while the industry
limit is a hard count.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (cfg_float, cfg_int, cfg, capital_for,
                    SIZING_REGIME_BASE_TRENDING, SIZING_REGIME_BASE_RISK_ON,
                    SIZING_REGIME_BASE_NEUTRAL, SIZING_REGIME_BASE_RECOVERING,
                    SIZING_REGIME_BASE_RISK_OFF, SIZING_REGIME_CONF_WEIGHT,
                    SIZING_REGIME_MAX_MULT, SIZING_DD_THRESHOLD_PCT,
                    SIZING_DD_STEP_PCT, SIZING_DD_STEP_MULT, SIZING_DD_MIN_MULT,
                    SIZING_WINRATE_LOOKBACK, SIZING_WINRATE_MIN_PCT,
                    SIZING_WINRATE_MAX_PCT, SIZING_WINRATE_LOW_MULT,
                    SIZING_WINRATE_HIGH_MULT, SIZING_MOMENTUM_MAX,
                    SIZING_CONSECUTIVE_WIN_BONUS, SIZING_CONSECUTIVE_LOSS_PENALTY,
                    SIZING_MIN_MULT, SIZING_MAX_MULT)


# Maximum share of DEPLOYED capital permitted in one sector, by regime.
# Trending markets reward concentration in whatever is working; stressed
# markets punish it, because correlations converge toward 1 exactly when a
# concentrated book can least afford it.
REGIME_SECTOR_CAP_PCT = {
    "TRENDING":   40.0,
    "RISK ON":    40.0,
    "NEUTRAL":    30.0,
    "RECOVERING": 25.0,
    "RISK OFF":   25.0,
}


@dataclass
class ConstraintVerdict:
    allowed:       bool
    reason:        str
    detail:        str
    max_qty:       int = 0
    max_value:     float = 0.0
    sector_pct_after:   float = 0.0
    industry_count_after: int = 0


def load_constraints(regime: str = "NEUTRAL") -> dict:
    regime_u = (regime or "NEUTRAL").upper()
    default_cap = REGIME_SECTOR_CAP_PCT.get(regime_u, 30.0)
    return {
        "sector_cap_pct":     cfg_float(f"portfolio_sector_cap_{regime_u.replace(' ', '_')}",
                                        default_cap),
        "max_per_industry":   cfg_int("portfolio_max_per_industry", 2),
        "max_positions":      cfg_int(f"max_positions_{regime_u.replace(' ', '_').lower()}",
                                      cfg_int("max_positions_neutral", 7)),
        "max_position_pct":   cfg_float("max_position_pct", 20.0),
        "risk_pct_per_trade": cfg_float("risk_pct_per_trade", 1.0),
        "max_portfolio_risk_pct": cfg_float("portfolio_max_total_risk_pct", 6.0),
    }


def compute_dynamic_multiplier(
    regime: str,
    regime_confidence: float = 0.5,
    session_pnl_pct: float = 0.0,
    recent_win_rate: float | None = None,
    consecutive_wins: int = 0,
    consecutive_losses: int = 0,
    setup_quality: float = 0.5,
    vol_mult: float = 1.0,
) -> float:
    """
    Composite dynamic position-size multiplier.

    All inputs are optional with safe defaults (1.0x = no behavior change).
    Returns value between SIZING_MIN_MULT (0.10) and SIZING_MAX_MULT (2.00).

    Components (multiplicative, self-limiting):
      1. Regime base × confidence scaling      (market structure only, NOT P&L)
      2. Drawdown scaling                       (from in-memory session P&L)
      3. Win rate scaling                       (from in-memory trade counter)
      4. Consecutive wins / losses              (momentum streak)
      5. Setup quality / momentum               (from candidate row)
      6. Volatility                             (already computed upstream)
    """
    mult = 1.0

    # ── 1. Regime base + confidence scaling (market structure only) ──
    regime_u = (regime or "NEUTRAL").upper()
    base_map = {
        "TRENDING":   cfg_float("sizing_regime_base_trending",
                                SIZING_REGIME_BASE_TRENDING),
        "RISK ON":    cfg_float("sizing_regime_base_risk_on",
                                SIZING_REGIME_BASE_RISK_ON),
        "NEUTRAL":    cfg_float("sizing_regime_base_neutral",
                                SIZING_REGIME_BASE_NEUTRAL),
        "RECOVERING": cfg_float("sizing_regime_base_recovering",
                                SIZING_REGIME_BASE_RECOVERING),
        "RISK OFF":   cfg_float("sizing_regime_base_risk_off",
                                SIZING_REGIME_BASE_RISK_OFF),
    }
    regime_mult = base_map.get(regime_u, 1.0)

    # Confidence scales the regime multiplier (up to SIZING_REGIME_MAX_MULT)
    conf_weight = cfg_float("sizing_regime_conf_weight", SIZING_REGIME_CONF_WEIGHT)
    conf_steps = int(regime_confidence * 10)  # 0 to 10
    regime_mult += conf_steps * conf_weight
    regime_mult = min(regime_mult, cfg_float("sizing_regime_max_mult",
                                             SIZING_REGIME_MAX_MULT))
    mult *= regime_mult

    # ── 2. Drawdown scaling (from in-memory session P&L) ──
    dd_thresh = cfg_float("sizing_dd_threshold_pct", SIZING_DD_THRESHOLD_PCT)
    dd_step = cfg_float("sizing_dd_step_pct", SIZING_DD_STEP_PCT)
    dd_step_mult = cfg_float("sizing_dd_step_mult", SIZING_DD_STEP_MULT)
    dd_min = cfg_float("sizing_dd_min_mult", SIZING_DD_MIN_MULT)

    if session_pnl_pct < -dd_thresh:
        steps = int(abs(session_pnl_pct + dd_thresh) / dd_step) + 1
        dd_mult = 1.0 - (steps * dd_step_mult)
        dd_mult = max(dd_min, dd_mult)
        mult *= dd_mult

    # ── 3. Win rate scaling (in-memory counter) ──
    wr_min = cfg_float("sizing_winrate_min_pct", SIZING_WINRATE_MIN_PCT)
    wr_max = cfg_float("sizing_winrate_max_pct", SIZING_WINRATE_MAX_PCT)
    if recent_win_rate is not None:
        if recent_win_rate < wr_min / 100.0:
            mult *= cfg_float("sizing_winrate_low_mult", SIZING_WINRATE_LOW_MULT)
        elif recent_win_rate > wr_max / 100.0:
            mult *= cfg_float("sizing_winrate_high_mult", SIZING_WINRATE_HIGH_MULT)

    # ── 4. Consecutive wins / losses ──
    win_bonus = cfg_float("sizing_consecutive_win_bonus", SIZING_CONSECUTIVE_WIN_BONUS)
    loss_penalty = cfg_float("sizing_consecutive_loss_penalty",
                             SIZING_CONSECUTIVE_LOSS_PENALTY)
    mult *= (1.0 + min(consecutive_wins * win_bonus, 0.20))
    mult *= (1.0 - min(consecutive_losses * loss_penalty, 0.30))

    # ── 5. Setup quality / momentum ──
    momentum_max = cfg_float("sizing_momentum_max", SIZING_MOMENTUM_MAX)
    momentum_mult = 1.0 + (setup_quality - 0.5) * (momentum_max - 1.0)
    mult *= max(0.85, min(momentum_mult, momentum_max))

    # ── 6. Volatility (already computed upstream) ──
    mult *= vol_mult

    # ── 7. Global clamp ──
    return max(cfg_float("sizing_min_mult", SIZING_MIN_MULT),
               min(cfg_float("sizing_max_mult", SIZING_MAX_MULT), mult))


def compute_blended_stop(entry_qty: int, entry_stop: float,
                         addon_qty: int, addon_stop: float) -> float:
    """
    Blended stop for a combined position after scale-in.
    Weighted average of entry stop and add-on stop, rounded to 2 decimals.
    """
    total_qty = entry_qty + addon_qty
    if total_qty <= 0:
        return entry_stop
    blended = (entry_qty * entry_stop + addon_qty * addon_stop) / total_qty
    return round(blended, 2)


def compute_scale_in_qty(
    entry_price: float,
    entry_qty: int,
    entry_stop: float,
    current_price: float,
    capital: float,
    remaining_risk_budget: float,
    size_mult: float = 1.0,
    scale_in_risk_pct: float = 0.5,
    scale_in_stop_tighten_pct: float = 1.0,
    max_position_pct: float = 100.0,
) -> dict:
    """
    Compute add-on quantity for scaling into a winning position.

    Returns dict with keys:
      addon_qty: number of shares to add (0 if no scale-in)
      addon_stop: stop price for the add-on shares
      blended_stop: combined stop for the full position
      addon_value: value of the add-on
      combined_value: total position value after add-on
      reason: why scale-in was or wasn't allowed
    """
    result = {
        "addon_qty": 0,
        "addon_stop": 0.0,
        "blended_stop": entry_stop,
        "addon_value": 0.0,
        "combined_value": entry_qty * entry_price,
        "reason": "not evaluated",
    }

    if entry_price <= 0 or current_price <= 0:
        result["reason"] = "invalid price"
        return result

    # New stop is tighter: % below current price
    addon_stop = round(current_price * (1 - scale_in_stop_tighten_pct / 100.0), 2)
    risk_per_share_addon = current_price - addon_stop
    if risk_per_share_addon <= 0:
        result["reason"] = "addon risk per share <= 0"
        return result

    # Risk budget for the add-on
    addon_risk_budget = capital * (scale_in_risk_pct / 100.0) * size_mult
    addon_risk_budget = min(addon_risk_budget, remaining_risk_budget)

    if addon_risk_budget <= 0:
        result["reason"] = "no risk budget remaining"
        return result

    addon_qty = int(addon_risk_budget // risk_per_share_addon)
    if addon_qty <= 0:
        result["reason"] = "computed qty is 0"
        return result

    addon_value = addon_qty * current_price
    combined_value = entry_qty * current_price + addon_value
    max_combined = capital * (max_position_pct / 100.0)

    # Cap to max_position_pct of capital
    if combined_value > max_combined:
        addon_qty = max(0, int((max_combined - entry_qty * current_price) // current_price))
        if addon_qty <= 0:
            result["reason"] = "combined position exceeds max_position_pct cap"
            return result
        addon_value = addon_qty * current_price
        combined_value = entry_qty * current_price + addon_value

    blended = compute_blended_stop(entry_qty, entry_stop, addon_qty, addon_stop)

    result.update({
        "addon_qty": addon_qty,
        "addon_stop": addon_stop,
        "blended_stop": blended,
        "addon_value": round(addon_value, 2),
        "combined_value": round(combined_value, 2),
        "reason": "ok",
    })
    return result


def compute_scale_out_qty(
    current_qty: int,
    current_price: float,
    scale_out_pct: int = 50,
) -> dict:
    """
    Compute partial exit quantity for scaling out of a losing or deteriorating position.
    Returns dict with keys:
      scale_out_qty: number of shares to sell (0 if none)
      remaining_qty: shares left after scale-out
      scale_out_value: value of the sale
      reason: why scale-out was or wasn't allowed
    """
    result = {
        "scale_out_qty": 0,
        "remaining_qty": current_qty,
        "scale_out_value": 0.0,
        "reason": "not evaluated",
    }

    if current_qty <= 0 or current_price <= 0:
        result["reason"] = "invalid qty or price"
        return result

    scale_out_qty = max(1, int(current_qty * scale_out_pct / 100.0))
    if scale_out_qty >= current_qty:
        scale_out_qty = max(1, current_qty - 1)  # always keep at least 1 share

    result.update({
        "scale_out_qty": scale_out_qty,
        "remaining_qty": current_qty - scale_out_qty,
        "scale_out_value": round(scale_out_qty * current_price, 2),
        "reason": "ok",
    })
    return result


def summarise_portfolio(open_positions: list[dict]) -> dict:
    """Current exposure by sector and industry, plus total open risk."""
    total_value = 0.0
    """Current exposure by sector and industry, plus total open risk."""
    total_value = 0.0
    by_sector:   dict[str, float] = {}
    by_industry: dict[str, int]   = {}
    open_risk    = 0.0

    for p in open_positions:
        val = float(p.get("invested_value") or 0)
        if not val:
            qty = float(p.get("current_qty") or p.get("actual_qty") or 0)
            val = qty * float(p.get("entry_price") or 0)
        total_value += val

        sec = (p.get("sector") or "unknown").lower()
        by_sector[sec] = by_sector.get(sec, 0.0) + val

        ind = (p.get("industry") or "").lower()
        if ind:
            by_industry[ind] = by_industry.get(ind, 0) + 1

        # Rupees still at risk = distance from live price to the ACTIVE stop.
        # Once the stop is at or above entry the position risks nothing further,
        # so it must not consume the portfolio risk budget.
        entry = float(p.get("entry_price") or 0)
        sl    = float(p.get("active_sl") or p.get("planned_stop") or 0)
        qty   = float(p.get("current_qty") or p.get("actual_qty") or 0)
        cur   = float(p.get("current_price") or entry)
        if sl and qty and cur > sl:
            open_risk += (cur - sl) * qty

    return {
        "total_value":  round(total_value, 2),
        "by_sector":    by_sector,
        "by_industry":  by_industry,
        "open_risk":    round(open_risk, 2),
        "n_positions":  len(open_positions),
    }


def check_new_entry(
    symbol:          str,
    sector:          str,
    industry:        str,
    entry_price:     float,
    risk_per_share:  float,
    open_positions:  list[dict],
    *,
    regime:          str = "NEUTRAL",
    total_capital:   float | None = None,
    available_cash:  float | None = None,
    product:         str = "CNC",
    vol_mult:        float = 1.0,
    size_mult:       float = 1.0,
) -> ConstraintVerdict:
    """
    Admissibility + maximum size for one prospective entry.

    Checked in order of how hard the constraint is:
      1. position count      — regime-scaled slot limit
      2. industry count      — correlation guard (hard count, default 2)
      3. portfolio risk       — total open risk budget across all positions
      4. sector capital cap   — regime-scaled share of deployed capital
      5. per-position cap     — no single name dominates
      5b. volatility exposure — book-level scaledown in high-VIX regimes
      6. minimum viable trade — friction against the risk budget, per product

    `product` decides which charge schedule step 6 prices against. It defaults
    to CNC because every caller of this function today is the swing book; an
    intraday caller must pass MIS, and passing nothing is the safe direction
    since CNC is the dearer of the two.

    `vol_mult` is analysis.overlays.vol_exposure()'s multiplier, read ONCE per
    slow tick by the caller and passed in rather than fetched here — this
    function is called per-candidate, and vol_exposure() reads Supabase, so
    fetching it here would turn one book-level read into dozens per cycle.
    Defaults to 1.0 (no scaling), reproducing today's behaviour exactly for
    any caller that does not pass it.
    """
    C       = load_constraints(regime)
    # The docstring above states every caller of this function today is the
    # swing book, so the FALLBACK is swing's sleeve, not the pooled total. An
    # intraday caller must pass total_capital explicitly, exactly as it must
    # pass product=MIS. While intraday is PAPER the two numbers are identical,
    # so this is a no-op today and correct on the day intraday goes live.
    capital = total_capital if total_capital is not None else capital_for("SWING")
    summary = summarise_portfolio(open_positions)
    sec     = (sector or "unknown").lower()
    ind     = (industry or "").lower()

    if entry_price <= 0:
        return ConstraintVerdict(False, "invalid_price", f"entry_price={entry_price}")

    # ── 1. Slot limit ────────────────────────────────────────────────────────
    if summary["n_positions"] >= C["max_positions"]:
        return ConstraintVerdict(
            False, "max_positions",
            f"{summary['n_positions']}/{C['max_positions']} slots used in {regime}. "
            f"Close something before adding.")

    # ── 2. Industry correlation guard ────────────────────────────────────────
    ind_count = summary["by_industry"].get(ind, 0)
    if ind and ind_count >= C["max_per_industry"]:
        return ConstraintVerdict(
            False, "industry_concentration",
            f"already hold {ind_count} in industry '{industry}' "
            f"(max {C['max_per_industry']}). Same-industry names are effectively "
            f"one position held twice.",
            industry_count_after=ind_count)

    # ── 3. Portfolio risk budget ─────────────────────────────────────────────
    risk_budget_total = capital * C["max_portfolio_risk_pct"] / 100.0
    risk_remaining    = risk_budget_total - summary["open_risk"]
    if risk_remaining <= 0:
        return ConstraintVerdict(
            False, "portfolio_risk_exhausted",
            f"open risk Rs.{summary['open_risk']:,.0f} already at the "
            f"{C['max_portfolio_risk_pct']}% budget (Rs.{risk_budget_total:,.0f}).")

    # ── 4. Sector capital cap ────────────────────────────────────────────────
    # Measured against capital, not against currently-deployed value: with an
    # empty book, deployed value is 0 and every sector would read as 100%.
    sector_cap_value = capital * C["sector_cap_pct"] / 100.0
    sector_now       = summary["by_sector"].get(sec, 0.0)
    sector_headroom  = sector_cap_value - sector_now
    if sector_headroom <= 0:
        return ConstraintVerdict(
            False, "sector_concentration",
            f"sector '{sector}' already at Rs.{sector_now:,.0f} of the "
            f"Rs.{sector_cap_value:,.0f} cap ({C['sector_cap_pct']}% in {regime}).",
            sector_pct_after=round(sector_now / capital * 100, 1))

    # ── 5. Size it ───────────────────────────────────────────────────────────
    risk_budget_trade = capital * C["risk_pct_per_trade"] / 100.0
    risk_budget_trade = min(risk_budget_trade, risk_remaining)

    qty_by_risk   = int(risk_budget_trade // risk_per_share) if risk_per_share > 0 else 0
    qty_by_sector = int(sector_headroom // entry_price)
    qty_by_maxpos = int((capital * C["max_position_pct"] / 100.0) // entry_price)
    qty_by_cash   = int(available_cash // entry_price) if available_cash is not None else qty_by_maxpos

    qty = max(0, min(qty_by_risk, qty_by_sector, qty_by_maxpos, qty_by_cash))

    # ── Apply dynamic size multiplier ──
    # Scales qty up/down based on regime, drawdown, win rate, streak, setup quality.
    # Defaults to 1.0x = no behavior change.
    qty = int(qty * size_mult)

    # ── Volatility-regime exposure scaling (Stage 9 overlay) ─────────────────
    # Applied to the already-capped qty, not to `capital` upstream of it — so
    # it changes what gets bought without also loosening or tightening the
    # sector/industry caps above, which exist for a correlation reason, not a
    # volatility one. See analysis.overlays.vol_exposure() for the VIX bands.
    qty_before_vol = qty
    if vol_mult < 1.0:
        qty = int(qty * vol_mult)

    if qty == 0:
        if qty_before_vol > 0:
            binding = "volatility exposure scaling"
        else:
            binding = min(
                [(qty_by_risk, "risk budget"), (qty_by_sector, "sector cap"),
                 (qty_by_maxpos, "max position size"), (qty_by_cash, "available cash")],
                key=lambda t: t[0])[1]
        return ConstraintVerdict(
            False, "size_zero",
            f"computed size is 0 — binding constraint: {binding}")

    value = qty * entry_price

    # ── Minimum viable position ──────────────────────────────────────────────
    # A residual cap can leave just enough headroom for a token position — a
    # sector 92% full admitted a single Rs.3,500 share in testing. Below roughly
    # 3% of capital the position cannot influence portfolio returns while still
    # paying full brokerage, STT and slippage, and it consumes one of only 7
    # slots. Refuse it and say the sector is effectively full.
    min_pos_pct   = cfg_float("portfolio_min_position_pct", 3.0)
    min_pos_value = capital * min_pos_pct / 100.0
    if value < min_pos_value:
        return ConstraintVerdict(
            False, "below_min_position",
            f"only Rs.{value:,.0f} fits ({qty} sh) against a Rs.{min_pos_value:,.0f} "
            f"minimum ({min_pos_pct}% of capital) — sector '{sector}' is "
            f"effectively full at Rs.{sector_now:,.0f}/Rs.{sector_cap_value:,.0f}. "
            f"A position this small pays full costs and occupies a slot without "
            f"moving portfolio returns.",
            max_qty=qty, max_value=round(value, 2))

    # ── Minimum VIABLE trade — friction measured against the risk budget ─────
    #
    # The rule above is a proxy: it assumes a position worth 3% of capital is
    # large enough to outrun its costs. It does not look at the costs. Measured
    # across 91 closed trades on 04-Aug-2026, that assumption fails badly at
    # the small end of the delivery book:
    #
    #     CNC clip  Rs     0-2,500   friction = 2.363 R      net R  -3.180
    #     CNC clip  Rs 2,500-5,000   friction = 1.046 R      net R  -0.598
    #     CNC clip  Rs 10,000+       friction = 0.605 R      net R  +1.322
    #
    # A trade whose friction is 2.4x its own risk budget cannot be won. It is
    # not a marginal trade or a low-edge trade; the arithmetic excludes it
    # before the thesis is considered. The flat Rs 15.04 DP fee is most of it,
    # which is why the lever is clip size and not target.
    #
    # cost_R is the architecture's own currency (§13): round-trip friction
    # divided by the risk the trade is sized to. Refusing on it means refusing
    # on the same quantity the allocator will later rank on.
    #
    # DEFAULTS TO OFF. A gate that refuses trades changes what the account
    # does with money, and this one would have refused a meaningful share of
    # the existing book. sizing_max_cost_r = 0 disables it, reproducing today's
    # behaviour exactly. See RETENTION_PROPOSAL.md's sibling section in
    # docs/6_IMPLEMENTATION_STATUS.md for the value proposed and the evidence.
    max_cost_r = cfg_float("sizing_max_cost_r", 0.0)
    if max_cost_r > 0 and risk_per_share > 0:
        try:
            from intraday.cost_model import round_trip
            friction = round_trip(entry_price, qty, product=product).total
            cost_r   = friction / (qty * risk_per_share)
        except Exception:
            cost_r = 0.0                      # never refuse on a broken cost model
        if cost_r > max_cost_r:
            need = friction / max_cost_r / risk_per_share
            return ConstraintVerdict(
                False, "friction_exceeds_budget",
                f"friction Rs.{friction:,.0f} is {cost_r:.2f}R against a "
                f"Rs.{qty * risk_per_share:,.0f} risk budget, over the "
                f"{max_cost_r:.2f}R limit — this {product} trade pays "
                f"{cost_r:.0%} of what it risks just to open and close. "
                f"It needs ~{int(need)} shares (Rs.{need * entry_price:,.0f}) "
                f"to clear the bar at this stop.",
                max_qty=qty, max_value=round(value, 2))

    return ConstraintVerdict(
        True, "ok",
        f"{qty} sh @ Rs.{entry_price:.2f} = Rs.{value:,.0f} | "
        f"risk Rs.{qty * risk_per_share:,.0f} "
        f"({qty * risk_per_share / capital * 100:.2f}% of capital) | "
        f"sector '{sector}' -> {(sector_now + value) / capital * 100:.1f}% "
        f"of Rs.{capital:,.0f}",
        max_qty=qty, max_value=round(value, 2),
        sector_pct_after=round((sector_now + value) / capital * 100, 1),
        industry_count_after=ind_count + 1,
    )


def annotate_candidates(candidates: list[dict], open_positions: list[dict],
                        regime: str = "NEUTRAL",
                        total_capital: float | None = None) -> list[dict]:
    """
    Stamp each ranked candidate with its portfolio verdict, in rank order, so
    the AI decision engine and the alert layer can show WHY a high-scoring name
    is not actionable today ("sector cap reached", "3rd in the same industry")
    instead of silently dropping it.

    Simulates sequential acceptance: an earlier candidate consuming the sector
    budget correctly constrains a later one from the same sector.
    """
    working = list(open_positions)
    out     = []

    for c in candidates:
        entry = float(c.get("current_price") or c.get("planned_entry") or 0)
        stop  = float(c.get("planned_stop") or 0)
        rps   = (entry - stop) if (entry and stop and stop < entry) else entry * 0.05

        v = check_new_entry(
            c.get("symbol", ""), c.get("sector", ""), c.get("industry", ""),
            entry, rps, working, regime=regime, total_capital=total_capital)

        enriched = dict(c)
        enriched.update({
            "pc_allowed":     v.allowed,
            "pc_reason":      v.reason,
            "pc_detail":      v.detail,
            "pc_max_qty":     v.max_qty,
            "pc_max_value":   v.max_value,
            "pc_sector_pct":  v.sector_pct_after,
        })
        out.append(enriched)

        # Provisionally accept so the next candidate sees the updated book.
        if v.allowed:
            working.append({
                "symbol": c.get("symbol"), "sector": c.get("sector"),
                "industry": c.get("industry"), "invested_value": v.max_value,
                "entry_price": entry, "current_price": entry,
                "active_sl": stop, "current_qty": v.max_qty,
            })
    return out
