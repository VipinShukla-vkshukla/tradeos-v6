"""
TradeOS v6 — Phase 2+: Kite Connect Client
Wrapper around Zerodha Kite Connect API.
Provides: historical data, live quotes, positions, order placement.

IMPORTANT: Requires manual token refresh once daily.
Run: python kite/kite_token_refresh.py to generate today's token.
"""
import sys
import os
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import KITE_API_KEY, KITE_API_SECRET, KITE_ACCESS_TOKEN, DATA, logger

_kite = None


def get_kite():
    """Get authenticated Kite instance. Raises if not configured."""
    global _kite
    if _kite is not None:
        return _kite

    if not KITE_API_KEY:
        raise RuntimeError("KITE_API_KEY not set in .env — Phase 2 not yet activated")

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=KITE_API_KEY)
        token = KITE_ACCESS_TOKEN or _load_token_from_file()
        if not token:
            raise RuntimeError(
                "KITE_ACCESS_TOKEN not set. Run: python kite/kite_token_refresh.py"
            )
        kite.set_access_token(token)
        _kite = kite
        logger.info("Kite Connect authenticated")
        return _kite
    except ImportError:
        raise RuntimeError("kiteconnect package not installed. Run: pip install kiteconnect")


def _load_token_from_file() -> Optional[str]:
    token_file = DATA / "kite_token.txt"
    if token_file.exists():
        content = token_file.read_text().strip()
        # Check if token was generated today
        date_file = DATA / "kite_token_date.txt"
        if date_file.exists():
            token_date = date_file.read_text().strip()
            if token_date == date.today().isoformat():
                return content
        logger.warning("Kite token is stale — run kite_token_refresh.py")
    return None


def fetch_historical(
    symbol: str,
    from_date: date,
    to_date:   date,
    interval:  str = "day",
) -> list[dict]:
    """
    Fetch historical OHLCV from Kite.
    interval: minute, 3minute, 5minute, 15minute, 30minute, 60minute, day
    """
    try:
        kite = get_kite()
        # Get instrument token for symbol
        token = _get_instrument_token(kite, symbol)
        if not token:
            logger.warning(f"Instrument token not found for {symbol}")
            return []

        records = kite.historical_data(
            instrument_token = token,
            from_date        = datetime.combine(from_date, datetime.min.time()),
            to_date          = datetime.combine(to_date,   datetime.max.time()),
            interval         = interval,
        )
        result = []
        for r in records:
            result.append({
                "date":   r["date"].date().isoformat() if hasattr(r["date"], "date") else str(r["date"])[:10],
                "symbol": symbol,
                "open":   r.get("open"),
                "high":   r.get("high"),
                "low":    r.get("low"),
                "close":  r.get("close"),
                "volume": r.get("volume"),
            })
        return result
    except Exception as e:
        logger.error(f"Kite historical fetch failed for {symbol}: {e}")
        return []


def fetch_quotes(symbols: list[str]) -> dict:
    """
    Fetch real-time LTP for a list of symbols.
    Returns: {symbol: {"ltp": float, "change": float, "change_pct": float}}
    """
    try:
        kite = get_kite()
        # Format: "NSE:SBIN"
        instruments = [f"NSE:{sym}" for sym in symbols]
        quotes = kite.quote(instruments)
        result = {}
        for instrument, data in quotes.items():
            sym = instrument.replace("NSE:", "")
            result[sym] = {
                "ltp":        data.get("last_price"),
                "change":     data.get("net_change"),
                "change_pct": data.get("change"),
                "volume":     data.get("volume"),
                "oi":         data.get("oi"),
            }
        return result
    except Exception as e:
        logger.error(f"Kite quotes failed: {e}")
        return {}


def fetch_positions() -> list[dict]:
    """Fetch live portfolio positions from Kite."""
    try:
        kite = get_kite()
        pos  = kite.positions()
        day_pos = pos.get("day", []) + pos.get("net", [])
        result = []
        for p in day_pos:
            if p.get("quantity", 0) == 0:
                continue
            result.append({
                "symbol":       p.get("tradingsymbol"),
                "quantity":     p.get("quantity"),
                "avg_price":    p.get("average_price"),
                "last_price":   p.get("last_price"),
                "pnl":          p.get("pnl"),
                "product":      p.get("product"),
            })
        return result
    except Exception as e:
        logger.error(f"Kite positions fetch failed: {e}")
        return []


def _get_instrument_token(kite, symbol: str) -> Optional[int]:
    """Get NSE instrument token for a symbol. Cached in data/instruments.json."""
    import json
    cache_file = DATA / "instruments.json"

    instruments_map = {}
    if cache_file.exists():
        try:
            instruments_map = json.loads(cache_file.read_text())
            if symbol in instruments_map:
                return instruments_map[symbol]
        except Exception:
            pass

    # Refresh instruments list
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            instruments_map[inst["tradingsymbol"]] = inst["instrument_token"]
        cache_file.write_text(json.dumps(instruments_map))
        return instruments_map.get(symbol)
    except Exception as e:
        logger.error(f"Instruments fetch failed: {e}")
        return None
