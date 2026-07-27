"""
TradeOS v6 — Trade Decision
============================
Turns a candidate plus a live price into an ACTION.

WHY THIS EXISTS
---------------
The alert layer served snapshot fields — score, conviction, entry zone — and
left the actual decision to the reader. Two things made that unavoidable:

  1. signal_output_daily.planned_stop / planned_target / implied_rr were 100%
     NULL, because final_snapshot never carried them forward. There was no
     stop, target or reward:risk to print. (Fixed; 41/43 populated.)
  2. Live price only reached TIER_1, via yfinance, on a 15-minute delay. A
     TIER_2 candidate was evaluated against last night's close all day.

With both fixed, an alert can answer the only question that matters when you
read it: at THIS price, right now, is this still a trade — and how big?

DECISION, NOT DESCRIPTION
-------------------------
Every field here is something you act on. The R:R is recomputed at the live
price rather than restated from last night, because a candidate that was 1.4R
at yesterday's close may be 0.6R after a gap-up — and 0.6R is a different
decision, not a smaller version of the same one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


# Actions, ordered by how much they demand of the reader.
ACT_BUY        = "BUY_NOW"        # in zone, R:R holds — execute
ACT_CHASE      = "CHASE_LIMIT"    # above zone but inside the approved chase
ACT_WAIT       = "WAIT"           # below/approaching zone — set an alert, do nothing
ACT_SKIP       = "SKIP"           # R:R gone, target hit, or stop broken
ACT_UNKNOWN    = "NO_DATA"        # missing plan or price — say so, do not guess


@dataclass
class Decision:
    symbol:        str
    action:        str
    headline:      str          # one line, the thing you read first
    reason:        str
    live_price:    float | None
    entry:         float | None
    stop:          float | None
    target:        float | None
    rr_live:       float | None   # reward:risk AT the live price
    risk_pct:      float | None   # stop distance from live price, %
    upside_pct:    float | None
    dist_to_zone_pct: float | None
    qty:           int | None
    invested:      float | None
    risk_amount:   float | None
    stale_price:   bool = False   # true when we fell back to the EOD close

    def as_dict(self) -> dict:
        return asdict(self)


def decide(
    row: dict,
    live_price: float | None,
    *,
    total_capital: float | None = None,
    open_positions: list[dict] | None = None,
    regime: str = "NEUTRAL",
    min_rr: float = 1.0,
    max_chase_pct: float | None = None,
) -> Decision:
    """
    row: a signal_output_daily / master_shortlist row carrying planned_stop,
         planned_target, entry_zone_low/high and current_price.
    live_price: real-time price. None falls back to row['current_price'] and
         the result is flagged stale_price so the caller can say so rather
         than presenting yesterday's number as today's.
    """
    sym   = row.get("symbol") or "?"
    stop  = _f(row.get("planned_stop"))
    tgt   = _f(row.get("planned_target"))
    zlo   = _f(row.get("entry_zone_low"))
    zhi   = _f(row.get("entry_zone_high")) or (zlo * 1.02 if zlo else None)

    stale = live_price is None
    px    = live_price if live_price else _f(row.get("current_price"))

    if not px or not stop or not tgt:
        missing = [n for n, v in (("price", px), ("stop", stop), ("target", tgt)) if not v]
        return Decision(sym, ACT_UNKNOWN,
                        f"{sym}: no decision — missing {', '.join(missing)}",
                        f"missing {', '.join(missing)}",
                        px, zlo, stop, tgt, None, None, None, None, None, None, None,
                        stale_price=stale)

    # ── Hard invalidations, checked before anything else ─────────────────────
    if px <= stop:
        return Decision(sym, ACT_SKIP,
                        f"{sym}: SKIP — price {px:.2f} is at or below the stop {stop:.2f}",
                        "setup invalidated: stop broken", px, zlo, stop, tgt,
                        None, None, None, None, None, None, None, stale_price=stale)
    if px >= tgt:
        return Decision(sym, ACT_SKIP,
                        f"{sym}: SKIP — price {px:.2f} already at target {tgt:.2f}",
                        "no upside left against this plan", px, zlo, stop, tgt,
                        0.0, None, 0.0, None, None, None, None, stale_price=stale)

    risk     = px - stop
    reward   = tgt - px
    rr       = reward / risk
    risk_pct = risk / px * 100
    up_pct   = reward / px * 100

    # ── Position size, respecting the live portfolio ─────────────────────────
    qty = invested = risk_amt = None
    try:
        from analysis.portfolio_constraints import check_new_entry
        v = check_new_entry(
            sym, row.get("sector") or "", row.get("industry") or "",
            px, risk, open_positions or [],
            regime=regime, total_capital=total_capital)
        if v.allowed:
            qty, invested, risk_amt = v.max_qty, v.max_value, round(v.max_qty * risk, 2)
        else:
            # A blocked entry is a decision too — and a more useful one than a
            # size, because it names the constraint.
            return Decision(sym, ACT_SKIP,
                            f"{sym}: SKIP — {v.reason.replace('_', ' ')}",
                            v.detail, px, zlo, stop, tgt, round(rr, 2),
                            round(risk_pct, 2), round(up_pct, 2), None,
                            0, 0.0, 0.0, stale_price=stale)
    except Exception as e:
        logger.debug(f"  {sym}: sizing unavailable — {e}")

    # ── Where is price relative to the zone? ─────────────────────────────────
    dist_zone = None
    if zlo:
        anchor    = zhi if (zhi and px > zhi) else zlo
        dist_zone = (px - anchor) / anchor * 100

    def _mk(action, headline, reason):
        return Decision(sym, action, headline, reason, px, zlo, stop, tgt,
                        round(rr, 2), round(risk_pct, 2), round(up_pct, 2),
                        round(dist_zone, 2) if dist_zone is not None else None,
                        qty, invested, risk_amt, stale_price=stale)

    size_note = f" · {qty} sh ≈ ₹{invested:,.0f}, risk ₹{risk_amt:,.0f}" if qty else ""

    if rr < min_rr:
        return _mk(ACT_SKIP,
                   f"{sym}: SKIP — R:R {rr:.2f} below {min_rr:g} at {px:.2f}",
                   f"reward {up_pct:.1f}% vs risk {risk_pct:.1f}% no longer justifies the trade")

    if zlo and px < zlo:
        gap = (zlo - px) / zlo * 100
        return _mk(ACT_WAIT,
                   f"{sym}: WAIT — {gap:.1f}% below the zone ({zlo:.2f})",
                   f"set an alert at {zlo:.2f}; R:R would be {rr:.2f}")

    if zhi and px > zhi:
        chase = (px - zhi) / zhi * 100
        if max_chase_pct is not None and chase > max_chase_pct:
            return _mk(ACT_SKIP,
                       f"{sym}: SKIP — {chase:.1f}% above the zone, past the {max_chase_pct:.1f}% chase limit",
                       "entering here pays for a move that has already happened")
        return _mk(ACT_CHASE,
                   f"{sym}: CHASE OK — {chase:.1f}% above zone, R:R still {rr:.2f}{size_note}",
                   f"stop {stop:.2f} · target {tgt:.2f}")

    return _mk(ACT_BUY,
               f"{sym}: BUY — in zone at {px:.2f}, R:R {rr:.2f}{size_note}",
               f"stop {stop:.2f} ({risk_pct:.1f}%) · target {tgt:.2f} (+{up_pct:.1f}%)")


def _f(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# LIVE PRICES
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_prices(symbols: list[str]) -> tuple[dict[str, float], str]:
    """
    Live prices for a symbol list, with the source named.

    Kite first: it is real-time and already authenticated for the intraday
    monitor. yfinance is the fallback and is ~15 minutes delayed — fine for a
    zone-proximity nudge, NOT fine for a stop decision, so the caller is told
    which one it got rather than having to assume.

    Returns ({symbol: price}, source) where source is 'kite' | 'yfinance' | 'none'.
    """
    if not symbols:
        return {}, "none"

    try:
        from kite.kite_client import fetch_ltp, is_available
        if is_available():
            px = fetch_ltp(symbols)
            live = {k: v for k, v in px.items() if v}
            if live:
                return live, "kite"
    except Exception as e:
        logger.debug(f"  live price via Kite unavailable: {e}")

    try:
        import yfinance as yf
        out: dict[str, float] = {}
        tk = yf.Tickers(" ".join(f"{s}.NS" for s in symbols))
        for s in symbols:
            try:
                fi = tk.tickers.get(f"{s}.NS").fast_info
                p = float(getattr(fi, "last_price", 0) or 0)
                if p:
                    out[s] = p
            except Exception:
                continue
        if out:
            return out, "yfinance"
    except Exception as e:
        logger.debug(f"  live price via yfinance unavailable: {e}")

    return {}, "none"
