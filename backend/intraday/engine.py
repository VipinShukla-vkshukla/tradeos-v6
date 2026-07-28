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
                # Only tiered candidates are worth streaming. Subscribing to the
                # whole shortlist would multiply the tick load for names the
                # engine will never act on.
                tiers = {"TIER_1", "TIER_2"}
                self.candidates = [r for r in rows if (r.get("ai_tier") or "") in tiers]
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

        symbols = self.watch_symbols()
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
        Everything the feed must carry: open positions, swing candidates near
        their entry limit, and the intraday universe.

        Positions are non-negotiable — an unwatched position is an unmanaged
        stop. The other two are opportunity, and the cap in the scanner keeps
        the total inside the websocket and historical-data budget.
        """
        return sorted({p["symbol"] for p in self.positions if p.get("symbol")} |
                      {c["symbol"] for c in self.candidates if c.get("symbol")} |
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

            if orders_enabled() and cfg_bool("intraday_auto_exit", False):
                self._auto_exit(p, d, ltp)

    def _auto_exit(self, p: dict, d: dict, ltp: float) -> None:
        """
        Phase 3 exits. Gated separately from entries by intraday_auto_exit.

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
        if is_paper("INTRADAY") and (p.get("mode") or "").upper() == "PAPER":
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
        place(OrderRequest(p["symbol"], "SELL", qty, "LIMIT", limit,
                           reason=f"{d['action']}: {d['detail']}"),
              self.sb, self.notifier)

    def act_on_candidates(self, entries: list[dict]) -> None:
        for e in entries:
            c, d, ltp = e["candidate"], e["decision"], e["ltp"]
            approaching = e.get("state") == "APPROACHING"

            if approaching:
                # Distinct kind so the notifier's de-duplication treats
                # "closing in" and "now buyable" as separate states. Sharing a
                # kind would suppress the far more important second alert as a
                # restatement of the first.
                self.notifier.send(Action(
                    symbol=c["symbol"], kind="ENTRY_APPROACHING",
                    headline=(f"{e['gap_pct']:.1f}% above the buy limit "
                              f"₹{d.max_entry:.2f} — rest a limit now"),
                    detail=(f"Below ₹{d.max_entry:.2f} this is {d.min_rr_used:g}R:R or better "
                            f"(currently {d.rr_live:.2f}).\n"
                            f"Stop ₹{d.stop} · target ₹{d.target}"
                            + (f" · at the zone low it is {d.rr_at_zone_low:.2f}R:R"
                               if d.rr_at_zone_low else "")),
                    ltp=ltp, urgency="INFO",
                    meta={"tier": c.get("ai_tier"), "max_entry": d.max_entry,
                          "state": "APPROACHING"},
                ))
                continue

            self.notifier.send(Action(
                symbol=c["symbol"], kind="ENTRY",
                headline=d.headline,
                detail=(f"{d.reason}\n"
                        f"Stop ₹{d.stop} · target ₹{d.target}"
                        + (f" · {d.qty} sh ≈ ₹{d.invested:,.0f}, risk ₹{d.risk_amount:,.0f}"
                           if getattr(d, 'qty', None) else "")),
                ltp=ltp, urgency="NORMAL",
                meta={"tier": c.get("ai_tier"), "action": d.action},
            ))
            # Entries are never auto-placed at Phase 3 by this loop. Committing
            # new capital on a live tick, without the evening pipeline's full
            # context, is the highest-variance thing this system could do.

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

            # Size against the market state, then ask whether the trade still
            # survives its own costs at that size. A setup that only works at
            # full size on a CAUTION day is not a setup, it is leverage.
            budget = TOTAL_CAPITAL * mc.size_multiplier
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

    def act_on_setups(self, setups: list) -> None:
        for s in setups:
            st, qty, mc = s["setup"], s["qty"], s["market"]
            corro = st.meta.get("corroborated_by") or []
            # In PAPER mode, actually TAKE the setup. Without this the
            # simulation measures exits but never a full round trip, and a
            # system judged only on how it leaves trades tells you nothing
            # about which trades it should have entered.
            self._maybe_open_paper(st, qty, mc)

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
        if not is_paper("INTRADAY"):
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
            paper_broker.open_position(
                st.symbol, qty, f.fill_price,
                {"stop": st.stop, "target": st.target, "strategy": st.strategy},
                "INTRADAY", self.sb)
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
