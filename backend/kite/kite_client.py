"""
TradeOS v6 — Kite Connect Client
=================================
THIS MODULE DID NOT EXIST. Three files imported it:

    control/sl_monitor.py:59        from kite.kite_client import fetch_quotes
    control/execution_engine.py:19  from kite.kite_client import get_kite

sl_monitor caught the ImportError and returned an empty price dict, so its
main loop hit `if not prices: return` on every invocation — the stop-loss
monitor has never checked a single stop. execution_engine failed at import
time outright (it also imports get_config / get_config_int / check_kill_switch
from config, none of which exist).

Every function here returns an empty/None result rather than raising when the
broker session is unavailable. A lapsed token must degrade the system to
"advisory only", never take the evening pipeline down.

READ-ONLY BY DESIGN. Order placement stays in control/execution_engine.py
behind its own autonomy-phase and approval gates; nothing in this module can
move money.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import KITE_API_KEY

_kite = None
_warned_no_token = False


def get_kite(force_reload: bool = False):
    """
    Authenticated KiteConnect handle, or None when unavailable.

    Callers MUST handle None. The warning fires once per process rather than
    on every call — sl_monitor runs every 15 minutes and would otherwise fill
    the log with identical lines.
    """
    global _kite, _warned_no_token
    if _kite is not None and not force_reload:
        return _kite

    if not KITE_API_KEY:
        if not _warned_no_token:
            logger.warning("  Kite: KITE_API_KEY not set — broker features disabled")
            _warned_no_token = True
        return None

    from kite.token_manager import get_access_token
    token = get_access_token()
    if not token:
        if not _warned_no_token:
            logger.warning(
                "  Kite: no valid access token — broker features disabled. "
                "Run: python -m kite.token_manager --login-url"
            )
            _warned_no_token = True
        return None

    try:
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=KITE_API_KEY)
        k.set_access_token(token)
        _kite = k
        return _kite
    except Exception as e:
        logger.warning(f"  Kite: client init failed: {e}")
        return None


def _nse(symbols: list[str]) -> list[str]:
    return [f"NSE:{s}" for s in symbols if s]


def fetch_quotes(symbols: list[str]) -> dict[str, dict]:
    """
    Full quotes keyed by bare symbol.

    Returns {} on any failure. Kite caps a quote() call at 500 instruments;
    we chunk at 200 to stay clear of that and of URL-length limits.
    """
    kite = get_kite()
    if not kite or not symbols:
        return {}

    out: dict[str, dict] = {}
    try:
        for i in range(0, len(symbols), 200):
            chunk = symbols[i:i + 200]
            data = kite.quote(_nse(chunk))
            for key, q in data.items():
                sym = key.split(":", 1)[1] if ":" in key else key
                ohlc = q.get("ohlc") or {}
                out[sym] = {
                    "ltp":         float(q.get("last_price") or 0),
                    "open":        float(ohlc.get("open")  or 0),
                    "high":        float(ohlc.get("high")  or 0),
                    "low":         float(ohlc.get("low")   or 0),
                    "close":       float(ohlc.get("close") or 0),   # previous close
                    "volume":      int(q.get("volume") or 0),
                    "avg_price":   float(q.get("average_price") or 0),
                    "lower_circuit": float((q.get("lower_circuit_limit") or 0)),
                    "upper_circuit": float((q.get("upper_circuit_limit") or 0)),
                }
    except Exception as e:
        logger.warning(f"  Kite quote fetch failed: {e}")
        return out
    return out


def fetch_ltp(symbols: list[str]) -> dict[str, float]:
    """Last traded price only — cheaper than quote() when that is all you need."""
    kite = get_kite()
    if not kite or not symbols:
        return {}
    out: dict[str, float] = {}
    try:
        for i in range(0, len(symbols), 400):
            data = kite.ltp(_nse(symbols[i:i + 400]))
            for key, q in data.items():
                sym = key.split(":", 1)[1] if ":" in key else key
                out[sym] = float(q.get("last_price") or 0)
    except Exception as e:
        logger.warning(f"  Kite LTP fetch failed: {e}")
    return out


def fetch_holdings() -> list[dict]:
    """
    CNC/delivery holdings — the authoritative record of what a swing system
    actually owns. Normalised to the field names position_lifecycle uses.

    `quantity` excludes T1 (unsettled) stock, so a same-day buy shows
    quantity=0 / t1_quantity=N. Both are returned; the caller decides.
    Total ownership is quantity + t1_quantity.
    """
    kite = get_kite()
    if not kite:
        return []
    try:
        raw = kite.holdings() or []
    except Exception as e:
        logger.warning(f"  Kite holdings fetch failed: {e}")
        return []

    out = []
    for h in raw:
        qty = int(h.get("quantity") or 0)
        t1  = int(h.get("t1_quantity") or 0)
        if qty + t1 <= 0:
            continue                                   # fully exited, still listed
        out.append({
            "symbol":        h.get("tradingsymbol"),
            "exchange":      h.get("exchange"),
            "product":       h.get("product"),
            "quantity":      qty,
            "t1_quantity":   t1,
            "total_quantity": qty + t1,
            "average_price": float(h.get("average_price") or 0),
            "last_price":    float(h.get("last_price") or 0),
            "close_price":   float(h.get("close_price") or 0),
            "pnl":           float(h.get("pnl") or 0),
            "day_change_pct": float(h.get("day_change_percentage") or 0),
            "isin":          h.get("isin"),
        })
    return out


def fetch_positions() -> dict[str, list[dict]]:
    """
    Intraday/derivative positions. A CNC swing book normally lives entirely in
    holdings(), so this exists mainly to detect a same-day buy that has not yet
    become a holding, and to notice MIS positions the framework did not open.
    """
    kite = get_kite()
    if not kite:
        return {"net": [], "day": []}
    try:
        p = kite.positions() or {}
        return {"net": p.get("net", []) or [], "day": p.get("day", []) or []}
    except Exception as e:
        logger.warning(f"  Kite positions fetch failed: {e}")
        return {"net": [], "day": []}


def fetch_trades() -> list[dict]:
    """
    Today's executed trades. This is the ONLY place a true fill price and fill
    timestamp can be obtained — holdings gives a blended average and orders
    gives an average across fills. Used to stamp exact entry/exit prices onto
    open_positions and closed_positions.

    Kite exposes trades for the CURRENT DAY only. If the reconciler misses a
    day, that day's exact fills are unrecoverable and position_lifecycle falls
    back to the holdings average.
    """
    kite = get_kite()
    if not kite:
        return []
    try:
        raw = kite.trades() or []
    except Exception as e:
        logger.warning(f"  Kite trades fetch failed: {e}")
        return []

    out = []
    for t in raw:
        ts = t.get("fill_timestamp") or t.get("exchange_timestamp") or t.get("order_timestamp")
        out.append({
            "trade_id":         t.get("trade_id"),
            "order_id":         t.get("order_id"),
            "symbol":           t.get("tradingsymbol"),
            "exchange":         t.get("exchange"),
            "product":          t.get("product"),
            "transaction_type": t.get("transaction_type"),     # BUY | SELL
            "quantity":         int(t.get("quantity") or 0),
            "price":            float(t.get("average_price") or t.get("price") or 0),
            "timestamp":        ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
        })
    return out


def fetch_orders() -> list[dict]:
    """Today's order book, including rejections — surfaced so a rejected exit
    is visible rather than silently leaving a position open."""
    kite = get_kite()
    if not kite:
        return []
    try:
        raw = kite.orders() or []
    except Exception as e:
        logger.warning(f"  Kite orders fetch failed: {e}")
        return []

    out = []
    for o in raw:
        ts = o.get("order_timestamp")
        out.append({
            "order_id":         o.get("order_id"),
            "status":           o.get("status"),
            "symbol":           o.get("tradingsymbol"),
            "transaction_type": o.get("transaction_type"),
            "product":          o.get("product"),
            "quantity":         int(o.get("quantity") or 0),
            "filled_quantity":  int(o.get("filled_quantity") or 0),
            "pending_quantity": int(o.get("pending_quantity") or 0),
            "price":            float(o.get("price") or 0),
            "average_price":    float(o.get("average_price") or 0),
            "trigger_price":    float(o.get("trigger_price") or 0),
            "status_message":   o.get("status_message"),
            "tag":              o.get("tag"),
            "timestamp":        ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
        })
    return out


def fetch_margins() -> dict:
    """Available equity cash — feeds position sizing so the system cannot
    propose a position larger than deployable capital."""
    kite = get_kite()
    if not kite:
        return {}
    try:
        m = kite.margins(segment="equity") or {}
        return {
            "available_cash": float((m.get("available") or {}).get("live_balance") or 0),
            "opening_balance": float((m.get("available") or {}).get("opening_balance") or 0),
            "utilised_debits": float((m.get("utilised") or {}).get("debits") or 0),
            "net":             float(m.get("net") or 0),
        }
    except Exception as e:
        logger.warning(f"  Kite margins fetch failed: {e}")
        return {}


def is_available() -> bool:
    """True when a usable broker session exists. Cheap — no network call."""
    return get_kite() is not None
