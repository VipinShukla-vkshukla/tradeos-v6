# TradeOS v6 — Master Deployment Guide
**Version 6.2 · April 2026**

---

## What Is TradeOS v6?

TradeOS v6 is a fully automated swing trading system for Indian equity markets (NSE/Nifty). It runs as a pipeline of Python scripts on GitHub Actions, stores all state in Supabase (PostgreSQL), sends trade alerts via Telegram, and executes orders through Zerodha Kite Connect.

**Objective:** Identify NSE stocks forming swing trade setups 1–5 sessions ahead of the actual move, enter at optimal levels, hold 1–3 weeks, and exit at predefined targets or stop-losses.

**Tech stack:** Python 3.12, Supabase (PostgreSQL), GitHub Actions, Zerodha Kite Connect, Telegram Bot API, scikit-learn (local ML), 6 LLM providers (Claude/OpenAI/Gemini/DeepSeek/Grok/Copilot).

**Google Sheet ID:** `1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw`
**Local codebase:** `C:\Users\vkshu\CRITICAL\Equity Indian Market Framework\tradeos-v6-complete\tradeos-v6`
**Standard git push:** `git add . && git commit -m "describe change" && git push`

---

## System Architecture

```
EXTERNAL SOURCES               PIPELINE (6 PM daily)           OUTPUT
────────────────               ──────────────────────           ──────
Chartink (500 stocks)  ──→    [01] ingest_market_news          Telegram
NSE Bhavcopy (OHLCV)   ──→    [02] ingest_macro_indicators     6 PM: regime + signals
Google Sheet (15 tabs) ──→    [03] ingest_global_cues          7 AM: morning brief
NSE FII/DII flows      ──→    [04] fetch_chartink  ← FATAL     Intraday: SL monitor
NSE Events             ──→    [05] ingest_bhavcopy
NSE ASM/GSM lists      ──→    [06] ingest_sheets   ← FATAL
Yahoo Finance          ──→    [07] ingest_fii_dii
                               [08] ingest_nse_events
                               [09] ingest_asm_gsm
                               [10] compute_indicators
                               [10.4] compute_regime  ← NEW P2 (shadow/full)
                               [10.5] screen_stocks   ← NEW P2.5
                               [10.6] compute_msl     ← NEW P2.5
                               [11] ml_regime_classifier
                               [12] generate_signals  ← FATAL
                               [13] append_history
                               [14] post_trade_analysis
                               [15] generate_shortlist
                               [16] market_intelligence_engine
                               [17] ai_enrich
                               [18] send_alerts
                               [19] data_quality_monitor

SUPABASE (31 tables) · WEEKLY: ml training + evolution_tracker
```

**Critical ordering rule:** Steps 10.4 → 10.5 → 10.6 → 12 must be in this exact sequence.
`compute_regime` classifies regime → `screen_stocks` uses regime context → `compute_msl` uses regime for all thresholds → `generate_signals` reads enriched `master_shortlist`.

---

## Current Phase Status

| Phase | Status | What it delivers |
|-------|--------|-----------------|
| Phase 0 | ✅ Complete | Pipeline runs daily. Signals generated. |
| Phase 1 | ✅ Complete | AI conviction, FII data, events, Telegram alerts. |
| Phase 2 | 🔶 Scripts built | Computation engine. Needs gate criteria. |
| **Phase 2.5** | 🆕 **Scripts ready** | **Dynamic screener + MSL intelligence engine.** |
| Phase 3 | 🔲 Design ready | Supervised execution via Telegram APPROVE/REJECT. |
| Phase 4 | 🔲 Scripts built | Full autonomy + self-evolving parameters. |

---

---

## Phase 2 — Compute Regime Engine (Step 10.4)

### Why This Exists

The Google Sheet formula for regime classification has four fundamental problems: binary text comparisons flip the regime on a single RSI reading, no momentum detection (market can fall 8% over 3 weeks and still show NEUTRAL if above 200DMA), no hysteresis (RISK OFF exits the moment one condition improves), and RISK OFF in reality lasts months, not days. `compute_regime.py` replaces this with an objective, data-driven 5-pillar scoring model.

