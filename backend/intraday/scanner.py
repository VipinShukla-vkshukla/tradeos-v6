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

LIVE RE-RANK — 10-Aug-2026
---------------------------
Everything above this point is PRIOR-DAY data (stock_data_daily), computed
once and cached until the calendar date changes. That is correct for the
tradability/liquidity/movement/quality FILTER — those are slow-moving facts —
but it means the RANKING was frozen at yesterday's close too: a stock that
went quiet today kept its slot for the whole session, and a stock outside
yesterday's top 40 that started moving hard at 10am was never seen at all,
because nothing was subscribed to it.

`universe()` now returns a wider BENCH (intraday_universe_bench_size, default
120 — LTP/quote subscription only, no bars, so it costs websocket capacity
Kite prices at up to 3000 instruments, not the rate-limited historical_data
budget the 40-cap exists to protect) and `live_rerank()` re-scores that bench
every 15-second cycle against LIVE relative volume — ticks the socket is
already carrying, zero extra API calls. The 40 names that actually get bars
(context_symbols(), the expensive resource) are picked from whichever
re-ranked top-40 is current when the 300-second slow timer next runs, so the
ranking updates on the loop the operator asked for (15s) while the budget it
was built to protect stays on the cadence it was built to protect it on
(300s). A symbol with no live tick yet keeps its static score unchanged
rather than being penalised — "no live signal" and "measured dead today"
must not produce the same answer.
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
# universe() and the warning dedup in _latest_date() for what each key guards.
# "bench" holds the FULL ranked pool (see universe()); symbols() is a plain
# slice of it, so there is exactly one stock_data_daily scan per date, not
# one for the static top-N and a second for the live-rerank bench.
_cache: dict = {"date": None, "bench": None, "warned_for": None}


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
    # Prior-day average volume (shares), the baseline live_rerank() scales by
    # elapsed session time to ask "is TODAY'S volume unusual". 0.0 (not None)
    # when absent, so downstream arithmetic never has to special-case a NULL.
    avg_vol_20d: float = 0.0


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


def _qualifies(r: dict, *, min_price: float, min_value: float, min_atr: float,
               max_atr: float, min_deliv: float, skip_flagged: bool,
               require_movement: bool = True) -> tuple[bool, str | None]:
    """
    One `stock_data_daily` row's gate result: (passes, rejection_reason).
    `rejection_reason` is one of "price"/"flagged"/"liquidity"/"movement"/
    "delivery", or None when every check requested is cleared.

    Extracted from build_universe()'s own inline loop, 23-Aug-2026 (Stage
    D2, docs/TRADEOS_ROADMAP.md Track D) — SO THE TWO CAN NEVER DISAGREE
    about what "otherwise tradeable" means. `require_movement=False` skips
    the ATR floor/ceiling specifically: movement_rejected_candidates()
    needs the population that fails ONLY on movement, and a second,
    independently-maintained copy of these same checks is exactly the
    shape of drift this project has been bitten by before (the hurdle/
    edge units mismatch, the sub_engine overwrite) — one definition, both
    callers read it.
    """
    sym = r.get("symbol")
    close = float(r.get("close") or 0)
    value = float(r.get("value_cr") or 0)
    atr   = float(r.get("atr_pct") or 0)
    deliv = r.get("delivery_pct")
    deliv = float(deliv) if deliv is not None else None

    if not sym or close < min_price:
        return False, "price"
    if skip_flagged and (r.get("asm_flag") or r.get("fo_ban_flag")):
        # ASM widens spreads and imposes margin penalties; an F&O ban
        # distorts the cash market for exactly the names it names.
        return False, "flagged"
    if value < min_value:
        return False, "liquidity"
    if require_movement:
        if atr < min_atr:
            # The cost-model filter. Below this the stock cannot produce a
            # move that survives a round trip, so streaming it is a wasted
            # slot.
            return False, "movement"
        if atr > max_atr:
            return False, "movement"
    if deliv is not None and deliv < min_deliv:
        return False, "delivery"
    return True, None


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
        avg_vol = float(r.get("avg_vol_20d") or 0)

        passes, reason = _qualifies(r, min_price=min_price, min_value=min_value,
                                    min_atr=min_atr, max_atr=max_atr,
                                    min_deliv=min_deliv, skip_flagged=skip_flagged)
        if not passes:
            rejected[reason] += 1
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
            avg_vol_20d=avg_vol,
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


