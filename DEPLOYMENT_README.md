# TradeOS v6 — Deployment Guide
**Step-by-step from zero to fully autonomous trading system**

---

## Architecture Overview

```
Your Google Sheet (Brain)
       ↓ ingest_sheets.py (6 PM daily)
   Supabase (Database)
       ↓ generate_signals.py
   Signal Log + Frontend
       ↓ (Phase 1+)
   AI Enrichment + Telegram
       ↓ (Phase 2+)
   Kite Live Prices + Auto Indicators
       ↓ (Phase 3+)
   Supervised Execution + Kill Switch
       ↓ (Phase 4+)
   Full Autonomy + Self-Evolution
```

#testing for git upload# 
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
9. Open your Google Sheet → Share → paste the service account email (ends in `@...iam.gserviceaccount.com`) → Viewer access
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

---

### Step 5: Set up Supabase database

1. Go to your Supabase project → **SQL Editor**
2. Copy and run each file in this exact order:

```
backend/db/schema_v6_base.sql      ← Run FIRST
backend/db/schema_v6_signals.sql   ← Run SECOND
backend/db/schema_rls.sql          ← Run LAST
```

3. Verify tables exist: go to **Table Editor** → you should see:
   `stock_data_daily, master_shortlist, open_positions, closed_positions,`
   `sector_strength, market_regime, event_calendar, lessons, msl_history,`
   `nse_holidays, raw_prices, system_config, strategy_config, signal_log,`
   `regime_history, nifty_upcoming_events, nifty_total_market`

---

### Step 6: Test ingestion locally

```bash
cd tradeos-v6/backend

# Test a single step first
python ingestion/ingest_sheets.py

# Check output — you should see something like:
# ✓ STOCK_DATA: 500 rows upserted
# ✓ MASTER_SHORTLIST: 35 rows upserted
# ✓ OPEN_POSITIONS: 8 rows synced
# ... etc
```

If you see errors:
- `GOOGLE_CREDENTIALS` error → check Step 3 above
- `SUPABASE` error → check your SUPABASE_URL and SUPABASE_SERVICE_KEY
- `column not found` → re-run the SQL schema files

---

### Step 7: One-time MSL history backfill

```bash
python scripts/backfill_msl_history.py
# Loads the 708 existing MSL history rows from your Sheet
# Only run this once
```

---

### Step 8: Test signal generation

```bash
python signals/generate_signals.py

# You should see:
# Signal breakdown: {'BUY_CANDIDATE': X, 'WATCH': Y, 'OPEN_POSITION': Z}
# Regime: RISK OFF
# BUY CANDIDATES: X
```

---

### Step 9: Run full pipeline test

```bash
python run_pipeline.py --dry-run
# Tests all steps without writing to Supabase
# All steps should show ✅ OK

python run_pipeline.py
# Real run — writes to Supabase
```

---

### Step 10: Deploy frontend

**Option A — Use with Claude.ai:**
use html

**Option B — Deploy on Vercel/Netlify (recommended):**

npm run dev
# Opens at http://localhost:5173

**Option C — Run locally:**
cd tradeos-v6/frontend
npx vite

---