### 5-State Regime Model

| Regime | Score | Description |
|--------|-------|-------------|
| TRENDING | ≥ 78/100 | All cylinders firing — strong bull market |
| RISK ON | 60–77 | Positive trend with some mixed signals |
| NEUTRAL | 40–59 | Neither bull nor bear — selective positioning |
| RECOVERING | 25–39 + 5 conditions | Coming out of bear — bounce confirmed but not yet neutral |
| RISK OFF | < 40 | Bear conditions — capital preservation only |

### 5-Pillar Scoring (0–100)

| Pillar | Max | What It Measures |
|--------|-----|-----------------|
| Price Structure | 25 | Nifty vs 50DMA / 200DMA, golden cross |
| Breadth | 25 | `above_200dma_pct`, `avg_sector_breadth`, advance/decline ratio |
| Momentum | 20 | Weekly Nifty RSI, 20d return, 5d return |
| Volatility | 15 | India VIX (primary: `macro_indicators`, fallback: `market_regime`) |
| FII Flows | 15 | `fii_net_20d`, `fii_net_5d` (fallback chain: rolling → daily net → flag) |

### Hysteresis Design (Asymmetric)

Downgrades are fast (1–2 days of evidence — capital protection priority). Upgrades are slow (3–5 days — avoids false positives on dead-cat bounces). RECOVERING state has a strict 5-condition gate: must have been in RISK OFF within 10 days AND breadth improving AND VIX declining AND Nifty bounced 3%+ from recent lows.

### Two Modes (compute_regime_mode)

```
shadow  (default) → writes computed_regime + regime_score_computed + regime_score_breakdown JSONB only.
                    market_regime.regime field untouched — Sheet formula still active.
                    Run 2+ weeks to validate agreement with Sheet.

full              → additionally writes regime field + strategy controls (ctl_enabled, max_positions etc).
                    Sheet regime formula ignored permanently.
```

### SQL Migration (run before deploying)
```sql
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS computed_regime text;
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS regime_score_computed numeric;
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS regime_score_breakdown jsonb;
ALTER TABLE market_regime ADD COLUMN IF NOT EXISTS regime_computed_at timestamptz;
NOTIFY pgrst, 'reload schema';
```

### Deployment Steps

**Step 1 — SQL migration:**
```sql
-- Run the four ALTER TABLE statements above
NOTIFY pgrst, 'reload schema';
```

**Step 2 — Deploy script:**
```bash
cp compute_regime.py backend/compute/compute_regime.py
git add backend/compute/compute_regime.py
git commit -m "feat: compute_regime v1.0 — 5-pillar scoring, 5-state model, hysteresis engine"
git push
```

**Step 3 — Add to run_pipeline.py (BEFORE screen_stocks):**
```python
# In Phase 2 all_steps list, AFTER compute_indicators (10), BEFORE screen_stocks (10.5):
(\"10.4_compute_regime\", step_compute_regime, False),  # ← MUST be before screen_stocks
(\"10.5_screen_stocks\",  step_screen_stocks,  False),  # ← Uses regime context
(\"10.6_compute_msl\",    step_compute_msl,    False),  # ← enriches screen_stocks output
```

**Step 4 — Verify (after next 6PM run):**
```sql
-- Check computed_regime is being populated:
SELECT date, regime, computed_regime, regime_score_computed, regime_score_breakdown
FROM market_regime ORDER BY date DESC LIMIT 5;

-- Check agreement rate:
SELECT
  COUNT(*) FILTER (WHERE regime = computed_regime) AS agree,
  COUNT(*) AS total,
  ROUND(COUNT(*) FILTER (WHERE regime = computed_regime) * 100.0 / COUNT(*), 1) AS pct_agree
FROM market_regime WHERE computed_regime IS NOT NULL;
```

**Step 5 — Advance to full mode when validated:**
```sql
UPDATE system_config SET value='full' WHERE key='compute_regime_mode';
```

### Regime Controls Written in Full Mode

