# TradeOS v6 — Master Deployment Guide
**Version 5.1 · April 2026**

---

## What Is TradeOS v6?

TradeOS v6 is a fully automated swing trading system for Indian equity markets (NSE/Nifty). It runs as a pipeline of Python scripts on GitHub Actions, stores all state in Supabase (PostgreSQL), sends trade alerts via Telegram, and executes orders through Zerodha Kite Connect.

**Objective:** Identify NSE stocks forming swing trade setups 1–5 sessions ahead of the actual move, enter at optimal levels, hold 1–3 weeks, and exit at predefined targets or stop-losses. The system does this autonomously, only requiring human approval at trade execution (Phase 3) or full autonomy (Phase 4).

**Tech stack:** Python 3.12, Supabase (PostgreSQL), GitHub Actions, Zerodha Kite Connect, Telegram Bot API, scikit-learn (local ML), 6 LLM providers (Claude/OpenAI/Gemini/DeepSeek/Grok/Copilot).

**Google Sheet ID:** `1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw`
**Local codebase:** `C:\Users\vkshu\CRITICAL\Equity Indian Market Framework\tradeos-v6-complete\tradeos-v6`
**Standard git push:** `git add . && git commit -m "describe change" && git push`

---

## System Architecture — One-Page Summary

```
EXTERNAL SOURCES               PIPELINE (6 PM daily)          OUTPUT
────────────────               ──────────────────────          ──────
Chartink (500 stocks)  ──→    [01] ingest_market_news         Telegram
NSE Bhavcopy (OHLCV)   ──→    [02] ingest_macro_indicators    6 PM: regime + signals
Google Sheet (15 tabs) ──→    [03] ingest_global_cues         7 AM: morning brief
NSE FII/DII flows      ──→    [04] fetch_chartink             Intraday: SL + targets
NSE Events             ──→    [05] ingest_bhavcopy
NSE ASM/GSM lists      ──→    [06] ingest_sheets
Yahoo Finance          ──→    [07] ingest_fii_dii              ← must be before signals
                               [08] ingest_nse_events          ← must be before signals
                               [09] ingest_asm_gsm             ← must be before signals
                               [10] compute_indicators (P2)
                               [11] ml_regime_classifier (P2)
                               [12] generate_signals           ← FATAL step
                               [13] append_history
                               [14] post_trade_analysis
                               [15] generate_shortlist
                               [16] market_intelligence_engine (P2)
                               [17] ai_enrich
                               [18] send_alerts
                               [19] data_quality_monitor (P2)

SUPABASE (29 tables): all state, all history, all config lives here
WEEKLY (Sunday 6 AM): ml_provider --train → ml_regime_classifier --train → evolution_tracker
```

**How signals feed swing trades:**
1. `generate_signals` classifies every MSL stock into one of 4 signal types
2. `PRE_BREAKOUT_WATCH` = stock coiling, 2–5 sessions before breakout → you position early
3. `BUY_CANDIDATE` = enter now (all gates aligned, price in zone)
4. `ai_enrich` adds HIGH/MEDIUM/LOW conviction via LLM using 10 tables of context
5. `send_alerts` delivers the full picture to Telegram at 6 PM

---

## Current Phase Status

| Phase | Status | What it delivers |
|-------|--------|-----------------|
| Phase 0 | ✅ Complete | Pipeline runs daily. Signals generated. Frontend live. |
| Phase 1 | ✅ Complete | AI conviction, FII data, events, post-trade analysis, Telegram alerts. |
| Phase 2 | 🔶 Scripts built | Computation engine built. Needs code fixes (below) + gate criteria before activation. |
| Phase 3 | 🔲 Design ready | Supervised execution via Telegram APPROVE/REJECT. |
| Phase 4 | 🔲 Scripts built | Full autonomy + self-evolving parameters. Gate criteria not met. |

---

## IMMEDIATE ACTIONS — Deploy These Now (Phase 0/1 Bug Fixes)

These fix confirmed bugs in the live codebase. They work in Phase 0/1 — no phase change required.

### Step 1 — Run SQL migrations in Supabase (do this FIRST)

Open Supabase → SQL Editor → run each file in order:

**File 1: `sql_signal_log_market_context.sql`**
```sql
-- Adds 4 market-context columns to signal_log (needed by ml_provider_v2 training)
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS india_vix          DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS nifty_5d_chg_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS above_200dma_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS fii_net_20d_ctx    DOUBLE PRECISION;
NOTIFY pgrst, 'reload schema';
```

