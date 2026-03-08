# TradeOS v6 — Deployment Guide
**Phase 0 is complete. This guide covers Phase 1 → 4.**

---

## Table of Contents

1. [Where You Are Now](#1-where-you-are-now)
2. [Phase 1 — Intelligence Layer](#2-phase-1--intelligence-layer)
3. [Phase 2 — Computation Engine (Sheet Elimination)](#3-phase-2--computation-engine)
4. [Phase 3 — Supervised Execution](#4-phase-3--supervised-execution)
5. [Phase 4 — Full Autonomy](#5-phase-4--full-autonomy)
6. [Frontend Evolution](#6-frontend-evolution--what-the-dashboard-gains-each-phase)
7. [Daily Operating Procedure](#7-daily-operating-procedure)
8. [Emergency Procedures](#8-emergency-procedures)
9. [Troubleshooting Reference](#9-troubleshooting-reference)
10. [Full Repository Structure & Script Reference](#10-full-repository-structure--script-reference)

---

## 1. Where You Are Now

**Phase 0 is live.** Your system currently runs automatically every evening at 6 PM IST:

- `fetch_chartink.py` scrapes Chartink Atlas → 500-stock CSV → Google Sheet + `chartink_raw_data` Supabase table
- `ingest_sheets.py` reads all 15 Sheet tabs → syncs to Supabase
- `generate_signals.py` runs CTL + SBS + TPO + EAP rule engine → writes `signal_log`
- `append_history.py` saves daily MSL snapshot → `msl_history`

**What is NOT yet automated (Phase 1 adds these):**
- AI conviction analysis on buy candidates
- FII/DII flow ingestion from NSE
- NSE events auto-fetch (you still update Sheet manually)
- Telegram alerts
- Two scoring dimensions gated off: industry rank (+10) and industry state (+5) — stored since Phase 0, just not yet contributing to scores
- Bhavcopy delivery% ingestion (script exists, not wired into pipeline yet)

---

## 2. Phase 1 — Intelligence Layer

**Goal:** AI reasoning, FII context, industry scoring, Telegram alerts. Eliminates all remaining manual data entry except position management.

**Timeline:** 4–6 weeks after Phase 0 is stable.

**Prerequisites before starting:**
- Phase 0 pipeline has run cleanly for 30+ consecutive trading days
- `chartink_raw_data` table has 30+ days of data
- `msl_history` has 30+ rows
- You have at least 30 closed trades (you have 54 — ML model is ready now)

---

### Step 1.1 — Wire Bhavcopy into the Pipeline

Your bhavcopy script already exists. You just need to add it as a named step.

**Open `backend/run_pipeline.py` and add this function:**

```python
def step_bhavcopy():
    """Fetch NSE bhavcopy for delivery%, delivery_qty, value_cr"""
    logger.info("=== STEP 0b: NSE Bhavcopy ===")
    result = subprocess.run(
        [sys.executable, "ingestion/ingest_bhavcopy.py"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error(f"Bhavcopy failed: {result.stderr}")
    else:
        logger.info(result.stdout)
```

**Add it to the steps list, after fetch_chartink and before ingest:**

```python
steps = [
    ("fetch_chartink", step_fetch_chartink),
    ("bhavcopy",       step_bhavcopy),       # ADD THIS LINE
    ("ingest",         step_ingest),
    ("signals",        step_signals),
    ("history",        step_history),
]
```

**Test:**
```bash
cd backend
python run_pipeline.py --step ingest_bhavcopy
```

Expected: `Bhavcopy: upserted 500 rows to stock_data_daily`

---

### Step 1.2 — Activate All Phase 1 Scoring Dimensions

`generate_signals.py` scores each stock across multiple dimensions. The full scoring framework covers RSI (daily/weekly/monthly), momentum, volume ratio, delivery%, position in 52-week range, sector rank, industry rank, pattern setup, and fundamentals. Each dimension has a weight.

Two of those dimensions — industry rank and industry state — have been collecting data since Phase 0 but their score contribution was gated off. This step turns them on alongside the other scoring components.

**Why they were gated:** The `industry_strength` table needed 30+ days of history before the rank and state values were stable enough to trust. That window has now passed.

**In Supabase SQL editor:**
```sql
INSERT INTO system_config (key, value)
VALUES ('industry_scoring_active', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';
```

**Verify `generate_signals.py` has this block inside the main scoring function.** If missing, add it alongside the other scoring dimensions (not as a separate block — it belongs with the rest of the score computation):

```python
# Industry rank and state — two scoring dimensions among ~10 total
# Only active once industry_strength table has 30+ days of history
if config.get('industry_scoring_active') == 'true':
    if row.get('industry_top5'):
        score += 10   # stock's industry is in top 5 by RSI breadth rank
    if row.get('industry_state') == 'STRONG':
        score += 5    # industry RSI breadth is in strong zone
```

The combined +15 maximum is in proportion to other dimensions (RSI monthly contributes up to +20, volume ratio up to +10, etc.). Do not inflate these weights relative to the others.

**Commit and push to git after adding.**

---

### Step 1.3 — Set Up FII/DII Ingestion

**Test the script locally first:**
```bash
cd backend
python ingestion/ingest_fii_dii.py --dry-run
```

Expected output:
```
FII Net: -₹1,245 Cr | DII Net: +₹2,103 Cr
Rolling 5d FII: -₹3,420 Cr → flag: CAUTION
Dry run — no rows written
```

If errors appear, check NSE endpoint availability. The script fetches from NSE's public FII/DII data page.

**Add to pipeline steps in `run_pipeline.py`:**
```python
steps = [
    ("fetch_chartink", step_fetch_chartink),
    ("bhavcopy",       step_bhavcopy),
    ("ingest",         step_ingest),
    ("fii_dii",        step_fii_dii),         # ADD after ingest
    ("signals",        step_signals),
    ("history",        step_history),
]
```

**After this is live**, the `fii_flag` column (`CAUTION` / `ACCELERATOR` / `NEUTRAL`) will appear in your signal context and Telegram alerts.

---

### Step 1.4 — Set Up NSE Events Auto-Fetch

**Test locally:**
```bash

python ingestion/ingest_nse_events.py --dry-run
```

Expected:
```
Fetched 47 upcoming corporate events from NSE
Mapped EAP: 3 stocks PRIORITISE, 2 stocks AVOID_ENTRY
Dry run — no rows written
```

**Add to pipeline:**
```python
("nse_events",  step_nse_events),   # after fii_dii, before signals
```

**After this is live, stop updating the Event Calendar tab in Google Sheet manually.** The pipeline owns it.

---

### Step 1.5 — Set Up 8 AM Global Cues Pipeline

Create `.github/workflows/pipeline_morning.yml`:

```yaml
name: Morning Pipeline
on:
  schedule:
    - cron: '30 2 * * 1-5'    # 8:00 AM IST = 2:30 AM UTC weekdays
  workflow_dispatch:           # manual trigger for testing

jobs:
  morning:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r backend/requirements.txt
      - run: python backend/ingestion/ingest_global_cues.py
        env:
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

**Test by triggering manually from GitHub Actions tab** (workflow_dispatch) before relying on the schedule.

---

### Step 1.6 — Choose and Configure AI Provider

You have 7 provider options. Start with ML (free) — it trains on your own trade history.

**Option A — ML Model (Recommended first)**

No API key needed. Trains on your closed trade history.

Before training, you must back-fill signal outcomes from your 54 closed trades.
The ML model learns from `signal_log.outcome` (WIN/LOSS) — this field is populated
by `post_trade_analysis.py` when it matches closed positions back to their original signals.

**Step 1: Ensure signal_log has rows**
```bash
# Run the full pipeline at least once so signal_log is populated
cd backend
python run_pipeline.py
```

**Step 2: Back-fill outcomes from closed trades**
```bash
# post_trade_analysis matches closed_positions → signal_log and writes WIN/LOSS outcomes
# Also generates lessons from each closed trade → lessons table
cd backend
python -m ai.post_trade_analysis
```

Expected output:
```
Processing 54 closed trades...
Matched 38 trades to signal_log entries
Outcomes written: 38 (WIN/LOSS)
Lessons generated: 38 → lessons table
```

> Note: Not all 54 closed trades will match — only trades that have a corresponding
> entry in signal_log (i.e. were active while the pipeline was running). Unmatched
> trades are skipped silently. This improves over time as the pipeline accumulates history.

**Step 3: Train the ML model**
```bash
cd backend
python -m ai.providers.ml_provider
```

Expected output:
```
Training on 38 labelled trades...
CV accuracy: 62%
Top features: rsi_weekly (0.18) > vol_ratio (0.14) > delivery_pct (0.12)
Model saved → models/ml_conviction.pkl
```

> If you see "No labelled signal outcomes yet — skipping ML training", Step 2 produced
> no matches. This means signal_log was empty when trades closed. In that case, skip ML
> for now and come back after the pipeline has been running for 2–3 weeks.

Enable in Supabase:
```sql
UPDATE system_config SET value = 'ml' WHERE key = 'ai_provider';
```

---

**Option B — DeepSeek (Cheapest paid, ~10x cheaper than Claude)**
```
OPENAI

Go to platform.openai.com → Sign up / Log in
Click your profile → API Keys → Create new secret key → name it tradeos
Copy the key immediately
Add credits: Settings → Billing → Add payment method
Add to .env: OPENAI_API_KEY=sk-xxxxxxxx
Add to GitHub secrets: Name = OPENAI_API_KEY
Enable: UPDATE system_config SET value = 'openai' WHERE key = 'ai_provider';


DEEPSEEK

Go to platform.deepseek.com → Sign up / Log in
Click profile icon → API Keys → Create new API key → name it tradeos
Copy the key immediately
Add credits: Top Up → minimum $5 lasts months
Add to .env: DEEPSEEK_API_KEY=sk-xxxxxxxx
Add to GitHub secrets: Name = DEEPSEEK_API_KEY
Enable: UPDATE system_config SET value = 'deepseek' WHERE key = 'ai_provider';


CLAUDE

Go to console.anthropic.com → Sign up / Log in
Click API Keys in left sidebar → Create Key → name it tradeos
Copy the key immediately
Add credits: Plans & Billing → Add payment method
Add to .env: ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
Add to GitHub secrets: Name = ANTHROPIC_API_KEY
Enable: UPDATE system_config SET value = 'claude' WHERE key = 'ai_provider';


GEMINI

Go to aistudio.google.com → Sign in with Google account
Click Get API Key → Create API key → select or create a Google Cloud project
Copy the key
Free tier available — no billing needed to start
Add to .env: GEMINI_API_KEY=AIzaxxxxxxxx
Add to GitHub secrets: Name = GEMINI_API_KEY
Enable: UPDATE system_config SET value = 'gemini' WHERE key = 'ai_provider';


GROK (xAI)

Go to console.x.ai → Sign in with X (Twitter) account
Click API Keys → Create API Key → name it tradeos
Copy the key immediately
Add credits: Billing → Add payment method
Add to .env: GROK_API_KEY=xai-xxxxxxxx
Add to GitHub secrets: Name = GROK_API_KEY
Enable: UPDATE system_config SET value = 'grok' WHERE key = 'ai_provider';


COPILOT (Azure OpenAI)

Go to portal.azure.com → Sign in with Microsoft account
Search Azure OpenAI → Create → fill in subscription, resource group, region
Once deployed, go to the resource → Keys and Endpoint → copy Key 1 and Endpoint
Go to Azure OpenAI Studio → Deployments → Create new deployment → select gpt-4o → note the deployment name
Add to .env:

envAZURE_OPENAI_API_KEY=xxxxxxxx
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=gpt-4o

Add to GitHub secrets: AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT
Enable: UPDATE system_config SET value = 'copilot' WHERE key = 'ai_provider';


Cost controls — run once regardless of provider:
sqlUPDATE system_config SET value = '200' WHERE key = 'ai_daily_budget_inr';
UPDATE system_config SET value = '20'  WHERE key = 'ai_max_stocks_per_day';

Recommendation for getting started: DeepSeek first — cheapest, no billing complexity, works immediately after top-up. Switch to Claude later if you want better reasoning quality on enrichment prompts.

Validation 
## Step a - python -c "from config import AI_KEYS; print(AI_KEYS)"
```
Expected output — your configured provider should show a key, others empty:
```
{'claude': '', 'openai': '', 'gemini': '', 'deepseek': 'sk-xxxx...', 'grok': '', 'copilot': ''}

## Step b
python -c "from config import cfg; print('Provider:', cfg('ai_provider'))"
```

Expected:
```
Provider: deepseek or whatever you want to configure

## Step b
python -m ai.post_trade_analysis

```
**Always set cost controls regardless of provider:**
```sql
UPDATE system_config SET value = '200' WHERE key = 'ai_daily_budget_inr';
UPDATE system_config SET value = '20'  WHERE key = 'ai_max_stocks_per_day';
```

---

### Step 1.7 — Set Up Telegram Alerts

**Create your Telegram bot (5 minutes):**

1. Open Telegram → search `@BotFather` → send `/newbot`
2. Choose a name (e.g., `TradeOS`) and username (e.g., `tradeos_yourname_bot`)
3. BotFather returns a token like `7234567890:AAHxxxxxxx` — copy it
4. Open your new bot in Telegram and press Start
5. Get your chat ID: open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser after pressing Start. Find `"chat":{"id":XXXXXXXXX}` — that number is your chat ID

**Add to GitHub Actions secrets:**
- `TELEGRAM_BOT_TOKEN` = your bot token
- `TELEGRAM_CHAT_ID` = your numeric chat ID

**Test locally:**
```bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
cd backend
python alerts/send_alerts.py --test
```

You should receive a test message in Telegram within 10 seconds.

**Enable alerts in Supabase:**
```sql
UPDATE system_config SET value = 'true' WHERE key = 'telegram_alerts_enabled';
```

**What a full evening alert looks like:**
```
TRADEOS DAILY — 07 Mar 2026
Regime: RISK OFF | VIX: 13.7

BUY CANDIDATES (2):
• GVT&D [CTL] Score:87 | Ind: STRONG #2 | AI: HIGH | FII: NEUTRAL
  Zone: ₹3820–3880 | CMP: ₹3848 | RSI: 61/66 | Vol: 1.8x
• SBIN [TPO] Score:79 | Ind: NEUTRAL #6 | AI: MED | ⚡ EAP PRIORITISE
  Zone: ₹1190–1215 | CMP: ₹1201 | RSI: 48/52 | Vol: 1.4x

POSITIONS WITH ACTION:
• TATASTEEL → ADD signal | +28.5% | Lifecycle: TREND_MATURE

FII: CAUTION (5d net: -₹3,420 Cr)
```

---

### Step 1.8 — Full Dry Run and Activation

**Run the complete pipeline in dry-run mode:**
```bash
cd backend
python run_pipeline.py --dry-run
```

Confirm all steps execute in order and the log shows each step completing cleanly.

**Run live once to verify:**
```bash
python run_pipeline.py
```

**Verify results in Supabase:**
```sql
-- Confirm industry scoring on signals
SELECT symbol, industry, industry_rank, industry_top5, final_score
FROM signal_log
WHERE date = CURRENT_DATE
ORDER BY final_score DESC LIMIT 10;

-- Confirm AI conviction populated
SELECT symbol, ai_conviction, ai_risks
FROM master_shortlist
WHERE date = CURRENT_DATE AND position_state = 'BUY_CANDIDATE';

-- Confirm FII data
SELECT * FROM fii_dii_flow ORDER BY date DESC LIMIT 1;
```

**Set phase to 1:**
```sql
UPDATE system_config SET value = '1' WHERE key = 'autonomy_phase';
```

**Phase 1 is complete when:**
- Telegram alerts arrive at 8 AM and 6 PM on trading days
- Each BUY signal shows `Ind: STRONG #2 | AI: HIGH | FII: NEUTRAL` style context
- NSE events auto-populating — no more manual Sheet updates
- ML model trained and giving conviction scores

---

## 3. Phase 2 — Computation Engine

**Goal:** Replace all Google Sheet formula columns with Python (`compute_indicators.py`). Sheet becomes read-only for everything except open/closed position entry.

**Timeline:** Month 3–4. Phase 1 must be stable for 30+ days first.

---

### Step 2.1 — Audit STOCK_DATA Sheet Formulas

Before writing any code, spend 30 minutes mapping every formula column in your STOCK_DATA Sheet.

For each column, note the formula and its inputs. The expected mapping:

**From `chartink_raw` directly (no computation needed):**
open, high, low, close, volume, avg_vol_20, avg_vol_50, rsi_daily, rsi_weekly, rsi_monthly, sma_10/20/50/200, ema_10/20/50, macd_line/signal, adx_14, supertrend, stochastic, bollinger_upper/lower, atr_14, vwap, ttm_net_profit, eps, market_cap, sector, industry

**From `bhavcopy` (already in pipeline from Step 1.1):**
delivery_pct, delivery_qty, value_cr

**Computed from above (your Sheet formulas → Python):**

| Column | Formula to Port |
|--------|-----------------|
| vol_ratio | volume / avg_vol_20 |
| ret_1w/1m/3m/6m/12m | (close_today - close_N_days_ago) / close_N_days_ago |
| above_sma50 | 1 if close > sma_50 else 0 |
| sma50_gt_200 | 1 if sma_50 > sma_200 else 0 |
| dist_sma50 | (close - sma_50) / sma_50 * 100 |
| consol_range | (week52_high - week52_low) / close * 100 |
| price_location | (close - week52_low) / (week52_high - week52_low) |
| breakout_setup | your composite condition — check Sheet formula |
| rs_vs_nifty | stock_ret_3m - nifty_ret_3m |

Screenshot or paste your exact breakout_setup formula — it is the most proprietary and must be ported exactly.

---

### Step 2.2 — Build compute_indicators.py

**Create `backend/compute/compute_indicators.py`.** The structure:

```python
"""
compute_indicators.py — Phase 2
Replaces all Google Sheet formula columns.
Input:  chartink_raw_data + stock_data_daily (bhavcopy columns already there)
Output: stock_data_daily (adds computed columns via upsert)
"""
import pandas as pd
from datetime import date, timedelta
from config import get_supabase

def run(date_str: str = None):
    if not date_str:
        date_str = str(date.today())
    sb = get_supabase()

    # Load today's chartink raw data
    raw = pd.DataFrame(
        sb.table("chartink_raw_data").select("*").eq("date", date_str).execute().data
    )
    if raw.empty:
        raise ValueError(f"No chartink_raw_data for {date_str}. Run fetch_chartink.py first.")

    # Load today's bhavcopy columns (already in stock_data_daily from ingest_bhavcopy)
    bhav = pd.DataFrame(
        sb.table("stock_data_daily")
          .select("symbol, delivery_pct, delivery_qty, value_cr")
          .eq("date", date_str).execute().data
    )
    df = raw.merge(bhav, on="symbol", how="left")

    # ── Computed columns (replace Sheet formulas) ──────────────────────────
    df["vol_ratio"]      = (df["volume"] / df["avg_vol_20"].replace(0, 1)).round(2)
    df["above_sma50"]    = (df["close"] > df["sma_50"]).astype(int)
    df["sma50_gt_200"]   = (df["sma_50"] > df["sma_200"]).astype(int)
    df["dist_sma50"]     = ((df["close"] - df["sma_50"]) / df["sma_50"].replace(0,1) * 100).round(2)
    df["consol_range"]   = ((df["week52_high"] - df["week52_low"]) / df["close"].replace(0,1) * 100).round(1)
    df["price_location"] = ((df["close"] - df["week52_low"]) /
                            (df["week52_high"] - df["week52_low"]).replace(0,1)).clip(0,1).round(3)

    # Returns — uses chartink_raw_data history
    df["ret_1w"]  = _compute_returns(sb, df, date_str, days=5)
    df["ret_1m"]  = _compute_returns(sb, df, date_str, days=21)
    df["ret_3m"]  = _compute_returns(sb, df, date_str, days=63)
    df["ret_6m"]  = _compute_returns(sb, df, date_str, days=126)
    df["ret_12m"] = _compute_returns(sb, df, date_str, days=252)

    # Nifty-relative strength
    nifty_ret_3m = _nifty_return(sb, date_str, days=63)
    df["rs_vs_nifty"] = (df["ret_3m"] - nifty_ret_3m).round(3)

    # Breakout setup — port your exact Sheet condition here
    df["breakout_setup"] = (
        (df["dist_sma50"].abs() < 5) &
        (df["above_sma50"] == 1) &
        (df["rsi_daily"].between(48, 72)) &
        (df["vol_ratio"] >= 1.2)
    ).astype(int)

    # ── Upsert back to stock_data_daily ────────────────────────────────────
    cols_to_write = [
        "symbol", "vol_ratio", "above_sma50", "sma50_gt_200", "dist_sma50",
        "consol_range", "price_location", "ret_1w", "ret_1m", "ret_3m",
        "ret_6m", "ret_12m", "rs_vs_nifty", "breakout_setup"
    ]
    records = df[cols_to_write].assign(date=date_str).to_dict(orient="records")
    sb.table("stock_data_daily").upsert(records, on_conflict="date,symbol").execute()
    print(f"compute_indicators: {len(df)} rows processed for {date_str}")

def _compute_returns(sb, df_today, date_str, days):
    """Compute N-day return by comparing today's close to close N trading days ago"""
    target_date = _nth_trading_day_before(date_str, days)
    hist = pd.DataFrame(
        sb.table("chartink_raw_data")
          .select("symbol, close")
          .eq("date", str(target_date)).execute().data
    ).rename(columns={"close": "close_past"})
    merged = df_today[["symbol","close"]].merge(hist, on="symbol", how="left")
    return ((merged["close"] - merged["close_past"]) / merged["close_past"].replace(0,1)).round(4)

def _nth_trading_day_before(date_str, n):
    d = date.fromisoformat(date_str) - timedelta(days=int(n * 1.45))
    return d  # approximate — refine with NSE holiday calendar if needed

def _nifty_return(sb, date_str, days):
    """Get Nifty 50 return over N days from nifty_total_market table"""
    rows = sb.table("nifty_total_market").select("date, close")\
             .lte("date", date_str).order("date", desc=True).limit(days+5).execute().data
    if len(rows) < 2:
        return 0
    return (rows[0]["close"] - rows[-1]["close"]) / rows[-1]["close"]

if __name__ == "__main__":
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(date_arg)
```

**Add to pipeline after ingest_sheets:**
```python
("compute",  step_compute_indicators),   # after ingest, before signals
```

---

### Step 2.3 — Backfill Historical Computations

Once working for today, run for all historical dates:

```bash
# Backfill — uses chartink_raw_data history accumulating since Phase 0
cd backend
python compute/compute_indicators.py --backfill --from 2025-01-01
```

Verify:
```sql
SELECT COUNT(*) as rows_with_returns
FROM stock_data_daily
WHERE ret_3m IS NOT NULL AND date >= '2025-01-01';
```

---

### Step 2.4 — Port MSL Scoring into generate_signals.py

Your final_score, validity_score, entry_zone calculations currently live as Sheet formulas. Move them into `generate_signals.py`.

**Key questions to answer from your Sheet before coding:**
- What columns make up final_score for each strategy?
- How is entry_zone_low/high calculated (ATR-based? Fixed %?)
- What sets lifecycle state? (single column, or computed from multiple?)
- What is the validity_score formula?

Port each formula exactly. Do not approximate.

---

### Step 2.5 — Set Up Kite Connect

**Prerequisites:** Zerodha Kite Connect subscription (₹2,000/month from kite.trade/apps).

**One-time setup:**
1. Create app at kite.trade → get `api_key` and `api_secret`
2. Add to `.env` and GitHub Actions secrets: `KITE_API_KEY`, `KITE_API_SECRET`

**Daily token refresh (manual — Zerodha security requirement, unavoidable):**

```bash
# Run every morning ~8:30 AM before market opens
python backend/kite/kite_token_refresh.py
```

This opens a browser. You login to Zerodha. The script captures the redirect URL and saves `access_token` to Supabase. All subsequent pipeline steps that need Kite prices use this saved token.

---

### Step 2.6 — Build Data Quality Monitor

**Create `backend/compute/data_quality_monitor.py`:**

```python
CHECKS = [
    # (table, column, check_type, bounds, auto_fix)
    ("stock_data_daily",  "rsi_daily",    "range",  (0, 100),   True),
    ("stock_data_daily",  "vol_ratio",    "max",    50,         True),   # cap outliers
    ("stock_data_daily",  "delivery_pct", "range",  (0, 100),   False),
    ("chartink_raw_data", "symbol",       "count",  (450, 510), False),  # expect ~500 rows
    ("master_shortlist",  "final_score",  "range",  (0, 120),   False),
]

def run_all_checks(date_str) -> dict:
    failures, warnings, fixes = [], [], []
    for check in CHECKS:
        result = validate(*check, date_str)
        if result.is_failure:
            if check[4]:  # auto_fix
                apply_correction(*check, date_str)
                fixes.append(f"AUTO-FIXED: {check[0]}.{check[1]}")
            else:
                failures.append(f"FAIL: {check[0]}.{check[1]} — {result.detail}")
        elif result.is_warning:
            warnings.append(f"WARN: {check[0]}.{check[1]} — {result.detail}")
    return {"failures": failures, "warnings": warnings, "fixes": fixes}
```

If any failures, sends Telegram alert and logs to `data_anomalies` table.

**Add as last pipeline step:**
```python
("quality_check", step_quality_check),   # always last
```

---

### Step 2.7 — Activate Phase 2

After 7+ clean days of compute_indicators running alongside the pipeline:

```sql
UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';
```

**From this point, the Sheet is read-only for:**
- Price data (Kite/Chartink owns this)
- All technical indicator columns (Python owns this)
- Score calculations (generate_signals.py owns this)

**You still update the Sheet for:**
- Open positions (entry date, entry price, SL updates)
- Closed positions (exit date, exit price, reason)
- Manual regime overrides if needed

---

## 4. Phase 3 — Supervised Execution

**Goal:** System places trades via Kite. Every order requires your explicit Telegram tap to approve.

**Timeline:** Month 5–6.

**Activation criteria — ALL required:**
- Phase 2 stable for 30+ days
- Win rate ≥ 50% on Phase 1/2 signals (check Analytics tab)
- Kill switch tested — activate and confirm pipeline halts, then deactivate
- You have read and understood all risk_manager.py guardrail checks
- Shadow trading tested for 2+ weeks (Step 3.4)

---

### Step 3.1 — Build Telegram Approval Bot

**File:** `backend/control/telegram_bot.py`

This runs as a persistent service (deploy on Render.com free tier or a VPS).

**Approval message format:**
```
🟡 BUY SIGNAL — GVT&D
Strategy: CTL | Score: 87 | AI: HIGH conviction
Entry zone: ₹3820–3880 | CMP: ₹3848
Qty: 3 shares | Value: ₹11,544 (5.8% of capital)
Industry: Industrials STRONG #2 | FII: NEUTRAL
EAP: NO_CHANGE | Regime: RISK OFF

[✅ APPROVE] [❌ REJECT] [⏸ DEFER]

Expires in 4 hours
```

**Key implementation detail:** Approval messages expire after 4 hours. If you do not tap by then, the signal is deferred to the next day's pipeline run.

---

### Step 3.2 — Build Execution Engine

**File:** `backend/control/execution_engine.py`

Every order goes through this sequence before Kite placement:
1. Kill switch check
2. Risk manager pre-trade checks (see Step 3.3)
3. Kite API order placement
4. Supabase signal_log update with order ID
5. Telegram confirmation message

---

### Step 3.3 — Configure Risk Manager Guardrails

**Edit these values in Supabase to match your risk tolerance before activating Phase 3:**

```sql
-- These are your hard guardrails for Phase 3 execution
UPDATE system_config SET value = '8'    WHERE key = 'max_positions_risk_on';
UPDATE system_config SET value = '7'    WHERE key = 'max_positions_neutral';
UPDATE system_config SET value = '6'    WHERE key = 'max_positions_risk_off';
UPDATE system_config SET value = '25'   WHERE key = 'max_sector_concentration';
UPDATE system_config SET value = 'false' WHERE key = 'block_buys_risk_off';
```

**The risk manager checks before EVERY order:**
- Kill switch not active
- Max positions for current regime not exceeded
- Sector concentration will not exceed max after this trade
- Symbol not on ASM/GSM list
- Sufficient capital available

If any check fails, the order is blocked and you get a Telegram message explaining why.

---

### Step 3.4 — Shadow Mode Testing (2 Weeks Minimum)

Before live execution, run in shadow mode. Approval messages look identical to live, but tapping APPROVE logs to `shadow_trades` instead of placing a Kite order.

```sql
-- Enable shadow mode
INSERT INTO system_config (key, value) VALUES ('execution_mode', 'shadow')
ON CONFLICT (key) DO UPDATE SET value = 'shadow';
```

After 2 weeks, review shadow performance:
```sql
SELECT symbol, approved_at, shadow_entry_price,
       current_price,
       (current_price - shadow_entry_price) / shadow_entry_price * 100 as shadow_pnl_pct
FROM shadow_trades
WHERE status = 'APPROVED'
ORDER BY approved_at;
```

Only proceed to live if shadow results make sense.

---

### Step 3.5 — Activate Phase 3

```sql
UPDATE system_config SET value = 'live' WHERE key = 'execution_mode';
UPDATE system_config SET value = '3'    WHERE key = 'autonomy_phase';
```

**From this point:**
- You stop placing BUY trades manually in Zerodha app
- All buys come from tapping APPROVE in Telegram
- You still exit positions manually (Phase 3 does not auto-exit)
- You must do the 8:30 AM Kite token refresh every trading day

---

## 5. Phase 4 — Full Autonomy

**Goal:** System executes buys and exits without per-trade approval. Self-evolves strategy parameters via a proposal → approval workflow.

**Timeline:** Month 7+.

**Activation criteria — ALL required. No exceptions:**

| Criterion | Target |
|-----------|--------|
| Days in Phase 3 | ≥ 90 consecutive trading days |
| Win rate (Phase 3 period) | ≥ 55% |
| Max drawdown (Phase 3 period) | ≤ 8% |
| Kill switch tested | Must have been tested at least once |
| Explicit SQL activation | You run the SQL below manually |

```sql
-- The ONLY way to activate Phase 4. Never happens automatically.
UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase';
```

---

### Step 4.1 — Evolution Tracker

**File:** `backend/history/evolution_tracker.py`

Runs every Sunday via `.github/workflows/evolution_weekly.yml`.

What it analyzes:
- CTL/SBS/TPO threshold effectiveness over last 90 days
- Which signal inputs actually predicted wins vs losses
- Whether industry scoring weights need adjustment

What it can propose (writes to `evolution_proposals` table):
- Strategy threshold changes (e.g., "raise CTL min monthly RSI from 58 to 62")
- Industry scoring weight adjustments
- Score bonus changes

**What it can NEVER do automatically:**
- Change position sizing formula
- Modify risk guardrail thresholds
- Add new data sources
- Change any code file

**Reviewing proposals:**
```sql
-- View pending proposals
SELECT id, proposal_type, current_value, proposed_value,
       evidence_summary, backtested_improvement
FROM evolution_proposals
WHERE status = 'PENDING'
ORDER BY created_at DESC;

-- Approve
UPDATE evolution_proposals SET status = 'APPROVED', approved_at = NOW() WHERE id = 42;

-- Reject
UPDATE evolution_proposals SET status = 'REJECTED' WHERE id = 43;
```

---

### Step 4.2 — Discovery Engine

**File:** `backend/history/discovery_engine.py`

Runs alongside evolution_tracker every Sunday. Mines `chartink_raw_data` history (which has been accumulating since Phase 0) to find hidden signal dimensions.

Each discovery goes to `discovery_proposals` table. Same approval workflow as evolution proposals.

**Example discoveries after 6+ months of data:**
- "3-day rising delivery% predicts breakout 71% of the time vs 54% baseline"
- "CTL Banking stocks outperform when 5d FII flow > +₹500 Cr (8.4% avg advantage)"
- "EARLY_TREND day 3–7 entries beat day 1 entries by 6.2% avg"

You review each proposal. It only enters the scoring model after your approval.

---

## 6. Frontend Evolution — What the Dashboard Gains Each Phase

The frontend (`frontend/App_v6.jsx`) is a single React file. It reads from Supabase directly via the anon key. Each phase adds new data to Supabase that the frontend should surface. The frontend does **not** need to change for the system to work — but without updates, users won't see new capabilities like AI conviction, execution status, or evolution proposals.

**By Phase 4, the frontend is the sole interface.** No Sheet, no external tool, no SQL queries for daily use. Everything visible and operable from the 7-tab dashboard.

---

### Phase 1 Frontend Changes

**Signals tab — signal cards gain context columns:**
- `ai_conviction` badge: `HIGH` (green) / `MED` (amber) / `LOW` (red) next to score
- `fii_flag` label: `CAUTION ⚠` / `ACCELERATOR ↑` / `NEUTRAL` as a secondary line
- Industry rank and state already display if `industry_strength` data is present — no change needed here since the data was always being stored. The score number just gets larger once bonuses activate.

**Intelligence tab — unlocks from locked state:**
```jsx
// Currently shows "Set autonomy_phase = 1 in Settings"
// Phase 1: replace placeholder with live data
<KPI label="AI Provider"  value={d.config?.ai_provider || "ml"} />
<KPI label="Stocks analysed today" value={aiQueue.length} />
<KPI label="Daily AI spend" value={`₹${aiSpend}`} />
// List: each BUY_CANDIDATE row with ai_conviction, ai_risks, ai_catalyst
```

**Settings tab — AI Configuration section becomes functional:**
- AI provider selector (currently cosmetic) now saves to Supabase and takes effect on next pipeline run
- Daily budget and max stocks inputs become meaningful
- Add a "FII Status" readout showing today's fii_flag and 5d rolling net

**What to change in `App_v6.jsx`:**
1. Add `fii_dii_flow` to the Supabase queries in the `load()` function
2. In `SignalCard` component, add `ai_conviction` and `fii_flag` display rows
3. In `PanelIntelligence`, replace the phase-locked placeholder with the conviction queue grid
4. In `PanelSettings`, confirm the AI provider `<select>` calls `saveCfg('ai_provider', v)` — it likely already does

---

### Phase 2 Frontend Changes

**New: Data Quality indicator in sidebar footer:**
```jsx
// Add to sidebar status footer alongside PHASE / REGIME / MODE
["QUALITY", anomalyCount > 0 ? `${anomalyCount} flagged` : "Clean", anomalyCount > 0 ? C.yellow : C.green]
```
Query: `supabase.from('data_anomalies').select('id').eq('date', today).eq('resolved', false)`

**Positions tab — Kite price column:**
Once Kite is live, `open_positions` rows will have a `kite_price` column populated by the evening pipeline. Show this alongside `current_price` with a diff indicator if they diverge.

**Settings tab — Data Connection section gains:**
- Kite connection status (reads `kite_access_token_expiry` from `system_config`)
- Warning if token is stale (not refreshed today): `⚠ Kite token expires at 3:30 AM — refresh by 8:30 AM`

**What to change in `App_v6.jsx`:**
1. Add `data_anomalies` count query to `load()` 
2. Add quality pill to sidebar footer
3. Add `kite_price` column to the open positions table in `PanelPositions`
4. Add Kite status row to the Settings Data Connection card

---

### Phase 3 Frontend Changes

**New: Execution Status in Positions tab:**
Show each open position's Kite order ID, execution price, and fill timestamp. Currently positions come from Sheet via ingest — Phase 3 they come directly from Kite sync.

**Analytics tab — Shadow Trade Comparison:**
During the 2-week shadow period, add a "Shadow Performance" sub-section showing shadow_trades vs what actually happened. After going live, this becomes execution accuracy (approved price vs fill price).

**Signals tab — Approval Status:**
Each BUY_CANDIDATE signal card shows its Telegram approval state:
- `PENDING` (grey) — alert sent, awaiting tap
- `APPROVED` (green) — tapped approve, order sent
- `REJECTED` (red) — tapped reject  
- `EXPIRED` (dim) — 4-hour window passed without action

```jsx
// Add to SignalCard
const approvalState = signalApprovals[item.symbol] || "PENDING";
const approvalColors = { APPROVED: C.green, REJECTED: C.red, EXPIRED: C.textDim, PENDING: C.yellow };
<Pill color={approvalColors[approvalState]}>{approvalState}</Pill>
```

**What to change in `App_v6.jsx`:**
1. Add `signal_approvals` table query to `load()` (new table, populated by `telegram_bot.py`)
2. Add approval status display to `SignalCard`
3. Add shadow/execution sub-tab to `PanelAnalytics`
4. Add Kite order details column to open positions table

---

### Phase 4 Frontend Changes

**Settings tab — Evolution Proposals section (new, critical):**
This is where you review and approve/reject parameter change proposals from `evolution_tracker.py`. Must be usable from the frontend — no SQL needed.

```jsx
const PanelEvolutionProposals = ({ proposals }) => (
  proposals.map(p => (
    <Card key={p.id}>
      <div>{p.proposal_type}: {p.current_value} → {p.proposed_value}</div>
      <div style={{color: C.textDim}}>{p.evidence_summary}</div>
      <div>Backtested improvement: {p.backtested_improvement}</div>
      <div style={{display:'flex', gap:8}}>
        <button onClick={() => approveProposal(p.id)} style={{color: C.green}}>✓ Approve</button>
        <button onClick={() => rejectProposal(p.id)} style={{color: C.red}}>✗ Reject</button>
      </div>
    </Card>
  ))
);
```

**Settings tab — Discovery Proposals section:**
Same pattern as evolution proposals. Each discovery shows: proposed signal dimension, backtested accuracy vs baseline, evidence count, estimated score impact.

**Intelligence tab — Full unlock:**
- Strategy evolution history chart (score parameter changes over time with performance correlation)
- ML model accuracy trend over time
- Feature importance rankings from last ML training run
- Discovery engine status: "Last run Sunday, 3 candidates proposed, 1 approved"

**Dashboard tab — System Health widget:**
Replace or augment the existing regime/signal summary with a pipeline health row:
- Last pipeline run: `Today 18:47 IST ✓`  
- All 8 steps: green checkmarks or red X with step name
- Data freshness: chartink rows today, AI enrichments today, FII last updated

**What to change in `App_v6.jsx`:**
1. Add `evolution_proposals` and `discovery_proposals` queries to `load()`
2. Add `PanelEvolutionProposals` component to Settings panel
3. Add `PanelDiscoveryProposals` component to Settings panel
4. Add `approveProposal()` and `rejectProposal()` functions that call `saveCfg()` / update the proposals table
5. Expand `PanelIntelligence` with ML accuracy history chart and evolution timeline
6. Add pipeline health row to `PanelDashboard`

---

### Frontend Versioning Strategy

The dashboard file is a single HTML/JSX file. As phases progress:

```
frontend/
├── App_v6.jsx          ← current Phase 0 version
├── App_v6_p1.jsx       ← after Phase 1 changes (keep old as backup)
├── App_v6_p2.jsx       ← after Phase 2 changes
└── ...                 ← or just tag git commits per phase milestone
```

**Recommendation:** Use git tags rather than separate files. When a phase goes live:
```bash
git tag -a phase-1-live -m "Phase 1 live: AI conviction, FII context, industry scoring active"
git push origin phase-1-live
```

If a frontend change breaks something, you can roll back to the tagged version in one command.

---

## 7. Daily Operating Procedure

### Phase 0–1 (Now → Month 2)

| Time | Task | Who |
|------|------|-----|
| ~8:05 AM | Morning Telegram brief (Phase 1+) | Auto |
| During market | No action needed | — |
| After 3:30 PM | Update positions in Sheet if trades made | You |
| 6:00 PM | Full pipeline runs automatically | Auto |
| ~6:45 PM | Evening Telegram alert | Auto |
| Evening | Review signals. Decide on next day's trades. | You |

### Phase 2 (Month 3–4)

Same as above. Additionally:
- Stop updating price/formula columns in Sheet (Python computes them now)
- 8:30 AM Kite token refresh if using Kite for live prices

### Phase 3 (Month 5–6)

| Time | Task | Who |
|------|------|-----|
| 8:30 AM | Kite token refresh | You |
| ~8:05 AM | Morning brief | Auto |
| ~6:45 PM | Telegram alerts with APPROVE/REJECT buttons | Auto |
| Evening | Tap APPROVE or REJECT for each BUY signal | You |
| As needed | Tap EXIT when system recommends | You |

### Phase 4 (Month 7+)

| Time | Task | Who |
|------|------|-----|
| 8:30 AM | Kite token refresh | You |
| Weekly (Sunday) | Review evolution + discovery proposals | You |
| Monthly | Review system performance Telegram digest | Auto |

---

## 8. Emergency Procedures

### Activate Kill Switch

**Use any of these three methods — all work immediately:**

```bash
# CLI
python backend/control/kill_switch.py

# SQL (from phone, tablet, any device with Supabase access)
UPDATE system_config SET value = 'true' WHERE key = 'master_kill_switch';

# Frontend Settings tab → Kill Switch button (red button at bottom)
```

Effect: All pipeline steps check kill switch at startup and exit without processing. No signals generated, no alerts sent, no trades placed.

**Deactivate when safe:**
```bash
python backend/control/kill_switch.py off
# OR
UPDATE system_config SET value = 'false' WHERE key = 'master_kill_switch';
```

### Pipeline Did Not Run

Check GitHub Actions → your repo → Actions tab → find the 6 PM workflow → expand failed step.

**Quick diagnosis:**

| Error message | Cause | Fix |
|---------------|-------|-----|
| `Playwright: No usable sandbox` | Missing browser | Add `playwright install chromium --with-deps` to workflow |
| `Could not find column 'industry_top5'` | Missing schema migration | Run ALTER TABLE in Supabase |
| `Sheets API: 403 Forbidden` | Service account lost Editor access | Re-share Sheet with service account as Editor |
| `chartink_raw_data: 0 rows inserted` | UNIQUE constraint violation | Check if today's data already exists, run `--force` flag |
| `SUPABASE_URL: KeyError` | Secret missing from GitHub | Add SUPABASE_URL and SUPABASE_SERVICE_KEY in repo Settings → Secrets |
| `kite.exceptions.TokenException` | Token expired | Run `kite_token_refresh.py` manually |
| `ML model not found` | Model not yet trained | Run `python ai/providers/ml_provider.py --train` manually |

### Manual Pipeline Trigger

If the automated run fails:

```bash
# Run from your machine after fixing the error
cd backend
python run_pipeline.py
```

Or trigger from GitHub Actions UI (Actions → pipeline_daily → Run workflow).

### Data Recovery After Bad Pipeline Run

```sql
-- Check what was written (look for wrong dates or corrupted values)
SELECT COUNT(*), MIN(close), MAX(close), MIN(rsi_daily), MAX(rsi_daily)
FROM stock_data_daily WHERE date = CURRENT_DATE;

-- If data is bad, delete and re-run
DELETE FROM stock_data_daily   WHERE date = CURRENT_DATE;
DELETE FROM chartink_raw_data  WHERE date = CURRENT_DATE;
DELETE FROM signal_log         WHERE date = CURRENT_DATE;

-- Then re-run pipeline
python backend/run_pipeline.py
```

---

## 9. Troubleshooting Reference

### Diagnostic SQL Queries

```sql
-- What did the pipeline write today? (run this first for any issue)
SELECT 'signal_log'        as tbl, COUNT(*) FROM signal_log        WHERE date = CURRENT_DATE
UNION ALL
SELECT 'chartink_raw_data' as tbl, COUNT(*) FROM chartink_raw_data WHERE date = CURRENT_DATE
UNION ALL
SELECT 'stock_data_daily'  as tbl, COUNT(*) FROM stock_data_daily  WHERE date = CURRENT_DATE
UNION ALL
SELECT 'fii_dii_flow'      as tbl, COUNT(*) FROM fii_dii_flow      WHERE date = CURRENT_DATE
UNION ALL
SELECT 'industry_strength' as tbl, COUNT(*) FROM industry_strength WHERE date = CURRENT_DATE;

-- Today's top buy candidates with full context
SELECT m.symbol, m.final_score, m.industry, m.industry_rank,
       m.industry_top5, m.ai_conviction, f.fii_flag, m.position_state
FROM master_shortlist m
LEFT JOIN fii_dii_flow f ON f.date = m.date
WHERE m.date = CURRENT_DATE AND m.position_state = 'BUY_CANDIDATE'
ORDER BY m.final_score DESC;

-- Pending evolution proposals
SELECT id, proposal_type, current_value, proposed_value,
       evidence_summary, created_at
FROM evolution_proposals WHERE status = 'PENDING'
ORDER BY created_at DESC;

-- System config snapshot (what is the system set to right now?)
SELECT key, value FROM system_config ORDER BY key;
```

### Common Symptom → Resolution

| Symptom | Check | Resolution |
|---------|-------|------------|
| No signals generated | `signal_log` has today's date? | `master_shortlist` must exist for today — check ingest step |
| Signals missing industry data | `industry_strength` has today? | Check ingest log shows INDUSTRY_STRENGTH tab read |
| AI conviction all NULL | `ai_provider` in system_config? | API key in GitHub secrets? |
| FII flag always NEUTRAL | `fii_dii_flow` has today? | Check ingest_fii_dii.py ran successfully |
| No Telegram messages | `telegram_alerts_enabled = true`? | Test bot token with API URL check |
| frontend shows Demo Mode | SUPABASE_URL in HTML? | Check Supabase anon RLS allows SELECT |
| Scores jumped unexpectedly | Industry scoring recently enabled? | Query signal_log to see if bonuses applied |
| compute_indicators slow | Missing index? | `CREATE INDEX IF NOT EXISTS idx_chartink_date_symbol ON chartink_raw_data (date, symbol);` |
| Kite prices stale | Token refreshed today? | Run kite_token_refresh.py and check saved token in system_config |

---

*TradeOS v6 · Phase 0 complete · 54 closed trades · ML model ready to train*
*Google Sheet ID: 1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw*
*chartink_raw_data accumulating daily since Phase 0 activation*

---

## 10. Full Repository Structure & Script Reference

Use this section when troubleshooting with Claude — paste the relevant file path and describe what's failing.

```
tradeos-v6/
├── .gitignore
├── .github/
│   └── workflows/
│       ├── pipeline_daily.yml          ← Main 6 PM pipeline (Mon–Fri)
│       ├── pipeline_morning.yml        ← 8 AM global cues (Phase 1+)
│       └── evolution_weekly.yml        ← Sunday ML training (Phase 1+)
│
├── frontend/
│   └── App_v6.jsx                      ← Single-file React dashboard (7 tabs)
│
└── backend/
    ├── run_pipeline.py                 ← Master orchestrator — runs all steps in order
    ├── config.py                       ← Supabase client, env loading, all shared config
    ├── requirements.txt                ← pip dependencies (includes playwright)
    ├── .env.example                    ← Template — copy to .env and fill in
    │
    ├── db/
    │   ├── schema_v6_base.sql          ← Run FIRST in Supabase — core tables
    │   ├── schema_v6_signals.sql       ← Run SECOND — signal_log, strategy_config
    │   └── schema_rls.sql              ← Run LAST — row-level security policies
    │
    ├── ingestion/
    │   ├── fetch_chartink.py           ← [Phase 0] Playwright → Chartink CSV → Sheet + Supabase
    │   ├── ingest_sheets.py            ← [Phase 0] Reads 15 Sheet tabs → Supabase
    │   ├── ingest_bhavcopy.py          ← [Phase 0] NSE bhavcopy → delivery%, delivery_qty, value_cr
    │   ├── ingest_fii_dii.py           ← [Phase 1] NSE FII/DII equity flows → fii_dii_flow
    │   ├── ingest_nse_events.py        ← [Phase 1] NSE corporate events → event_calendar
    │   ├── ingest_global_cues.py       ← [Phase 1] Gift Nifty, USD/INR, crude → global_cues
    │   └── ingest_asm_gsm.py           ← [Phase 2] NSE ASM/GSM/FO_BAN → safety_lists
    │
    ├── signals/
    │   ├── generate_signals.py         ← [Phase 0] CTL+SBS+TPO+EAP rule engine → signal_log
    │   └── independent_scanner.py      ← [Phase 1] Pattern scanner (VOLUME_BREAKOUT, RS_LEADER, etc.)
    │
    ├── compute/                        ← [Phase 2] — entire folder built in Phase 2
    │   ├── compute_indicators.py       ← Replaces ALL Sheet formula columns (build in Step 2.2)
    │   └── data_quality_monitor.py     ← Validates every column daily, auto-corrects (build in Step 2.6)
    │
    ├── ai/
    │   ├── ai_router.py                ← [Phase 1] Routes to configured provider, tracks cost
    │   ├── claude_enrich.py            ← [Phase 1] Adds AI conviction to master_shortlist rows
    │   ├── post_trade_analysis.py      ← [Phase 1] AI retrospective on closed trades → lessons
    │   ├── generate_shortlist.py       ← [Phase 1] AI shortlist review and ranking
    │   ├── providers/
    │   │   ├── base_provider.py        ← Abstract base class for all AI providers
    │   │   ├── claude_provider.py      ← Anthropic Claude API
    │   │   ├── openai_provider.py      ← OpenAI GPT API
    │   │   ├── gemini_provider.py      ← Google Gemini API
    │   │   ├── deepseek_provider.py    ← DeepSeek API (~10x cheaper than Claude)
    │   │   ├── grok_provider.py        ← xAI Grok API
    │   │   ├── copilot_provider.py     ← Azure OpenAI / Copilot API
    │   │   └── ml_provider.py          ← Local ML model (free, trains from your trade history)
    │   └── fallback/
    │       ├── web_scraper.py          ← Free NSE/Moneycontrol/BSE news scraping
    │       ├── sentiment_scorer.py     ← Scores scraped text for bullish/bearish sentiment
    │       └── news_aggregator.py      ← Aggregates multiple free news sources
    │
    ├── alerts/
    │   └── send_alerts.py              ← [Phase 1] Telegram daily digest (evening + morning)
    │
    ├── control/
    │   ├── kill_switch.py              ← [Phase 0] Halt entire system — CLI, SQL, or frontend
    │   ├── execution_engine.py         ← [Phase 3] Kite order placement after Telegram approval
    │   ├── telegram_bot.py             ← [Phase 3] Approval bot with APPROVE/REJECT/DEFER buttons
    │   ├── risk_manager.py             ← [Phase 3] Pre-trade guardrail checks
    │   └── shadow_trade_logger.py      ← [Phase 3] Simulated trades for testing before going live
    │
    ├── history/
    │   ├── append_history.py           ← [Phase 0] Daily MSL snapshot → msl_history table
    │   ├── evolution_tracker.py        ← [Phase 4] Weekly strategy analysis → evolution_proposals
    │   └── discovery_engine.py         ← [Phase 4] Hidden signal mining → discovery_proposals
    │
    ├── kite/
    │   ├── kite_client.py              ← [Phase 2] Zerodha live prices, historical OHLCV
    │   └── kite_token_refresh.py       ← [Phase 2] Daily manual login → saves access_token
    │
    └── scripts/
        └── backfill_msl_history.py     ← [Phase 0, one-time] Load 708 rows of MSL history
```

---

### Script-by-Script Reference

Every script listed below. For each: what it does, what inputs it needs, what it writes, and how to run it manually.

---

#### `backend/run_pipeline.py`
**What it does:** Master orchestrator. Runs all pipeline steps in the correct order. Checks kill switch before each step. Phase-aware — automatically adds steps as `autonomy_phase` increases in `system_config`.

**Inputs:** Nothing directly — it calls all other scripts as subprocesses.

**Writes:** Nothing directly — each step writes its own tables.

**Run manually:**
```bash
cd backend
python run_pipeline.py             # full run
python run_pipeline.py --dry-run   # test without writing
python run_pipeline.py --step signals   # single step only
```

**Troubleshoot with Claude:** Paste the full terminal output. The log format is `=== STEP X: NAME ===` followed by each script's output.

---

#### `backend/config.py`
**What it does:** Loads all environment variables, creates the Supabase client, exposes `get_supabase()` used by every other script.

**Inputs:** `.env` file in `backend/` (or GitHub Actions secrets when running in CI).

**Writes:** Nothing.

**Troubleshoot with Claude:** If you see `SUPABASE_URL not set` or `KeyError: 'SUPABASE_SERVICE_KEY'`, the `.env` file is missing or not being loaded. Paste the error + the first 10 lines of `config.py`.

---

#### `backend/ingestion/fetch_chartink.py`
**What it does:** Playwright browser automation. Navigates to Chartink Atlas, hovers over CSV download button, downloads Nifty 500 data. Writes to Google Sheet tab "Chartink Raw Data_Nifty 500" and upserts to Supabase `chartink_raw_data` table.

**Inputs:** `CHARTINK_EMAIL`, `CHARTINK_PASSWORD` from `.env`. Google Sheet ID from `.env`. Supabase credentials.

**Writes:** `chartink_raw_data` (500 rows, UNIQUE constraint on `date + symbol`). Google Sheet tab "Chartink Raw Data_Nifty 500".

**Run manually:**
```bash
python ingestion/fetch_chartink.py
python ingestion/fetch_chartink.py --headless false   # shows browser window for debugging
```

**Debug file:** Saves `chartink_hover_debug.png` on failure — shows where the browser was when it failed.

**Troubleshoot with Claude:** Paste error + attach `chartink_hover_debug.png` if it exists.

---

#### `backend/ingestion/ingest_sheets.py`
**What it does:** Reads 15 Google Sheet tabs using the Google Sheets API. Syncs all data to Supabase. Row offsets are hardcoded to your exact Sheet layout.

**Tabs it reads (in order):**
1. `STOCK_DATA` — 500 rows, 78 columns → `stock_data_daily`
2. `MASTER_SHORTLIST` — 35 rows → `master_shortlist`
3. `OPEN_POSITIONS` — up to 8 rows → `open_positions` (authoritative — deletes removed symbols)
4. `CLOSED_POSITIONS` → `closed_positions`
5. `SECTOR_STRENGTH` → `sector_strength`
6. `INDUSTRY_STRENGTH` — rows 2-3, cols A-H → `industry_strength`
7. `MARKET_REGIME` → `market_regime`
8. `EVENT_CALENDAR` → `event_calendar`
9. `LESSONS` → `lessons`
10. `MSL_HISTORY` → `msl_history`
11. `NSE_HOLIDAYS` → `nse_holidays`
12. `NIFTY_BHAVCOPY` → `stock_data_daily` (delivery cols)
13. `NIFTY_UPCOMING_EVENTS` → `event_calendar`
14. `NIFTY_TOTAL_MARKET` → `nifty_total_market`
15. `Chartink Raw Data_Nifty 500` — freshly written by fetch_chartink.py
16. `STRATEGY_CONTROLS` → `strategy_config`
17. `SYSTEM_PARAMETERS` → `system_config`

**Inputs:** `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_JSON` path from `.env`. Supabase credentials.

**Writes:** Multiple Supabase tables (see above).

**Run manually:**
```bash
python ingestion/ingest_sheets.py
python ingestion/ingest_sheets.py --tab MASTER_SHORTLIST   # single tab
```

**Troubleshoot with Claude:** The script logs each tab as it reads: `Reading STOCK_DATA... 500 rows`. If a tab fails, paste the full error including the tab name.

---

#### `backend/ingestion/ingest_bhavcopy.py`
**What it does:** Downloads NSE end-of-day bhavcopy CSV. Extracts `delivery_pct`, `delivery_qty`, `value_cr`, `prev_close` for all NSE stocks. Joins with `chartink_raw_data` on `symbol + date` and upserts delivery columns to `stock_data_daily`.

**NOTE:** This script already exists in your outputs from a previous session. It needs to be added to `run_pipeline.py` as Step 0b (see Step 1.1 in Phase 1).

**Inputs:** No API key needed — fetches from NSE public URL. Supabase credentials.

**Writes:** `stock_data_daily` — updates `delivery_pct`, `delivery_qty`, `value_cr`, `prev_close` columns.

**Run manually:**
```bash
python ingestion/ingest_bhavcopy.py
python ingestion/ingest_bhavcopy.py --date 2026-01-15   # specific date
python ingestion/ingest_bhavcopy.py --dry-run
```

---

#### `backend/ingestion/ingest_fii_dii.py`
**What it does:** Fetches FII/DII daily equity flow data from NSE. Computes rolling 5d/10d/20d cumulative flows. Sets `fii_flag`: `CAUTION` if 5d net sell < -₹2000 Cr, `ACCELERATOR` if 5d net buy > +₹1000 Cr, `NEUTRAL` otherwise.

**Inputs:** No API key — NSE public data. Supabase credentials.

**Writes:** `fii_dii_flow` table.

**Run manually:**
```bash
python ingestion/ingest_fii_dii.py
python ingestion/ingest_fii_dii.py --dry-run
```

---

#### `backend/ingestion/ingest_nse_events.py`
**What it does:** Fetches NSE corporate actions calendar (results, dividends, AGMs, board meetings). Upserts to `event_calendar`. Computes EAP timing: flags stocks `PRIORITISE` 2 days before event, `AVOID_ENTRY` 2 days after.

**Inputs:** No API key — NSE public calendar. Supabase credentials.

**Writes:** `event_calendar` table.

**Run manually:**
```bash
python ingestion/ingest_nse_events.py
python ingestion/ingest_nse_events.py --dry-run
```

---

#### `backend/ingestion/ingest_global_cues.py`
**What it does:** Morning script (8 AM). Fetches Gift Nifty gap signal, USD/INR, Brent crude, Gold, US market close. Maps each to affected sectors. Sends Telegram morning brief if enabled.

**Inputs:** No API key (public data). Supabase + Telegram credentials.

**Writes:** `global_cues` table.

**Run manually:**
```bash
python ingestion/ingest_global_cues.py
```

---

#### `backend/ingestion/ingest_asm_gsm.py`
**What it does:** Fetches ASM Stage 1/2, GSM Stage 1-6, and FO_BAN lists from NSE. Populates `safety_lists` table. `generate_signals.py` checks this before flagging any stock as BUY_CANDIDATE.

**Inputs:** No API key. Supabase credentials.

**Writes:** `safety_lists` table.

**Run manually:**
```bash
python ingestion/ingest_asm_gsm.py
```

---

#### `backend/signals/generate_signals.py`
**What it does:** The core rule engine. Loads `master_shortlist`, `market_regime`, `industry_strength`, `fii_dii_flow`, `safety_lists`, `event_calendar` from Supabase. Applies CTL, SBS, TPO, EAP rules. Scores each stock. Determines `position_state` (WATCHING / BUY_CANDIDATE / OPEN_POSITION). Writes to `signal_log`.

**Phase 0:** Runs rules, stores industry data but does not score with it.
**Phase 1+:** Industry scoring bonuses active (+10 top5, +5 STRONG). AI conviction integrated. FII flag in context.

**Inputs:** Multiple Supabase tables (reads only). `system_config` for phase + parameters.

**Writes:** `signal_log`.

**Run manually:**
```bash
python signals/generate_signals.py
python signals/generate_signals.py --dry-run   # prints signals, no write
python signals/generate_signals.py --date 2026-01-15   # specific date
```

**Troubleshoot with Claude:** Paste the dry-run output. If scores look wrong, check which columns have NULL values — that usually means an ingestion step upstream failed.

---

#### `backend/signals/independent_scanner.py`
**What it does:** Pattern scanner that runs in parallel with the rule engine. Detects VOLUME_BREAKOUT, RS_LEADER, POST_CONSOL, MEAN_REVERSION, DELIVERY_SURGE. Cross-references with `master_shortlist` — stocks appearing in both the rule engine and the scanner get a score bonus.

**Inputs:** `stock_data_daily`, `master_shortlist` from Supabase.

**Writes:** `scanner_signals` table.

**Run manually:**
```bash
python signals/independent_scanner.py
```

---

#### `backend/compute/compute_indicators.py`
**What it does:** (Phase 2 — built in Step 2.2). Replaces all Google Sheet formula columns. Reads `chartink_raw_data` + bhavcopy columns from `stock_data_daily`. Computes vol_ratio, returns, distances, breakout signals, RS vs Nifty. Upserts computed columns back to `stock_data_daily`.

**Inputs:** `chartink_raw_data`, `stock_data_daily` (bhavcopy cols), `nifty_total_market`.

**Writes:** `stock_data_daily` (computed columns only — never overwrites raw Chartink cols).

**Run manually:**
```bash
python compute/compute_indicators.py
python compute/compute_indicators.py --date 2026-01-15
python compute/compute_indicators.py --backfill --from 2025-01-01
```

---

#### `backend/compute/data_quality_monitor.py`
**What it does:** (Phase 2 — built in Step 2.6). Runs after every pipeline execution. Validates RSI ranges, row counts, delivery% bounds, score ranges. Auto-corrects what it can (caps outliers). Alerts via Telegram on failures. Logs all anomalies to `data_anomalies` table.

**Inputs:** All major Supabase tables.

**Writes:** `data_anomalies` table. Telegram alerts on failures.

**Run manually:**
```bash
python compute/data_quality_monitor.py
python compute/data_quality_monitor.py --date 2026-01-15
```

---

#### `backend/ai/ai_router.py`
**What it does:** Routes AI enrichment requests to the configured provider. Reads `ai_provider` from `system_config`. Tracks daily cost against `ai_daily_budget_inr`. Falls back to ML model or web scraper if budget exceeded or provider fails. Logs each enrichment to `ai_model_performance`.

**Providers:** `claude` | `openai` | `gemini` | `deepseek` | `grok` | `copilot` | `ml` | `disabled`

**Inputs:** `master_shortlist` (BUY_CANDIDATE rows). Provider API keys from `.env`. `system_config` for budget + provider.

**Writes:** Updates `master_shortlist` with `ai_conviction`, `ai_risks`, `ai_catalyst`, `ai_suggested_action`. Logs to `ai_context` and `ai_model_performance`.

**Run manually:**
```bash
python ai/ai_router.py
python ai/ai_router.py --symbol SBIN   # enrich single stock
python ai/ai_router.py --dry-run       # print what AI would say, no write
```

---

#### `backend/ai/providers/ml_provider.py`
**What it does:** Local ML model (scikit-learn RandomForest). Trains on your `closed_positions` history. Runs as an AI provider — gives `HIGH/MEDIUM/LOW` conviction with feature importance explanations. Free, no API key.

**Inputs on train:** `closed_positions` + linked `signal_log` rows (needs 30+ trades).

**Inputs on predict:** Current `stock_data_daily` row for the stock being evaluated.

**Writes on train:** Saves `models/ml_model.pkl` and logs to `ml_training_log`.

**Run manually:**
```bash
python ai/providers/ml_provider.py --train           # train from scratch
python ai/providers/ml_provider.py --train --force   # retrain even if model is recent
python ai/providers/ml_provider.py --evaluate        # print accuracy on test set
```

---

#### `backend/ai/post_trade_analysis.py`
**What it does:** Triggered when a position closes. Sends the full trade context (entry signal, hold period, exit reason, P&L) to the AI provider. AI returns a structured lesson: scenario_type, root_cause, corrective_rule. Stored in `lessons` table and used as context for future signals of the same type.

**Inputs:** `closed_positions` row + linked `signal_log` row at entry time.

**Writes:** `lessons` table.

**Run manually:**
```bash
python ai/post_trade_analysis.py --symbol RBLBANK   # analyze specific closed trade
python ai/post_trade_analysis.py --all-recent       # analyze all trades closed in last 7 days
```

---

#### `backend/alerts/send_alerts.py`
**What it does:** Sends formatted Telegram messages. Called twice daily: morning brief (8 AM, via `ingest_global_cues.py`) and evening digest (6 PM, as last step in main pipeline). Phase 3+: adds APPROVE/REJECT inline keyboard buttons to BUY signals.

**Inputs:** `signal_log`, `master_shortlist`, `fii_dii_flow`, `market_regime` from Supabase. Telegram credentials.

**Writes:** Nothing to Supabase. Sends Telegram messages.

**Run manually:**
```bash
python alerts/send_alerts.py
python alerts/send_alerts.py --test     # sends test message only
python alerts/send_alerts.py --morning  # force morning brief format
```

---

#### `backend/control/kill_switch.py`
**What it does:** Activates or deactivates the master kill switch. When active, all pipeline steps exit immediately without processing.

**Run manually:**
```bash
python control/kill_switch.py         # activate
python control/kill_switch.py off     # deactivate
python control/kill_switch.py status  # check current state
```

---

#### `backend/control/risk_manager.py`
**What it does:** (Phase 3) Pre-trade guardrail checks. Called by `execution_engine.py` before every Kite order. Checks: kill switch, max positions for regime, sector concentration, ASM/GSM status, capital availability. Returns pass/fail with reason.

**Inputs:** `system_config`, `open_positions`, `market_regime`, `safety_lists` from Supabase. Current order details.

**Writes:** Nothing — returns a result object only.

**Troubleshoot with Claude:** If a trade was blocked, paste the `risk_manager.py` output showing which check failed.

---

#### `backend/control/execution_engine.py`
**What it does:** (Phase 3) Places Kite orders after Telegram approval. Handles the full flow: risk check → Kite API call → Supabase logging → Telegram confirmation.

**Inputs:** Approved signal details from `telegram_bot.py`. Kite access token from `system_config`.

**Writes:** Updates `signal_log` with `kite_order_id`, `execution_price`, `executed_at`.

---

#### `backend/control/shadow_trade_logger.py`
**What it does:** (Phase 3, pre-live) Logs "paper trades" to `shadow_trades` table. Identical approval flow to live execution, but no actual Kite order placed. Use for 2-week testing before going live.

**Enable shadow mode:**
```sql
INSERT INTO system_config (key, value) VALUES ('execution_mode', 'shadow')
ON CONFLICT (key) DO UPDATE SET value = 'shadow';
```

---

#### `backend/control/telegram_bot.py`
**What it does:** (Phase 3) Long-polling Telegram bot that listens for button taps. When you tap APPROVE on a signal, it calls `execution_engine.py`. When you tap REJECT or DEFER, it logs the decision. Must run as a persistent service (not just triggered at 6 PM).

**Deploy options:** Render.com free tier, Railway, any VPS, or locally if you're always online at signal time.

**Run:**
```bash
python control/telegram_bot.py   # starts long-polling, runs forever
```

---

#### `backend/history/append_history.py`
**What it does:** Saves a daily snapshot of the full `master_shortlist` to `msl_history`. Captures score, rank, lifecycle state, position_state for each stock every day. Used by ML training and evolution tracker to compute velocity metrics.

**Inputs:** Today's `master_shortlist` from Supabase.

**Writes:** `msl_history` (appends, never overwrites).

**Run manually:**
```bash
python history/append_history.py
python history/append_history.py --date 2026-01-15   # backfill specific date
```

---

#### `backend/history/evolution_tracker.py`
**What it does:** (Phase 4) Runs every Sunday. Analyzes 90 days of closed trade outcomes vs the signal conditions at entry. Identifies parameter drift in CTL/SBS/TPO thresholds. Writes improvement proposals to `evolution_proposals` table with evidence. Does NOT apply changes — waits for your approval.

**Run manually:**
```bash
python history/evolution_tracker.py
python history/evolution_tracker.py --dry-run   # print proposals, no write
```

---

#### `backend/history/discovery_engine.py`
**What it does:** (Phase 4) Runs every Sunday alongside evolution_tracker. Mines `chartink_raw_data` history for hidden signal patterns not in the current framework. Correlates data dimensions with trade outcomes. Writes candidate signals to `discovery_proposals` table with backtested evidence. Does NOT activate them — waits for your approval.

**Run manually:**
```bash
python history/discovery_engine.py
python history/discovery_engine.py --dry-run
```

---

#### `backend/kite/kite_token_refresh.py`
**What it does:** (Phase 2) Opens a browser window for Zerodha login. After login, captures the `request_token` from the redirect URL. Exchanges it for an `access_token` via Kite API. Saves `access_token` and expiry to `system_config` table in Supabase.

**Must be run manually every morning** (~8:30 AM) — Zerodha does not allow automated login. This is a Zerodha security requirement and cannot be automated.

**Run:**
```bash
python kite/kite_token_refresh.py
```

Expected output: `Access token saved. Expires: 2026-03-06 03:30:00 IST`

---

#### `backend/kite/kite_client.py`
**What it does:** (Phase 2) Zerodha Kite Connect API wrapper. Used by `generate_signals.py` (Phase 2+) to get live prices, and by `execution_engine.py` (Phase 3) to place orders. Reads `access_token` from `system_config`.

**Never run directly** — imported as a module by other scripts.

---

#### `backend/scripts/backfill_msl_history.py`
**What it does:** One-time script. Loads 708 rows of historical MSL snapshots from your Google Sheet's MSL_HISTORY tab into Supabase. Already run during Phase 0 setup. Only needed again if the `msl_history` table is accidentally dropped.

**Run (only if needed):**
```bash
python scripts/backfill_msl_history.py
```

---

### GitHub Actions Workflows

#### `.github/workflows/pipeline_daily.yml`
**Triggers:** 6:00 PM IST (12:30 UTC) Monday–Friday, plus manual dispatch.

**Steps in order:**
1. Install Python 3.11
2. `pip install -r backend/requirements.txt`
3. `playwright install chromium` ← **Critical — must be present or Playwright fails**
4. `python backend/ingestion/fetch_chartink.py`
5. `python backend/ingestion/ingest_bhavcopy.py` (after Step 1.1 wiring)
6. `python backend/run_pipeline.py`

**Secrets required:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_JSON`, `CHARTINK_EMAIL`, `CHARTINK_PASSWORD`

**Phase 1+ adds:** `ANTHROPIC_API_KEY` (or whichever AI provider), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**Phase 2+ adds:** `KITE_API_KEY`, `KITE_API_SECRET`

---

#### `.github/workflows/pipeline_morning.yml`
**Triggers:** 8:00 AM IST (2:30 UTC) Monday–Friday, plus manual dispatch.

**Steps:**
1. `python backend/ingestion/ingest_global_cues.py`

**Secrets required:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

---

#### `.github/workflows/evolution_weekly.yml`
**Triggers:** Every Sunday 6:00 AM IST (00:30 UTC), plus manual dispatch.

**Steps:**
1. `python backend/ai/providers/ml_provider.py --train`
2. `python backend/history/evolution_tracker.py` (Phase 4+)
3. `python backend/history/discovery_engine.py` (Phase 4+)

**Secrets required:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`

---

### Supabase Tables Reference

| Table | Written by | Read by | Phase |
|-------|------------|---------|-------|
| `chartink_raw_data` | fetch_chartink.py | compute_indicators, ingest_sheets | 0 |
| `stock_data_daily` | ingest_sheets, ingest_bhavcopy, compute_indicators | generate_signals, independent_scanner | 0 |
| `master_shortlist` | ingest_sheets, ai_router | generate_signals, send_alerts, frontend | 0 |
| `open_positions` | ingest_sheets (auth), execution_engine | generate_signals, risk_manager, frontend | 0 |
| `closed_positions` | ingest_sheets | ml_provider, post_trade_analysis, frontend | 0 |
| `sector_strength` | ingest_sheets | generate_signals, frontend | 0 |
| `industry_strength` | ingest_sheets | generate_signals, frontend | 0 |
| `market_regime` | ingest_sheets | generate_signals, risk_manager, frontend | 0 |
| `signal_log` | generate_signals | send_alerts, evolution_tracker, frontend | 0 |
| `msl_history` | append_history, backfill script | ml_provider, evolution_tracker | 0 |
| `event_calendar` | ingest_sheets, ingest_nse_events | generate_signals, frontend | 0 |
| `lessons` | post_trade_analysis | ai_router (context), frontend | 0 |
| `nse_holidays` | ingest_sheets | generate_signals (EAP timing) | 0 |
| `nifty_total_market` | ingest_sheets | compute_indicators (Nifty RS) | 0 |
| `system_config` | manual SQL, frontend Settings | every script | 0 |
| `strategy_config` | ingest_sheets, evolution_proposals | generate_signals | 0 |
| `fii_dii_flow` | ingest_fii_dii | generate_signals, send_alerts | 1 |
| `global_cues` | ingest_global_cues | send_alerts | 1 |
| `scanner_signals` | independent_scanner | send_alerts | 1 |
| `ai_context` | ai_router | send_alerts, frontend | 1 |
| `ai_model_performance` | ai_router | evolution_tracker | 1 |
| `ml_training_log` | ml_provider | frontend (Analytics) | 1 |
| `safety_lists` | ingest_asm_gsm | generate_signals, risk_manager | 1/2 |
| `data_anomalies` | data_quality_monitor | frontend (Settings) | 2 |
| `shadow_trades` | shadow_trade_logger | frontend (Analytics) | 3 |
| `evolution_proposals` | evolution_tracker | frontend (Settings), you via SQL | 4 |
| `discovery_proposals` | discovery_engine | frontend (Settings), you via SQL | 4 |

---

### Environment Variables Reference

**Required for Phase 0 (must be set before anything works):**

| Variable | Where to get it | Example |
|----------|-----------------|---------|
| `SUPABASE_URL` | Supabase project Settings → API | `https://xyzxyz.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase project Settings → API → service_role key | `eyJhbGci...` |
| `GOOGLE_SHEET_ID` | Your Sheet URL between `/d/` and `/edit` | `1yclJSWpRt...` |
| `GOOGLE_CREDENTIALS_JSON` | Path to service account JSON file | `credentials/service_account.json` |
| `CHARTINK_EMAIL` | Your Chartink login | `you@gmail.com` |
| `CHARTINK_PASSWORD` | Your Chartink password | `yourpass` |
| `TOTAL_CAPITAL` | Your total trading capital in ₹ | `200000` |

**Phase 1 additions:**

| Variable | Required for | Notes |
|----------|-------------|-------|
| `TELEGRAM_BOT_TOKEN` | Alerts + Phase 3 bot | From @BotFather |
| `TELEGRAM_CHAT_ID` | Alerts + Phase 3 bot | Your numeric chat ID |
| `ANTHROPIC_API_KEY` | If using Claude provider | console.anthropic.com |
| `OPENAI_API_KEY` | If using GPT provider | platform.openai.com |
| `GEMINI_API_KEY` | If using Gemini provider | aistudio.google.com |
| `DEEPSEEK_API_KEY` | If using DeepSeek provider | platform.deepseek.com |
| `GROK_API_KEY` | If using Grok provider | console.x.ai |
| `AZURE_OPENAI_API_KEY` | If using Copilot/Azure | Azure portal |

**Phase 2 additions:**

| Variable | Required for |
|----------|-------------|
| `KITE_API_KEY` | Kite Connect live prices + execution |
| `KITE_API_SECRET` | Kite Connect authentication |

---

*TradeOS v6 · Full repository: 73 files · Google Sheet ID: 1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw*
