# TradeOS v6 — SQL Migrations + New Script Stubs
**Reference document · Companion to DEPLOYMENT_README_v3_0.md**

---

## SQL MIGRATIONS — RUN IN ORDER

### Block 1: Phase 1 Gap Fixes (Apply Now)

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- G1: 9 ML feature columns in signal_log
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.signal_log
  ADD COLUMN IF NOT EXISTS rsi_daily     FLOAT,
  ADD COLUMN IF NOT EXISTS rsi_weekly    FLOAT,
  ADD COLUMN IF NOT EXISTS adx           FLOAT,
  ADD COLUMN IF NOT EXISTS vol_ratio     FLOAT,
  ADD COLUMN IF NOT EXISTS delivery_pct  FLOAT,
  ADD COLUMN IF NOT EXISTS atr_pct       FLOAT,
  ADD COLUMN IF NOT EXISTS ret_6m        FLOAT,
  ADD COLUMN IF NOT EXISTS dist_sma50    FLOAT,
  ADD COLUMN IF NOT EXISTS days_in_list  INT;

-- ─────────────────────────────────────────────────────────────────────────────
-- G4: ai_strategy_validation in signal_log
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.signal_log
  ADD COLUMN IF NOT EXISTS ai_strategy_validation TEXT;

-- ─────────────────────────────────────────────────────────────────────────────
-- G7: lessons quality columns
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.lessons
  ADD COLUMN IF NOT EXISTS confidence     NUMERIC DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS times_applied  INT     DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_worked   INT     DEFAULT 0,
  ADD COLUMN IF NOT EXISTS is_active      BOOLEAN DEFAULT true,
  ADD COLUMN IF NOT EXISTS linked_symbols TEXT[]  DEFAULT '{}';

UPDATE public.lessons SET confidence = 0.7 WHERE source = 'MANUAL';
UPDATE public.lessons SET confidence = 0.5 WHERE source LIKE 'AI:%' OR source IS NULL;

-- ─────────────────────────────────────────────────────────────────────────────
-- G16: global_cues change % + S&P500 (confirm applied — was done prev session)
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.global_cues
  ADD COLUMN IF NOT EXISTS us_dow_chg_pct    NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS us_nasdaq_chg_pct NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_close       NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_chg_pct     NUMERIC NULL;

NOTIFY pgrst, 'reload schema';
```

### Block 2: Phase 2 New Tables

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- safety_lists: ASM / GSM / F&O ban
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.safety_lists (
  date           DATE PRIMARY KEY,
  asm_symbols    TEXT[]   DEFAULT '{}',
  gsm_symbols    TEXT[]   DEFAULT '{}',
  fo_ban_symbols TEXT[]   DEFAULT '{}',
  asm_count      INT      DEFAULT 0,
  gsm_count      INT      DEFAULT 0,
  fo_ban_count   INT      DEFAULT 0,
  fetched_at     TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- data_anomalies: daily quality check log
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.data_anomalies (
  id          BIGSERIAL PRIMARY KEY,
  date        DATE,
  check_name  TEXT,
  severity    TEXT,   -- 'OK' | 'WARN' | 'ERROR'
  value       TEXT,
  message     TEXT,
  affected    TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- stock_data_daily: Kite price + ML predicted_regime columns
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.stock_data_daily
  ADD COLUMN IF NOT EXISTS kite_price        NUMERIC,
  ADD COLUMN IF NOT EXISTS predicted_regime  TEXT;

NOTIFY pgrst, 'reload schema';
```