**File 2: `sql_ml_regime_classifier_columns.sql`**
```sql
-- Adds columns to ml_training_log for regime classifier training logs
ALTER TABLE ml_training_log
  ADD COLUMN IF NOT EXISTS model_type         TEXT,
  ADD COLUMN IF NOT EXISTS training_samples   INTEGER,
  ADD COLUMN IF NOT EXISTS feature_names      TEXT,
  ADD COLUMN IF NOT EXISTS feature_importance TEXT,
  ADD COLUMN IF NOT EXISTS trained_at         TIMESTAMPTZ;
NOTIFY pgrst, 'reload schema';
```

Verify both ran successfully: no error = good.

### Step 2 — Replace append_history.py

```bash
cp append_history_fixed.py backend/history/append_history.py
git add backend/history/append_history.py
git commit -m "fix: G14 regime_history snapshot_date→date bug"
git push
```
**What this fixes:** `regime_history` table was never being populated (G14 fix was silently failing). From this deployment forward, `regime_history` accumulates one row per trading day — required for `ml_regime_classifier` training.

**Verify next morning:** Check Supabase → `regime_history` table → should have a new row dated today.

### Step 3 — Replace run_pipeline.py

```bash
cp run_pipeline_fixed.py backend/run_pipeline.py
git add backend/run_pipeline.py
git commit -m "fix: Phase 2 fii/events/asm step ordering + add regime_predict step"
git push
```
**What this fixes:** FII data, NSE events, and ASM/GSM lists were running after signal generation in Phase 2 — all signals were scored against yesterday's data. Also adds the `regime_predict` step (runs between compute_indicators and signals).

**Note:** Phase 0/1 pipeline order is unchanged. This only affects Phase 2 (`autonomy_phase >= 2`).

### Step 4 — Replace generate_signals.py

```bash
cp generate_signals_v2.py backend/signals/generate_signals.py
git add backend/signals/generate_signals.py
git commit -m "feat: capture 4 market-context fields at signal time for ML training"
git push
```
**What this adds:** Captures `india_vix`, `nifty_5d_chg_pct`, `above_200dma_pct`, `fii_net_20d_ctx` at signal time and writes them to `signal_log`. These are the market conditions when each signal fired — needed for `ml_provider_v2` training. All other signal logic is identical.

### Step 5 — Replace ml_provider.py

```bash
cp ml_provider_v2.py backend/ai/providers/ml_provider.py
git add backend/ai/providers/ml_provider.py
git commit -m "feat: ml_provider v2 - 26 features, 4-class regime, market context"
git push
```
**What this improves:** Adds 7 new features (VIX, Nifty momentum, market breadth, FII 20d, score_adjusted, in_scanner, breakout_setup). Fixes regime encoding from 3-class to 4-class (RISK OFF/CAUTION/NEUTRAL/TRENDING — `CAUTION` was previously mapped to `NEUTRAL`, model never learned CAUTION = worse outcomes). Raises minimum trades from 60 to 90 for 26-feature model.

### Step 6 — Replace ml_regime_classifier.py

```bash
cp ml_regime_classifier_v2.py backend/ai/providers/ml_regime_classifier.py
git add backend/ai/providers/ml_regime_classifier.py
git commit -m "fix: ml_regime_classifier v2 - 3 schema bugs corrected"
git push
```
**What this fixes:** Three schema bugs that would have caused crashes on first run: (1) queried `regime_history.snapshot_date` (column is `date`), (2) queried `nifty_total_market.close` (index membership table, no price data), (3) used `sector_strength.strength_score` (column is `breadth_sma50`).

### Step 7 — Replace send_alerts.py

```bash
cp send_alerts_v2.py backend/alerts/send_alerts.py
git add backend/alerts/send_alerts.py
git commit -m "feat: send_alerts v2 - PRE_BREAKOUT_WATCH section + richer regime header"
git push
```
**What this fixes:** `PRE_BREAKOUT_WATCH` (2–5 days advance notice) and `STAGED_ENTRY` (1–3 days advance notice) signals were never shown in Telegram. These are the most valuable swing trade signals — they fire before the breakout happens. Now shown in a dedicated "ADVANCE NOTICE" section in both morning and evening messages.

**Also adds:** Market breadth %, regime confidence, FII 20d trend in regime header. `signal_type_label()` helper gives human-readable timing context per signal type.

### Step 8 — Verify deployment

After all 7 steps are deployed and the next 6 PM pipeline runs:

