# TradeOS v6 — Deployment Guide
**Step-by-step from zero to fully autonomous trading system**

---

## Architecture Overview

```
Your Google Sheet (Brain)
       ↓ fetch_chartink.py (6 PM daily — runs FIRST)
       ↓ ingest_sheets.py (6 PM daily — runs SECOND)
   Supabase (Database)
       ↓ generate_signals.py
   Signal Log + Frontend
       ↓ (Phase 1+)
   AI Enrichment + Telegram (with Industry Strength scoring)
       ↓ (Phase 2+)
   Kite Live Prices + Auto Indicators
       ↓ (Phase 3+)
   Supervised Execution + Kill Switch
       ↓ (Phase 4+)
   Full Autonomy + Self-Evolution
```

---

## PHASE 0 — Operational (Do this first)

**Goal:** Frontend working with live data from your Sheet. Pipeline runs at 6 PM daily.

---

### Step 1: Prerequisites

Install on your local machine (for testing):
```bash
python --version        # Must be 3.11 or 3.12
git --version           # Any recent version
```

---

### Step 2: Clone and configure

```bash
# Clone or unzip the package
cd tradeos-v6/backend

# Copy .env template
cp .env.example .env

# Edit .env — fill in these required values:
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key
GOOGLE_SHEET_ID=1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw
GOOGLE_CREDENTIALS_JSON=credentials/service_account.json
TOTAL_CAPITAL=200000
CHARTINK_EMAIL=your@email.com
CHARTINK_PASSWORD=yourpassword
```

---

### Step 3: Google Sheets API setup

You only do this once.

1. Go to https://console.cloud.google.com
2. Create a new project (or use existing)
3. Enable the **Google Sheets API** for that project
4. Go to **IAM & Admin → Service Accounts**
5. Click **Create Service Account** → give it any name → Done
6. Click the service account → **Keys** tab → **Add Key** → JSON
7. A JSON file downloads — rename it `service_account.json`
8. Create folder `backend/credentials/` and put the file there
9. Open your Google Sheet → Share → paste the service account email (ends in `@...iam.gserviceaccount.com`) → **Editor** access (required for fetch_chartink to write CSV data)
10. Update `.env`: `GOOGLE_CREDENTIALS_JSON=credentials/service_account.json`

---

### Step 4: Install Python dependencies

```bash
cd tradeos-v6/backend
pip install -r requirements.txt
```

On Windows if pip fails:
```bash
pip install -r requirements.txt --break-system-packages
```

Playwright (required for fetch_chartink):
```bash
playwright install chromium
```

---

### Step 5: Set up Supabase database

1. Go to your Supabase project → **SQL Editor**
2. Copy and run each file in this exact order:

```
backend/db/schema_v6_base.sql      ← Run FIRST
backend/db/schema_v6_signals.sql   ← Run SECOND
backend/db/schema_rls.sql          ← Run LAST
```