```python
REGIME_CONTROLS = {
    \"TRENDING\":   {ctl:✅, sbs:✅, tpo:✅, eap:✅, max_positions:12},
    \"RISK ON\":    {ctl:✅, sbs:✅, tpo:✅, eap:✅, max_positions:10},
    \"NEUTRAL\":    {ctl:✅, sbs:✅, tpo:❌, eap:❌, max_positions:8},
    \"RECOVERING\": {ctl:✅, sbs:❌, tpo:❌, eap:❌, max_positions:6},
    \"RISK OFF\":   {ctl:✅, sbs:✅, tpo:❌, eap:❌, max_positions:6},
}
```

---



### Overview

The Google Sheet MASTER_SHORTLIST tab transitions from **source of truth** to **manual override layer**.

| Component | File | Pipeline Step | Output |
|-----------|------|--------------|--------|
| **Regime Engine** | `compute_regime.py` | **10.4** | `market_regime.computed_regime` (shadow) / `market_regime.regime` (full) |
| Stock Screener | `screen_stocks.py` | 10.5 | `msl_computed` (shadow) / `master_shortlist` (hybrid/full) |
| MSL Intelligence | `compute_msl.py` | 10.6 | enriches symbols from step 10.5 |
| Pattern Scanner | `independent_scanner_v2.py` | replaces `independent_scanner.py` | `scanner_signals` (unchanged — feeds generate_signals cross-ref bonus) |

### Why screen_stocks and independent_scanner BOTH exist

These serve completely different purposes:
- `screen_stocks` → `msl_computed/master_shortlist` (stock **selection**)
- `independent_scanner` → `scanner_signals` (generate_signals **cross-reference bonus**)

`generate_signals._apply_scanner_crossref()` reads `scanner_signals` to award +5 `score_adjusted` to any stock confirmed by both the rule engine AND the scanner. `screen_stocks` never writes to `scanner_signals`. Removing `independent_scanner` silently kills that bonus for all signals.

### 9 Screener Engines (screen_stocks.py)

| Engine | Type | Key Evolution |
|--------|------|--------------|
| CTL | Evolved | +ADX directional confirm + golden cross mandatory + MACD positive |
| SBS | Evolved | BB squeeze replaces consolidation as primary gate |
| TPO | Evolved | Regime-aware RSI window: bull=44-58, bear=38-50 |
| EAP | Evolved | Programmatic calendar overlay with sector rotation |
| VBD | NEW | 3-7% single-session + 2x volume + delivery >45% |
| IAD | NEW | Delivery >60% + volume expanding + consol <8% — fires BEFORE VBD |
| RSB | NEW | RS leader coiling near 30d high |
| MOM | NEW | EXPANSION phase + accelerating + near entry zone |
| RVS | NEW | SMA50 bounce RSI 42-54 — disabled in RISK OFF |
| SEC | NEW | Early movers in freshly leading sectors |

### 15 Intelligence Functions (compute_msl.py)

Critical design: `get_rsi_extended_threshold()` is shared by all functions. RSI 85 in TRENDING+ADX40 = extended threshold 87 (continuation signal). RSI 85 in NEUTRAL+ADX15 = extended threshold 72 (exhaustion). Static thresholds are the primary source of false signals in the Sheet formula approach.

**Key new outputs (not in Sheet at all):**
- `holding_score` — 0-100: should I **stay** in this? Different from `validity_score` (should I enter?)
- `bb_context` — BB squeeze/position: SQUEEZE_BULLISH = coiling, RIDING_UPPER = strong trend (not overbought)
- `vwap_alignment` — multi-timeframe institutional cost basis: ABOVE_ALL = all longs profitable
- `ma_alignment_score` — 0-6: full EMA10/20/50 + SMA20/50/200 stack
- `institutional_score` — 0-100: delivery + RS + volume expansion + VWAP
- `breakout_readiness` — 0-100: coiling tightness + approaching resistance
- `risk_score` — 0-100: penalises final_score for overextension

