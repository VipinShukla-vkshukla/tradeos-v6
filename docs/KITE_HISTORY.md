# Kite historical data folder

A local, resumable copy of everything Kite serves historically, kept outside the repository (default
`D:\kite_history`, override with `KITE_HISTORY_DIR` or `--root`). It exists so engines can be trained and
tested on the full record rather than on whichever days a one-off study happened to fetch.

Code: `backend/tools/kite_history/` (`store.py` layout, `download.py` the run, `universe.py` what to fetch,
`verify.py` integrity). Tests: `tests/test_kite_history.py`, `tests/test_kite_history_cli.py`.

## What Kite can and cannot give

| | |
|---|---|
| Finest resolution | 1-minute candles: open, high, low, close, volume (+ open interest for derivatives). |
| Not available | Ticks, market depth, order book, bid/ask, delivery %, corporate actions, news. |
| Reach | Minute candles: at most 60 days per request; how far back is decided per instrument by Kite (measured by `probe` and by the first backfill). |
| Expired contracts / options | Not served: history exists only for instruments still in the instrument master. Options (100,000+) are not in the default set. |
| Adjustment | Prices are as-traded. Splits, bonuses and demergers are NOT adjusted; `verify` lists the >25% one-day jumps. |

Other intervals (3/5/10/15/30/60 minute) are built from the minutes, not stored.

## Commands (from `backend/`)

```bash
python -m tools.kite_history probe --symbol RELIANCE      # how far back is each interval served?
python -m tools.kite_history universe                     # instrument dump + how many targets
python -m tools.kite_history backfill --interval day      # daily first: small, fast
python -m tools.kite_history backfill --interval minute   # the big one, most liquid names first
python -m tools.kite_history update --interval minute     # bring finished symbols forward
python -m tools.kite_history status                       # coverage, rows, size, errors
python -m tools.kite_history verify                       # integrity report (exit 1 on hard errors)
```

`backfill` and `update` are safe to interrupt and rerun: finished symbols are skipped, a half-walked symbol
writes nothing, and files are replaced atomically. They refuse to run Mon-Fri 08:50-15:45 IST (the live daemon
shares Kite's 3 requests/second historical limit; `--allow-market-hours` overrides), pace at one request per
0.4 s across all workers, and stop cleanly when the access token expires (07:30 IST daily). To resume:
refresh the token (`python -m kite.token_manager --login-url`, then `--exchange <request_token>`) and rerun the
same command.

## Layout

```
instruments/all_<date>.parquet             the full instrument master, dated
minute/<SEGMENT>/<SYMBOL>/<YYYY>.parquet   1-minute candles, one file per symbol per year
day/<SEGMENT>/<SYMBOL>.parquet             daily candles, one file per symbol
_state/progress.db, download.log
```

SEGMENT is `NSE` (mainboard equities and ETFs; the ~7,000 bond, SDL, SME and trade-to-trade rows in Kite's NSE
dump are excluded by default), `INDICES` (NSE indices) or `NFO-FUT` (current futures, with an `oi` column).
Columns: `ts` (Asia/Kolkata, the START of the candle), `open`, `high`, `low`, `close` (float64), `volume`
(int64), `oi` (int64, derivatives only). Parquet, zstd, byte-stream-split prices, delta-coded time and volume:
about 12 bytes per candle.

```python
from tools.kite_history.store import read
df = read(r"D:\kite_history", "minute", "NSE", "RELIANCE", start="2024-01-01", end="2024-03-31")
```

## Status of this folder

See the ledger entry in `docs/FINDINGS.md` dated with the first backfill for what was downloaded, the depth
Kite actually served, the size on disk, and the integrity report.
