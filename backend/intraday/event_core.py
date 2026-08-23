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
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_bool, today_ist


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
            best, _all = evaluate_all(ctx, phase)
        except Exception as e:
            logger.debug(f"  event_core: evaluation failed for {sym} — {e}")
            continue
        if best is None:
            continue
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
