"""
Market-hours daemon — the entrypoint.

    python -m intraday.run                 # run until the close
    python -m intraday.run --once          # one cycle, then exit (for testing)
    python -m intraday.run --dry           # never write to the broker
    python -m intraday.run --status        # show phase, gates and watch list

WHY MARKET HOURS AND NOT 24/7
-----------------------------
NSE equities trade 09:15-15:30 Mon-Fri: 31 hours of the 168 in a week. Outside
them there are no ticks, so a 24/7 loop would hold a broker session it cannot
use, and write rows describing nothing. It also would not help: the Kite token
expires daily at 07:30 regardless, so continuous running does not remove the one
manual step in the day.

Running continuously would also QUIETLY BREAK the point-in-time guarantee.
signal_output_daily is keyed (date, symbol) and upserted, so a second run on the
same day overwrites the first. The immutable daily snapshot is a property of
running once per day; it is not something more frequency improves.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, is_kill_switch_active
from intraday.config import (is_trading_session, is_market_open, is_holiday,
                             autonomy_phase, gtt_enabled, orders_enabled,
                             eval_interval_s, gtt_sync_interval_s, poll_interval_s)
from intraday.engine import IntradayEngine
from intraday.notifier import Notifier
from intraday.price_feed import PriceFeed


def _banner() -> None:
    phase = autonomy_phase()
    logger.info("=" * 70)
    logger.info(f"TradeOS Intraday | phase {phase} | "
                f"GTT {'ON' if gtt_enabled() else 'off'} | "
                f"orders {'ON' if orders_enabled() else 'off'}")
    if orders_enabled():
        from intraday.config import max_order_value, max_orders_per_day, max_notional_per_day
        logger.warning(f"  ORDERS ARE LIVE — caps: ₹{max_order_value():,.0f}/order, "
                       f"{max_orders_per_day()}/day, ₹{max_notional_per_day():,.0f}/day notional")
    logger.info("=" * 70)


def status() -> dict:
    sb = get_supabase()
    eng = IntradayEngine(sb)
    eng.load_state()
    st = {
        "phase": autonomy_phase(),
        "gtt_enabled": gtt_enabled(),
        "orders_enabled": orders_enabled(),
        "kill_switch": is_kill_switch_active(),
        "trading_session": is_trading_session(),
        "market_open": is_market_open(),
        "positions": [p["symbol"] for p in eng.positions],
        "candidates": [c["symbol"] for c in eng.candidates],
    }
    for k, v in st.items():
        logger.info(f"  {k:<16}: {v}")
    return st


def main(once: bool = False, dry: bool = False) -> None:
    if dry:
        import os
        os.environ["DRY_RUN"] = "True"

    _banner()
    sb = get_supabase()
    notifier = Notifier(sb)
    engine = IntradayEngine(sb, notifier)

    if is_holiday(sb):
        logger.info("NSE holiday — nothing to do")
        return

    engine.load_state()
    symbols = engine.watch_symbols()
    if not symbols:
        logger.warning("No positions and no tiered candidates — nothing to watch")
        if not once:
            return

    engine.refresh_contexts()
    logger.info(f"Watching {len(symbols)} symbols: {', '.join(symbols[:12])}"
                + (f" +{len(symbols)-12} more" if len(symbols) > 12 else ""))

    feed = PriceFeed(symbols)
    if not feed.start_websocket():
        logger.warning("  Falling back to REST polling — prices refresh every "
                       f"{poll_interval_s()}s instead of continuously")

    last_eval = 0.0
    last_gtt = 0.0
    last_state = 0.0
    last_beat = 0.0
    last_poll = 0.0

    try:
        while True:
            now = time.time()

            if not once and not is_trading_session():
                logger.info("Outside the trading session — shutting down")
                break

            if is_kill_switch_active():
                logger.error("Kill switch active — intraday daemon stopping")
                break

            # Poll only when the websocket is not carrying the load.
            if not feed.connected and now - last_poll >= poll_interval_s():
                feed.poll_once()
                last_poll = now

            if now - last_eval >= eval_interval_s() or once:
                prices = feed.all_prices()
                if prices:
                    sync = (now - last_gtt >= gtt_sync_interval_s())
                    result = engine.cycle(prices, sync_gtt=sync)
                    if sync:
                        last_gtt = now
                    if result["position_actions"] or result["entry_signals"]:
                        logger.info(f"  cycle: {result}")
                else:
                    logger.warning("  no prices yet — skipping evaluation")
                last_eval = now

            # Reload positions/candidates periodically so a fill reported through
            # Telegram, or a manual exit, is picked up without a restart.
            if now - last_state >= 300:
                engine.load_state()
                feed.resubscribe(engine.watch_symbols())
                # Bars refresh on the same slow timer: minute candles change
                # once a minute, and the historical endpoint is rate-limited far
                # more tightly than quotes.
                engine.refresh_contexts()
                last_state = now

            if now - last_beat >= 900:
                notifier.heartbeat(
                    f"{len(engine.positions)} positions, {len(engine.candidates)} candidates, "
                    f"prices via {feed.source}"
                )
                last_beat = now

            if once:
                break
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
    finally:
        feed.stop()
        logger.info("Intraday daemon stopped")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TradeOS v6 — Intraday monitor")
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--dry", action="store_true", help="force DRY_RUN, no broker writes")
    ap.add_argument("--status", action="store_true", help="print gates and watch list")
    a = ap.parse_args()
    if a.status:
        status()
    else:
        main(once=a.once, dry=a.dry)