```bash
# Check Supabase:
# 1. signal_log latest row — should have india_vix, nifty_5d_chg_pct populated
# 2. regime_history — should have today's row
# 3. Telegram 6 PM message — should show "ADVANCE NOTICE" section (if any exist)
# 4. Check GitHub Actions log — no unexpected errors

# To run regime classifier status check:
python backend/ai/providers/ml_regime_classifier.py --status
# Expected: shows data readiness. After 30+ regime_history rows: --train will work.
```

---

## Phase 2 — Computation Engine

**Goal:** Eliminate Google Sheet from data computation. Python computes all indicators from raw data. ML predicts regime. Data quality monitored automatically.

### Phase 2 Gate Criteria (ALL required before activating)

- [ ] All 7 deployment steps above completed and verified
- [ ] Phase 1 pipeline clean for 30+ consecutive trading days
- [ ] `chartink_raw_data` has 30+ days of history
- [ ] `regime_history` has 30+ rows (starts accumulating from Step 2 deploy date)
- [ ] Google Sheet formula audit complete — each computed column classified as Type A (will replace) or Type B (keep Sheet as source)
- [ ] All Phase 2 SQL migrations run (see below)

### Phase 2 SQL Migrations (run before activating)

```sql
-- safety_lists (ASM/GSM per symbol)
CREATE TABLE IF NOT EXISTS public.safety_lists (
  symbol TEXT NOT NULL, list_type TEXT NOT NULL,
  stage TEXT, reason TEXT, effective_date DATE,
  source TEXT DEFAULT 'NSE', updated_at TIMESTAMPTZ DEFAULT NOW(),
  listed_date DATE, ingested_at TIMESTAMPTZ,
  UNIQUE (symbol, list_type)
);

-- data_anomalies (quality monitor output)
CREATE TABLE IF NOT EXISTS public.data_anomalies (
  id BIGSERIAL PRIMARY KEY, date DATE, check_name TEXT,
  severity TEXT, value TEXT, message TEXT, affected TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- macro_indicators (CPI/WPI/GDP/IIP)
CREATE TABLE IF NOT EXISTS public.macro_indicators (
  id BIGSERIAL PRIMARY KEY, indicator_date DATE NOT NULL,
  indicator_name TEXT NOT NULL, indicator_value NUMERIC,
  previous_value NUMERIC, change_bps NUMERIC, source TEXT,
  release_date DATE, ingested_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (indicator_date, indicator_name)
);

-- compute_indicators reconcile config
INSERT INTO system_config (key, value, description)
VALUES ('compute_indicators_reconcile', 'true',
        'When true: compute_indicators reconciles vs sheet. Set false when confident in compute output.')
ON CONFLICT (key) DO NOTHING;

-- open_positions: target + trailing SL
ALTER TABLE open_positions
  ADD COLUMN IF NOT EXISTS target_price    NUMERIC,
  ADD COLUMN IF NOT EXISTS target_pct      NUMERIC,
  ADD COLUMN IF NOT EXISTS target_hit      BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS target_hit_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trailing_sl_pct NUMERIC;

NOTIFY pgrst, 'reload schema';
```

### Activate Phase 2

Only after ALL gate criteria are met:

```sql
UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';
NOTIFY pgrst, 'reload schema';
```

This single SQL update switches the pipeline to the Phase 2 step sequence automatically. No code deployment required.

### Phase 2 Step Sequence (active when `autonomy_phase = 2`)

| Step | Script | Fatal | Purpose |
|------|--------|-------|---------|
| 01 | `ingest_market_news.py` | No | Scrape NSE/RBI/ET/SEBI/Google News → `market_news` |
| 02 | `ingest_macro_indicators.py` | No | CPI/WPI/GDP/IIP from RBI + MOSPI → `macro_indicators` |
| 03 | `ingest_global_cues.py EVENING` | No | Gift Nifty, DOW, crude, 10yr, silver → `global_cues` |
| 04 | `fetch_chartink.py` | **YES** | 500 stocks raw → `chartink_raw_data` |
| 05 | `ingest_bhavcopy.py` | No | OHLCV + delivery → `raw_prices` |
| 06 | `ingest_sheets.py` | **YES** | Google Sheet → 14 tables |
| 07 | `ingest_fii_dii.py` | No | FII flows + rolling sums → `fii_dii_flow` |
| 08 | `ingest_nse_events.py` | No | Corporate calendar → `event_calendar` |
| 09 | `ingest_asm_gsm.py` | No | Surveillance lists → `safety_lists` |
| 10 | `compute_indicators.py` | No | 21 renames + 30 computed cols → `stock_data_daily` |
| 11 | `ml_regime_classifier.py --predict` | No | 7 features → `predicted_regime` in `market_regime` |
| 12 | `generate_signals.py` | **YES** | 4 signal types → `signal_log` |
| 13 | `append_history.py` | No | MSL + regime snapshots |
| 14 | `post_trade_analysis.py` | No | Outcomes → `lessons` |
| 15 | `generate_shortlist.py` | No | AI top-12 ranking → `master_shortlist` |
| 16 | `market_intelligence_engine.py` | No | News synthesis → `market_news`, `lessons`, `signal_log` |
| 17 | `ai_enrich.py` | No | 10-table context → `ai_context` |
| 18 | `send_alerts.py` | No | Telegram digest |
| 19 | `data_quality_monitor.py` | No | 10 checks → `data_anomalies` |