**Critical schema note:** `composite_score` (from screen_stocks) and `final_score` (from compute_msl) are **two separate columns** in `msl_computed`. They are different computations. Never confuse them.

### 3-Mode Transition (Zero Risk)

```
shadow  (default) → writes to msl_computed only. master_shortlist untouched.
                    Run for 2+ weeks. Use validation query to compare.

hybrid            → writes computed fields to master_shortlist.
                    Sheet identity + ai_* fields preserved (PRESERVE_FIELDS).
                    Run for 4+ weeks. Compare signal quality.

full              → master_shortlist is source of truth.
                    Sheet MSL tab = manual override (force_include=TRUE) only.
```

### Deployment Steps

**Step 1 — SQL migrations (run both, in this order):**
```sql
-- File 1: compute_msl_v2_migration_FIXED.sql  ← USE THIS, not the original
-- File 2: screen_stocks_migration.sql
NOTIFY pgrst, 'reload schema';
```

**Step 2 — Deploy scripts:**
```bash
cp screen_stocks.py              backend/signals/screen_stocks.py
cp compute_msl_v2_fixed.py       backend/signals/compute_msl.py
cp independent_scanner_v2.py     backend/signals/independent_scanner.py
git add backend/signals/
git commit -m "feat: P2.5 screen_stocks + compute_msl v2 + independent_scanner v2"
git push
```

**Step 3 — Add to run_pipeline.py:**
```python
# In Phase 2 all_steps list, AFTER compute_indicators (10), BEFORE ml_regime_classifier (11):
("10.4_compute_regime", step_compute_regime, False),  # ← MUST be before screen_stocks
("10.5_screen_stocks",  step_screen_stocks,  False),  # ← MUST be before compute_msl
("10.6_compute_msl",    step_compute_msl,    False),  # ← enriches screen_stocks output
```

**Step 4 — Verify (after next 6PM run):**
```sql
-- Both tables should have data:
SELECT 'msl_computed' AS tbl, COUNT(*) AS rows, MAX(date) AS latest FROM msl_computed
UNION ALL
SELECT 'master_shortlist', COUNT(*), MAX(date) FROM master_shortlist WHERE date=CURRENT_DATE;

-- Verify composite_score ≠ final_score (different values = working correctly):
SELECT symbol, composite_score, final_score, engines_list, momentum_state
FROM msl_computed WHERE date=CURRENT_DATE ORDER BY composite_score DESC LIMIT 10;
```

**Step 5 — Advance modes when ready:**
```sql
-- Both scripts advance together:
UPDATE system_config SET value='hybrid' WHERE key IN ('screener_mode','compute_msl_mode');
-- After 4+ weeks:
UPDATE system_config SET value='full' WHERE key IN ('screener_mode','compute_msl_mode');
```

---

## Immediate Bug Fixes (Deploy Now)

### 1 — SQL migrations for generate_signals v2
```sql
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS india_vix          DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS nifty_5d_chg_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS above_200dma_pct   DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS fii_net_20d_ctx    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS filter_reason      TEXT;
NOTIFY pgrst, 'reload schema';
```

### 2 — Replace generate_signals.py and send_alerts.py
```bash
cp generate_signals_v2.py backend/signals/generate_signals.py
cp send_alerts_v2.py      backend/alerts/send_alerts.py
git add backend/signals/generate_signals.py backend/alerts/send_alerts.py
git commit -m "fix: generate_signals v2 + send_alerts v2 (truncation removed)"
git push
```

---

## Dependent Script Changes After Full Transition

Once `screener_mode=full` and `compute_msl_mode=full`, these scripts need updates. They currently work correctly — these changes make them **better**, not fix breakage.

### Priority: HIGH — Change Before Full Transition

**`run_pipeline.py`**
- Add `step_compute_regime` at position 10.4, `step_screen_stocks` at 10.5, and `step_compute_msl` at 10.6
- Confirm ordering: compute_regime → screen_stocks → compute_msl (strict sequence)
```python
("10.4_compute_regime", step_compute_regime, False),  # ← MUST be before screen_stocks
("10.5_screen_stocks",  step_screen_stocks,  False),  # ← MUST be before compute_msl
("10.6_compute_msl",    step_compute_msl,    False),
```

