"""
The intraday universe — which stocks are worth streaming today.

THE HOLE THIS FILLS
-------------------
Until now the intraday engines only saw symbols the SWING pipeline shortlisted:
seven names picked for a one-to-three-week thesis. That is the wrong universe.
A stock worth owning for a fortnight and a stock worth trading for four hours
share almost no selection criteria — the swing screen rewards structure,
relative strength over months and fundamental quality, none of which predicts
whether a name will move 0.8% with enough liquidity to get in and out today.

So intraday picks its own universe, from its own criteria, and the two lists
overlap only by coincidence.

SELECTION, IN ORDER OF WHAT DISQUALIFIES FASTEST
------------------------------------------------
  tradability   price floor and F&O-ban / ASM exclusion. A stock you cannot
                exit cleanly is not a candidate at any price.
  liquidity     20-day traded VALUE, not share volume. A ₹40 stock trading a
                million shares is ₹4 crore of turnover and will move on your
                own order; a ₹4,000 stock trading 50,000 shares is ₹20 crore
                and will not. Value is the honest measure.
  movement      daily ATR% must clear the cost floor. This is the filter that
                the cost model makes non-negotiable: a stock that typically
                moves 0.4% in a whole session cannot produce the ~0.7% target
                that survives a 0.21% round trip. Screening it in wastes a
                websocket slot on a name that can never qualify.
  quality       delivery percentage as a crude operator filter — a name where
                almost nothing is delivered is being churned, and churn produces
                the false breaks every engine here is designed to avoid.

WHY THE LIST IS CAPPED
----------------------
Not ambition — websocket and API budget. Every symbol costs a subscription slot
and, more expensively, one historical_data call per context refresh. Forty names
is comfortable; five hundred would exhaust the rate limit on the first refresh
and leave every engine reasoning about stale bars.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_float, cfg_int, cfg_bool, today_ist


# Module-level, single-process cache — this daemon runs as one long-lived
# process, and the underlying data (a fixed trading date's stock_data_daily
# rows) does not change between one 300s slow-tick call and the next. See
# symbols() and the warning dedup in _latest_date() for what each key guards.
_cache: dict = {"date": None, "symbols": None, "warned_for": None}


@dataclass
class UniverseEntry:
    symbol: str
    close: float
    value_cr: float
    atr_pct: float
    delivery_pct: float | None
    sector: str
    score: float
    reason: str


def _latest_date(sb) -> str | None:
    """
    The most recent date whose rows can actually answer a liquidity question.

    NOT simply max(date). `value_cr`, `delivery_pct` and `delivery_qty` are the
    only three columns on this table that come from the bhavcopy rather than
    from computation, and the bhavcopy for a session is not published until
    after that session closes. So a pipeline run made DURING market hours —
    or any run given today's date before the file exists — writes a row with
    all 83 computed indicators present and those three null.

    That row is not wrong, it is incomplete. But max(date) selects it, and the
    filter below reads a null traded value as ZERO traded value: on 05-Aug-2026
    that rejected 472 of 499 names as illiquid, returned an empty universe, and
    took the intraday book to zero setups for the whole session while every
    health check stayed green. Missing data and a failed test are different
    facts and must not produce the same answer.

    So the date is chosen by whether the liquidity column is populated on it,
    and a fallback is stated rather than silent.
    """
    rows = (sb.table("stock_data_daily").select("date")
              .order("date", desc=True).limit(1).execute().data or [])
    newest = rows[0]["date"] if rows else None
    if not newest:
        return None

    # Does the newest date actually carry traded value? One probe, not a scan:
    # the column is written for every symbol or for none, because it comes from
    # a single bhavcopy join.
    probe = (sb.table("stock_data_daily").select("date")
               .eq("date", newest).not_.is_("value_cr", "null")
               .limit(1).execute().data or [])
    if probe:
        return newest

    older = (sb.table("stock_data_daily").select("date")
               .lt("date", newest).not_.is_("value_cr", "null")
               .order("date", desc=True).limit(1).execute().data or [])
    if not older:
        logger.error(
            f"  scanner: {newest} has no traded-value data and neither does any "
            f"earlier date — the universe cannot be built. Check ingest_bhavcopy.")
        return None

    # ONCE per newest-date, not once per 300s call. This function is the cheap
    # check symbols() runs every slow tick specifically so it does not have to
    # repeat the expensive scan — but a warning that repeats unchanged every
    # 5 minutes for a whole session reads as noise, or worse, as "still
    # broken", when it is the same known, already-explained fact restated.
    if _cache.get("warned_for") != newest:
        logger.warning(
            f"  scanner: {newest} carries no traded value (bhavcopy not in yet — "
            f"a pipeline run dated today before the file publishes leaves value_cr "
            f"null). Building the universe from {older[0]['date']} instead, which "
            f"has it. Liquidity is a prior-session fact, so this is the correct "
            f"input, not a degraded one.")
        _cache["warned_for"] = newest
    return older[0]["date"]


def build_universe(sb=None, limit: int | None = None) -> list[UniverseEntry]:
    """
    Today's tradeable intraday universe, ranked.

    Reads the same stock_data_daily the swing pipeline maintains — a separate
    ingest for intraday would be a second source of truth for prices that
    already exist, and the two would eventually disagree.
    """
    sb = sb or get_supabase()
    limit = limit or cfg_int("intraday_max_universe", 40)

    d = _latest_date(sb)
    if not d:
        logger.warning("  scanner: no stock_data_daily rows")
        return []

    rows = (sb.table("stock_data_daily")
              .select("symbol,close,value_cr,atr_pct,delivery_pct,volume,"
                      "avg_vol_20d,sector,industry,asm_flag,fo_ban_flag,market_cap")
              .eq("date", d).execute().data or [])
    if not rows:
        return []

    min_price   = cfg_float("intraday_min_price", 50.0)
    min_value   = cfg_float("intraday_min_turnover_cr", 25.0)
    min_atr     = cfg_float("intraday_min_atr_pct", 1.20)
    max_atr     = cfg_float("intraday_max_atr_pct", 8.00)
    min_deliv   = cfg_float("intraday_min_delivery_pct", 20.0)
    skip_flagged = cfg_bool("intraday_skip_flagged", True)

    out: list[UniverseEntry] = []
    rejected = {"price": 0, "liquidity": 0, "movement": 0, "delivery": 0, "flagged": 0}

    for r in rows:
        sym = r.get("symbol")
        close = float(r.get("close") or 0)
        value = float(r.get("value_cr") or 0)
        atr   = float(r.get("atr_pct") or 0)
        deliv = r.get("delivery_pct")
        deliv = float(deliv) if deliv is not None else None

        if not sym or close < min_price:
            rejected["price"] += 1
            continue
        if skip_flagged and (r.get("asm_flag") or r.get("fo_ban_flag")):
            # ASM widens spreads and imposes margin penalties; an F&O ban
            # distorts the cash market for exactly the names it names.
            rejected["flagged"] += 1
            continue
        if value < min_value:
            rejected["liquidity"] += 1
            continue
        if atr < min_atr:
            # The cost-model filter. Below this the stock cannot produce a move
            # that survives a round trip, so streaming it is a wasted slot.
            rejected["movement"] += 1
            continue
        if atr > max_atr:
            rejected["movement"] += 1
            continue
        if deliv is not None and deliv < min_deliv:
            rejected["delivery"] += 1
            continue

        # Rank by movement AND liquidity together. Ranking on either alone
        # produces a bad list: pure ATR surfaces thin volatile names you cannot
        # exit, pure turnover surfaces mega-caps that never move enough to pay
        # for the trade.
        liq_score = min(1.0, value / max(min_value * 8, 1))
        mov_score = min(1.0, (atr - min_atr) / max(max_atr - min_atr, 0.01))
        score = round(0.55 * mov_score + 0.45 * liq_score, 4)

        out.append(UniverseEntry(
            symbol=sym, close=close, value_cr=round(value, 1),
            atr_pct=round(atr, 2), delivery_pct=deliv,
            sector=r.get("sector") or "", score=score,
            reason=f"ATR {atr:.2f}% · ₹{value:.0f}cr/day"
                   + (f" · {deliv:.0f}% delivery" if deliv is not None else ""),
        ))

    out.sort(key=lambda e: -e.score)
    kept = out[:limit]

    logger.info(
        f"  scanner: {len(kept)} of {len(rows)} symbols qualify for intraday "
        f"(rejected — price {rejected['price']}, liquidity {rejected['liquidity']}, "
        f"movement {rejected['movement']}, delivery {rejected['delivery']}, "
        f"flagged {rejected['flagged']})"
    )
    if not kept:
        logger.warning(
            "  scanner: NOTHING qualified. Check intraday_min_turnover_cr and "
            "intraday_min_atr_pct — an empty universe means the engines run on "
            "nothing while reporting success."
        )
    return kept


def persist(entries: list[UniverseEntry], sb=None) -> None:
    """Record today's universe so a later review can ask what was watchable."""
    if not entries:
        return
    sb = sb or get_supabase()
    try:
        sb.table("intraday_universe").upsert([{
            "trade_date": today_ist().isoformat(),
            "symbol": e.symbol, "close": e.close, "value_cr": e.value_cr,
            "atr_pct": e.atr_pct, "delivery_pct": e.delivery_pct,
            "sector": e.sector, "score": e.score, "reason": e.reason,
        } for e in entries], on_conflict="trade_date,symbol").execute()
    except Exception as ex:
        logger.debug(f"  scanner: universe persist skipped — {ex}")