**Key ordering rule:** Steps 07/08/09 (fii_dii, nse_events, asm_gsm) MUST run before step 12 (generate_signals) because `generate_signals` reads `fii_flag`, `event_calendar`, and `safety_lists` at runtime.

### compute_indicators.py — Hybrid Reconcile Mode

- Reads `chartink_raw_data` + compares against `stock_data_daily` (sheet values)
- Per field, decides: `COMPUTED_MATCH` / `COMPUTED_ONLY` / `DIVERGED` (uses sheet) / `SHEET_ONLY`
- Writes `compute_meta JSONB` per symbol for transparency
- Graduate fields to `COMPUTE_ALWAYS` as you verify:

```sql
-- When vol_ratio matches sheet consistently:
UPDATE system_config SET value='COMPUTE_ALWAYS' WHERE key='compute_trust_vol_ratio';
-- When consol_range uses a different window in sheet:
UPDATE system_config SET value='SHEET_ALWAYS' WHERE key='compute_trust_consol_range';
```

### ml_regime_classifier.py — Usage

```bash
# Check data readiness before training
python backend/ai/providers/ml_regime_classifier.py --status

# Train (Sunday W2, or manually after 30+ regime_history rows)
python backend/ai/providers/ml_regime_classifier.py --train
# Expected: CV accuracy ~0.72, model saved to models/ml_regime_model.pkl

# Predict today (daily step 11)
python backend/ai/providers/ml_regime_classifier.py --predict
# Expected: predicted_regime=NEUTRAL (conf=0.81) written to market_regime
```

---

## Phase 3 — Supervised Execution

**Goal:** Every trade requires explicit Telegram APPROVE/REJECT tap. Kite executes on approval.

### What needs to be built before activating Phase 3

**SQL migrations (run before any Phase 3 code):**
```sql
-- Links positions to signals for clean attribution
ALTER TABLE open_positions
  ADD COLUMN IF NOT EXISTS signal_id      BIGINT,
  ADD COLUMN IF NOT EXISTS signal_date    DATE,
  ADD COLUMN IF NOT EXISTS signal_subtype TEXT,
  ADD COLUMN IF NOT EXISTS original_qty   INT,
  ADD COLUMN IF NOT EXISTS current_qty    INT,
  ADD COLUMN IF NOT EXISTS partial_bookings JSONB;

-- Records every order placed
CREATE TABLE IF NOT EXISTS public.order_history (
  id               BIGSERIAL PRIMARY KEY,
  order_date       DATE NOT NULL DEFAULT CURRENT_DATE,
  symbol           TEXT NOT NULL,
  signal_id        BIGINT,
  broker_order_id  TEXT,
  order_type       TEXT,
  qty_requested    INT,
  qty_executed     INT,
  price_requested  NUMERIC,
  price_executed   NUMERIC,
  slippage_pct     NUMERIC,
  status           TEXT,
  rejection_reason TEXT,
  kite_response    JSONB,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);
NOTIFY pgrst, 'reload schema';
```

**Scripts to complete:**
- `execution_engine.py` — extend skeleton in `backend/control/`. Wire Telegram inline buttons APPROVE/REJECT. On APPROVE: call `kite_client.place_order()`. On REJECT: log and skip.
- `risk_manager.py` — 5 pre-trade checks: regime gate, position size, portfolio exposure, SL distance, liquidity. Called by `execution_engine` before sending to Kite.
- Partial profit booking: `book_partial(symbol, pct=50)`. Telegram "BOOK 50%" button → sells half, moves SL to breakeven. Uses `open_positions.partial_bookings JSONB`.