3. Run the Chartink raw data table (new — run once):
```sql
CREATE TABLE IF NOT EXISTS chartink_raw_data (
    id               BIGSERIAL PRIMARY KEY,
    date             DATE NOT NULL,
    symbol           TEXT NOT NULL,
    sector           TEXT, industry TEXT, market_cap NUMERIC, market_cap_cat TEXT,
    daily_open NUMERIC, daily_high NUMERIC, daily_low NUMERIC, daily_close NUMERIC,
    week52_high NUMERIC, week52_low NUMERIC, high_30d NUMERIC,
    sma_10 NUMERIC, sma_20 NUMERIC, sma_50 NUMERIC, sma_200 NUMERIC,
    ema_10 NUMERIC, ema_20 NUMERIC, ema_50 NUMERIC,
    rsi_daily NUMERIC, rsi_weekly NUMERIC, rsi_monthly NUMERIC,
    adx_14 NUMERIC, adx_plus_di NUMERIC, adx_minus_di NUMERIC,
    volume BIGINT, avg_vol_20 NUMERIC, avg_vol_50 NUMERIC,
    vwap_daily NUMERIC, vwap_20d NUMERIC, vwap_50d NUMERIC,
    pct_change NUMERIC, atr_14 NUMERIC, atr_pct NUMERIC,
    ha_high NUMERIC, ha_low NUMERIC, ha_close NUMERIC,
    supertrend NUMERIC, macd_line NUMERIC, macd_signal NUMERIC, macd_histogram NUMERIC,
    parabolic_sar NUMERIC, upper_bb NUMERIC, lower_bb NUMERIC, stochastic NUMERIC,
    ttm_net_profit NUMERIC, net_profit_yr NUMERIC, eps NUMERIC,
    qtr_net_profit NUMERIC, qtr_var_profit NUMERIC,
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_chartink_date   ON chartink_raw_data (date DESC);
CREATE INDEX IF NOT EXISTS idx_chartink_symbol ON chartink_raw_data (symbol);
```

4. Run the signal_log industry columns (new — run once):
```sql
ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS industry          TEXT,
    ADD COLUMN IF NOT EXISTS industry_rank     INT,
    ADD COLUMN IF NOT EXISTS industry_top5     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS industry_state    TEXT,
    ADD COLUMN IF NOT EXISTS industry_avg_rsi  NUMERIC;
```

5. Verify all tables exist: go to **Table Editor** → you should see:
   `stock_data_daily, master_shortlist, open_positions, closed_positions,`
   `sector_strength, industry_strength, market_regime, event_calendar, lessons, msl_history,`
   `nse_holidays, raw_prices, system_config, strategy_config, signal_log,`
   `regime_history, nifty_upcoming_events, nifty_total_market, chartink_raw_data`

---

### Step 6: Test Chartink ingestion locally

```bash
cd tradeos-v6/backend

# Run fetch_chartink FIRST — writes to Google Sheet tab + Supabase
python ingestion/fetch_chartink.py

# You should see:
# ✓ CSV downloaded: 500 rows
# ✓ Written to 'Chartink Raw Data_Nifty 500': 500 rows
# ✓ Upserted to chartink_raw_data: 500 rows
```

---

### Step 7: Test sheet ingestion locally

```bash
# Run ingest_sheets SECOND — reads all 15 tabs including the freshly updated Chartink tab
python ingestion/ingest_sheets.py

# Check output — you should see something like:
# ✓ STOCK_DATA: 500 rows upserted
# ✓ MASTER_SHORTLIST: 35 rows upserted
# ✓ OPEN_POSITIONS: 8 rows synced
# ✓ INDUSTRY_STRENGTH: X rows upserted
# ... etc
```

If you see errors:
- `GOOGLE_CREDENTIALS` error → check Step 3 above
- `403 on Google Sheets` → ensure service account has **Editor** (not Viewer) access
- `SUPABASE` error → check your SUPABASE_URL and SUPABASE_SERVICE_KEY
- `column not found` → re-run the SQL schema files
- Chartink CSV button not appearing → ensure `headless=False` and run once manually to confirm hover works

---

### Step 8: One-time MSL history backfill

```bash
python scripts/backfill_msl_history.py
# Loads the 708 existing MSL history rows from your Sheet
# Only run this once
```

---

### Step 9: Test signal generation

```bash
python signals/generate_signals.py

# You should see:
# Signal breakdown: {'BUY_CANDIDATE': X, 'WATCH': Y, 'OPEN_POSITION': Z}
# Regime: RISK OFF
# BUY CANDIDATES: X
```

---

### Step 10: Run full pipeline test

```bash
python run_pipeline.py --dry-run
# Tests all steps without writing to Supabase
# All steps should show ✅ OK

python run_pipeline.py
# Real run — writes to Supabase
# Order: fetch_chartink → ingest_sheets → generate_signals → ...
```

---

