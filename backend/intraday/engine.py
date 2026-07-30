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
from config import IST, get_supabase, cfg, cfg_bool, cfg_int, cfg_float, today_ist
from intraday.config import (is_market_open, gtt_enabled, orders_enabled,
                             autonomy_phase)
from intraday.notifier import Notifier, Action


class IntradayEngine:
    def __init__(self, sb=None, notifier: Notifier | None = None):
        self.sb = sb or get_supabase()
        self.notifier = notifier or Notifier(self.sb)
        self.positions: list[dict] = []
        self.candidates: list[dict] = []
        self._policy = None
        self._intraday_policy = None
        self._last_state: dict[str, str] = {}
        # Bar history per symbol, assembled once per cycle and shared by all
        # seven engines — seven engines each fetching their own history would
        # multiply API calls sevenfold for identical data, and would let two
        # engines disagree about the same bar.
        self._contexts: dict = {}
        self._index_ctx = None
        # The intraday universe, selected on its OWN criteria. Until this
        # existed the engines only saw swing shortlist names — stocks chosen for
        # a one-to-three-week thesis, which predicts almost nothing about
        # whether a name moves enough today to pay for a round trip.
        self._universe: list[str] = []
        # symbol:strategy -> (entry, verdict) already recorded this session.
        self._recorded: dict[str, tuple[float, str]] = {}
        # Event awareness and AI advice, both refreshed on the SLOW timer.
        # The AI in particular can never sit in the fast loop: step 19's
        # DeepSeek call took 88.6s measured, and this loop runs every 15s.
        self._news = None
        self._advice: dict = {}
        self._pending_review: list = []
        # Confidence of every intraday setup already alerted this session, for
        # the streaming top-N in _intraday_alert_worthy(). Session-scoped and in
        # memory on purpose: a restart SHOULD re-announce the current best,
        # since you have no way of knowing whether the earlier message was seen.
        self._alerted_conf: list = []

    # ── state ───────────────────────────────────────────────────────────────
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
        prev: dict = {}
        try:
            hist = (self.sb.table("stock_data_daily")
                      .select("symbol,date,high,low,close,atr_pct,volume")
                      .in_("symbol", symbols)
                      .order("date", desc=True).limit(len(symbols) * 3)
                      .execute().data or [])
            for r in hist:
                if r["symbol"] not in prev and str(r["date"]) < today.isoformat():
                    prev[r["symbol"]] = r
        except Exception as e:
            logger.debug(f"  contexts: prev-day levels unavailable — {e}")

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
        """
        return sorted({p["symbol"] for p in self.positions if p.get("symbol")} |
                      {c["symbol"] for c in self.candidates if c.get("symbol")} |
                      set(self._universe))

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
        try:
            from execution.gates import max_orders_per_day
            from execution.order_manager import _today_totals
            n, _mine, _all = _today_totals(self.sb, "INTRADAY")
            cap = max(1, max_orders_per_day("INTRADAY"))
            used = min(1.0, n / cap)
        except Exception:
            used = 0.0
        return round(base + (scarce - base) * used, 3)

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
        """Rebuild the intraday universe. Daily input, so refreshed on the slow timer."""
        if not cfg_bool("intraday_strategies_enabled", True):
            self._universe = []
            return 0
        try:
            from intraday import scanner
            self._universe = scanner.symbols(self.sb)
        except Exception as e:
            logger.warning(f"  universe refresh failed: {e}")
            self._universe = []
        return len(self._universe)

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
                                                  load_intraday_policy)
                if self._intraday_policy is None:
                    self._intraday_policy = load_intraday_policy()
                d = evaluate_intraday_exit(p, float(ltp), self._intraday_policy)
            else:
                d = evaluate_exit(p, float(ltp), held, self._policy)

            if d["action"] == "HOLD":
                continue
            actions.append({"position": p, "decision": d, "ltp": float(ltp)})
        return actions

    def evaluate_candidates(self, prices: dict[str, float]) -> list[dict]:
        """Run the shared entry decision against live prices."""
        from analysis.trade_decision import decide
        from config import TOTAL_CAPITAL

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
        for c in self.candidates:
            sym = c.get("symbol")
            ltp = prices.get(sym)
            if not sym or not ltp:
                continue
            if any(p.get("symbol") == sym for p in self.positions):
                continue  # already held
            d = decide(c, float(ltp), total_capital=TOTAL_CAPITAL,
                       open_positions=self.positions, regime=regime,
                       max_chase_pct=c.get("ai_max_chase_pct") or None,
                       available_cash=cash)
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
                    self.sb.table("open_positions").update({
                        "active_sl": d["new_sl"], "trail_activated": True,
                        "updated_at": datetime.now(IST).isoformat(),
                    }).eq("symbol", sym).execute()
                    p["active_sl"] = d["new_sl"]
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
        qty = int(d.get("book_qty") or 0) or int(p.get("current_qty") or p.get("actual_qty") or 0)
        if qty <= 0:
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
                    self.sb.table("open_positions").update({
                        "current_qty": max(0, left),
                        "partial_booked_qty": int(p.get("partial_booked_qty") or 0) + qty,
                        "partial_booked_price": ltp,
                    }).eq("symbol", p["symbol"]).execute()
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
                        "partial_booked_qty": int(p.get("partial_booked_qty") or 0) + qty,
                        "partial_booked_price": limit,
                    }
                    if d.get("new_sl"):
                        upd["active_sl"] = d["new_sl"]
                upd["updated_at"] = datetime.now(IST).isoformat()
                self.sb.table("open_positions").update(upd).eq("symbol", p["symbol"]).execute()
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
        if not sym or any(p.get("symbol") == sym for p in self.positions):
            return

        # Today's swing entries, counted from the BROKER LOG rather than from
        # this process, so a restart does not hand the day a fresh budget and
        # the laptop and the server cannot each take two.
        try:
            from execution.order_manager import _today_totals
            n_today, _mine, _all = _today_totals(self.sb, "SWING")
        except Exception:
            n_today = 0
        max_new = cfg_int("swing_max_new_per_day", 2)
        if n_today >= max_new:
            return

        # Rank against the whole day's field, not against arriving first.
        #
        # Without this the loop takes whichever plan reaches its limit earliest
        # in the session, and time-of-arrival is uncorrelated with quality. With
        # only two entries a day it is the worst available tie-breaker. A plan
        # outside today's top few is passed over — if it is still buyable later
        # and the better ones never triggered, it will be back.
        here = None
        try:
            from analysis.entry_ranking import score_plan, rank
            here = score_plan(c)
            field = rank(self.candidates)
            keep = min(max_new * 2, len(field))
            if keep and here.total < field[keep - 1].total:
                logger.info(f"  {sym}: rank {here.total:.0f} is outside today's top "
                            f"{keep} — holding the entry for a stronger plan")
                return
        except Exception as e:
            logger.debug(f"  ranking unavailable for {sym}: {e}")

        qty = int(getattr(d, "qty", 0) or 0)
        if qty <= 0:
            return
        rationale = f"rank {here.total:.0f} — {here.why()}" if here else None

        if not live:
            from execution import paper_broker
            allowed, why, _ = paper_broker.capacity("SWING", self.sb)
            if not allowed:
                logger.info(f"  📄 swing paper skip {sym} — {why}")
                return
            f = paper_broker.simulate_fill(sym, "BUY", qty, "LIMIT", ltp, ltp)
            if f.ok and paper_broker.open_position(
                    sym, qty, f.fill_price,
                    {"stop": d.stop, "target": d.target,
                     "strategy": c.get("strategy"), "entry_rationale": rationale},
                    "SWING", self.sb, charges=f.charges):
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

        res = place(OrderRequest(sym, "BUY", qty, "LIMIT", limit,
                                 reason=f"AUTO_ENTRY: {getattr(d, 'reason', '')[:120]}"),
                    self.sb, self.notifier, framework="SWING")
        if not (res and res.ok):
            return

        # Write it back IMMEDIATELY, for the reason the exit side learned the
        # hard way: an order placed and not recorded is re-derived and placed
        # again on the next cycle. PPLPHARMA sold twice that way.
        try:
            self.sb.table("open_positions").upsert({
                "symbol": sym, "mode": "LIVE", "framework": "SWING",
                "product": "CNC", "status": "ACTIVE",
                "entry_date": today_ist().isoformat(), "entry_price": limit,
                "actual_qty": qty, "current_qty": qty, "original_qty": qty,
                "invested_value": round(limit * qty, 2),
                "current_price": limit, "high_water_mark": limit,
                "planned_stop": d.stop, "active_sl": d.stop,
                "planned_target": d.target, "target_price": d.target,
                "sl_type": "AUTO_ENTRY", "strategy": c.get("strategy"),
                "entry_signal_type": c.get("signal_type"),
                "signal_id": c.get("id"), "signal_date": c.get("date"),
                "sector": c.get("sector"), "source": "auto_entry",
                "entry_rationale": rationale,
                "synced_at": datetime.now(IST).isoformat(),
            }, on_conflict="symbol").execute()
            self.load_state()
            logger.success(f"  🟢 AUTO-ENTRY {sym} {qty} @ ~{limit} "
                           f"(order {res.order_id}) — {rationale or 'no rank'}")
        except Exception as e:
            logger.error(f"  {sym}: BOUGHT {qty} but the position row could not be "
                         f"written — {e}. It may be bought again next cycle. "
                         f"Reconcile against the broker NOW.")

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
        from config import TOTAL_CAPITAL

        if not cfg_bool("intraday_strategies_enabled", True):
            return []

        st = session_state()
        if not st.can_enter:
            return []

        mc = mkt.from_context(self._index_ctx)
        if not mc.allow_longs:
            if self._last_state.get("_market") != mc.state:
                mkt.log_state(mc)
                self._last_state["_market"] = mc.state
            return []

        out = []
        for sym, ctx in (self._contexts or {}).items():
            if any(p.get("symbol") == sym for p in self.positions):
                continue
            ltp = prices.get(sym)
            if not ltp:
                continue
            ctx.ltp = float(ltp)

            best, _all = evaluate_all(ctx, st.phase)
            if not best:
                continue

            # Event gate BEFORE cost: a results-day setup is not a pricing
            # question, and computing a position size for a trade that must not
            # be taken wastes the check that matters.
            if self._news is not None:
                ev = self._news.check(best.symbol, ctx.sector)
                if not ev.allow:
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_EVENT", 0)
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
                    "INTRADAY", [b.high for b in ctx.bars], [b.low for b in ctx.bars])
                if not ok_s:
                    self._record_setup(best, st.phase, 0.0, "BLOCKED_STRUCTURE", 0)
                    continue
                best.meta["structure"] = st_struct.state

            # AI advice from the previous slow tick. Queue this setup for the
            # next review regardless, so a first sighting is never delayed
            # waiting for an opinion that takes 88 seconds to form.
            self._pending_review.append(best)
            from intraday import ai_advisor
            allow_ai, adj_conf, ai_note = ai_advisor.apply(best, self._advice)
            if not allow_ai:
                self._record_setup(best, st.phase, 0.0, "VETOED_AI", 0)
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
                self._record_setup(best, st.phase, 0.0, "BELOW_CONVICTION", 0)
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
            budget = min(TOTAL_CAPITAL * pos_pct * mc.size_multiplier,
                         max_order_value("INTRADAY"))
            qty = int(budget // best.entry) if best.entry else 0
            if qty <= 0:
                continue

            ok, why = is_worth_taking(best.entry, qty, best.target, best.stop)
            rt = round_trip(best.entry, qty)
            self._record_setup(best, st.phase, rt.pct_of_position,
                               "TAKEN" if ok else "REJECTED_COST", qty)
            if not ok:
                continue

            out.append({"setup": best, "qty": qty, "cost_pct": rt.pct_of_position,
                        "market": mc, "phase": st.phase, "cost_note": why})
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
            # In PAPER mode, actually TAKE the setup. Without this the
            # simulation measures exits but never a full round trip, and a
            # system judged only on how it leaves trades tells you nothing
            # about which trades it should have entered.
            self._maybe_open_paper(st, qty, mc)

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

    def _maybe_open_paper(self, st, qty: int, mc) -> None:
        """
        Simulate the entry, so the paper book contains real round trips.

        Gated on capacity as well as the usual rails: a simulation that opens
        forty positions tests nothing about a system that can hold five, and its
        results would not transfer to the account it exists to inform.
        """
        from execution.gates import is_paper
        # Two switches, mirroring control/paper_entry.py for swing.
        # intraday_auto_entry says whether setups are taken at all;
        # intraday_live_auto_entry says whether that may spend real money.
        # Both existed in system_config and NOTHING read them — the panel showed
        # them as on, and they did nothing, which is the same class of failure as
        # swing_auto_entry before it was wired.
        if not cfg_bool("intraday_auto_entry", True):
            return

        # How many NEW intraday positions today. Distinct from
        # intraday_max_orders_per_day, which caps ORDERS — the same distinction
        # swing needed, and for the same reason: one is a safety rail against a
        # runaway loop, the other is a decision about concentration.
        open_today = len([p for p in self.positions
                          if (p.get("framework") or "").upper() == "INTRADAY"])
        if open_today >= cfg_int("intraday_max_new_per_day", 4):
            return
        if not is_paper("INTRADAY"):
            if not cfg_bool("intraday_live_auto_entry", False):
                logger.info(f"  {st.symbol}: INTRADAY is LIVE and "
                            f"intraday_live_auto_entry is off — alerting only")
                return
            # Live auto-entry is deliberately not implemented here. Committing
            # capital on a single live tick is the highest-variance action this
            # system can take, and it is not one to enable by flipping a switch.
            logger.warning(f"  {st.symbol}: live auto-entry is not implemented — "
                           f"entries stay manual. Alerting only.")
            return
        try:
            from execution import paper_broker
            allowed, why, _left = paper_broker.capacity("INTRADAY", self.sb)
            if not allowed:
                logger.info(f"  📄 paper skip {st.symbol} — {why}")
                return
            if any(p.get("symbol") == st.symbol for p in self.positions):
                return
            f = paper_broker.simulate_fill(st.symbol, "BUY", qty, "LIMIT",
                                           st.entry, st.entry)
            if not f.ok:
                return
            from intraday.exit_policy import invalidation_level_from
            inv_level, inv_note = invalidation_level_from(st)
            paper_broker.open_position(
                st.symbol, qty, f.fill_price,
                {"stop": st.stop, "target": st.target, "strategy": st.strategy,
                 "invalidation_level": inv_level, "invalidation_note": inv_note},
                "INTRADAY", self.sb, charges=f.charges)
            # Reload so the same setup cannot be opened twice in one session and
            # so the exit engine sees it on the very next cycle.
            self.load_state()
        except Exception as e:
            logger.warning(f"  paper entry failed for {st.symbol}: {e}")

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
                continue
            if paper_broker.close_position(
                    p["symbol"], float(px), "SQUARE_OFF",
                    "intraday session ended — flat by design", self.sb):
                closed += 1
        if closed:
            logger.info(f"  📄 squared off {closed} paper intraday position(s)")
            self.load_state()
        return closed

    def _record_setup(self, s, phase: str, cost_pct: float, verdict: str, qty: int) -> None:
        """
        Persist every setup DETECTED, including cost rejections.

        "How often did ORB fire and how did those resolve" is unanswerable if
        only taken trades are stored — and that question is the only way an
        engine's lifecycle state can ever be justified rather than guessed.
        """
        if not self._setup_is_new(s, verdict):
            return
        try:
            self.sb.table("intraday_setups").insert({
                "trade_date": today_ist().isoformat(),
                "symbol": s.symbol, "strategy": s.strategy, "phase": phase,
                "direction": s.direction, "entry": s.entry, "stop": s.stop,
                "target": s.target, "risk_pct": round(s.risk_pct, 3),
                "reward_pct": round(s.reward_pct, 3), "rr": round(s.rr, 2),
                "confidence": s.confidence, "rationale": s.rationale,
                "invalidation": s.invalidation, "cost_pct": cost_pct,
                "cost_verdict": verdict,
                "corroborated_by": ",".join(s.meta.get("corroborated_by") or []) or None,
                "meta": json.dumps({**s.meta, "qty": qty}, default=str),
            }).execute()
            self._recorded[f"{s.symbol}:{s.strategy}"] = (s.entry, verdict)
        except Exception as e:
            logger.debug(f"  setup record failed for {s.symbol}: {e}")

    # ── one cycle ───────────────────────────────────────────────────────────
    def cycle(self, prices: dict[str, float], sync_gtt: bool = False) -> dict:
        pos_actions = self.evaluate_positions(prices)
        self.act_on_positions(pos_actions)

        entries = []
        if cfg_bool("intraday_watch_candidates", True):
            entries = self.evaluate_candidates(prices)
            self.act_on_candidates(entries)

        # Dedicated intraday engines — separate from the swing candidate watch
        # above, which evaluates swing plans against live prices.
        setups = []
        try:
            setups = self.evaluate_intraday_setups(prices)
            self.act_on_setups(setups)
        except Exception as e:
            logger.warning(f"  intraday strategies failed: {e}")

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
