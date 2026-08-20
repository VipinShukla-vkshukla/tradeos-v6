"""
TradeOS v7 — Trade Decision
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

    # ── The price at which this trade becomes worth taking ──────────────────
    # rr_live answers "is this good HERE", which on most days is "no" and ends
    # the conversation. It does not answer the question that follows, which is
    # the one worth money: "at what price WOULD it be good?"
    #
    # On 2026-07-24, 27 of 43 candidates sat inside their entry zone and 32 were
    # skipped — almost all for rr < 1. The zone spans ~3% while the stop sits
    # ~5% below it, so entering near the zone HIGH collapses R:R even though the
    # identical plan at the zone LOW is perfectly sound. Reporting only rr_live
    # discards every one of those setups instead of turning it into a limit
    # order.
    #
    # max_entry is the exact solution of rr >= min_rr:
    #     (tgt - px) / (px - stop) >= min_rr
    #  => px <= (tgt + min_rr * stop) / (1 + min_rr)
    max_entry:     float | None = None   # buy at or below this for min_rr
    rr_at_zone_low: float | None = None  # R:R if filled at the zone low
    min_rr_used:   float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def max_entry_for_rr(stop: float, target: float, min_rr: float) -> float | None:
    """
    Highest entry price at which reward:risk still meets min_rr.

    Pure arithmetic, no opinions — the caller decides what to do with it. Returns
    None when the levels cannot produce a valid answer (target at or below stop).
    """
    if not stop or not target or target <= stop or min_rr <= 0:
        return None
    return (target + min_rr * stop) / (1.0 + min_rr)


def regime_min_rr(regime: str | None) -> float:
    """
    The reward:risk floor for THIS regime, not a flat one regardless of tape.

    generate_signals.py already computes min_rr_to_enter_<REGIME> for the
    evening pipeline's own gate (NEUTRAL 1.0, TRENDING 0.9, RISK_ON 1.1,
    RECOVERING 1.3, RISK_OFF 1.5 by default) — decide()'s live callers never
    read it and passed nothing, so the daemon entered every regime at the
    same flat 1.0R bar regardless of what the tape was doing. F-43,
    20-Aug-2026: eight straight NEUTRAL sessions with Nifty red every one of
    them and breadth fading 59.6% -> 57.8% is exactly the case a rising bar
    exists for, and the live entry path could not raise it.

    Same key convention as swing/brain/dynamic_registry.py's own regime->key
    builder: space replaced with underscore. The swing regime is written
    space-separated ("RISK ON") — see docs/TERMINOLOGY.md.
    """
    from config import cfg_float
    key = str(regime or "NEUTRAL").strip().replace(" ", "_") or "NEUTRAL"
    base = cfg_float("min_rr_to_enter", 1.0)
    return cfg_float(f"min_rr_to_enter_{key}", base)


def decide(
    row: dict,
    live_price: float | None,
    *,
    total_capital: float | None = None,
    open_positions: list[dict] | None = None,
    regime: str = "NEUTRAL",
    min_rr: float = 1.0,
    max_chase_pct: float | None = None,
    available_cash: float | None = None,
    vol_mult: float = 1.0,
) -> Decision:
    """
    row: a signal_output_daily / master_shortlist row carrying planned_stop,
         planned_target, entry_zone_low/high and current_price.
    live_price: real-time price. None falls back to row['current_price'] and
         the result is flagged stale_price so the caller can say so rather
         than presenting yesterday's number as today's.
    vol_mult: analysis.overlays.vol_exposure()'s book-level multiplier,
         1.0 (no scaling) unless the caller passes the cycle's cached reading
         — see check_new_entry's docstring for why this is not fetched here.
    """
    sym   = row.get("symbol") or "?"
    stop  = _f(row.get("planned_stop"))
    tgt   = _f(row.get("planned_target"))
    zlo   = _f(row.get("entry_zone_low"))
    zhi   = _f(row.get("entry_zone_high")) or (zlo * 1.02 if zlo else None)

    stale = live_price is None
    px    = live_price if live_price else _f(row.get("current_price"))

    if not px or not stop or not tgt:
        # A plan can be absent for two very different reasons, and collapsing
        # them into "no data" hides a real decision behind a data-quality
        # message. compute_trade_levels records WHY it declined in
        # planned_stop_source — e.g. risk_too_wide_8.2pct means the ATR-based
        # stop exceeded the risk cap, which is the model working, not failing.
        src = (row.get("planned_stop_source") or "").strip()
        if src and src not in ("atr", "structure", "atr_capped"):
            pretty = src.replace("_", " ")
            if src.startswith("risk_too_wide"):
                pct = src.rsplit("_", 1)[-1].replace("pct", "")
                pretty = (f"stop would sit {pct}% below entry, past the risk cap — "
                          f"too volatile to size sensibly")
            return Decision(sym, ACT_SKIP,
                            f"{sym}: SKIP — {pretty}",
                            pretty, px, zlo, stop, tgt,
                            None, None, None, None, None, None, None,
                            stale_price=stale)

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
        # available_cash matters as much as total_capital and was never passed.
        # TOTAL_CAPITAL is the size of the STRATEGY; cash is what can actually
        # be spent this minute. On 2026-07-27 those were Rs 20,000 and Rs 5,200
        # — sizing on the former proposes positions the broker will reject.
        # Holdings are capital, but they are not purchasing power.
        v = check_new_entry(
            sym, row.get("sector") or "", row.get("industry") or "",
            px, risk, open_positions or [],
            regime=regime, total_capital=total_capital,
            available_cash=available_cash, vol_mult=vol_mult)
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

    # The actionable levels, computed once for every branch below.
    max_entry = max_entry_for_rr(stop, tgt, min_rr)
    rr_zlo = ((tgt - zlo) / (zlo - stop)) if (zlo and zlo > stop) else None

    def _mk(action, headline, reason):
        return Decision(sym, action, headline, reason, px, zlo, stop, tgt,
                        round(rr, 2), round(risk_pct, 2), round(up_pct, 2),
                        round(dist_zone, 2) if dist_zone is not None else None,
                        qty, invested, risk_amt, stale_price=stale,
                        max_entry=round(max_entry, 2) if max_entry else None,
                        rr_at_zone_low=round(rr_zlo, 2) if rr_zlo else None,
                        min_rr_used=min_rr)

    size_note = f" · {qty} sh ≈ ₹{invested:,.0f}, risk ₹{risk_amt:,.0f}" if qty else ""

    if rr < min_rr:
        # Not "no", but "not here". A setup whose R:R fails at the current price
        # usually still works a little lower, and the exact price is knowable —
        # so quote it and let a resting limit order do the waiting.
        if max_entry and max_entry < px:
            gap = (px - max_entry) / px * 100
            return _mk(ACT_WAIT,
                       f"{sym}: WAIT — R:R {rr:.2f} here; buy at or below "
                       f"{max_entry:.2f} ({gap:.1f}% lower) for {min_rr:g}R:R",
                       f"reward {up_pct:.1f}% vs risk {risk_pct:.1f}% does not justify entry at "
                       f"{px:.2f}. Rest a limit at {max_entry:.2f}; stop {stop:.2f}, target {tgt:.2f}")
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

    # KITE IS PRIMARY. yfinance is only ever a fallback — it is ~15 minutes
    # delayed, which is tolerable for a zone-proximity nudge and NOT tolerable
    # for a stop decision. The moment Kite quotes start working this path takes
    # over automatically with no config change.
    try:
        from kite.kite_client import fetch_ltp, is_available
        if is_available():
            px = fetch_ltp(symbols)
            live = {k: v for k, v in px.items() if v}
            if live:
                return live, "kite"
            logger.warning(
                "  Kite session is valid but returned no quotes. If this says "
                "'Insufficient permission', the Kite Connect app tied to "
                "KITE_API_KEY does not have an active market-data subscription "
                "— billing is PER APP at developers.kite.trade, so confirm the "
                "subscription is on this exact api_key. Falling back to yfinance."
            )
    except Exception as e:
        logger.warning(f"  Kite quote path failed ({e}) — falling back to yfinance")

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
