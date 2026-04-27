"""
fetch_bulk_history_yf.py
========================
Drop-in replacement for fetch_bulk_history() in compute_indicators.py
Uses yfinance to fetch NSE EOD history for all symbols.

Strategy:
  1. Check price_history_yf table — count rows per symbol (lightweight)
  2. Symbols with >= MIN_SESSIONS_REQUIRED and fresh data → use cache
  3. Symbols with insufficient cache → fetch from yfinance
  4. Cache new yfinance data into price_history_yf for future runs
  5. Returns {symbol: [{close, low, high, date}, ...]} newest first

Cache query design:
  - Pass 1: fetch symbol+date only (tiny) to check sufficiency per symbol
  - Pass 2: fetch full OHLCV only for sufficient symbols
  - Chunked in batches of 100 symbols with explicit .limit() to avoid
    Supabase's silent row cap (~1000 default, 5000 with some clients)
"""

from datetime import datetime, timedelta
from collections import defaultdict

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False

MIN_SESSIONS_REQUIRED = 20   # minimum sessions to use cache
                              # ret_12m needs 240 but will be None for newer symbols — correct


def fetch_bulk_history_yf(sb, today: str, symbols: list,
                           days_back: int = 370,
                           cache_to_db: bool = True,
                           dry_run: bool = False) -> dict:
    """
    Fetch OHLC history for all symbols.
    Priority: price_history_yf (cached) -> yfinance (live fetch + cache)

    Args:
        sb          : Supabase client
        today       : date string YYYY-MM-DD
        symbols     : list of NSE symbol strings e.g. ['RELIANCE', 'TCS']
        days_back   : how many calendar days of history to fetch
        cache_to_db : if True, saves yfinance results to price_history_yf table
        dry_run     : if True, skips writing to price_history_yf

    Returns:
        {symbol: [{close, low, high, date}, ...]} newest first
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import logger

    cutoff     = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days_back + 100)).date())
    today_date = datetime.strptime(today, "%Y-%m-%d").date()
    result     = {}
    need_yf    = []

    # ── Step 1: Lightweight sufficiency check (symbol + date only) ────────
    logger.info(f"  fetch_bulk_history: checking price_history_yf cache ({len(symbols)} symbols)...")

    # Check sufficiency per symbol individually — avoids Supabase row cap
    # Only need: count of rows and most recent date per symbol
    sym_counts     = {}   # {symbol: row_count}
    sym_latest     = {}   # {symbol: most_recent_date_str}

    for i in range(0, len(symbols), 20):
        # Small chunks of 20 — each symbol gets ~319 rows, 20×319 = ~6380 well within cap
        chunk = symbols[i:i + 20]
        rows = (sb.table("price_history_yf")
                  .select("symbol,date")
                  .in_("symbol", chunk)
                  .gte("date", cutoff)
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(10000)
                  .execute().data)
        counts = defaultdict(int)
        latest = {}
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            counts[sym] += 1
            if sym not in latest:
                latest[sym] = r["date"]   # first = most recent (ordered desc)
        sym_counts.update(counts)
        sym_latest.update(latest)

    # Determine sufficient vs need_yf
    sufficient_syms = []
    need_yf_full    = []   # no base history — fetch full days_back
    need_yf_tail    = []   # has base history — only fetch last 7 days

    for sym in symbols:
        count      = sym_counts.get(sym, 0)
        latest_str = sym_latest.get(sym)
        if not latest_str or count < MIN_SESSIONS_REQUIRED:
            need_yf_full.append(sym)
        else:
            sufficient_syms.append(sym)
            need_yf_tail.append(sym)   # always refresh tail regardless of staleness

    need_yf = need_yf_full + need_yf_tail

    logger.info(
        f"  price_history_yf cache: {len(sufficient_syms)} have base | "
        f"{len(need_yf_tail)} tail-refresh | {len(need_yf_full)} full fetch"
    )

    logger.info(f"  price_history_yf cache: {len(sufficient_syms)} sufficient | {len(need_yf)} need yfinance")

    # ── Step 2: Fetch full OHLCV for sufficient symbols ───────────────────
    # Chunk size 20: 20 symbols × ~319 sessions = ~6380 rows per query
    # stays well within Supabase's effective row limit
    for i in range(0, len(sufficient_syms), 20):
        chunk = sufficient_syms[i:i + 20]
        rows = (sb.table("price_history_yf")
                  .select("symbol,date,open,high,low,close,volume")
                  .in_("symbol", chunk)
                  .gte("date", cutoff)
                  .lte("date", today)
                  .order("date", desc=True)
                  .limit(10000)
                  .execute().data)
        for r in rows:
            sym = r.get("symbol")
            if not sym:
                continue
            if sym not in result:
                result[sym] = []
            result[sym].append({
                "close": float(r["close"]) if r.get("close") else None,
                "low":   float(r["low"])   if r.get("low")   else None,
                "high":  float(r["high"])  if r.get("high")  else None,
                "date":  r.get("date"),
            })

    # ── Step 3: Fetch missing symbols from yfinance ───────────────────────
    # ── Step 3: Fetch missing/stale symbols from yfinance ─────────────────
    if need_yf and YF_AVAILABLE:
        logger.info(f"  Fetching {len(need_yf_full)} full + {len(need_yf_tail)} tail symbols from yfinance...")

        end_date    = str((datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)).date())
        tail_cutoff = str((datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).date())

        def _download_batch(syms, start):
            if not syms:
                return None, []
            tkrs = [f"{s}.NS" for s in syms]
            df = yf.download(
                tkrs,
                start=start,
                end=end_date,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            return df, tkrs

        rows_to_cache = []

        for batch_syms, raw_df, batch_tickers in [
            (need_yf_full, *_download_batch(need_yf_full, cutoff)),
            (need_yf_tail, *_download_batch(need_yf_tail, tail_cutoff)),
        ]:
            if raw_df is None or not batch_syms:
                continue

            for sym, ticker in zip(batch_syms, batch_tickers):
                try:
                    if len(batch_tickers) == 1:
                        sym_df = raw_df.copy()
                    else:
                        sym_df = (raw_df.xs(ticker, axis=1, level=1)
                                  if ticker in raw_df.columns.get_level_values(1)
                                  else None)

                    if sym_df is None or sym_df.empty:
                        logger.debug(f"  yfinance: no data for {sym}")
                        continue

                    sym_df = sym_df.dropna(subset=["Close"])
                    sym_df = sym_df.sort_index(ascending=False)

                    history = []
                    for date_idx, row in sym_df.iterrows():
                        date_str = str(date_idx.date())
                        entry = {
                            "close": round(float(row["Close"]), 4),
                            "low":   round(float(row["Low"]),   4),
                            "high":  round(float(row["High"]),  4),
                            "date":  date_str,
                        }
                        history.append(entry)

                        if cache_to_db and not dry_run:
                            rows_to_cache.append({
                                "symbol": sym,
                                "date":   date_str,
                                "open":   round(float(row["Open"]), 4),
                                "high":   entry["high"],
                                "low":    entry["low"],
                                "close":  entry["close"],
                                "volume": int(row["Volume"]) if row.get("Volume") else None,
                            })

                    # For tail-refresh, merge into existing result
                    if sym in result:
                        existing_dates = {r["date"] for r in result[sym]}
                        for h in history:
                            if h["date"] not in existing_dates:
                                result[sym].insert(0, h)   # prepend newest
                        result[sym].sort(key=lambda x: x["date"], reverse=True)
                    else:
                        result[sym] = history

                    logger.debug(f"  yfinance: {sym} — {len(history)} sessions")

                except Exception as e:
                    logger.warning(f"  yfinance: {sym} failed — {e}")

        # ── Step 4: Cache to price_history_yf ─────────────────────────────
        if cache_to_db and not dry_run and rows_to_cache:
            logger.info(f"  Caching {len(rows_to_cache)} rows to price_history_yf...")
            for i in range(0, len(rows_to_cache), 500):
                sb.table("price_history_yf").upsert(
                    rows_to_cache[i:i + 500],
                    on_conflict="symbol,date"
                ).execute()
            logger.info(f"  Cached successfully")
        elif dry_run and rows_to_cache:
            logger.info(f"  [DRY RUN] Skipping cache write of {len(rows_to_cache)} rows")

    elif need_yf and not YF_AVAILABLE:
        logger.warning(
            f"  yfinance not installed — {len(need_yf)} symbols will have no history. "
            f"Run: pip install yfinance"
        )

    total_sessions = sum(len(v) for v in result.values())
    yf_fetched     = len([s for s in need_yf if result.get(s)])
    logger.info(
        f"  fetch_bulk_history: {len(result)} symbols | "
        f"{total_sessions} total sessions | "
        f"{yf_fetched} fetched from yfinance"
    )

    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from config import get_supabase, today_ist, logger

    sb    = get_supabase()
    today = str(today_ist())

    # Get symbols from most recent chartink_raw_data
    latest = (sb.table("chartink_raw_data")
                .select("date")
                .order("date", desc=True)
                .limit(1)
                .execute().data)
    if not latest:
        logger.error("No data in chartink_raw_data")
        sys.exit(1)

    latest_date = latest[0]["date"]
    sym_rows    = (sb.table("chartink_raw_data")
                     .select("symbol")
                     .eq("date", latest_date)
                     .execute().data)
    symbols     = [r["symbol"] for r in sym_rows if r.get("symbol")]

    logger.info(f"Populating price_history_yf for {len(symbols)} symbols (date: {latest_date})...")
    result = fetch_bulk_history_yf(sb, today, symbols, cache_to_db=True, dry_run=False)
    logger.info(f"Done — {sum(len(v) for v in result.values())} total sessions cached")