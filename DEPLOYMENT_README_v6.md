# TradeOS v6 — Master Deployment Guide
**Version 6.0 · April 2026**

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
Yahoo Finance          ──→    [07] ingest_fii_dii
                               [08] ingest_nse_events
                               [09] ingest_asm_gsm
                               [10] compute_indicators
                               [10.5] screen_stocks      ← NEW (Phase 2.5) 
                               [10.6] compute_msl        ← NEW (Phase 2.5)
                               [11] ml_regime_classifier
                               [12] generate_signals           ← FATAL step
                               [13] append_history
                               [14] post_trade_analysis
                               [15] generate_shortlist
                               [16] market_intelligence_engine
                               [17] ai_enrich
                               [18] send_alerts
                               [19] data_quality_monitor

SUPABASE (31 tables): all state, all history, all config lives here
WEEKLY (Sunday 6 AM): ml_provider --train → ml_regime_classifier --train → evolution_tracker
```

**How signals feed swing trades:**
1. `screen_stocks` (NEW) scans all 500 stocks through 9 engines → top 30 to `msl_computed`
2. `compute_msl` (NEW) computes all intelligence fields for MSL symbols → enriched `msl_computed`
3. `generate_signals` classifies every MSL stock into one of 4 signal types
4. `PRE_BREAKOUT_WATCH` = stock coiling, 2–5 sessions before breakout → you position early
5. `BUY_CANDIDATE` = enter now (all gates aligned, price in zone)
6. `ai_enrich` adds HIGH/MEDIUM/LOW conviction via LLM using 10 tables of context
7. `send_alerts` delivers the full picture to Telegram at 6 PM

---

## Current Phase Status

| Phase | Status | What it delivers |
|-------|--------|-----------------|
| Phase 0 | ✅ Complete | Pipeline runs daily. Signals generated. Frontend live. |
| Phase 1 | ✅ Complete | AI conviction, FII data, events, post-trade analysis, Telegram alerts. |
| Phase 2 | 🔶 Scripts built | Computation engine built. Needs code fixes + gate criteria before activation. |
| **Phase 2.5** | 🆕 **Scripts ready** | **Dynamic screener + MSL computation engine. Shadow mode first.** |
| Phase 3 | 🔲 Design ready | Supervised execution via Telegram APPROVE/REJECT. |
| Phase 4 | 🔲 Scripts built | Full autonomy + self-evolving parameters. Gate criteria not met. |

---

## NEW: Phase 2.5 — Dynamic Screener + MSL Computation Engine

This is the most significant architectural upgrade in v6. The Google Sheet MSL tab transitions from being the **source of truth** to being a **manual override layer**.

### What's New

**`screen_stocks.py`** — Dynamic Stock Screener
- Scans all 500 stocks in `stock_data_daily` through 9 strategy engines every day
- **Evolved engines** (from your Sheet): CTL, SBS, TPO, EAP (all upgraded with new signals)
- **New proprietary scanners**: VBD, IAD, RSB, MOM, RVS, SEC
- Selects top 30 symbols with sector diversification (max 5 per sector)
- Multi-engine convergence bonus rewards stocks confirmed by multiple strategies
- Writes to `msl_computed` (shadow) or `master_shortlist` (hybrid/full)

**`compute_msl.py`** — MSL Intelligence Engine
- Runs on the selected symbols from `screen_stocks`
- Recomputes all intelligence fields from actual market data (not Sheet formulas)
- 15 computation functions including regime-aware RSI, BB squeeze, PSAR dual confirm
- New fields: `holding_score`, `ma_alignment_score`, `bb_context`, `vwap_alignment`
- Writes enriched data to `msl_computed` (shadow) or `master_shortlist` (hybrid/full)

### 9 Strategy Engines

| Engine | Type | Description |
|--------|------|-------------|
| CTL | Evolved | Core Trend Leaders: monthly RSI + weekly RSI + ADX + MACD + golden cross |
| SBS | Evolved | Structural Breakout Swing: BB squeeze + delivery trend + resistance proximity |
| TPO | Evolved | Trend Pullback: regime-aware RSI window + MACD turning point detection |
| EAP | Evolved | Event overlay: programmatic calendar + sector rotation events |
| VBD | NEW | Velocity Burst: 3-6% single-session move + 2x volume + institutional delivery |
| IAD | NEW | Institutional Accumulation: high delivery + RS + volume expansion in tight range |
| RSB | NEW | Relative Strength Breakout: RS leader coiling near 30d high |
| MOM | NEW | Momentum Continuation: EXPANSION phase + accelerating + near entry zone |
| RVS | NEW | Reversal Setup: SMA50 bounce + RSI turning from 45-52 (NEUTRAL/TRENDING only) |
| SEC | NEW | Sector Rotation: early movers in freshly leading sectors |

### Transition Plan (3 Phases, Zero Risk)

```
SHADOW MODE (default, run for 2+ weeks)
  screen_stocks → msl_computed only
  compute_msl   → msl_computed only
  master_shortlist: UNTOUCHED (Sheet still drives everything)
  Action: Compare msl_computed vs master_shortlist daily using validation query