### Step 11: Deploy frontend

**Option A — Use with Claude.ai:**
use html

**Option B — Deploy on Vercel/Netlify (recommended):**

npm run dev
# Opens at http://localhost:5173

**Option C — Run locally:**
cd tradeos-v6/frontend
npx vite

---

### Step 12: Automate with GitHub Actions

This runs the pipeline every weekday at 6 PM IST.

1. Push your code to GitHub:
```bash
cd tradeos-v6
git init
git add .
git commit -m "TradeOS v6 Phase 0"
git remote add origin https://github.com/YOUR_USERNAME/tradeos-v6.git
git push -u origin main
```
```
1.1 Regular Git Commit Steps
a. cd "C:\Users\vkshu\CRITICAL\Equity Indian Market Framework\tradeos-v6-complete\tradeos-v6"
git add .
b. git commit -m "describe what you changed"
c. git push
d. git status
e. git log --oneline
```

2. Go to GitHub → your repo → **Settings** → **Secrets and variables** → **Actions**

3. Add these secrets (click "New repository secret" for each):
```
SUPABASE_URL            → your Supabase project URL
SUPABASE_SERVICE_KEY    → your service role key
GOOGLE_SHEET_ID         → 1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw
GOOGLE_CREDENTIALS_JSON → paste the ENTIRE contents of service_account.json
TOTAL_CAPITAL           → 200000
CHARTINK_EMAIL          → your Chartink login email
CHARTINK_PASSWORD       → your Chartink password
```

4. Go to **Actions** tab → you should see the workflows listed
5. Click "TradeOS v6 Daily Pipeline" → "Run workflow" → test it manually once
6. From tomorrow it runs automatically at 6 PM IST weekdays

> **Note on Playwright in GitHub Actions:** `pipeline_daily.yml` must install Playwright browsers.
> Add this step before the Python run step:
> ```yaml
> - name: Install Playwright browsers
>   run: playwright install chromium
> ```

---

### Phase 0 is complete when:
- ✅ `python ingestion/fetch_chartink.py` downloads 500 rows and writes to Sheet + Supabase
- ✅ `python run_pipeline.py` completes without errors
- ✅ Supabase tables populated with your Sheet data including `chartink_raw_data` and `industry_strength`
- ✅ Frontend shows your positions, sectors, and BUY candidates
- ✅ RISK OFF warning banner visible (current regime)
- ✅ GitHub Actions running at 6 PM IST

---

## PHASE 1 — Intelligence + Automation (Month 2)

**Goal:** Eliminate manual weekly steps. Add AI reasoning. Add FII/DII data. Activate Industry Strength in signal scoring and alerts.

### What changes:
- AI provider activated (your choice) OR free ML/scraping fallback
- NSE events auto-fetched (no more manual update)
- FII/DII flow data from NSE
- Telegram alerts enabled — now includes Industry Strength context per signal
- Independent scanner runs alongside rule engine
- Industry Strength data from `industry_strength` table actively used in signal scoring

### Steps:

**1. Activate AI provider (choose one)**

In `.env`, set ONE of:
```bash
ANTHROPIC_API_KEY=sk-ant-...     # Claude — cheapest on Haiku ~₹100/day
OPENAI_API_KEY=sk-...            # ChatGPT — gpt-4o-mini
GEMINI_API_KEY=...               # Gemini 1.5 Flash
DEEPSEEK_API_KEY=...             # DeepSeek — very cheap, ~10x less than Claude
GROK_API_KEY=...                 # Grok
AZURE_OPENAI_API_KEY=...         # Copilot (if you have Azure)
AZURE_OPENAI_ENDPOINT=https://...
```

In Supabase SQL Editor, update system_config:
```sql
UPDATE system_config SET value = 'claude' WHERE key = 'ai_provider';
-- Or: 'openai', 'gemini', 'deepseek', 'grok', 'copilot', 'ml', 'disabled'
```