**`ingest_sheets.py`**
- Remove `ingest_master_shortlist()` from the steps list in `main()`
- Keep all other 14 tabs — only the MASTER_SHORTLIST tab ingestion is deprecated
- Retain `force_include` support: if Sheet MSL tab has a symbol marked `force_include=YES`, write that flag to `master_shortlist` without writing the other computed fields
- Change: `steps` list remove `("master_shortlist", ingest_master_shortlist)`
- Keep: all sector, regime, events, FII, positions, lessons ingestion unchanged

### Priority: MEDIUM — Change Within 4 Weeks of Full Transition

**`generate_signals.py`**
- Read new `master_shortlist` fields for richer signal classification:
```python
# In the signal dict, add from msl_row (now written by compute_msl):
"holding_score":      msl_row.get("holding_score"),
"bb_context":         msl_row.get("bb_context"),
"vwap_alignment":     msl_row.get("vwap_alignment"),
"weekly_structure":   msl_row.get("weekly_structure"),
"momentum_score":     msl_row.get("momentum_score"),
"institutional_score":msl_row.get("institutional_score"),
"breakout_readiness": msl_row.get("breakout_readiness"),
"risk_score":         msl_row.get("risk_score"),
```
- Use `holding_score` in `is_buy_candidate()` for held positions: if `in_position=True` and `holding_score < 35`, set `signal_type = "EXIT"` as an additional exit trigger
- These fields now flow from `master_shortlist` (written by compute_msl) into `signal_log` for downstream use by send_alerts and ai_enrich

**`ai_enrich.py`**
- The new compute_msl fields in `master_shortlist` are automatically available since `ai_enrich` already reads `master_shortlist['all MSL fields']`
- **Enhancement**: add the new fields explicitly to the AI prompt context dict:
```python
# In the prompt assembly (stock_data dict):
"bb_context": msl.get("bb_context"),           # "SQUEEZE_BULLISH" = coiling
"vwap_alignment": msl.get("vwap_alignment"),   # "ABOVE_ALL" = institutions profitable
"weekly_structure": msl.get("weekly_structure"),# "STRONG" = textbook uptrend
"holding_score": msl.get("holding_score"),     # 0-100: trend intact for held positions
"institutional_score": msl.get("institutional_score"),
"breakout_readiness": msl.get("breakout_readiness"),
```
- This gives AI richer context without changing the JSON output schema — no provider changes needed

**`send_alerts.py`**
- Add a `🔭 WATCHING` section in `build_structured()` and `build_morning()` showing HOLD/WATCH signals with `holding_score` and `bb_context`:
```python
watches = [s for s in signals if s["signal_type"] in {"WATCH", "HOLD"}]
if watches:
    lines.append(f"<b>👁 WATCHING ({len(watches)})</b>")
    for s in sorted(watches, key=lambda x: x.get("score",0), reverse=True)[:6]:
        bb = s.get("bb_context","")
        hold = s.get("holding_score","")
        lines.append(
            f"  <b>{s['symbol']}</b> [{s.get('strategy','?')}] "
            f"Score:{s.get('score',0):.0f}"
            + (f"  BB:{bb}" if bb else "")
            + (f"  Hold:{hold:.0f}" if hold else "")
        )
```

### Priority: LOW — Change Within 8 Weeks of Full Transition

**`append_history.py`**
- Add new compute_msl fields to `msl_history` snapshot for trend analysis:
```python
# Add to the snapshot row in _snapshot_msl():
"momentum_score":      msl_row.get("momentum_score"),
"institutional_score": msl_row.get("institutional_score"),
"breakout_readiness":  msl_row.get("breakout_readiness"),
"risk_score":          msl_row.get("risk_score"),
"holding_score":       msl_row.get("holding_score"),
"bb_context":          msl_row.get("bb_context"),
"vwap_alignment":      msl_row.get("vwap_alignment"),
```
- Also add the corresponding columns to `msl_history` table via SQL migration
- Enables `compute_msl.compute_msl_history_context()` to use these for trend detection over 60-day lookback