HYBRID MODE (after shadow validation)
  screen_stocks → msl_computed + master_shortlist (computed fields only)
  compute_msl   → master_shortlist (overwrites computed fields, preserves Sheet fields)
  Action: Review signal quality for 4+ weeks. Run Telegram comparison.

FULL MODE (after hybrid validation)
  screen_stocks → master_shortlist (screener is source of truth)
  compute_msl   → master_shortlist (all intelligence fields)
  Sheet MSL tab: manual override only (force_include=TRUE to add stock manually)
```

### Deployment Steps for Phase 2.5

**Step 1 — Run SQL migrations:**
```sql
-- File 1: compute_msl_v2_migration.sql (adds new columns to master_shortlist, msl_computed)
-- File 2: screen_stocks_migration.sql (adds screener columns + system_config keys)
-- Run both in Supabase SQL Editor
NOTIFY pgrst, 'reload schema';
```

**Step 2 — Deploy new scripts:**
```bash
cp screen_stocks.py backend/signals/screen_stocks.py
cp compute_msl_v2.py backend/signals/compute_msl.py
git add backend/signals/
git commit -m "feat: screen_stocks + compute_msl v2 - dynamic screener + MSL intelligence"
git push
```

**Step 3 — Add to pipeline (run_pipeline.py):**
```python
# Add after step 10 (compute_indicators), before step 11 (ml_regime_classifier):
("10.5_compute_msl",   step_compute_msl,   False),  # MSL intelligence engine
("10.6_screen_stocks", step_screen_stocks, False),  # Dynamic stock screener
```

**Step 4 — Verify shadow mode (default):**
```sql
-- After next 6 PM run, check msl_computed has data:
SELECT symbol, priority_rank, final_score, engines_list
FROM msl_computed
WHERE date = CURRENT_DATE
ORDER BY priority_rank;

-- Compare screener vs Sheet:
-- Run the comparison query from screen_stocks_migration.sql
```

**Step 5 — When ready to advance:**
```sql
-- Advance screener to hybrid:
UPDATE system_config SET value='hybrid' WHERE key='screener_mode';
-- Advance compute_msl to hybrid:
UPDATE system_config SET value='hybrid' WHERE key='compute_msl_mode';
```

---

## IMMEDIATE ACTIONS — Deploy These Now (Phase 0/1 Bug Fixes)

These fix confirmed bugs in the live codebase.

### Step 1 — SQL migrations
```sql
-- sql_signal_log_market_context.sql
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS india_vix          DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS nifty_5d_chg_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS above_200dma_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS fii_net_20d_ctx    DOUBLE PRECISION;

-- signal_log: filter_reason column (generate_signals v2)
ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS filter_reason TEXT;