Or change it from the frontend Settings tab.

**To use FREE ML fallback only (no API key needed):**
```sql
UPDATE system_config SET value = 'ml' WHERE key = 'ai_provider';
UPDATE system_config SET value = 'true' WHERE key = 'ai_fallback_ml';
```
ML model trains automatically every Sunday from your closed trades.
Needs minimum 30 closed trades to activate (you have 54 — ready now).

**2. Activate Industry Strength in signal scoring**

`generate_signals.py` already loads `industry_strength` from Supabase (added in Phase 0).
In Phase 1, scoring bonuses go live:
- `top5_flag = True` → +10 to signal score
- `industry_state = 'STRONG'` → +5 to signal score
- `industry_rank` and `industry_state` stored on every `signal_log` row

Verify columns exist (run once if not already done):
```sql
ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS industry          TEXT,
    ADD COLUMN IF NOT EXISTS industry_rank     INT,
    ADD COLUMN IF NOT EXISTS industry_top5     BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS industry_state    TEXT,
    ADD COLUMN IF NOT EXISTS industry_avg_rsi  NUMERIC;
```

**3. Enable Telegram alerts with Industry Strength**

Create a Telegram bot:
1. Message @BotFather on Telegram → `/newbot` → follow prompts
2. Copy the bot token → add to `.env`: `TELEGRAM_BOT_TOKEN=...`
3. Start your bot (send any message to it)
4. Get your chat ID: message @userinfobot → copy your ID
5. Add to `.env`: `TELEGRAM_CHAT_ID=...`

Add these secrets to GitHub too.

Then enable in Settings tab or:
```sql
UPDATE system_config SET value = 'true' WHERE key = 'telegram_alerts_enabled';
```

Each BUY CANDIDATE alert now shows:
```
SBIN [CTL] Score:72 | Ind: STRONG #3 ⚠️RISK OFF
```

**4. Enable Phase 1 pipeline**

```sql
UPDATE system_config SET value = '1' WHERE key = 'autonomy_phase';
```

The pipeline now runs 4 steps (adds alerts as step 4).

**5. Add NSE events automation**

This will be a new `ingestion/ingest_nse_events.py` file — contact for Phase 1 delivery.

**6. Update ML training secret in GitHub Actions**

No changes needed — `evolution_weekly.yml` already runs every Sunday and trains the ML model on your growing closed positions data.

---

## PHASE 2 — Live Prices via Kite (Month 3-4)

**Goal:** Eliminate manual price updates. Your Sheet only needs positions updated.

### Prerequisites:
- Zerodha account (you already have this)
- Kite Connect developer account: https://developers.kite.trade
  - Free for personal use
  - Monthly fee may apply for high volume

### Steps:

**1. Get Kite API credentials**
1. Go to https://developers.kite.trade/login
2. Create an app → note your `api_key` and `api_secret`
3. Add to `.env`:
```bash
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
```

**2. Daily token refresh (required by Zerodha)**

Zerodha requires manual login once per day:
```bash
python kite/kite_token_refresh.py
# Opens browser → you login → token saved automatically
# Run this at 8:30 AM each trading day
```

This is unavoidable — Zerodha's security policy requires it.

**3. Industry Strength in Phase 2**

`chartink_raw_data` (daily Supabase append) becomes the primary data source for:
- Historical RSI/volume/momentum backlooks per symbol
- Cross-referencing industry breadth vs individual stock performance
- `independent_scanner.py` uses `top5_flag=True` industries as scan universe filter

**4. Enable Phase 2**
```sql
UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';
```

Pipeline now uses live Kite data for prices instead of Sheet.

---

## PHASE 3 — Supervised Execution (Month 5-6)

**Goal:** System executes trades with your explicit Telegram approval on each.

### Prerequisites:
- Phase 2 stable for 30+ days
- Win rate ≥ 50% on Phase 1 signals
- Kill switch tested