**Activate Phase 3:**
```sql
UPDATE system_config SET value = '3' WHERE key = 'autonomy_phase';
```

---

## Phase 4 — Full Autonomy

**Goal:** System proposes, you approve. No manual signal review required.

### What's already built (waiting for gate criteria)

- `evolution_tracker.py` — Tier 1 (weekly threshold optimisation), Tier 2 (monthly new field discovery from `stock_data_daily`), Tier 3 (quarterly gate retirement). All proposals → `evolution_proposals` as `PENDING`.
- `discovery_engine.py` — Discovers `stock_data_daily` fields not yet used as signal gates.

### Phase 4 Gate Criteria

- Phase 3 running cleanly with execution history
- `closed_positions` ≥ 200 trades (for meaningful evolution analysis)
- `compute_indicators.py` trust graduation complete (all fields at `COMPUTE_ALWAYS` or `SHEET_ALWAYS`)
- `ingest_sheets.py` deprecated for data computation (Sheet still used for master_shortlist and config)

### Approve evolution proposals (when ready)

```sql
-- Review what evolution_tracker is proposing
SELECT proposed_date, param_name, current_value, proposed_value,
       evidence, expected_improvement, confidence
FROM evolution_proposals
WHERE status = 'PENDING'
ORDER BY confidence DESC;

-- Approve a specific proposal (system reads at next Sunday W3 run)
UPDATE evolution_proposals
SET status='APPROVED', approved_by='V', approved_at=NOW()
WHERE id = <proposal_id>;
```

---

## Daily Operating Procedure

### 7:00 AM — Morning brief on Telegram (auto)
- Section 0: Data quality alerts (ERROR severity only)
- Section 1: Global cues (Gift Nifty, DOW, S&P, crude, 10yr yield, silver, USD/INR)
- Section 2: Advance notice signals (PRE_BREAKOUT_WATCH — 2-5 sessions ahead)
- Section 3: Entry-ready candidates with entry zones + AI conviction
- Section 4: Open positions SL proximity watch
- Section 5: Event risk for held stocks (5-day window)

### 8:30 AM — Manual: Kite token refresh (Phase 2+)
```bash
python backend/kite/kite_token_refresh.py
# Opens browser → log in → token saved to system_config automatically
```

### 9:15 AM to 3:30 PM — Auto: SL + target monitor (every 30 min)
`sl_monitor.py` + `position_target_monitor.py` — Telegram only on SL breach or target hit.

### 6:00 PM — Evening digest on Telegram (auto, ~15 min after pipeline starts)
- Regime header + breadth + FII
- ADVANCE NOTICE: PRE_BREAKOUT_WATCH + STAGED_ENTRY (ahead-of-time swing signals)
- ENTRY READY: BUY_CANDIDATE + PRIME_SETUP (enter now)
- Open positions + EXIT signals

### Sunday 6:00 AM — ML training (auto)
- W1: `ml_provider.py --train` (needs ≥90 closed trades)
- W2: `ml_regime_classifier.py --train` (needs ≥30 regime_history rows)
- W3: `evolution_tracker.py` (proposals created, you approve via SQL)

---

## Kill Switch

```sql
-- Halt entire system immediately (all scripts check this at startup)
UPDATE system_config SET value = 'true' WHERE key = 'kill_switch_active';

-- Resume
UPDATE system_config SET value = 'false' WHERE key = 'kill_switch_active';
```

---

## Troubleshooting

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| 0 signals generated | `stock_data_daily` or `master_shortlist` empty for today | Check `fetch_chartink` + `ingest_sheets` in Actions log |
| `regime` shows UNKNOWN | `market_regime` has no row for today | `ingest_sheets` likely failed — check Actions log |
| AI conviction all NULL | No valid API key or all providers failed | Check env vars; `ml_provider` needs 90+ closed trades |
| `regime_history` still empty after deploy | `append_history_fixed.py` not deployed | Deploy Step 2 |
| ml_regime_classifier crash | v1 schema bugs | Deploy Step 6 |
| FII flag stale / ASM not blocking | Step ordering bug | Deploy Step 3 |
| Telegram shows no advance notice signals | `send_alerts` v1 still deployed | Deploy Step 7 |
| PRE_BREAKOUT_WATCH in signal_log but not Telegram | Same as above | Deploy Step 7 |
| Kite reconcile mismatches | Token expired | Run `kite_token_refresh.py` |
| `data_anomalies` growing fast | Check `data_quality_monitor` log; C01 error = chartink fetch; C09 = ML vs manual regime disagrees | Investigate the specific check that's firing |