NOTIFY pgrst, 'reload schema';
```

### Step 2 — Replace generate_signals.py (v2)
```bash
cp generate_signals_v2.py backend/signals/generate_signals.py
git add backend/signals/generate_signals.py
git commit -m "feat: generate_signals v2 - market context + filter_reason diagnostic"
git push
```

### Step 3 — Replace send_alerts.py (v2, truncation removed)
```bash
cp send_alerts_v2.py backend/alerts/send_alerts.py
git add backend/alerts/send_alerts.py
git commit -m "fix: send_alerts v2 - remove truncation + PRE_BREAKOUT_WATCH section"
git push
```

### Step 4 — Verify deployment
After next 6 PM pipeline:
- `signal_log` latest row should have `india_vix`, `filter_reason` populated
- Telegram message should show full rationale (no `…` truncation)
- WATCH signals in log should show `filter_reason` breakdown

---

## Phase 2 Step Sequence (active when `autonomy_phase = 2`)

| Step | Script | Fatal | Purpose |
|------|--------|-------|---------|
| 01 | `ingest_market_news.py` | No | Scrape NSE/RBI/ET news → `market_news` |
| 02 | `ingest_macro_indicators.py` | No | CPI/WPI/GDP → `macro_indicators` |
| 03 | `ingest_global_cues.py EVENING` | No | Gift Nifty, crude, 10yr → `global_cues` |
| 04 | `fetch_chartink.py` | **YES** | 500 stocks raw → `chartink_raw_data` |
| 05 | `ingest_bhavcopy.py` | No | OHLCV + delivery → `raw_prices` |
| 06 | `ingest_sheets.py` | **YES** | Google Sheet → 14 tables |
| 07 | `ingest_fii_dii.py` | No | FII flows → `fii_dii_flow` |
| 08 | `ingest_nse_events.py` | No | Corporate calendar → `event_calendar` |
| 09 | `ingest_asm_gsm.py` | No | Surveillance lists → `safety_lists` |
| 10 | `compute_indicators.py` | No | 21 renames + 30 computed cols → `stock_data_daily` |
| **10.5** | **`compute_msl.py`** | **No** | **MSL intelligence engine → `msl_computed`** |
| **10.6** | **`screen_stocks.py`** | **No** | **Dynamic screener → `msl_computed`** |
| 11 | `ml_regime_classifier.py --predict` | No | ML predicted regime |
| 12 | `generate_signals.py` | **YES** | 4 signal types → `signal_log` |
| 13 | `append_history.py` | No | MSL + regime snapshots |
| 14 | `post_trade_analysis.py` | No | Outcomes → `lessons` |
| 15 | `generate_shortlist.py` | No | AI top-12 ranking |
| 16 | `market_intelligence_engine.py` | No | News synthesis |
| 17 | `ai_enrich.py` | No | LLM conviction → `ai_context` |
| 18 | `send_alerts.py` | No | Telegram digest |
| 19 | `data_quality_monitor.py` | No | 10 checks → `data_anomalies` |

---

## Future Script Changes (Post Full Transition)

Once `screen_stocks` and `compute_msl` are in full mode, these scripts need updates:

| Script | Change Required | Priority |
|--------|----------------|----------|
| `ingest_sheets.py` | Remove `ingest_master_shortlist()` step (Sheet MSL tab becomes manual override only). Keep all other 13 tabs. | High |
| `generate_signals.py` | Read new fields from `master_shortlist`: `holding_score`, `ma_alignment_score`, `bb_context`, `vwap_alignment`, `momentum_score`, `risk_score`. Use `holding_score` in is_buy_candidate logic. | High |
| `ai_enrich.py` | Pass new compute_msl fields to LLM context: `bb_context`, `vwap_alignment`, `weekly_structure`, `institutional_score`, `breakout_readiness`. This gives the LLM richer setup context. | Medium |
| `generate_shortlist.py` | Use `composite_score` from `msl_computed` as ranking input (replaces `final_score` from Sheet formula). | Medium |
| `send_alerts.py` | Add WATCHING section showing HOLD stocks with `holding_score` + `bb_context`. Add new signal fields to structured alert format. | Medium |
| `append_history.py` | Capture new fields in `msl_history`: `momentum_score`, `institutional_score`, `breakout_readiness`, `risk_score`, `holding_score` for trend analysis. | Low |
| `run_pipeline.py` | Add step_compute_msl and step_screen_stocks to Phase 2 step list (positions 10.5 and 10.6). | High |
| `post_trade_analysis.py` | Include `engines_list` from `msl_computed` in outcome attribution (which engine found the winner?). | Low |

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
```

### 9:15 AM to 3:30 PM — Auto: SL + target monitor (every 30 min)

### 6:00 PM — Evening digest on Telegram (auto)
- Regime header + breadth + FII
- ADVANCE NOTICE: PRE_BREAKOUT_WATCH + STAGED_ENTRY
- ENTRY READY: BUY_CANDIDATE + PRIME_SETUP
- WATCHING: HOT stocks with high holding_score (post full transition)
- Open positions + EXIT signals

---

## Repository Structure