### Execution flow:
```
Signal fires
    ↓
Claude reviews
    ↓
Telegram message to you:
  "BUY SBIN 50 shares @ ₹1201
   Score: 72 | CTL | EAP: PRIORITISE
   Industry: STRONG #3 | Ind RSI: 61.2
   Claude: HIGH conviction — SL: ₹1152
   [✅ APPROVE] [❌ REJECT] [⏸ WAIT]"
    ↓
You tap APPROVE
    ↓
Kite order placed
    ↓
Confirmation sent to Telegram
```

**Enable Phase 3:**
```sql
UPDATE system_config SET value = '3' WHERE key = 'autonomy_phase';
```

---

## PHASE 4 — Full Autonomy (Month 7+)

**Activation criteria (ALL must be met):**
- 90 days of Phase 3 supervised execution
- Win rate ≥ 55%
- Max drawdown ≤ 8% during Phase 3
- You explicitly set:

```sql
UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase';
```

**System can now:**
- Execute trades within guardrails without per-trade approval
- Propose and (after your approval) apply strategy parameter changes
- Train and improve its own ML model weekly
- Generate lessons from every closed trade
- Use `chartink_raw_data` history for multi-day momentum pattern detection

**System can NEVER do without your approval:**
- Change position sizing formula
- Modify guardrail thresholds
- Add new data sources
- Change code

---

## Daily Operating Procedure (Phase 0-1)

**You do this after market close (4:00-5:30 PM):**
1. Update your Google Sheet as usual (prices, positions)
2. Pipeline runs automatically at 6 PM IST
3. `fetch_chartink.py` runs first — downloads Nifty 500 data, writes to Sheet tab + Supabase
4. `ingest_sheets.py` runs second — reads all tabs including freshly updated Chartink data
5. Check frontend for signals
6. Check Telegram (Phase 1) for alerts including industry context
7. Make trading decisions based on signals

**You do NOT need to:**
- Run any scripts manually
- Update Supabase manually
- Touch the backend code

---

## Emergency Procedures

**Kill switch (halt everything immediately):**
```bash
# From command line
cd tradeos-v6/backend
python control/kill_switch.py          # Activate
python control/kill_switch.py off      # Deactivate

# From Supabase SQL Editor
UPDATE system_config SET value = 'true' WHERE key = 'master_kill_switch';
UPDATE system_config SET value = 'false' WHERE key = 'master_kill_switch';
```

Or use the Settings tab in the frontend.