---

## Environment Variables

```
SUPABASE_URL           Supabase project URL
SUPABASE_KEY           Supabase service role key
ANTHROPIC_API_KEY      Claude (provider #1)
OPENAI_API_KEY         OpenAI (provider #2)
GOOGLE_API_KEY         Gemini (provider #3)
DEEPSEEK_API_KEY       DeepSeek (provider #4)
XAI_API_KEY            Grok (provider #5)
AZURE_OPENAI_KEY       Copilot/Azure (provider #6)
AZURE_OPENAI_ENDPOINT  Azure endpoint URL
TELEGRAM_TOKEN         Bot token
TELEGRAM_CHAT_ID       Your chat ID
KITE_API_KEY           Zerodha (Phase 2+)
KITE_API_SECRET        Zerodha (Phase 2+)
CHARTINK_COOKIE        Chartink session cookie
GOOGLE_SHEET_ID        Sheet ID (see top of this file)
DRY_RUN                Set 'True' to skip all Supabase writes
```

---

## Repository Structure

```
tradeos-v6/
├── run_pipeline.py                   ← Main orchestrator
├── config.py                         ← Supabase client, cfg helpers, kill switch
├── requirements.txt
│
├── .github/workflows/
│   ├── pipeline_daily.yml            ← 6 PM IST weekdays
│   ├── pipeline_morning.yml          ← 7 AM IST weekdays
│   ├── pipeline_intraday.yml         ← Every 30 min market hours
│   └── evolution_weekly.yml          ← Sunday 6 AM
│
└── backend/
    ├── ingestion/                    P0-P2 data sources
    │   ├── fetch_chartink.py         P0 ✅
    │   ├── ingest_bhavcopy.py        P0 ✅
    │   ├── ingest_sheets.py          P0 ✅ (deprecated P4)
    │   ├── ingest_fii_dii.py         P1 ✅
    │   ├── ingest_nse_events.py      P1 ✅
    │   ├── ingest_global_cues.py     P1 ✅
    │   ├── ingest_asm_gsm.py         P2 ✅ built
    │   ├── ingest_market_news.py     P2 ✅ built
    │   └── ingest_macro_indicators.py P2 ✅ built
    │
    ├── compute/                      P2 computation
    │   ├── compute_indicators.py     P2 ✅ built (hybrid reconcile)
    │   └── data_quality_monitor.py   P2 ✅ built (10 checks)
    │
    ├── signals/
    │   ├── generate_signals.py       P0 ✅ → REPLACE with generate_signals_v2.py
    │   └── independent_scanner.py    P1 ✅
    │
    ├── ai/
    │   ├── ai_enrich.py              P1 ✅
    │   ├── ai_router.py              P1 ✅
    │   ├── generate_shortlist.py     P1 ✅
    │   ├── post_trade_analysis.py    P1 ✅
    │   ├── market_intelligence_engine.py  P2 ✅ built
    │   └── providers/
    │       ├── base_provider.py      P1 ✅
    │       ├── ml_provider.py        P1 ✅ → REPLACE with ml_provider_v2.py
    │       ├── ml_regime_classifier.py P2 ✅ → REPLACE with ml_regime_classifier_v2.py
    │       └── [6 LLM providers]     P1 ✅
    │
    ├── alerts/
    │   └── send_alerts.py            P1 ✅ → REPLACE with send_alerts_v2.py
    │
    ├── history/
    │   ├── append_history.py         P0 ✅ → REPLACE with append_history_fixed.py
    │   ├── evolution_tracker.py      P4 ✅ built
    │   └── discovery_engine.py       P4 ✅ built
    │
    ├── control/
    │   ├── kill_switch.py            P0 ✅
    │   ├── sl_monitor.py             P2 ✅
    │   ├── position_target_monitor.py P2 ✅ built
    │   ├── execution_engine.py       P3 skeleton
    │   └── risk_manager.py           P3 skeleton
    │
    ├── kite/
    │   ├── kite_token_refresh.py     P2 ✅
    │   ├── kite_client.py            P2 ✅
    │   └── kite_reconcile.py         P2 ✅
    │
    ├── db/                           SQL migration files
    └── models/
        ├── ml_conviction.pkl         Written by ml_provider.py --train (W1)
        └── ml_regime_model.pkl       Written by ml_regime_classifier.py --train (W2)
```

**Legend:** ✅ Live/built  |  skeleton = structure exists, not complete  |  → REPLACE = file delivered in this session
