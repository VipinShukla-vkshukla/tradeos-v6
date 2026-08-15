"""
The intraday decision loop.

Reuses the EXISTING decision logic rather than reimplementing it:

    control.position_lifecycle.evaluate_exit   exits, partials, trailing, time stop
    analysis.trade_decision.decide             entries, R:R, chase limits
    analysis.portfolio_constraints             sector caps and sizing

That reuse is the single most important design decision in this package. A
second copy of the exit rules would drift from the first — this project has
already been through exactly that, with three divergent copies of the R:R model
producing three different answers for the same stock on the same day. The
intraday loop must reach the SAME verdict as the evening pipeline given the same
inputs, or neither can be trusted.

What is genuinely new here is only: when to look, and what to do about it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (IST, get_supabase, cfg, cfg_bool, cfg_int, cfg_float,
                    today_ist, fetch_all)
from intraday.config import (is_market_open, gtt_enabled, orders_enabled,
                             autonomy_phase)
from intraday.notifier import Notifier, Action
from intraday import direction as D


def _intraday_may_join_swing_holding(other: dict | None, direction: str) -> bool:
    """
    True if an INTRADAY setup may proceed despite the name being held elsewhere.

    Layered UNDER `one_framework_per_symbol`, which stays the master invariant
    and is unaffected by this: the SWING side of `_other_framework_holding`
    (evaluate_candidates, standing down when INTRADAY already holds a name)
    keeps refusing unconditionally, exactly as before. This only narrows the
    INTRADAY side of the same check — the one that used to refuse
    unconditionally whenever SWING got to a name first.

    A SHORT is refused regardless of the switch: shorting a name the swing
    book is long reintroduces the "two exit ladders that contradict each
    other" risk DESIGN_NOTES.md #1 warns about, and is not what
    `intraday_allow_swing_held_symbols` is for. Only a SWING holding is ever
    joinable — a name INTRADAY itself already holds is a different case
    (duplicate entry) this function has no opinion on.
    """
    if other is None:
        return True
    if (other.get("framework") or "SWING").upper() != "SWING":
        return False
    if D.is_short(direction):
        return False
    return cfg_bool("intraday_allow_swing_held_symbols", True)


def _legacy_rank_gate_blocks(here_total: float, field_totals: list[float],
                             keep: int, alloc_live_swing: bool) -> bool:
    """
    True if entry_ranking's own top-N gate should veto this swing candidate.

    Built 04-Aug-2026 as swing's ONLY ranking — "which of today's buyable
    plans deserves the day's limited entries" (entry_ranking.py's own
    docstring), reading final_score/AI-tier/timing/sector, over the day's
    full candidate field. `swing_assignment`'s reservation mechanism, wired
    live 07-Aug, now does the equivalent job over a DIFFERENT field (edge —
    engine-family + regime-bucket historical R, cost-netted) and nothing
    ties the two rankings' winners together.

    Confirmed against real data the day this was found: the allocator's
    edge-ranked TAKE winners (HEG, ACE, CHALET) were not in entry_ranking's
    top-6 by score that same day, and the one candidate that WAS rank-
    eligible (PIDILITIND) never had a high enough edge to win an allocator
    slot. Both gates individually correct; together, on 07-Aug, they
    admitted nothing — swing_max_new_per_day=3 logged "3 to take" from the
    allocator on nearly every cycle and still finished the session at 0/3
    entries used.

    So: once the allocator is the live veto, its own ranking already
    answered this question, and re-asking it here over an unrelated field is
    not a second opinion — it never engages. This gate is the fallback for
    whenever the allocator is off or still shadow-only, exactly the role it
    has always had.
    """
    if alloc_live_swing:
        return False
    if not keep:
        return False
    return here_total < field_totals[keep - 1]


def _runway_refusal_summary(runway_refused: list[str], out: list[dict]) -> str | None:
    """
    Pure. What to tell the operator when one or more shorts were refused on
    runway this cycle — and, in the same cycle, what WAS actionable instead.

    Runway refusals are about the clock, not the market: can_short()'s cover-
    deadline check says nothing about whether the tape is offering anything,
    but a run of "short refused — N min to the cover deadline" lines in the
    console reads exactly like a dead market. Pointing at the long setups the
    SAME cycle produced turns that into "here is what to look at instead"
    rather than a dead end — the entry-side twin of exit_policy.py's runway-
    tighten rung, which does the equivalent for a short already open.

    Returns None when there is nothing to say, so the caller can log
    unconditionally without an extra `if runway_refused:` at the call site.
    """
    if not runway_refused:
        return None
    longs_fired = sum(1 for s in out if not s["setup"].is_short)
    if longs_fired:
        return (f"      {len(runway_refused)} short(s) refused on runway this "
               f"cycle ({', '.join(runway_refused)}) — {longs_fired} long "
               f"setup(s) fired in the same cycle, unaffected by the cover deadline")
    return (f"      {len(runway_refused)} short(s) refused on runway this "
           f"cycle ({', '.join(runway_refused)}) — no long setups fired "
           f"either; the quiet stretch is not runway-specific")


def _parse_runway_refusals(rows: list[dict]) -> list[dict]:
    """
    Pure. Filters raw intraday_setups rows (cost_verdict=BLOCKED_SHORTABILITY)
    down to the ones refused specifically for RUNWAY — not circuit-band,
    squeeze or liquidity, which share the same verdict bucket but are about
    the STOCK, not the clock, and have no "try again tomorrow" story.

    Matches on the same "cover deadline" phrase _runway_refusal_summary()
    already keys on for the live per-cycle line, so the two cannot classify
    the same refusal differently.
    """
    out = []
    for r in rows:
        meta = r.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        meta = meta or {}
        if "cover deadline" in (meta.get("shortability_reason") or ""):
            out.append(r)
    return out


def _gap_continuation_shorts(runway_rows: list[dict], y_closes: dict[str, float],
                             today_opens: dict[str, float]) -> list[str]:
    """
    Pure. Which of yesterday's runway-refused shorts gapped down again at
    today's open — the most conservative revalidation available: the tape is
    still confirming the thesis, not merely "detected yesterday and never
    disproven". Anything looser (re-running the engine cold, a bare
    reminder) is a different bet than this checks.

    y_closes / today_opens: symbol -> price. A symbol missing either price
    is skipped rather than guessed at — no data is not evidence either way.
    """
    out, seen = [], set()
    for row in runway_rows:
        sym = row.get("symbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        yclose = y_closes.get(sym)
        opn = today_opens.get(sym)
        if not yclose or not opn:
            continue
        if opn < yclose:   # gapped down, continuing the weakness the short wanted
            out.append(sym)
    return out


class IntradayEngine:
    def __init__(self, sb=None, notifier: Notifier | None = None):
        self.sb = sb or get_supabase()
        self.notifier = notifier or Notifier(self.sb)
        self.positions: list[dict] = []
        self.candidates: list[dict] = []
        self._policy = None
        self._intraday_policy = None
        # Live trend/deterioration evidence for held SWING positions — see
        # refresh_trend_context(). Kept separate from self._policy (the exit
        # RULES, loaded once) because this is EVIDENCE, refreshed on its own
        # cadence and merged into self._policy["_trend_ctx"] on every refresh.
        self._trend_ctx: dict = {}
        self._last_state: dict[str, str] = {}
        # Bar history per symbol, assembled once per cycle and shared by all
        # seven engines — seven engines each fetching their own history would
        # multiply API calls sevenfold for identical data, and would let two
        # engines disagree about the same bar.
        self._contexts: dict = {}
        self._index_ctx = None
        # symbol -> latest stock_data_daily row, for the FULL bench (not just
        # context_symbols()'s 40) — refreshed alongside self._contexts in
        # refresh_contexts(). merge_live_bars() uses this to give a
        # tick-only, bench-only context its prev_close/prev_high/prev_low/
        # atr_pct/avg_volume_20d without spending a historical_data call.
        self._daily_ref: dict = {}
        # Epoch-ish sentinel so the first cycle logs parity immediately rather
        # than waiting a full interval for the first sample.
        self._last_parity_at = datetime.fromtimestamp(0, IST)
        # Throttles merge_live_bars()'s own summary line to the same 300s
        # cadence as the parity log above, for the same reason: it runs
        # every 15s cycle and logging on every one would be noise, not
        # visibility. See merge_live_bars().
        self._last_bars_log_at = datetime.fromtimestamp(0, IST)
        self._allocator = None
        # The intraday universe, selected on its OWN criteria. Until this
        # existed the engines only saw swing shortlist names — stocks chosen for
        # a one-to-three-week thesis, which predicts almost nothing about
        # whether a name moves enough today to pay for a round trip.
        self._universe: list[str] = []
        # The wider LTP-only candidate pool self._universe is re-ranked from
        # every 15s cycle — see rerank_universe_live(). Rebuilt on the slow
        # timer alongside self._universe; a list of scanner.UniverseEntry,
        # not symbols, because live_rerank() needs each entry's avg_vol_20d.
        self._bench: list = []
        # symbol:strategy -> (entry, verdict) already recorded this session.
        # REHYDRATED FROM THE DATABASE, NOT STARTED EMPTY — see
        # _rehydrate_recorded(). A restart used to hand the day a blank map.
        self._recorded: dict[str, tuple[float, str]] = {}
        # Event awareness and AI advice, both refreshed on the SLOW timer.
        # The AI in particular can never sit in the fast loop: step 19's
        # DeepSeek call took 88.6s measured, and this loop runs every 15s.
        self._news = None
        self._advice: dict = {}
        self._pending_review: list = []
        # Structural overlays (analysis.overlays) — expiry day-type and VIX
        # exposure scaling. Both are BOOK-level, not per-symbol, and both are
        # slow-changing (the day type does not change intraday; VIX moves in
        # minutes, not seconds), so they are computed once on the SLOW timer
        # in refresh_advisory() rather than per-symbol inside the 15s loop,
        # which would multiply their DB reads by the watchlist size for a
        # value that has not moved.
        #
        # Two separate multipliers, not one: expiry-day sizedown is an
        # INTRADAY-only overlay (its rationale is cash-tape microstructure —
        # pinning, unwind flows — that condition opening-range/VWAP-reversion
        # archetypes specifically, not a 1-3 week thesis). VIX exposure
        # scaling applies to BOTH books' open risk.
        self._overlay_intraday_mult: float = 1.0   # expiry x VIX, intraday budget only
        self._overlay_vol_mult: float = 1.0         # VIX only, shared with swing sizing
        self._overlay_note: str = ""
        # Confidence of every intraday setup already alerted this session, for
        # the streaming top-N in _intraday_alert_worthy(). Session-scoped and in
        # memory on purpose: a restart SHOULD re-announce the current best,
        # since you have no way of knowing whether the earlier message was seen.
        self._alerted_conf: list = []
        # Swing entries taken by THIS process today. Guards the daily cap
        # against read-after-write lag on the broker log — see
        # _maybe_enter_swing().
        self._entries_taken = 0
        # symbol -> Kite order_id, for a BUY submitted but not yet confirmed
        # filled. In-memory guard against re-submitting the same entry on the
        # next 15s cycle; the DB row (status=PENDING_FILL, entry_order_id) is
        # the durable copy this rebuilds from on a restart — see
        # _maybe_enter_swing() and _resolve_pending_fills().
        self._pending_fills: dict[str, str] = {}

    # ── state ───────────────────────────────────────────────────────────────
    def _rehydrate_recorded(self) -> None:
        """
        Rebuild the setup-dedup map from today's already-persisted rows.

        WHY A RESTART USED TO MAKE THE ALLOCATOR'S BAR WORSE — 10-Aug-2026.

        `_setup_is_new` exists because the loop runs every 15 seconds and a
        setup valid at 11:00 is still valid at 11:00:15; without it CONCOR/PDL
        was written four times in two minutes. But the map it consults lived
        only in memory, so `systemd` restarting the daemon mid-session handed
        the day a blank one and every setup already recorded that morning
        counted as new again. Observed live on 10-Aug: the service restarted
        at 13:11:57 and the very next AI review went from 11 setups to 31.

        That is not merely duplicate rows. `intraday_setups` is the population
        `scoring.intraday_priors()` builds every engine's prior from, and
        `allocation_decisions` is the population `hurdle()` takes its
        percentile of — so a restart re-inflates both, and a re-inflated
        arrival population LOWERS the bar. A daemon restart was quietly making
        the allocator more permissive for the rest of the session.

        THE KEY IS THE ENGINE, THE COLUMN IS THE FAMILY. `_record_setup`
        deliberately writes `strategy = family` so the learning loop groups on
        the family (ORB's 30 detections become the family's 230), and keeps the
        engine that actually fired in `meta.sub_engine`. But `_setup_is_new`
        keys on `s.strategy`, the ENGINE. Rehydrating off the `strategy` column
        alone would therefore build keys like "PAYTM:ORB" (family) that the
        live dedup test — asking about "PAYTM:GAP" (engine) — never matches,
        and the rehydration would silently do nothing while reporting a count.
        So `meta.sub_engine` is preferred and the column is only the fallback.

        Reads only today's rows. Failure is non-fatal and LOUD: an empty map is
        the old behaviour, which is survivable, but it must not be reached
        silently.
        """
        try:
            # PAGED — a session now routinely exceeds the 1000-row PostgREST
            # cap (14-Aug-2026: 2289 detections). Capped, this map rehydrates
            # only the part of the morning that fitted, so a restart re-records
            # everything it could not see — which is the exact re-inflation of
            # the prior population and the allocator's arrival bar that this
            # function was written to prevent, arriving through a different
            # door and reporting a healthy-looking count on the way in.
            rows = fetch_all(lambda: self.sb.table("intraday_setups")
                             .select("symbol,strategy,entry,cost_verdict,meta")
                             .eq("trade_date", today_ist().isoformat()))
            for r in rows:
                sym = r.get("symbol")
                if not sym:
                    continue
                meta = r.get("meta")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                engine = (meta or {}).get("sub_engine") or r.get("strategy")
                if not engine:
                    continue
                try:
                    entry = float(r.get("entry") or 0)
                except (TypeError, ValueError):
                    continue
                self._recorded[f"{sym}:{engine}"] = (entry, r.get("cost_verdict") or "")
            if rows:
                logger.info(f"  setup dedup: rehydrated {len(self._recorded)} "
                            f"symbol:engine key(s) from today's rows")
        except Exception as e:
            logger.warning(
                f"  setup dedup: could not rehydrate today's recorded setups "
                f"({str(e)[:90]}) — starting empty. Detections already written "
                f"this session may be recorded a second time, which inflates "
                f"both the prior population and the allocator's arrival bar.")

    def load_state(self) -> None:
        """Re-read positions and candidates. Cheap; called on a slow timer."""
        # status is 'ACTIVE', matching control/position_lifecycle. Filtering on
        # 'OPEN' returned zero rows without raising — a monitor watching nothing
        # while reporting success, which is the exact failure this whole
        # subsystem exists to prevent. So the filter is verified rather than
        # trusted: if it drops every row, say so loudly and fall back to
        # watching everything, because over-watching is recoverable and
        # silently watching nothing is not.
        all_rows = self.sb.table("open_positions").select("*").execute().data or []
        active = [r for r in all_rows if (r.get("status") or "").upper() == "ACTIVE"]
        if all_rows and not active:
            logger.error(
                f"  engine: {len(all_rows)} position row(s) exist but none have "
                f"status=ACTIVE (found: {sorted({r.get('status') for r in all_rows})}). "
                f"Watching all of them rather than silently monitoring nothing."
            )
            active = all_rows
        self.positions = active

        # Rebuild the pending-fill guard from the DB, not just from memory —
        # a restart between placing an entry order and confirming its fill
        # must not forget the entry was attempted, or the next cycle submits
        # a second one for the same symbol. all_rows is already unfiltered by
        # status, so this costs nothing extra.
        self._pending_fills = {
            r["symbol"]: r["entry_order_id"]
            for r in all_rows
            if (r.get("status") or "").upper() == "PENDING_FILL" and r.get("entry_order_id")
        }

        try:
            latest = (self.sb.table("signal_output_daily").select("date")
                        .order("date", desc=True).limit(1).execute().data or [])
            if latest:
                rows = (self.sb.table("signal_output_daily").select("*")
                          .eq("date", latest[0]["date"]).execute().data or [])
                # Filter on what the plan SAYS TO DO, not on an AI label.
                #
                # This used to keep only ai_tier in {TIER_1, TIER_2}. That set
                # does not match the vocabulary the model actually emits: of 84
                # plans, 75 came back WATCH_CLOSELY, 5 TIER_3, and only 4 were
                # TIER_1/TIER_2. So 6 of the day's 8 BUY_NOW plans were never
                # put on the price feed — including five whose tier is literally
                # named "watch closely".
                #
                # SKIP is dropped because a hard gate failed and no intraday
                # price will change that. Everything else is kept: a WAIT plan
                # is precisely the one worth watching, since the reason it says
                # wait is that it wants a better entry, and catching that is the
                # entire purpose of streaming swing candidates.
                from analysis.trade_decision import decide
                keep = []
                for r in rows:
                    try:
                        if decide(r, None).action != "SKIP":
                            keep.append(r)
                    except Exception:
                        keep.append(r)   # undecidable is not the same as unwanted
                cap = cfg_int("swing_watch_max", 150)
                if len(keep) > cap:
                    keep.sort(key=lambda p: -(p.get("final_score") or p.get("score") or 0))
                    logger.info(f"  engine: {len(keep)} live swing candidates, "
                                f"streaming the top {cap} by score")
                    keep = keep[:cap]
                self.candidates = keep
        except Exception as e:
            logger.warning(f"  engine: candidate load failed — {e}")
            self.candidates = []

        if self._policy is None:
            from control.position_lifecycle import load_exit_policy
            self._policy = load_exit_policy()

    def refresh_contexts(self) -> int:
        """
        Assemble today's bars and reference levels for every watched symbol,
        plus the index.

        Called on the slow timer, not per cycle. Minute bars change once a
        minute, so re-fetching them every 15 seconds would triple the API load
        for data that has not moved — and the historical endpoint is rate
        limited far more tightly than quotes.

        Silently produces nothing when the broker is unavailable, which
        correctly disables the strategy engines rather than letting them run on
        a partial view: an ORB computed from four bars is not a conservative
        ORB, it is a wrong one.
        """
        from intraday.strategies.base import Bar, SymbolContext
        from intraday.market_context import INDEX_SYMBOL
        try:
            from kite import kite_client
            kite = kite_client.get_kite()
            if not kite:
                self._contexts, self._index_ctx = {}, None
                return 0
        except Exception:
            self._contexts, self._index_ctx = {}, None
            return 0

        # context_symbols(), not watch_symbols(): bars are the rate-limited
        # resource and only the engines and open positions consume them.
        symbols = self.context_symbols()
        if not symbols:
            return 0

        interval = cfg("intraday_bar_interval", "minute")
        today = today_ist()
        built: dict = {}

        try:
            tokens = kite.ltp([f"NSE:{s}" for s in symbols] + [f"NSE:{INDEX_SYMBOL}"])
        except Exception as e:
            logger.warning(f"  contexts: token lookup failed — {e}")
            return 0

        # Daily reference levels come from the table the swing pipeline already
        # maintains. Re-deriving prev_high/prev_low from the broker would be a
        # second source of truth for a number stock_data_daily already holds.
        #
        # WIDENED TO THE FULL BENCH, NOT JUST context_symbols() — 11-Aug-2026.
        # This is one Supabase select with a longer IN() list, not one call
        # per symbol like historical_data() — it is not the resource
        # intraday_max_universe exists to protect. merge_live_bars() reads
        # self._daily_ref to give a bench-only, tick-built context its
        # prev_close/prev_high/prev_low/atr_pct/avg_volume_20d without
        # spending a single extra historical_data call.
        ref_symbols = sorted(set(symbols) | {e.symbol for e in (self._bench or [])})
        prev: dict = {}
        try:
            hist = (self.sb.table("stock_data_daily")
                      .select("symbol,date,high,low,close,atr_pct,volume,"
                              "value_cr,sector")
                      .in_("symbol", ref_symbols)
                      .order("date", desc=True).limit(len(ref_symbols) * 3)
                      .execute().data or [])
            for r in hist:
                if r["symbol"] not in prev and str(r["date"]) < today.isoformat():
                    prev[r["symbol"]] = r
        except Exception as e:
            logger.debug(f"  contexts: prev-day levels unavailable — {e}")
        self._daily_ref = prev

        for key, meta in (tokens or {}).items():
            sym = key.split(":", 1)[1]
            try:
                raw = kite.historical_data(
                    meta["instrument_token"], today, today, interval) or []
            except Exception:
                continue
            if len(raw) < 5:
                continue

            bars = [Bar(b["date"], float(b["open"]), float(b["high"]),
                        float(b["low"]), float(b["close"]), float(b.get("volume") or 0))
                    for b in raw]

            # VWAP from today's own bars — the definition, not an approximation.
            tv = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars)
            vol = sum(b.volume for b in bars)
            vwap = (tv / vol) if vol else None
            if vwap is None and bars:
                # Indices report zero volume, so the weighted average is
                # undefined and came back None — which made "above VWAP" always
                # false and pinned the market gate at CAUTION forever. A gate
                # that can never say RISK_ON is not a gate. Fall back to the
                # UNWEIGHTED average of typical price, which is what an index
                # VWAP line effectively is anyway.
                vwap = sum((b.high + b.low + b.close) / 3.0 for b in bars) / len(bars)

            p = prev.get(sym, {})
            ctx = SymbolContext(
                symbol=sym, ltp=float(meta.get("last_price") or bars[-1].close),
                bars=bars, vwap=vwap,
                day_open=bars[0].open,
                day_high=max(b.high for b in bars),
                day_low=min(b.low for b in bars),
                prev_close=float(p.get("close") or 0) or None,
                prev_high=float(p.get("high") or 0) or None,
                prev_low=float(p.get("low") or 0) or None,
                atr_pct_daily=float(p.get("atr_pct") or 0) or None,
                avg_volume_20d=float(p.get("volume") or 0) or None,
                value_cr=float(p.get("value_cr") or 0) or None,
                # Stamped so every consumer can ask how old this is. Contexts
                # are rebuilt on the 300 s timer and read on the 15 s one.
                as_of=datetime.now(IST),
            )
            if sym == INDEX_SYMBOL:
                # The index is not in stock_data_daily — that table holds
                # stocks — so its prev_close came back None and the market gate
                # silently degraded to NEUTRAL on every cycle. A gate that is
                # always neutral is not a gate. Take the previous close from the
                # broker's own history instead, which is where it actually is.
                if not ctx.prev_close:
                    try:
                        y = kite.historical_data(
                            meta["instrument_token"],
                            today - timedelta(days=7), today - timedelta(days=1),
                            "day") or []
                        if y:
                            ctx.prev_close = float(y[-1]["close"])
                            ctx.prev_high = float(y[-1]["high"])
                            ctx.prev_low = float(y[-1]["low"])
                    except Exception as e:
                        logger.debug(f"  index prev close unavailable: {e}")
                self._index_ctx = ctx
            else:
                built[sym] = ctx

        # Relative strength against the index, computed once and identically for
        # every engine so "strong today" means one thing across the system.
        idx_chg = None
        if self._index_ctx and self._index_ctx.prev_close:
            idx_chg = ((self._index_ctx.ltp - self._index_ctx.prev_close)
                       / self._index_ctx.prev_close * 100.0)
        for ctx in built.values():
            if idx_chg is not None and ctx.prev_close:
                ctx.rs_vs_index_pct = round(
                    (ctx.ltp - ctx.prev_close) / ctx.prev_close * 100.0 - idx_chg, 2)

        self._contexts = built
        logger.info(f"  contexts: {len(built)} symbols with bars"
                    + (f", index {idx_chg:+.2f}%" if idx_chg is not None else ", no index"))
        return len(built)

    def apply_live_quotes(self, feed) -> int:
        """
        Overlay live day range, volume and prev_close onto the contexts. Per
        cycle. VWAP is a separate, independently-gated overlay — see below.

        WHY THIS EXISTS. refresh_contexts() runs on the 300-second timer because
        the historical endpoint is rate-limited; the decision loop runs every 15
        seconds. Between refreshes the day high, the day low and the session
        volume were simply wrong — a breakout at 10:47 was measured against a
        range that stopped at 10:45. The websocket already carries all three in
        QUOTE mode, at no extra API cost, so the fix is to prefer the live value
        and keep the fetched one as the fallback.

        FALLBACK IS PER FIELD, NOT PER SYMBOL. A tick that carries volume but no
        OHLC must update volume and leave the range alone. Treating a missing
        field as zero is how a live upgrade becomes a downgrade.

        THE INDEX FALLBACK IS PRESERVED DELIBERATELY. Indices report zero traded
        volume, so a volume-weighted average price is undefined for them, and
        refresh_contexts falls back to the unweighted average of typical price.
        An earlier version of that fallback was missing and the market gate sat
        pinned at CAUTION forever — a gate that can never say RISK_ON is not a
        gate. So a zero or absent VWAP from the tick stream never overwrites the
        computed one.

        TWO SWITCHES, BOTH DEFAULT ON — 08-Aug-2026, migration 056.
        `intraday_quote_mode_range` gates day_open/day_high/day_low/volume/
        prev_close; `intraday_quote_mode_vwap` gates vwap on its own. Both
        default to the same 'true' migration 043 already set for the single
        switch this replaces — this is a REGROUPING for independent control
        and ongoing verification, not a behaviour change from what has been
        live since Phase 4. tools/quote_parity.py's `check_quote_parity`
        (tools/health.py) now watches both continuously, so if either one
        ever does need turning off, that gets caught by the check run before
        every trading session rather than left to be noticed by hand.

        PREV_CLOSE'S PLACE IN THIS. It measured FAULT against refresh_contexts()
        (worst -4.18%), but the live side here is Kite's own broker-reported
        previous close, and the fetched side is `stock_data_daily` via a
        global, not per-symbol, row LIMIT that can silently resolve to a
        stale multi-day-old close for a thin symbol. The more likely
        direction is that the live tick is CORRECTING the fetched side's bug,
        not disagreeing with a correct one — so it stays in the overlay
        rather than being pulled out on an unconfirmed guess about which side
        is wrong. The stock_data_daily query itself is a separate, still-open
        bug, unrelated to whether this overlay runs.

        VWAP'S FAULT IS A DIFFERENT SHAPE — a formula difference (live tick
        VWAP vs refresh_contexts()'s bar-approximation), not a staleness gap,
        so some disagreement is structural and expected rather than a sign
        either side is simply wrong. Kept on to match current behaviour;
        `_vwap_engine_relevance`/`vwap_verdict` in tools/quote_parity.py are
        what would tell you if it starts moving a real engine decision.
        """
        from intraday.market_context import INDEX_SYMBOL
        # 07-Aug-2026: `now` was read at three points below (the parity-log
        # interval check, and as_of on both the per-symbol and index overlays)
        # and assigned at NONE of them — this function has never had a local
        # `now`. Every call with intraday_quote_parity_log on raised
        # NameError at the very first reference, before the intraday_quote_mode
        # branch below it was ever reached, and cycle()'s outer
        # `except Exception: logger.debug(...)` swallowed it below normal log
        # level. Both switches have read TRUE in system_config since 04-Aug;
        # the live overlay itself has run zero times since, which is why
        # intraday_quote_parity — logged from inside the very block this
        # crashed in — has zero rows despite three days of market sessions.
        now = datetime.now(IST)
        # Parity logging is independent of quote MODE being consumed: the whole
        # point is to compare the two BEFORE the live one is trusted, which means
        # it must run while quote mode is still off.
        #
        # RATE-LIMITED AND BATCHED, because this function runs every 15 seconds.
        # The first version wrote one synchronous insert per symbol per field per
        # cycle — roughly 95 x 4 x 240 = 91,000 round trips in a session, every
        # one of them inside the decision loop that is required to do no
        # synchronous writes at all. It would have delayed exit evaluation on
        # live positions behind a network call, which is the exact failure the
        # buffered-write rule exists to prevent.
        #
        # Once per slow-timer interval is also the right sampling rate: the
        # fetched side only changes when refresh_contexts runs, so comparing more
        # often just records the same fetched number against a moving live one.
        if feed is not None and cfg_bool("intraday_quote_parity_log", False):
            interval = cfg_float("intraday_context_refresh_s", 300.0)
            if (now - self._last_parity_at).total_seconds() >= interval:
                self._last_parity_at = now
                try:
                    from tools import quote_parity
                    batch = []
                    for sym, ctx in (self._contexts or {}).items():
                        q = feed.quote(sym)
                        if not q:
                            continue
                        # Bar-summed volume is the "fetched" side of the
                        # comparison — quote_parity.py already reports it
                        # unscored (see SCORED there), but nothing computed a
                        # fetched value to log it against until now, so it was
                        # documented as logged and never actually collected.
                        bar_vol = sum(b.volume for b in ctx.bars) if ctx.bars else None
                        for field, fetched in (("day_high", ctx.day_high),
                                               ("day_low", ctx.day_low),
                                               ("vwap", ctx.vwap),
                                               ("prev_close", ctx.prev_close),
                                               ("volume", bar_vol)):
                            row = quote_parity.compare(sym, field, q.get(field), fetched)
                            if row:
                                batch.append(row)
                    quote_parity.record_many(self.sb, batch)
                except Exception as e:
                    logger.debug(f"  parity logging skipped: {e}")

        range_on = cfg_bool("intraday_quote_mode_range", False)
        vwap_on = cfg_bool("intraday_quote_mode_vwap", False)
        if not (range_on or vwap_on):
            return 0
        touched = 0

        def _overlay(ctx, q) -> list[str]:
            live = []
            if range_on:
                for field, key in (("day_high", "day_high"), ("day_low", "day_low"),
                                   ("day_open", "day_open"), ("prev_close", "prev_close")):
                    v = q.get(key)
                    if v:
                        setattr(ctx, field, float(v))
                        live.append(field)
                if q.get("volume"):
                    ctx.session_volume = float(q["volume"])
                    live.append("volume")
            # Never let a zero VWAP through — see the index note above.
            if vwap_on and q.get("vwap"):
                ctx.vwap = float(q["vwap"])
                live.append("vwap")
            return live

        for sym, ctx in (self._contexts or {}).items():
            q = feed.quote(sym) if feed else None
            if not q:
                continue
            live = _overlay(ctx, q)
            if live:
                ctx.as_of = q.get("at") or now
                ctx.live_fields = tuple(live)
                touched += 1

        # THE INDEX GETS THE SAME OVERLAY, FOR THE SAME REASON.
        #
        # self._index_ctx is not part of self._contexts — it is assembled and
        # stored separately in refresh_contexts() — so the loop above never
        # touched it, and this function's own docstring already anticipated
        # index-specific behaviour ("the index fallback is preserved
        # deliberately") for a case the code never actually reached. Day
        # high/low move slowly enough that a 300s-stale range is a small
        # error; see _refresh_index_ltp() for the more consequential half —
        # LTP, refreshed unconditionally, not gated behind quote mode.
        if feed is not None and self._index_ctx is not None:
            q = feed.quote(INDEX_SYMBOL)
            if q:
                live = _overlay(self._index_ctx, q)
                if live:
                    self._index_ctx.as_of = q.get("at") or now
                    self._index_ctx.live_fields = tuple(live)
                    touched += 1
        return touched

    def merge_live_bars(self, feed) -> int:
        """
        Extend today's bars for every BENCH name from the tick stream, not
        just the intraday_max_universe (40) that get historical_data() bars
        on the 300s timer. See bar_builder.py for the mechanism.

        THE CORE FIX — 11-Aug-2026. refresh_contexts() ties bar availability
        to intraday_max_universe because historical_data() is rate limited.
        feed.bars() is not — it is built from ticks the socket already
        carries, at zero incremental API cost — so it can cover the full
        bench (intraday_universe_bench_size, 120 by default) without
        touching that budget at all. This is what turns
        scanner.live_rerank() (10-Aug) from "reshuffle which 40 get to
        matter, on a 300s delay" into "every bench name is actually
        watchable within one 15s cycle of it starting to move" — the
        constraint the 11-Aug institutional review identified as binding
        selection quality more than engine logic did.

        TWO CASES, ONE LOOP:
          - a symbol already has a context (it is one of the 40 in
            context_symbols()) — append any live-built minute bars newer
            than what refresh_contexts() gave it. Scalar fields (day_high/
            day_low/vwap) are apply_live_quotes()'s job, not this one's;
            this touches only the bars LIST.
          - a symbol is bench-only (outside the top 40, so refresh_contexts()
            never built it a context at all) — build one from live bars plus
            the cheap daily-reference row already fetched for the whole
            bench in refresh_contexts() (self._daily_ref). Gated on
            intraday_min_live_bars_for_context (default 5) so a name that
            started ticking thirty seconds ago is correctly "not enough
            bars yet" — the same guard the historical side already applies
            (`len(raw) < 5: continue`).

        Every engine keeps reading ctx.bars exactly as before; nothing about
        the Setup contract changes. A symbol that never accumulates enough
        live bars (illiquid, or the session just started) simply keeps
        whatever refresh_contexts() gave it, or nothing — no engine runs on
        a partial view, same as today.
        """
        if feed is None or not cfg_bool("intraday_live_bars_enabled", True):
            return 0
        from intraday.strategies.base import SymbolContext

        min_bars = cfg_int("intraday_min_live_bars_for_context", 5)
        touched = 0
        bench_only_new = 0

        for entry in (self._bench or []):
            sym = entry.symbol
            live = feed.bars(sym)
            if not live:
                continue

            ctx = self._contexts.get(sym)
            if ctx is not None:
                existing_ts = {b.ts for b in ctx.bars}
                new = [b for b in live if b.ts not in existing_ts]
                if new:
                    ctx.bars = sorted(ctx.bars + new, key=lambda b: b.ts)
                    touched += 1
                continue

            # Bench-only: refresh_contexts() never built this symbol a
            # context (it is outside the top intraday_max_universe today).
            if len(live) < min_bars:
                continue
            ref = self._daily_ref.get(sym) or {}
            ltp = feed.get(sym) or live[-1].close
            tv = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in live)
            vol = sum(b.volume for b in live)
            vwap = ((tv / vol) if vol else
                    sum((b.high + b.low + b.close) / 3.0 for b in live) / len(live))
            self._contexts[sym] = SymbolContext(
                symbol=sym, ltp=float(ltp), bars=live, vwap=vwap,
                day_open=live[0].open,
                day_high=max(b.high for b in live),
                day_low=min(b.low for b in live),
                prev_close=float(ref.get("close") or 0) or None,
                prev_high=float(ref.get("high") or 0) or None,
                prev_low=float(ref.get("low") or 0) or None,
                atr_pct_daily=float(ref.get("atr_pct") or 0) or None,
                avg_volume_20d=float(ref.get("volume") or 0) or None,
                value_cr=float(ref.get("value_cr") or 0) or None,
                sector=ref.get("sector") or "",
                as_of=datetime.now(IST),
                live_fields=("bars",),
            )
            touched += 1
            bench_only_new += 1

        # THROTTLED, NOT PER-CYCLE — this function runs every 15s and a line
        # every cycle would be noise. Logged UNCONDITIONALLY at the interval
        # (including when the count is 0) rather than only when something
        # happened: a check that only speaks when it has good news to report
        # is not distinguishable from a check that stopped running at all.
        now = datetime.now(IST)
        interval = cfg_float("intraday_context_refresh_s", 300.0)
        if (now - self._last_bars_log_at).total_seconds() >= interval:
            self._last_bars_log_at = now
            bench_only_live = sum(
                1 for c in self._contexts.values()
                if c.live_fields == ("bars",))
            logger.info(
                f"  live bars: {bench_only_live} bench-only context(s) built "
                f"from ticks alone (beyond the {cfg_int('intraday_max_universe', 40)} "
                f"historical_data names), {bench_only_new} new this cycle")
        return touched

    def refresh_trend_context(self) -> int:
        """
        Live evidence for assess_trend()'s runner/deterioration checks on
        every held SWING position — refreshed on the slow timer, not once a
        day.

        UNTIL NOW. policy["_trend_ctx"] was populated only inside
        manage_open_positions(), which per its own comment is "only ever
        written once a day... at EOD" (see engine.py's apply_live_quotes
        docstring, which already pointed at that line). This class's live
        15s loop calls evaluate_exit() every cycle, all session, with a
        policy dict that never carried this key — grep confirms zero other
        references to `_trend_ctx` anywhere in this file before this method.
        So target_decision()'s runner conversion and deterioration_check()'s
        "thesis broke while in profit" exit were both blind for the entire
        trading day, every day.

        NEITHER FIRED INCORRECTLY. TrendQuality.has_evidence requires
        checks >= exit_runner_min_checks (default 3); zero live evidence
        means has_evidence is False, and target_decision() explicitly banks
        rather than runs when that is true ("no evidence is a reason to
        bank, not a reason to run"). So the gap was silent and safe, not
        dangerous — but it meant a position hitting 3R at 11am was ALWAYS
        banked, never run, no matter how strong the actual trend was,
        because the evidence to say otherwise only ever existed
        retroactively, after the position had usually already been closed.

        Reuses load_signal_context() verbatim — the same pure fetch
        manage_open_positions() already trusts — rather than a second
        implementation that could quietly disagree with it. Also attaches
        dist_vwap_live from the live tick VWAP already sitting in
        self._contexts (apply_live_quotes(), every 15s) — the one vote in
        assess_trend()'s list that is genuinely live rather than a
        faster-refreshed snapshot of last night's numbers.

        Behind its own switch, DEFAULT OFF. This changes which live SWING
        exits fire — a position can now legitimately convert to a runner,
        or exit early on deterioration, during market hours, neither of
        which could happen before. That is a money-moving behaviour change
        on the LIVE book, so it waits for an explicit flip like everything
        else in this class, not a default-on "fix".
        """
        if not cfg_bool("exit_live_trend_ctx", False):
            return 0

        symbols = sorted({p["symbol"] for p in self.positions
                          if (p.get("framework") or "SWING").upper() == "SWING"
                          and p.get("symbol")})
        if not symbols:
            self._trend_ctx = {}
            if self._policy is not None:
                self._policy["_trend_ctx"] = self._trend_ctx
            return 0

        try:
            from control.exit_rules import load_signal_context
            ctx = load_signal_context(self.sb, symbols)
        except Exception as e:
            logger.warning(f"  trend context unavailable ({e}) — runners and "
                           f"deterioration exits abstain this cycle")
            ctx = {}

        # The one live vote: today's price against today's session VWAP,
        # already flowing through self._contexts every 15s (apply_live_quotes).
        # Written onto the SAME dict assess_trend() reads, matching the
        # pattern load_signal_context() already uses for _structure_highs/lows.
        for sym, row in ctx.items():
            c = self._contexts.get(sym)
            if c and c.ltp and getattr(c, "vwap", None):
                row["dist_vwap_live"] = round((c.ltp - c.vwap) / c.vwap * 100.0, 3)

        self._trend_ctx = ctx
        if self._policy is not None:
            self._policy["_trend_ctx"] = ctx

        missing = [s for s in symbols if s not in ctx]
        if missing:
            logger.warning(f"  no live trend context for {len(missing)}/{len(symbols)} "
                           f"position(s): {', '.join(missing)} — runner/deterioration "
                           f"checks abstain for these this cycle")
        return len(ctx)

    def requeue_runway_refused_shorts(self) -> list[str]:
        """
        Once per session, in the OPENING phase (09:15-09:30, when a fresh
        short obviously clears the 75-minute runway requirement): pull
        yesterday's runway-refused shorts and re-seed the ones that gapped
        down again into TODAY's universe.

        RE-SEEDS THE UNIVERSE, NOT THE ORDER. This only adds symbols back to
        self._universe so evaluate_intraday_setups() looks at them again
        THROUGH THE FULL PIPELINE — structure, cost, AI, allocator, and
        can_short() itself, all unchanged. No entry path here bypasses the
        allocator (FR5, docs/1_PHASE4_ARCHITECTURE.md) — this only decides
        what gets LOOKED at again, never what gets taken.

        GAP-DOWN CONTINUATION ONLY — see _gap_continuation_shorts(). A
        refused-then-quiet name is not re-seeded; only one where the tape
        itself kept confirming the thesis overnight.

        Behind its own switch, DEFAULT OFF — unmeasured against this book's
        own outcomes yet, ships inert until an operator arms it, same
        posture as every other new consequential piece this session.
        """
        if not cfg_bool("intraday_requeue_runway_refused", False):
            return []
        from intraday.session import phase_at, OPENING
        from config import today_ist
        now = datetime.now(IST)
        if phase_at(now) != OPENING:
            return []
        today = today_ist().isoformat()
        if self._last_state.get("_runway_requeue_date") == today:
            return []  # already run once this session
        self._last_state["_runway_requeue_date"] = today

        yesterday = (today_ist() - timedelta(days=1)).isoformat()
        try:
            # paging-exempt: ONE day AND cost_verdict=BLOCKED_SHORTABILITY —
            # 15 rows for 14-Aug-2026, against 2289 detections that session.
            # The day filter alone would NOT be enough on this table.
            rows = (self.sb.table("intraday_setups").select("symbol,meta")
                      .eq("trade_date", yesterday)
                      .eq("cost_verdict", "BLOCKED_SHORTABILITY")
                      .execute().data or [])
        except Exception as e:
            logger.warning(f"  runway requeue: could not read yesterday's refusals ({e})")
            return []

        runway_rows = _parse_runway_refusals(rows)
        if not runway_rows:
            return []

        symbols = sorted({r["symbol"] for r in runway_rows if r.get("symbol")})
        try:
            y_rows = (self.sb.table("stock_data_daily").select("symbol,close")
                        .eq("date", yesterday).in_("symbol", symbols)
                        .execute().data or [])
            y_closes = {r["symbol"]: float(r["close"]) for r in y_rows if r.get("close")}
        except Exception as e:
            logger.warning(f"  runway requeue: could not read yesterday's closes ({e})")
            return []

        today_opens = {sym: c.day_open for sym, c in (self._contexts or {}).items()
                       if c.day_open}
        matched = _gap_continuation_shorts(runway_rows, y_closes, today_opens)
        if matched:
            self._universe = sorted(set(self._universe) | set(matched))
            logger.info(
                f"  runway requeue: {len(matched)} of {len(symbols)} yesterday's "
                f"runway-refused short(s) gapped down again at today's open — "
                f"back in today's universe, ample runway now: {', '.join(matched)}")
        elif symbols:
            logger.info(
                f"  runway requeue: {len(symbols)} runway-refused short(s) from "
                f"yesterday, none gapped down again — not re-seeded")
        return matched

    def _refresh_index_ltp(self, prices: dict[str, float]) -> None:
        """
        Freshen self._index_ctx.ltp from the live tick, unconditionally.

        WHY THIS EXISTS
        ----------------
        market_context.classify() gates the ENTIRE intraday book on the
        index's state, and until this was added the index was not on
        watch_symbols() at all — it never subscribed to the websocket, so it
        never ticked. Its price came only from the one-off REST call inside
        refresh_contexts(), the 300-second slow timer. Every fast-loop
        decision in between read a snapshot that could be up to five minutes
        old, silently — the same class of staleness the per-symbol
        stale_contexts() guard exists to catch, except this one path was
        exempt from that guard too, because self._index_ctx lives outside
        self._contexts, which is the only dict that guard inspects.

        UNCONDITIONAL, NOT GATED ON intraday_quote_mode. Every ordinary
        symbol's ltp is set the same way, unconditionally, in this function's
        per-symbol loop below — quote mode only gates the RANGE/VOLUME/VWAP
        overlay, never the price itself. The index should be held to the same
        standard: a live price does not require an opt-in switch.

        Mutates self._index_ctx in place rather than replacing it, so the
        existing prev_close/day_high/day_low/sector fields survive untouched
        and every OTHER reader of self._index_ctx this cycle sees the update.
        """
        if self._index_ctx is None:
            return
        from intraday.market_context import INDEX_SYMBOL
        px = prices.get(INDEX_SYMBOL)
        if px:
            self._index_ctx.ltp = float(px)

    def stale_contexts(self, max_age_s: float | None = None) -> dict[str, float | None]:
        """
        Which contexts are too old to decide on, and by how much.

        A dead socket with a live cache is the failure this answers. Unknown age
        counts as stale — "we do not know when this was true" is not a reason to
        proceed.
        """
        limit = (max_age_s if max_age_s is not None
                 else cfg_float("intraday_context_max_age_s", 420.0))
        now = datetime.now(IST)
        return {s: c.age_seconds(now) for s, c in (self._contexts or {}).items()
                if c.is_stale(limit, now)}

    def watch_symbols(self) -> list[str]:
        """
        Everything the TICK FEED must carry: open positions, every live swing
        candidate, and the intraday universe.

        Positions are non-negotiable — an unwatched position is an unmanaged
        stop. The other two are opportunity.

        This is deliberately WIDER than context_symbols(). A websocket
        subscription is nearly free (Kite allows 3000 instruments); a bar fetch
        is not. Conflating the two meant the expensive limit was applied to the
        cheap resource, and swing plans were dropped from the price feed to
        protect a budget they never spent — a swing candidate is evaluated by
        decide(symbol, ltp), which needs a price and no bars at all.

        THE INDEX IS INCLUDED, DELIBERATELY, FOR THE SAME REASON.
        market_context.classify() gates every intraday entry on the index's
        state — RISK_OFF blocks the whole 40-name book at once — yet the index
        was never on this list, so it never ticked. Its price was refreshed
        only on the 300s slow timer, meaning the single gate the whole book
        depends on could be running on a snapshot up to five minutes stale,
        silently, on every 15s cycle in between. See _refresh_index_ltp().

        THE BENCH IS INCLUDED TOO, NOT JUST self._universe — 10-Aug-2026. A
        websocket subscription is nearly free (Kite allows 3000 instruments);
        without it, a bench name outside today's static top 40 would never
        tick at all, and rerank_universe_live() would have nothing live to
        promote it on. This is the one line that turns the daily universe
        from "yesterday's list, replayed" into something that can actually
        notice a name moving today.
        """
        from intraday.market_context import INDEX_SYMBOL
        return sorted({p["symbol"] for p in self.positions if p.get("symbol")} |
                      {c["symbol"] for c in self.candidates if c.get("symbol")} |
                      set(self._universe) |
                      {e.symbol for e in self._bench} | {INDEX_SYMBOL})

    def _log_swing_state(self, prices: dict) -> None:
        """One line per cycle: what swing is eligible for, and why not more."""
        from analysis.trade_decision import decide
        from analysis.entry_ranking import rank
        from config import capital_for
        from execution.gates import is_paper
        if not self.candidates:
            return
        mx = cfg_int("swing_max_new_per_day", 2)
        keep = max(1, mx * 2)
        field = rank(self.candidates)[:keep]
        elig = {r.symbol: r for r in field}

        buyable, ready = 0, []
        for c in self.candidates:
            sym = c.get("symbol")
            ltp = prices.get(sym)
            if not sym or not ltp:
                continue
            try:
                d = decide(c, float(ltp), total_capital=capital_for("SWING"),
                           open_positions=self._swing_positions(),
                           vol_mult=self._overlay_vol_mult)
            except Exception:
                continue
            if d.action not in ("BUY_NOW", "CHASE_LIMIT"):
                continue
            buyable += 1
            if sym in elig:
                ready.append(sym)

        try:
            from execution.order_manager import _today_totals
            taken, _m, _a = _today_totals(self.sb, "SWING")
        except Exception:
            taken = 0
        taken = max(taken, self._entries_taken)
        left = max(0, mx - taken)

        held = sum(1 for p in self.positions
                   if (p.get("framework") or "SWING").upper() == "SWING")
        logger.info(f"  swing: {held} held · {len(self.candidates)} watched · "
                    f"{buyable} buyable now · eligible today "
                    f"[{', '.join(elig) or 'none'}] · {taken}/{mx} entries used")
        if ready and left:
            logger.success(f"     ready to enter: {', '.join(ready)}")
        elif buyable and not ready and left:
            # The single most useful line, and it was invisible: plans ARE
            # buyable, they are simply not the ones the ranking prefers.
            logger.info(f"     {buyable} buyable but none in today's top {keep} — "
                        f"waiting for a ranked name rather than taking the first")
        elif not left:
            # "swing:" prefixed explicitly -- this line prints right above
            # _log_intraday_state's own summary every cycle, and intraday has
            # its own, completely independent budget check (see
            # _maybe_enter_intraday's "intraday daily entry budget spent").
            # Unlabelled, the two looked like one shared gate from the console.
            logger.info(f"     swing: daily entry budget spent ({taken}/{mx})")

    def _log_intraday_state(self, setups: list) -> None:
        """
        One line per cycle: what intraday scanned and found — mirroring
        _log_swing_state's own reason for existing.

        07-Aug-2026: that function's docstring already records the ORIGINAL
        shape of this problem — "the console showed the intraday engine scan
        in detail and swing not at all" — and was added to fix it. Nobody
        added the mirror once swing had its own heartbeat: intraday only
        logs when a detection clears every gate (SETUP_*, BLOCKED_*), so a
        quiet 15s cycle with nothing to report produces no line at all,
        while swing's unconditional summary keeps printing every cycle
        regardless. From outside, that reads as "only swing is running" —
        confirmed live: several minutes of consecutive cycles with the swing
        line repeating and nothing intraday-shaped between them, on a
        session that was in fact scanning normally the whole time
        (evaluate_intraday_setups runs unconditionally in cycle(), same as
        evaluate_candidates). The absence of a line was never evidence of
        anything; there was simply no line for "scanned, found nothing" to
        be.
        """
        held = sum(1 for p in self.positions
                   if (p.get("framework") or "SWING").upper() == "INTRADAY")
        scanned = len(self._contexts or {})
        names = [s["setup"].symbol for s in (setups or [])]
        logger.info(f"  intraday: {held} held · {scanned} scanned · "
                    f"{len(setups or [])} setup(s) this cycle "
                    f"[{', '.join(names) or 'none'}]")

    def _failed_today(self) -> set:
        """
        Intraday names that already lost money today.

        WHY THIS EXISTS
        ---------------
        Nothing stopped the engine re-entering a name it had just been stopped
        out of. `_held_by_framework` only asks whether a symbol is held RIGHT
        NOW, and the capacity gate counts only positions currently open — so the
        moment a loser closed, its slot reopened and the same symbol was free to
        come straight back through the same engine on the same thesis.

        On 31 Jul that is exactly what happened. ACMESOLAR was stopped out for
        -1.35R and re-bought two minutes later at the same 0.57 conviction; ZEEL
        was invalidated and re-entered. Seven entries were taken against a cap of
        five because re-entries did not count against anything.

        A level that has already failed once today is not the same setup at a
        discount — it is a level with a demonstrated seller at it, and the second
        attempt is the one professionals stand down from. The cost is real and
        one-sided: a genuine second breakout is missed, which costs opportunity;
        a revenge re-entry is taken, which costs money.

        Cached for the session because closed_positions only grows and the
        answer cannot go backwards.
        """
        today = str(today_ist())
        if getattr(self, "_failed_day", None) != today:
            self._failed_day, self._failed_set = today, set()
        try:
            rows = (self.sb.table("closed_positions")
                    .select("symbol,r_multiple,realized_pnl")
                    .eq("framework", "INTRADAY").eq("entry_date", today)
                    .execute().data or [])
            self._failed_set = {r["symbol"] for r in rows
                                if float(r.get("r_multiple") or r.get("realized_pnl") or 0) < 0}
        except Exception as e:
            # Keep whatever is already known rather than returning an empty set:
            # forgetting a loser is the failure mode this guard exists to stop.
            logger.debug(f"  failed-today lookup failed, keeping cached set: {e}")
        return self._failed_set

    def _held_by_framework(self, symbol: str, framework: str) -> bool:
        """
        Does THIS framework already hold this symbol, under any product?

        The product key lets swing and intraday both hold a name. It must not
        let ONE framework hold it twice. That is not hypothetical: changing
        intraday_product from CNC to MIS leaves yesterday's CNC intraday
        positions invisible to a product-keyed guard, so the engine would open a
        second MIS tranche in a name it already holds — two intraday positions,
        two stops, one thesis, and a square-off that only knows about one.

        Cross-framework is the opportunity. Within a framework it is a bug.
        """
        fw = (framework or "INTRADAY").upper()
        return any((x.get("symbol") == symbol
                    and (x.get("framework") or "SWING").upper() == fw)
                   for x in self.positions)

    def _stock_row(self, symbol: str) -> dict:
        """
        The daily fundamentals row the shortability filter reads.

        Cached per session and loaded ONCE for the whole universe rather than
        per symbol per cycle: the filter runs inside the 15-second loop, and a
        per-name query there would put ~40 round trips in front of exit
        evaluation on live positions.

        The date is chosen the way `scanner._latest_date` chooses it — by
        whether the traded-value column is actually populated, not by max(date).
        A pipeline run dated today before the bhavcopy publishes writes a row
        with value_cr, delivery_pct and delivery_qty null, and reading a null
        traded value as ZERO is what emptied the universe on 05-Aug. Missing data
        and a failed test must not produce the same answer, so an unavailable row
        returns {} and the filter's checks that depend on it are SKIPPED rather
        than failed — the ones that matter most (runway, upper-band proxy) read
        the live context and still run.
        """
        today = today_ist().isoformat()
        if getattr(self, "_stock_rows_day", None) != today:
            self._stock_rows_day, self._stock_rows = today, {}
            try:
                from intraday import scanner
                ref = scanner._latest_date(self.sb)
                if ref:
                    rows = (self.sb.table("stock_data_daily")
                            .select("symbol,value_cr,delivery_pct,asm_flag,fo_ban_flag")
                            .eq("date", ref).execute().data) or []
                    self._stock_rows = {r["symbol"]: r for r in rows}
                    logger.debug(f"  shortability: {len(self._stock_rows)} rows from {ref}")
            except Exception as e:
                # Loud, because every check that reads this row is silently
                # skipped when it is empty — and a short filter that skips its
                # squeeze and liquidity checks is not the filter it claims to be.
                logger.warning(f"  shortability: daily rows unavailable ({e}) — the "
                               f"squeeze and liquidity checks will be SKIPPED. "
                               f"Runway and the band proxy still apply.")
        return self._stock_rows.get(symbol, {})

    def _other_framework_holding(self, symbol: str, framework: str) -> dict | None:
        """
        Is the OTHER book already in this name? Returns the position, or None.

        ONE SYMBOL, ONE BOOK — AND IT IS NOW ENFORCED IN BOTH DIRECTIONS.

        The rule was written down twice and implemented once. The intraday side
        skipped any symbol appearing in `self.positions`, which blocks a name the
        swing book holds. The swing side called `_held_by_framework(sym,
        "SWING")`, which does not look at the intraday book at all — and its own
        comment block opens with "checked across BOTH frameworks, not just
        swing" and closes by saying an intraday MIS tranche "does not block
        this". Those two sentences cannot both be true, and the code implemented
        the second one.

        The asymmetry ran in the dangerous direction. Intraday, which is PAPER,
        refused to touch a name swing held. Swing, which is REAL MONEY, would
        buy a name the paper book was already trading. So the collision could
        only ever be created by the live book, and only ever on top of a
        simulated position — which is also the case nothing reconciles, because
        one row is PAPER and one is LIVE and neither square-off knows about the
        other.

        WHY ONE BOOK PER NAME IS THE RIGHT POLICY HERE, not merely the tidy one:

          · The exit ladders contradict each other. `exit_policy` flattens MIS
            by 15:15; `position_lifecycle` runs a multi-week thesis with a
            15-session time stop. Held together, the intraday square-off sells
            into the swing plan's morning and the swing trail sits under an
            intraday stop that has already fired.
          · At Rs 20,000 it is concentration, not diversification. An intraday
            setup is sized at up to 25% of capital and a swing plan takes its
            own tranche; the same name in both books is a third of the account
            on one thesis, and neither book's sizing knows about the other.
          · The learning loop cannot attribute the result. `signal_log` scores
            the swing signal, `intraday_setups` scores the detection, and one
            price series produced both — so the same move is counted twice, once
            in each book's expectancy.

        Cross-framework was originally called "the opportunity" migration 028
        unlocked. Migration 028 unlocked the KEY — two rows can coexist without
        overwriting each other, which is what makes reconcile correct. That is a
        storage guarantee, not a trading policy, and it was read as permission.

        Switchable, because it is a policy and not a safety invariant, and the
        operator is the one who trades the account.
        """
        if not cfg_bool("one_framework_per_symbol", True):
            return None
        fw = (framework or "INTRADAY").upper()
        for x in self.positions:
            if x.get("symbol") != symbol:
                continue
            other = (x.get("framework") or "SWING").upper()
            if other and other != fw:
                return x
        return None

    def _held(self, symbol: str, product: str) -> bool:
        """
        Is this (symbol, product) already held?

        Keyed the same way open_positions is, so the guard and the constraint
        can never disagree. Asking by symbol alone was correct while one row per
        symbol was the rule; now it would refuse a legitimate intraday MIS
        tranche because a swing CNC core exists — the exact opportunity
        migration 028 was written to unlock.
        """
        p = (product or "CNC").upper()
        return any((x.get("symbol") == symbol
                    and (x.get("product") or "CNC").upper() == p)
                   for x in self.positions)

    def _confidence_floor(self) -> float:
        """
        How convinced the engine must be, given what is left to spend today.

        base at a full budget, rising linearly to `scarce` at zero remaining.
        Reading the count from the broker log rather than tracking it in memory
        keeps it correct across a restart and across the two daemons, only one
        of which is acting at a time.
        """
        base   = cfg_float("intraday_min_confidence", 0.55)
        scarce = cfg_float("intraday_min_confidence_scarce", 0.80)
        used = min(1.0, self._entries_today() / max(1, self._entry_cap()))
        return round(base + (scarce - base) * used, 3)

    def _entry_cap(self) -> int:
        """
        The budget the floor above is measuring consumption OF.

        This read `intraday_max_orders_per_day` (20) — the BROKER's daily order
        cap, a risk limit on how many orders may be sent. The budget entries
        are actually drawn from is `intraday_max_new_per_day` (10), checked in
        _maybe_enter_intraday. Two different numbers for two different things,
        and the floor was scaling against the wrong one, so it rose at exactly
        half the intended rate: on 12-Aug-2026 the tenth and final entry of the
        day left the floor reading 0.675 — "half the budget spent" — when the
        budget was in fact fully spent and the floor should have been at
        `intraday_min_confidence_scarce` (0.80) since the entry before.

        The cost was not theoretical. The floor stopped at 0.675 and stayed
        there; SDN's modal confidence on a down tape is 0.66 (0.62 base + 0.04
        for negative RS), so every short the session offered after 09:38 was
        refused by fifteen thousandths of a point, by a bar that was itself set
        by ten losing longs taken in the first six minutes.
        """
        try:
            return max(1, cfg_int("intraday_max_new_per_day", 4))
        except Exception:
            return 4

    def _entries_today(self) -> int:
        """
        Intraday positions ENTERED today, open or already closed.

        This used to read intraday_broker_log via _today_totals, which only LIVE
        orders write to. Intraday runs in PAPER, so the count was 0 on every
        cycle of every session and the rising floor never rose — it sat at the
        0.55 base from 09:15 to 15:15. That is why a 0.57 VWR was entered at
        13:40 on 31 Jul with most of the day's budget already spent, while a
        0.97 GAP at 10:43 got nothing.

        Counting POSITIONS rather than orders is also the more honest measure of
        "budget spent": an order that never became a position consumed nothing,
        and a closed position still used its slot. It reads the same in paper and
        live, which is the point — a simulation whose gates behave differently
        from the live ones is measuring a system nobody will run.
        """
        try:
            # str(): today_ist() returns a date, and comparing a date to the
            # sliced string form of entry_date is silently always False — the
            # same zero-by-accident this function exists to fix.
            today = str(today_ist())
            n = len([p for p in self.positions
                     if (p.get("framework") or "").upper() == "INTRADAY"
                     and str(p.get("entry_date") or "")[:10] == today])
            closed = (self.sb.table("closed_positions")
                      .select("id", count="exact")
                      .eq("framework", "INTRADAY")
                      .eq("entry_date", today).execute())
            return n + int(closed.count or 0)
        except Exception as e:
            # Fail to the STRICT side. An unknown count previously meant used=0,
            # i.e. the lowest floor of the day — a query failure quietly made the
            # system take worse trades. Assume the budget is spent instead.
            logger.debug(f"  entries-today count failed, assuming budget spent: {e}")
            return self._entry_cap()

    def _swing_positions(self) -> list[dict]:
        """
        Only this book's own holdings. check_new_entry()'s caps — position
        count, sector, industry, open-risk budget — are scoped to swing by
        the function's own docstring ("every caller today is the swing
        book"), but every call site here passed self.positions unfiltered:
        an intraday PAPER position was consuming a swing slot. 5 real swing
        holdings plus 2 intraday paper ones read as "7/7 slots used" and
        refused every swing candidate today, regardless of how many swing
        slots were actually free.
        """
        return [p for p in self.positions if (p.get("framework") or "SWING").upper() == "SWING"]

    def context_symbols(self) -> list[str]:
        """
        What needs BARS, which is the genuinely expensive list.

        One historical_data call per symbol per refresh, on an endpoint rate
        limited far more tightly than quotes. Only two things need bars: the
        intraday engines, which compute ranges and VWAP from them, and open
        positions, whose exit rules read the high-water mark.

        Swing candidates are excluded because nothing about them consumes bars.
        """
        return sorted({p["symbol"] for p in self.positions if p.get("symbol")} |
                      set(self._universe))

    def refresh_advisory(self) -> None:
        """
        Refresh event data and AI advice. SLOW timer only.

        Both are advisory: if either fails the engines carry on with mechanics
        alone, which is the correct degradation because they were never
        dependent on it.
        """
        try:
            from intraday.news_gate import NewsGate
            if self._news is None:
                self._news = NewsGate(self.sb)
            self._news.refresh(self.watch_symbols())
        except Exception as e:
            logger.warning(f"  news gate refresh failed: {e}")

        # Structural overlays — expiry-day sizedown x VIX-regime exposure.
        # Computed ONCE here rather than inside decide()/evaluate_intraday_
        # setups(), which run per-candidate every 15s: vol_exposure() and
        # expiry_size_multiplier() each read Supabase, and up to ~60 swing
        # candidates x ~40 intraday names every cycle would turn one book-level
        # read into a hundred.
        try:
            from analysis.overlays import expiry_size_multiplier, vol_exposure
            exp_mult = expiry_size_multiplier()
            vol_mult, vol_why = vol_exposure()
            # exp_mult is INTRADAY-ONLY — see the __init__ comment on these
            # two attributes for why. vol_mult alone is shared with swing.
            self._overlay_intraday_mult = exp_mult * vol_mult
            self._overlay_vol_mult = vol_mult
            note = f"expiry x{exp_mult:.2f}, {vol_why}"
            if note != self._overlay_note:
                if vol_mult < 1.0 or exp_mult < 1.0:
                    logger.info(f"  overlays: {note} -> intraday x{self._overlay_intraday_mult:.2f}, "
                                f"swing x{vol_mult:.2f}")
                self._overlay_note = note
        except Exception as e:
            # Fail OPEN to no scaling, same as the functions' own internal
            # fallback — an overlay that cannot read its input is not a
            # reason to stop trading, only a reason to stop shrinking.
            logger.warning(f"  structural overlay refresh failed: {e}")
            self._overlay_intraday_mult = 1.0
            self._overlay_vol_mult = 1.0

        # The AI reviews setups ALREADY DETECTED since the last refresh, so its
        # latency is absorbed between slow ticks rather than blocking a decision.
        if self._pending_review:
            try:
                from intraday import ai_advisor, market_context as mkt
                mc = mkt.from_context(self._index_ctx)
                self._advice = ai_advisor.review(
                    self._pending_review, mc.state,
                    [p.get("symbol") for p in self.positions], self.sb)
            except Exception as e:
                logger.warning(f"  intraday AI review failed: {e}")
            finally:
                self._pending_review = []

    def refresh_universe(self) -> int:
        """
        Rebuild the intraday universe and its live-rerank bench. Daily input
        (stock_data_daily), so the underlying scan is on the slow timer —
        see scanner.universe() for why that is cheap even called every 300s.

        self._universe itself is then re-ranked continuously between slow
        ticks by rerank_universe_live(), called every 15s from cycle(). This
        call re-seeds it from the static score whenever the bench itself
        changes (a new date, or the first call of the session) — the correct
        starting point before any live tick has arrived to say otherwise.
        """
        if not cfg_bool("intraday_strategies_enabled", True):
            self._universe = []
            self._bench = []
            return 0
        try:
            from intraday import scanner
            self._bench = scanner.universe(self.sb)
            limit = cfg_int("intraday_max_universe", 40)
            self._universe = [e.symbol for e in self._bench[:limit]]
        except Exception as e:
            logger.warning(f"  universe refresh failed: {e}")
            self._universe = []
            self._bench = []
        return len(self._universe)

    def rerank_universe_live(self, feed) -> None:
        """
        Re-rank self._universe from the bench using LIVE relative volume.
        Called every 15s from cycle() — cheap, since scanner.live_rerank() is
        pure arithmetic over ticks the websocket is already carrying (no DB,
        no historical_data call), which is exactly why the ranking can run on
        this loop while the expensive part (bars, in refresh_contexts()) stays
        on the 300s timer. See scanner.py's "LIVE RE-RANK" module docstring
        section for the full reasoning.

        A symbol promoted in here has no bars until the next slow tick picks
        it up via context_symbols() — a bounded, deliberate lag, not a bug;
        widening watch_symbols() to the bench is what let it start ticking at
        all rather than being invisible for the rest of the session.
        """
        if not self._bench or feed is None:
            return
        try:
            from intraday import scanner
            from intraday.config import minutes_since_open
            quotes = {e.symbol: feed.quote(e.symbol) for e in self._bench}
            limit = cfg_int("intraday_max_universe", 40)
            self._universe = scanner.live_rerank(
                self._bench, quotes, minutes_since_open(), limit)
        except Exception as e:
            logger.debug(f"  live universe re-rank skipped: {e}")

    # ── evaluation ──────────────────────────────────────────────────────────
    def evaluate_positions(self, prices: dict[str, float]) -> list[dict]:
        """Run the shared exit state machine against live prices."""
        from control.position_lifecycle import evaluate_exit

        actions = []
        for p in self.positions:
            sym = p.get("symbol")
            ltp = prices.get(sym)
            if not sym or not ltp:
                continue

            held = 0
            if p.get("entry_date"):
                try:
                    d0 = datetime.fromisoformat(str(p["entry_date"])[:10]).date()
                    held = max(0, (datetime.now(IST).date() - d0).days)
                except Exception:
                    held = 0

            # An INTRADAY position must NOT be managed by the swing policy: a
            # 3R target overrides the setup's own 2R, and a 15-SESSION time stop
            # is meaningless on a trade that must be flat by 15:20. Route by
            # framework, not by which loop happens to be running.
            if (p.get("framework") or "SWING").upper() == "INTRADAY":
                from intraday.exit_policy import (evaluate_intraday_exit,
                                                  load_intraday_policy,
                                                  last_completed_close)
                if self._intraday_policy is None:
                    self._intraday_policy = load_intraday_policy()
                # The close of the last FINISHED bar, for the invalidation
                # check. None when this symbol has no context or too few bars
                # yet, which `_invalidated` reads as "not confirmed" rather
                # than falling back to the tick. Only consumed when
                # `intraday_invalidation_require_close` is on.
                _c = self._contexts.get(p["symbol"])
                d = evaluate_intraday_exit(
                    p, float(ltp), self._intraday_policy,
                    last_close=last_completed_close(getattr(_c, "bars", None) or []))
            else:
                d = evaluate_exit(p, float(ltp), held, self._policy)

            # LIVE METRICS ON EVERY CYCLE, INCLUDING WHEN NOTHING IS TO BE DONE.
            #
            # This loop only ever wrote to a position when it was ACTING on it —
            # a partial, an exit, a trail. So high_water_mark stayed at the entry
            # price for the life of the trade and max_favorable_excursion was
            # never written at all. close_position() copies those columns
            # through, which is why all fourteen closed intraday positions carry
            # NULL for both.
            #
            # The cost is that the intraday book cannot be measured the way the
            # swing book can. "Median capture ratio 3%" and "24 of 28 losers had
            # been in profit" — the two measurements that rebuilt the swing exit
            # ladder — are both computed from MFE, and neither can be asked of
            # intraday. A book that cannot be measured cannot be improved, and
            # this one is PAPER precisely so that it can be.
            #
            # HOLD is where most of the excursion happens, so writing only on
            # action would miss the peak by construction.
            self._track_excursion(p, float(ltp))

            if d["action"] == "HOLD":
                continue
            actions.append({"position": p, "decision": d, "ltp": float(ltp)})
        return actions

    def _track_excursion(self, p: dict, ltp: float) -> None:
        """Keep high_water_mark, MFE and MAE current. Cheap, and only on change."""
        try:
            entry = float(p.get("entry_price") or 0)
            if not entry or not ltp:
                return
            # Signed IN THE POSITION'S FAVOUR. `(ltp - entry)/entry` is the price
            # move, which for a short has the opposite sign to the P&L — so a
            # short in profit recorded a negative pnl_pct, its MFE (a max) only
            # ever captured its worst moment, and its MAE (a min) captured its
            # best. Every excursion statistic inverted, silently, and the
            # give-back rule that will eventually be calibrated on this data
            # would have been calibrated on it backwards.
            d = D.normalise(p.get("direction"))
            pct = D.gain_pct(entry, ltp, d)
            hwm = D.favourable_excursion(float(p.get("high_water_mark") or 0),
                                         ltp, entry, d)
            mfe = max(float(p.get("max_favorable_excursion") or 0.0), pct)
            mae = min(float(p.get("max_adverse_excursion") or 0.0), pct)

            # R-MULTIPLE — the dashboard's primary unit, and never written here.
            # control/position_lifecycle.py (SWING) writes r_multiple_current;
            # nothing on the intraday side ever did, so every intraday position
            # showed "Awaiting first price update" for its ENTIRE life, not
            # just its opening seconds. Found via SAPPHIRE: 17 minutes open,
            # already 0.1% off entry, still r_multiple_current=None.
            #
            # planned_stop, not active_sl, to match position_lifecycle's own
            # choice — R is measured against the ORIGINAL risk, not a trailed
            # one, or the number would compress every time the stop moves
            # rather than only when the price does.
            #
            # risk_per_share returns 0.0 for a stop on the wrong side of entry
            # (bad data or a position mid-repair) — that must read as unknown,
            # not as "exactly at breakeven", so None is written explicitly
            # rather than trusting gain_r's own 0.0 fallback for a zero risk.
            stop = float(p.get("planned_stop") or 0)
            risk = D.risk_per_share(entry, stop, d)
            r_now = round(D.gain_r(entry, ltp, risk, d), 3) if risk > 0 else None

            # RUPEE P&L — the frontend's primary displayed figure
            # (PositionsTab reads p.unrealized_pnl directly), and until now
            # only ever written once a day by manage_open_positions() at EOD.
            # This loop already refreshed pnl_pct live every cycle, so the
            # dashboard showed a live percentage next to a rupee figure frozen
            # since the previous evening's batch run — for BOTH books, since
            # this function serves swing and intraday positions alike. Signed
            # with D.sign so a SHORT's P&L moves the correct way; LONG is
            # unaffected (sign=+1).
            qty = int(p.get("current_qty") or p.get("actual_qty") or 0)
            unrealized_pnl = round(D.sign(d) * (ltp - entry) * qty, 2)
            current_value = round(ltp * qty, 2)

            # Only write when something actually moved, so a quiet position does
            # not produce a database round trip every 15 seconds per name. R is
            # part of that comparison, not just the excursion stats — a
            # position flat in raw percent terms can still have crossed an
            # R-based exit rung, which is the transition this dashboard exists
            # to surface.
            prev_r = p.get("r_multiple_current")
            r_moved = (r_now is None) != (prev_r is None) or (
                r_now is not None and prev_r is not None
                and abs(r_now - float(prev_r)) >= 0.01)
            prev_pnl = p.get("unrealized_pnl")
            pnl_moved = prev_pnl is None or abs(unrealized_pnl - float(prev_pnl)) >= 0.01
            if (not r_moved and not pnl_moved
                    and abs(hwm - float(p.get("high_water_mark") or 0)) < 0.005
                    and abs(mfe - float(p.get("max_favorable_excursion") or 0)) < 0.005
                    and abs(mae - float(p.get("max_adverse_excursion") or 0)) < 0.005):
                return

            upd = {"current_price": round(ltp, 2),
                   "current_value": current_value,
                   "unrealized_pnl": unrealized_pnl,
                   "high_water_mark": round(hwm, 2),
                   "max_favorable_excursion": round(mfe, 3),
                   "max_adverse_excursion": round(mae, 3),
                   "pnl_pct": round(pct, 3),
                   "r_multiple_current": r_now}
            self._update_position(p, upd)
            p.update(upd)
        except Exception as e:
            logger.debug(f"  {p.get('symbol')}: excursion tracking skipped — {e}")

    def _update_position(self, p: dict, upd: dict) -> None:
        """
        Update ONE position row, keyed on (symbol, product).

        open_positions is unique on the pair since migration 028. Matching on
        symbol alone writes this tranche's price, stop and excursion onto the
        other one when the same name is held CNC by swing and MIS by intraday —
        the exact corruption the composite key exists to prevent, and the reason
        DESIGN_NOTES sequences the two-book capability behind clean single-book
        operation.
        """
        q = self.sb.table("open_positions").update(upd).eq("symbol", p["symbol"])
        if p.get("product"):
            q = q.eq("product", p["product"])
        q.eq("status", "ACTIVE").execute()

    def evaluate_candidates(self, prices: dict[str, float]) -> list[dict]:
        """Run the shared entry decision against live prices."""
        from analysis.trade_decision import decide
        from config import capital_for

        regime = "NEUTRAL"
        if self.candidates:
            regime = self.candidates[0].get("regime") or "NEUTRAL"

        # Fetched ONCE per cycle, not per candidate: it is the same number for
        # every symbol and a REST call per name would be 40 calls every 15s.
        cash = None
        try:
            from kite import kite_client
            cash = float((kite_client.fetch_margins() or {}).get("available_cash") or 0) or None
        except Exception:
            cash = None

        out = []
        # Every decision this cycle, not just the buyable/approaching subset
        # `out` keeps — _allocate_shadow reads this to build the allocator's
        # reservation field, which needs the whole watch list (decide() already
        # prices and sizes every candidate; WAIT ones just weren't kept before).
        self._all_swing_decisions = []
        for c in self.candidates:
            sym = c.get("symbol")
            ltp = prices.get(sym)
            if not sym or not ltp:
                continue
            if any(p.get("symbol") == sym for p in self.positions):
                continue  # already held
            d = decide(c, float(ltp), total_capital=capital_for("SWING"),
                       open_positions=self._swing_positions(), regime=regime,
                       max_chase_pct=c.get("ai_max_chase_pct") or None,
                       available_cash=cash, vol_mult=self._overlay_vol_mult)
            self._all_swing_decisions.append({"candidate": c, "decision": d})
            if d.action in ("BUY_NOW", "CHASE_LIMIT"):
                out.append({"candidate": c, "decision": d, "ltp": float(ltp),
                            "state": "BUYABLE"})
                continue

            # THIS is what intraday data buys the swing strategy.
            #
            # The evening pipeline computes max_entry — the exact price at which
            # a setup's R:R becomes acceptable — from the CLOSE. On 24 Jul, 20 of
            # 43 candidates were priced just above their max_entry, several by
            # under 1%. Whether those trades ever became takeable is decided
            # entirely by intraday movement the evening run cannot see, and a
            # once-daily check answers the question ~18 hours late.
            #
            # Watching the approach rather than only the arrival is the point: an
            # alert fired AT max_entry leaves no time to place anything, while
            # one fired as price closes in lets a limit rest and be filled.
            if d.action == "WAIT" and d.max_entry and float(ltp) > d.max_entry:
                approach_pct = cfg_float("intraday_entry_approach_pct", 1.0)
                gap = (float(ltp) - d.max_entry) / d.max_entry * 100
                if gap <= approach_pct:
                    out.append({"candidate": c, "decision": d, "ltp": float(ltp),
                                "state": "APPROACHING", "gap_pct": gap})
        return out

    # ── acting ──────────────────────────────────────────────────────────────
    def act_on_positions(self, actions: list[dict]) -> None:
        for a in actions:
            p, d, ltp = a["position"], a["decision"], a["ltp"]
            sym = p["symbol"]
            urgency = "CRITICAL" if d["action"].startswith("EXIT") else "NORMAL"

            r_now = None
            try:
                entry = float(p.get("entry_price") or 0)
                stop0 = float(p.get("planned_stop") or 0)
                if entry and stop0 and stop0 < entry:
                    r_now = (ltp - entry) / (entry - stop0)
            except Exception:
                pass

            qty = int(p.get("current_qty") or p.get("actual_qty") or 0)
            book = int(d.get("book_qty") or 0)
            what = (f"Sell {book} of {qty}" if book else
                    f"Exit all {qty}" if d["action"].startswith("EXIT") else
                    "Adjust stop")

            self.notifier.send(Action(
                symbol=sym, kind=d["action"],
                headline=f"{what} — {d['detail']}",
                detail=(f"Entry ₹{p.get('entry_price')} · stop ₹{p.get('active_sl')} "
                        f"· reason {d['reason']}"),
                ltp=ltp, r_multiple=r_now, urgency=urgency,
                meta={"reason": d["reason"], "book_qty": book},
                # A swing position's exit is swing news even though the intraday
                # loop noticed it. Routed by the POSITION, not by the process.
                framework=(p.get("framework") or "SWING").upper(),
            ))

            # TRAIL_SL is a book update, not something you do — persist it so
            # the GTT sync has the new intended stop to push to the broker.
            if d["action"] == "TRAIL_SL" and d.get("new_sl"):
                try:
                    upd = {"active_sl": d["new_sl"],
                           "updated_at": datetime.now(IST).isoformat()}
                    # BREAKEVEN is not a trail. trail_activated flips the
                    # stop-breach label to TRAIL_SL_HIT, and recording a
                    # breakeven stop as a trailing one merges two different
                    # rules into one unreadable bucket.
                    if d.get("reason") == "BREAKEVEN":
                        upd["breakeven_moved"] = True
                    else:
                        upd["trail_activated"] = True
                    self._update_position(p, upd)
                    p.update(upd)
                except Exception as e:
                    logger.warning(f"  engine: trail persist failed for {sym} — {e}")

            # Gate on the POSITION's framework, not a fixed key. This read
            # cfg_bool("intraday_auto_exit") for every position, so a swing
            # exit was permitted or refused by the intraday switch — turning
            # intraday off would have silently frozen swing partial books on a
            # LIVE account, and swing_auto_exit did nothing here at all.
            from execution.gates import auto_exit_enabled
            fw = (p.get("framework") or "SWING").upper()
            if auto_exit_enabled(fw):
                self._auto_exit(p, d, ltp)

    def _auto_exit(self, p: dict, d: dict, ltp: float) -> None:
        """
        Phase 3 exits. Gated separately from entries by the FRAMEWORK's
        own auto-exit switch — swing_auto_exit or intraday_auto_exit.

        Exits are the safer half to automate — they reduce exposure, they act on
        a position that already exists, and the quantity is bounded by what is
        held. Entries commit new capital on a judgement call, so they stay
        manual longer.
        """
        from execution.order_manager import OrderRequest, place

        # TRAIL_SL AND HOLD ARE NOT EXITS. They must never reach an order.
        #
        # This function sold current_qty for ANY action with no book_qty, and
        # TRAIL_SL has no book_qty because it moves a stop rather than selling
        # anything. So a stop adjustment placed a market-priced SELL for the
        # whole position. GABRIEL was sold at 1420.00 on a TRAIL_SL that was only
        # asking to raise its stop to 1310.
        #
        # It never fired before only because the R-based trail needs 2R and no
        # position had reached it. Any position that ever did would have been
        # liquidated by its own trail — the bug was latent, not new.
        #
        # Whitelist rather than blacklist: an action not named here does not
        # place an order. A future action added to the exit policy should have to
        # opt IN to spending money.
        # EXIT_DETERIORATION belongs here and was missing. exit_rules.py
        # computes it, position_lifecycle routes it and alerts on it, and the
        # dashboard advertises it as a capability — but the daemon silently
        # dropped it, so the one exit designed to protect a PROFITABLE position
        # whose thesis broke was the one exit that needed a human to act. That
        # is the opposite of what it is for: it exists to beat the trail, and a
        # rule that only fires when you happen to be watching cannot.
        # EXIT_GIVEBACK and EXIT_STALL are here because position_lifecycle's own
        # order-placing branch already acts on them. Leaving them out made the
        # SAME decision execute or not depending on which process evaluated it —
        # the pipeline sold, the daemon alerted — and the daemon is the one
        # running every 15 seconds during market hours, so in practice the two
        # newest loss-cutting rules would almost never have fired.
        #
        # Divergent behaviour between two callers of one decision function is
        # the exact failure the "import the decision, never reimplement it"
        # rule exists to prevent; a whitelist that disagrees with the other
        # caller's whitelist reintroduces it by the back door.
        action = (d.get("action") or "").upper()
        if action not in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME",
                          "EXIT_INVALIDATED", "EXIT_SQUAREOFF",
                          "EXIT_DETERIORATION", "EXIT_GIVEBACK", "EXIT_STALL",
                          "BOOK_PARTIAL"):
            return

        qty = int(d.get("book_qty") or 0) or int(p.get("current_qty") or p.get("actual_qty") or 0)
        if qty <= 0:
            return
        # A partial with no book_qty would sell everything. Refuse instead.
        if action == "BOOK_PARTIAL" and int(d.get("book_qty") or 0) <= 0:
            logger.error(f"  {p.get('symbol')}: BOOK_PARTIAL with no book_qty — "
                         f"refusing rather than selling the whole position")
            return

        # Paper: there is no broker to notice the holding vanish, so the close
        # has to be written here. Full exits go through close_position so the
        # outcome record is identical to a live one; partials only shrink the
        # quantity, exactly as reconcile does for real fills.
        from execution.gates import is_paper
        fw = (p.get("framework") or "SWING").upper()
        if is_paper(fw) and (p.get("mode") or "").upper() == "PAPER":
            from execution import paper_broker
            if d["action"].startswith("EXIT"):
                paper_broker.close_position(p["symbol"], ltp, d["reason"],
                                            d["detail"], self.sb)
            else:
                left = int(p.get("current_qty") or p.get("actual_qty") or 0) - qty
                try:
                    upd = {
                        "current_qty": max(0, left),
                        # actual_qty/kite_qty ALSO SET HERE — 11-Aug-2026. This
                        # write used to touch current_qty alone. There is no
                        # broker for a paper fill to correct these two from
                        # afterward (reconcile skips PAPER rows by design), so
                        # leaving them stale here was PERMANENT, not a window —
                        # a paper PPLPHARMA-shaped position would carry a wrong
                        # kite_qty for the rest of its life. See the live path
                        # below for the fuller trace of why this matters.
                        "actual_qty": max(0, left),
                        "kite_qty": max(0, left),
                        "partial_booked_qty": int(p.get("partial_booked_qty") or 0) + qty,
                        "partial_booked_price": ltp,
                    }
                    # The partial carries the stop to breakeven with it. This was
                    # dropped on the PAPER path only, so a paper position booked
                    # half and kept its original stop while the live path moved
                    # it — the two books were running different exit policies on
                    # the same decision, which defeats the point of paper.
                    if d.get("new_sl"):
                        upd["active_sl"] = d["new_sl"]
                        upd["breakeven_moved"] = True
                    self._update_position(p, upd)
                except Exception as e:
                    logger.warning(f"  paper partial failed for {p['symbol']}: {e}")
            self.load_state()
            return
        # Marketable limit: priced through the bid so it fills like a market
        # order without accepting an unbounded price in a thin book.
        limit = round(ltp * (1 - cfg_int("intraday_exit_slip_bps", 30) / 10000.0), 1)
        # framework decides which caps preflight applies and how the order is
        # attributed in the broker log. Omitting it defaulted every exit —
        # including intraday ones — to the SWING rails.
        res = place(OrderRequest(p["symbol"], "SELL", qty, "LIMIT", limit,
                                 reason=f"{d['action']}: {d['detail']}"),
                    self.sb, self.notifier, framework=fw)

        # RECORD THE EXIT AGAINST THE POSITION, IMMEDIATELY.
        #
        # Without this the row still reads current_qty=15, partial_booked_qty=0
        # after a successful sale, so the very next cycle re-derives the SAME
        # BOOK_PARTIAL and places it again. The only thing standing between that
        # and a double sale was order_manager's 5-minute duplicate window — and
        # once it lapsed, PPLPHARMA sold 7 shares at 05:35 and another 7 at
        # 05:40, leaving 1 of 15 where a 50% book was intended. Real money, and
        # the runner half of the position gone.
        #
        # Written optimistically on PLACED rather than waiting for a confirmed
        # fill, because the failure modes are not symmetric: an unfilled order
        # recorded as booked is corrected by the next reconcile against broker
        # holdings, while a filled order NOT recorded sells the position twice.
        if res and res.ok:
            try:
                if d["action"].startswith("EXIT"):
                    upd = {"current_qty": 0, "status": "CLOSING"}
                else:
                    left = int(p.get("current_qty") or p.get("actual_qty") or 0) - qty
                    upd = {
                        "current_qty": max(0, left),
                        # actual_qty/kite_qty ALSO SET HERE, OPTIMISTICALLY,
                        # THE SAME WAY current_qty ALREADY IS — 11-Aug-2026.
                        #
                        # This write used to touch current_qty alone.
                        # PPLPHARMA booked 5 of 11 on 10-Aug: current_qty
                        # correctly went to 6, but actual_qty and kite_qty
                        # stayed at 11 — and STAYED WRONG, confirmed live a
                        # day later (broker holding: 6; DB actual_qty/
                        # kite_qty: still 11). The reason it never
                        # self-corrected is the reconcile "already matches"
                        # fast path (position_lifecycle.py): it compares
                        # current_qty against the broker, found them equal
                        # (6 == 6, since current_qty was ALREADY right), and
                        # — reasonably, given only one pair was ever checked
                        # — concluded there was nothing left to fix. It set
                        # reconcile_status=MATCHED without ever looking at
                        # actual_qty/kite_qty, which is exactly what let the
                        # dashboard's own mismatch banner (gated on
                        # reconcile_status != 'MATCHED') stay silent while
                        # kite_qty (11) and current_qty (6) visibly
                        # disagreed on the same row. Fixed at both ends: this
                        # write no longer lets the three fields diverge in
                        # the first place, and reconcile's fast path
                        # (position_lifecycle.py) now checks all three
                        # before declaring MATCHED, as a second line of
                        # defence for any other write path that repeats this
                        # mistake.
                        "actual_qty": max(0, left),
                        "kite_qty": max(0, left),
                        "partial_booked_qty": int(p.get("partial_booked_qty") or 0) + qty,
                        "partial_booked_price": limit,
                    }
                    if d.get("new_sl"):
                        upd["active_sl"] = d["new_sl"]
                upd["updated_at"] = datetime.now(IST).isoformat()
                self._update_position(p, upd)
                self.load_state()
            except Exception as e:
                # A recorded sale that cannot be persisted is the dangerous case,
                # so this is an error rather than a debug line.
                logger.error(f"  {p['symbol']}: SOLD {qty} but could not update the "
                             f"position row — {e}. It may be re-sold on the next "
                             f"cycle. Reconcile against the broker now.")

    def _swing_contenders(self) -> dict:
        """
        The only swing names worth interrupting you about today.

        WATCHING AND ALERTING ARE DIFFERENT QUESTIONS
        --------------------------------------------
        Sixty plans belong on the price feed: a tick costs nothing, and the last
        time this list was narrowed six of the day's eight actionable plans went
        unwatched. But sixty plans do NOT belong in your phone. With two entries
        a day, a plan ranked fortieth cannot be bought no matter what it does, so
        an alert about it is a message you can only ignore — and the reflex you
        build for the frequent message is the one you apply to the rare one.

        So the feed stays wide and the alerts narrow to the names actually in
        contention: the top N by composite rank. That is the swing lens the
        alerts were missing — they were reporting price events, while a swing
        decision is about which plan deserves capital over one to three weeks.

        Returns {symbol: (rank_position, Ranked)} for the contenders only.
        """
        try:
            from analysis.entry_ranking import rank
            top_n = cfg_int("swing_alert_top_n", 5)
            return {r.symbol: (i + 1, r)
                    for i, r in enumerate(rank(self.candidates)[:top_n])}
        except Exception as e:
            logger.debug(f"  contender ranking unavailable: {e}")
            return {}

    def _replacement_case(self, cand_rank) -> tuple[bool, str]:
        """
        Is this plan good enough to justify replacing something already held?

        The book is finite. When it is full, a new plan is not competing against
        cash, it is competing against the weakest position you own — and that is
        a comparison the system never made. It would alert BUY on a name it had
        no room for, leaving you to work out silently whether it beat what you
        already had.

        Only a MATERIAL margin counts. Churning one holding for another of
        similar conviction pays a round trip and a fresh stop for no edge, so the
        margin has to exceed what the swap costs before it is worth naming.
        """
        held = [p for p in self.positions
                if (p.get("framework") or "SWING").upper() == "SWING"]
        if not held:
            return False, ""
        weakest, weakest_rank = None, None
        for p in held:
            raw = p.get("entry_rationale") or ""
            try:
                # "rank 79 — screener 59 · ..." — the score it was chosen on.
                val = float(raw.split("rank", 1)[1].split("—")[0].strip())
            except Exception:
                continue
            if weakest_rank is None or val < weakest_rank:
                weakest, weakest_rank = p, val
        if weakest is None:
            return False, ""
        margin = cfg_float("swing_replace_margin", 20.0)
        if cand_rank.total >= weakest_rank + margin:
            return True, (f"ranks {cand_rank.total:.0f} against {weakest['symbol']} "
                          f"at {weakest_rank:.0f} — {cand_rank.total - weakest_rank:.0f} "
                          f"points better than the weakest thing you hold")
        return False, ""

    def act_on_candidates(self, entries: list[dict]) -> None:
        contenders = self._swing_contenders()
        for e in entries:
            c, d, ltp = e["candidate"], e["decision"], e["ltp"]
            approaching = e.get("state") == "APPROACHING"

            # Not in contention -> no alert. The entry path below still runs, so
            # nothing that could legitimately be bought is skipped; it simply
            # stops narrating names that cannot win the day's capital.
            sym = c.get("symbol")
            place = contenders.get(sym)
            if not place:
                self._maybe_enter_swing(c, d, ltp)
                continue
            pos, rk = place

            if approaching:
                # Distinct kind so the notifier's de-duplication treats
                # "closing in" and "now buyable" as separate states. Sharing a
                # kind would suppress the far more important second alert as a
                # restatement of the first.
                self.notifier.send(Action(
                    symbol=c["symbol"], kind="ENTRY_APPROACHING",
                    headline=(f"#{pos} today · {e['gap_pct']:.1f}% above the buy "
                              f"limit ₹{d.max_entry:.2f} — rest a limit now"),
                    detail=(f"Below ₹{d.max_entry:.2f} this is {d.min_rr_used:g}R:R or better "
                            f"(currently {d.rr_live:.2f}).\n"
                            f"Stop ₹{d.stop} · target ₹{d.target}"
                            + (f" · at the zone low it is {d.rr_at_zone_low:.2f}R:R"
                               if d.rr_at_zone_low else "")),
                    ltp=ltp, urgency="INFO",
                    meta={"tier": c.get("ai_tier"), "max_entry": d.max_entry,
                          "state": "APPROACHING", "rank": rk.total, "rank_pos": pos},
                    framework="SWING",   # a swing plan nearing its entry limit
                ))
                continue

            # Room for another position, or is this a swap? Two different
            # decisions, and the alert should not present them as one.
            from execution.order_manager import _today_totals
            try:
                n_today, _m, _a = _today_totals(self.sb, "SWING")
            except Exception:
                n_today = 0
            room = n_today < cfg_int("swing_max_new_per_day", 2)
            swap, swap_why = (False, "") if room else self._replacement_case(rk)
            if not room and not swap:
                # Cannot buy it and it does not beat what is held. Silence here
                # is the correct message: there is no decision to make.
                continue

            ai_bits = " · ".join(x for x in (
                str(c.get("ai_tier") or ""),
                (f"AI: {str(c.get('ai_conviction_reason'))[:90]}"
                 if c.get("ai_conviction_reason") else ""),
                (f"note: {str(c.get('ai_note'))[:90]}" if c.get("ai_note") else ""),
            ) if x)

            self.notifier.send(Action(
                symbol=c["symbol"],
                kind="ENTRY" if room else "SWAP_CANDIDATE",
                headline=(d.headline if room
                          else f"Better than what you hold — {d.headline}"),
                detail=(f"{d.reason}\n"
                        f"Stop ₹{d.stop} · target ₹{d.target}"
                        + (f" · {d.qty} sh ≈ ₹{d.invested:,.0f}, risk ₹{d.risk_amount:,.0f}"
                           if getattr(d, 'qty', None) else "")
                        + f"\n#{pos} of today's plans — {rk.why()}"
                        + (f"\n{swap_why}" if swap else "")
                        + (f"\n{ai_bits}" if ai_bits else "")),
                ltp=ltp, urgency="NORMAL",
                meta={"tier": c.get("ai_tier"), "action": d.action,
                      "rank": rk.total, "rank_pos": pos, "swap": swap},
                framework="SWING",       # from signal_output_daily, a swing plan
            ))
            self._maybe_enter_swing(c, d, ltp)

    def _maybe_enter_swing(self, c: dict, d, ltp: float) -> None:
        """
        Take a swing entry — paper or live — on a plan that is buyable NOW.

        WHY HERE AND NOT IN THE EVENING PIPELINE
        ----------------------------------------
        control/paper_entry.py runs after the close. That is fine for a
        simulated fill and impossible for a real one, because preflight requires
        an open market: a live order from the evening pipeline is rejected
        MARKET_CLOSED every time. Live entry has to happen while there is a
        market, on live prices, which means here.

        IT DOES NOT RE-DECIDE
        ---------------------
        decide() has already run and returned BUY_NOW or CHASE_LIMIT on the same
        data the alert above just reported. This acts on that decision; it does
        not form a second one. If the alert and the order ever disagree about a
        symbol, one of them is broken.

        THE RAILS, IN ORDER OF WHAT EACH PROTECTS
        -----------------------------------------
          swing_auto_entry        whether entries happen at all
          swing_live_auto_entry   whether they may spend real money
          composite rank          whether this is the best use of a scarce entry
          swing_max_new_per_day   how many the strategy wants
          max_entry               never chase past the price where the plan's
                                  own R:R holds — beyond it, it is not the trade
                                  the pipeline approved
          preflight               per-order cap, daily count, daily notional,
                                  the combined account guard, available cash,
                                  and the duplicate window

        Only the last reads broker truth rather than the book, which is why it
        is last and why nothing here tries to reason around it.
        """
        from execution.gates import is_paper
        if not cfg_bool("swing_auto_entry", False):
            return
        live = not is_paper("SWING")
        if live and not cfg_bool("swing_live_auto_entry", False):
            return

        sym = c.get("symbol")
        # ONE SYMBOL, ONE BOOK — AND THIS SIDE NOW ACTUALLY CHECKS BOTH.
        #
        # Same framework: a second swing entry in a name already held. This is
        # unconditional. open_positions is keyed on (symbol, product) since
        # migration 028, so a same-book re-entry UPSERTs over the first — entry
        # price, stop, target and the R baseline all replaced by whichever
        # tranche bought last.
        if not sym or self._held_by_framework(sym, "SWING"):
            return

        # A BUY was already submitted for this symbol and has not yet been
        # confirmed filled — do not submit a second one. _held_by_framework
        # cannot see this on its own: it reads self.positions, which is
        # loaded from status='ACTIVE' only, and an unconfirmed entry is
        # deliberately NOT active yet (see _resolve_pending_fills). Without
        # this separate guard, a limit order still sitting unfilled at the
        # next 15s cycle would look exactly like "not held" and get bought
        # again.
        if sym in self._pending_fills:
            return

        # Other framework: the intraday book is already in this name. The
        # comment that used to sit here claimed to check "across BOTH
        # frameworks, not just swing" and then said an intraday MIS tranche
        # "does not block this" — so the guard read as symmetric and was not.
        # Real money could be committed on top of a paper intraday position
        # while the paper book, which is the one that cannot lose anything,
        # refused to do the reverse. See _other_framework_holding for why one
        # book per name is the policy: two exit ladders that contradict each
        # other, a third of the account on one thesis, and a move that gets
        # counted twice in the learning loop.
        other = self._other_framework_holding(sym, "SWING")
        if other is not None:
            logger.info(
                f"  {sym}: the INTRADAY book holds this name "
                f"({other.get('mode') or 'PAPER'}, "
                f"{other.get('current_qty') or other.get('actual_qty')} "
                f"@ {other.get('entry_price')}) — standing down rather than "
                f"putting the swing book into the same thesis. One book per symbol")
            return

        # THE ALLOCATOR'S VETO — the swing side of the same gate that protects
        # intraday. This is the ONE choke point both act_on_candidates call
        # sites (line ~1084 and ~1147) funnel through, so the veto lives here
        # rather than being duplicated at each call site.
        #
        # A prior version of this migration wrote the veto directly into
        # act_on_candidates and it silently did not land — an edit that never
        # applied, caught only because nothing in this function referenced
        # alloc_live_swing when it should have. Placing it in the one function
        # that actually places an order is what makes that class of miss
        # impossible: there is no second path to a swing fill that could have
        # been missed.
        ok, why = self.allocator_permits(sym, "CNC", "SWING")
        if not ok:
            logger.info(f"  {sym}: allocator declined swing entry — {why[:90]}")
            return

        # Today's swing entries, counted from the BROKER LOG rather than from
        # this process, so a restart does not hand the day a fresh budget and
        # the laptop and the server cannot each take two.
        try:
            from execution.order_manager import _today_totals
            n_today, _mine, _all = _today_totals(self.sb, "SWING")
        except Exception:
            n_today = 0

        # Count THIS process's entries too, and take the larger.
        #
        # Reading the count from the broker log alone assumes the previous
        # insert is visible by the time the next candidate is evaluated. Three
        # entries fired three seconds apart on 30 Jul — CIPLA, ETERNAL, GABRIEL
        # — against a cap of two, because each read still saw the count from
        # before its predecessor landed. A cap enforced by a read-after-write is
        # not a cap; it is a race that usually goes your way.
        #
        # The in-process counter cannot miss its own writes. The database count
        # covers the restart case, where the counter is empty but the day's
        # entries are not. Neither is sufficient alone, so both are consulted.
        n_today = max(n_today, self._entries_taken)
        max_new = cfg_int("swing_max_new_per_day", 2)
        if n_today >= max_new:
            return

        # A RESERVE, NOT A GAP — 11-Aug-2026, replacing the gap-based
        # pacing shipped earlier the same day. That version blocked any
        # entry arriving within a fixed window of the LAST one, and on the
        # actual 10-Aug sequence it was built from, that was the wrong
        # rule: the allocator scored 8 proposals together at 09:36:23 ("3
        # to take, 5 refused") and picked AUBANK and SCI as two of that
        # day's three best trades IN THE SAME JOINT DECISION, two seconds
        # apart. A gap would have let AUBANK through and refused SCI for
        # 20 minutes for no reason — overriding a decision the allocator
        # had already made well, at zero benefit. What actually went wrong
        # was that all THREE of the day's slots were spent on a single
        # snapshot of the first ~21 minutes (itself mostly a symptom of
        # the Kite token being stale until then), leaving nothing for the
        # remaining ~5.5 hours. See order_manager.entry_reserved()'s own
        # docstring for the full trace.
        #
        # This caps how many of TODAY's entries may land before a cutoff
        # clock time — no restriction at all on how close together they
        # are otherwise, before OR after the cutoff. Two or three
        # candidates the allocator approves together in one pass are taken
        # together exactly as it decided; only a separate, EARLY entry
        # beyond the reserved count is refused.
        #
        # max_before=0 (or an unparseable cutoff) restores exactly
        # count-only behaviour, the rollback lever. The FIRST entry of the
        # day is never gated by this at cutoffs that leave room for it.
        from execution.order_manager import entry_reserved, parse_hhmm
        reserve_n = cfg_int("swing_max_entries_before_time", 1)
        cutoff = parse_hhmm(cfg("swing_entries_time_cutoff", "10:30"))
        blocked, why = entry_reserved(n_today, datetime.now(IST), reserve_n, cutoff)
        if blocked:
            logger.info(f"  {sym}: swing entry reserved — {why}")
            return

        # Rank against the whole day's field, not against arriving first —
        # UNLESS the allocator is the one holding the veto, in which case IT
        # is that ranking now, and gating on a second, disagreeing one is not
        # a second opinion, it is a second, unrelated question.
        #
        # This gate predates the allocator (04-Aug, vs the veto's 05-Aug) and
        # was never revisited when swing_assignment's reservation mechanism
        # went live (07-Aug, same review this comment is from). Confirmed
        # against real data the day it was wired: the allocator's edge-ranked
        # winners (HEG, ACE, CHALET — edge 0.052/0.052/0.053, comfortably
        # above a bar of 0.048) were NOT in entry_ranking's top-6 by
        # final_score/timing/sector that same day, and PIDILITIND — which WAS
        # in that top-6 — never had a high enough edge to win an allocator
        # slot either. Two rankings, over two different fields (edge is
        # engine-family + regime-bucket historical R; rank is per-stock
        # final_score/AI-tier/timing/sector), each internally coherent, with
        # nothing guaranteeing their winners overlap — so swing_max_new_
        # per_day=3 logged "3 to take" on nearly every cycle all day and
        # still finished at 0/3 used.
        #
        # `here` is still computed either way — act_on_candidates' own
        # rationale string below reads it, and a plan's rank stays worth
        # recording even when it is not the gate.
        here = None
        try:
            from analysis.entry_ranking import score_plan, rank
            here = score_plan(c)
            field = rank(self.candidates)
            keep = min(max_new * 2, len(field))
            field_totals = [r.total for r in field]
            if _legacy_rank_gate_blocks(here.total, field_totals, keep,
                                       cfg_bool("alloc_live_swing", False)):
                logger.info(f"  {sym}: rank {here.total:.0f} outside "
                           f"today's top {keep}")
                return
        except Exception as e:
            logger.warning(f"  {sym}: ranking unavailable, entry proceeding "
                           f"WITHOUT the rank check — {e}")

        qty = int(getattr(d, "qty", 0) or 0)
        if qty <= 0:
            return

        # LIQUIDITY / CIRCUIT-BAND GATE — can this be got OUT of at plan?
        # Same overlay intraday uses (analysis.overlays.liquidity_ok), on the
        # same rationale: a CNC stop is still a plan for a continuous price,
        # and a swing position is held for 1-3 weeks of chances to gap through
        # it. Reuses the SymbolContext refresh_contexts() already built for
        # this symbol (swing candidates are in context_symbols()) rather than
        # a fresh query — a context that never built (too few bars, or a name
        # refresh_contexts() has not reached yet) reads as unknown liquidity,
        # which liquidity_ok() already refuses by default.
        sctx = self._contexts.get(sym)
        from analysis.overlays import liquidity_ok
        # FALL BACK TO THE DAILY ROW WHEN NO CONTEXT BUILT.
        #
        # A SymbolContext only exists for names refresh_contexts() reached this
        # cycle — 42 of ~96 watched on 2026-08-06, because the bar fetch is
        # rate-limited and its token lookup was timing out. Treating a missing
        # context as unknown liquidity refused TECHM and ASHOKLEY, whose traded
        # value was sitting in stock_data_daily the whole time (₹352 cr and
        # ₹523 cr) — large caps blocked as possibly-illiquid by a data gap.
        #
        # _stock_row() is the right source anyway: it is loaded once per day for
        # the entire universe (no extra query here), and it picks its date by
        # whether value_cr is actually populated. It is also what the gate
        # documents itself as wanting — the PREVIOUS session's traded value.
        # An unavailable row still yields None, so genuinely unknown liquidity
        # is still refused; only the false unknown is repaired.
        vcr = sctx.value_cr if sctx else None
        if not vcr:
            vcr = (self._stock_row(sym) or {}).get("value_cr")
        liq_ok, liq_why = liquidity_ok(
            {"value_cr": vcr,
             "atr_pct": sctx.atr_pct_daily if sctx else None},
            planned_value=qty * ltp,
        )
        if not liq_ok:
            logger.info(f"  {sym}: swing entry blocked — {liq_why}")
            return

        rationale = f"rank {here.total:.0f} — {here.why()}" if here else None

        if not live:
            from execution import paper_broker
            allowed, why, _ = paper_broker.capacity("SWING", self.sb)
            if not allowed:
                logger.info(f"  📄 swing paper skip {sym} — {why}")
                return
            f = paper_broker.simulate_fill(sym, "BUY", qty, "LIMIT", ltp, ltp,
                                           product=paper_broker.product_for("SWING"))
            if f.ok and paper_broker.open_position(
                    sym, qty, f.fill_price,
                    {"stop": d.stop, "target": d.target,
                     "strategy": c.get("strategy"), "entry_rationale": rationale,
                     "sector": c.get("sector")},
                    "SWING", self.sb, charges=f.charges):
                self._entries_taken += 1
                self.load_state()
            return

        # ── LIVE ────────────────────────────────────────────────────────────
        # Marketable limit priced THROUGH the offer, so it fills like a market
        # order without accepting an unbounded price in a thin book — the mirror
        # of what the exit side does, and for the same reason.
        from execution.order_manager import OrderRequest, place
        slip = cfg_int("swing_entry_slip_bps", 20) / 10000.0
        limit = round(ltp * (1 + slip), 1)

        max_entry = getattr(d, "max_entry", None)
        if max_entry and limit > float(max_entry):
            logger.info(f"  {sym}: a {limit} limit would exceed the plan's max entry "
                        f"{max_entry} — not chasing past its own R:R")
            return

        from control.position_lifecycle import find_originating_signal
        try:
            _s = find_originating_signal(self.sb, sym, today_ist().isoformat()) or {}
            _sig = {"signal_id": _s.get("id"), "signal_date": _s.get("date") or c.get("date")}
        except Exception:
            _sig = {"signal_id": None, "signal_date": c.get("date")}

        res = place(OrderRequest(sym, "BUY", qty, "LIMIT", limit,
                                 reason=f"AUTO_ENTRY: {getattr(d, 'reason', '')[:120]}"),
                    self.sb, self.notifier, framework="SWING")
        if not (res and res.ok):
            return

        # Write it back IMMEDIATELY, for the reason the exit side learned the
        # hard way: an order placed and not recorded is re-derived and placed
        # again on the next cycle. PPLPHARMA sold twice that way.
        #
        # STATUS IS 'PENDING_FILL', NOT 'ACTIVE' — 05-Aug-2026.
        #
        # place() returning ok only means Kite accepted the order; a LIMIT day
        # order that never trades through its price expires unfilled at
        # session close, and nothing downstream of this call could tell the
        # difference. TMCV was submitted, recorded as a live position with a
        # GTT stop, and never actually bought — the exit logic spent the next
        # session trying to sell shares that did not exist, and reconcile
        # eventually invented a "sale" that corrupted signal_log's outcome
        # columns and generated an AI-consumed lesson from a trade that never
        # happened.
        #
        # PENDING_FILL is invisible to everything that matters until
        # confirmed: every existing reader of this table already filters
        # status='ACTIVE' (self.positions, GTT sync, exits, alerts, and
        # reconcile's own "in DB, not in Kite" check), so this needed no
        # changes anywhere else. _resolve_pending_fills(), on the slow timer,
        # asks Kite what actually happened and promotes or removes the row —
        # see that method for the COMPLETE / REJECTED / CANCELLED handling.
        try:
            # Local import: control.position_lifecycle imports this package, so
            # module scope would be circular. It was previously imported only
            # inside load_state(), which put the name out of scope here — every
            # auto-entry placed its order and then raised NameError writing the
            # position, leaving a real holding with no row. PPLPHARMA was bought
            # at 09:24 and never appeared in the book.
            from control.position_lifecycle import _upsert_position
            _upsert_position(self.sb, {
                "symbol": sym, "mode": "LIVE", "framework": "SWING",
                "product": "CNC", "status": "PENDING_FILL",
                "entry_order_id": str(res.order_id),
                "entry_date": today_ist().isoformat(), "entry_price": limit,
                "actual_qty": qty, "current_qty": qty, "original_qty": qty,
                "invested_value": round(limit * qty, 2),
                "current_price": limit, "high_water_mark": limit,
                "planned_stop": d.stop, "active_sl": d.stop,
                "planned_target": d.target, "target_price": d.target,
                "sl_type": "AUTO_ENTRY", "strategy": c.get("strategy"),
                "entry_signal_type": c.get("signal_type"),
                # signal_output_daily has NO id column, so c.get("id") was
                # always None and every auto-entry landed unattributed —
                # producing outcomes signal_outcomes cannot learn from, which is
                # the exact gap position_lifecycle was built to close. The id
                # lives in signal_log; find_originating_signal is what reconcile
                # already uses to backfill it.
                **_sig,
                "sector": c.get("sector"), "source": "auto_entry",
                "entry_rationale": rationale,
                # SAME GAP AS entry_rationale ABOVE, TWO MORE COMPONENTS —
                # 07-Aug-2026. Neither field was written here, so neither ever
                # reached closed_positions (close_position() at :921/:924 was
                # always carrying forward None). entry_ranking.score_plan()
                # weights entry_timing_type (rank_weight_timing) and
                # sector_rank_at_entry (rank_weight_sector) same as it weights
                # the rank that entry_rationale fixed — "a check that cannot
                # fail is not a check" applies to a weighted component exactly
                # as much as to the composite rank itself. Confirmed present on
                # signal_output_daily (final_snapshot.py writes both under
                # these exact names) before adding, not assumed.
                "entry_timing_type": c.get("entry_timing_type"),
                "sector_rank_at_entry": c.get("sector_rank_at_entry"),
                "synced_at": datetime.now(IST).isoformat(),
            })
            self._entries_taken += 1
            self._pending_fills[sym] = str(res.order_id)
            self.load_state()
            logger.success(f"  🟡 ENTRY SUBMITTED {sym} {qty} @ ~{limit} "
                           f"(order {res.order_id}) — {rationale or 'no rank'} "
                           f"— awaiting fill confirmation")
        except Exception as e:
            logger.error(f"  {sym}: BOUGHT {qty} but the position row could not be "
                         f"written — {e}. It may be bought again next cycle. "
                         f"Reconcile against the broker NOW.")

    def _resolve_pending_fills(self) -> None:
        """
        Ask Kite what actually happened to every entry still awaiting
        confirmation, and promote or remove the row accordingly.

        SLOW TIMER ONLY — one order_history() call per pending row, and there
        are at most a handful of these on any given day (swing_max_new_per_day
        is 2-4). Nothing here is time-critical: a fill that completed thirty
        seconds ago is exactly as confirmable now as it will be at the next
        15s tick, and Kite's order lifecycle does not need faster polling than
        the rest of this engine's slow-timer work already runs at.

        FAILS TOWARD "STILL PENDING", NEVER TOWARD A GUESS. No broker session,
        no order_history response, or an ambiguous COMPLETE with no fill data
        all leave the row exactly as it was — PENDING_FILL, invisible to
        everything, retried next cycle. tools.health's stale-pending check
        (not this function) is what surfaces a row that never resolves.
        """
        try:
            rows = (self.sb.table("open_positions").select("*")
                      .eq("status", "PENDING_FILL").execute().data or [])
        except Exception as e:
            logger.warning(f"  pending fills: could not read PENDING_FILL rows — {e}")
            return
        if not rows:
            return

        from kite import kite_client
        kite = kite_client.get_kite()
        if not kite:
            return

        for pos in rows:
            sym = pos.get("symbol")
            order_id = pos.get("entry_order_id")
            if not order_id:
                # Cannot be resolved by this path — pre-dates this fix, or was
                # written by a caller that has not adopted it. Left for a
                # human; tools.health's stale-pending check will surface it.
                continue
            try:
                hist = kite.order_history(order_id)
            except Exception as e:
                logger.debug(f"  {sym}: order_history failed for {order_id} — {e}")
                continue
            if not hist:
                # Kite exposes order history for the CURRENT trading day only
                # (the same limit reconcile's old BROKER_EXIT guess ran into).
                # A row that survives past that window cannot be resolved
                # here either — left pending rather than guessed at.
                continue

            final  = hist[-1]              # order_history is chronological
            status = (final.get("status") or "").upper()

            if status == "COMPLETE":
                filled_qty = int(final.get("filled_quantity") or 0)
                avg_price  = float(final.get("average_price") or 0)
                if filled_qty <= 0 or avg_price <= 0:
                    logger.warning(
                        f"  {sym}: order {order_id} reports COMPLETE with no "
                        f"usable fill data ({filled_qty} @ {avg_price}) — "
                        f"leaving PENDING_FILL rather than guessing")
                    continue
                self._promote_pending_fill(pos, filled_qty, avg_price)

            elif status in ("REJECTED", "CANCELLED"):
                self._discard_pending_fill(pos, status,
                                           final.get("status_message") or "")

            # Anything else — OPEN, TRIGGER PENDING, PUT ORDER REQ RECEIVED —
            # is still live at the exchange. Nothing to do; check again next
            # slow tick.

    def _promote_pending_fill(self, pos: dict, filled_qty: int, avg_price: float) -> None:
        """
        Confirmed COMPLETE at the broker — this is now a real position.

        filled_qty can be LESS than what was ordered, on a partial fill.
        Kite's own settlement is the number every downstream P&L, R-multiple
        and exit-sizing computation must read, not the order this engine
        placed — so actual_qty/current_qty/invested_value are corrected here
        to match it. The plan itself (stop, target) is untouched: it was
        computed against the setup, not the fill size.
        """
        sym = pos.get("symbol")
        try:
            (self.sb.table("open_positions").update({
                "status": "ACTIVE",
                "entry_price": avg_price,
                "actual_qty": filled_qty, "current_qty": filled_qty,
                "original_qty": filled_qty,
                "invested_value": round(avg_price * filled_qty, 2),
                "current_price": avg_price, "high_water_mark": avg_price,
                "synced_at": datetime.now(IST).isoformat(),
            }).eq("symbol", sym).eq("product", pos.get("product") or "CNC")
              .eq("status", "PENDING_FILL").execute())
        except Exception as e:
            logger.error(f"  {sym}: fill confirmed but the position row could "
                         f"not be promoted to ACTIVE — {e}. Still PENDING_FILL; "
                         f"will retry next cycle.")
            return
        self._pending_fills.pop(sym, None)
        logger.success(f"  🟢 ENTRY CONFIRMED {sym} {filled_qty} @ {avg_price:.2f} — now live")
        self.notifier.send(Action(
            sym, "ENTRY_CONFIRMED",
            f"Fill confirmed: {filled_qty} @ ₹{avg_price:.2f}",
            f"Order {pos.get('entry_order_id')} completed at the broker. "
            f"This position is now active and under management.",
            ltp=avg_price, urgency="NORMAL",
            framework=pos.get("framework") or "SWING"), force=True)

    def _discard_pending_fill(self, pos: dict, status: str, message: str) -> None:
        """
        The order never became a position — REJECTED outright, or CANCELLED
        (which includes a LIMIT day order that simply expired unfilled at
        session close, exactly what happened to TMCV on 03-Aug).

        Deleted, not closed at 0%. A closed-at-breakeven row still reads as a
        real round trip that happened not to move — this one never existed.
        """
        sym = pos.get("symbol")
        try:
            (self.sb.table("open_positions").delete()
               .eq("symbol", sym).eq("product", pos.get("product") or "CNC")
               .eq("status", "PENDING_FILL").execute())
        except Exception as e:
            logger.error(f"  {sym}: entry {status} at the broker but the pending "
                         f"row could not be removed — {e}. Remove manually; "
                         f"nothing was actually bought.")
            return
        self._pending_fills.pop(sym, None)
        logger.warning(f"  🔴 ENTRY DID NOT FILL — {sym} {status.lower()} at the "
                       f"broker" + (f": {message[:100]}" if message else "")
                       + ". Nothing was bought.")
        self.notifier.send(Action(
            sym, "ENTRY_NOT_FILLED",
            f"Entry {status.lower()} — nothing was bought",
            f"Order {pos.get('entry_order_id')} for {sym} did not fill"
            + (f": {message[:150]}" if message else ".")
            + " No position was opened; this symbol is free again next cycle.",
            urgency="NORMAL", framework=pos.get("framework") or "SWING"), force=True)

    # ── intraday strategy engines ───────────────────────────────────────────
    def evaluate_intraday_setups(self, prices: dict[str, float]) -> list:
        """
        Run the dedicated intraday engines over the watch list.

        Gated on THREE things before a single engine runs, in this order:
        session phase, index context, then per-setup cost. Order matters — the
        cheapest check that can rule everything out goes first, and the index
        gate exists because six long-only engines with no market awareness
        produce a cluster of correlated losses on exactly the day you least
        want them.
        """
        from intraday.session import session_state
        from intraday.strategies.registry import evaluate_all
        from intraday.strategies.base import SymbolContext
        from intraday import market_context as mkt
        from intraday.cost_model import is_worth_taking, round_trip
        from config import capital_for

        if not cfg_bool("intraday_strategies_enabled", True):
            return []

        st = session_state()
        if not st.can_enter:
            return []

        # Freshen the index's LTP from the live tick BEFORE gating on it — see
        # _refresh_index_ltp() for why this was missing. Mutates self._index_ctx
        # in place, so _allocate_shadow()'s own mkt.from_context() call later in
        # THIS SAME cycle sees the same fresh value without needing prices
        # threaded through separately.
        self._refresh_index_ltp(prices)
        mc = mkt.from_context(self._index_ctx)
        # THE MARKET GATE IS NOW PER DIRECTION, NOT A SINGLE DOOR.
        #
        # `allow_longs` alone returned [] and skipped the whole scan — which in
        # RISK_OFF is exactly the regime a short book exists to trade. The two
        # flags are complements: RISK_OFF blocks longs and permits shorts, and
        # RISK_ON does the reverse. Bailing out early only when NEITHER is
        # permitted keeps the cheap exit for a genuinely untradeable tape while
        # letting the scan run for whichever side the market is offering.
        shorts_live = cfg_bool("intraday_allow_shorts", False) and mc.allow_shorts
        if not mc.allow_longs and not shorts_live:
            if self._last_state.get("_market") != mc.state:
                mkt.log_state(mc)
                self._last_state["_market"] = mc.state
            return []

        # STALENESS GUARD — no entry is decided on data of unknown age.
        #
        # Contexts are rebuilt on the 300-second timer. If that refresh fails —
        # broker unavailable, rate limit, a dead socket with a live cache — the
        # previous contexts stay in memory and keep being read, and every day
        # range and volume in them is from whenever the failure started. Nothing
        # about that reads as broken: the engines run, the prices tick, the
        # verdicts look ordinary.
        #
        # The bound is 420 s rather than 300 so an ordinary late refresh does not
        # blank the book; anything beyond that is a refresh that did not happen.
        # Exits are NOT gated on this — a stale range is a bad reason to enter and
        # never a reason to stop protecting an open position.
        stale = self.stale_contexts()
        if stale:
            worst = max((a for a in stale.values() if a is not None), default=None)
            logger.warning(
                f"  staleness guard: {len(stale)} context(s) older than the bound"
                + (f" (worst {worst:.0f}s)" if worst else " (age unknown)")
                + " — no new entries on those names until the next refresh")

        out = []
        # Symbols refused specifically for runway (can_short()'s cover-deadline
        # check), not for quality — the entry-side twin of the runway-tighten
        # rung in exit_policy.py. Tracked so the end-of-cycle line can point at
        # what WAS actionable in the same cycle instead of six refusals in a
        # row reading like a dead market. See the summary below the loop.
        runway_refused: list[str] = []
        for sym, ctx in (self._contexts or {}).items():
            # Already an intraday position in this name: nothing to decide, and
            # no detection worth recording — it would be the same thesis at a
            # later price. Skipped before the engines run, as it always was.
            if self._held_by_framework(sym, "INTRADAY"):
                continue
            if sym in stale:
                continue
            ltp = prices.get(sym)
            if not ltp:
                continue
            ctx.ltp = float(ltp)

            best, _all = evaluate_all(ctx, st.phase)

            # SHADOWED ENGINES ARE RECORDED, NOT DISCARDED.
            #
            # A retiree earns its removal — or its reprieve — from detections
            # that were scored, and outcomes.resolve_day scores whatever is in
            # intraday_setups. If a shadowed detection is never written, the
            # engine goes quiet, the quarter passes with no evidence either way,
            # and "no data" gets read as "no edge". That is not a retirement
            # test, it is a retirement with extra steps.
            #
            # Written with verdict SHADOW so it can never be mistaken for a
            # setup that was refused on merit, and with qty 0 so it can never be
            # mistaken for one that traded.
            for s in _all:
                if s.meta.get("lifecycle") == "SHADOW":
                    try:
                        self._record_setup(s, st.phase, 0.0, "SHADOW", 0, mc_state=(mc.state if mc else None))
                    except Exception as e:
                        logger.debug(f"  shadow record failed for {s.strategy}: {e}")

            if not best:
                continue

            # ── CAN THIS BE SHORTED, AND MORE TO THE POINT, COVERED? ────────
            #
            # Runs before conviction, cost or the allocator, because it is not a
            # quality filter — it is a solvency filter. Every other gate here
            # asks whether the trade is good. This one asks whether the position
            # can be closed at all, and a "no" from it cannot be outweighed by
            # any amount of edge.
            #
            # Two switches, both of which must be on: the master
            # `intraday_allow_shorts`, and the market context actively
            # confirming weakness. A short is refused by default in every state
            # that is not RISK_OFF or CAUTION, including an unreadable index.
            #
            # THE VERDICT NAMES WHICH OF THE TWO SAID NO — 10-Aug-2026.
            # `shorts_live` is an AND of a config switch and a market state,
            # and the single verdict "BLOCKED_SHORTS_OFF" reported both as the
            # switch. On 10-Aug that produced 27 BLOCKED_SHORTS_OFF rows while
            # `intraday_allow_shorts` was true and `intraday_engine_sdn_
            # lifecycle` was ACTIVE — the refusals were the index being risk-on
            # (mc.allow_shorts False), which is the SDN engine working exactly
            # as designed. Anyone reading that histogram concluded shorting was
            # switched off and went looking for a config bug that did not
            # exist. Two causes, two names: one is a standing operator
            # decision, the other is today's tape.
            if best.is_short:
                if not shorts_live:
                    self._record_setup(
                        best, st.phase, 0.0,
                        "BLOCKED_SHORTS_OFF" if not cfg_bool("intraday_allow_shorts", False)
                        else "BLOCKED_SHORTS_MARKET", 0, mc_state=(mc.state if mc else None))
                    continue
                from intraday import shortability
                # NOT getattr(st, "minutes_to_close", 0). SessionState has no
                # such field — it is minutes_to_squareoff, and minutes_to_close
                # is a module function — so the default 0 was taken silently on
                # every call, and every short saw a runway of 0 - 10 = -10 min.
                # -10 >= 75 is false at every hour of every session, so no short
                # has ever cleared this gate; the log read like a market with no
                # shortable names. A getattr default is precisely what let a
                # wrong attribute name look like a working call.
                from intraday.session import minutes_to_cover_deadline
                ok_sh, why_sh, notes = shortability.can_short(
                    ctx, self._stock_row(sym),
                    minutes_left=minutes_to_cover_deadline())
                best.meta["shortability"] = notes
                if not ok_sh:
                    # Persisted, not just logged — requeue_runway_refused_shorts()
                    # reads this back tomorrow to tell a runway refusal apart
                    # from a circuit-band/squeeze/liquidity one, both filed
                    # under the same BLOCKED_SHORTABILITY verdict. Without it,
                    # meta carried only `notes` (the checks that PASSED before
                    # the one that failed), never the failure reason itself.
                    best.meta["shortability_reason"] = why_sh
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_SHORTABILITY", 0, mc_state=(mc.state if mc else None))
                    logger.info(f"      {sym}: short refused — {why_sh[:120]}")
                    # "cover deadline" is the literal phrase can_short() uses
                    # for the runway check specifically (shortability.py) —
                    # distinct from the circuit-band/squeeze/liquidity refusals
                    # the same function can also return, which are about the
                    # STOCK, not the clock, and have no "try again tomorrow"
                    # story the way a runway refusal does.
                    if "cover deadline" in why_sh:
                        runway_refused.append(sym)
                    continue

            # ONE SYMBOL, ONE BOOK. The swing book got here first, so it owns
            # the name until it closes.
            #
            # RECORDED, not skipped silently. This used to be a bare `continue`
            # at the top of the loop and it is the single most confusing thing
            # the two frameworks do to each other from the outside: the engines
            # find a setup, say nothing, and the operator sees a name the
            # scanner clearly liked produce no detection at all. A refusal that
            # leaves no row is also a rule nobody can price — the weekly review
            # cannot ask what standing down cost.
            # THE TERMINAL CHECK RUNS BEFORE THE ANNOUNCEMENT — 10-Aug-2026.
            #
            # The satellite-join notice below used to print BEFORE the
            # re-entry guard, so a name that had already lost money today
            # produced this pair, every cycle, for hours:
            #
            #   MANAPPURAM: VWR conf 0.51 — ... joining as a same-direction
            #                                  LONG satellite ...
            #   MANAPPURAM: VWR conf 0.51 — already lost money in this name
            #                                  today, standing down
            #
            # An announcement of an entry that the very next line refuses. It
            # is not merely noise: read live, it says the book is joining a
            # swing position when it is doing nothing of the kind, and it
            # repeated twice a minute across the whole session. `_failed_today`
            # is cached per session, terminal, and cheaper than the holding
            # lookup, so it belongs first on both counts.
            if cfg_bool("intraday_block_reentry_after_loss", True) \
                    and sym in self._failed_today():
                self._record_setup(best, st.phase, 0.0, "BLOCKED_REENTRY", 0, mc_state=(mc.state if mc else None))
                logger.info(f"      {sym}: {best.strategy} conf {best.confidence:.2f} "
                            f"— already lost money in this name today, standing down")
                continue

            other = self._other_framework_holding(sym, "INTRADAY")
            if other is not None:
                if _intraday_may_join_swing_holding(other, best.direction):
                    logger.info(
                        f"      {sym}: {best.strategy} conf {best.confidence:.2f} — SWING "
                        f"already holds this name ({other.get('current_qty') or other.get('actual_qty')} "
                        f"@ {other.get('entry_price')}); joining as a same-direction LONG "
                        f"satellite (intraday_allow_swing_held_symbols). The swing position "
                        f"is untouched — only this MIS tranche squares off at session end")
                    # falls through to the gates below, no `continue`
                else:
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_CROSS_FRAMEWORK", 0, mc_state=(mc.state if mc else None))
                    logger.info(
                        f"      {sym}: {best.strategy} conf {best.confidence:.2f} — the "
                        f"{(other.get('framework') or 'SWING').upper()} book already holds "
                        f"this name ({other.get('current_qty') or other.get('actual_qty')} "
                        f"@ {other.get('entry_price')}). One book per symbol; the intraday "
                        f"square-off must not sell a multi-week thesis")
                    continue

            # Event gate BEFORE cost: a results-day setup is not a pricing
            # question, and computing a position size for a trade that must not
            # be taken wastes the check that matters.
            # Carry the sector onto the setup. ctx has it, the setup does not,
            # and _maybe_open_paper never sees ctx — which is why sector was
            # NULL on every intraday position ever opened.
            best.meta["sector"] = ctx.sector

            if self._news is not None:
                ev = self._news.check(best.symbol, ctx.sector)
                if not ev.allow:
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_EVENT", 0, mc_state=(mc.state if mc else None))
                    continue
                if ev.reason:
                    best.meta["event_note"] = ev.reason

            # Swing STRUCTURE gate, on intraday settings. Every engine here
            # reasons about a static level — the range high, VWAP, the coil top
            # — and none knows the SEQUENCE that level sits in. Breaking the
            # opening range high while making lower highs all morning is buying
            # a lower high, which is the trade a downtrend exists to punish.
            if cfg_bool("intraday_structure_gate", True) and ctx.bars:
                from analysis.market_structure import gate_for_framework
                ok_s, why_s, st_struct = gate_for_framework(
                    "INTRADAY", [b.high for b in ctx.bars], [b.low for b in ctx.bars],
                    direction=best.direction)
                if not ok_s:
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_STRUCTURE", 0, mc_state=(mc.state if mc else None))
                    continue
                best.meta["structure"] = st_struct.state

            # AI advice from the previous slow tick. Queue this setup for the
            # next review regardless, so a first sighting is never delayed
            # waiting for an opinion that takes 88 seconds to form.
            self._pending_review.append(best)
            from intraday import ai_advisor
            allow_ai, adj_conf, ai_note = ai_advisor.apply(best, self._advice)
            if not allow_ai:
                self._record_setup(best, st.phase, 0.0, "VETOED_AI", 0, mc_state=(mc.state if mc else None))
                continue
            best.confidence = adj_conf
            if ai_note:
                best.meta["ai_note"] = ai_note

            # CONVICTION FLOOR, TIGHTENING AS THE DAY'S BUDGET IS SPENT.
            #
            # The cap is 5 orders a day and the session is six hours long, so
            # taking the first five qualifying setups is a strictly worse policy
            # than taking the best five — and a fixed threshold cannot express
            # that, because "good enough" at 09:20 with the whole budget intact
            # is not good enough at 14:30 with one order left.
            #
            # So the floor rises with the fraction of the budget already used.
            # Early, a decent setup is worth taking; late, only a strong one is,
            # because the alternative to a mediocre trade is no trade, and no
            # trade costs nothing while a mediocre one costs 0.21% plus the risk.
            floor = self._confidence_floor()
            if best.confidence < floor:
                self._record_setup(best, st.phase, 0.0, "BELOW_CONVICTION", 0, mc_state=(mc.state if mc else None))
                logger.info(f"      {sym}: {best.strategy} conf {best.confidence:.2f} "
                            f"< floor {floor:.2f} — passing, budget is better spent later")
                continue

            # Size against the market state, then ask whether the trade still
            # survives its own costs at that size. A setup that only works at
            # full size on a CAUTION day is not a setup, it is leverage.
            # Three limits, smallest wins:
            #   · a fixed FRACTION of capital per position. Without this the
            #     budget was TOTAL_CAPITAL x multiplier, i.e. the entire account
            #     in a single intraday setup on any risk-on day.
            #   · the market-state multiplier, which shrinks size when the index
            #     is not cooperating.
            #   · the per-order rupee cap, the same one preflight enforces.
            #     Sizing above it would produce orders that are computed and
            #     then rejected, and in PAPER — which does not run preflight —
            #     a book the live account would have refused.
            from execution.gates import max_order_value
            pos_pct = cfg_float("intraday_max_position_pct", 25.0) / 100.0
            # mc.size_multiplier reacts to the INDEX's technical state
            # (RISK_ON/CAUTION/...); self._overlay_intraday_mult reacts to
            # expiry mechanics and the VIX level. Different signals, both
            # real — multiplied together rather than one replacing the other.
            budget = min(capital_for("INTRADAY") * pos_pct * mc.size_multiplier * self._overlay_intraday_mult,
                         max_order_value("INTRADAY"))
            qty = int(budget // best.entry) if best.entry else 0
            if qty <= 0:
                continue

            # LIQUIDITY / CIRCUIT-BAND GATE — can this be got out of at plan?
            # Structural, not a pricing question, so it runs before the cost
            # check: a setup that cannot be exited at plan is not made viable
            # by being cheap to enter.
            from analysis.overlays import liquidity_ok
            liq_ok, liq_why = liquidity_ok(
                {"value_cr": ctx.value_cr, "atr_pct": ctx.atr_pct_daily},
                planned_value=qty * best.entry,
            )
            if not liq_ok:
                self._record_setup(best, st.phase, 0.0, "BLOCKED_LIQUIDITY", 0, mc_state=(mc.state if mc else None))
                logger.info(f"      {sym}: {best.strategy} — {liq_why}")
                continue

            # DIRECTION MUST BE PASSED, NOT DEFAULTED. is_worth_taking() was
            # made direction-aware in the sign-convention spine and defaults to
            # LONG so every pre-shorting caller keeps working unchanged — but
            # this IS the caller a short setup reaches, and omitting the
            # argument here silently invoked that same default. A short's
            # target sits BELOW entry and its stop ABOVE it; scored as LONG
            # that reads as "target is on the wrong side of entry", and every
            # short would be REJECTED_COST regardless of how good the trade
            # was — the setup detects, passes shortability and the structure
            # gate, and dies at the one check that never learned it was a
            # short. The exact shape of the open_positions.direction gap found
            # during merge review, one call site over: a correct function,
            # forgotten at its call.
            ok, why = is_worth_taking(best.entry, qty, best.target, best.stop,
                                      direction=best.direction)
            rt = round_trip(best.entry, qty)
            self._record_setup(best, st.phase, rt.pct_of_position,
                               "TAKEN" if ok else "REJECTED_COST", qty, mc_state=(mc.state if mc else None))
            if not ok:
                continue

            out.append({"setup": best, "qty": qty, "cost_pct": rt.pct_of_position,
                        "market": mc, "phase": st.phase, "cost_note": why})

        # BEST FIRST, not first-seen first.
        #
        # This loop walks self._contexts, a dict, so `out` came back in watch
        # list order and act_on_setups consumed it in that order until the book
        # was full. Whichever symbol happened to sit earlier in the dict won the
        # slot — the ranking inside a symbol (evaluate_all picks its best
        # engine) had no equivalent ACROSS symbols.
        #
        # On 31 Jul four setups cleared every gate in the same minute and the
        # slot went to the alphabetically luckier one. Sorting costs nothing and
        # is the whole difference between "take five setups" and "take the best
        # five setups available at the moment of choosing".
        #
        # Tie-break on R:R: at equal conviction the trade that pays more for the
        # same risk is strictly better, and a stable second key also stops two
        # equally-rated setups from swapping places between ticks.
        out.sort(key=lambda s: (s["setup"].confidence, s["setup"].rr), reverse=True)

        msg = _runway_refusal_summary(runway_refused, out)
        if msg:
            logger.info(msg)

        return out

    def _intraday_alert_worthy(self, st) -> bool:
        """
        Is this setup good enough to interrupt you, given what today has shown?

        WHY THIS CANNOT WORK LIKE THE SWING VERSION
        -------------------------------------------
        Swing ranks a FIXED field: last night's pipeline produced every plan and
        their scores, so "today's top five" is knowable at 09:15 and stable all
        day. Intraday has no such list — a setup exists only once price has made
        it, so the day's best cannot be known until the day is over, and by then
        the alert is worthless.

        So this is a STREAMING top-N: alert freely until N have been sent, then
        only for a setup that beats the weakest one already sent. That converges
        on the best N of the session without needing to see the future, and it
        degrades in the right direction — an ordinary morning still tells you
        about its best setups, while a busy one stops repeating mediocre ones.

        The alternative — a fixed confidence threshold — cannot adapt. Set it
        high and a quiet day says nothing; set it low and a busy day says
        everything. This asks the only question that matters at each moment: is
        this better than what I have already interrupted you about?
        """
        n = cfg_int("intraday_alert_top_n", 5)
        if n <= 0:
            return True
        if len(self._alerted_conf) < n:
            return True
        # Compare against the weakest of the N BEST so far, not of everything
        # ever sent. Keeping an unbounded list made min() fall with each new
        # alert, so the bar dropped as the day went on and every setup
        # eventually qualified — a top-N that ratchets the wrong way.
        return st.confidence > min(sorted(self._alerted_conf, reverse=True)[:n])

    def _record_alerted(self, st) -> None:
        """Keep only the N best confidences seen, so the bar rises not falls."""
        n = max(1, cfg_int("intraday_alert_top_n", 5))
        self._alerted_conf.append(st.confidence)
        self._alerted_conf = sorted(self._alerted_conf, reverse=True)[:n]

    def act_on_setups(self, setups: list) -> None:
        for s in setups:
            st, qty, mc = s["setup"], s["qty"], s["market"]
            corro = st.meta.get("corroborated_by") or []

            # THE ALLOCATOR'S VETO, before the position is opened or alerted.
            # Recorded as a refusal so the decision stays in intraday_setups
            # rather than vanishing — a setup that was found and then declined
            # on opportunity cost is evidence, and dropping it silently would
            # make the allocator unscoreable against the greedy path.
            ok, why = self.allocator_permits(st.symbol, "MIS", "INTRADAY")
            if not ok:
                self._record_setup(st, s["phase"], s.get("cost_pct") or 0.0,
                                   "ALLOCATOR_DECLINED", 0, mc_state=(mc.state if mc else None))
                logger.info(f"      {st.symbol}: allocator declined — {why[:90]}")
                continue
            # In PAPER mode, actually TAKE the setup. Without this the
            # simulation measures exits but never a full round trip, and a
            # system judged only on how it leaves trades tells you nothing
            # about which trades it should have entered.
            self._maybe_open_paper(st, qty, mc, phase=s["phase"],
                                   cost_pct=s.get("cost_pct") or 0.0)

            if not self._intraday_alert_worthy(st):
                logger.info(f"      {st.symbol}: {st.strategy} conf {st.confidence:.2f} "
                            f"does not beat the weakest of today's "
                            f"{cfg_int('intraday_alert_top_n', 5)} alerted setups — quiet")
                continue
            self._record_alerted(st)

            self.notifier.send(Action(
                symbol=st.symbol, kind=f"SETUP_{st.strategy}",
                headline=(f"{st.strategy} — buy {qty} @ ₹{st.entry:.2f}, "
                          f"stop ₹{st.stop:.2f}, target ₹{st.target:.2f} "
                          f"(R:R {st.rr:.1f})"),
                detail=(f"{st.rationale}\n"
                        f"Dies if: {st.invalidation}\n"
                        f"Risk {st.risk_pct:.2f}% · reward {st.reward_pct:.2f}% · "
                        f"{s['cost_note']}\n"
                        f"Market {mc.state} (size ×{mc.size_multiplier:g}) · {s['phase']}"
                        + (f"\nAlso flagged by: {', '.join(corro)}" if corro else "")),
                ltp=st.entry, urgency="NORMAL",
                meta={"strategy": st.strategy, "qty": qty, "rr": round(st.rr, 2)},
                framework="INTRADAY",
            ))

    def _setup_is_new(self, s, verdict: str) -> bool:
        """
        Has this setup already been recorded this session?

        The decision loop runs every 15 seconds, and a setup that is valid at
        11:00 is usually still valid at 11:00:15 — so recording on every cycle
        wrote CONCOR/PDL four times in two minutes and would have produced
        thousands of near-identical rows a day. That breaks two things at once:
        the storage budget this package was designed around, and the scorecard,
        because a hit rate computed over duplicates counts one setup fifty
        times and buries every other engine.

        A setup is NEW when its symbol+engine has not been seen today, or when
        its levels have moved materially — a genuinely different trade at a
        different price, rather than the same one restated.
        """
        key = f"{s.symbol}:{s.strategy}"
        prev = self._recorded.get(key)
        if prev is None:
            return True
        prev_entry, prev_verdict = prev
        # A cost verdict flipping is worth recording: the same setup becoming
        # takeable (or ceasing to be) is a real change.
        if prev_verdict != verdict:
            return True
        drift = cfg_float("intraday_setup_dedup_pct", 0.35) / 100.0
        return bool(prev_entry and abs(s.entry - prev_entry) / prev_entry > drift)

    def _maybe_open_paper(self, st, qty: int, mc, phase: str = "?",
                          cost_pct: float = 0.0) -> None:
        """
        Simulate the entry, so the paper book contains real round trips.

        Gated on capacity as well as the usual rails: a simulation that opens
        forty positions tests nothing about a system that can hold five, and its
        results would not transfer to the account it exists to inform.

        EVERY REFUSAL BELOW IS RECORDED — 10-Aug-2026. This function used to be
        the one place in the whole cost/allocator/capacity chain that refused a
        setup silently: BLOCKED_SHORTABILITY, BLOCKED_STRUCTURE, VETOED_AI,
        ALLOCATOR_DECLINED and every other gate upstream write a row explaining
        themselves, but a setup that cleared ALL of them and then hit a
        concurrency cap, a daily budget, a paper-broker capacity refusal, a
        race against `_held_by_framework`, a failed fill, or a bare exception
        simply vanished. `tools/taken_reconciliation.py` measured the result on
        this account: 27 of 85 TAKEN detections across 10 sessions had NEITHER
        a real position NOR an ALLOCATOR_DECLINED row — a setup that passed
        every quality gate and the allocator's own opportunity-cost check, and
        then disappeared with no trace of why. That is exactly the "a refusal
        that leaves no row is a rule nobody can price" failure this file's own
        cross-framework guard was written to stop, one level deeper in the
        same function.
        """
        from execution.gates import is_paper

        def _blocked(verdict: str) -> None:
            self._record_setup(st, phase, cost_pct, verdict, 0, mc_state=(mc.state if mc else None))

        # Two switches, mirroring control/paper_entry.py for swing.
        # intraday_auto_entry says whether setups are taken at all;
        # intraday_live_auto_entry says whether that may spend real money.
        # Both existed in system_config and NOTHING read them — the panel showed
        # them as on, and they did nothing, which is the same class of failure as
        # swing_auto_entry before it was wired.
        if not cfg_bool("intraday_auto_entry", True):
            _blocked("BLOCKED_AUTO_ENTRY_OFF")
            return

        # How many NEW intraday positions today. Distinct from
        # intraday_max_orders_per_day, which caps ORDERS — the same distinction
        # swing needed, and for the same reason: one is a safety rail against a
        # runaway loop, the other is a decision about concentration.
        # TWO limits, and they are not the same question.
        #
        # `intraday_max_concurrent` is about attention and correlation: how many
        # open intraday risks at once. `intraday_max_new_per_day` is about churn:
        # how many times the system is allowed to be wrong before it stops for
        # the day.
        #
        # This checked only currently-OPEN positions against max_new_per_day, so
        # every close freed a slot and the "daily" cap could never bind. On
        # 31 Jul it permitted 7 entries under a cap of 4 — and since 5 of those
        # 7 lost, the cap that was supposed to stop the bleeding was the one
        # measuring the wrong thing.
        open_now = len([p for p in self.positions
                        if (p.get("framework") or "").upper() == "INTRADAY"])
        if open_now >= cfg_int("intraday_max_concurrent",
                               cfg_int("intraday_max_new_per_day", 4)):
            _blocked("BLOCKED_CONCURRENCY")
            return
        if self._entries_today() >= cfg_int("intraday_max_new_per_day", 4):
            logger.info(f"  {st.symbol}: intraday daily entry budget spent "
                        f"({self._entries_today()}) — no more new risk today")
            _blocked("BLOCKED_DAILY_BUDGET")
            return

        # A RESERVE, NOT A GAP — 11-Aug-2026, replacing the same day's
        # gap-based pacing. See order_manager.entry_reserved()'s docstring
        # and _maybe_enter_swing's own comment for the full reasoning: a
        # fixed minimum gap between entries blocks candidates the allocator
        # already approved TOGETHER, in one joint scoring pass, purely
        # because of arrival timing — overriding a decision that was
        # already opportunity-cost-aware, at zero benefit.
        #
        # DEFAULT OFF (0) FOR INTRADAY, DELIBERATELY, UNLIKE SWING'S 1. Two
        # reasons swing's evidence does not transfer here: (1) no verified
        # intraday incident of the SAME failure — the direct check
        # (intraday_setups timing) is contaminated by pre-dedup duplicate
        # rows and closed_positions has no entry-clock-time column, so this
        # was left unmeasured rather than guessed at; (2) ORB-family
        # engines are STRUCTURALLY supposed to cluster shortly after the
        # opening range closes — an early reserve would fight a genuine
        # feature of that engine, not a bug, without evidence it is needed.
        # Raise intraday_max_entries_before_time above 0 only with the same
        # kind of evidence swing's default was built on.
        from execution.order_manager import entry_reserved, parse_hhmm
        reserve_n = cfg_int("intraday_max_entries_before_time", 0)
        cutoff = parse_hhmm(cfg("intraday_entries_time_cutoff", "10:00"))
        blocked, why = entry_reserved(self._entries_today(), datetime.now(IST),
                                      reserve_n, cutoff)
        if blocked:
            logger.info(f"  {st.symbol}: intraday entry reserved — {why}")
            _blocked("BLOCKED_ENTRY_RESERVED")
            return
        if not is_paper("INTRADAY"):
            if not cfg_bool("intraday_live_auto_entry", False):
                logger.info(f"  {st.symbol}: INTRADAY is LIVE and "
                            f"intraday_live_auto_entry is off — alerting only")
                _blocked("BLOCKED_LIVE_AUTO_ENTRY_OFF")
                return
            # Live auto-entry is deliberately not implemented here. Committing
            # capital on a single live tick is the highest-variance action this
            # system can take, and it is not one to enable by flipping a switch.
            logger.warning(f"  {st.symbol}: live auto-entry is not implemented — "
                           f"entries stay manual. Alerting only.")
            _blocked("BLOCKED_LIVE_NOT_IMPLEMENTED")
            return
        try:
            from execution import paper_broker
            allowed, why, _left = paper_broker.capacity("INTRADAY", self.sb)
            if not allowed:
                logger.info(f"  📄 paper skip {st.symbol} — {why}")
                _blocked("BLOCKED_PAPER_CAPACITY")
                return
            if self._held_by_framework(st.symbol, "INTRADAY"):
                _blocked("BLOCKED_ALREADY_HELD")
                return
            # A SHORT OPENS WITH A SELL. Hardcoding "BUY" would have simulated
            # the wrong leg: paper_broker fills a BUY at the worse side of the
            # spread going UP, so a short's entry would have been modelled at a
            # price better than reality on the wrong side, and its charges
            # attributed to the entry leg rather than the exit one.
            f = paper_broker.simulate_fill(st.symbol, D.entry_side(st.direction),
                                           qty, "LIMIT", st.entry, st.entry,
                                           product=paper_broker.product_for("INTRADAY"))
            if not f.ok:
                logger.info(f"  📄 fill failed {st.symbol} — {f.message}")
                _blocked("BLOCKED_FILL_FAILED")
                return
            from intraday.exit_policy import invalidation_level_from
            inv_level, inv_note = invalidation_level_from(st)
            paper_broker.open_position(
                st.symbol, qty, f.fill_price,
                {"stop": st.stop, "target": st.target, "strategy": st.strategy,
                 "invalidation_level": inv_level, "invalidation_note": inv_note,
                 "sector": st.meta.get("sector"),
                 # FOUND DURING MERGE REVIEW, migration 047: this was the one
                 # missing link. The entry FILL already used D.entry_side(
                 # st.direction) a few lines up, so a short's opening leg was
                 # already correct — but without this key the resulting row
                 # carried no direction at all, and every later reader
                 # (excursion tracking, the exit ladder, cover-vs-sell at
                 # square-off) would read it back as None -> LONG. A short
                 # would have opened correctly and then been managed as a
                 # long for the rest of its life.
                 "direction": st.direction},
                "INTRADAY", self.sb, charges=f.charges)
            # Reload so the same setup cannot be opened twice in one session and
            # so the exit engine sees it on the very next cycle.
            self.load_state()
        except Exception as e:
            logger.warning(f"  paper entry failed for {st.symbol}: {e}")
            # A FIXED verdict string, not the exception folded into it — every
            # other reader of this column (taken_reconciliation, engine_
            # scorecard, the priors) does an exact match against cost_verdict,
            # and a dynamic value would silently stop matching any of them.
            # The detail belongs in meta, same as ai_note/event_note elsewhere.
            st.meta["exception"] = str(e)[:200]
            _blocked("BLOCKED_EXCEPTION")

    def square_off_paper(self, prices: dict) -> int:
        """
        Force every INTRADAY paper position flat at the configured time.

        An intraday position that survives the close is not an intraday
        position — it is an accidental overnight hold, and letting the
        simulation keep one would make paper results describe a strategy nobody
        intends to run. Live MIS is squared off by the broker; paper has no
        broker, so this stands in for it.
        """
        from execution.gates import is_paper
        if not is_paper("INTRADAY"):
            return 0
        from intraday.session import phase_at, SQUARE_OFF, CLOSED
        if phase_at() not in (SQUARE_OFF, CLOSED):
            return 0

        closed = 0
        from execution import paper_broker
        for p in list(self.positions):
            if (p.get("framework") or "").upper() != "INTRADAY":
                continue
            if (p.get("mode") or "").upper() != "PAPER":
                continue
            px = prices.get(p["symbol"]) or p.get("current_price")
            if not px:
                # A SHORT WITH NO PRICE IS THE ONE CASE THAT MUST BE LOUD.
                #
                # A long left open by a missing price is an accidental overnight
                # hold in a simulation — untidy, and it distorts the paper
                # results. A short left open is the shape of the failure that in
                # the live book becomes short delivery and an auction penalty of
                # roughly 20% of the trade. Same code path, two very different
                # consequences, so they do not get the same log level.
                if D.is_short(p.get("direction")):
                    logger.error(
                        f"  ⚠ {p['symbol']}: SHORT could not be covered at square-off "
                        f"— no price available. In the live book this is short "
                        f"delivery and an auction penalty. Cover it by hand and "
                        f"find out why the feed had no price for this name.")
                continue
            # `close_position` is the flattening leg — a BUY for a short. Named
            # explicitly rather than left implicit so the intent survives the
            # next person to read it.
            if paper_broker.close_position(
                    p["symbol"], float(px), "SQUARE_OFF",
                    ("intraday session ended — covering the short"
                     if D.is_short(p.get("direction")) else
                     "intraday session ended — flat by design"),
                    self.sb, side=D.exit_side(p.get("direction"))):
                closed += 1
        if closed:
            logger.info(f"  📄 squared off {closed} paper intraday position(s)")
            self.load_state()
        return closed

    def _allocate_shadow(self, entries: list, setups: list) -> None:
        """
        Score what both books just proposed, record the verdicts, act on none.

        BUFFERED, NEVER WRITTEN HERE. select() appends to an in-memory buffer;
        the 300-second timer flushes it. A synchronous write in this loop would
        put a network round trip in front of exit evaluation on live positions,
        which is the one latency this system cannot afford.
        """
        # VERDICTS ARE SCOPED TO THE CYCLE THAT PRODUCED THEM.
        #
        # `self._verdicts` was only ever ASSIGNED, at the end of this function,
        # and every path that returns early left the PREVIOUS cycle's verdicts
        # in place — while allocator_permits kept reading them. A DECLINE issued
        # at 09:20 against one setup would then veto a different setup in the
        # same name at 14:00, on arithmetic that no longer referred to it. Worse,
        # turning alloc_shadow_enabled off mid-session while alloc_live_* stayed
        # on froze the last verdicts permanently, because those two switches are
        # read independently.
        #
        # Cleared FIRST, so a stale verdict cannot survive any exit from here.
        # An empty map fails open, which is the documented behaviour.
        self._verdicts = {}

        if not cfg_bool("alloc_shadow_enabled", False):
            return
        if not entries and not setups:
            return

        from allocation.proposal import from_intraday, from_swing
        from allocation.allocator import Allocator
        from intraday.session import session_state
        from intraday import market_context as mkt

        if self._allocator is None:
            self._allocator = Allocator(self.sb)
            self._allocator.refresh_priors()

        props = []
        for e in entries or []:
            d = e.get("decision") if isinstance(e, dict) else e
            # `evaluate_candidates` builds these dicts as {"candidate": c,
            # "decision": d, "ltp": ..., "state": ...} — there has never been a
            # "plan" key. `e.get("plan")` silently returned None on every call
            # since the allocator was wired in, so from_swing's `strategy` and
            # `final_score` reads always saw an empty dict: every swing
            # Proposal's source fell to the "CONTINUATION" literal regardless
            # of engine, and native_rank was always 0.0. `c` (the candidate,
            # a signal_output_daily row) is what actually carries both fields.
            p = from_swing(d, e.get("candidate") if isinstance(e, dict) else None)
            if p:
                props.append(p)
        for s in setups or []:
            setup = s.get("setup") if isinstance(s, dict) else s
            qty   = int((s.get("qty") if isinstance(s, dict) else 0) or 0)
            p = from_intraday(setup, qty)
            if p:
                props.append(p)
        if not props:
            return

        st = session_state()
        mc = mkt.from_context(self._index_ctx)

        # SLOTS ARE PER BOOK, FROM EACH BOOK'S OWN CAP.
        #
        # This was `alloc_max_slots` (2, pooled) minus EVERY position entered
        # today in EITHER framework. One swing entry in the morning therefore
        # left the intraday book — which is governed by intraday_max_new_per_day
        # (4) — with a single slot for the whole session, and two entries of any
        # kind left it with none, at which point hurdle() returns an infinite
        # bar and every setup is declined. Nothing in the intraday logs said so;
        # the cap that bound was the swing book's morning.
        #
        # The two books do not share an entry budget in any other part of this
        # system, and the allocator is an opportunity-cost optimiser, not a risk
        # control — the risk controls are the per-book caps, the sector cap and
        # the combined account guard, all of which still run. So each book
        # brings its own number.
        today = today_ist().isoformat()
        swing_used = len([x for x in self.positions
                          if (x.get("framework") or "SWING").upper() == "SWING"
                          and str(x.get("entry_date") or "")[:10] == today])
        swing_max = max(cfg_int("swing_max_new_per_day", 2), 1)
        intra_max = max(cfg_int("intraday_max_new_per_day", 4), 1)
        slots = {"SWING":    max(swing_max - swing_used, 0),
                 "INTRADAY": max(intra_max - self._entries_today(), 0)}

        # THE SECOND INSTANCE OF THE SAME MISSING ATTRIBUTE, found while
        # reading the fix for the first one.
        #
        # SessionState has `minutes_to_squareoff`. It has never had
        # `minutes_to_close` — that is a module FUNCTION in
        # intraday/session.py — so getattr's default was taken on every
        # call since the allocator was first wired in (cbbefc4), and the
        # allocator has always been told the session has 0 minutes left.
        #
        # hurdle() computes `time_frac = minutes_left / alloc_session_minutes`
        # and `time_mult = 1 + alloc_time_weight * time_frac`. At
        # minutes_left = 0 that is time_mult = 1.0, every cycle, forever —
        # so the "bar is HIGH at 09:20 because five hours of arrivals
        # remain, and LOW at 14:45 because an unspent slot earns nothing"
        # half of the hurdle's own docstring has never once operated. The
        # scarcity term still worked, which is why the bar still moved at
        # all and nothing looked obviously dead.
        minutes_left = max(st.minutes_to_squareoff or 0, 0)

        # swing_assignment()'s reservation field, built and unused since it was
        # written: `select()` never received a `field` kwarg, so swing has run
        # as a pure stopping rule its whole life — arrivals judged one at a
        # time, no memory of a better untriggered plan elsewhere in the field.
        # decide() already prices and sizes every watched candidate every
        # cycle (evaluate_candidates() just discarded the WAIT ones); this
        # reuses that, plus the allocator's own scoring, so an untriggered
        # plan is measured in the same edge units as a triggered one.
        from allocation import policies as swing_policies
        from allocation.scoring import swing_family
        triggered_syms = {p.symbol for p in props if p.framework == "SWING"}
        field = []
        for item in getattr(self, "_all_swing_decisions", []):
            c, d = item["candidate"], item["decision"]
            if d.symbol in triggered_syms:
                field.append({"symbol": d.symbol, "triggered": True})
                continue
            if None in (d.entry, d.stop, d.target) or not d.qty:
                continue   # decide() could not size it — excluded, not invented
            edge = self._allocator.score_hypothetical(
                d.symbol, d.entry, d.stop, d.target, d.qty, "CNC",
                swing_family(c.get("strategy")), native_rank=float(c.get("final_score") or 0.0))
            if edge is None:
                continue
            pt = swing_policies.p_trigger(d.dist_to_zone_pct, minutes_left, c.get("atr_pct"))
            if pt is None:
                continue
            field.append({"symbol": d.symbol, "triggered": False, "edge": edge, "p_trigger": pt})

        v = self._allocator.select(
            props, regime=mc.state,
            slots_by_framework=slots,
            max_slots_by_framework={"SWING": swing_max, "INTRADAY": intra_max},
            minutes_left=minutes_left,
            field=field,
            open_positions=self.positions)
        takes = sum(1 for x in v if x["verdict"] == "TAKE")

        # Keyed for the veto below. (symbol, product) is the same key
        # open_positions has been unique on since migration 028 — a bare symbol
        # would let a swing CNC tranche and an intraday MIS tranche of one name
        # collide, which is the corruption that key exists to prevent.
        self._verdicts = {(x["proposal"].symbol, x["proposal"].product): x for x in v}

        live_books = [b for b in ("intraday", "swing")
                      if cfg_bool(f"alloc_live_{b}", False)]
        mode = f"LIVE for {'+'.join(live_books)}" if live_books else "shadow"
        bars = {x["proposal"].framework: (x.get("hurdle_inputs") or {})
                for x in v}
        detail = " · ".join(
            f"{fw} slots {slots.get(fw, '?')} bar "
            + ("cold start" if (i or {}).get("cold_start")
               else f"{(i or {}).get('base')}"
                    + (" POOLED" if (i or {}).get("pooled_across_buckets") else "")
                    + (" FLOORED@0" if (i or {}).get("absolute_floor_applied") else ""))
            for fw, i in bars.items())
        logger.info(f"  allocator ({mode}): {len(v)} proposal(s) scored, "
                    f"{takes} to take, {len(v)-takes} refused — {detail}")

        # A BOOK THAT STANDS DOWN MUST SAY WHY, LOUDLY — 10-Aug-2026.
        #
        # The absolute edge floor can legitimately refuse an entire session's
        # proposals: if every candidate's net-of-cost expected R is negative,
        # taking none of them is the correct answer, not a malfunction. But
        # "correct" and "obvious from the console" are different properties,
        # and a quiet book has been misread as a broken one in this project
        # more than once. So when the floor is the thing doing the refusing,
        # the line says so in the operator's terms rather than leaving them to
        # infer it from a bar that reads 0.0.
        for fw, i in bars.items():
            if (i or {}).get("absolute_floor_applied") and not any(
                    x["verdict"] == "TAKE" and x["proposal"].framework == fw for x in v):
                logger.warning(
                    f"  {fw}: every proposal this cycle was refused by the ABSOLUTE "
                    f"EDGE FLOOR, not by a market judgement. The measured "
                    f"{fw.lower()} population is currently negative "
                    f"(unclamped bar {(i or {}).get('base')}), so every candidate's "
                    f"expected R is below its own round trip. Standing down is the "
                    f"correct answer to that — but if this persists for a full "
                    f"session, the prior is the thing to investigate "
                    f"(tools/expectancy_ledger.py), not the bar.")
        return v

    def allocator_permits(self, symbol: str, product: str, framework: str) -> tuple[bool, str]:
        """
        Does the allocator allow this entry? THE VETO.

        Until 05-Aug-2026 `alloc_live_*` set a column value in
        allocation_decisions and nothing else — the allocator scored, recorded,
        and the greedy path took whatever it always would have. Flipping the
        switch would have produced a system REPORTING itself as live-allocating
        while allocating nothing, which is the silent-success failure this
        project has hit four times. This is the consumer that makes the switch
        mean what it says.

        FAILS OPEN, DELIBERATELY. A proposal the allocator never saw — because
        scoring threw, or because the adapter could not express it — is allowed
        through. The allocator is an opportunity-cost optimiser layered on top
        of gates that have already said yes; it is not a safety control, and
        turning its absence into a refusal would make an allocator bug look
        exactly like a market with no opportunities. The gates that ARE safety
        controls (cost, structure, event, conviction, staleness) have all run
        before this point and none of them fails open.
        """
        if not cfg_bool(f"alloc_live_{framework.lower()}", False):
            return True, "allocator not live for this book"
        v = (getattr(self, "_verdicts", None) or {}).get((symbol, product))
        if v is None:
            return True, "no allocator verdict — failing open"
        if v["verdict"] == "TAKE":
            return True, v.get("reason") or "allocator selected it"

        # ── THE FLOOR MUST NOT CREATE AN ABSORBING STATE — 10-Aug-2026 ──────
        #
        # `alloc_edge_absolute_floor` refuses any proposal whose expected R
        # does not cover its own round trip. Correct for real capital. On a
        # PAPER book it is a trap, and the replay proved it: across 10
        # sessions the floored policy took ZERO trades.
        #
        # Zero trades means zero new TAKEN rows. `priors_intraday_taken_only`
        # builds every prior FROM TAKEN rows. So the prior freezes at whatever
        # it was the day the floor engaged, and no amount of engine
        # improvement can ever show up in it — the book cannot demonstrate it
        # got better because it is not allowed to try. Once negative, silent
        # forever. That is this project's own "a check that cannot pass"
        # failure, one level up: a GATE that cannot be cleared.
        #
        # The floor's purpose is to protect capital. A paper book has none.
        # So: LIVE books are blocked, PAPER books explore and are RECORDED.
        #
        # "BUT A SIMULATION MUST BEHAVE LIKE THE LIVE SYSTEM." It does, and
        # this is why the carve-out is honest rather than a fudge: BOTH
        # verdicts are written for every proposal — the allocator's DECLINE
        # goes to `allocation_decisions` exactly as it would live, and the
        # exploration trade goes to `intraday_setups`. So the live-equivalent
        # P&L ("what the floor would have saved") is computable exactly, at
        # any time, from data already on disk. The simulation measures BOTH
        # systems at once, which is strictly more information than measuring
        # the blocked one and learning nothing.
        #
        # `alloc_floor_blocks_paper` flips it back for anyone who wants the
        # paper book to mirror the live block exactly and accept the freeze.
        # ── THE WAIVER WAS UNRANKED, AND THAT WAS THE REAL DEFECT ───────────
        # 12-Aug-2026. The carve-out above was written to stop the floor
        # freezing the prior, and that reasoning still holds. What it got wrong
        # was its SCOPE: it waved through every floor-declined proposal in the
        # cycle, which discarded the allocator's edge ORDER and its
        # `slots_left` budget along with the floor. The allocator scored five
        # proposals at 09:30:28, ranked them, declined all five — and the paper
        # book opened all five. The book was not exploring; it had no selection
        # at all, and the day's whole budget went to arrival order.
        #
        # `floor_only_rank` (policies.intraday_stopping) is that same ranking,
        # computed in the same pass by the same rule against the pre-floor bar.
        # Permitting only ranked proposals means paper and live now run ONE
        # decision procedure with ONE ranking and ONE slot budget. The only
        # divergence left is whether the absolute floor binds, and it is capped
        # at the top `slots_left` rather than unbounded.
        #
        # WHY NOT SIMPLY MAKE THEM IDENTICAL. `alloc_floor_blocks_paper=true`
        # does exactly that and the operator may set it. The cost is measured,
        # not theoretical: scoring.intraday_priors() fetches every resolved row
        # in intraday_setups with NO time bound, so a prior that has gone
        # negative never ages out. Zero trades means zero new TAKEN rows means
        # the prior cannot move, and no engine improvement can ever reach it.
        # The honest fix for the divergence is to make the floor stop binding —
        # a positive prior — not to remove the only path that can produce one.
        floor_declined = ((v.get("hurdle_inputs") or {}).get("absolute_floor_applied")
                          and v["verdict"] != "DEFER")
        if floor_declined and not cfg_bool("alloc_floor_blocks_paper", False):
            from execution.gates import is_paper
            rank = v.get("floor_only_rank")
            if is_paper(framework) and rank is not None:
                return True, (f"below the absolute floor, but {framework} is PAPER "
                              f"and the allocator ranked it #{rank + 1} of the "
                              f"slots available — taken as EXPLORATION so the prior "
                              f"keeps learning; the DECLINE is recorded and the "
                              f"live-equivalent book excludes it")
            if is_paper(framework):
                return False, (v.get("reason") or "below the absolute floor")

        return False, v.get("reason") or f"allocator returned {v['verdict']}"

    def _record_setup(self, s, phase: str, cost_pct: float, verdict: str, qty: int,
                      mc_state: str | None = None) -> None:
        """
        Persist every setup DETECTED, including cost rejections.

        "How often did ORB fire and how did those resolve" is unanswerable if
        only taken trades are stored — and that question is the only way an
        engine's lifecycle state can ever be justified rather than guessed.

        `mc_state` RECORDS THE MARKET REGIME AT DETECTION — 11-Aug-2026, and
        the second attempt at this specific column. hurdle.py's own docstring
        already documents the first one: a query once tried to read a column
        `regime_at_detection` that no migration had added and no code had ever
        written, PostgREST rejected the whole request, and a bare `except`
        swallowed it — see migrations/000-ish history and hurdle.py's "THE BAR
        IS MEASURED IN THE SAME UNITS" section. This is that column, built the
        other way round: added by migration 068 FIRST, written HERE, so the
        column exists before anything tries to read it.

        Every call site passes its own local `mc` (a market_context.
        MarketContext, in scope at all twelve of them — evaluate_intraday_
        setups computes it once per cycle, _maybe_open_paper and
        act_on_setups both receive or unpack it). Optional and defaults to
        None rather than being required, so a call site that genuinely has no
        market context (there are none today, but a future one might) fails
        soft into an unclassified row rather than a crash.

        WHAT THIS ENABLES, NOT WHAT IT DOES. This function does not use
        mc_state to change anything — see allocation/scoring.py's
        regime_fit_multiplier(), which is the mechanism that eventually will,
        gated at weight 0.0 pending exactly the evidence this column exists
        to accumulate. Writing the data and using it are different
        commitments; this is only the first one.
        """
        if not self._setup_is_new(s, verdict):
            return
        # SCORED UNDER THE FAMILY, RECORDED WITH THE CONDITION.
        #
        # `strategy` is what the learning loop groups on, so writing the family
        # there is what actually raises detections-per-engine — ORB's 30 becomes
        # the family's 230 — and what makes a monthly verdict statistically
        # possible. The condition that fired is kept in meta.sub_engine, so
        # nothing is lost and a family can be split again on evidence.
        #
        # Falls back to the engine's own name when no family is set, so a
        # detection from before this change reads exactly as it did.
        family = s.meta.get("family") or s.strategy
        try:
            self.sb.table("intraday_setups").insert({
                "trade_date": today_ist().isoformat(),
                "symbol": s.symbol, "strategy": family, "phase": phase,
                "direction": s.direction, "entry": s.entry, "stop": s.stop,
                "target": s.target, "risk_pct": round(s.risk_pct, 3),
                "reward_pct": round(s.reward_pct, 3), "rr": round(s.rr, 2),
                "confidence": s.confidence, "rationale": s.rationale,
                "invalidation": s.invalidation, "cost_pct": cost_pct,
                "cost_verdict": verdict,
                "corroborated_by": ",".join(s.meta.get("corroborated_by") or []) or None,
                "meta": json.dumps({**s.meta, "qty": qty}, default=str),
                "regime_at_detection": mc_state,
            }).execute()
            self._recorded[f"{s.symbol}:{s.strategy}"] = (s.entry, verdict)
        except Exception as e:
            logger.debug(f"  setup record failed for {s.symbol}: {e}")

    # ── one cycle ───────────────────────────────────────────────────────────
    def cycle(self, prices: dict[str, float], sync_gtt: bool = False,
              feed=None) -> dict:
        # Live day range, volume and VWAP onto the contexts BEFORE anything
        # reads them. Cheap — a dict lookup per symbol over data the websocket
        # already delivered — and it is what makes a 10:47 breakout measured
        # against a 10:47 range instead of a 10:45 one. No-op unless quote mode
        # is on. `feed` is optional so existing callers keep working unchanged.
        if feed is not None:
            try:
                self.apply_live_quotes(feed)
            except Exception as e:
                # A failed overlay must leave the fetched context in place, not
                # take the cycle down. The staleness guard still applies.
                logger.debug(f"  live quote overlay skipped: {e}")
            try:
                self.merge_live_bars(feed)
            except Exception as e:
                # Same contract as the quote overlay above: a failed merge
                # leaves whatever bars refresh_contexts() last gave a symbol
                # in place rather than taking the cycle down.
                logger.debug(f"  live bar merge skipped: {e}")

        # Re-rank the intraday universe from live relative volume, every
        # cycle. Cheap (pure arithmetic over already-ticking quotes) and has
        # no immediate effect within THIS cycle — self._universe is only read
        # by watch_symbols()/context_symbols(), both consumed on the 300s
        # slow timer — so doing this here, before anything else, means the
        # slow timer always sees whatever the freshest 15s of ticks implied.
        self.rerank_universe_live(feed)

        pos_actions = self.evaluate_positions(prices)
        self.act_on_positions(pos_actions)

        entries = []
        if cfg_bool("intraday_watch_candidates", True):
            entries = self.evaluate_candidates(prices)

        # Dedicated intraday engines — separate from the swing candidate watch
        # above, which evaluates swing plans against live prices.
        setups = []
        try:
            setups = self.evaluate_intraday_setups(prices)
        except Exception as e:
            logger.warning(f"  intraday strategies failed: {e}")

        # ── ALLOCATION, OBSERVING ONLY ──────────────────────────────────────
        #
        # Placed AFTER both books have produced proposals and BEFORE either acts,
        # which is the only position from which it can see the same choice the
        # live path is about to make. Read-only with respect to everything the
        # live path subsequently reads, and wrapped so it cannot break the cycle.
        #
        # In shadow it records what it WOULD have chosen and changes nothing —
        # act_on_* below run exactly as they always have. That is not a
        # limitation, it is the evidence-gathering mode the promotion gate is
        # denominated in: >=30 scored DISAGREEMENTS with the greedy path, which
        # cannot be counted without a row per proposal the allocator refused.
        try:
            self._allocate_shadow(entries, setups)
        except Exception as e:
            logger.debug(f"  allocator skipped: {e}")

        # The live path, unchanged.
        if entries:
            self.act_on_candidates(entries)
        if setups:
            self.act_on_setups(setups)

        # SWING, SAID OUT LOUD.
        #
        # The console showed the intraday engine scan in detail and swing not at
        # all — so "why did it not buy" had no answer on screen, only in a
        # database. Both books are managed by this loop; both should report.
        try:
            self._log_swing_state(prices)
        except Exception as e:
            logger.debug(f"  swing summary failed: {e}")

        try:
            self._log_intraday_state(setups)
        except Exception as e:
            logger.debug(f"  intraday summary failed: {e}")

        self.square_off_paper(prices)

        gtt_result = None
        if sync_gtt and gtt_enabled():
            from execution import gtt_manager
            gtt_result = gtt_manager.sync(self.positions, prices, self.notifier)

        return {
            "positions": len(self.positions),
            "candidates": len(self.candidates),
            "position_actions": len(pos_actions),
            "entry_signals": len(entries),
            "intraday_setups": len(setups),
            "gtt": gtt_result,
            "phase": autonomy_phase(),
        }
