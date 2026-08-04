"""
TradeOS v6 — Portfolio Construction Constraints
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
from config import cfg_float, cfg_int, cfg, TOTAL_CAPITAL


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


def summarise_portfolio(open_positions: list[dict]) -> dict:
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
) -> ConstraintVerdict:
    """
    Admissibility + maximum size for one prospective entry.

    Checked in order of how hard the constraint is:
      1. position count      — regime-scaled slot limit
      2. industry count      — correlation guard (hard count, default 2)
      3. portfolio risk       — total open risk budget across all positions
      4. sector capital cap   — regime-scaled share of deployed capital
      5. per-position cap     — no single name dominates
      6. minimum viable trade — friction against the risk budget, per product

    `product` decides which charge schedule step 6 prices against. It defaults
    to CNC because every caller of this function today is the swing book; an
    intraday caller must pass MIS, and passing nothing is the safe direction
    since CNC is the dearer of the two.
    """
    C       = load_constraints(regime)
    capital = total_capital if total_capital is not None else TOTAL_CAPITAL
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
    if qty == 0:
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
