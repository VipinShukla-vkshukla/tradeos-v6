"""
The two genuinely new alpha engines — post-earnings drift, and accumulation.

    PEAD   results-day gap, delivery-confirmed, drift held rather than faded
    ACC    delivery-% persistence as the PRIMARY sort, structure as confirmation

WHY THESE TWO AND NOT A SIXTEENTH BREAKOUT VARIANT
---------------------------------------------------
The methodology review's finding F10 is that nine screening engines and seven
intraday engines all monetise one market state — trend persistence — and that
the resulting "diversification" is cosmetic. These two are the only additions in
the whole plan that carry a genuinely different burden of proof.

**PEAD (F5).** The system ingests results calendars, blocks entries around them,
and warns on held names through the event. Nowhere does it TRADE the event. That
is a retail reflex: treat scheduled information arrival as danger. Post-earnings
announcement drift is among the best-documented anomalies in Indian equities,
and every input it needs already flows — results dates from
nifty_upcoming_events, gap and delivery and volume from stock_data_daily. Its
edge is structural rather than statistical: large buyers cannot compress a week
of accumulation into a single session regardless of how good their model is, so
the drift exists because of market mechanics rather than because of a pattern.

**ACC (F7).** The current screeners sort on structure and use delivery as one
indicator among 86. This inverts the burden of proof: sort on sustained
delivery elevation and block-deal confirmation, and require structure only to
confirm. Same universe, opposite question. The point is not that delivery is a
better indicator — it is that the crowded thing is the breakout level, and
accumulation evidence is what the crowd cannot see.

BOTH SHADOW FIRST. NON-NEGOTIABLE.
-----------------------------------
Neither receives capital until it has shadowed at detection level for a full
quarter and shown independent expectancy. They are registered SHADOW and the
existing screen_stocks lifecycle handling keeps their scores out of the
composite while still recording them to screener_performance — which is the
mechanism that makes "integrate a new strategy" safe, and it already works.

A new engine that goes live on its author's confidence is the exact failure the
whole governance stage exists to prevent, and these two are mine.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from config import cfg_float, cfg_int, get_supabase, today_ist, fetch_all


def _results_dates(sb, lookback_days: int = 30) -> dict[str, str]:
    """
    Most recent results date per symbol, from the calendar already ingested.

    nifty_upcoming_events holds `purpose` as free text from NSE — 'Financial
    Results', 'Quarterly Results', 'Audited Results' and several variants — so
    the match is a substring rather than an equality. An exact match here would
    silently find nothing, which is the failure mode this project keeps
    rediscovering.
    """
    since = (today_ist() - timedelta(days=lookback_days)).isoformat()
    out: dict[str, str] = {}

    # results_calendar FIRST, nifty_upcoming_events only as a fallback.
    #
    # The upcoming table is exactly what its name says and drops dates once they
    # pass — measured 04-Aug-2026 it held one day of past and two weeks of
    # future. PEAD asks "what reported N days ago", which that table structurally
    # cannot answer, and against it the engine returned 0 from 165 recent
    # results. Migration 039 gives the calendar a memory.
    try:
        rows = (sb.table("results_calendar")
                  .select("symbol,purpose,event_date")
                  .gte("event_date", since)
                  .lte("event_date", today_ist().isoformat())
                  .limit(5000).execute().data) or []
    except Exception:
        rows = []
        logger.warning("  PEAD: results_calendar missing — falling back to the "
                       "forward-only feed, which cannot see past results. Apply "
                       "migration 039.")
    if not rows:
        try:
            rows = (sb.table("nifty_upcoming_events")
                      .select("symbol,purpose,event_date")
                      .gte("event_date", since)
                      .lte("event_date", today_ist().isoformat())
                      .limit(2000).execute().data) or []
        except Exception as e:
            logger.warning(f"  PEAD: results calendar unavailable ({e}) — engine produces nothing")
            return {}
    for r in rows:
        if "result" not in str(r.get("purpose") or "").lower():
            continue
        sym, d = str(r.get("symbol") or "").upper(), str(r.get("event_date"))[:10]
        if sym and (sym not in out or d > out[sym]):
            out[sym] = d
    return out


def capture_results_calendar(sb=None) -> int:
    """
    Copy today's forward calendar into the persistent one.

    Called nightly. The forward feed knows about results two weeks out, so
    capturing it means the future becomes the past on its own as dates arrive —
    no waiting a quarter for history to accumulate.

    Upserted on (symbol, event_date): a re-run cannot duplicate a results day,
    and a date that shifts is corrected rather than doubled.
    """
    sb = sb or get_supabase()
    try:
        rows = (sb.table("nifty_upcoming_events")
                  .select("symbol,purpose,event_date,company_name")
                  .limit(5000).execute().data) or []
    except Exception as e:
        logger.warning(f"  results_calendar: source unavailable ({e})")
        return 0
    seen, payload = set(), []
    for r in rows:
        if "result" not in str(r.get("purpose") or "").lower():
            continue
        sym, d = str(r.get("symbol") or "").upper().strip(), str(r.get("event_date") or "")[:10]
        if not sym or not d or (sym, d) in seen:
            continue
        seen.add((sym, d))
        payload.append({"symbol": sym, "event_date": d,
                        "purpose": r.get("purpose"), "company_name": r.get("company_name")})
    if not payload:
        return 0
    try:
        sb.table("results_calendar").upsert(payload, on_conflict="symbol,event_date").execute()
        logger.info(f"  results_calendar: {len(payload)} results day(s) recorded")
        return len(payload)
    except Exception as e:
        logger.warning(f"  results_calendar write skipped ({e}) — apply migration 039")
        return 0


def run_pead(stock_map: dict, sector_rank: dict, sb=None) -> dict:
    """
    Post-earnings drift: the market under-reacts to results, so ride the
    under-reaction rather than fearing the announcement.

    THE THESIS, AND WHY IT IS NOT A PATTERN.
    An institution that decides on results day to build a position cannot buy
    its full size that day without moving the price against itself. So it buys
    over days or weeks. The drift is the visible residue of that constraint, and
    it persists because it is a mechanical consequence of size rather than an
    inefficiency someone can arbitrage away by noticing it.

    WHAT SEPARATES DRIFT FROM A DEAD-CAT BOUNCE IS DELIVERY.
    A results gap on low delivery is speculative repositioning that unwinds
    within days. A results gap on ELEVATED delivery means shares actually
    changed hands into custody — someone paid up and kept it. Delivery is the
    confirmation leg and it is not optional; without it this engine is just
    "buy things that gapped up", which is the crowded trade it is meant to avoid.
    """
    sb = sb or get_supabase()
    results = {}

    window   = cfg_int("pead_window_days", 5)          # sessions since results
    min_gap  = cfg_float("pead_min_gap_pct", 2.0)      # the reaction must be real
    max_gap  = cfg_float("pead_max_gap_pct", 12.0)     # above this it is a repricing, not a drift
    min_deliv = cfg_float("pead_min_delivery_pct", 45.0)
    min_vol_r = cfg_float("pead_min_vol_ratio", 1.5)

    events = _results_dates(sb, lookback_days=window * 3)
    if not events:
        return {}

    # THE GAP MUST BE MEASURED ON THE RESULTS DAY, NOT TODAY.
    #
    # The first version read pct_change straight off today's row, which is the
    # move that happened today — days after the announcement. It produced zero
    # candidates against 165 recent results, and zero is exactly what it would
    # keep producing: it was asking "did this stock move 2-12% today AND have
    # results this week", which is a coincidence rather than a thesis.
    #
    # The drift thesis needs the REACTION (the results-day bar) and then the
    # continuation after it. So the reaction is fetched from history at the
    # results date, and today's row supplies only the current state.
    since = (today_ist() - timedelta(days=window * 4)).isoformat()
    reaction: dict[tuple[str, str], float] = {}
    syms = [x for x in stock_map if x.upper() in events]
    try:
        # SORTED PAGING. The chunk size is PostgREST's IN-list limit and has
        # nothing to do with the 1000-row cap: 60 symbols across a window*4-day
        # window is several thousand rows, so this pages — and it paged with no
        # ORDER BY, which can repeat rows and skip others at a stable total.
        # A skipped row here is a results-day bar that silently does not exist,
        # and PEAD then scores that name with no reaction at all.
        for i in range(0, len(syms), 60):
            chunk = syms[i:i + 60]
            for r in fetch_all(lambda c=chunk: sb.table("stock_data_daily")
                               .select("symbol,date,pct_change,delivery_pct")
                               .in_("symbol", c).gte("date", since),
                               order_by="symbol,date"):
                reaction[(r["symbol"], str(r["date"])[:10])] = (
                    float(r.get("pct_change") or 0), float(r.get("delivery_pct") or 0))
    except Exception as e:
        logger.warning(f"  PEAD: results-day bars unavailable ({e}) — engine produces nothing")
        return {}

    today = today_ist()
    for sym, s in stock_map.items():
        rdate = events.get(sym.upper())
        if not rdate:
            continue
        try:
            days_since = (today - today.__class__.fromisoformat(rdate)).days
        except Exception:
            continue
        # Day 0 is excluded deliberately. The results-day bar itself is the
        # reaction, not the drift, and entering into it is buying the gap rather
        # than the under-reaction that follows it.
        if not (1 <= days_since <= window):
            continue

        # The reaction, from the results-day bar itself.
        react = reaction.get((sym, rdate))
        if not react:
            continue
        gap, react_delivery = react
        # Delivery on the RESULTS day is the confirmation that someone took
        # stock into custody on the news. Today's delivery is a different
        # question and is not what confirms a drift.
        delivery = react_delivery
        vol_r    = float(s.get("vol_ratio") or 0)
        above_50 = bool(s.get("above_sma50"))
        rsi_d    = float(s.get("rsi_daily") or 0)

        if not (min_gap <= gap <= max_gap):   continue
        if delivery < min_deliv:              continue   # the confirmation leg
        if vol_r < min_vol_r:                 continue
        if not above_50:                      continue
        # Above ~80 the drift has already been taken and what remains is the
        # crowded part of it.
        if rsi_d >= cfg_float("pead_max_rsi", 80.0):
            continue

        score = 40.0
        score += min(gap * 3.0, 20.0)                       # a bigger surprise drifts further
        score += min((delivery - min_deliv) * 0.6, 18.0)    # conviction of the buyer
        score += min((vol_r - min_vol_r) * 6.0, 12.0)       # participation
        # Freshness: drift decays. Day 1 is worth more than day 5.
        score += max(0.0, (window - days_since + 1) * 2.0)
        if sector_rank.get(s.get("sector", ""), 99) <= 8:
            score += 6.0

        results[sym] = round(min(score, 100.0), 1)

    logger.info(f"  PEAD: {len(results)} candidate(s) from {len(events)} recent results")
    return results


def run_acc(stock_map: dict, sector_rank: dict, sb=None) -> dict:
    """
    Accumulation-confirmed: delivery persistence FIRST, structure only to confirm.

    THE INVERSION IS THE POINT.

    Every existing screener asks "is the structure good?" and then checks whether
    delivery agrees. This asks "has someone been quietly taking stock?" and then
    checks whether the structure permits an entry. Same universe, opposite
    burden of proof — and it matters because the structure is the crowded half.
    A visible breakout level in an Indian small cap is a retail stop pool;
    sustained delivery elevation is not visible to anyone who is not looking for
    it.

    PERSISTENCE, NOT A SINGLE DAY. One high-delivery session is a block trade or
    a bulk deal — an event, not accumulation. The signal is delivery holding
    above its own baseline across many sessions, which is what a buyer working
    an order over weeks actually leaves behind.

    BLOCK AND BULK DEALS ARE CORROBORATION, NOT THE TRIGGER. They are lumpy,
    they are public the same evening, and a single large print is as likely to be
    a stake sale as an accumulation. They raise the score; they never create the
    candidate.
    """
    sb = sb or get_supabase()
    results = {}

    lookback  = cfg_int("acc_lookback_sessions", 20)
    min_days  = cfg_int("acc_min_elevated_days", 8)     # how many must be elevated
    elevated  = cfg_float("acc_elevated_delivery_pct", 55.0)
    max_run   = cfg_float("acc_max_pct_from_low", 25.0) # already-run names are not accumulation

    symbols = list(stock_map.keys())
    hist: dict[str, list[float]] = {}
    since = (today_ist() - timedelta(days=int(lookback * 1.8))).isoformat()
    try:
        for i in range(0, len(symbols), 60):
            off = 0
            while True:
                rows = (sb.table("stock_data_daily")
                          .select("symbol,date,delivery_pct")
                          .in_("symbol", symbols[i:i + 60])
                          .gte("date", since)
                          .order("date").range(off, off + 999).execute().data) or []
                for r in rows:
                    if r.get("delivery_pct") is not None:
                        hist.setdefault(r["symbol"], []).append(float(r["delivery_pct"]))
                if len(rows) < 1000:
                    break
                off += 1000
    except Exception as e:
        logger.warning(f"  ACC: delivery history unavailable ({e}) — engine produces nothing")
        return {}

    deals = _recent_net_buying(sb, lookback)

    for sym, s in stock_map.items():
        d = hist.get(sym, [])[-lookback:]
        if len(d) < min_days:
            continue

        n_elev = sum(1 for x in d if x >= elevated)
        if n_elev < min_days:
            continue

        # Elevated against ITS OWN baseline, not against a fixed number. Some
        # names simply trade at high delivery; that is a property of the
        # shareholder register, not evidence that anything changed.
        baseline = sorted(d)[len(d) // 2]
        recent   = sum(d[-5:]) / min(len(d), 5)
        if recent <= baseline:
            continue

        # A name already extended is not being accumulated — it is being
        # distributed into. Computed from low_52w rather than read from a
        # column: there is no pct_from_52w_low on stock_data_daily, and a
        # `.get()` on a name that does not exist would have returned 0 and
        # silently disabled this filter forever.
        low52  = float(s.get("low_52w") or 0)
        close  = float(s.get("close") or 0)
        if low52 > 0 and close > 0:
            if (close - low52) / low52 * 100.0 > max_run * 4:
                continue

        above_50 = bool(s.get("above_sma50"))
        rsi_d    = float(s.get("rsi_daily") or 0)
        # STRUCTURE CONFIRMS, IT DOES NOT SELECT. The only requirement is that
        # the name is not actively breaking down — a falling knife with high
        # delivery is a seller finding buyers, not a buyer finding stock.
        if not above_50 and rsi_d < 45:
            continue

        score = 35.0
        score += min((n_elev / len(d)) * 40.0, 30.0)          # persistence, the primary sort
        score += min((recent - baseline) * 1.2, 15.0)         # and it is rising
        net_cr = deals.get(sym.upper(), 0.0)
        if net_cr > 0:
            score += min(net_cr * 0.5, 12.0)                  # corroboration only
        if sector_rank.get(s.get("sector", ""), 99) <= 10:
            score += 5.0

        results[sym] = round(min(score, 100.0), 1)

    logger.info(f"  ACC: {len(results)} candidate(s) on delivery persistence "
                f"({len(deals)} name(s) with net block/bulk buying)")
    return results


def _recent_net_buying(sb, lookback_sessions: int) -> dict[str, float]:
    """
    Net block/bulk deal value per symbol, in crore, over the window.

    Reads bulk_deals — the structured table migration 038 added. Returns an
    empty dict when that table is absent or empty, which is the correct
    degradation: the engine loses its corroboration leg and keeps its primary
    sort, exactly as the readiness review's M3 requires, and says so rather than
    absorbing it silently.
    """
    since = (today_ist() - timedelta(days=int(lookback_sessions * 1.6))).isoformat()
    out: dict[str, float] = {}
    try:
        rows = (sb.table("bulk_deals")
                  .select("symbol,side,value_cr,deal_date")
                  .gte("deal_date", since).limit(5000).execute().data) or []
    except Exception:
        logger.warning("  ACC: bulk_deals unavailable — running on delivery persistence "
                       "ALONE. This is a degraded mode, not the designed one; apply "
                       "migration 038 and enable bulk_deal_capture_enabled.")
        return {}
    if not rows:
        logger.info("  ACC: no block/bulk deals in the window — corroboration leg "
                    "contributes nothing today (expected until capture has run a while)")
    for r in rows:
        v = float(r.get("value_cr") or 0)
        if not v:
            continue
        sym = str(r.get("symbol") or "").upper()
        out[sym] = out.get(sym, 0.0) + (v if str(r.get("side")).upper() == "BUY" else -v)
    return {k: v for k, v in out.items() if v > 0}