### Step 11: Automate with GitHub Actions

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
```

4. Go to **Actions** tab → you should see the workflows listed
5. Click "TradeOS v6 Daily Pipeline" → "Run workflow" → test it manually once
6. From tomorrow it runs automatically at 6 PM IST weekdays

---

### Phase 0 is complete when:
- ✅ `python run_pipeline.py` completes without errors
- ✅ Supabase tables populated with your Sheet data
- ✅ Frontend shows your positions, sectors, and BUY candidates
- ✅ RISK OFF warning banner visible (current regime)
- ✅ GitHub Actions running at 6 PM IST

---

## PHASE 1 — Intelligence + Automation (Month 2)

**Goal:** Eliminate manual weekly steps. Add AI reasoning. Add FII/DII data.

### What changes:
- AI provider activated (your choice) OR free ML/scraping fallback
- NSE events auto-fetched (no more manual update)
- FII/DII flow data from NSE
- Telegram alerts enabled
- Independent scanner runs alongside rule engine

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

**2. Enable Telegram alerts**

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

**3. Enable Phase 1 pipeline**

```sql
UPDATE system_config SET value = '1' WHERE key = 'autonomy_phase';
```

The pipeline now runs 4 steps (adds alerts as step 4).

**4. Add NSE events automation**

This will be a new `ingestion/ingest_nse_events.py` file — contact for Phase 1 delivery.

**5. Update ML training secret in GitHub Actions**

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

**3. Enable Phase 2**
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
3. Check frontend for signals
4. Check Telegram (Phase 1) for alerts
5. Make trading decisions based on signals

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

---

## Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `Could not find column 'X'` | Column missing from Supabase | Run ALTER TABLE or re-run schema SQL |
| `403 on Google Sheets` | Service account not shared on Sheet | Re-share Sheet with service account email |
| `SUPABASE_URL not set` | .env not loaded | Check .env file exists and is in backend/ |
| `insufficient data (13 rows)` | Not enough history for indicators | Run backfill_msl_history.py first |
| `KILL SWITCH ACTIVE` | Kill switch was triggered | Run `python control/kill_switch.py off` |
| `ML model not found` | Model not yet trained | Need 30+ closed trades, runs Sunday |

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
    ├── config.py                    ← All settings, Supabase client
    ├── requirements.txt
    ├── .env.example                 ← Copy to .env
    ├── db/
    │   ├── schema_v6_base.sql       ← Run FIRST
    │   ├── schema_v6_signals.sql    ← Run SECOND
    │   └── schema_rls.sql           ← Run LAST
    ├── ingestion/
    │   ├── ingestion/
    │   ├── fetch_chartink.py        ← Phase 0a: Chartink CSV → "Chartink Raw Data_Nifty 500"
    │   └── ingest_sheets.py         ← Phase 0b: all 15 Sheet tabs (runs AFTER fetch_chartink)
    │                                     
    ├── signals/
    │   └── generate_signals.py      ← CTL+SBS+TPO+EAP engine
    ├── ai/
    │   ├── ai_router.py             ← Routes to configured provider
    │   ├── providers/               ← Claude, OpenAI, Gemini, DeepSeek, Grok, Copilot, ML
    │   └── fallback/                ← Web scraping + sentiment scoring
    ├── alerts/
    │   └── send_alerts.py           ← Telegram (Phase 1)
    ├── control/
    │   └── kill_switch.py           ← Emergency halt
    ├── history/
    │   └── append_history.py        ← Daily MSL snapshot
    └── scripts/
        └── backfill_msl_history.py  ← One-time: load 708 history rows

---

## What Gets Built in Future Phases

| Component | Phase | Description |
|---|---|---|
| `ingestion/ingest_fii_dii.py` | 1 | Daily FII/DII flows from NSE |
| `ingestion/ingest_nse_events.py` | 1 | Auto NSE corporate filings calendar |
| `signals/independent_scanner.py` | 1 | Pattern scanner beyond rule engine |
| `ai/claude_enrich.py` | 1 | AI conviction on signals |
| `ingestion/ingest_global_cues.py` | 1 | Gift Nifty, USD/INR, crude (8 AM) |
| `kite/kite_client.py` | 2 | Zerodha live prices |
| `ingestion/ingest_asm_gsm.py` | 2 | Safety list from NSE |
| `ingestion/ingest_bulk_deals.py` | 2 | Institutional deal detection |
| `compute/compute_indicators.py` | 2 | Auto-compute from Kite data |
| `control/execution_engine.py` | 3 | Kite order execution |
| `control/telegram_bot.py` | 3 | Approval interface |
| `history/evolution_tracker.py` | 4 | Weekly rule proposals |

---

*Built specifically for your Google Sheet structure (25 tabs analyzed).*
*All row offsets are hardcoded to match your exact Sheet layout.*