### Block 3: Phase 3 New Tables

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- shadow_trades: paper trade log (2-week mandatory before live)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.shadow_trades (
  id            BIGSERIAL PRIMARY KEY,
  signal_id     BIGINT REFERENCES signal_log(id),
  symbol        TEXT,
  strategy      TEXT,
  action        TEXT,       -- 'APPROVED' | 'REJECTED' | 'DEFERRED'
  entry_price   NUMERIC,
  qty           INT,
  approved_at   TIMESTAMPTZ,
  would_execute BOOLEAN     DEFAULT false,
  notes         TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- signal_log: execution tracking columns
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE public.signal_log
  ADD COLUMN IF NOT EXISTS kite_order_id   TEXT,
  ADD COLUMN IF NOT EXISTS execution_price NUMERIC,
  ADD COLUMN IF NOT EXISTS executed_at     TIMESTAMPTZ;

NOTIFY pgrst, 'reload schema';
```

### Block 4: Phase 4 New Tables

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- discovery_proposals: ML statistical discoveries from chartink history
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.discovery_proposals (
  id                 BIGSERIAL PRIMARY KEY,
  week_of            DATE,
  feature_name       TEXT,
  correlation        NUMERIC,
  sample_size        INT,
  win_rate_with      NUMERIC,
  win_rate_without   NUMERIC,
  evidence           TEXT,
  status             TEXT DEFAULT 'PENDING',  -- 'PENDING' | 'INCORPORATED' | 'REJECTED'
  created_at         TIMESTAMPTZ DEFAULT now()
);

-- ─────────────────────────────────────────────────────────────────────────────
-- evolution_proposals: AI strategy change proposals (verify exists)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.evolution_proposals (
  id                BIGSERIAL PRIMARY KEY,
  week_of           DATE,
  proposal_type     TEXT,      -- 'PARAMETER_CHANGE' | 'STRATEGY_GATE' | 'PROVIDER_SWITCH'
  target_config     TEXT,      -- strategy_config key being changed
  current_value     TEXT,
  proposed_value    TEXT,
  evidence          TEXT,      -- AI reasoning with trade count + WR delta
  expected_wr_delta NUMERIC,
  status            TEXT DEFAULT 'PENDING',
  applied_at        TIMESTAMPTZ,
  created_at        TIMESTAMPTZ DEFAULT now()
);

NOTIFY pgrst, 'reload schema';
```

---

## NEW SCRIPT STUBS

---

### P2.1 — `backend/ingestion/compute_indicators.py`

```python
"""
TradeOS v6 — Phase 2: Compute Indicators
Replaces all Google Sheet formula columns. Run AFTER audit of every Sheet formula.
Wire: Step 03 in run_pipeline.py (before ingest_sheets), Phase 2+.
"""
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, logger


def fetch_today_raw(sb, today: str) -> list[dict]:
    rows = sb.table("chartink_raw_data").select("*").eq("date", today).execute().data
    logger.info(f"chartink_raw_data: {len(rows)} rows for {today}")
    return rows


def fetch_nifty_return(sb, today: str, sessions: int) -> float | None:
    try:
        rows = sb.table("nifty_total_market").select("date,close") \
            .lte("date", today).order("date", desc=True).limit(sessions + 1).execute().data
        if len(rows) < 2:
            return None
        return (float(rows[0]["close"]) - float(rows[-1]["close"])) / float(rows[-1]["close"]) * 100
    except Exception as e:
        logger.warning(f"Nifty return fetch failed: {e}"); return None


def fetch_historical_closes(sb, symbol: str, today: str, sessions: int) -> list[float]:
    rows = sb.table("chartink_raw_data").select("close") \
        .eq("symbol", symbol).lte("date", today) \
        .order("date", desc=True).limit(sessions + 1).execute().data
    return [float(r["close"]) for r in rows if r.get("close")]


def compute_row(raw: dict, closes_120: list, nifty_ret_1m: float | None) -> dict:
    close     = float(raw.get("close")      or 0)
    volume    = float(raw.get("volume")     or 0)
    avg_vol   = float(raw.get("avg_vol_20") or 0)
    atr_14    = float(raw.get("atr_14")     or 0)
    sma_50    = float(raw.get("sma_50")     or 0)
    sma_200   = float(raw.get("sma_200")    or 0)
    wk52_high = float(raw.get("week52_high") or 0)
    high_30d  = float(raw.get("high_30d")   or 0)
    low_30d   = float(raw.get("low_30d")    or 0)

    vol_ratio   = round(min(volume / avg_vol, 50.0), 4) if avg_vol > 0 else None
    atr_pct     = round(atr_14 / close * 100, 4)        if close  > 0 else None
    dist_sma50  = round((close - sma_50) / sma_50 * 100, 4)   if sma_50  > 0 else None
    dist_sma200 = round((close - sma_200) / sma_200 * 100, 4) if sma_200 > 0 else None
    above_sma50  = close > sma_50  if sma_50  > 0 else None
    above_sma200 = close > sma_200 if sma_200 > 0 else None
    breakout_setup = close > wk52_high * 0.98 if wk52_high > 0 else None
    consol_range   = round((high_30d - low_30d) / low_30d * 100, 4) if low_30d > 0 else None

    def pct_change(closes, n):
        if len(closes) > n:
            past = closes[n]
            return round((close - past) / past * 100, 4) if past > 0 else None
        return None

    ret_1m = pct_change(closes_120, 20)
    ret_3m = pct_change(closes_120, 60)
    ret_6m = pct_change(closes_120, 120)
    rs_vs_nifty = round(ret_1m - nifty_ret_1m, 4) if ret_1m is not None and nifty_ret_1m is not None else None

    return {
        "vol_ratio": vol_ratio, "atr_pct": atr_pct,
        "dist_sma50": dist_sma50, "dist_sma200": dist_sma200,
        "above_sma50": above_sma50, "above_sma200": above_sma200,
        "breakout_setup": breakout_setup, "consol_range": consol_range,
        "ret_1m": ret_1m, "ret_3m": ret_3m, "ret_6m": ret_6m,
        "rs_vs_nifty": rs_vs_nifty, "computed_at": datetime.now(IST).isoformat(),
    }


def main():
    sb    = get_supabase()
    today = str(today_ist())
    logger.info(f"compute_indicators: {today}")

    raw_rows     = fetch_today_raw(sb, today)
    nifty_ret_1m = fetch_nifty_return(sb, today, 20)
    if not raw_rows:
        logger.warning("No chartink_raw_data today — skipping"); return {"computed": 0}

    upsert_rows, skipped = [], 0
    for raw in raw_rows:
        try:
            sym          = raw.get("symbol")
            closes_120   = fetch_historical_closes(sb, sym, today, 120)
            computed     = compute_row(raw, closes_120, nifty_ret_1m)
            upsert_rows.append({"date": today, "symbol": sym, **computed})
        except Exception as e:
            logger.warning(f"{raw.get('symbol','?')}: {e}"); skipped += 1

    for i in range(0, len(upsert_rows), 50):
        sb.table("stock_data_daily").upsert(upsert_rows[i:i+50], on_conflict="date,symbol").execute()

    logger.success(f"compute_indicators: {len(upsert_rows)} computed, {skipped} skipped")
    return {"computed": len(upsert_rows), "skipped": skipped}


if __name__ == "__main__":
    main()
```

---

### P2.2 — `backend/ingestion/ingest_asm_gsm.py`

```python
"""
TradeOS v6 — Phase 2: Ingest ASM / GSM / F&O Ban Lists
Wire: Step 08a after nse_events, Phase 2+. Non-fatal — falls back to previous day.
"""
import sys, requests, csv, io
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, logger

# NSE URLs — verify these before Phase 2 (NSE changes them occasionally)
ASM_URL    = "https://nsearchives.nseindia.com/content/equities/asm_securities.csv"
GSM_URL    = "https://nsearchives.nseindia.com/content/equities/gsm_securities.csv"
FO_BAN_URL = "https://www.nseindia.com/api/fo-ban-list"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/json,*/*",
    "Referer": "https://www.nseindia.com",
}


def get_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)  # cookie handshake
    except Exception:
        pass
    return session


def fetch_csv_symbols(session, url: str, sym_col: str = "Symbol") -> list[str]:
    try:
        r = session.get(url, timeout=15); r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        return [row.get(sym_col, "").strip().upper() for row in reader if row.get(sym_col, "").strip()]
    except Exception as e:
        logger.warning(f"CSV fetch failed {url}: {e}"); return []


def fetch_fo_ban(session) -> list[str]:
    try:
        r = session.get(FO_BAN_URL, timeout=15); r.raise_for_status()
        return [item.get("symbol", "").upper() for item in r.json().get("data", []) if item.get("symbol")]
    except Exception as e:
        logger.warning(f"FO BAN fetch failed: {e}"); return []


def main():
    sb    = get_supabase()
    today = str(today_ist())
    sess  = get_nse_session()

    asm  = fetch_csv_symbols(sess, ASM_URL)
    gsm  = fetch_csv_symbols(sess, GSM_URL)
    ban  = fetch_fo_ban(sess)

    if not asm and not gsm and not ban:
        prev = sb.table("safety_lists").select("*").order("date", desc=True).limit(1).execute().data
        if prev:
            logger.warning("All NSE safety fetches failed — retaining previous day")
            asm  = prev[0].get("asm_symbols",    [])
            gsm  = prev[0].get("gsm_symbols",    [])
            ban  = prev[0].get("fo_ban_symbols", [])

    row = {
        "date": today, "asm_symbols": asm, "gsm_symbols": gsm, "fo_ban_symbols": ban,
        "asm_count": len(asm), "gsm_count": len(gsm), "fo_ban_count": len(ban),
        "fetched_at": datetime.now(IST).isoformat(),
    }
    sb.table("safety_lists").upsert(row, on_conflict="date").execute()
    logger.success(f"safety_lists: ASM={len(asm)}, GSM={len(gsm)}, FO_BAN={len(ban)}")
    return {"asm": len(asm), "gsm": len(gsm), "fo_ban": len(ban)}


if __name__ == "__main__":
    main()
```

---

### P2.3 — `backend/ingestion/data_quality_monitor.py`

✅ **Standalone file generated** — download `data_quality_monitor.py` directly.

**10 checks (C01–C10):**

| Check | What | Severity |
|-------|------|----------|
| C01 | chartink_raw_data row count 450–510 | ERROR if 0, WARN if outside band |
| C02 | RSI values 0–100 for all stocks | ERROR if >10 bad, else WARN |
| C03 | vol_ratio outliers auto-capped at 50x | Always OK (safe auto-correct) |
| C04 | delivery_pct values 0–100 | WARN |
| C05 | signal_log scores 0–120 | WARN |
| C06 | MSL score jumps >20pts vs yesterday | WARN |
| C07 | Pipeline completeness — all steps wrote today | ERROR (fatal steps) / WARN (non-fatal) |
| C08 | ai_context has G6/G13/G17/G18 patch fields | WARN — tracks patch rollout progress |
| C09 | ML predicted_regime vs manual regime tier diff | ERROR if diff ≥ 2, skips cleanly if P2 not deployed |
| C10 | Open positions ≤ regime max (8/6/4/3) | WARN — signals risk_manager will block new entries |

**Key design decisions vs original stub:**
- 10 checks vs original 5 — C07/C08/C09/C10 are new, driven by G17/G18 context and ML regime work
- C08 specifically detects whether G6/G13/G17/G18 patches are live in production (checks ai_context.context_json for market_regime, global_cues, portfolio, upcoming_events, sector_context fields)
- C09 cross-checks ML vs manual regime independently from ml_regime_classifier.py — provides a second audit point
- Dedup guard prevents duplicate data_anomalies rows when pipeline reruns same day
- Each check wrapped in independent try/except — one exception never blocks the others
- Telegram fires only on ERROR severity; WARNs are silent log entries only
---

### P2.4 — `backend/ai/providers/ml_regime_classifier.py`

✅ **Standalone file generated** — download `ml_regime_classifier.py` directly.

**Key decisions vs original stub:**
- **Sparse data solved**: primary source is `regime_history` (needs G14 Patch 13); automatically supplements from `market_regime` table when history < 30 rows — gives 6+ months of labelled training data from Day 1 of Phase 2
- **`class_weight="balanced"`**: handles class imbalance (far more NEUTRAL days than RISK OFF in history)
- **`--train` always also runs `--predict`**: today's market_regime row always populated after Sunday training
- **Disagreement detection**: tier diff ≥ 2 between ML and manual regime logs to `data_anomalies` — independently cross-checked by C09 in data_quality_monitor
- **Fail-safe guard**: if model trained on < 15 samples, `--predict` skips writing to avoid unreliable predictions corrupting regime context
- **7 features**: nifty_ret_5d, nifty_ret_20d, advance_decline, breadth_pct, fii_net_5d, fii_net_20d, sector_dispersion

**Wire reminders:**
- `evolution_weekly.yml` Step 2: `python ai/providers/ml_regime_classifier.py --train` (trains + predicts)
- `run_pipeline.py` Step 03 daily: `python ai/providers/ml_regime_classifier.py --predict` (no retraining)
---

### P3.1 — `backend/control/shadow_trade_logger.py`

✅ **Standalone file generated** — download `shadow_trade_logger.py` directly.

**Key decisions vs original stub:**
- **Mirrors execution_engine exactly**: same `calculate_position_size()` logic, same `risk_manager.run_all_checks()` call — shadow review is meaningful not a simplified approximation
- **`process_approval(signal_id, action)`** is the main entry point (called by `telegram_bot.py`) — not just a logging helper
- **`--summary` flag**: 14-day report with approval_rate, risk_block_rate, estimated P&L on APPROVED trades, and a `ready_for_live` boolean to make the go/no-go decision data-driven
- **`would_execute` boolean**: shows what would have happened in live mode (APPROVED + all risk checks passed)
- **Per-decision Telegram notification**: catch problems in real-time during the 2-week shadow period
- **`_build_notes()`**: combines risk check results + AI conviction + manual notes into a single readable audit string per shadow_trades row
---

### P3.2 — `backend/control/risk_manager.py`

✅ **Standalone file generated** — download `risk_manager.py` directly (prior session).

**5 checks in order:**
1. `_check_kill_switch()` — always first, stops everything
2. `_check_max_positions(sb)` — reads current regime, applies TRENDING=8 / NEUTRAL=6 / CAUTION=4 / RISK_OFF=3
3. `_check_sector(sb, symbol, order_value)` — sector concentration ≤30% of TOTAL_CAPITAL including the new order
4. `_check_asm_gsm(sb, symbol)` — reads today's safety_lists (Phase 2+); skips cleanly if table not yet populated
5. `_check_capital(sb, order_value)` — total_capital minus deployed vs order value

**Fail-safe in execution_engine**: if `risk_manager.py` cannot be imported, execution_engine blocks the order rather than proceeding unguarded.
---

### P3.3 — `backend/control/execution_engine.py`

✅ **Standalone file generated** — download `execution_engine.py` directly.

**Key decisions vs original stub:**
- **Three-mode gate** (`disabled` → `shadow` → `live`): defaults to `disabled` — nothing happens until explicit SQL update. Mode read from `system_config` not an env var so it can be changed without redeploy.
- **Kill switch always Gate 1**: checked before reading execution_mode — blocks even shadow logging
- **`handle_action(signal_id, action, notes)`** is the single entry point for all three actions (APPROVED / REJECTED / DEFERRED) — `telegram_bot.py` calls only this
- **REJECTED / DEFERRED**: update `signal_log.execution_status` for full audit trail, no price fetch, no risk checks needed
- **One Kite retry** on transient failures (timeout/502/503 only) — never a silent retry loop
- **Kite import failure is safe**: if `kite_client` unavailable, returns error dict and notifies Telegram — never crashes the bot
- **`place_order()` shim**: backward-compatible entry point for any existing telegram_bot.py calls
---

### P4.1 — `backend/history/discovery_engine.py`

```python
"""
TradeOS v6 — Phase 4: Discovery Engine
Statistical correlation of chartink_raw_data columns vs trade outcomes.
Requires: G1+G2 fixes applied + 6+ months chartink_raw_data accumulated.
Wire: evolution_weekly.yml, Phase 4+ only.
Writes to discovery_proposals (PENDING only — never auto-applied).
"""
import sys, json
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger

MIN_SAMPLE  = 30
MAX_RESULTS = 5
LOOKBACK    = 180   # days of history to use


def load_outcome_signals(sb) -> list[dict]:
    """Load signal_log rows for closed trades where outcome_pnl_pct is known (G2 fix)."""
    cutoff = str(today_ist() - timedelta(days=LOOKBACK))
    rows = sb.table("signal_log").select("*") \
        .not_.is_("outcome_pnl_pct", "null") \
        .gte("date", cutoff).execute().data
    logger.info(f"Outcome signals: {len(rows)} rows")
    return rows


def load_chartink_map(sb, symbols: list, dates: list) -> dict:
    """Returns (symbol, date) → chartink row."""
    result = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i+50]
        rows = sb.table("chartink_raw_data").select("*") \
            .in_("symbol", batch).in_("date", dates[:50]).execute().data
        for r in rows:
            result[(r["symbol"], r["date"])] = r
    return result


def numeric_cols(row: dict) -> list[str]:
    skip = {"id", "date", "symbol", "company_name", "sector", "industry", "created_at"}
    return [k for k, v in row.items() if k not in skip and v is not None
            and isinstance(v, (int, float))]


def pearson(xs, ys) -> float | None:
    if len(xs) < 10: return None
    n = len(xs); mx = sum(xs)/n; my = sum(ys)/n
    num = sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    dx  = sum((x-mx)**2 for x in xs) ** 0.5
    dy  = sum((y-my)**2 for y in ys) ** 0.5
    return round(num/(dx*dy), 4) if dx*dy > 0 else None


def win_rate_split(xs, ys) -> dict | None:
    if len(xs) < MIN_SAMPLE*2: return None
    import statistics
    med = statistics.median(xs)
    hi  = [(x,y) for x,y in zip(xs,ys) if x >= med]
    lo  = [(x,y) for x,y in zip(xs,ys) if x <  med]
    if len(hi) < MIN_SAMPLE or len(lo) < MIN_SAMPLE: return None
    return {
        "win_rate_with":    round(sum(1 for _,y in hi if y > 0)/len(hi), 4),
        "win_rate_without": round(sum(1 for _,y in lo if y > 0)/len(lo), 4),
        "split":            round(abs(sum(1 for _,y in hi if y > 0)/len(hi) - sum(1 for _,y in lo if y > 0)/len(lo)), 4),
        "n": len(xs),
    }


def main():
    if is_kill_switch_active():
        return {"status": "skipped"}

    sb = get_supabase()
    today = str(today_ist())
    signals = load_outcome_signals(sb)
    if len(signals) < MIN_SAMPLE:
        logger.info(f"Insufficient outcome signals ({len(signals)})")
        return {"status": "skipped", "reason": "insufficient_data"}

    syms  = list({s["symbol"] for s in signals})
    dates = list({s["date"]   for s in signals})
    cmap  = load_chartink_map(sb, syms, dates)

    sample_row = next(iter(cmap.values()), None)
    if not sample_row:
        logger.warning("No chartink features available"); return {"status": "skipped"}
    cols = numeric_cols(sample_row)

    discoveries = []
    for col in cols:
        xs, ys = [], []
        for sig in signals:
            key = (sig["symbol"], sig["date"])
            row = cmap.get(key)
            if row and row.get(col) is not None:
                try:
                    xs.append(float(row[col]))
                    ys.append(float(sig.get("outcome_pnl_pct") or 0))
                except (TypeError, ValueError):
                    pass

        if len(xs) < MIN_SAMPLE: continue
        corr = pearson(xs, ys)
        split = win_rate_split(xs, ys)
        if corr is None or split is None: continue
        if abs(corr) < 0.15 and split["split"] < 0.10: continue

        discoveries.append({
            "feature_name": col, "correlation": corr,
            "sample_size": len(xs),
            "win_rate_with": split["win_rate_with"],
            "win_rate_without": split["win_rate_without"],
            "evidence": f"col={col} corr={corr:.3f} split={split['split']:.1%} n={len(xs)}",
        })

    discoveries.sort(key=lambda d: d["win_rate_with"] - d["win_rate_without"], reverse=True)
    top = discoveries[:MAX_RESULTS]

    if top:
        sb.table("discovery_proposals").insert([
            {**d, "week_of": today, "status": "PENDING", "created_at": datetime.now(IST).isoformat()}
            for d in top
        ]).execute()

    logger.success(f"discovery_engine: {len(top)} proposals written")
    return {"proposals": len(top)}


if __name__ == "__main__":
    print(main())
```

---

## GITHUB ACTIONS — CHANGES NEEDED

### `evolution_weekly.yml` — Add Phase 2+ regime training + Phase 4 conditionals

```yaml
steps:
  - name: ML Conviction Model Training
    working-directory: backend
    run: python ai/providers/ml_provider.py --train

  - name: ML Regime Classifier Training    # Phase 2+
    if: ${{ vars.AUTONOMY_PHASE >= '2' }}
    working-directory: backend
    run: python ai/providers/ml_regime_classifier.py --train

  - name: Predict Today Regime             # Phase 2+
    if: ${{ vars.AUTONOMY_PHASE >= '2' }}
    working-directory: backend
    run: python ai/providers/ml_regime_classifier.py --predict

  - name: Evolution Tracker               # Phase 4 only — G8+G9+G15 fixes required
    if: ${{ vars.AUTONOMY_PHASE == '4' }}
    working-directory: backend
    run: python history/evolution_tracker.py

  - name: Discovery Engine               # Phase 4 only — 6+ months data required
    if: ${{ vars.AUTONOMY_PHASE == '4' }}
    working-directory: backend
    run: python history/discovery_engine.py
```