def universe(sb=None) -> list[UniverseEntry]:
    """
    Today's full qualifying pool, ranked by static score — the BENCH
    live_rerank() promotes from, wider than the intraday_max_universe cap.

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
    if d and d == _cache["date"] and _cache["bench"] is not None:
        return _cache["bench"]

    bench_size = max(cfg_int("intraday_universe_bench_size", 120),
                     cfg_int("intraday_max_universe", 40))
    entries = build_universe(sb, bench_size)
    _cache["date"], _cache["bench"] = d, entries

    # The persisted daily snapshot stays exactly what it always was — the
    # static top-N, not the wider bench — so intraday_universe's schema and
    # every existing reader of it (review tooling, discover_engines) see no
    # change at all.
    top = entries[:cfg_int("intraday_max_universe", 40)]
    persist(top, sb)
    return entries


def symbols(sb=None, limit: int | None = None) -> list[str]:
    """The static top-`limit` symbols by score — a slice of universe()."""
    limit = limit or cfg_int("intraday_max_universe", 40)
    return [e.symbol for e in universe(sb)[:limit]]


def live_rerank(bench: list[UniverseEntry], quotes: dict[str, dict],
                minutes: float, limit: int) -> list[str]:
    """
    Re-rank the bench by live relative volume, blended with each entry's
    static score, and return the top `limit` symbols. PURE — no I/O, so this
    is cheap enough to run every 15-second cycle, which is the point: the
    static score alone is a full trading day stale by the time it matters.

    `quotes` is symbol -> PriceFeed.quote() (or None/{} for a name with no
    live tick yet — quote mode off, or the name simply has not ticked). A
    missing or zero live volume leaves that entry's score UNCHANGED rather
    than penalising it to the bottom: "no live signal yet" and "measured
    dead today" are different facts, the same distinction the allocator's
    cold-start floor already draws for the same reason.

    Before the open (minutes <= 0) or with the switch off, this is a no-op —
    the static ranking stands, exactly as it always has.
    """
    if not bench:
        return []
    if minutes <= 0 or not cfg_bool("intraday_universe_live_rerank", True):
        return [e.symbol for e in bench[:limit]]

    live_weight = cfg_float("intraday_rerank_live_weight", 0.4)
    rvol_cap = cfg_float("intraday_rvol_cap", 3.0)
    session_minutes = 375.0  # 09:15 -> 15:30 IST

    scored: list[tuple[float, str]] = []
    for e in bench:
        blended = e.score
        q = quotes.get(e.symbol) if quotes else None
        vol = q.get("volume") if q else None
        if vol and e.avg_vol_20d > 0:
            expected = e.avg_vol_20d * (min(minutes, session_minutes) / session_minutes)
            if expected > 0:
                rvol = vol / expected
                live_score = min(1.0, rvol / rvol_cap) if rvol_cap > 0 else 0.0
                blended = (1 - live_weight) * e.score + live_weight * live_score
        scored.append((blended, e.symbol))

    scored.sort(key=lambda t: -t[0])
    return [sym for _, sym in scored[:limit]]


# ── STAGE D2 — LIVE UNIVERSE RE-QUALIFICATION — 23-Aug-2026 ──────────────────
#
# docs/TRADEOS_ROADMAP.md, Track D. The gap live_rerank() above cannot close:
# it only reorders names ALREADY in today's bench, and the bench is built
# once a day from YESTERDAY's turnover/ATR. A stock too quiet yesterday to
# qualify, then gapping hard today, is invisible all session no matter how
# large the bench cap is — the constraint was never the number, it was WHEN
# the eligibility list gets decided.
#
# CORRECTED, 23-Aug-2026 (the operator's own catch): stock_data_daily is
# NOT the full NSE EQ bhavcopy — it is swing's own sheet-baseline table,
# enriched (not populated) by ingest_bhavcopy.py with value_cr/delivery_pct
# for whichever symbols already have a row there. Confirmed live: 499 rows
# for a date where raw_prices (the actual bhavcopy ingest, used only to
# feed delivery% back into stock_data_daily's EXISTING rows, not to create
# new ones) carries 2,633. Two candidate populations follow from this:
#
#   movement_rejected_candidates() below — names ALREADY in stock_data_
#   daily that failed only yesterday's ATR band. Real historical ATR
#   exists for these; the check is relative (today's move vs. that ATR
#   floor), same as before this correction.
#
#   unreferenced_candidates() further down — names NOT in stock_data_daily
#   at all. No historical ATR exists to be relative to, so these need an
#   ABSOLUTE threshold instead. Sourced from nifty_total_market (751 rows,
#   751-499=253 of them not in stock_data_daily — a real, confirmed gap,
#   not a guess) and, for names not even THERE, Kite's own instrument
#   master — the one source that knows about a listing from its first
#   tradeable day, since nifty_total_market is index-membership and lags
#   an actual listing by however long NSE takes to reconstitute an index
#   (weeks to months), not days.

def movement_rejected_candidates(sb=None, exclude: set[str] | None = None
                                 ) -> list[UniverseEntry]:
    """
    EQ names that qualify on price/liquidity/delivery but missed ONLY
    yesterday's ATR floor or ceiling — the exact population a live
    re-check should re-test against TODAY's own numbers instead of
    yesterday's. Everything that failed for a DIFFERENT reason (too
    illiquid, too cheap, ASM/F&O-flagged, poor delivery) stays excluded
    here too: a live gap does not fix a stock nobody could exit cleanly.

    `exclude` is normally the caller's current bench — no reason to
    re-fetch or re-check a name already being watched.
    """
    sb = sb or get_supabase()
    exclude = exclude or set()
    d = _latest_date(sb)
    if not d:
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
    for r in rows:
        sym = r.get("symbol")
        if not sym or sym in exclude:
            continue
        passes_ex_movement, _ = _qualifies(
            r, min_price=min_price, min_value=min_value, min_atr=min_atr,
            max_atr=max_atr, min_deliv=min_deliv, skip_flagged=skip_flagged,
            require_movement=False)
        if not passes_ex_movement:
            continue
        atr = float(r.get("atr_pct") or 0)
        if min_atr <= atr <= max_atr:
            continue  # would already be in the daily bench — not our population
        out.append(UniverseEntry(
            symbol=sym, close=float(r.get("close") or 0),
            value_cr=round(float(r.get("value_cr") or 0), 1),
            atr_pct=round(atr, 2),  # yesterday's REAL, sub-floor ATR, carried honestly
            delivery_pct=(float(r["delivery_pct"]) if r.get("delivery_pct") is not None else None),
            sector=r.get("sector") or "", score=0.0,
            reason=f"yesterday ATR {atr:.2f}% missed the {min_atr:.2f}-{max_atr:.2f}% band",
            avg_vol_20d=float(r.get("avg_vol_20d") or 0),
        ))
    return out


def live_requalify(candidates: list[UniverseEntry], quotes: dict[str, dict],
                   *, move_pct: float, turnover_cr: float,
                   min_price: float | None = None) -> list[UniverseEntry]:
    """
    PURE. Which of `candidates` (from movement_rejected_candidates() OR
    unreferenced_candidates()) has, by TODAY's own live numbers, moved and
    traded enough to be admitted mid-session.

    `quotes` is symbol -> kite_client.fetch_quotes()'s per-symbol dict
    (ltp, open, high, low, close [PREVIOUS close, per fetch_quotes()'s own
    docstring], volume, avg_price). A candidate with no quote — not
    fetched, or Kite returned nothing for it — is silently skipped:
    absence is not evidence either way, the same rule this module's
    live_rerank() already applies to a missing tick.

    `move_pct` is compared against the ABSOLUTE value of today's percent
    change from previous close — a stock down 9% is exactly as
    tradeable-for-movement as one up 9%, matching how build_universe()'s
    own ATR floor is a magnitude test, not a directional one.

    `min_price`, checked against today's live `ltp`: a movement_rejected_
    candidates() row already cleared _qualifies()'s own price gate on
    yesterday's close, so this is redundant (and harmless) for it — but
    an unreferenced_candidates() row was NEVER run through _qualifies() at
    all (no stock_data_daily row exists to read a price from), so without
    this check a low-priced name could clear the move/turnover floors on
    tick noise alone. This is the ONE _qualifies()-equivalent gate that
    can be applied here; delivery% and ASM/F&O-ban flag have no live-quote
    equivalent and stay unchecked for that population — named, not
    silently assumed covered.
    """
    out = []
    for c in candidates:
        q = quotes.get(c.symbol)
        if not q:
            continue
        prev_close = float(q.get("close") or 0)
        ltp = float(q.get("ltp") or 0)
        volume = float(q.get("volume") or 0)
        avg_price = float(q.get("avg_price") or 0)
        if prev_close <= 0 or ltp <= 0:
            continue
        if min_price is not None and ltp < min_price:
            continue
        today_move_pct = abs(ltp - prev_close) / prev_close * 100.0
        today_turnover_cr = (volume * avg_price) / 1e7
        if today_move_pct < move_pct or today_turnover_cr < turnover_cr:
            continue
        out.append(UniverseEntry(
            symbol=c.symbol, close=ltp, value_cr=round(today_turnover_cr, 1),
            atr_pct=c.atr_pct, delivery_pct=c.delivery_pct, sector=c.sector,
            # Scored by live_rerank() once merged into the bench — a fresh
            # entrant has no static score of its own to blend from.
            score=0.0,
            # Reuses the candidate's OWN reason as context rather than
            # assuming "yesterday ATR" — that framing is only true for
            # movement_rejected_candidates()' population. A candidate from
            # unreferenced_candidates() has no historical ATR to name, and
            # hardcoding that language for it would misdescribe why it was
            # a candidate at all.
            reason=(f"LIVE REQUALIFIED: {today_move_pct:.2f}% move, "
                   f"₹{today_turnover_cr:.0f}cr today ({c.reason})"),
            avg_vol_20d=c.avg_vol_20d,
        ))
    return out


# Module-level, once-per-day cache for the three reference reads Population
# B and the ASM/GSM/F&O-ban check need (stock_data_daily's known symbols,
# nifty_total_market's members, safety_lists' flagged names). None of these
# three change intraday — stock_data_daily and nifty_total_market are
# EOD/weekly writes, safety_lists is a once-daily NSE surveillance ingest —
# so re-querying them every 45s (500+ times across one session) was pure
# waste. Mirrors kite_client.py's own `_instr_cache` pattern exactly.
_ref_cache: dict = {"date": None, "known": None, "market_rows": None, "flagged": None}


def _daily_reference_reads(sb) -> tuple[set[str], list[dict], set[str]]:
    """Returns (known symbols in stock_data_daily, nifty_total_market rows,
    ASM/GSM/FO_BAN-flagged symbols), cached for the rest of today."""
    today = today_ist().isoformat()
    if _ref_cache["date"] == today and _ref_cache["known"] is not None:
        return _ref_cache["known"], _ref_cache["market_rows"], _ref_cache["flagged"]

    d = _latest_date(sb)
    known: set[str] = set()
    if d:
        sdd_rows = (sb.table("stock_data_daily").select("symbol")
                     .eq("date", d).execute().data or [])
        known = {r["symbol"] for r in sdd_rows if r.get("symbol")}

    try:
        market_rows = sb.table("nifty_total_market").select(
            "symbol,company_name,industry").execute().data or []
    except Exception as e:
        logger.debug(f"  scanner: nifty_total_market read skipped — {e}")
        market_rows = []

    flagged: set[str] = set()
    if cfg_bool("intraday_skip_flagged", True):
        try:
            sl_rows = (sb.table("safety_lists").select("symbol")
                        .in_("list_type", ["ASM", "GSM", "FO_BAN"])
                        .execute().data or [])
            flagged = {r["symbol"] for r in sl_rows if r.get("symbol")}
        except Exception as e:
            logger.debug(f"  scanner: safety_lists read skipped — {e}")

    _ref_cache.update(date=today, known=known, market_rows=market_rows, flagged=flagged)
    return known, market_rows, flagged


# Module-level, once-per-day cache for the Kite-instrument baseline diff —
# same reasoning as _ref_cache above, plus fetch_nse_eq_symbols() is itself
# a ~10k-row fetch best done once, not every 45s.
_baseline_cache: dict = {"date": None, "symbols": None}


def _seed_baseline(sb, symbols: set[str]) -> None:
    rows = [{"symbol": s} for s in symbols]
    for i in range(0, len(rows), 500):
        try:
            sb.table("kite_symbol_baseline").upsert(
                rows[i:i + 500], on_conflict="symbol").execute()
        except Exception as e:
            logger.debug(f"  scanner: kite_symbol_baseline write skipped — {e}")
            return


def new_listings(sb=None) -> set[str]:
    """
    Which Kite-known mainboard/ETF symbols have NEVER BEEN SEEN before —
    the actual "new listing" signal. Stage D2d, 23-Aug-2026 (docs/
    TRADEOS_ROADMAP.md, Track D), replacing unreferenced_candidates()'
    original Population C definition ("every Kite symbol not in
    nifty_total_market or stock_data_daily"), which measured 2,081 names
    live on 23-Aug — almost none of them actual IPOs, nearly all of them
    small/micro-cap names simply outside both reference tables' own
    coverage. Quoting ~2,100 names every 45s to catch an event that
    happens a handful of times a MONTH was the operator's own, correct
    objection — this fixes the definition, not just the size.

    Diffs today's live `kite_client.fetch_nse_eq_symbols()` against
    `kite_symbol_baseline`, a table this function itself maintains: a
    symbol present today but never recorded before is genuinely new, and
    is written to the baseline immediately so it is never reported new
    again. BOOTSTRAP: on the very first run ever (empty baseline table —
    true the first time this ships), the WHOLE current universe (~2,979
    names) is seeded as already-known and NONE of it is reported new —
    there is no listing history yet to diff against, so "new" is
    undefined, not "all of it".

    Returns an empty set on any failure to read either side of the diff —
    advisory only, same contract as every other function that touches
    Kite or a reference table in this module; an empty result means
    "could not check", not "nothing is new today".
    """
    sb = sb or get_supabase()
    try:
        from kite.kite_client import fetch_nse_eq_symbols
        live = fetch_nse_eq_symbols()
    except Exception as e:
        logger.debug(f"  scanner: Kite instrument fetch skipped — {e}")
        return set()
    if not live:
        return set()

    today = today_ist().isoformat()
    if _baseline_cache["date"] != today or _baseline_cache["symbols"] is None:
        try:
            rows = sb.table("kite_symbol_baseline").select("symbol").execute().data or []
            _baseline_cache["date"] = today
            _baseline_cache["symbols"] = {r["symbol"] for r in rows if r.get("symbol")}
        except Exception as e:
            logger.debug(f"  scanner: kite_symbol_baseline read skipped — {e}")
            return set()

    known = _baseline_cache["symbols"]
    if not known:
        _seed_baseline(sb, live)
        _baseline_cache["symbols"] = set(live)
        return set()

    fresh = live - known
    if fresh:
        _seed_baseline(sb, fresh)
        _baseline_cache["symbols"] = known | fresh
    return fresh


def unreferenced_candidates(sb=None, exclude: set[str] | None = None
                            ) -> list[UniverseEntry]:
    """
    Tradeable NSE EQ names that are NOT in stock_data_daily at all — so no
    historical ATR exists to be relative to, and live_requalify()'s normal
    admission floor must be applied as an ABSOLUTE threshold instead.
    Stage D2b/d, 23-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D).

    TWO SOURCES:

      Population B — nifty_total_market members (754 rows after the
      weekly refresh) not in stock_data_daily (231 of them, live-
      confirmed 23-Aug). Known, legitimate NSE names simply outside
      swing's own sheet-baseline universe — not new listings, just
      untracked by that pipeline. Bounded and stable; not the population
      that needed fixing.

      Population C — new_listings() above: Kite-known symbols never
      seen before ANY prior run, not "every Kite symbol we don't already
      track". Typically zero, occasionally a handful around a real IPO —
      not the ~2,081-name flood the original, cruder definition produced.

    `atr_pct=0.0` on every returned entry, HONESTLY — there is nothing
    real to put there, and 0.0 combined with the `reason` string naming
    exactly which source found it is preferable to fabricating a number
    or borrowing an unrelated one.

    ASM/GSM/F&O-BAN, CHECKED INDEPENDENTLY OF `stock_data_daily`. Neither
    population has an `asm_flag`/`fo_ban_flag` column to read (that lives
    on `stock_data_daily` rows, which these names are not), so
    `_qualifies()`'s own "flagged" gate cannot see them at all without
    this. `safety_lists` (`swing/ingestion/ingest_asm_gsm.py`) is keyed on
    bare symbol independent of `stock_data_daily`, so it can answer this
    for ANY name, tracked or not. Reuses `intraday_skip_flagged` — the
    same switch `_qualifies()` reads — rather than a new one, so arming
    it behaves identically everywhere this project checks surveillance
    status.
    """
    sb = sb or get_supabase()
    exclude = exclude or set()

    known, market_rows, flagged = _daily_reference_reads(sb)

    out: list[UniverseEntry] = []
    seen: set[str] = set()

    for r in market_rows:
        sym = r.get("symbol")
        if not sym or sym in exclude or sym in known or sym in seen or sym in flagged:
            continue
        seen.add(sym)
        out.append(UniverseEntry(
            symbol=sym, close=0.0, value_cr=0.0, atr_pct=0.0, delivery_pct=None,
            sector=r.get("industry") or "", score=0.0,
            reason="not tracked in stock_data_daily — nifty_total_market member",
            avg_vol_20d=0.0,
        ))

    try:
        instrument_symbols = new_listings(sb)
    except Exception as e:
        logger.debug(f"  scanner: Kite new-listing diff skipped — {e}")
        instrument_symbols = set()

    for sym in sorted(instrument_symbols):
        if sym in exclude or sym in known or sym in seen or sym in flagged:
            continue
        seen.add(sym)
        out.append(UniverseEntry(
            symbol=sym, close=0.0, value_cr=0.0, atr_pct=0.0, delivery_pct=None,
            sector="", score=0.0,
            reason=("never seen in Kite's instrument master before today — "
                   "genuine new listing, not in stock_data_daily or nifty_total_market"),
            avg_vol_20d=0.0,
        ))

    return out
