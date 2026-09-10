"""
Stage D3 — the event-driven core, IN SHADOW ONLY.

    docs/TRADEOS_ROADMAP.md, Track D, Stage D3. Branch feat/intraday-event-
    core. Bounded shadow window: 10 trading sessions or 200 directly-
    comparable decisions, whichever comes first (Gate D3).

WHAT THIS IS, PRECISELY
------------------------
The existing loop (intraday/run.py) evaluates every watched symbol on a
FIXED 15-second timer (`intraday_eval_interval_s`), regardless of when a
symbol actually moved. `intraday/price_feed.py`'s own module docstring
says this outright: "TICKS UPDATE STATE; A TIMER DECIDES... It
deliberately does not call back into decision logic on every tick." A
breakout at second 3 of a 15-second window sits un-evaluated until the
next boundary — not a bug, a deliberate, documented trade-off, and this
stage exists to measure what a TICK-TRIGGERED alternative would have
looked like, side by side, before ever proposing to replace it.

THIS RUNS ON THE SAME THREAD AS EVERYTHING ELSE — A DELIBERATE CHOICE,
NOT AN OMISSION. A separate always-on worker thread was considered and
rejected: intraday/engine.py's mutable state (self._contexts, self._bench,
open positions) was never built for concurrent access, and introducing a
second thread that reads it would be a genuinely new class of bug this
project has never had to guard against. `check()` below is instead called
from intraday/run.py's own main loop, on its own tight interval
(`intraday_event_core_interval_s`, 2s default) — far tighter than the 15s
polling cycle, so the latency win this stage exists to measure is real
and immediate, without any of the concurrency risk a dedicated thread
would introduce. The ONLY genuine cross-thread boundary is PriceFeed's own
already-thread-safe dirty-symbol tracking (price_feed.py::drain_dirty()),
fed by the websocket's own thread exactly as `_px`/`_at` already are.

DECIDES NOTHING THAT WRITES ANYWHERE THE TRUSTED LOOP READS. Calls the
SAME `registry.evaluate_all()` the polling loop calls, on the SAME
`SymbolContext` objects (refreshed with the SAME `apply_live_quotes()`/
`merge_live_bars()` the polling loop already calls every cycle — reused,
not reimplemented). Every result is written to `intraday_event_shadow`
ONLY (migration 105) — never `intraday_setups`, never
`execution.paper_broker`, never `allocation.allocator`. A bug here can
pollute only its own shadow log.

THE ONE DELIBERATE EXCEPTION — IGN's FAST-ENTRY PATH, 08-Sep-2026
--------------------------------------------------------------------
`intraday_ign_fast_entry_enabled` (migration 132), scoped to the IGN
engine ONLY, the operator's own explicit instruction: a genuinely
violent circuit/volume-pump move can be gone before the ordinary 15s
cycle ever sees it. Investigated first, not assumed — `Allocator.
select()`'s own docstring says it is "pure arithmetic... microseconds,
no I/O", already runs every 15s cycle, not on a slower timer; the real
bottleneck was the cycle INTERVAL, not the allocator's own logic. So
this is the one place in the file where that changes: when the
symbol's `best` is IGN AND the switch is armed, `_try_ign_fast_entry()`
below calls `engine._evaluate_one_intraday_candidate()` — the EXACT
same pipeline (shortability with real stock/runway data, re-entry,
cross-framework, event risk, structure, AI advice, conviction, sizing,
liquidity, depth, cost) the ordinary loop runs for every candidate, for
the ONE symbol this tick just detected — then a single-proposal
allocator pass, then `engine._maybe_open_paper()`, the exact function
every other engine's TAKE already uses. No new decision logic exists
here; every gate is imported, not reimplemented. Every OTHER engine's
`best`/`found` still only ever reaches the shadow-log path below,
completely unchanged.

A SECOND EXCEPTION — THE IGN EXIT-LAG PROBE, 09-Sep-2026
--------------------------------------------------------------------
`intraday_ign_exit_lag_probe_enabled` (migration 135). Unlike the
fast-entry path above, this one CHANGES NOTHING about what happens to
any position — it is pure measurement, built to answer a real question
without guessing: does the ordinary 15s exit cycle actually cost
anything on a fast-moving IGN hold, and if so how much? This system
stores no tick-level price history, so that question is unanswerable
retroactively — it has to be instrumented BEFORE the trades it would
measure, or the evidence is gone. `_probe_ign_exit_lag()` below re-runs
`exit_policy.evaluate_intraday_exit()` — the EXACT function the
ordinary 15s loop uses, never a second implementation of the ladder —
against the freshest 2s tick, for any symbol that is a currently open
INTRADAY/IGN position. The first time it would return a real action
(not HOLD/TRAIL_SL), that moment is written once to `open_positions`
(`exit_lag_action`/`exit_lag_probe_at`) and never again for that
position. The REAL exit is still decided ONLY by the ordinary 15s
loop, exactly as before — this probe never calls close_position()
itself, never books a partial, never moves a stop. `close_position()`
computes `exit_lag_seconds` (probe time vs the real close time) so the
gap is queryable once enough IGN trades exist:
`SELECT avg(exit_lag_seconds) FROM closed_positions WHERE sub_engine=
'IGN' AND exit_lag_seconds IS NOT NULL`. See docs/FINDINGS.md,
09-Sep-2026.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_bool, cfg_int, today_ist


def check(engine, feed) -> int:
    """
    One shadow pass. Returns how many shadow detections were logged.

    ADVISORY ONLY, LIKE EVERY OTHER FUNCTION THAT TOUCHES LIVE STATE IN
    THIS MODULE — a failure anywhere in here must never take the caller
    down; the trusted polling loop's own cycle is what actually protects
    positions and enters trades, and this function runs alongside it,
    never in place of it.
    """
    if not cfg_bool("intraday_event_core_enabled", False):
        return 0
    if engine is None or feed is None:
        return 0

    try:
        dirty = feed.drain_dirty()
    except Exception as e:
        logger.debug(f"  event_core: dirty-symbol drain failed — {e}")
        return 0
    if not dirty:
        return 0

    # Reused, not reimplemented — the SAME two calls cycle() already makes
    # every 15s, run again here so a dirty symbol's context reflects the
    # tick that just marked it dirty, not whatever refresh_contexts() last
    # built on the slow timer.
    try:
        engine.apply_live_quotes(feed)
    except Exception as e:
        logger.debug(f"  event_core: live quote overlay skipped — {e}")
    try:
        engine.merge_live_bars(feed)
    except Exception as e:
        logger.debug(f"  event_core: live bar merge skipped — {e}")

    try:
        from intraday.session import session_state
        from intraday.strategies.registry import evaluate_all
        phase = session_state().phase
    except Exception as e:
        logger.debug(f"  event_core: session/registry unavailable — {e}")
        return 0

    detected_at = datetime.now(IST)
    trade_date = today_ist().isoformat()
    logged = 0

    for sym in dirty:
        ctx = (engine._contexts or {}).get(sym)
        if ctx is None:
            # No context yet (outside the top intraday_max_universe, or the
            # 300s slow tick has not built one for this symbol at all) —
            # nothing to evaluate against. Not an error; the polling loop
            # has this exact same limit.
            continue
        try:
            price = feed.get(sym)
            if price:
                ctx.ltp = float(price)
        except Exception as e:
            logger.debug(f"  event_core: price refresh failed for {sym} — {e}")

        # EXIT-LAG PROBE — see this module's own docstring, second
        # exception. A DIFFERENT set of symbols than best/found below (an
        # OPEN position, not a new detection), so it runs unconditionally
        # per dirty symbol rather than being folded into the found-setup
        # branch — most dirty symbols will not be an open IGN position at
        # all, and _ign_open_position()'s own lookup is the cheap way to
        # find out.
        if cfg_bool("intraday_ign_exit_lag_probe_enabled", True):
            try:
                _probe_ign_exit_lag(engine, sym, ctx, detected_at)
            except Exception as e:
                logger.debug(f"  event_core: exit-lag probe failed for {sym} — {e}")

        try:
            best, _all = evaluate_all(ctx, phase)
        except Exception as e:
            logger.debug(f"  event_core: evaluation failed for {sym} — {e}")
            continue
        if best is None:
            continue

        # IGN FAST-ENTRY — see this module's own docstring for the full
        # reasoning. Scoped by strategy name AND its own switch; every
        # other engine's best/found falls straight through to the
        # shadow-log path below, unchanged.
        if best.strategy == "IGN" and cfg_bool("intraday_ign_fast_entry_enabled", False):
            try:
                _try_ign_fast_entry(engine, sym, ctx, best, phase)
            except Exception as e:
                logger.debug(f"  event_core: IGN fast-entry failed for {sym} — {e}")

        try:
            engine.sb.table("intraday_event_shadow").insert({
                "trade_date":  trade_date,
                "symbol":      best.symbol,
                "strategy":    best.strategy,
                "sub_engine":  (best.meta or {}).get("sub_engine") or best.strategy,
                "direction":   best.direction,
                "entry":       best.entry,
                "stop":        best.stop,
                "target":      best.target,
                "confidence":  best.confidence,
                "rationale":   best.rationale,
                "detected_at": detected_at.isoformat(),
                "meta": json.loads(json.dumps(best.meta or {}, default=str)),
            }).execute()
            logged += 1
        except Exception as e:
            logger.debug(f"  event_core: shadow log failed for {sym} — {e}")

    return logged


def _try_ign_fast_entry(engine, sym: str, ctx, best, phase: str) -> None:
    """
    IGN only. Reuses `engine._evaluate_one_intraday_candidate()` — the
    EXACT pipeline the ordinary 15s loop runs for every candidate
    (shortability with REAL stock/runway data, re-entry, cross-framework,
    event risk, structure, AI advice, conviction, sizing, liquidity,
    depth, cost) — for the ONE symbol this tick just detected, then a
    single-proposal allocator pass, then the same paper-entry function
    every other engine's TAKE already uses.

    NEVER TOUCHES `engine._verdicts`. That dict is the ordinary 15s
    cycle's shared, cycle-scoped view — allocator_permits(), the swing
    alert-kind logic and the pick-label logic all read it assuming it
    reflects the full candidate set that cycle proposed. Scoring this
    ONE candidate through `engine._score_proposals()` directly (not
    `_allocate_shadow()`, which owns that assignment) keeps this call
    from corrupting that view between one 15s cycle and the next — see
    `_score_proposals()`'s own docstring for the full reasoning.

    Every refusal below is recorded exactly as the ordinary path records
    it — `_evaluate_one_intraday_candidate()` calls `_record_setup()`
    internally for each of its own gates, and this function does the same
    for the one gate that sits after it (the allocator) — so a fast-path
    refusal is exactly as auditable in `intraday_setups` as a slow-path
    one, not a second, invisible decision path.
    """
    from intraday.session import session_state
    from intraday import market_context as mkt
    from allocation.proposal import from_intraday
    from allocation.policies import TAKE

    if engine._held_by_framework(sym, "INTRADAY"):
        return

    st = session_state()
    if not st.can_enter:
        # Cannot actually happen if `best` exists at all — IGN's own
        # `phases` tuple is exactly session.TRADEABLE, the set that
        # defines can_enter — kept as a defensive mirror of the ordinary
        # path's own first gate, not because this is expected to fire.
        return
    mc = mkt.from_context(engine._index_ctx)
    shorts_live = cfg_bool("intraday_allow_shorts", False) and mc.allow_shorts

    # `[]` for runway_refused: the ordinary loop accumulates this across
    # every symbol in one cycle for its own end-of-cycle summary line: a
    # single fast-path call has no such summary to feed, and the
    # BLOCKED_SHORTABILITY row this function may still write below
    # carries the same "cover deadline" reason text either way.
    result = engine._evaluate_one_intraday_candidate(
        sym, ctx, best, mc, st, shorts_live, [])
    if not result:
        return

    proposal = from_intraday(result["setup"], result["qty"])
    if proposal is None:
        return
    v = engine._score_proposals([proposal])
    if not v or v[0].get("verdict") != TAKE:
        why = (v[0].get("reason") if v else "no allocator verdict") or "declined"
        engine._record_setup(
            result["setup"], result["phase"], result["cost_pct"],
            "ALLOCATOR_DECLINED", 0,
            mc_state=(result["market"].state if result["market"] else None))
        logger.info(f"      {sym}: IGN fast-entry — allocator declined — {why[:90]}")

        # THE BOUNDED BOOTSTRAP OVERRIDE — 08-Sep-2026, migration 133. Same
        # shape as act_on_setups()'s own wiring on the ordinary 15s loop,
        # same two engine methods, no duplicated counting logic — see that
        # function's own comment for the full reasoning. `best.strategy ==
        # "IGN"` is defensive here (event_core.check() already gates this
        # whole function on it), mirroring the ordinary path's own explicit
        # check rather than relying solely on the caller.
        bootstrap_slot = None
        if best.strategy == "IGN":
            used = engine._ign_bootstrap_used_count()
            cap = cfg_int("intraday_ign_exploration_trades", 10)
            if used < cap:
                bootstrap_slot = used + 1
                logger.info(f"      {sym}: IGN bootstrap-override slot "
                            f"{bootstrap_slot}/{cap} — proceeding despite "
                            f"the decline above")
        if bootstrap_slot is None:
            return
        opened_ok = engine._maybe_open_paper(
            result["setup"], result["qty"], result["market"],
            phase=result["phase"], cost_pct=result["cost_pct"],
            pick_label=None, bootstrap_override_slot=bootstrap_slot)
        if opened_ok:
            # Only a CONFIRMED write consumes the lifetime slot (constraint
            # 6) — a downstream failure must not.
            engine._consume_ign_bootstrap_slot()
        return

    engine._maybe_open_paper(
        result["setup"], result["qty"], result["market"],
        phase=result["phase"], cost_pct=result["cost_pct"],
        pick_label=v[0].get("pick_label"))
    logger.info(f"      {sym}: IGN fast-entry — TAKE, entering on the 2s loop "
                f"rather than waiting for the next 15s cycle")


def _probe_ign_exit_lag(engine, sym: str, ctx, now: datetime) -> None:
    """
    IGN only. See this module's own "A SECOND EXCEPTION" docstring section
    for the full reasoning — this is pure measurement, not a decision.

    Re-runs exit_policy.evaluate_intraday_exit() — the SAME pure function
    (documented "no I/O") the ordinary 15s loop uses via
    IntradayEngine.evaluate_positions() — against the freshest 2s tick,
    for the one open INTRADAY/IGN position in this symbol, if any. Writes
    NOTHING per tick: only the FIRST time a real action (not HOLD/
    TRAIL_SL) would fire does this write anything at all, and it writes
    exactly once — `_ign_open_position()`'s own `exit_lag_action` check is
    what stops every subsequent 2s pass from doing it again. Never calls
    close_position(), never books a partial, never moves a stop: the
    REAL exit is still decided only by the ordinary 15s loop, unchanged.
    """
    pos = engine._ign_open_position(sym)
    if pos is None or pos.get("exit_lag_action"):
        return
    from intraday.exit_policy import evaluate_intraday_exit, load_intraday_policy
    # IGN's own prospective policy, not the pooled one -- this function is
    # IGN-only already, and once IGN has its own management-rung override
    # (see docs/FINDINGS.md, 10-Sep-2026) the probe should measure against
    # what IGN would ACTUALLY run under, not a policy it may no longer use.
    policy = load_intraday_policy(engine="IGN")
    result = evaluate_intraday_exit(pos, ltp=ctx.ltp, policy=policy, now=now)
    action = result.get("action")
    if action in ("HOLD", "TRAIL_SL"):
        # TRAIL_SL is a tightening, not an exit -- not what this measures.
        return
    try:
        engine.sb.table("open_positions").update({
            "exit_lag_action": action,
            "exit_lag_probe_at": now.isoformat(),
        }).eq("symbol", sym).eq("product", pos.get("product") or "MIS").execute()
        # Kept in sync with the DB write so a second dirty tick for the
        # same symbol later in this same process sees it without needing
        # a fresh load_state() round trip.
        pos["exit_lag_action"] = action
        pos["exit_lag_probe_at"] = now.isoformat()
        logger.info(f"      {sym}: IGN exit-lag probe — {action} would fire "
                    f"now, ahead of the ordinary 15s cycle")
    except Exception as e:
        logger.debug(f"  event_core: exit-lag probe write failed for {sym} — {e}")