```
tradeos-v6/
├── run_pipeline.py
├── config.py
├── requirements.txt
│
├── .github/workflows/
│   ├── pipeline_daily.yml
│   ├── pipeline_morning.yml
│   ├── pipeline_intraday.yml
│   └── evolution_weekly.yml
│
└── backend/
    ├── ingestion/
    │   ├── fetch_chartink.py         P0 ✅
    │   ├── ingest_bhavcopy.py        P0 ✅
    │   ├── ingest_sheets.py          P0 ✅ (MSL tab deprecated post-full-transition)
    │   ├── ingest_fii_dii.py         P1 ✅
    │   ├── ingest_nse_events.py      P1 ✅
    │   ├── ingest_global_cues.py     P1 ✅
    │   ├── ingest_asm_gsm.py         P2 ✅
    │   ├── ingest_market_news.py     P2 ✅
    │   └── ingest_macro_indicators.py P2 ✅
    │
    ├── compute/
    │   ├── compute_indicators.py     P2 ✅
    │   └── data_quality_monitor.py   P2 ✅
    │
    ├── signals/
    │   ├── generate_signals.py       P0 ✅ → needs update post full-transition
    │   ├── screen_stocks.py          P2.5 🆕 NEW — dynamic stock screener
    │   ├── compute_msl.py            P2.5 🆕 NEW — MSL intelligence engine
    │   └── independent_scanner.py    P1 ✅
    │
    ├── ai/
    │   ├── ai_enrich.py              P1 ✅ → needs update post full-transition
    │   ├── ai_router.py              P1 ✅
    │   ├── generate_shortlist.py     P1 ✅ → needs update post full-transition
    │   ├── post_trade_analysis.py    P1 ✅
    │   ├── market_intelligence_engine.py  P2 ✅
    │   └── providers/
    │       ├── base_provider.py      P1 ✅
    │       ├── ml_provider.py        P1 ✅
    │       ├── ml_regime_classifier.py P2 ✅
    │       └── [6 LLM providers]     P1 ✅
    │
    ├── alerts/
    │   └── send_alerts.py            P1 ✅ → needs WATCHING section post full-transition
    │
    ├── history/
    │   ├── append_history.py         P0 ✅
    │   ├── evolution_tracker.py      P4 ✅
    │   └── discovery_engine.py       P4 ✅
    │
    ├── control/
    │   ├── kill_switch.py            P0 ✅
    │   ├── sl_monitor.py             P2 ✅
    │   ├── position_target_monitor.py P2 ✅
    │   ├── execution_engine.py       P3 skeleton
    │   └── risk_manager.py           P3 skeleton
    │
    ├── kite/
    │   ├── kite_token_refresh.py     P2 ✅
    │   ├── kite_client.py            P2 ✅
    │   └── kite_reconcile.py         P2 ✅
    │
    ├── db/
    │   ├── compute_msl_v2_migration.sql     🆕 NEW
    │   └── screen_stocks_migration.sql      🆕 NEW
    │
    └── models/
        ├── ml_conviction.pkl
        └── ml_regime_model.pkl
```

**Legend:** ✅ Live/built | 🆕 NEW in v6 | skeleton = structure exists, not complete | deprecated = to be removed post full-transition

---

## Kill Switch

```sql
-- Halt entire system immediately
UPDATE system_config SET value = 'true' WHERE key = 'kill_switch_active';
-- Resume
UPDATE system_config SET value = 'false' WHERE key = 'kill_switch_active';
```

---

## Troubleshooting

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| 0 signals generated | `stock_data_daily` or `master_shortlist` empty | Check `fetch_chartink` + `ingest_sheets` in Actions log |
| msl_computed empty | `screen_stocks` failed | Check logs; verify `stock_data_daily` has data for today |
| Same stocks every day | `screener_mode=shadow` (expected) | Advance to hybrid when validated |
| Screener picks wrong sectors | `sector_strength` stale | Verify `sector_strength` table has today's date |
| `compute_msl` RSI threshold wrong | Regime not loading | Check `market_regime` table has today's row |
| AI conviction all NULL | No valid API key | Check env vars |
| Telegram shows no advance notice | `send_alerts` v1 | Deploy send_alerts v2 |
| filter_reason NULL in signal_log | Missing column migration | Run `signal_log filter_reason` ALTER TABLE |

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
GOOGLE_SHEET_ID        Sheet ID
DRY_RUN                Set 'True' to skip all Supabase writes
```