def symbols(sb=None, limit: int | None = None) -> list[str]:
    """
    Cached on the resolved date, not on the wall clock.

    Called every 300s from the slow timer. Its inputs — stock_data_daily for
    whichever date _latest_date() resolves to — do not change between one
    call and the next: that date is either today's close (fixed once the
    session ends) or yesterday's (fixed all session, per _latest_date()'s own
    fallback). So a full 499-row scan plus a 40-row upsert into
    intraday_universe was running every 5 minutes, all day, to recompute a
    result that could not have changed — confirmed by watching the log:
    identical rejection counts on every cycle from open to now.

    Not wrong, since an idempotent upsert of the same rows is harmless — but
    real, avoidable load for zero benefit, and the kind of thing that reads as
    "is this actually doing anything" from the log. _latest_date() is two
    cheap queries; skip the expensive 499-row scan and the write whenever it
    says nothing has moved.

    The cache is intentionally invalidated only by the DATE changing (the
    bhavcopy landing mid-session, or a new trading day) — not by a timer of
    its own, because there is no cadence at which yesterday's close becomes
    a different set of numbers.
    """
    sb = sb or get_supabase()
    d = _latest_date(sb)
    if d and d == _cache["date"] and _cache["symbols"] is not None:
        return _cache["symbols"]

    u = build_universe(sb, limit)
    persist(u, sb)
    out = [e.symbol for e in u]
    _cache["date"], _cache["symbols"] = d, out
    return out
