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

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, cfg_bool, cfg_int, today_ist
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

    def watch_symbols(self) -> list[str]:
        return sorted({p["symbol"] for p in self.positions if p.get("symbol")} |
                      {c["symbol"] for c in self.candidates if c.get("symbol")})

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
                out.append({"candidate": c, "decision": d, "ltp": float(ltp)})
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
        from intraday.order_manager import OrderRequest, place
        qty = int(d.get("book_qty") or 0) or int(p.get("current_qty") or p.get("actual_qty") or 0)
        if qty <= 0:
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

    # ── one cycle ───────────────────────────────────────────────────────────
    def cycle(self, prices: dict[str, float], sync_gtt: bool = False) -> dict:
        pos_actions = self.evaluate_positions(prices)
        self.act_on_positions(pos_actions)

        entries = []
        if cfg_bool("intraday_watch_candidates", True):
            entries = self.evaluate_candidates(prices)
            self.act_on_candidates(entries)

        gtt_result = None
        if sync_gtt and gtt_enabled():
            from intraday import gtt_manager
            gtt_result = gtt_manager.sync(self.positions, prices, self.notifier)

        return {
            "positions": len(self.positions),
            "candidates": len(self.candidates),
            "position_actions": len(pos_actions),
            "entry_signals": len(entries),
            "gtt": gtt_result,
            "phase": autonomy_phase(),
        }