**`generate_shortlist.py`**
- Use `composite_score` from `msl_computed` (screener aggregate) as primary ranking input instead of `final_score` from Sheet formula
- Add `engines_list` to the AI prompt so AI knows which engines validated each stock:
```python
# In the prompt for each stock:
f"Engines: {msl.get('engines_list','unknown')} | "
f"Convergence: {msl.get('convergence_pts',0):.0f}pts bonus"
```
- Multi-engine convergence (e.g. CTL+IAD+MOM) is strong signal quality evidence

**`post_trade_analysis.py`**
- Add `engines_list` to outcome attribution logging:
```python
# When writing lesson:
"source": f"AI:{provider} | engines:{signal.get('engines_list','?')}",
```
- This lets `evolution_tracker` analyse which engine combinations produce winners vs losers

**`ml_training_log` / `ml_provider.py`**
- Add new compute_msl fields as ML features for the conviction model retraining:
  - `holding_score`, `institutional_score`, `breakout_readiness`, `risk_score`
  - `bb_context` (encoded: SQUEEZE_BULLISH=3, RIDING_UPPER=2, UPPER_HALF=1, MIDDLE=0, NEAR_LOWER=-1)
  - `vwap_alignment` (encoded: ABOVE_ALL=2, ABOVE_20D=1, MIXED=0, BELOW_ALL=-1)
  - `weekly_structure` (encoded: STRONG=2, CONSOLIDATING=1, CAUTION=0, WEAK=-1)
- These 7 new features bring the model from 26 to 33 features
- Requires 90+ closed trades with the new fields populated before retraining

---

## Daily Operating Procedure

### 7:00 AM — Morning brief (auto)
- Section 0: Data quality alerts (ERROR only)
- Section 1: Global cues (Gift Nifty, DOW, crude, 10yr, silver, USD/INR)
- Section 2: Advance notice (PRE_BREAKOUT_WATCH — 2-5 sessions ahead)
- Section 3: Entry-ready with entry zones + AI conviction
- Section 4: Open positions SL watch + event risk (5-day)

### 8:30 AM — Kite token refresh (Phase 2+, manual)
```bash
python backend/kite/kite_token_refresh.py
```

### 9:15–3:30 PM — SL + target monitor (every 30 min, auto)

### 6:00 PM — Evening digest (auto)
- Regime header + breadth + FII
- `10.4 compute_regime` → scores 5 pillars → computes `computed_regime` (shadow) or updates `regime` (full)
- ADVANCE NOTICE: PRE_BREAKOUT_WATCH + STAGED_ENTRY
- ENTRY READY: BUY_CANDIDATE + PRIME_SETUP
- WATCHING: HOT stocks with holding_score context (post full transition)
- Open positions + EXIT signals

### Sunday 6 AM — ML training (auto)
- W1: `ml_provider.py --train` (needs ≥90 closed trades)
- W2: `ml_regime_classifier.py --train` (needs ≥30 regime_history rows)
- W3: `evolution_tracker.py` (proposals created, you approve via SQL)

---

## Kill Switch

```sql
-- Halt immediately:
UPDATE system_config SET value='true' WHERE key='kill_switch_active';
-- Resume:
UPDATE system_config SET value='false' WHERE key='kill_switch_active';
```

---

