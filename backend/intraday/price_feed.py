"""
Live prices — KiteTicker websocket, with quote polling as a fallback.

WHY A WEBSOCKET RATHER THAN POLLING
-----------------------------------
Polling /quote every 30s for 40 symbols is 1,200 REST calls a session and still
leaves a 30-second blind spot on every stop. The websocket pushes ticks as they
happen, costs one connection, and does not consume the REST rate limit at all.

WHY POLLING IS STILL HERE
-------------------------
A websocket is a stateful thing on a laptop: wifi drops, the machine sleeps,
Zerodha restarts a server. When it is not connected the system must degrade to
something that still works rather than to nothing — the failure mode that made
the old monitor useless was silently having no prices at all and skipping every
cycle while reporting success.

TICKS UPDATE STATE; A TIMER DECIDES
-----------------------------------
This class only maintains "latest price per symbol". It deliberately does not
call back into decision logic on every tick — see intraday/config.eval_interval_s.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_bool


class PriceFeed:
    """
    Thread-safe latest-price store fed by a websocket or a poller.

    `source` is always truthful about where the numbers came from, because a
    price that is 15 minutes stale and a price that is live must never be
    presented identically — that distinction is the difference between a valid
    stop decision and a wrong one.
    """

    def __init__(self, symbols: list[str]):
        self.symbols = sorted(set(symbols))
        self._px: dict[str, float] = {}
        self._at: dict[str, datetime] = {}
        self._quote: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._ticker = None
        self._connected = False
        self._token_to_symbol: dict[int, str] = {}
        self.source = "none"
        self.mode = "ltp"
        self._capture_quote = False

    # ── read side ───────────────────────────────────────────────────────────
    def get(self, symbol: str) -> float | None:
        with self._lock:
            return self._px.get(symbol)

    def all_prices(self) -> dict[str, float]:
        with self._lock:
            return dict(self._px)

    def quote(self, symbol: str) -> dict | None:
        """
        Live day range, volume and average price for one symbol, or None.

        None means "quote mode is off, or this symbol has not ticked yet", and
        the caller must fall back rather than treat absence as zero. A zero
        volume and an unknown volume are different facts and only one of them
        is a reason to refuse a setup.
        """
        with self._lock:
            q = self._quote.get(symbol)
            return dict(q) if q else None

    def age_seconds(self, symbol: str) -> float | None:
        with self._lock:
            at = self._at.get(symbol)
        return (datetime.now(IST) - at).total_seconds() if at else None

    def _set(self, symbol: str, price: float) -> None:
        with self._lock:
            self._px[symbol] = price
            self._at[symbol] = datetime.now(IST)

    @property
    def connected(self) -> bool:
        return self._connected

    def _refresh_capture_flags(self) -> bool:
        """
        Should the socket be carrying quote fields at all?

        True when the feature is on OR the parity harness is armed. Kept as an
        attribute rather than read inside on_ticks so the handler stays a pure
        in-memory update, and recomputed on the slow timer so ARMING PARITY
        MID-SESSION TAKES EFFECT — the first version captured this once at
        connect time, which meant arming it at 12:42 did nothing until the
        daemon was restarted the next morning.

        Returns True when the value changed, so the caller knows to re-issue
        set_mode.
        """
        want = (cfg_bool("intraday_quote_mode_range", False)
                or cfg_bool("intraday_quote_mode_vwap", False)
                or cfg_bool("intraday_quote_parity_log", False))
        changed = want != self._capture_quote
        self._capture_quote = want
        return changed

    # ── websocket ───────────────────────────────────────────────────────────
    def start_websocket(self) -> bool:
        """
        Connect and subscribe. Returns False if it could not start, in which
        case the caller should poll instead.
        """
        try:
            from kiteconnect import KiteTicker
            from config import KITE_API_KEY
            from kite.token_manager import get_access_token
            from kite import kite_client

            token = get_access_token()
            if not token:
                logger.warning("  price_feed: no Kite session — websocket unavailable")
                return False

            # The ticker speaks instrument tokens, not trading symbols.
            kite = kite_client.get_kite()
            if not kite:
                return False
            instruments = kite.ltp([f"NSE:{s}" for s in self.symbols])
            self._token_to_symbol = {
                int(v["instrument_token"]): k.split(":", 1)[1]
                for k, v in (instruments or {}).items()
            }
            if not self._token_to_symbol:
                logger.warning("  price_feed: no instrument tokens resolved")
                return False

            # Seed from the same call so decisions can be made immediately
            # instead of waiting for the first tick on a quiet symbol.
            for k, v in (instruments or {}).items():
                self._set(k.split(":", 1)[1], float(v.get("last_price") or 0))

            ticker = KiteTicker(KITE_API_KEY, token)

            # CAPTURE IS BROADER THAN CONSUMPTION, AND HAS TO BE.
            #
            # The first version tied capture to intraday_quote_mode alone, which
            # made the parity harness unable to ever collect anything: the fields
            # it compares are only captured once the switch it is meant to
            # justify is already on. Arming parity, running a session and finding
            # zero rows was not a quiet failure — it was a guarantee.
            #
            # So the socket subscribes in QUOTE mode when EITHER the feature or
            # the parity log wants it, and `apply_live_quotes` decides separately
            # whether anything acts on what was captured.
            self._refresh_capture_flags()

            def on_ticks(ws, ticks):
                # THE HANDLER DOES NO I/O AND NO LOGGING. Not a style
                # preference — this runs on the websocket thread for every tick
                # of ~95 symbols, and a single logger call or database write in
                # here would back the socket up behind it and make prices late
                # in exactly the fast market where lateness costs money.
                # In-memory writes under one lock, then return.
                for t in ticks:
                    sym = self._token_to_symbol.get(int(t.get("instrument_token", 0)))
                    px = t.get("last_price")
                    if not sym or not px:
                        continue
                    self._set(sym, float(px))
                    # A plain attribute read — no config lookup, no I/O. The flag
                    # is recomputed on the slow timer by _refresh_capture_flags.
                    if not self._capture_quote:
                        continue
                    ohlc = t.get("ohlc") or {}
                    # Only the fields present in this tick are written. QUOTE
                    # ticks carry ohlc and volume; a stray LTP tick does not,
                    # and overwriting a known day range with None would turn a
                    # live field back into an unknown one.
                    q = {}
                    if ohlc:
                        q["day_open"]  = ohlc.get("open")
                        q["day_high"]  = ohlc.get("high")
                        q["day_low"]   = ohlc.get("low")
                        q["prev_close"] = ohlc.get("close")
                    if t.get("volume_traded") is not None:
                        q["volume"] = t.get("volume_traded")
                    if t.get("average_traded_price") is not None:
                        q["vwap"] = t.get("average_traded_price")
                    if q:
                        q["at"] = datetime.now(IST)
                        with self._lock:
                            self._quote.setdefault(sym, {}).update(q)

            def on_connect(ws, response):
                ws.subscribe(list(self._token_to_symbol))
                # MODE_LTP carries only last price, so day range, volume and the
                # exchange's own average price came from a 300-second historical
                # refresh — a breakout at 10:47 judged against a range built at
                # 10:45. MODE_QUOTE carries all of them on the same socket.
                #
                # FULL is still not requested: it adds market depth, which
                # nothing here reads, at a large bandwidth cost.
                #
                # Behind a switch defaulting OFF because the specification
                # requires one session of dual logging before trusting parity —
                # see tools/quote_parity.
                if self._capture_quote:
                    ws.set_mode(ws.MODE_QUOTE, list(self._token_to_symbol))
                    self.mode = "quote"
                else:
                    ws.set_mode(ws.MODE_LTP, list(self._token_to_symbol))
                    self.mode = "ltp"
                self._connected = True
                self.source = "kite_ws"
                logger.success(f"  price_feed: websocket connected, "
                               f"{len(self._token_to_symbol)} symbols, mode={self.mode}")

            def on_close(ws, code, reason):
                self._connected = False
                logger.warning(f"  price_feed: websocket closed ({code}: {reason})")

            def on_error(ws, code, reason):
                self._connected = False
                logger.warning(f"  price_feed: websocket error ({code}: {reason})")

            ticker.on_ticks = on_ticks
            ticker.on_connect = on_connect
            ticker.on_close = on_close
            ticker.on_error = on_error

            # threaded=True keeps the reconnect loop off the decision thread.
            ticker.connect(threaded=True, disable_ssl_verification=False)
            self._ticker = ticker
            return True

        except Exception as e:
            logger.warning(f"  price_feed: websocket start failed — {e}")
            return False

    def stop(self) -> None:
        try:
            if self._ticker:
                self._ticker.close()
        except Exception:
            pass
        self._connected = False

    # ── fallback ────────────────────────────────────────────────────────────
    def poll_once(self) -> int:
        """
        Refresh every symbol over REST. Used when the websocket is down.

        Falls through Kite -> yfinance rather than giving up, because a delayed
        price still supports a stop decision while no price supports none. The
        source string records which, so nothing downstream can mistake a 15
        minute old print for a live one.
        """
        try:
            from analysis.trade_decision import fetch_live_prices
            prices, source = fetch_live_prices(self.symbols)
            for s, p in (prices or {}).items():
                self._set(s, float(p))
            if prices:
                self.source = source
            return len(prices or {})
        except Exception as e:
            logger.warning(f"  price_feed: poll failed — {e}")
            return 0

    def resubscribe(self, symbols: list[str]) -> None:
        """Track a changed watch list without tearing down the connection."""
        new = sorted(set(symbols))
        if new == self.symbols:
            return
        self.symbols = new
        if self._connected and self._ticker:
            try:
                from kite import kite_client
                kite = kite_client.get_kite()
                instruments = kite.ltp([f"NSE:{s}" for s in new]) if kite else {}
                tokens = {int(v["instrument_token"]): k.split(":", 1)[1]
                          for k, v in (instruments or {}).items()}
                self._token_to_symbol = tokens
                self._ticker.subscribe(list(tokens))
                # Resubscribing must not silently drop back to LTP. The
                # universe is rebuilt on the slow timer, so a mode reset here
                # would quietly disable quote fields mid-session and every
                # consumer would fall back without anything saying so.
                #
                # This is also where a mid-session change to either switch takes
                # effect: the daemon calls resubscribe on the same 300s timer
                # that re-reads config, so arming the parity log is live within
                # one slow tick rather than at the next restart.
                if self._refresh_capture_flags():
                    logger.info(f"  price_feed: switching to "
                                f"{'QUOTE' if self._capture_quote else 'LTP'} mode")
                self.mode = "quote" if self._capture_quote else "ltp"
                mode = (self._ticker.MODE_QUOTE if self.mode == "quote"
                        else self._ticker.MODE_LTP)
                self._ticker.set_mode(mode, list(tokens))
                logger.info(f"  price_feed: resubscribed to {len(tokens)} symbols")
            except Exception as e:
                logger.warning(f"  price_feed: resubscribe failed — {e}")