**Pipeline fails:**
1. Check GitHub Actions → failed run → view logs
2. Download log artifact from the failed run
3. Most common issues:
   - Sheet format changed → update row offsets in `ingest_sheets.py`
   - Supabase schema mismatch → check error, add missing column
   - Google API quota exceeded → wait 24 hours, or increase quota in Cloud Console
   - Chartink hover fails → check `chartink_hover_debug.png` saved in backend folder

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Could not find column 'X'` | Column missing from Supabase | Run ALTER TABLE or re-run schema SQL |
| `403 on Google Sheets` | Service account not shared on Sheet | Re-share Sheet with service account as **Editor** |
| `SUPABASE_URL not set` | .env not loaded | Check .env file exists and is in backend/ |
| `insufficient data (13 rows)` | Not enough history for indicators | Run backfill_msl_history.py first |
| `KILL SWITCH ACTIVE` | Kill switch was triggered | Run `python control/kill_switch.py off` |
| `ML model not found` | Model not yet trained | Need 30+ closed trades, runs Sunday |
| `CSV button never appeared` | Chartink hover not triggering | Check `chartink_hover_debug.png`; run with `headless=False` |
| `industry_strength empty` | Tab not ingested yet | Run `python ingestion/ingest_sheets.py` and check INDUSTRY_STRENGTH log line |
| `chartink_raw_data 0 rows` | Supabase upsert failed | Check service account key and UNIQUE constraint on date+symbol |

---

## File Reference

```
tradeos-v6/
├── DEPLOYMENT_README.md             ← This file
├── .gitignore
├── .github/workflows/
│   ├── pipeline_daily.yml           ← 6 PM IST weekdays
│   └── evolution_weekly.yml         ← Sunday ML training
├── frontend/
│   └── App_v6.jsx                   ← Full frontend (React)
└── backend/
    ├── run_pipeline.py              ← Main orchestrator
    │                                     step_fetch_chartink() runs before step_ingest()
    ├── config.py                    ← All settings, Supabase client
    ├── requirements.txt
    ├── .env.example                 ← Copy to .env
    ├── db/
    │   ├── schema_v6_base.sql       ← Run FIRST (includes chartink_raw_data table)
    │   ├── schema_v6_signals.sql    ← Run SECOND (includes industry columns on signal_log)
    │   └── schema_rls.sql           ← Run LAST
    ├── ingestion/
    │   ├── fetch_chartink.py        ← Phase 0a: Playwright → Chartink Atlas CSV →
    │   │                                 Google Sheet "Chartink Raw Data_Nifty 500" (header preserved)
    │   │                                 + Supabase chartink_raw_data (daily append, UNIQUE date+symbol)
    │   └── ingest_sheets.py         ← Phase 0b: all 15 Sheet tabs including:
    │                                     • industry_strength → Supabase (daily append)
    │                                     Runs AFTER fetch_chartink
    ├── signals/
    │   └── generate_signals.py      ← CTL+SBS+TPO+EAP engine
    │                                     Phase 0: loads industry_strength, stores on signal_log
    │                                     Phase 1: scoring bonuses active (top5 +10, STRONG +5)
    ├── ai/
    │   ├── ai_router.py             ← Routes to configured provider
    │   ├── providers/               ← Claude, OpenAI, Gemini, DeepSeek, Grok, Copilot, ML
    │   └── fallback/                ← Web scraping + sentiment scoring
    ├── alerts/
    │   └── send_alerts.py           ← Telegram (Phase 1)
    │                                     Includes: Ind: STRONG #3 per BUY CANDIDATE
    ├── control/
    │   └── kill_switch.py           ← Emergency halt
    ├── history/
    │   └── append_history.py        ← Daily MSL snapshot
    └── scripts/
        └── backfill_msl_history.py  ← One-time: load 708 history rows
```

---

## What Gets Built in Future Phases

| Component | Phase | Description |
|---|---|---|
| `ingestion/ingest_fii_dii.py` | 1 | Daily FII/DII flows from NSE |
| `ingestion/ingest_nse_events.py` | 1 | Auto NSE corporate filings calendar |
| `signals/independent_scanner.py` | 1 | Pattern scanner beyond rule engine |
| `signals/score_industry.py` | 1 | Industry rank/state as signal scoring input (top5 +10, STRONG +5) |
| `ai/claude_enrich.py` | 1 | AI conviction on signals |
| `ingestion/ingest_global_cues.py` | 1 | Gift Nifty, USD/INR, crude (8 AM) |
| `kite/kite_client.py` | 2 | Zerodha live prices |
| `ingestion/ingest_asm_gsm.py` | 2 | Safety list from NSE |
| `ingestion/ingest_bulk_deals.py` | 2 | Institutional deal detection |
| `compute/compute_indicators.py` | 2 | Auto-compute from Kite data |
| `compute/chartink_history_analysis.py` | 2 | Multi-day momentum from chartink_raw_data history |
| `control/execution_engine.py` | 3 | Kite order execution |
| `control/telegram_bot.py` | 3 | Approval interface (includes industry context in message) |
| `history/evolution_tracker.py` | 4 | Weekly rule proposals |

---

*Built specifically for your Google Sheet structure (25 tabs analyzed).*
*All row offsets are hardcoded to match your exact Sheet layout.*