## Troubleshooting

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| computed_regime NULL after deploy | compute_regime SQL migration not run | Run 4x ALTER TABLE + NOTIFY pgrst migration |
| computed_regime diverges from Sheet regime | Expected in shadow mode — working correctly | Compare scores; if agreement <70% after 10 days, review pillar thresholds |
| score_breakdown always 0/100 for FII pillar | fii_net_20d / fii_net_5d NULL in fii_dii_flow | Verify ingest_fii_dii patch deployed; check fii_dii_flow has recent rows |
| msl_computed empty | screen_stocks failed | Check logs; verify stock_data_daily has today's data |
| composite_score populated, final_score NULL | compute_msl not yet run (or wrong order) | Verify 10.5 before 10.6 in run_pipeline.py |
| final_score = composite_score (same value) | Wrong migration — old single column | Run compute_msl_v2_migration_FIXED.sql |
| Same stocks every day in shadow | Expected — screener running correctly | Advance to hybrid when shadow validated |
| Screener misses obvious stocks | force_include=FALSE, score below threshold | SET force_include=TRUE in master_shortlist |
| holding_score NULL in signals | generate_signals not yet reading new fields | Apply generate_signals.py update (Priority: MEDIUM) |
| AI conviction unchanged despite new fields | ai_enrich not explicitly passing new fields | Apply ai_enrich.py update (Priority: MEDIUM) |
| 0 signals generated | master_shortlist or stock_data_daily empty | Check fetch_chartink + ingest_sheets in Actions log |
| filter_reason NULL in signal_log | Missing column migration | Run `ALTER TABLE signal_log ADD COLUMN filter_reason TEXT` |
| Telegram no advance notice | send_alerts v1 still deployed | Deploy send_alerts v2 |

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

---

## Repository Structure

```
tradeos-v6/
├── run_pipeline.py              ← Add steps 10.5 + 10.6
├── config.py
├── requirements.txt
│
└── backend/
    ├── ingestion/
    │   ├── fetch_chartink.py         P0 ✅ FATAL
    │   ├── ingest_bhavcopy.py        P0 ✅
    │   ├── ingest_sheets.py          P0 ✅ FATAL → remove ingest_master_shortlist() post-full
    │   ├── ingest_fii_dii.py         P1 ✅
    │   ├── ingest_nse_events.py      P1 ✅
    │   ├── ingest_global_cues.py     P1 ✅
    │   ├── ingest_asm_gsm.py         P2 ✅
    │   ├── ingest_market_news.py     P2 ✅
    │   └── ingest_macro_indicators.py P2 ✅
    │
    ├── compute/
    │   ├── compute_indicators.py     P2 ✅
    │   ├── compute_regime.py         P2 🆕 step 10.4 — 5-pillar regime scoring engine
    │   └── data_quality_monitor.py   P2 ✅
    │
    ├── signals/
    │   ├── generate_signals.py       P0 ✅ → add holding_score + new fields (MEDIUM priority)
    │   ├── screen_stocks.py          P2.5 🆕 step 10.5 — 9-engine screener
    │   ├── compute_msl.py            P2.5 🆕 step 10.6 — 15 intelligence functions
    │   └── independent_scanner.py    P1 ✅ v2 upgraded — 5 evolved patterns
    │
    ├── ai/
    │   ├── ai_enrich.py              P1 ✅ → add new fields to prompt (MEDIUM priority)
    │   ├── ai_router.py              P1 ✅
    │   ├── generate_shortlist.py     P1 ✅ → use composite_score + engines_list (LOW)
    │   ├── post_trade_analysis.py    P1 ✅ → add engines_list to attribution (LOW)
    │   ├── market_intelligence_engine.py P2 ✅
    │   └── providers/
    │       ├── ml_provider.py        P1 ✅ → add 7 new features post full-transition (LOW)
    │       ├── ml_regime_classifier.py P2 ✅
    │       └── [6 LLM providers]     P1 ✅
    │
    ├── alerts/
    │   └── send_alerts.py            P1 ✅ → add WATCHING section (MEDIUM priority)
    │
    ├── history/
    │   ├── append_history.py         P0 ✅ → add compute_msl fields to msl_history (LOW)
    │   ├── evolution_tracker.py      P4 ✅
    │   └── discovery_engine.py       P4 ✅
    │
    └── db/
        ├── compute_msl_v2_migration_FIXED.sql   🆕 USE THIS (replaces original)
        └── screen_stocks_migration.sql           🆕 NEW
```

**Change priority legend:**
- 🆕 New — deploy now
- MEDIUM — change within 4 weeks of full transition (improves quality)
- LOW — change within 8 weeks of full transition (enhances ML + history)
- ✅ → means currently working, change is an enhancement not a fix
