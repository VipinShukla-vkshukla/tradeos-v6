# TradeOS v6 — Master Deployment Guide (Golden Copy)
**Version 4.5 · March 2026 · Supersedes all previous versions**

> **One-stop reference for everything: what was built, what needs patching, and every step to Phase 4.**
> Every action in this document moves you toward the **Target State** documented in `tradeos_connectivity_map.html`.

---

## Table of Contents

1. [System Status at a Glance](#1-system-status-at-a-glance)
2. [Architecture Overview](#2-architecture-overview)
   - [2.1 System Layers](#21-system-layers)
   - [2.2 Data Flow — Evening Pipeline](#22-data-flow--evening-pipeline-6-pm-the-main-run)
   - [2.3 Morning Sequence](#23-morning-sequence-no-duplicates)
   - [2.4 Supabase Database — Table Reference](#24-supabase-database--table-reference)
   - [2.5 Key Design Principles](#25-key-design-principles)
   - [2.6 Target State Diagram](#26-target-state-diagram-phase-4)
3. [Phase 0 — Foundation (COMPLETED)](#3-phase-0--foundation-completed)
4. [Phase 1 — Intelligence Layer (COMPLETED)](#4-phase-1--intelligence-layer-completed)
5. [⚠️ Pending Patches — Apply Before Phase 2](#5-️-pending-patches--apply-before-phase-2)
   - [5A: Strategic Fixes (Critical Bugs)](#5a-strategic-fixes--critical-bugs-found-in-code-audit)
   - [5B: Gap Register G1–G18](#5b-gap-register-g1g18--intelligence-completeness-patches)
6. [Phase 2 — Computation Engine](#6-phase-2--computation-engine)
7. [Phase 3 — Supervised Execution](#7-phase-3--supervised-execution)
8. [Phase 4 — Full Autonomy](#8-phase-4--full-autonomy)
9. [Frontend Dashboard Evolution](#9-frontend-dashboard-evolution)
10. [Daily Operating Procedure](#10-daily-operating-procedure)
11. [Emergency Procedures](#11-emergency-procedures)
12. [Complete Script Reference](#12-complete-script-reference)
13. [Supabase Tables Reference](#13-supabase-tables-reference)
14. [GitHub Actions Workflows](#14-github-actions-workflows)
15. [Environment Variables Reference](#15-environment-variables-reference)
16. [Troubleshooting Reference](#16-troubleshooting-reference)
17. [Repository Structure](#17-repository-structure)

---

## 1. System Status at a Glance

| Phase | Status | Description |
|-------|--------|-------------|
| **Phase 0** | ✅ **COMPLETE** | Pipeline running, signals generated, frontend live |
| **Phase 1** | ✅ **COMPLETE** | AI conviction, FII/DII, NSE events, Telegram alerts, scanner, shortlist AI |
| **Patches** | ✅ **APPLIED 03.14.2026** | Fix #1–#6 + NEW-A + NEW-B + base_provider + ai_router deployed. G1–G18 applied. |
| **Phase 2** | 🔶 Scripts Built | `compute_indicators`, `ingest_asm_gsm`, `data_quality_monitor` built. Gate criteria not yet met. `ml_regime_classifier.py` still to build. |
| **Phase 3** | 🔲 Future | Supervised execution via Telegram approval |
| **Phase 4** | 🔶 Scripts Built | `discovery_engine.py` built. `evolution_tracker.py` fully patched. Gate criteria not yet met. |

**Pipeline runs:** 7:00 AM (global cues + consolidated morning brief), 6:00 PM (main evening), Sundays 6 AM (ML training). Kite reconcile at 8:45 AM activates in Phase 2+ only.

**Google Sheet ID:** `1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw`

**Local codebase:** `C:\Users\vkshu\CRITICAL\Equity Indian Market Framework\tradeos-v6-complete\tradeos-v6`

**Standard git commit:**
```bash
cd "C:\Users\vkshu\CRITICAL\Equity Indian Market Framework\tradeos-v6-complete\tradeos-v6"
git add .
git commit -m "describe what you changed"
git push
```

---

## 2. Architecture Overview

> Full interactive diagram available in `tradeos_connectivity_map.html` → **Architecture tab** (first tab, loads by default).

### 2.1 System Layers

TradeOS v6 is a five-layer system. Every component has exactly one layer it belongs to.

| Layer | What It Does | Key Scripts |
|-------|-------------|-------------|
| **External Sources** | Pull raw data from the outside world | `fetch_chartink.py`, `ingest_bhavcopy.py`, `ingest_fii_dii.py`, `ingest_nse_events.py`, `ingest_global_cues.py`, `ingest_asm_gsm.py` |
| **Supabase Database** | Central state store — all scripts are stateless, all state lives here | 28 tables; no state ever lives in script memory between runs |
| **Signal + Computation Engine** | Generate and score trading signals from data | `generate_signals.py` (CTL/SBS/TPO/EAP), `independent_scanner.py` (5 patterns), `compute_indicators.py` (P2) |
| **AI/ML Layer** | Add conviction, learn from outcomes, evolve parameters | `ai_enrich.py` → 6 providers, `ml_provider.py`, `post_trade_analysis.py`, `evolution_tracker.py` |
| **Execution + Output Layer** | Alert, approve, execute, and monitor | `send_alerts.py`, `sl_monitor.py`, `kite_reconcile.py`, `execution_engine.py` (P3), `risk_manager.py` (P3) |

### 2.2 Data Flow — Evening Pipeline (6 PM, the main run)

```
NSE Chartink Atlas (CSV)
        │ fetch_chartink.py
        ▼
chartink_raw_data (500 stocks × ~60 cols)
        │
        ├── [Phase 2+] compute_indicators.py
        │   Applies RENAME_MAP (21 col renames) + computes:
        │   vol_ratio, ret_1w/1m/3m/6m/12m, dist_sma50/200,
        │   above_sma50/200, above_st, consol_range, breakout_setup,
        │   bk_trigger, price_location, rs_vs_nifty
        │   └──▶ stock_data_daily (computed cols + renamed pass-throughs)
        │
        ├── ingest_bhavcopy.py
        │   NSE delivery + OHLCV
        │   └──▶ raw_prices ──▶ [via ingest_sheets or compute P2] stock_data_daily
        │
        └── ingest_sheets.py  [deprecated Phase 4]
            Google Sheet 15 tabs
            └──▶ stock_data_daily, master_shortlist, open_positions,
                 closed_positions, market_regime, sector_strength,
                 industry_strength, event_calendar, lessons, nse_holidays,
                 nifty_total_market, nifty_upcoming_events, strategy_config,
                 system_config (14 tables total)

        ▼
generate_signals.py  [CTL + SBS + TPO + EAP rules]
Reads: stock_data_daily, master_shortlist, open_positions,
       market_regime, event_calendar, safety_lists, fii_dii_flow,
       industry_strength, sector_strength
        │
        └──▶ signal_log (BUY_CANDIDATE / EXIT / ADD / WATCH)
             with 9 ML feature cols (G1 fix): rsi, adx, vol_ratio,
             delivery_pct, atr_pct, ret_6m, dist_sma50, days_in_list

        ▼
ingest_fii_dii.py + ingest_nse_events.py + append_history.py

        ▼
post_trade_analysis.py  [for closed trades]
Reads: closed_positions, regime_history (entry context), nifty_upcoming_events
Writes: lessons (is_active=True, confidence=1.0) + outcome_pnl_pct to signal_log

        ▼
ai_enrich.py  [10-table context assembly]
Per BUY/EXIT/ADD signal, assembles:
  signal_log + stock_data_daily + event_calendar (14d)
  sector_strength + industry_strength + market_regime
  global_cues + open_positions + fii_dii_flow + lessons (active only)
Routes to: Claude / OpenAI / Gemini / DeepSeek / Grok / Copilot / ML local
Fallback: ai/fallback/ (web scraper + rule-based sentiment scorer)
Writes: ai_context (full JSON) + signal_log (ai_conviction, ai_note)
        + master_shortlist (ai_conviction cols)
        + ai_model_performance (cost + accuracy tracking)

        ▼
generate_shortlist.py  [AI top-12 selection]
Reads: master_shortlist (top 25), market_regime, event_calendar, open_positions
Writes: master_shortlist (ai_shortlist_rank, ai_shortlist_reason)
        + ai_context (symbol=__SHORTLIST__ reference row)

        ▼
send_alerts.py --evening  [Telegram digest]
Reads: signal_log + ai_context + market_regime + global_cues
       + open_positions + lessons + fii_dii_flow + master_shortlist
Sends: Regime header + BUY candidates (with entry zones + AI conviction)
       + exits + adds + FII footer
```

### 2.3 Morning Sequence (single message, no duplicates)

One job, one Telegram message at 7:00 AM. Everything you need before 9:15 AM is in one place.

```
7:00 AM  ingest_global_cues.py MORNING
         → global_cues (MORNING session: true US close-to-close %)

7:00 AM  send_alerts.py --morning
         → ☀️ ONE consolidated Telegram brief containing:
             Section 1: Overnight global cues (Gift Nifty, DOW, S&P, crude, USD/INR)
             Section 2: BUY candidates (AI conviction + entry zones from master_shortlist)
             Section 3: Open positions SL proximity watch
             Section 4: Position event risk (results/board meetings on held stocks, 5d window)
                        — only shows if events found; silent when all clear

8:30 AM  [MANUAL] kite_token_refresh.py              ← Phase 2+ only
         → system_config: kite_access_token

8:45 AM  kite_reconcile.py                           ← Phase 2+ only
         → open_positions sync + ⚠️ Telegram ONLY on mismatches

9:15 AM  sl_monitor.py [every 30 min during market hours]
         → 🚨 Telegram ONLY on SL breach or proximity (< 2% of SL)
```

**No more separate position_event_monitor.py step.** Position event risk is fetched inside `send_alerts.py --morning` from `nifty_upcoming_events` + `event_calendar` and rendered as Section 4 of the morning brief. `position_event_monitor.py` remains in the codebase as a standalone script for ad-hoc use but is not wired into `pipeline_morning.yml`.

**Kite jobs are Phase 2+ only.** `kite_reconcile.py` is not wired in `pipeline_morning.yml` during Phase 0/1. Wire it in when `autonomy_phase` is set to 2 and Kite API keys are active.

### 2.4 Supabase Database — Table Reference

28 active tables across 7 functional groups:

| Group | Tables | Purpose |
|-------|--------|---------|
| **Raw Ingestion** | `chartink_raw_data`, `raw_prices`, `stock_data_daily` | Source data + computed indicators |
| **Signals** | `signal_log`, `master_shortlist`, `scanner_signals` | Trade signals, MSL scores, pattern hits |
| **Market Context** | `market_regime`, `regime_history`, `fii_dii_flow`, `global_cues`, `nifty_total_market` | Macro + FII context |
| **Events** | `event_calendar`, `nifty_upcoming_events`, `nse_holidays`, `safety_lists` | Corporate actions, surveillance lists |
| **Positions** | `open_positions`, `closed_positions`, `shadow_trades` | Live trades + history |
| **AI/ML** | `ai_context`, `lessons`, `ai_model_performance`, `ml_training_log`, `evolution_proposals`, `discovery_proposals` | AI outputs + learning + self-improvement |
| **Config/Quality** | `strategy_config`, `system_config`, `sector_strength`, `industry_strength`, `data_anomalies`, `msl_history` | Parameters, kill switch, monitoring |

### 2.5 Key Design Principles

**Kill switch first.** Every script calls `is_kill_switch_active()` at entry. One SQL update halts the entire system immediately — no partial runs.

**Non-fatal by design.** Only three steps are FATAL (stop the pipeline on failure): `fetch_chartink`, `ingest_sheets`, `generate_signals`. All other steps are non-fatal — a failed FII ingestion never prevents signals from firing.

**Phase gate via config.** `system_config.autonomy_phase` controls all feature flags. Phase 2 scripts are already built — activating Phase 2 requires only an SQL update and meeting gate criteria, not a code deployment.

**Stateless scripts.** No script stores state between runs. All state lives in Supabase. Any script can be rerun safely without side effects.

**AI fallback chain.** `ai_router.py` tries the configured provider → falls back on error → tries other providers → falls back to local `ml_provider.py` (RandomForest, free, no API key) → falls back to `ai/fallback/` web scraper sentiment. The system always produces a conviction result.

**Google Sheet is temporary.** `ingest_sheets.py` currently feeds 14 Supabase tables from the Sheet. `compute_indicators.py` (Phase 2) replaces all formula columns from raw data. `ingest_sheets.py` is marked deprecated and is removed from the pipeline in Phase 4.

**Human approval at every critical gate.** Phase 3: every trade requires explicit Telegram APPROVE tap. Phase 4: every evolution/discovery proposal status is `PENDING` until you run an approval SQL. The system proposes, you decide.

### 2.6 Target State Diagram (Phase 4)

```
External Data Sources                    Zerodha Kite
──────────────────────────              ────────────────────
Chartink (500 stocks, daily)            Live Prices (LTP)
NSE Bhavcopy (OHLCV + delivery)         Order Placement (P3+)
NSE FII/DII flows                       Holdings Sync
NSE Corporate Calendar                  GTT orders
NSE ASM/GSM + FO Ban lists
Yahoo Finance (global cues)
         │                                      │
         ▼                                      ▼
──────────────────────────────────────────────────────────
                  SUPABASE  (28 tables)
        All state · All history · All config
──────────────────────────────────────────────────────────
         │                    │                 │
         ▼                    ▼                 ▼
  Signal Engine         AI/ML Layer        Evolution Layer
  CTL+SBS+TPO+EAP       6 providers        evolution_tracker
  generate_signals      ai_enrich          discovery_engine
  independent_scanner   ml_provider        lessons system
  compute_indicators    post_trade
         │                    │                 │
         └────────────────────┴─────────────────┘
                              │
                              ▼
                    Risk + Execution Layer
                    risk_manager (5 checks)
                    execution_engine (shadow → live)
                    kill_switch (always Gate 1)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         Telegram          Kite          Frontend
         Morning brief     Orders        HTML map
         Evening digest    GTT mgmt      (this file)
         SL alerts         Holdings
         APPROVE/REJECT    sync
         (Phase 3+)
```

---

## 3. Phase 0 — Foundation (COMPLETED)

> **Goal:** Pipeline running automatically. Google Sheet → Supabase → Signals → Frontend.

### What Was Built

| Script | What It Does | Status |
|--------|-------------|--------|
| `fetch_chartink.py` | Playwright scraper → Chartink Atlas CSV → Google Sheet + `chartink_raw_data` | ✅ Live |
| `ingest_sheets.py` | Reads all 15 Sheet tabs → syncs to Supabase | ✅ Live |
| `ingest_bhavcopy.py` | NSE bhavcopy → `stock_data_daily` delivery/volume columns | ✅ Live |
| `generate_signals.py` | CTL + SBS + TPO + EAP rule engine → `signal_log` | ✅ Live |
| `append_history.py` | Daily MSL snapshot → `msl_history` | ✅ Live |
| `kill_switch.py` | Emergency pipeline halt via `system_config` | ✅ Live |
| `backfill_msl_history.py` | One-time: loaded 708 historical MSL rows | ✅ Done |
| `pipeline_daily.yml` | GitHub Actions: 6 PM IST Mon–Fri | ✅ Running |
| `evolution_weekly.yml` | GitHub Actions: Sunday 6 AM IST, ML training | ✅ Running |

### One-Time Setup Steps (Already Completed — Reference Only)

<details>
<summary>Expand if you need to rebuild from scratch</summary>

**Step 1: Prerequisites**
```bash
python --version   # Must be 3.11 or 3.12
git --version
```

**Step 2: Configure .env**
```bash
cd tradeos-v6/backend
cp .env.example .env
# Fill in: SUPABASE_URL, SUPABASE_SERVICE_KEY, GOOGLE_SHEET_ID,
#          GOOGLE_CREDENTIALS_JSON, CHARTINK_EMAIL, CHARTINK_PASSWORD, TOTAL_CAPITAL
```

**Step 3: Google Sheets API (once only)**
1. console.cloud.google.com → Create project → Enable Google Sheets API
2. IAM & Admin → Service Accounts → Create → Keys → JSON
3. Rename to `service_account.json` → place in `backend/credentials/`
4. Share Google Sheet with service account email as **Editor**

**Step 4: Install dependencies**
```bash
cd tradeos-v6/backend
pip install -r requirements.txt
playwright install chromium
```

**Step 5: Database schema**
```sql
-- Run in Supabase SQL Editor in this order:
-- 1. backend/db/schema_v6_base.sql
-- 2. backend/db/schema_v6_signals.sql
-- 3. backend/db/schema_rls.sql
```
Then verify these additional tables/columns exist:
```sql
-- chartink_raw_data table (run if missing):
CREATE TABLE IF NOT EXISTS chartink_raw_data (
    id BIGSERIAL PRIMARY KEY, date DATE NOT NULL, symbol TEXT NOT NULL,
    sector TEXT, industry TEXT, market_cap NUMERIC, market_cap_cat TEXT,
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
    ingested_at TIMESTAMPTZ DEFAULT NOW(), UNIQUE (date, symbol)
);
CREATE INDEX IF NOT EXISTS idx_chartink_date   ON chartink_raw_data (date DESC);
CREATE INDEX IF NOT EXISTS idx_chartink_symbol ON chartink_raw_data (symbol);

-- signal_log industry columns:
ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS industry         TEXT,
    ADD COLUMN IF NOT EXISTS industry_rank    INT,
    ADD COLUMN IF NOT EXISTS industry_top5    BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS industry_state   TEXT,
    ADD COLUMN IF NOT EXISTS industry_avg_rsi NUMERIC;
NOTIFY pgrst, 'reload schema';
```

**Step 6: GitHub Actions secrets**
```
SUPABASE_URL, SUPABASE_SERVICE_KEY, GOOGLE_SHEET_ID,
GOOGLE_CREDENTIALS_JSON (full JSON contents), CHARTINK_EMAIL,
CHARTINK_PASSWORD, TOTAL_CAPITAL
```

**Step 7: Test locally then push**
```bash
python ingestion/fetch_chartink.py    # Should write 500 rows
python ingestion/ingest_sheets.py     # Should sync all 15 tabs
python signals/generate_signals.py    # Should produce BUY_CANDIDATEs
python run_pipeline.py --dry-run
```
</details>

### Phase 0 Completion Checklist
- ✅ `python run_pipeline.py` completes without errors
- ✅ `chartink_raw_data` has 30+ days of data
- ✅ `msl_history` has 708+ rows
- ✅ Frontend shows positions, sectors, BUY candidates
- ✅ RISK OFF warning banner visible
- ✅ GitHub Actions running at 6 PM IST weekdays

---

## 4. Phase 1 — Intelligence Layer (COMPLETED)

> **Goal:** AI conviction on signals, FII/DII context, NSE events automated, Telegram alerts live.

### What Was Built

| Script | What It Does | Status |
|--------|-------------|--------|
| `ai_enrich.py` | AI conviction + context → updates `signal_log` | ✅ Live |
| `ai_router.py` | Routes to configured provider, `raw_completion()` + `is_ai_available()` (patched) | ✅ Live |
| `providers/base_provider.py` | Shared `ConvictionResult` dataclass + `BaseProvider` ABC — all providers inherit this | ✅ Live |
| `providers/ml_provider.py` | Local RandomForest conviction (free fallback) | ✅ Live |
| `providers/claude/openai/gemini/deepseek/grok/copilot_provider.py` | Commercial AI providers (6 total) | ✅ Live |
| `ai/fallback/news_aggregator.py` | Combines scraped sources; fallback when AI budget exceeded | ✅ Live |
| `ai/fallback/web_scraper.py` | Scrapes NSE/BSE/Moneycontrol headlines (free, no auth) | ✅ Live |
| `ai/fallback/sentiment_scorer.py` | Rule-based headline scorer; deterministic, no LLM | ✅ Live |
| `generate_shortlist.py` | AI-powered top-12 MSL selection; replaces manual SHORTLISTED_12 | ✅ Live |
| `post_trade_analysis.py` | AI lesson extraction when trade closes | ✅ Live (has bugs — fix in §5A) |
| `ingest_fii_dii.py` | Daily FII/DII flows from NSE | ✅ Live (has bugs — fix in §5A) |
| `ingest_nse_events.py` | Auto-fetches corporate event calendar | ✅ Live |
| `ingest_global_cues.py` | 8 AM: Gift Nifty, USD/INR, crude → Telegram brief. **Also wired as step `00_global_cues` in evening pipeline (P0+, non-fatal)** | ✅ Live |
| `ingest_asm_gsm.py` | Fetches ASM/GSM/FO_BAN lists from NSE → `safety_lists`. Also mirrors `asm_flag`/`fo_ban_flag` into `stock_data_daily`. **Built ahead of Phase 2 gate.** | ✅ Built (v4.3 fix: kill switch + dry-run) |
| `position_event_monitor.py` | Standalone script: scans open positions for events. **Event risk is embedded in `send_alerts.py --morning` (Section 4) — not a separate pipeline step.** | ✅ Built (standalone only) |
| `independent_scanner.py` | 5 pattern scans parallel to rule engine | ✅ Live |
| `send_alerts.py` | 7 AM consolidated morning brief (cues + signals + SL watch + event risk) + 6 PM evening digest | ✅ Live |
| `pipeline_morning.yml` | GitHub Actions: single job 7 AM IST — global cues + one consolidated morning brief | ✅ Running |

### Phase 1 Activation (Already Done — Reference Only)
```sql
-- AI provider
UPDATE system_config SET value = 'claude' WHERE key = 'ai_provider';
-- Or: 'openai', 'gemini', 'deepseek', 'grok', 'copilot', 'ml', 'disabled'

-- Budget: ₹200/day max (20 stocks max)
UPDATE system_config SET value = '200' WHERE key = 'ai_daily_budget_inr';
UPDATE system_config SET value = '20'  WHERE key = 'ai_max_stocks_per_day';

-- Industry scoring bonuses
INSERT INTO system_config (key, value)
VALUES ('industry_scoring_active', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';

-- Telegram alerts
UPDATE system_config SET value = 'true' WHERE key = 'telegram_alerts_enabled';

-- Generate shortlist AI (optional — supplements ai_enrich.py)
INSERT INTO system_config (key, value)
VALUES ('shortlist_ai_enabled', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';

-- Phase gate
UPDATE system_config SET value = '1' WHERE key = 'autonomy_phase';
```

### Phase 1 Completion Checklist
- ✅ AI conviction (HIGH/MEDIUM/LOW) appearing on BUY_CANDIDATE signals
- ✅ `fii_dii_flow` table receiving daily rows (verify: not NULL)
- ✅ `event_calendar` auto-updated without manual Sheet entry
- ✅ Telegram receiving single morning brief at 7 AM (cues + signals + SL watch + event risk)
- ✅ Telegram receiving evening digest at 6 PM IST
- ✅ `lessons` table accumulating after each trade closes
- ✅ `chartink_raw_data` has 30+ days accumulated for ML training
- ✅ `generate_shortlist.py` producing `ai_shortlist_rank` on `master_shortlist`
- ✅ `ingest_asm_gsm.py` populating `safety_lists`
- ✅ `position_event_monitor.py` built — event risk embedded in morning brief (Section 4 of `send_alerts --morning`)

---

## 5. ⚠️ Pending Patches — Apply Before Phase 2

> These patches fix confirmed bugs found in a line-by-line code audit of the deployed scripts. The strategic fixes (#1–#6) are the most urgent — some mean critical features have never worked. Apply ALL patches before starting Phase 2.
>
> **Reference files:** The patch files are in `strategic_patches/` folder (generated alongside this document).

---

### 5A: Strategic Fixes — Critical Bugs Found in Code Audit

**Apply in this exact order. Run the SQL migration first.**

#### STEP 0 — Run SQL Migration (REQUIRED BEFORE ALL PATCHES)

Run `strategic_patches/sql/migration_strategic_fixes.sql` in Supabase SQL Editor.

This adds columns to 6 tables:
- `fii_dii_flow`: `_cr` alias columns for backward compatibility
- `lessons`: `is_active`, `times_applied`, `times_worked`, `confidence`
- `evolution_proposals`: `week_of`, `confidence`, `impact_measured_at`, `performance_delta`, `notes`
- `market_regime`: `predicted_regime`, `regime_confidence`, `regime_predicted_at`
- `shadow_trades`: CREATE TABLE (new)
- `open_positions`: `kite_qty`, `reconcile_status`, `last_reconciled_at`, `sl_breach_alerted`, `sl_proximity_alerted`

---

#### FIX #1 — FII/DII Column Name Mismatch (CRITICAL)
**File:** `backend/ingestion/ingest_fii_dii.py`
**Deploy:** Replace with `strategic_patches/patches/patch_ingest_fii_dii.py`

**Root cause:** Script wrote `fii_net_cr`, `dii_net_cr`, `fii_net_5d_cumulative`, `fii_signal` — none of these columns exist in the schema. Every run produced DB column-not-found errors silently. `compute_rolling_flows()` also read back `fii_net_cr`. Result: FII data was **never written**. The `fii_flag` in all signals and AI prompts has been NULL since Day 1.

**Fix applied:** Renamed all writes to canonical columns: `fii_net`, `dii_net`, `fii_net_5d`, `fii_net_10d`, `fii_net_20d`, `fii_flag`. Legacy `_cr` aliases also written for any external consumers. `compute_rolling_flows()` now reads `fii_net`.

```bash
# Deploy:
cp strategic_patches/patches/patch_ingest_fii_dii.py backend/ingestion/ingest_fii_dii.py

# Verify locally:
python ingestion/ingest_fii_dii.py --dry-run
# Expected: FII Net: -₹X Cr | DII Net: +₹Y Cr | Rolling 5d FII: -₹Z Cr → flag: CAUTION
```

---

#### FIX #2 — Post-Trade Analysis Circular Import / Max Recursion (CRITICAL)
**File:** `backend/ai/post_trade_analysis.py`
**Deploy:** Replace with `strategic_patches/patches/patch_post_trade_analysis.py`

**Root cause:** Two compounding bugs. (1) Unused `from ai.ai_router import analyze as ai_analyze` at top of file — ai_router imports post_trade_analysis during its own init, creating a circular import chain → `maximum recursion depth exceeded` before `main()` ever ran. (2) Duplicate `from config import cfg, AI_KEYS` inside `analyze_trade()` re-triggered module resolution on every call. Result: The **entire learning loop has been non-functional** — no lessons have been generated by AI; all lesson rows were written manually or never.

**Fix applied:** Removed unused ai_router import. Removed duplicate config import inside analyze_trade(). Added API key guard in `_call_provider()`. New lessons now correctly written with `is_active=True`, `times_applied=0`, `times_worked=0`, `confidence=1.0` defaults.

```bash
# Deploy:
cp strategic_patches/patches/patch_post_trade_analysis.py backend/ai/post_trade_analysis.py

# Verify (analyze a recent closed trade):
python ai/post_trade_analysis.py --all-recent
# Expected: lessons written to Supabase with is_active=True
```

---

#### FIX #3 — AI Enrich Silently Drops EXIT Signals (CRITICAL)
**File:** `backend/ai/ai_enrich.py`
**Deploy:** Replace with `strategic_patches/patches/patch_ai_enrich.py`

**Root cause:** All signals were loaded together with `.order("score", desc=True).limit(max_stocks)`. EXIT signals score ~50; BUY_CANDIDATEs score 70+. On busy days with 20+ buy candidates, open position EXIT analysis was silently dropped from the AI queue entirely — the stocks you already hold were never analyzed for exit timing. Also: `sd.get("adx_14")` was reading the wrong column name — should be `sd.get("adx")` after the RENAME_MAP.

**Fix applied:** EXIT + ADD signals now loaded first with no budget limit (naturally bounded by open positions count, typically 5–12). BUY_CANDIDATEs fill remaining budget. `adx` field corrected. `get_relevant_lessons()` now filters `is_active=True`.

```bash
# Deploy:
cp strategic_patches/patches/patch_ai_enrich.py backend/ai/ai_enrich.py
```

python ai\ai_enrich.py

---

#### FIX #4 — CAUTION Regime Completely Ignored in Signal Generation (STRATEGIC)
**File:** `backend/signals/generate_signals.py`
**Deploy:** 4 surgical edits — instructions in `strategic_patches/patches/patch_generate_signals_regime_eap.py`

**Root cause:** `generate_signals.py` only handled `RISK OFF` regime. `CAUTION` (the output label from `ml_regime_classifier`) was silently ignored — no score penalty, no warning flag. Also: the script always read the manual `regime` column, never `predicted_regime` from the ML classifier, meaning Phase 2's ML regime predictions would have had zero effect even when deployed.

**Fix applied:**
- Edit A: New `get_eap_action()` with event-type weighting (see Fix #6 below — same edit)
- Edit B: New `_resolve_regime()` helper: uses `predicted_regime` if `regime_predicted_at` < 24h old, falls back to manual `regime` otherwise
- Edit C: All regime reads now go through `_resolve_regime()`
- Edit D: CAUTION regime now penalises BUY_CANDIDATE score by 15% and sets `regime_warning=True`

```bash
# Deploy: Open patch_generate_signals_regime_eap.py and follow the 4 surgical edit instructions
# The file is annotated — do NOT replace the whole file, make the 4 targeted changes

# Verify:
python signals/generate_signals.py --dry-run
# Expect: CAUTION penalty visible in score output when regime = CAUTION
```

---

#### FIX #5 — Lessons Are Never Retired (STRATEGIC)
**File:** `backend/history/evolution_tracker.py`
**Deploy:** Replace with `strategic_patches/patches/patch_evolution_tracker.py`

**Root cause:** `lessons.is_active` existed in the schema but nothing ever set it to `False`. Old bull-market lessons from months ago were being fed as AI context in bear-market conditions indefinitely. Also: `week_of` field was written in code but the column didn't exist in the schema (SQL migration adds it).

**Fix applied:** Added `retire_stale_lessons()` function: sets `is_active=False` for any lesson with `times_applied >= 5` AND `times_worked / times_applied < 0.30` (less than 30% effectiveness). Runs at start of Sunday evolution run before proposal generation. Lesson query in `generate_proposals()` now filters `is_active=True`.

```bash
# Deploy:
cp strategic_patches/patches/patch_evolution_tracker.py backend/history/evolution_tracker.py
```

---

#### FIX #6 — EAP Treats All Events Identically (STRATEGIC)
**File:** `backend/signals/generate_signals.py`
**Deploy:** Included in Edit A of Fix #4 patch — same file, same deploy step.

**Root cause:** `get_eap_action()` triggered identical AVOID_ENTRY / PRIORITISE logic regardless of event type. A quarterly earnings announcement (high risk) was treated the same as a dividend record date (low risk, actually bullish). The `event_type` and `purpose` columns existed in `event_calendar` but were never read.

**Fix applied:**
- `HIGH` events (RESULTS, EARNINGS, BOARD_MEETING): AVOID_ENTRY 2 days before + PRIORITISE 2 days after
- `MEDIUM` events (AGM, CONCALL): AVOID_ENTRY 2 days before only
- `LOW` events (DIVIDEND, BONUS, SPLIT): PRIORITISE 2 days after only — never AVOID_ENTRY
- Per-symbol event check added (was sector-only before)

---

#### FIX NEW-A — No Intraday Stop-Loss Monitor (CRITICAL)
**New Script:** `backend/control/sl_monitor.py`
**Deploy:** Copy `strategic_patches/new_scripts/sl_monitor.py` to `backend/control/sl_monitor.py`

**Root cause:** `active_sl` was stored in Supabase for every open position, but nothing checked live prices against it during market hours. A stock could breach its stop by 3–5% before the 6 PM pipeline ran. You'd get an EXIT signal 7+ hours after the breach.

**New script behavior:**
- Runs every 30 minutes, 9:15–15:30 IST, Mon–Fri (via new `pipeline_intraday.yml` workflow)
- `is_market_open()` check: exits cleanly if called outside market hours
- **BREACH** (LTP ≤ active_sl): Urgent Telegram alert + ERROR anomaly + sets `sl_breach_alerted=True`
- **PROXIMITY** (within 2% of SL): Warning Telegram + WARN anomaly + sets `sl_proximity_alerted=True`
- Updates `current_price` and `last_reconciled_at` on every run

```bash
# Deploy script:
cp strategic_patches/new_scripts/sl_monitor.py backend/control/sl_monitor.py

# Deploy workflow:
cp strategic_patches/new_workflows/pipeline_intraday.yml .github/workflows/pipeline_intraday.yml

# Add GitHub Actions secrets (if not already present):
# KITE_API_KEY, KITE_ACCESS_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_KEY,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

# Verify locally:
python control/sl_monitor.py --dry-run
```

---

#### FIX NEW-B — No Kite↔Supabase Reconciliation (CRITICAL)
**New Script:** `backend/kite/kite_reconcile.py`
**Deploy:** Copy `strategic_patches/new_scripts/kite_reconcile.py` to `backend/kite/kite_reconcile.py`

**Root cause:** `open_positions` in Supabase could diverge silently from actual Kite holdings whenever a GTT fired, a manual exit happened, or a partial fill occurred. The risk manager used stale position data for sizing calculations — meaning position sizing could be miscalculated by entire positions.

**New script behavior:**
- Runs at 8:45 AM IST daily (via updated `pipeline_morning.yml`)
- Fetches `kite.holdings()` (CNC delivery positions)
- Four outcomes: MATCHED (no action), QTY_MISMATCH (Telegram warn), KITE_ONLY (Telegram critical — untracked position), DB_ONLY (Telegram warn — likely GTT-closed)
- Updates `current_price`, `kite_qty`, `reconcile_status`, `last_reconciled_at` on all matched positions
- Non-fatal: stale Kite token → logs warning, exits cleanly without failing morning pipeline

```bash
# Deploy script:
cp strategic_patches/new_scripts/kite_reconcile.py backend/kite/kite_reconcile.py

# Deploy updated morning workflow:
cp strategic_patches/new_workflows/pipeline_morning_updated.yml .github/workflows/pipeline_morning.yml
```

---

### Strategic Patch Deployment Checklist

```
[x] Step 0:   Run migration_strategic_fixes.sql in Supabase      ✅ Done 03.14.2026
[x] Step 0b:  Run sql_patches_g14_g16.sql (regime_history + global_cues cols) ✅ Done 03.14.2026
[x] Step 1:   Replace ingest_fii_dii.py          (Fix #1)        ✅ Done 03.14.2026
[x] Step 2:   Replace post_trade_analysis.py     (Fix #2)        ✅ Done 03.14.2026
[x] Step 3:   Replace ai_enrich.py               (Fix #3 + G6/G13/G17/G18) ✅ Done 03.14.2026
[x] Step 4:   Replace base_provider.py           (prompt upgrade — pairs with Step 3) ✅ Done 03.14.2026
[x] Step 5:   Replace ai_router.py               (raw_completion + is_ai_available) ✅ Done 03.14.2026
[x] Step 6:   Edit generate_signals.py (4 spots) (Fix #4 + #6)   ✅ Done 03.14.2026
[x] Step 7:   Replace evolution_tracker.py       (evolution_tracker_final.py — all fixes) ✅ Done 03.14.2026
[x] Step 8:   Replace append_history.py          (G14 regime snapshot) ✅ Done 03.14.2026
[x] Step 9:   Add sl_monitor.py                  (NEW-A)         ✅ Done 03.14.2026
[x] Step 10:  Add kite_reconcile.py              (NEW-B)         ✅ Done 03.14.2026
[x] Step 11:  Add pipeline_intraday.yml          (new workflow)  ✅ Done 03.14.2026
[x] Step 12:  Replace pipeline_morning.yml       (updated)       ✅ Done 03.14.2026
[x] Step 13:  git push → verify GitHub Actions   ✅ Done 03.14.2026
```

**Minimum viable deploy before tonight's pipeline:** Steps 0, 1, 2, 3 — these are independent and safe to deploy immediately.

---

### 5B: Gap Register G1–G18 — Intelligence Completeness Patches

> These patches complete the AI context assembly. After applying, the AI has the full picture it needs to make quality decisions. Apply in the order shown.

**Apply order:** G1 → G2 → G6 → G17 → G18 → G7-SQL → G3 → G5 → G13 → G8 → G9 → G4 → G14 → G15 → G16 → G10

| Gap | Severity | File | What It Fixes |
|-----|----------|------|---------------|
| G1  | 🔴 Critical | `generate_signals.py` | 9 ML feature columns missing from signal_log write |
| G2  | 🔴 Critical | `post_trade_analysis.py` | `outcome_pnl_pct` never written back to signal_log |
| G3  | 🟡 Moderate | `ai_enrich.py` | AI given empty computed fields with no warning |
| G4  | ⚪ Minor    | `ai_enrich.py` + SQL | `ai_strategy_validation` not persisted to signal_log |
| G5  | 🟡 Moderate | 4 provider files | JSON fence `\`\`\`json` not stripped before parsing |
| G6  | 🔴 Critical | `ai_enrich.py` | event_calendar never in AI context |
| G7  | 🟡 Moderate | SQL only | `lessons` missing confidence, is_active, effectiveness cols |
| G8  | 🟡 Moderate | `evolution_tracker.py` | v5 kill switch pattern (already in Fix #5 patch) |
| G9  | 🟡 Moderate | `evolution_tracker.py` | Lessons fetched without dedup/filtering (in Fix #5 patch) |
| G10 | ⚪ Low      | `ingest_fii_dii.py` | Verify column names match DB (fixed by Fix #1 above) |
| G11 | ✅ Resolved | `pipeline_morning.yml` | Cron confirmed correct |
| G12 | ✅ Resolved | `pipeline_morning.yml` | Morning alert confirmed wired |
| G13 | 🟡 Moderate | `ai_enrich.py` | sector_strength + industry_strength not in AI context |
| G14 | ⚪ Low      | `append_history.py` | `regime_history` snapshot never wired |
| G15 | ⚪ Low      | `evolution_tracker.py` | `ai_model_performance` not fed to evolution AI |
| G16 | ⚪ Low      | `ingest_global_cues.py` | S&P500 and change% columns missing |
| G17 | 🔴 Critical | `ai_enrich.py` | market_regime + global_cues never in AI context |
| G18 | 🔴 Critical | `ai_enrich.py` | open_positions portfolio never in AI context |

---

#### PATCH G1 — Add 9 ML Feature Columns to signal_log

**SQL first:**
```sql
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS rsi_daily    FLOAT,
  ADD COLUMN IF NOT EXISTS rsi_weekly   FLOAT,
  ADD COLUMN IF NOT EXISTS adx          FLOAT,
  ADD COLUMN IF NOT EXISTS vol_ratio    FLOAT,
  ADD COLUMN IF NOT EXISTS delivery_pct FLOAT,
  ADD COLUMN IF NOT EXISTS atr_pct      FLOAT,
  ADD COLUMN IF NOT EXISTS ret_6m       FLOAT,
  ADD COLUMN IF NOT EXISTS dist_sma50   FLOAT,
  ADD COLUMN IF NOT EXISTS days_in_list INT;
NOTIFY pgrst, 'reload schema';
```

**File:** `backend/signals/generate_signals.py` — in the `sig = { ... }` dict before the upsert, add:
```python
# G1 FIX — ML feature columns
"rsi_daily":    float(stock.get("rsi_daily")    or 0),
"rsi_weekly":   float(stock.get("rsi_weekly")   or 0),
"adx":          float(stock.get("adx_14")       or 0),
"vol_ratio":    float(stock.get("vol_ratio")    or 0),
"delivery_pct": float(stock.get("delivery_pct") or 0),
"atr_pct":      float(stock.get("atr_pct")      or 0),
"ret_6m":       float(stock.get("ret_6m")       or 0),
"dist_sma50":   float(stock.get("dist_sma50")   or 0),
"days_in_list": int(stock.get("days_in_list")   or 0),
```

---

#### PATCH G2 — Write outcome_pnl_pct Back to signal_log

**File:** `backend/ai/post_trade_analysis.py` — in the signal_log update block:
```python
# G2 FIX — add to existing signal_log update dict
"outcome_pnl_pct": round(pnl_pct, 4),
```

---

#### PATCH G3 — Zero-Data Prompt Guard

**File:** `backend/ai/ai_enrich.py` — before building the prompt string:
```python
# G3 FIX — warn AI when Phase 2 computed indicators not yet available
computed_cols = [vol_ratio, adx, dist_sma50, ret_6m, atr_pct]
computed_missing = all(v is None or float(v or 0) == 0 for v in computed_cols)
computed_note = (
    "\nNOTE: Computed technical indicators (vol_ratio, adx, dist_sma50, ret_6m) "
    "are not yet populated (compute_indicators.py Phase 2 not deployed). "
    "Base conviction on RSI, sector context, FII data, and event calendar only. "
    "Do not return empty JSON — use available fields."
) if computed_missing else ""
# Include computed_note in the prompt template: f"...{computed_note}..."
```

---

#### PATCH G5 — JSON Fence Strip (4 Files)

**Files:** `claude_provider.py`, `openai_provider.py`, `gemini_provider.py`, `grok_provider.py`

In each file, replace bare `json.loads(raw)` with:
```python
import re
json_match = re.search(r'\{.*\}', raw, re.DOTALL)
if not json_match:
    raise ValueError(f"No JSON object found in AI response: {raw[:200]}")
data = json.loads(json_match.group())
```

---

#### PATCH G6 — event_calendar in AI Context

**File:** `backend/ai/ai_enrich.py` — in context-building function:
```python
# G6 FIX
from datetime import datetime, timedelta
today_date = datetime.now(IST).date()
lookahead  = today_date + timedelta(days=14)
events = sb.table("event_calendar") \
    .select("event_type, event_date, detail") \
    .eq("symbol", symbol) \
    .gte("event_date", str(today_date)) \
    .lte("event_date", str(lookahead)) \
    .order("event_date").limit(5).execute().data
ai_context["upcoming_events"] = [
    f"{e['event_type']} on {e['event_date']}" + (f": {e['detail']}" if e.get("detail") else "")
    for e in events
] if events else ["No corporate events in next 14 days"]
```

---

#### PATCH G7 — lessons Quality Columns (SQL Only)

```sql
ALTER TABLE public.lessons
  ADD COLUMN IF NOT EXISTS confidence    NUMERIC  DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS times_applied INT      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_worked  INT      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS is_active     BOOLEAN  DEFAULT true,
  ADD COLUMN IF NOT EXISTS linked_symbols TEXT[]  DEFAULT '{}';
UPDATE public.lessons SET confidence = 0.7 WHERE source = 'MANUAL';
UPDATE public.lessons SET confidence = 0.5 WHERE source LIKE 'AI:%' OR source IS NULL;
NOTIFY pgrst, 'reload schema';
```
> Note: If you've already applied Fix #2 SQL migration above, this is already done.

---

#### PATCH G13 — sector_strength + industry_strength in AI Context

**File:** `backend/ai/ai_enrich.py` — after G6 block:
```python
# G13 FIX
sector   = stock.get("sector",   "")
industry = stock.get("industry", "")
sector_row = sb.table("sector_strength") \
    .select("sector, strength_score, trend, rank").eq("sector", sector).limit(1).execute().data
industry_row = sb.table("industry_strength") \
    .select("industry, rank, state, avg_rsi").eq("industry", industry).limit(1).execute().data
ai_context["sector_context"]   = sector_row[0]   if sector_row   else {"note": f"No sector data for {sector}"}
ai_context["industry_context"] = industry_row[0] if industry_row else {"note": f"No industry data for {industry}"}
```

---

#### PATCH G17 — market_regime + global_cues in AI Context

**File:** `backend/ai/ai_enrich.py` — after G13 block:
```python
# G17 FIX
regime_row = sb.table("market_regime") \
    .select("regime, regime_score, breadth_pct, advance_decline") \
    .order("date", desc=True).limit(1).execute().data
ai_context["market_regime"] = regime_row[0] if regime_row else {"regime": "UNKNOWN"}

cues = sb.table("global_cues") \
    .select("gift_nifty_chg_pct, gap_signal, brent_chg_pct, usd_inr_chg_pct, "
            "us_dow_chg_pct, us_nasdaq_chg_pct, sector_impacts") \
    .order("date", desc=True).limit(1).execute().data
ai_context["global_cues"] = cues[0] if cues else {"note": "No global cues today"}
```

---

#### PATCH G18 — open_positions Portfolio in AI Context

**File:** `backend/ai/ai_enrich.py` — after G17 block:
```python
# G18 FIX
positions = sb.table("open_positions") \
    .select("symbol, sector, strategy, invested_value, pnl_pct, lifecycle").execute().data
position_count  = len(positions)
sectors_held    = {}
strategies_held = {}
for p in positions:
    sec  = p.get("sector",   "Unknown")
    strat = p.get("strategy", "Unknown")
    sectors_held[sec]      = sectors_held.get(sec, 0) + 1
    strategies_held[strat] = strategies_held.get(strat, 0) + 1

already_held = any(p["symbol"] == symbol for p in positions)
sector_count = sectors_held.get(stock.get("sector", ""), 0)

ai_context["portfolio"] = {
    "total_open":        position_count,
    "already_held":      already_held,
    "sector_exposure":   sectors_held,
    "strategy_exposure": strategies_held,
    "this_sector_count": sector_count,
    "note": (
        f"Already holding {symbol} — consider HOLD not new entry." if already_held
        else f"{sector_count} positions already in {stock.get('sector','')} sector."
    )
}
```

---

#### PATCH G4 — Persist ai_strategy_validation

**SQL:**
```sql
ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS ai_strategy_validation TEXT;
NOTIFY pgrst, 'reload schema';
```
**File:** `backend/ai/ai_enrich.py` — in the signal_log update dict:
```python
"ai_strategy_validation": conviction_result.to_dict().get("ai_strategy_validation"),
```

---

#### PATCH G14 — Wire regime_history Snapshot

**File:** `backend/history/append_history.py` — add function + call at end of `main()`:
```python
def snapshot_regime(sb, today: str):
    try:
        regime = sb.table("market_regime").select("*").order("date", desc=True).limit(1).execute().data
        if regime:
            row = {k: v for k, v in regime[0].items() if k != "id"}
            row["snapshot_date"] = today
            sb.table("regime_history").upsert(row, on_conflict="snapshot_date").execute()
            logger.info(f"regime_history snapshot: {regime[0].get('regime')}")
    except Exception as e:
        logger.warning(f"regime_history snapshot failed (non-fatal): {e}")

# Call at end of main():
snapshot_regime(sb, str(today_ist()))
```

---

#### PATCH G15 — Feed ai_model_performance to evolution_tracker

**File:** `backend/history/evolution_tracker.py` — in `generate_proposals()` before building the AI prompt:
```python
# G15 FIX
perf_rows = sb.table("ai_model_performance") \
    .select("provider, accuracy, calls_today, cost_today, date") \
    .order("date", desc=True).limit(14).execute().data
provider_summary = "No provider performance data available"
if perf_rows:
    by_provider = {}
    for r in perf_rows:
        p = r.get("provider", "unknown")
        by_provider.setdefault(p, []).append(r)
    lines = []
    for prov, rows in by_provider.items():
        avg_acc = sum(float(r.get("accuracy") or 0) for r in rows) / len(rows)
        lines.append(f"  {prov}: avg_accuracy={avg_acc:.1%} over {len(rows)} days")
    provider_summary = "\n".join(lines)
# Include provider_summary in AI evolution prompt as {provider_performance}
```

---

#### PATCH G16 — S&P500 + Change % in global_cues

**SQL:**
```sql
ALTER TABLE public.global_cues
  ADD COLUMN IF NOT EXISTS us_dow_chg_pct    NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS us_nasdaq_chg_pct NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_close       NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_chg_pct     NUMERIC NULL;
NOTIFY pgrst, 'reload schema';
```

**File:** `backend/ingestion/ingest_global_cues.py` — in data-fetch section:
```python
# G16 FIX
sp500       = yf.Ticker("^GSPC")
sp500_close = float(sp500.fast_info.get("lastPrice", 0) or 0)
sp500_prev  = float(sp500.fast_info.get("previousClose", 0) or 0)
sp500_chg_pct = round((sp500_close - sp500_prev) / sp500_prev * 100, 4) if sp500_prev else None

dow_prev    = float(yf.Ticker("^DJI").fast_info.get("previousClose", 0) or 0)
nasdaq_prev = float(yf.Ticker("^IXIC").fast_info.get("previousClose", 0) or 0)
dow_chg_pct = round((us_dow_close - dow_prev) / dow_prev * 100, 4) if dow_prev else None
nq_chg_pct  = round((us_nasdaq_close - nasdaq_prev) / nasdaq_prev * 100, 4) if nasdaq_prev else None
# Add to row dict: "us_dow_chg_pct": dow_chg_pct, "us_nasdaq_chg_pct": nq_chg_pct,
#                  "sp500_close": sp500_close, "sp500_chg_pct": sp500_chg_pct
```

---

#### PATCH G10 — Verify FII/DII Column Names

> **Already fixed by Fix #1 (Strategic Patch)**. G10 is resolved — ingest_fii_dii.py now writes to canonical column names.

---

### Complete Gap Register Checklist

```
Critical (must apply before Phase 2):
[x] G1:  SQL (9 cols) + generate_signals.py sig dict ✅ Done 03.14.2026
[x] G2:  post_trade_analysis.py outcome_pnl_pct  (in Fix #2 patch file) ✅ Done 03.14.2026
[x] G6:  ai_enrich.py — event_calendar in context ✅ Done 03.14.2026
[x] G17: ai_enrich.py — market_regime + global_cues in context ✅ Done 03.14.2026
[x] G18: ai_enrich.py — open_positions portfolio in context ✅ Done 03.14.2026
[x] G7:  SQL — lessons quality columns             (in SQL migration)✅ Done 03.14.2026
[x] G10: Already fixed by Fix #1 ✅ Done 03.14.2026

Moderate (apply before Phase 2):
[] G3:  ai_enrich.py — zero-data prompt guard
[x] G5:  4 provider files — JSON fence strip ✅ Done 03.14.2026
[x] G13: ai_enrich.py — sector + industry in context ✅ Done 03.14.2026
[x] G8:  evolution_tracker.py kill switch          (in Fix #5 patch) ✅ Done 03.14.2026
[x] G9:  evolution_tracker.py rich lessons context (in Fix #5 patch) ✅ Done 03.14.2026

Minor (apply before Phase 4):
[x] G4:  ai_enrich.py + SQL — ai_strategy_validation ✅ Done 03.14.2026
[x] G14: append_history.py — regime_history snapshot ✅ Done 03.14.2026
[x] G15: evolution_tracker.py — provider performance in AI ✅ Done 03.14.2026
[x] G16: ingest_global_cues.py — S&P500 + change% ✅ Done 03.14.2026

Resolved:
[x] G11: pipeline_morning.yml cron — confirmed correct ✅
[x] G12: Morning alert — confirmed wired ✅
[x] G10: FII column names — fixed by Fix #1 ✅ Done 03.14.2026
[x] G8:  Kill switch pattern — fixed by Fix #5 ✅ Done 03.14.2026
[x] G9:  Lesson dedup/filtering — fixed by Fix #5 ✅ Done 03.14.2026

03.15.2026 - Fixed all the data gaps in AI enrich, generate signals lessons and other scripts
```

---

### 5C: v4.3 Script Fixes — Code Audit Against Future State Design

> Applied March 2026. These fixes correct bugs found by auditing the 5 ahead-of-gate scripts against the HTML connectivity map future state. No SQL migrations required for any of these.

#### FIX v4.3-A — `ingest_asm_gsm.py` Kill Switch + Dry-Run
**Root cause:** Used `check_kill_switch()` — the v5 pattern that raises an exception instead of returning a bool. Would crash rather than skip cleanly. Also had no dry-run support for testing.
**Fix:** Replaced with `is_kill_switch_active()` guard. Added `DRY_RUN` env var + `--dry-run` CLI flag. `update_stock_data_flags()` extracted as non-fatal helper with proper try/catch.

#### FIX v4.3-B — `compute_indicators.py` Field Name Errors (CRITICAL)
**Root cause:** Four compounding bugs:
1. `raw.get("close")` used throughout — field is `daily_close` in `chartink_raw_data`. All Level 1/2 computations were reading `None` and producing `None` outputs silently.
2. `fetch_historical_closes()` queried a `close` column that doesn't exist in `chartink_raw_data` (correct column: `daily_close`). All return calculations (`ret_1m/3m/6m`) were always `None`.
3. `raw.get("low_30d")` — this field doesn't exist in chartink. Rolling low must be computed from `daily_low` history. New `fetch_low_30d()` function added.
4. `build_upsert_row()` was writing `adx_14` (raw name) instead of `adx` (canonical). RENAME_MAP pass-through was entirely missing — none of the 21 renamed fields (`open`, `high`, `low`, `close`, `adx`, `di_plus`, etc.) were being written.

**Fix:** All four corrected. Full RENAME_MAP applied in `build_upsert_row()`. All pass-through fields wired. Missing computed fields added: `above_st`, `sma50_gt_200`, `price_location`, `bk_trigger`, `ret_1w`, `ret_12m`. `breakout_setup` formula corrected to design spec (`close > sma50 AND vol_ratio > 1.5 AND consol_range < 8`). Kill switch + dry-run added.

#### FIX v4.3-C — `generate_shortlist.py` AIRouter Class + Missing Write
**Root cause:** Two bugs:
1. Imported `AIRouter` class which no longer exists after the ai_router patch (§5A Step 5). Would raise `ImportError` on every run.
2. Only wrote to `ai_context` table — never wrote `ai_shortlist_rank` / `ai_shortlist_reason` back to `master_shortlist`, which is the entire purpose of the script per the future state design.

**Fix:** Replaced `AIRouter()` with module-level `raw_completion()` + `is_ai_available()` imports. New `write_to_master_shortlist()` function writes rank and reason per symbol back to `master_shortlist` (primary write). `ai_context` write demoted to secondary backup. Kill switch + dry-run added.

#### FIX v4.3-D — `position_event_monitor.py` Built from Scratch
**Status:** Was not in codebase. Now built at `backend/ingestion/position_event_monitor.py`.

**Note on wiring (v4.5 update):** Position event risk is now embedded directly inside `send_alerts.py --morning` as Section 4 of the consolidated morning brief. `position_event_monitor.py` is **not wired as a separate pipeline step** — it remains available for ad-hoc CLI use (`python ingestion/position_event_monitor.py --days 5`).

No `pipeline_morning.yml` step needed for this script.

#### No Changes Needed
- `discovery_engine.py` — code was correct. HTML STEP field names were wrong (fixed in HTML v1.3).

---

## 6. Phase 2 — Computation Engine

> **Goal:** Eliminate Google Sheet from the data computation path. All technical indicators computed in Python from raw data. ASM/GSM auto-fetched. Data quality monitored. ML regime classifier replaces Sheet formula.

### Gate Criteria (ALL required before starting)
- [ ] All strategic patches (§5A) deployed and verified
- [ ] All critical gap patches G1, G2, G6, G17, G18 applied
- [ ] Phase 1 pipeline has run cleanly for 30+ consecutive trading days
- [ ] `chartink_raw_data` table has 30+ days of data
- [ ] Google Sheet formula audit completed (screenshot each computed column, classify as Type A: will eliminate, or Type B: will keep)

### Activate Phase 2
```sql
UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';
NOTIFY pgrst, 'reload schema';
```

---

### Step 2.0 — Run Phase 2 SQL Migrations

```sql
-- safety_lists table (ASM/GSM/FO_BAN) — per-symbol rows, full replace daily
-- ⚠ SCHEMA NOTE: actual code writes per-symbol rows (not per-date aggregate).
-- ingest_asm_gsm.py does full DELETE + bulk INSERT each run (no upsert).
CREATE TABLE IF NOT EXISTS public.safety_lists (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  list_type   TEXT NOT NULL,          -- 'ASM' | 'GSM' | 'FO_BAN'
  stage       TEXT,                   -- ASM stage number or GSM stage, NULL for FO_BAN
  added_date  DATE DEFAULT CURRENT_DATE,
  UNIQUE (symbol, list_type)
);

-- data_anomalies table
CREATE TABLE IF NOT EXISTS public.data_anomalies (
  id         BIGSERIAL PRIMARY KEY,
  date       DATE,
  check_name TEXT,
  severity   TEXT,  -- 'OK' | 'WARN' | 'ERROR'
  value      TEXT,
  message    TEXT,
  affected   TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- market_regime ML prediction columns (if not added in migration_strategic_fixes.sql)
ALTER TABLE market_regime
  ADD COLUMN IF NOT EXISTS predicted_regime    TEXT,
  ADD COLUMN IF NOT EXISTS regime_confidence   NUMERIC,
  ADD COLUMN IF NOT EXISTS regime_predicted_at TIMESTAMPTZ;

-- stock_data_daily Phase 2 columns
ALTER TABLE stock_data_daily
  ADD COLUMN IF NOT EXISTS kite_price       NUMERIC,
  ADD COLUMN IF NOT EXISTS predicted_regime TEXT;

NOTIFY pgrst, 'reload schema';
```

---

### Step 2.1 — Apply RENAME_MAP in compute_indicators.py (CRITICAL)

**Why:** `chartink_raw_data` uses raw Chartink column names (e.g., `adx_14`). `stock_data_daily` uses canonical names (e.g., `adx`). This mapping must be applied when computing indicators so all downstream scripts read the right column names.

**21 Column Renames to apply in `compute_indicators.py`:**

| Chartink Raw Name | Canonical Name (stock_data_daily) |
|-------------------|-----------------------------------|
| `adx_14`          | `adx` |
| `adx_plus_di`     | `adx_plus_di` (same) |
| `adx_minus_di`    | `adx_minus_di` (same) |
| `atr_14`          | `atr` |
| `atr_pct`         | `atr_pct` (same) |
| `daily_close`     | `close` |
| `daily_open`      | `open` |
| `daily_high`      | `high` |
| `daily_low`       | `low` |
| `sma_10`          | `sma10` |
| `sma_20`          | `sma20` |
| `sma_50`          | `sma50` |
| `sma_200`         | `sma200` |
| `avg_vol_20`      | `vol_avg_20` |
| `avg_vol_50`      | `vol_avg_50` |
| `rsi_daily`       | `rsi_daily` (same) |
| `rsi_weekly`      | `rsi_weekly` (same) |
| `rsi_monthly`     | `rsi_monthly` (same) |
| `pct_change`      | `pct_change_daily` |
| `ha_close`        | `ha_close` (same) |
| `vwap_daily`      | `vwap` |

In `compute_indicators.py` define and apply the map:
```python
RENAME_MAP = {
    "adx_14": "adx", "atr_14": "atr", "daily_close": "close",
    "daily_open": "open", "daily_high": "high", "daily_low": "low",
    "sma_10": "sma10", "sma_20": "sma20", "sma_50": "sma50", "sma_200": "sma200",
    "avg_vol_20": "vol_avg_20", "avg_vol_50": "vol_avg_50",
    "pct_change": "pct_change_daily", "vwap_daily": "vwap",
}
df = df.rename(columns=RENAME_MAP)
```

---

### Step 2.2 — Build compute_indicators.py ✅ ALREADY BUILT

> **Script exists in `backend/compute/compute_indicators.py`** — built ahead of Phase 2 gate. Activate once gate criteria are met by wiring into `run_pipeline.py` and setting `autonomy_phase=2`.

**Path:** `backend/compute/compute_indicators.py` *(not `backend/ingestion/` — see repo structure)*

**Reads:** `chartink_raw_data`, `stock_data_daily` (bhavcopy cols), `nifty_total_market`

**Computes and writes to `stock_data_daily`:**
```
Level 1 (direct from raw):
  vol_ratio        = volume / avg_vol_20
  atr_pct          = atr / close * 100
  above_sma50      = close > sma50
  above_sma200     = close > sma200
  dist_sma50       = (close - sma50) / sma50 * 100
  dist_sma200      = (close - sma200) / sma200 * 100

Level 2 (requires rolling window on chartink history):
  ret_1m           = (close - close_20d_ago) / close_20d_ago * 100
  ret_3m           = (close - close_63d_ago) / close_63d_ago * 100
  ret_6m           = (close - close_126d_ago) / close_126d_ago * 100
  consol_range     = (high_20d - low_20d) / close * 100
  breakout_setup   = close > sma50 AND vol_ratio > 1.5 AND consol_range < 8

Level 3 (market-relative, needs nifty_total_market):
  rs_vs_nifty      = ret_20d_stock - ret_20d_nifty
```

**Wire into `run_pipeline.py`:**
```python
def step_compute_indicators():
    from ingestion.compute_indicators import main as fn; return fn()

# Add BEFORE ingest_sheets in the steps list (Phase 2+):
("03_compute_indicators", step_compute_indicators, False),  # non-fatal
```

**Test:**
```bash
python ingestion/compute_indicators.py --dry-run
# Expected: vol_ratio, ret_6m, dist_sma50 computed for 500 stocks
python ingestion/compute_indicators.py
# Write to Supabase. Verify: stock_data_daily.vol_ratio is no longer NULL
```

---

### Step 2.3 — Build ingest_asm_gsm.py ✅ ALREADY BUILT

> **Script exists in `backend/ingestion/ingest_asm_gsm.py`** — built and classified Phase 1 in its docstring. `safety_lists` table must be created (Step 2.0 SQL) and the step wired into `run_pipeline.py` for Phase 2+.

**Path:** `backend/ingestion/ingest_asm_gsm.py`

**Reads:** NSE website (ASM Stage 1/2, GSM Stage 1–6, F&O ban list)
**Writes:** `safety_lists` table

**Wire into `run_pipeline.py`:** After `nse_events`, before `post_trade`:
```python
("08a_asm_gsm", step_asm_gsm, False),  # non-fatal
```

**Fallback:** If fetch fails, retain previous day's list (non-fatal to pipeline).

**Test:**
```bash
python ingestion/ingest_asm_gsm.py --dry-run
# Expected: 15–40 ASM symbols, 5–20 GSM symbols, 10–30 FO_BAN symbols
```

---

### Step 2.4 — Build ml_regime_classifier.py 🔲 STILL TO BUILD

**Path:** `backend/ai/providers/ml_regime_classifier.py` *(not yet in codebase — only script in Phase 2 not yet built)*

**What it does:** Trains RandomForest to classify market regime from objective data instead of a manual Sheet formula.

**Features (7):** Nifty 5d/20d return, advance/decline ratio, `breadth_pct`, FII net 5d/20d, sector dispersion

**Labels:** `TRENDING` / `NEUTRAL` / `CAUTION` / `RISK OFF`

**Training data:** `regime_history` table (primary), with `market_regime` table as fallback for sparse history

**Writes:** `predicted_regime` + `regime_confidence` to `market_regime` table alongside the manual `regime` value. Both coexist — the `_resolve_regime()` helper in `generate_signals.py` (Fix #4) picks the ML prediction when it's fresh.

**Wire into `evolution_weekly.yml`** (Step 2, after ml_provider.py training):
```yaml
- name: Train Regime Classifier
  run: python backend/ai/providers/ml_regime_classifier.py --train
```

**Wire into `run_pipeline.py`** (daily predict only, non-fatal):
```python
def step_regime_predict():
    import subprocess, sys
    subprocess.run([sys.executable, "ai/providers/ml_regime_classifier.py", "--predict"],
                   capture_output=True)

# Add in Evening Pipeline steps, Phase 2+, after ingest_sheets:
("regime_predict", step_regime_predict, False),
```

**Test:**
```bash
python ai/providers/ml_regime_classifier.py --train
# Expected: Training complete. Accuracy: ~0.72. Model saved.
python ai/providers/ml_regime_classifier.py --predict
# Expected: predicted_regime=NEUTRAL (conf=0.81) written to market_regime
```

---

### Step 2.5 — Build data_quality_monitor.py ✅ ALREADY BUILT

> **Script exists in `backend/compute/data_quality_monitor.py`** — built ahead of Phase 2 gate. A duplicate also exists at `backend/ingestion/data_quality_monitor.py` — the canonical location is `backend/compute/`. Wire into `run_pipeline.py` as Step 99 when activating Phase 2.

**Path:** `backend/compute/data_quality_monitor.py` *(canonical — not `backend/ingestion/`)*

**What it does:** Runs after every pipeline. Validates data quality across all major tables. Logs anomalies. Alerts on errors.

**10 Checks:**

| Check | What It Validates | On Failure |
|-------|------------------|------------|
| C01 | Chartink row count 450–510 | ERROR → Telegram |
| C02 | RSI range 0–100 | WARN (auto-cap) |
| C03 | vol_ratio auto-cap at 50x | WARN (correct silently) |
| C04 | delivery_pct bounds 0–100 | WARN |
| C05 | signal scores 0–120 | WARN |
| C06 | MSL score jumps > 20pts in one day | WARN |
| C07 | Pipeline completeness (did all steps write today?) | ERROR → Telegram |
| C08 | AI context completeness (G6/G17/G18 patches active?) | WARN |
| C09 | ML vs manual regime disagreement ≥ 2 tiers (Phase 2+) | WARN |
| C10 | Open positions vs regime cap (e.g., >8 positions in RISK OFF) | WARN |

**Wire:** Last step in pipeline, always, non-fatal:
```python
("99_quality_check", step_quality_check, False),
```

---

### Step 2.6 — Update run_pipeline.py for Phase 2

> **Note:** `step_global_cues_evening` (step `00_global_cues`) is **already wired as a non-fatal P0 step** in the current `run_pipeline.py`. The Phase 2 upgrade adds `compute_indicators`, `asm_gsm`, `regime_predict`, and `quality_check` around the existing step sequence.

```python
# New step definitions (canonical paths — compute/ not ingestion/):
def step_compute_indicators():
    from compute.compute_indicators import main as fn; return fn()

def step_asm_gsm():
    from ingestion.ingest_asm_gsm import main as fn; return fn()

def step_quality_check():
    from compute.data_quality_monitor import main as fn; return fn()

def step_regime_predict():
    import subprocess, sys
    subprocess.run([sys.executable, "ai/providers/ml_regime_classifier.py", "--predict"],
                   capture_output=True)

# Rebuild all_steps when phase >= 2:
# NOTE: steps_p0[0] = 00_global_cues (already wired as P0 step — keep it)
# steps_p0 indexes: [0]=global_cues, [1]=fetch_chartink, [2]=ingest_bhavcopy,
#                   [3]=ingest_sheets, [4]=signals, [5]=history
if phase >= 2:
    all_steps = (
        [steps_p0[0], steps_p0[1], steps_p0[2]]                       # global_cues, chartink, bhavcopy
        + [("03_compute_indicators", step_compute_indicators, False)]  # NEW — before sheets
        + [("03b_regime_predict", step_regime_predict, False)]         # NEW — ML regime before signals
        + steps_p0[3:]                                                 # ingest_sheets, signals, history
        + steps_p1[:2]                                                 # fii_dii, nse_events
        + [("08a_asm_gsm", step_asm_gsm, False)]                      # NEW — after nse_events
        + steps_p1[2:]                                                 # post_trade, ai_enrich, alerts
        + [("99_quality_check", step_quality_check, False)]            # NEW — always last
    )
```

---

### Step 2.7 — Set Up Kite Token Refresh

> **This is a permanent daily manual step — Zerodha cannot be automated for login.**

```bash
# Run every morning at ~8:30 AM before pipeline:
python kite/kite_token_refresh.py
# Opens browser → you log in → token saved to system_config automatically
# Expected: "Access token saved. Expires: 2026-XX-XX 03:30:00 IST"
```

Add to GitHub Actions secrets:
```
KITE_API_KEY     → your Kite app key
KITE_API_SECRET  → your Kite app secret
```

---

### Phase 2 Completion Checklist
```
[ ] SQL: Phase 2 tables created (safety_lists — per-symbol schema, data_anomalies)
[ ] SQL: RENAME_MAP columns verified in stock_data_daily
[ ] SQL: Add asm_flag BOOLEAN, fo_ban_flag BOOLEAN columns to stock_data_daily (for ingest_asm_gsm flag writes)
[x] compute_indicators.py    — ✅ BUILT + FIXED (backend/compute/). Wire into run_pipeline.py + test vol_ratio/ret_6m not NULL
[x] ingest_asm_gsm.py        — ✅ BUILT + FIXED (backend/ingestion/). Wire into run_pipeline.py + test safety_lists populated
[ ] ml_regime_classifier.py  — 🔲 NOT YET BUILT. Build at backend/ai/providers/ml_regime_classifier.py
[x] data_quality_monitor.py  — ✅ BUILT (backend/compute/). Wire into run_pipeline.py as step 99 + test data_anomalies rows
[ ] run_pipeline.py updated with Phase 2 step order (step indexes updated per §2.6 above)
[ ] evolution_weekly.yml updated with ml_regime_classifier --train step
[ ] Kite token refresh routine established
[ ] Phase 1 was stable 30+ days before this was started
[ ] UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase'
```

---

## 7. Phase 3 — Supervised Execution

> **Goal:** Every trade requires your explicit Telegram tap. System proposes, you approve, Kite executes. No position is taken without your action.

### Gate Criteria (ALL required)
- [ ] Phase 2 stable for 30+ days
- [ ] Win rate ≥ 50% (check `closed_positions` for last 90 trades)
- [ ] Kill switch tested manually at least once
- [ ] 2-week shadow trade review completed (shadow_trade_logger.py --summary reviewed)
- [ ] `data_anomalies` showing no recurring ERROR-level issues

### Execution Flow
```
Evening pipeline fires signal
        ↓
ai_enrich.py gives conviction (HIGH/MEDIUM/LOW)
        ↓
send_alerts.py sends Telegram message:
  "BUY SBIN 50 shares @ ₹1201
   Score: 72 | CTL | EAP: PRIORITISE
   Ind: STRONG #3 | FII: ACCELERATOR
   Claude: HIGH conviction — SL: ₹1152
   [✅ APPROVE] [❌ REJECT] [⏸ DEFER]"
        ↓
You tap APPROVE
        ↓
telegram_bot.py → execution_engine.py
        ↓
risk_manager.py checks: kill switch, max positions, sector concentration, ASM/GSM, capital
        ↓ (all checks pass)
Kite order placed
        ↓
Telegram confirmation sent
```

---

### Step 3.0 — Run Phase 3 SQL Migrations

```sql
-- signal_log execution tracking
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS execution_status TEXT,   -- PENDING | APPROVED | REJECTED | DEFERRED | EXECUTED
  ADD COLUMN IF NOT EXISTS kite_order_id    TEXT,
  ADD COLUMN IF NOT EXISTS execution_price  NUMERIC,
  ADD COLUMN IF NOT EXISTS executed_at      TIMESTAMPTZ;

-- shadow_trades (already added in strategic patch SQL migration)
-- Verify it exists:
SELECT COUNT(*) FROM shadow_trades;

NOTIFY pgrst, 'reload schema';
```

---

### Step 3.1 — Deploy risk_manager.py

**Path:** `backend/control/risk_manager.py` ✅ Already generated in prior session.

**5 Checks in order:**
1. Kill switch active → block
2. Max positions for current regime (TRENDING: 12, NEUTRAL: 10, CAUTION: 8, RISK OFF: 0 new buys)
3. Sector concentration ≤ 30% of total capital
4. Symbol not on ASM/GSM/FO_BAN list
5. Available capital ≥ order size

Returns `RiskCheckResult(passed, reason, check_name)` — never touches Kite directly.

**Test:**
```bash
python control/risk_manager.py --symbol SBIN --qty 50 --price 1200
# Expected: RiskCheckResult(passed=True, reason='', check_name='')
```

---

### Step 3.2 — Deploy shadow_trade_logger.py

**Path:** `backend/control/shadow_trade_logger.py` ✅ Already generated.

**Enable shadow mode:**
```sql
INSERT INTO system_config (key, value) VALUES ('execution_mode', 'shadow')
ON CONFLICT (key) DO UPDATE SET value = 'shadow';
```

**Run for 2 weeks in shadow mode** before going live. After 2 weeks:
```bash
python control/shadow_trade_logger.py --summary
# Reviews: approval_rate, risk_block_rate, estimated_pnl on APPROVED trades
# Shows: ready_for_live boolean
```

**Only move to live after summary shows `ready_for_live: True`.**

---

### Step 3.3 — Deploy execution_engine.py

**Path:** `backend/control/execution_engine.py` ✅ Already generated.

**Three-mode gate:**
- `disabled` (default — Phase 2 and below)
- `shadow` (routes to shadow_trade_logger, no real orders)
- `live` (real Kite orders, activated after shadow review)

Kill switch is always Gate 1, before even checking execution_mode.

---

### Step 3.4 — Update telegram_bot.py with Inline Buttons

**Path:** `backend/control/telegram_bot.py`

Add `APPROVE / REJECT / DEFER` inline keyboard to each BUY_CANDIDATE message.

**Deploy as persistent service** (not a pipeline step — must be always-on):
- **Render.com** (free tier): Deploy as a web service, set start command to `python backend/control/telegram_bot.py`
- **Railway**: Similar setup
- **Local**: Only if you're always available at signal time

```bash
python control/telegram_bot.py   # starts long-polling loop
```

---

### Step 3.5 — Activate Phase 3

```sql
-- Step 1: Start in shadow mode
UPDATE system_config SET value = '3'      WHERE key = 'autonomy_phase';
UPDATE system_config SET value = 'shadow' WHERE key = 'execution_mode';

-- Step 2: After 2-week shadow review passes:
UPDATE system_config SET value = 'live'   WHERE key = 'execution_mode';
```

---

### Phase 3 Completion Checklist
```
[ ] Phase 3 SQL migrations run
[ ] risk_manager.py deployed and tested
[ ] shadow_trade_logger.py running in shadow mode
[ ] 2-week shadow review completed and --summary shows ready_for_live: True
[ ] execution_engine.py deployed
[ ] telegram_bot.py running as persistent service
[ ] Inline APPROVE/REJECT buttons tested with a test signal
[ ] UPDATE system_config: autonomy_phase = '3'
[ ] UPDATE system_config: execution_mode = 'shadow' (2 weeks)
[ ] UPDATE system_config: execution_mode = 'live'   (after review)
```

---

## 8. Phase 4 — Full Autonomy

> **Goal:** Autonomous execution within hard limits. Self-evolving strategy parameters. Google Sheet fully eliminated. Pattern discovery from 6+ months of data.

### Gate Criteria (ALL required — no exceptions)
- [ ] 90 continuous days of Phase 3 clean operation
- [ ] Win rate ≥ 55% across last 90 closed trades
- [ ] Max drawdown ≤ 8% during entire Phase 3 period
- [ ] G8 + G9 + G15 evolution_tracker patches verified working
- [ ] 6+ months of `chartink_raw_data` accumulated for discovery_engine
- [ ] You explicitly run the activation SQL — there is no automated trigger for Phase 4

```sql
-- Manual activation only — after all gates confirmed:
UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase';
```

---

### Step 4.0 — Run Phase 4 SQL Migrations

```sql
-- discovery_proposals table
CREATE TABLE IF NOT EXISTS public.discovery_proposals (
  id               BIGSERIAL PRIMARY KEY,
  week_of          DATE,
  feature_name     TEXT,
  correlation      NUMERIC,
  sample_size      INT,
  win_rate_with    NUMERIC,
  win_rate_without NUMERIC,
  evidence         TEXT,
  status           TEXT DEFAULT 'PENDING',
  created_at       TIMESTAMPTZ DEFAULT now()
);

-- evolution_proposals (verify exists, create if not)
CREATE TABLE IF NOT EXISTS public.evolution_proposals (
  id                BIGSERIAL PRIMARY KEY,
  week_of           DATE,
  proposal_type     TEXT,      -- 'PARAMETER_CHANGE' | 'STRATEGY_GATE' | 'PROVIDER_SWITCH'
  target_config     TEXT,      -- strategy_config key to change
  current_value     TEXT,
  proposed_value    TEXT,
  evidence          TEXT,
  expected_wr_delta NUMERIC,
  confidence        NUMERIC,
  status            TEXT DEFAULT 'PENDING',  -- 'PENDING' | 'APPROVED' | 'REJECTED' | 'REVERTED'
  applied_at        TIMESTAMPTZ,
  impact_measured_at TIMESTAMPTZ,
  performance_delta NUMERIC,
  notes             TEXT,
  created_at        TIMESTAMPTZ DEFAULT now()
);

NOTIFY pgrst, 'reload schema';
```

---

### Step 4.1 — Deploy evolution_tracker.py (with G8+G9+G15 Patches)

**Already deployed via Fix #5 patch.** Verify:
- `retire_stale_lessons()` is in the file and runs first on Sunday
- Lesson query filters `is_active=True`
- `week_of` is written on proposals
- G15 `ai_model_performance` context is included in evolution prompt

**Wire into `evolution_weekly.yml`** (Phase 4 conditional):
```yaml
- name: Run Evolution Tracker
  if: env.AUTONOMY_PHASE == '4'
  run: python backend/history/evolution_tracker.py
  env:
    AUTONOMY_PHASE: ${{ secrets.AUTONOMY_PHASE }}
```

---

### Step 4.2 — Build discovery_engine.py ✅ ALREADY BUILT

> **Script exists in `backend/history/discovery_engine.py`** — built ahead of Phase 4 gate. Wire into `evolution_weekly.yml` with Phase 4 conditional when gate criteria are met.

**Path:** `backend/history/discovery_engine.py`

**What it does:** Statistical ML (Pearson correlation, Mann-Whitney U test) on `chartink_raw_data` history vs `outcome_pnl_pct` outcomes. Identifies which raw data columns actually predict trade success — things the rule engine might not currently use.

**Requires:** G1 + G2 fixes applied AND 6+ months of `chartink_raw_data`

**Writes:** `discovery_proposals` with `status=PENDING` — never auto-applies anything

**Wire into `evolution_weekly.yml`** (Phase 4 conditional, after evolution_tracker):
```yaml
- name: Run Discovery Engine
  if: env.AUTONOMY_PHASE == '4'
  run: python backend/history/discovery_engine.py
```

---

### Step 4.3 — Google Sheet Elimination

When `autonomy_phase = 4`, remove `ingest_sheets.py` from `steps_p0`. All data now flows through automated Python scripts:

| Data Source | Before (Sheet) | After (Phase 4) |
|-------------|----------------|-----------------|
| Price + technicals | Sheet formula columns | `compute_indicators.py` from `chartink_raw_data` |
| Delivery data | Sheet manual entry | `ingest_bhavcopy.py` (NSE direct) |
| Live prices | Sheet manual update | Kite Connect (`kite_client.py`) |
| Open positions | Sheet manual update | Kite holdings sync via `execution_engine.py` |
| Closed positions | Sheet manual entry | Kite trade history |
| FII/DII | Sheet manual entry | `ingest_fii_dii.py` (NSE direct) |
| Events | Sheet manual entry | `ingest_nse_events.py` (NSE direct) |
| ASM/GSM | Sheet manual entry | `ingest_asm_gsm.py` (NSE direct) |
| Market regime | Sheet formula | `ml_regime_classifier.py` (weekly trained) |
| Strategy config | Sheet tab | `strategy_config` table (evolution_tracker after approval) |

---

### System Can / Cannot in Phase 4

**System CAN:**
- Execute trades within guardrails without per-trade approval
- Propose and (after your approval) apply strategy parameter changes
- Train and improve its own ML model weekly
- Retire ineffective lessons automatically
- Discover new signal patterns from history

**System CANNOT without your explicit SQL approval:**
- Change position sizing formula
- Modify guardrail thresholds or regime caps
- Add new data sources
- Change code
- Apply evolution or discovery proposals without your review

---

### Phase 4 Completion Checklist
```
[ ] 90 days Phase 3 clean confirmed
[ ] Win rate ≥ 55%, Max DD ≤ 8% confirmed
[ ] Phase 4 SQL migrations run
[ ] evolution_tracker.py G8+G9+G15 patches verified working
[x] discovery_engine.py — ✅ BUILT (backend/history/). Wire into evolution_weekly.yml Phase 4 conditional
[ ] evolution_weekly.yml updated with Phase 4 conditionals
[ ] Sheet formula audit: all Type A formulas replaced by compute_indicators.py
[ ] ingest_sheets.py removed from run_pipeline.py steps (Phase 4)
[ ] UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase'
```

---

## 9. Frontend Dashboard Evolution

> Each phase adds capabilities to `tradeos_connectivity_map.html` and the main trading dashboard.

| Phase | What the Dashboard Gains |
|-------|--------------------------|
| Phase 0 ✅ | Positions, signals (BUY/WATCH/EXIT), sector heat, regime banner, MSL scores |
| Phase 1 ✅ | AI conviction badges (HIGH/MEDIUM/LOW), FII flag on signals, industry strength context, Telegram preview |
| Patches | Correct FII data appears, EXIT signals properly AI-analyzed, CAUTION regime banner visible, event-type weighting in EAP flags |
| Phase 2 | Data quality anomalies panel, ML vs manual regime comparison, computed indicator completeness indicator, reconciliation status on positions |
| Phase 3 | Shadow trade log viewer, execution approval history, P&L vs shadow P&L comparison, risk block rate |
| Phase 4 | Evolution proposals panel (approve/reject inline), discovery proposals panel, lesson effectiveness dashboard, Sheet elimination progress tracker |

**Connectivity Map HTML (`tradeos_connectivity_map.html`):**
- Updated after each phase to reflect actual deployed state
- Includes 🔴 Patch Status tab showing all 8 strategic fixes + gap register status
- Use this as your architecture reference when making changes

---

## 10. Daily Operating Procedure

### 7:00 AM IST — Morning Pipeline (automated via `pipeline_morning.yml`)
1. `ingest_global_cues.py MORNING` → fetches true US close-to-close prices → `global_cues` (MORNING session)
2. `send_alerts.py --morning` → **single consolidated Telegram brief** containing:
   - 🌍 Overnight global cues (Gift Nifty gap, DOW, S&P 500, crude, USD/INR)
   - 🎯 BUY candidates with AI conviction + entry zones from `master_shortlist`
   - 📂 Open positions SL proximity watch
   - 📅 Position event risk (results/board meetings on held stocks, 5-day window — only renders if events found)

### 8:30 AM IST — Manual (Phase 2+, every trading day)
```bash
python kite/kite_token_refresh.py
# Zerodha security — cannot be automated. Takes 1 minute.
# Phase 2+ only — skip until autonomy_phase = 2
```

### 8:45 AM IST — Reconciliation (Phase 2+ only — not yet wired)
> Not active during Phase 0/1. Wire `kite_reconcile.py` into `pipeline_morning.yml` when activating Phase 2.
> `kite_reconcile.py` → compares Kite holdings vs Supabase `open_positions` → Telegram alert only on mismatches

### 4:00–5:30 PM IST — Manual Input (Phase 0–1 only, eliminated in Phase 4)
1. Update Google Sheet: current prices, position changes
2. Verify no formula errors in indicator columns

### 6:00 PM IST — Evening Pipeline (automated via `pipeline_daily.yml`)
```
Phase 0+ (all phases):
  00 global_cues_evening  (non-fatal)   → global_cues (EVENING session row — already wired in run_pipeline.py)
  01 fetch_chartink       (fatal)       → chartink_raw_data (500 stocks)
  02 ingest_bhavcopy      (non-fatal)   → stock_data_daily (delivery/volume)
  [Phase 2+]
  03 compute_indicators   (non-fatal)   → stock_data_daily (all computed cols)  ← compute/ path
  03b regime_predict      (non-fatal)   → market_regime.predicted_regime
  [All phases]
  04/05 ingest_sheets     (fatal)       → 15 tables (eliminated Phase 4)
  05/06 generate_signals  (fatal)       → signal_log (9 ML cols after G1)
  06/07 append_history    (non-fatal)   → msl_history + regime_history (after G14)
Phase 1+:
  07/08 fii_dii           (non-fatal)   → fii_dii_flow (correct cols after Fix #1)
  08/09 nse_events        (non-fatal)   → event_calendar
  [Phase 2+]
  09a   asm_gsm           (non-fatal)   → safety_lists (per-symbol schema)
  [All Phase 1+]
  09/10 post_trade        (non-fatal)   → lessons (working after Fix #2)
  10/11 ai_enrich         (non-fatal)   → signal_log + ai_context (full context after patches)
  11/12 generate_shortlist(non-fatal)   → master_shortlist (ai_shortlist_rank — Phase 1+)
  12/13 send_alerts       (non-fatal)   → Telegram evening digest
  [Phase 2+]
  99    quality_check     (non-fatal)   → data_anomalies (always last)  ← compute/ path
```

### Every 30 Minutes, 9:15–15:30 IST — Intraday (automated via `pipeline_intraday.yml`)
1. `sl_monitor.py` → checks live Kite prices vs stop-losses → Telegram breach/proximity alerts

### Every Sunday 6:00 AM IST — Weekly Evolution (`evolution_weekly.yml`)
1. `ml_provider.py --train` → RandomForest conviction model retrained on closed trades
2. `ml_regime_classifier.py --train` (Phase 2+) → Regime classifier retrained
3. `evolution_tracker.py` (Phase 4+, G8+G9+G15 required) → AI proposes parameter changes
4. `discovery_engine.py` (Phase 4+, 6+ months data required) → Statistical pattern discovery

---

## 11. Emergency Procedures

### Kill Switch — Halt Everything Immediately
```sql
-- Activate (stops all pipeline steps):
UPDATE system_config SET value = 'true' WHERE key = 'kill_switch_active';

-- Deactivate:
UPDATE system_config SET value = 'false' WHERE key = 'kill_switch_active';
```
```bash
# From command line:
cd backend
python control/kill_switch.py         # activate
python control/kill_switch.py off     # deactivate
python control/kill_switch.py status  # check
```

### Regime Override
```sql
-- Force regime manually (overrides both manual and ML values):
INSERT INTO system_config (key, value) VALUES ('regime_override', 'RISK OFF')
ON CONFLICT (key) DO UPDATE SET value = 'RISK OFF';

-- Remove override (returns to normal):
DELETE FROM system_config WHERE key = 'regime_override';
```

### Run Single Pipeline Step Manually
```bash
cd backend
python run_pipeline.py --step signals       # re-run signal generation only
python run_pipeline.py --step ai_enrich     # re-run AI enrichment only
python run_pipeline.py --step post_trade    # re-run lesson extraction
python run_pipeline.py --step fii_dii       # re-run FII/DII ingestion
python run_pipeline.py --step alerts        # re-send Telegram alerts
python run_pipeline.py --step quality_check # re-run data quality checks
```

### Pipeline Fails in GitHub Actions
1. GitHub → Actions tab → failed run → click into it → view logs
2. Download log artifact if text is truncated
3. Most common causes:
   - Sheet format changed → check column offsets in `ingest_sheets.py`
   - Supabase schema mismatch → run ALTER TABLE for missing column
   - Google API quota → wait 24h or increase quota in Cloud Console
   - Chartink hover fails → check `chartink_hover_debug.png` in backend folder; run with `headless=False`
   - Playwright not installed → add `playwright install chromium` step to YML

---

## 12. Complete Script Reference

### Ingestion Scripts

**`fetch_chartink.py`** — Phase 0
Playwright browser → Chartink Atlas CSV download → writes to Google Sheet tab "Chartink Raw Data_Nifty 500" → upserts to `chartink_raw_data`. Runs as Step 01 of evening pipeline.
```bash
python ingestion/fetch_chartink.py
python ingestion/fetch_chartink.py --dry-run   # print CSV, no write
```

**`ingest_sheets.py`** — Phase 0 (eliminated Phase 4)
Reads all 15 Google Sheet tabs → syncs to 15 Supabase tables. Must run after fetch_chartink (reads the freshly updated Chartink tab). Service account must have **Editor** access.
```bash
python ingestion/ingest_sheets.py
python ingestion/ingest_sheets.py --tab MASTER_SHORTLIST  # single tab
```

**`ingest_bhavcopy.py`** — Phase 0
NSE Bhavcopy → delivery%, delivery_qty, traded_value → upserts to `stock_data_daily`. Non-fatal.
```bash
python ingestion/ingest_bhavcopy.py
```

**`ingest_fii_dii.py`** — Phase 1 (patched Fix #1)
NSE FII/DII daily data → `fii_dii_flow`. Fixed column names: `fii_net`, `dii_net`, `fii_net_5d/10d/20d`, `fii_flag`.
```bash
python ingestion/ingest_fii_dii.py
python ingestion/ingest_fii_dii.py --dry-run
```

**`ingest_nse_events.py`** — Phase 1
NSE corporate events calendar → `event_calendar`. Replaces manual Sheet event entry.
```bash
python ingestion/ingest_nse_events.py
python ingestion/ingest_nse_events.py --positions-only  # events for held stocks only (Phase 2+)
```

**`ingest_global_cues.py`** — Phase 1 (patch G16 adds S&P500)
8 AM: Gift Nifty, USD/INR, Brent, Gold, US markets → `global_cues` → Telegram morning brief.
```bash
python ingestion/ingest_global_cues.py
```

**`compute_indicators.py`** — Phase 2 (build in Step 2.2)
Reads `chartink_raw_data` → computes vol_ratio, returns, distances, RS, breakout_setup → upserts computed columns to `stock_data_daily`.
```bash
python ingestion/compute_indicators.py
python ingestion/compute_indicators.py --backfill --from 2025-01-01
```

**`ingest_asm_gsm.py`** — Phase 2 (build in Step 2.3)
NSE ASM/GSM/FO_BAN lists → `safety_lists`.
```bash
python ingestion/ingest_asm_gsm.py
```

### Signal Scripts

**`generate_signals.py`** — Phase 0 (patched Fix #4+#6, G1)
Core rule engine: CTL + SBS + TPO + EAP strategies → scores → `signal_log`. Post-patches: CAUTION regime penalises scores, EAP weighted by event type, 9 ML feature columns written, `_resolve_regime()` uses ML prediction when fresh.
```bash
python signals/generate_signals.py
python signals/generate_signals.py --dry-run
python signals/generate_signals.py --date 2026-01-15
```

**`independent_scanner.py`** — Phase 1
5 pattern scans parallel to rule engine (VOLUME_SURGE, RS_BREAKOUT, POST_CONSOL, MEAN_REVERSION, DELIVERY_SURGE) → `scanner_signals`. Cross-reference bonus for stocks in both.
```bash
python signals/independent_scanner.py
```

### AI Scripts

**`ai_enrich.py`** — Phase 1 (patched Fix #3, G3/G5/G6/G13/G17/G18)
Loads BUY_CANDIDATE and EXIT signals (EXIT priority after Fix #3), builds full AI context (events, regime, sector, portfolio, FII after patches), calls AI provider, updates `signal_log`.
```bash
python ai/ai_enrich.py
python ai/ai_enrich.py --symbol SBIN
python ai/ai_enrich.py --dry-run
```

**`ai_router.py`** — Phase 1
Routes to configured provider. Tracks daily cost vs `ai_daily_budget_inr`. Auto-falls back to ML if budget exceeded. Logs to `ai_model_performance`.

**`post_trade_analysis.py`** — Phase 1 (patched Fix #2, G2)
When trade closes: builds context → AI extracts lesson → writes to `lessons` with `is_active=True`, `confidence=1.0`. Fixed: circular import and duplicate config import removed.
```bash
python ai/post_trade_analysis.py --all-recent
python ai/post_trade_analysis.py --symbol RBLBANK
```

**`ml_provider.py`** — Phase 1
Local RandomForest conviction model. Trains on `closed_positions` + `signal_log`. Needs 30+ closed trades (you have 54 — ready). Free, no API key.
```bash
python ai/providers/ml_provider.py --train
python ai/providers/ml_provider.py --evaluate
```

**`ml_regime_classifier.py`** — Phase 2 (build in Step 2.4)
RandomForest regime classifier. Replaces manual Sheet formula. `--train` on Sundays, `--predict` daily.
```bash
python ai/providers/ml_regime_classifier.py --train
python ai/providers/ml_regime_classifier.py --predict
```

### Control Scripts

**`kill_switch.py`** — Phase 0
```bash
python control/kill_switch.py         # activate
python control/kill_switch.py off     # deactivate
python control/kill_switch.py status  # check
```

**`sl_monitor.py`** — Phase 2 / NEW-A (deployed in §5A)
Every 30 min intraday: checks Kite live prices vs `active_sl` → Telegram breach/proximity alerts.
```bash
python control/sl_monitor.py
python control/sl_monitor.py --dry-run
```

**`risk_manager.py`** — Phase 3
Pre-trade guardrail check. Called by execution_engine.py. Returns pass/fail with reason.
```bash
python control/risk_manager.py --symbol SBIN --qty 50 --price 1200
```

**`shadow_trade_logger.py`** — Phase 3
Paper trade logger. Identical flow to execution_engine but no real Kite order.
```bash
python control/shadow_trade_logger.py --summary   # 14-day review
```

**`execution_engine.py`** — Phase 3
Places Kite orders after Telegram approval. Three modes: disabled → shadow → live.

**`telegram_bot.py`** — Phase 3 (update existing)
Long-polling bot. Listens for APPROVE/REJECT/DEFER taps. Must run as persistent service.
```bash
python control/telegram_bot.py   # runs forever
```

### Kite Scripts

**`kite_token_refresh.py`** — Phase 2
Manual daily token refresh (8:30 AM). Cannot be automated — Zerodha security requirement.
```bash
python kite/kite_token_refresh.py
```

**`kite_reconcile.py`** — Phase 2 / NEW-B (deployed in §5A)
Daily: compares `kite.holdings()` vs `open_positions` → alerts on divergence.
```bash
python kite/kite_reconcile.py
```

**`kite_client.py`** — Phase 2
Kite Connect API wrapper. Imported by other scripts — not run directly.

### History Scripts

**`append_history.py`** — Phase 0 (patch G14 adds regime snapshot)
Daily MSL snapshot → `msl_history`. After G14: also snapshots `market_regime` → `regime_history`.
```bash
python history/append_history.py
python history/append_history.py --date 2026-01-15
```

**`evolution_tracker.py`** — Phase 4 (patched Fix #5, G8/G9/G15)
Sunday: retires stale lessons, analyzes 90-day outcomes, proposes parameter changes → `evolution_proposals`. Never auto-applies.
```bash
python history/evolution_tracker.py
python history/evolution_tracker.py --dry-run
```

**`discovery_engine.py`** — Phase 4 (build in Step 4.2)
Sunday: statistical correlation of `chartink_raw_data` features vs outcomes → `discovery_proposals`. Never auto-applies.
```bash
python history/discovery_engine.py
python history/discovery_engine.py --dry-run
```

### Alert Scripts

**`send_alerts.py`** — Phase 1
Morning brief + evening digest → Telegram. Phase 3+: includes APPROVE/REJECT buttons.
No duplicate alerts — `--position-risk` flag is a recognised no-op (covered by morning brief + sl_monitor + position_event_monitor).
```bash
python alerts/send_alerts.py            # evening digest (structured)
python alerts/send_alerts.py --morning  # morning brief (7 AM)
```

### Utility Scripts

**`data_quality_monitor.py`** — Phase 2 (build in Step 2.5)
10 validation checks after every pipeline → `data_anomalies`. Non-fatal, always last.
```bash
python ingestion/data_quality_monitor.py
```

**`backfill_msl_history.py`** — One-time (already done)
```bash
python scripts/backfill_msl_history.py  # Only if msl_history table was dropped
```

---

## 13. Supabase Tables Reference

| Table | Written By | Read By | Phase | State |
|-------|-----------|---------|-------|-------|
| `chartink_raw_data` | fetch_chartink | compute_indicators, discovery_engine | 0 | ✅ |
| `stock_data_daily` | ingest_bhavcopy, ingest_sheets, compute_indicators | generate_signals, ai_enrich | 0 | ✅ (computed cols 0 until P2) |
| `master_shortlist` | ingest_sheets | generate_signals, ai_enrich, send_alerts | 0 | ✅ |
| `open_positions` | ingest_sheets (P3: Kite sync) | generate_signals, ai_enrich(G18), risk_manager, kite_reconcile | 0 | ✅ ⚠️G18 |
| `closed_positions` | ingest_sheets | ml_provider, post_trade_analysis, evolution_tracker | 0 | ✅ |
| `sector_strength` | ingest_sheets | generate_signals, ai_enrich(G13) | 0 | ✅ ⚠️G13 |
| `industry_strength` | ingest_sheets | generate_signals, ai_enrich(G13) | 0 | ✅ ⚠️G13 |
| `market_regime` | ingest_sheets, ml_regime_classifier(P2) | generate_signals, ai_enrich(G17), risk_manager | 0 | ✅ ⚠️G17 |
| `signal_log` | generate_signals, ai_enrich | send_alerts, ml_provider, evolution_tracker | 0 | ✅ ⚠️G1,G2 |
| `msl_history` | append_history, backfill | ml_provider, evolution_tracker | 0 | ✅ |
| `regime_history` | append_history (after G14) | evolution_tracker (P4) | 0 | ⚠️G14 orphan |
| `event_calendar` | ingest_sheets, ingest_nse_events | generate_signals, ai_enrich(G6) | 0/1 | ✅ ⚠️G6 |
| `lessons` | post_trade_analysis(Fix#2) | ai_router, evolution_tracker | 1 | ✅ ⚠️Fix#2 |
| `nse_holidays` | ingest_sheets | generate_signals | 0 | ✅ |
| `nifty_total_market` | ingest_sheets | compute_indicators | 0 | ✅ |
| `system_config` | manual SQL | every script | 0 | ✅ |
| `strategy_config` | ingest_sheets, evolution_proposals | generate_signals | 0 | ✅ |
| `fii_dii_flow` | ingest_fii_dii(Fix#1) | generate_signals, ai_enrich, send_alerts | 1 | ✅ ⚠️Fix#1 |
| `global_cues` | ingest_global_cues | ai_enrich(G17), send_alerts | 1 | ✅ ⚠️G17,G16 |
| `ai_context` | ai_enrich | send_alerts, frontend | 1 | ✅ |
| `ai_model_performance` | ai_router | evolution_tracker(G15) | 1 | ⚠️G15 written not read |
| `ml_training_log` | ml_provider | frontend Analytics | 1 | ✅ |
| `scanner_signals` | independent_scanner | send_alerts | 1 | ✅ |
| `safety_lists` | ingest_asm_gsm | generate_signals, risk_manager | 2 | 🔲 |
| `data_anomalies` | data_quality_monitor, sl_monitor, kite_reconcile | frontend | 2 | ✅ (new cols from migration) |
| `shadow_trades` | shadow_trade_logger | frontend Analytics | 3 | ✅ (table created in migration) |
| `evolution_proposals` | evolution_tracker(Fix#5) | frontend, you via SQL | 4 | ⚠️Fix#5 bugs |
| `discovery_proposals` | discovery_engine | frontend, you via SQL | 4 | 🔲 |

**Legend:** ✅ Live · ⚠️ Live with gap/bug · 🔲 Not yet built

---

## 14. GitHub Actions Workflows

### `pipeline_daily.yml` — Main Evening Pipeline
**Cron:** `30 12 * * 1-5` (6:00 PM IST Mon–Fri)
**Steps:**
1. Python 3.11 setup + `pip install -r backend/requirements.txt`
2. `playwright install chromium` ← **Required or fetch_chartink fails**
3. `python backend/ingestion/fetch_chartink.py`
4. `python backend/run_pipeline.py`

**Secrets:** `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GOOGLE_SHEET_ID`, `GOOGLE_CREDENTIALS_JSON`, `CHARTINK_EMAIL`, `CHARTINK_PASSWORD`, `TOTAL_CAPITAL`
**Phase 1+ adds:** `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
**Phase 2+ adds:** `KITE_API_KEY`, `KITE_API_SECRET`

---

### `pipeline_morning.yml` — Morning Pre-Market Brief

**Single job, single cron:** `30 1 * * 1-5` (7:00 AM IST Mon–Fri)

**Steps:**
1. `python backend/ingestion/ingest_global_cues.py MORNING` — fetches true US close-to-close prices
2. `python backend/alerts/send_alerts.py --morning` — one consolidated Telegram brief (cues + signals + SL watch + event risk in 4 sections)

`--position-risk` flag removed — event risk is now Section 4 of the morning brief.
Kite reconcile (`kite_reconcile.py`) is Phase 2+ only — add a second job to this file when activating Phase 2 (see §2.3 for the job definition).

---

### `pipeline_intraday.yml` — SL Monitor (NEW — deployed in §5A)
**Cron:** `*/30 3-10 * * 1-5` (every 30 min, 9:15–15:30 IST Mon–Fri)
**Steps:**
1. `python backend/control/sl_monitor.py`

`is_market_open()` guard inside script handles out-of-hours calls cleanly.

**Secrets:** Same as pipeline_daily + `KITE_ACCESS_TOKEN`

---

### `evolution_weekly.yml` — Sunday ML Training
**Cron:** `30 0 * * 0` (6:00 AM IST Sunday)
**Steps:**
1. `python backend/ai/providers/ml_provider.py --train`
2. `python backend/ai/providers/ml_regime_classifier.py --train` (Phase 2+)
3. `python backend/history/evolution_tracker.py` (Phase 4+ only, conditional on `AUTONOMY_PHASE`)
4. `python backend/history/discovery_engine.py` (Phase 4+ only, conditional)

---

## 15. Environment Variables Reference

### Phase 0 (required from Day 1)

| Variable | Where to Get | Notes |
|----------|-------------|-------|
| `SUPABASE_URL` | Supabase project → Settings → API | `https://xxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase project → Settings → API → service_role key | NOT the anon key |
| `GOOGLE_SHEET_ID` | Sheet URL between `/d/` and `/edit` | `1yclJSWpRt...` |
| `GOOGLE_CREDENTIALS_JSON` | Contents of `service_account.json` | Paste entire JSON in GitHub Secret |
| `CHARTINK_EMAIL` | Your Chartink login email | |
| `CHARTINK_PASSWORD` | Your Chartink password | |
| `TOTAL_CAPITAL` | Total trading capital in ₹ | `200000` |

### Phase 1 additions

| Variable | Required For | Where to Get |
|----------|-------------|-------------|
| `TELEGRAM_BOT_TOKEN` | Alerts + Phase 3 bot | @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Alerts + Phase 3 bot | @userinfobot on Telegram |
| `ANTHROPIC_API_KEY` | Claude provider | console.anthropic.com |
| `OPENAI_API_KEY` | GPT provider | platform.openai.com |
| `GEMINI_API_KEY` | Gemini provider | aistudio.google.com |
| `DEEPSEEK_API_KEY` | DeepSeek provider | platform.deepseek.com |
| `GROK_API_KEY` | Grok provider | console.x.ai |
| `AZURE_OPENAI_API_KEY` | Copilot/Azure provider | Azure portal |
| `AZURE_OPENAI_ENDPOINT` | Copilot/Azure provider | Azure portal → your deployment endpoint URL |

### Phase 2 additions

| Variable | Required For |
|----------|-------------|
| `KITE_API_KEY` | Kite Connect live prices + execution |
| `KITE_API_SECRET` | Kite Connect authentication |
| `KITE_ACCESS_TOKEN` | sl_monitor.py (refreshed daily by kite_token_refresh.py) |

---

## 16. Troubleshooting Reference

| Error | Cause | Fix |
|-------|-------|-----|
| `Could not find column 'fii_net_cr'` | FII column mismatch — before Fix #1 | Deploy Fix #1 patch |
| `maximum recursion depth exceeded` | Circular import in post_trade_analysis — before Fix #2 | Deploy Fix #2 patch |
| `EXIT signals not appearing in AI` | ai_enrich loads by score, EXIT score too low — before Fix #3 | Deploy Fix #3 patch |
| `Could not find column 'X'` | Column missing from Supabase | Run ALTER TABLE for that column |
| `403 on Google Sheets` | Service account not shared | Re-share Sheet with service account email as **Editor** |
| `SUPABASE_URL not set` | .env not loaded | Check .env in backend/ directory |
| `insufficient data (13 rows)` | Not enough MSL history | Run `backfill_msl_history.py` |
| `KILL SWITCH ACTIVE` | Kill switch triggered | `python control/kill_switch.py off` |
| `ML model not found` | Not yet trained | Run `ml_provider.py --train` (needs 30+ closed trades) |
| `CSV button never appeared` | Chartink hover not triggering | Check `chartink_hover_debug.png`; set `headless=False` and run manually |
| `industry_strength empty` | Tab not ingested | Run `python ingestion/ingest_sheets.py` |
| `chartink_raw_data 0 rows` | Supabase upsert failed | Check service account key; verify UNIQUE constraint on date+symbol |
| `Kite token expired` | Access token > 24h old | Run `python kite/kite_token_refresh.py` |
| `No JSON object found in AI response` | JSON fences not stripped — before G5 | Apply PATCH G5 to 4 provider files |
| `sl_monitor: no positions found` | open_positions empty or Kite token expired | Check token; check Supabase data |
| `kite_reconcile: kite_unavailable` | Stale/missing Kite token | Run token refresh; reconciler is non-fatal |
| `CAUTION regime not showing penalty` | Fix #4 not yet applied | Apply patch_generate_signals_regime_eap.py edits |
| `compute_indicators: vol_ratio all NULL` | Field name bug — before v4.3 fix | Replace with v4.3 compute_indicators.py (uses `daily_close` not `close`) |
| `compute_indicators: ret_6m all NULL` | Historical close query bug — before v4.3 fix | Replace with v4.3 compute_indicators.py (`fetch_historical_closes` now queries `daily_close`) |
| `generate_shortlist: ImportError AIRouter` | Using pre-v4.3 script with patched ai_router | Replace with v4.3 generate_shortlist.py (uses `raw_completion()`/`is_ai_available()` module functions) |

---

## 17. Repository Structure

```
tradeos-v6/
├── DEPLOYMENT_README.md             ← This file (golden copy)
├── tradeos_connectivity_map.html    ← Architecture reference + Patch Status tab
├── .gitignore
│
├── .github/workflows/
│   ├── pipeline_daily.yml           ← 6 PM IST weekdays (main evening pipeline)
│   ├── pipeline_morning.yml         ← 7 AM IST weekdays — single job: global cues + consolidated morning brief
│   ├── pipeline_intraday.yml        ← Every 30 min market hours (NEW — sl_monitor)
│   └── evolution_weekly.yml         ← Sunday 6 AM IST (ML training + Phase 4 evolution)
│
├── frontend/
│   └── App_v6.jsx                   ← React dashboard
│
└── backend/
    ├── run_pipeline.py              ← Main orchestrator (update step indexes for Phase 2)
    ├── config.py                    ← Env vars, Supabase client, is_kill_switch_active()
    ├── requirements.txt
    ├── .env.example
    │
    ├── db/
    │   ├── schema_v6_base.sql                ← Run FIRST on fresh install
    │   ├── schema_v6_signals.sql             ← Run SECOND
    │   ├── schema_rls.sql                    ← Run LAST
    │   ├── migration_strategic_fixes.sql     ← Patch SQL (already run 03.14.2026)
    │   └── sql_patches_g14_g16.sql           ← regime_history + global_cues cols (already run)
    │
    ├── ingestion/
    │   ├── fetch_chartink.py        ← P0 ✅
    │   ├── ingest_sheets.py         ← P0 ✅ (deprecated P4)
    │   ├── ingest_bhavcopy.py       ← P0 ✅
    │   ├── ingest_fii_dii.py        ← P1 ✅ PATCH Fix#1 applied
    │   ├── ingest_nse_events.py     ← P1 ✅
    │   ├── ingest_global_cues.py    ← P1 ✅ G16 applied · also wired as step 00 in evening pipeline
    │   ├── ingest_asm_gsm.py        ← P1/P2 ✅ BUILT + v4.3 fixed (kill switch, dry-run, stock_data_daily flags)
    │   └── position_event_monitor.py ← P1 ✅ BUILT (standalone — event risk embedded in send_alerts --morning)
    │
    ├── compute/                     ← NEW folder — Phase 2 computation scripts
    │   ├── compute_indicators.py    ← P2 ✅ BUILT + v4.3 fixed (daily_close field, RENAME_MAP, low_30d, all computed cols)
    │   └── data_quality_monitor.py  ← P2 ✅ BUILT (activate as step 99 at Phase 2)
    │
    ├── signals/
    │   ├── generate_signals.py      ← P0 ✅ PATCH Fix#4+#6, G1 applied
    │   └── independent_scanner.py   ← P1 ✅
    │
    ├── ai/
    │   ├── ai_enrich.py             ← P1 ✅ PATCH Fix#3, G3+G5+G6+G13+G17+G18 applied
    │   ├── ai_router.py             ← P1 ✅ PATCHED: raw_completion() + is_ai_available() added
    │   ├── generate_shortlist.py    ← P1 ✅ v4.3 fixed (AIRouter removed, master_shortlist write added)
    │   ├── post_trade_analysis.py   ← P1 ✅ PATCH Fix#2, G2 applied
    │   ├── fallback/
    │   │   ├── news_aggregator.py   ← P1 ✅ Combines scraped sources (budget-exceeded fallback)
    │   │   ├── web_scraper.py       ← P1 ✅ NSE/BSE/Moneycontrol scraper (free, no auth)
    │   │   └── sentiment_scorer.py  ← P1 ✅ Rule-based headline scorer (no LLM)
    │   └── providers/
    │       ├── base_provider.py          ← P1 ✅ PATCHED: ConvictionResult dataclass + BaseProvider ABC
    │       ├── ml_provider.py            ← P1 ✅
    │       ├── ml_regime_classifier.py   ← P2 🔲 NOT YET BUILT — only remaining Phase 2 script
    │       ├── claude_provider.py        ← P1 ✅ G5 patch applied
    │       ├── openai_provider.py        ← P1 ✅ G5 patch applied
    │       ├── gemini_provider.py        ← P1 ✅ G5 patch applied
    │       ├── deepseek_provider.py      ← P1 ✅
    │       ├── grok_provider.py          ← P1 ✅ G5 patch applied
    │       └── copilot_provider.py       ← P1 ✅ Azure OpenAI / Microsoft Copilot provider
    │
    ├── alerts/
    │   └── send_alerts.py           ← P1 ✅
    │
    ├── control/
    │   ├── kill_switch.py           ← P0 ✅
    │   ├── sl_monitor.py            ← P2 ✅ NEW (deployed §5A)
    │   ├── risk_manager.py          ← P3 ✅ Generated
    │   ├── shadow_trade_logger.py   ← P3 ✅ Generated
    │   ├── execution_engine.py      ← P3 ✅ Generated
    │   └── telegram_bot.py          ← P3 ⚠️ Update: add inline buttons
    │
    ├── kite/
    │   ├── kite_token_refresh.py    ← P2 ✅ (manual 8:30 AM daily)
    │   ├── kite_client.py           ← P2 ✅
    │   └── kite_reconcile.py        ← P2 ✅ NEW (deployed §5A)
    │
    ├── history/
    │   ├── append_history.py        ← P0 ✅ PATCH G14 applied (regime_history snapshot)
    │   ├── evolution_tracker.py     ← P4 ✅ FULLY PATCHED Fix#5 + G8+G9+G15 + AIRouter fix
    │   └── discovery_engine.py      ← P4 ✅ BUILT (activate at Phase 4 gate; HTML STEP fields corrected in v4.3)
    │
    └── scripts/
        └── backfill_msl_history.py  ← One-time ✅ Already run
```

**Legend:** ✅ Live/Built · ⚠️ Live with patch needed · 🔲 Not yet built

---

## Appendix: AI Context Assembly — Complete Target State

After all G-patches applied, every AI signal conviction call receives:

| Table | Fields Sent | Why |
|-------|------------|-----|
| `signal_log` | score, strategy, signal_type, regime, fii_flag | Core signal being evaluated |
| `stock_data_daily` | rsi_daily, vol_ratio, delivery_pct, atr_pct, dist_sma50, ret_6m | Entry-time technicals |
| `event_calendar` | event_type, event_date, detail (next 14 days) | Results in 2 days changes conviction entirely |
| `sector_strength` | strength_score, trend, rank | Is the sector accelerating or deteriorating? |
| `industry_strength` | rank, state, avg_rsi | Position within sector |
| `market_regime` | regime, regime_score, breadth_pct | RISK OFF or CAUTION context |
| `global_cues` | gift_nifty_chg_pct, gap_signal, dow_chg_pct, sector_impacts | DOW -2% today = lower conviction |
| `open_positions` | count, sector_exposure, already_held | No 4th banking stock if 3 already held |
| `fii_dii_flow` | net_equity, flag, rolling_5d, rolling_20d | Actual flow data, not just the flag |
| `lessons` | scenario_type, root_cause, corrective_rule, confidence | What similar setups taught us (active only) |

---

*TradeOS v6 · Golden Copy v4.5 · March 2026*
*Google Sheet ID: 1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw*
*Phases: 0 ✅ · 1 ✅ · Patches ✅ (03.14–15.2026) · G1–G18 ✅ · v4.3 fixes ✅ · 2 🔶 (scripts built+fixed, gate not met) · 3 🔲 · 4 🔶 (discovery_engine built, gate not met)*
*Scripts: 39 live/built · 1 to build (ml_regime_classifier.py)*
*Tables: 24 active · 3 pending activation (safety_lists, discovery_proposals, data_anomalies)*
