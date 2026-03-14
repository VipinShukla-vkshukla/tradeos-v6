# TradeOS v6 — Deployment Guide v3.0
**Status: Phase 1 Complete · Gaps G1–G18 Identified · Phase 2 Planning**
*Supersedes v2.0 · March 2026*

---

## ═══════════════════════════════════════════════════
## PHASE 0 + PHASE 1 CORRECTIONS — APPLY IMMEDIATELY
## ═══════════════════════════════════════════════════

These are surgical patches to already-deployed scripts. Each entry states the
exact file, the exact location, and the exact replacement. Apply in order —
G1 and G2 first as they unblock the ML feedback loop.

---

### PATCH 01 — G1: Add 9 ML Feature Columns to signal_log write
**File:** `backend/signals/generate_signals.py`
**Where:** Find the `sig = { ... }` dictionary where signal rows are constructed,
just before `sb.table("signal_log").upsert(sig ...)`. Add the 9 keys below.

```python
# ── REPLACE THIS (existing sig dict, partial example): ──────────────────────
sig = {
    "date":       str(today),
    "symbol":     symbol,
    "score":      final_score,
    "strategy":   strategy,
    # ... existing fields ...
}

# ── WITH THIS (add the 9 ML feature keys): ──────────────────────────────────
sig = {
    "date":       str(today),
    "symbol":     symbol,
    "score":      final_score,
    "strategy":   strategy,
    # ... all existing fields unchanged ...

    # G1 FIX — ML feature columns (values already computed above in the function)
    "rsi_daily":     float(stock.get("rsi_daily")    or 0),
    "rsi_weekly":    float(stock.get("rsi_weekly")   or 0),
    "adx":           float(stock.get("adx_14")       or 0),
    "vol_ratio":     float(stock.get("vol_ratio")    or 0),
    "delivery_pct":  float(stock.get("delivery_pct") or 0),
    "atr_pct":       float(stock.get("atr_pct")      or 0),
    "ret_6m":        float(stock.get("ret_6m")       or 0),
    "dist_sma50":    float(stock.get("dist_sma50")   or 0),
    "days_in_list":  int(stock.get("days_in_list")   or 0),
}
```

**SQL to run first (Supabase SQL editor):**
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

---

### PATCH 02 — G2: Write outcome_pnl_pct Back to signal_log
**File:** `backend/ai/post_trade_analysis.py`
**Where:** Find the signal_log upsert block (around line 391). It updates the
signal row when a trade is closed. Add `outcome_pnl_pct` to that dict.

```python
# ── REPLACE THIS: ────────────────────────────────────────────────────────────
sb.table("signal_log").update({
    "outcome": outcome_label,
    # ... existing fields ...
}).eq("id", signal_id).execute()

# ── WITH THIS: ───────────────────────────────────────────────────────────────
sb.table("signal_log").update({
    "outcome":         outcome_label,
    "outcome_pnl_pct": round(pnl_pct, 4),   # G2 FIX — links outcome to entry conditions
    # ... all existing fields unchanged ...
}).eq("id", signal_id).execute()
```

---

### PATCH 03 — G3: DeepSeek Empty JSON Guard
**File:** `backend/ai/ai_enrich.py`
**Where:** In the prompt-building function, before constructing the AI prompt string.

```python
# ── ADD THIS BLOCK before building the prompt string: ────────────────────────
# G3 FIX — warn AI when computed indicators are not yet available (Phase 2)
computed_cols = [vol_ratio, adx, dist_sma50, ret_6m, atr_pct]
computed_missing = all(v is None or float(v or 0) == 0 for v in computed_cols)
computed_note = (
    "\nNOTE: Computed technical indicators (vol_ratio, adx, dist_sma50, ret_6m) "
    "are not yet populated (compute_indicators.py Phase 2 not deployed). "
    "Base conviction on RSI, sector context, FII data, and event calendar only. "
    "Do not return empty JSON — use available fields."
) if computed_missing else ""
# Then include computed_note in the prompt template: f"...{computed_note}..."
```

---

### PATCH 04 — G5: JSON Fence Strip — 4 AI Provider Files
**Files:** `backend/ai/providers/claude_provider.py`,
           `backend/ai/providers/openai_provider.py`,
           `backend/ai/providers/gemini_provider.py`,
           `backend/ai/providers/grok_provider.py`
**Where:** In each file, find where the raw API response string is parsed as JSON.

```python
# ── REPLACE THIS in all 4 files: ─────────────────────────────────────────────
data = json.loads(raw)

# ── WITH THIS (add `import re` at top of file if not present): ───────────────
import re
json_match = re.search(r'\{.*\}', raw, re.DOTALL)
if not json_match:
    raise ValueError(f"No JSON object found in AI response: {raw[:200]}")
data = json.loads(json_match.group())
```

---

### PATCH 05 — G6: Add event_calendar to ai_enrich.py Context
**File:** `backend/ai/ai_enrich.py`
**Where:** In the function that builds the context dict for a stock, after fetching
stock data and before constructing the AI prompt. Add after existing fetches.

```python
# ── ADD THIS BLOCK (G6 FIX): ─────────────────────────────────────────────────
from datetime import datetime, timedelta
today_date = datetime.now(IST).date()
lookahead  = today_date + timedelta(days=14)

events = sb.table("event_calendar") \
    .select("event_type, event_date, detail") \
    .eq("symbol", symbol) \
    .gte("event_date", str(today_date)) \
    .lte("event_date", str(lookahead)) \
    .order("event_date") \
    .limit(5).execute().data

ai_context["upcoming_events"] = [
    f"{e['event_type']} on {e['event_date']}"
    + (f": {e['detail']}" if e.get("detail") else "")
    for e in events
] if events else ["No corporate events in next 14 days"]
```

---

### PATCH 06 — G13: Add sector_strength + industry_strength to ai_enrich.py
**File:** `backend/ai/ai_enrich.py`
**Where:** Same context-building function, after PATCH 05 block.

```python
# ── ADD THIS BLOCK (G13 FIX): ────────────────────────────────────────────────
sector   = stock.get("sector",   "")
industry = stock.get("industry", "")

sector_row = sb.table("sector_strength") \
    .select("sector, strength_score, trend, rank") \
    .eq("sector", sector).limit(1).execute().data
industry_row = sb.table("industry_strength") \
    .select("industry, rank, state, avg_rsi") \
    .eq("industry", industry).limit(1).execute().data

ai_context["sector_context"] = sector_row[0] if sector_row else {
    "note": f"No sector_strength data for {sector}"
}
ai_context["industry_context"] = industry_row[0] if industry_row else {
    "note": f"No industry_strength data for {industry}"
}
```

---

### PATCH 07 — G17 (NEW CRITICAL): Add market_regime to ai_enrich.py Context
**File:** `backend/ai/ai_enrich.py`
**Where:** Same context-building function.

```python
# ── ADD THIS BLOCK (G17 FIX — NEW): ─────────────────────────────────────────
regime_row = sb.table("market_regime") \
    .select("regime, regime_score, breadth_pct, advance_decline") \
    .order("date", desc=True).limit(1).execute().data

ai_context["market_regime"] = regime_row[0] if regime_row else {"regime": "UNKNOWN"}

# Also pass current global cues (today morning's data)
cues = sb.table("global_cues") \
    .select("gift_nifty_chg_pct, gap_signal, brent_chg_pct, usd_inr_chg_pct, "
            "us_dow_chg_pct, us_nasdaq_chg_pct, sector_impacts") \
    .order("date", desc=True).limit(1).execute().data

ai_context["global_cues"] = cues[0] if cues else {"note": "No global cues today"}
```

---

### PATCH 08 — G18 (NEW CRITICAL): Add open_positions to ai_enrich.py Context
**File:** `backend/ai/ai_enrich.py`
**Where:** Same context-building function.

```python
# ── ADD THIS BLOCK (G18 FIX — NEW): ─────────────────────────────────────────
positions = sb.table("open_positions") \
    .select("symbol, sector, strategy, invested_value, pnl_pct, lifecycle") \
    .execute().data

position_count  = len(positions)
sectors_held    = {}
strategies_held = {}
for p in positions:
    sec  = p.get("sector",   "Unknown")
    strat = p.get("strategy", "Unknown")
    sectors_held[sec]     = sectors_held.get(sec, 0) + 1
    strategies_held[strat] = strategies_held.get(strat, 0) + 1

# Check if this stock is already held
already_held = any(p["symbol"] == symbol for p in positions)
sector_count = sectors_held.get(stock.get("sector", ""), 0)

ai_context["portfolio"] = {
    "total_open":     position_count,
    "already_held":   already_held,
    "sector_exposure": sectors_held,
    "strategy_exposure": strategies_held,
    "this_sector_count": sector_count,
    "note": (
        f"Already holding {symbol} — consider HOLD not new entry."
        if already_held
        else f"{sector_count} positions already in {stock.get('sector','')} sector."
    )
}
```

---

### PATCH 09 — G8: Fix evolution_tracker.py Kill Switch (v5 → v6)
**File:** `backend/history/evolution_tracker.py`
**Where:** Top of file imports section + top of `main()` function.

```python
# ── REPLACE THIS (v5 pattern): ───────────────────────────────────────────────
from config import ..., check_kill_switch, ...
# and in main():
check_kill_switch()

# ── WITH THIS (v6 pattern): ──────────────────────────────────────────────────
from config import ..., is_kill_switch_active, ...
# and in main():
if is_kill_switch_active():
    logger.warning("Kill switch active — evolution_tracker skipped")
    return {"status": "skipped", "reason": "kill_switch"}
```

---

### PATCH 10 — G9: Replace evolution_tracker.py Bare Lessons Fetch
**File:** `backend/history/evolution_tracker.py`
**Where:** Find `get_lessons_summary()` or equivalent function that fetches lessons.
Replace the entire lessons fetch + format block.

```python
# ── REPLACE existing lessons fetch with this rich-context version: ────────────
def get_lessons_rich_context(sb) -> str:
    """
    Fetch lessons filtered by confidence and is_active.
    Deduplicate by scenario_type (max 2 per type, highest confidence first).
    Build structured context string for AI evolution prompt.
    Requires G7 SQL migration (confidence, is_active columns) to be applied.
    """
    rows = sb.table("lessons") \
        .select("scenario_type, trigger_event, scenario_context, what_failed, "
                "root_cause, corrective_rule, confidence, times_applied, "
                "times_worked, source, linked_symbols") \
        .eq("is_active", True) \
        .gte("confidence", 0.5) \
        .order("confidence", desc=True) \
        .limit(40).execute().data

    if not rows:
        return "No active lessons with confidence >= 0.5"

    # Deduplicate: max 2 per scenario_type
    seen: dict[str, int] = {}
    filtered = []
    for r in rows:
        stype = r.get("scenario_type", "GENERAL")
        if seen.get(stype, 0) < 2:
            filtered.append(r)
            seen[stype] = seen.get(stype, 0) + 1

    lines = []
    for r in filtered:
        eff = ""
        if r.get("times_applied") and r["times_applied"] > 0:
            rate = (r.get("times_worked") or 0) / r["times_applied"]
            eff = f" | Effectiveness: {rate:.0%} ({r['times_applied']} applied)"
        lines.append(
            f"[{r['scenario_type']} | conf={r.get('confidence',0.5):.1f}"
            f" | src={r.get('source','?')}{eff}]\n"
            f"  Context: {r.get('scenario_context','')}\n"
            f"  What failed: {r.get('what_failed','')}\n"
            f"  Root cause: {r.get('root_cause','')}\n"
            f"  Rule: {r.get('corrective_rule','')}"
        )
    return "\n\n".join(lines)
```

---

### PATCH 11 — G7: SQL for lessons Quality Columns
**Run in Supabase SQL editor:**
```sql
ALTER TABLE public.lessons
  ADD COLUMN IF NOT EXISTS confidence     NUMERIC  DEFAULT 0.5,
  ADD COLUMN IF NOT EXISTS times_applied  INT      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS times_worked   INT      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS is_active      BOOLEAN  DEFAULT true,
  ADD COLUMN IF NOT EXISTS linked_symbols TEXT[]   DEFAULT '{}';

UPDATE public.lessons SET confidence = 0.7 WHERE source = 'MANUAL';
UPDATE public.lessons SET confidence = 0.5 WHERE source LIKE 'AI:%' OR source IS NULL;
NOTIFY pgrst, 'reload schema';
```

---

### PATCH 12 — G4: Persist ai_strategy_validation to signal_log
**File:** `backend/ai/ai_enrich.py`
**Where:** After receiving the AI conviction result, in the signal_log update dict.

```python
# ── ADD to signal_log update dict (G4 FIX): ──────────────────────────────────
"ai_strategy_validation": conviction_result.to_dict().get("ai_strategy_validation"),
```

**SQL:**
```sql
ALTER TABLE signal_log ADD COLUMN IF NOT EXISTS ai_strategy_validation TEXT;
NOTIFY pgrst, 'reload schema';
```

---

### PATCH 13 — G14: Wire regime_history in append_history.py
**File:** `backend/history/append_history.py`
**Where:** At the end of the `main()` function, after existing history snapshot logic.

```python
# ── ADD THIS FUNCTION + CALL (G14 FIX): ──────────────────────────────────────
def snapshot_regime(sb, today: str):
    """Snapshot today's market_regime into regime_history for Phase 4 drift analysis."""
    try:
        regime = sb.table("market_regime") \
            .select("*").order("date", desc=True).limit(1).execute().data
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

### PATCH 14 — G16: Fix ingest_global_cues.py for S&P500 + change %
**File:** `backend/ingestion/ingest_global_cues.py`
**Where:** In the data-fetching section and the row-construction dict.

```python
# ── ADD to data fetch section: ────────────────────────────────────────────────
# Fetch S&P500 (yfinance: "^GSPC", or use existing Yahoo Finance session)
sp500_close   = float(yf.Ticker("^GSPC").fast_info.get("lastPrice", 0) or 0)
sp500_prev    = float(yf.Ticker("^GSPC").fast_info.get("previousClose", 0) or 0)
sp500_chg_pct = round((sp500_close - sp500_prev) / sp500_prev * 100, 4) if sp500_prev else None

# For DOW and NQ already fetched, add change % computation:
dow_prev     = float(yf.Ticker("^DJI").fast_info.get("previousClose", 0) or 0)
nasdaq_prev  = float(yf.Ticker("^IXIC").fast_info.get("previousClose", 0) or 0)
dow_chg_pct  = round((us_dow_close - dow_prev) / dow_prev * 100, 4) if dow_prev else None
nq_chg_pct   = round((us_nasdaq_close - nasdaq_prev) / nasdaq_prev * 100, 4) if nasdaq_prev else None

# ── ADD to row dict: ──────────────────────────────────────────────────────────
"us_dow_chg_pct":    dow_chg_pct,
"us_nasdaq_chg_pct": nq_chg_pct,
"sp500_close":       sp500_close,
"sp500_chg_pct":     sp500_chg_pct,
```

**SQL (already applied — confirm in Supabase):**
```sql
ALTER TABLE public.global_cues
  ADD COLUMN IF NOT EXISTS us_dow_chg_pct    NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS us_nasdaq_chg_pct NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_close       NUMERIC NULL,
  ADD COLUMN IF NOT EXISTS sp500_chg_pct     NUMERIC NULL;
NOTIFY pgrst, 'reload schema';
```

---

### PATCH 15 — G15: Feed ai_model_performance to evolution_tracker
**File:** `backend/history/evolution_tracker.py`
**Where:** In `generate_proposals()` function, before constructing the AI prompt.

```python
# ── ADD THIS BLOCK (G15 FIX): ────────────────────────────────────────────────
perf_rows = sb.table("ai_model_performance") \
    .select("provider, accuracy, calls_today, cost_today, date") \
    .order("date", desc=True).limit(14).execute().data

provider_summary = "No provider performance data available"
if perf_rows:
    by_provider: dict[str, list] = {}
    for r in perf_rows:
        p = r.get("provider", "unknown")
        by_provider.setdefault(p, []).append(r)
    lines = []
    for prov, rows in by_provider.items():
        avg_acc = sum(float(r.get("accuracy") or 0) for r in rows) / len(rows)
        lines.append(f"  {prov}: avg_accuracy={avg_acc:.1%} over {len(rows)} days")
    provider_summary = "\n".join(lines)

# Include in AI prompt as {provider_performance} template variable
```

---

## COMPLETE GAP REGISTER — G1 to G18

| Gap | Severity | Status | Script | Fix |
|-----|----------|--------|--------|-----|
| G1 | 🔴 Critical | Patch 01 above | generate_signals.py | 9 ML cols to sig dict + SQL |
| G2 | 🔴 Critical | Patch 02 above | post_trade_analysis.py | outcome_pnl_pct to update dict |
| G3 | 🟡 Moderate | Patch 03 above | ai_enrich.py | Zero-data prompt guard |
| G4 | ⚪ Minor | Patch 12 above | ai_enrich.py + SQL | Persist ai_strategy_validation |
| G5 | 🟡 Moderate | Patch 04 above | 4 provider files | JSON fence strip |
| G6 | 🔴 Critical | Patch 05 above | ai_enrich.py | event_calendar in AI context |
| G7 | 🟡 Moderate | Patch 11 above | SQL only | lessons quality cols |
| G8 | 🟡 Moderate | Patch 09 above | evolution_tracker.py | v5 kill switch → v6 |
| G9 | 🟡 Moderate | Patch 10 above | evolution_tracker.py | Rich lessons context |
| G10 | ⚪ Low | Manual verify | ingest_fii_dii.py | Verify column names match table |
| G11 | ✅ Resolved | — | pipeline_morning.yml | Cron confirmed correct |
| G12 | ✅ Resolved | — | pipeline_morning.yml | Morning alert confirmed wired |
| G13 | 🟡 Moderate | Patch 06 above | ai_enrich.py | sector + industry in AI context |
| G14 | ⚪ Low | Patch 13 above | append_history.py | Wire regime_history snapshot |
| G15 | ⚪ Low | Patch 15 above | evolution_tracker.py | ai_model_performance in AI |
| G16 | ⚪ Low | Patch 14 above | ingest_global_cues.py + SQL | S&P500 + change % cols |
| G17 | 🔴 Critical | Patch 07 above | ai_enrich.py | market_regime in AI context |
| G18 | 🔴 Critical | Patch 08 above | ai_enrich.py | open_positions in AI context |

**Apply order:** G1 → G2 → G6 → G17 → G18 → G7 SQL → G3 → G5 → G13 → G8 → G9 → G4 → G14 → G15 → G16 → G10

---

## AI AND ML — ARCHITECTURE ROLES

### AI (Primary Intelligence)
AI providers drive every task that requires reasoning, language, or context synthesis:

- **Daily — Signal Conviction** (`ai_enrich.py`): Given full context (signal, regime, events, sector, FII, portfolio), reason about whether this candidate is worth acting on and why.
- **Daily — Trade Lessons** (`post_trade_analysis.py`): After each trade closes, read entry context + hold period events + P&L, write structured root-cause lesson.
- **Weekly — Strategy Evolution** (`evolution_tracker.py`): Given 90-day performance across all context tables, identify what is drifting and why, propose parameter changes with evidence counts.

Provider routing (cheapest first): DeepSeek → Claude → Grok → Gemini → OpenAI → Azure. Auto-switch to ML if daily budget exceeded.

**Complete AI context for signal conviction (target state after all patches):**

| Table | Fields | Why |
|-------|--------|-----|
| `signal_log` | score, strategy, signal_type, regime, fii_flag | Core signal being evaluated |
| `stock_data_daily` | rsi_daily, vol_ratio, delivery_pct, atr_pct, dist_sma50, ret_6m | Entry-time technicals |
| `event_calendar` | event_type, event_date, detail (next 14 days) | Results/AGM in 2 days changes conviction |
| `sector_strength` | strength_score, trend, rank | Is the sector accelerating or dying? |
| `industry_strength` | rank, state, avg_rsi | Industry position within sector |
| `market_regime` | regime, regime_score, breadth_pct | TPO in RISK_OFF = no entry |
| `global_cues` | gift_nifty_chg_pct, gap_signal, dow_chg_pct, sector_impacts | DOW -2% today = lower conviction |
| `open_positions` | count, sector_exposure, already_held | No 4th banking stock if 3 already held |
| `fii_dii_flow` | net_equity, flag, rolling_5d, rolling_20d | Direct flow data, not just the flag |
| `lessons` | scenario_type, root_cause, corrective_rule, confidence | What similar setups taught us |

**Complete AI context for weekly evolution (target state after G8+G9+G15 fixes):**

All 10 tables above plus: `closed_positions` (90-day P&L segmented by strategy/sector/regime), `msl_history` (score velocity), `ai_model_performance` (provider accuracy), `ml_training_log` (feature importance), `strategy_config` (current parameter values).

### ML (RandomForest — Three Specific Roles)

**Role 1 — Signal Conviction Backup (current):** When AI providers fail or budget is exceeded, ML scores conviction from signal_log features. Requires G1 fix to be useful.

**Role 2 — Regime Classification (Phase 2 addition):** Replace the manual Sheet formula for `market_regime` with a trained classifier. Features: Nifty return, advance/decline ratio, breadth_pct, FII net equity, sector dispersion. Trains weekly alongside the conviction model. This is better than a Sheet formula and does not require AI reasoning.

**Role 3 — Discovery Analytics (Phase 4):** `discovery_engine.py` uses statistical ML (Pearson correlation, Mann-Whitney U) to identify which chartink_raw_data columns correlate with trade outcomes. This is pure statistics — not LLM reasoning. ML is better than AI here because actual numbers don't hallucinate.

**ML does NOT:** Write lessons, propose parameter changes, interpret market context, replace AI for any reasoning task.

---

## PHASE 2 — COMPUTATION ENGINE

**Goal:** Eliminate Google Sheet from the formula compute path. All technical columns computed in Python. ASM/GSM auto-fetched. Data quality validated. ML regime classifier replaces Sheet formula.

**Gate to start:**
- All critical gaps applied (G1, G2, G6, G17, G18)
- Phase 1 stable 30+ days with no pipeline failures
- Google Sheet formula audit completed (screenshot every formula column, classify Type A/B)

**Activate:**
```sql
UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';
NOTIFY pgrst, 'reload schema';
```

### Phase 2 New Scripts

**P2.1 — `backend/ingestion/compute_indicators.py`** (stub in tradeos_sql_and_scripts.md)
- Reads: `chartink_raw_data`, `stock_data_daily` (bhavcopy cols), `nifty_total_market`
- Writes: `stock_data_daily` (all computed cols: vol_ratio, atr_pct, dist_sma50, dist_sma200, above_sma50, above_sma200, ret_1m, ret_3m, ret_6m, rs_vs_nifty, consol_range, breakout_setup)
- Wire: Step 03 in `run_pipeline.py`, insert before `03_ingest_sheets`, Phase 2+
- Dependency order: Level 1 (direct from raw) → Level 2 (derived) → Level 3 (market-relative, needs nifty_total_market)

**P2.2 — `backend/ingestion/ingest_asm_gsm.py`** (stub in tradeos_sql_and_scripts.md)
- Reads: NSE website (ASM/GSM/F&O ban lists)
- Writes: `safety_lists`
- Wire: Step 08a in `run_pipeline.py`, after `07_nse_events`, Phase 2+
- Fallback: retain previous day's list (non-fatal)

**P2.3 — `backend/ingestion/data_quality_monitor.py`** ✅ standalone file generated
- Reads: `stock_data_daily`, `chartink_raw_data`, `signal_log`, `master_shortlist`, `msl_history`, `ai_context`, `market_regime`, `open_positions`
- Writes: `data_anomalies` (WARN/ERROR rows only, dedup-guarded on reruns)
- Wire: Step 99 (last, always, non-fatal). Kill switch aware — skips cleanly if active.
- 10 checks: C01 chartink row count (450–510) · C02 RSI range · C03 vol_ratio auto-cap 50x · C04 delivery% bounds · C05 signal scores 0–120 · C06 MSL score jumps >20pts · C07 pipeline completeness (did all steps write today?) · C08 ai_context completeness (G6/G13/G17/G18 patches active?) · C09 ML vs manual regime disagreement (P2+ only) · C10 open positions vs regime cap
- Telegram alert only on ERROR severity — WARNs are silent (log only)

**P2.4 — `backend/ai/providers/ml_regime_classifier.py`** ✅ standalone file generated
- Trains RandomForest on `regime_history` (primary) with `market_regime` table as fallback when G14 history is sparse — gives 6+ months of training data immediately from Day 1 of Phase 2
- Features: Nifty 5d/20d return, A/D ratio, breadth_pct, FII net 5d/20d, sector dispersion (7 features)
- Labels: TRENDING / NEUTRAL / CAUTION / RISK OFF — `class_weight="balanced"` handles class imbalance
- `--train` always also runs `--predict` so today's row is always fresh after Sunday training
- Writes: `predicted_regime` + `regime_confidence` to `market_regime` table alongside manual value
- Flags tier disagreements (ML vs manual diff ≥ 2) to `data_anomalies` — cross-checked independently by C09 in data_quality_monitor
- Wire: `evolution_weekly.yml` Step 2 (after `ml_provider.py --train`) + Step 03 in `run_pipeline.py` daily `--predict` only

### Phase 2 SQL

```sql
-- safety_lists table
CREATE TABLE IF NOT EXISTS public.safety_lists (
  date           DATE PRIMARY KEY,
  asm_symbols    TEXT[]  DEFAULT '{}',
  gsm_symbols    TEXT[]  DEFAULT '{}',
  fo_ban_symbols TEXT[]  DEFAULT '{}',
  asm_count      INT     DEFAULT 0,
  gsm_count      INT     DEFAULT 0,
  fo_ban_count   INT     DEFAULT 0,
  fetched_at     TIMESTAMPTZ DEFAULT now()
);

-- data_anomalies table
CREATE TABLE IF NOT EXISTS public.data_anomalies (
  id          BIGSERIAL PRIMARY KEY,
  date        DATE,
  check_name  TEXT,
  severity    TEXT,  -- 'OK', 'WARN', 'ERROR'
  value       TEXT,
  message     TEXT,
  affected    TEXT,
  created_at  TIMESTAMPTZ DEFAULT now()
);

-- Kite live price column in stock_data_daily
ALTER TABLE stock_data_daily
  ADD COLUMN IF NOT EXISTS kite_price     NUMERIC,
  ADD COLUMN IF NOT EXISTS predicted_regime TEXT;
NOTIFY pgrst, 'reload schema';
```

### Phase 2 run_pipeline.py Changes

```python
# ── New step definitions to add: ─────────────────────────────────────────────
def step_compute_indicators():
    from ingestion.compute_indicators import main as fn; return fn()

def step_asm_gsm():
    from ingestion.ingest_asm_gsm import main as fn; return fn()

def step_quality_check():
    from ingestion.data_quality_monitor import main as fn; return fn()

# ── Rebuild all_steps when phase >= 2: ───────────────────────────────────────
if phase >= 2:
    all_steps = (
        [steps_p0[0], steps_p0[1]]                              # fetch_chartink, ingest_bhavcopy
        + [("03_compute_indicators", step_compute_indicators, False)]  # NEW: before sheets
        + steps_p0[2:]                                           # ingest_sheets, signals, history
        + steps_p1[:2]                                           # fii_dii, nse_events
        + [("08a_asm_gsm", step_asm_gsm, False)]                # NEW: after nse_events
        + steps_p1[2:]                                           # post_trade, ai_enrich, alerts
        + [("99_quality_check", step_quality_check, False)]     # NEW: always last
    )
```

---

## PHASE 3 — SUPERVISED EXECUTION

**Goal:** Every trade requires your explicit Telegram tap. System proposes, you approve, Kite executes. No position is taken without your action.

**Gate (ALL required):**
- Phase 2 stable 30+ days
- Win rate ≥ 50% (check `closed_positions`)
- Kill switch tested manually
- 2-week shadow mode completed and reviewed

**Activate:**
```sql
UPDATE system_config SET value = '3' WHERE key = 'autonomy_phase';
UPDATE system_config SET value = 'shadow' WHERE key = 'execution_mode';
-- After 2-week shadow review:
UPDATE system_config SET value = 'live' WHERE key = 'execution_mode';
```

### Phase 3 New Scripts

**P3.1 — `backend/control/shadow_trade_logger.py`** ✅ standalone file generated
- Mirrors execution_engine.py EXACTLY — same position sizing, same risk_manager calls — so the 2-week review is meaningful not a simplified approximation
- `process_approval(signal_id, action)` is the main entry point — called by `telegram_bot.py` in shadow mode
- `generate_summary_report()` (`--summary` flag): produces 14-day review with approval_rate, risk_block_rate, estimated P&L on APPROVED trades, and a `ready_for_live` boolean
- Sends Telegram notification per shadow decision so you can catch problems in real-time
- Writes: `shadow_trades` with `would_execute` boolean showing whether risk checks would have passed

**P3.2 — `backend/control/risk_manager.py`** ✅ standalone file generated (prior session)
- Called by `execution_engine.py` before every order — also called by `shadow_trade_logger.py` for full fidelity
- 5 checks in order: kill switch → max positions by regime → sector concentration ≤30% → ASM/GSM/FO_BAN → available capital
- Returns: `RiskCheckResult(passed, reason, check_name)` dataclass — never touches Kite directly
- Fail-safe: if risk_manager cannot be imported, execution_engine blocks the order rather than proceeding

**P3.3 — `backend/control/execution_engine.py`** ✅ standalone file generated
- Called by `telegram_bot.py` on inline button callback — NOT a pipeline step
- Three-mode gate: `disabled` (default until Phase 3 SQL) → `shadow` (routes to shadow_trade_logger) → `live` (Kite order)
- Kill switch is always Gate 1 — blocks everything before even reading execution_mode
- One Kite retry on transient failures (timeout/503 only) — never silent retry loops
- REJECTED and DEFERRED update `signal_log.execution_status` for full audit trail
- `handle_action(signal_id, action, notes)` is the main entry point

**P3.4 — `backend/control/telegram_bot.py`** (UPDATE existing)
- Add APPROVE / REJECT / DEFER inline keyboard per BUY_CANDIDATE
- Run as **persistent service** (Render.com or Railway) — not in run_pipeline.py

### Phase 3 SQL

```sql
-- shadow_trades table
CREATE TABLE IF NOT EXISTS public.shadow_trades (
  id            BIGSERIAL PRIMARY KEY,
  signal_id     BIGINT REFERENCES signal_log(id),
  symbol        TEXT,
  strategy      TEXT,
  action        TEXT,       -- 'APPROVED' | 'REJECTED' | 'DEFERRED'
  entry_price   NUMERIC,
  qty           INT,
  approved_at   TIMESTAMPTZ,
  would_execute BOOLEAN DEFAULT false,
  notes         TEXT,
  created_at    TIMESTAMPTZ DEFAULT now()
);

-- Execution tracking in signal_log
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS kite_order_id   TEXT,
  ADD COLUMN IF NOT EXISTS execution_price NUMERIC,
  ADD COLUMN IF NOT EXISTS executed_at     TIMESTAMPTZ;
NOTIFY pgrst, 'reload schema';
```

---

## PHASE 4 — FULL AUTONOMY

**Goal:** Autonomous execution within hard limits. Self-evolving strategy parameters proposed by AI weekly. Google Sheet fully eliminated. Pattern discovery from 6+ months history.

**Gate (manual SQL activation ONLY — no automated trigger):**
- 90 continuous days of Phase 3 clean operation
- Win rate ≥ 55% across last 90 closed trades
- Max drawdown ≤ 8% in Phase 3
- G8 + G9 + G15 evolution_tracker fixes verified working
- 6+ months of `chartink_raw_data` accumulated for discovery_engine

```sql
-- Only activate with all gates confirmed:
UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase';
```

### Phase 4 New Scripts

**P4.1 — `backend/history/discovery_engine.py`** (stub in tradeos_sql_and_scripts.md)
- Statistical correlation of all `chartink_raw_data` columns vs `outcome_pnl_pct`
- Requires G1+G2 fixes applied and 6+ months data
- Writes: `discovery_proposals` (PENDING status only — never auto-applied)

**P4.2 — `backend/history/evolution_tracker.py`** (FIX existing — G8+G9+G15)
- After patches applied: reads all 15 context tables, AI proposes parameter changes
- Writes: `evolution_proposals` (PENDING only)
- Activated via `evolution_weekly.yml` when `AUTONOMY_PHASE == '4'`

### Phase 4 SQL

```sql
-- discovery_proposals table
CREATE TABLE IF NOT EXISTS public.discovery_proposals (
  id                 BIGSERIAL PRIMARY KEY,
  week_of            DATE,
  feature_name       TEXT,
  correlation        NUMERIC,
  sample_size        INT,
  win_rate_with      NUMERIC,
  win_rate_without   NUMERIC,
  evidence           TEXT,
  status             TEXT DEFAULT 'PENDING',
  created_at         TIMESTAMPTZ DEFAULT now()
);

-- evolution_proposals (verify it exists — create if not)
CREATE TABLE IF NOT EXISTS public.evolution_proposals (
  id             BIGSERIAL PRIMARY KEY,
  week_of        DATE,
  proposal_type  TEXT,      -- 'PARAMETER_CHANGE' | 'STRATEGY_GATE' | 'PROVIDER_SWITCH'
  target_config  TEXT,      -- which strategy_config key
  current_value  TEXT,
  proposed_value TEXT,
  evidence       TEXT,
  expected_wr_delta NUMERIC,
  status         TEXT DEFAULT 'PENDING',  -- 'PENDING' | 'APPROVED' | 'REJECTED' | 'REVERTED'
  applied_at     TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT now()
);
NOTIFY pgrst, 'reload schema';
```

### Phase 4 Google Sheet Elimination

When `autonomy_phase = 4`, remove `ingest_sheets.py` from `steps_p0`. All data sources in target state:

| Data | Phase 4 Source |
|------|---------------|
| Price + technicals | `chartink_raw_data` via `compute_indicators.py` |
| Delivery data | `ingest_bhavcopy.py` (NSE direct) |
| Live prices | Kite Connect (`kite_client.py`) |
| Open positions | Kite holdings sync via `execution_engine.py` |
| Closed positions | Kite trade history |
| FII/DII | `ingest_fii_dii.py` (NSE direct) |
| Events | `ingest_nse_events.py` (NSE direct) |
| ASM/GSM | `ingest_asm_gsm.py` (NSE direct) |
| Market regime | `ml_regime_classifier.py` (ML model, weekly trained) |
| Strategy config | `strategy_config` table (evolution_tracker writes after approval) |
| **Google Sheet** | **Fully eliminated** |

---

## DAILY OPERATING PROCEDURE

### 7:00 AM IST — Morning Pipeline (automated)
1. `ingest_global_cues.py MORNING` → `global_cues`
2. `send_alerts.py --morning` → Telegram morning brief (gap, SL proximity, open positions)

### 8:30 AM IST — Manual (Phase 2+ only)
- `python kite/kite_token_refresh.py` — Zerodha security, cannot automate

### 6:00 PM IST — Evening Pipeline (automated)
```
Phase 0+:
  01 fetch_chartink       (fatal)       → chartink_raw_data
  02 ingest_bhavcopy      (non-fatal)   → stock_data_daily (+delivery)
  [Phase 2+]
  03 compute_indicators   (non-fatal)   → stock_data_daily (all computed cols)
  [All phases]
  03/04 ingest_sheets     (fatal)       → 15 tables (deprecated Phase 4)
  04/05 signals           (fatal)       → signal_log (all 9 ML cols after G1)
  05/06 history           (non-fatal)   → msl_history + regime_history (after G14)
Phase 1+:
  06/07 fii_dii           (non-fatal)   → fii_dii_flow
  07/08 nse_events        (non-fatal)   → event_calendar
  [Phase 2+]
  08a asm_gsm             (non-fatal)   → safety_lists
  [All Phase 1+]
  08/09 post_trade        (non-fatal)   → lessons (with outcome_pnl_pct after G2)
  09/10 ai_enrich         (non-fatal)   → ai_context + signal_log (full context after patches)
  10/11 alerts            (non-fatal)   → Telegram evening digest
  [Phase 2+]
  99 quality_check        (non-fatal)   → data_anomalies (always last)
```

---

## WEEKLY OPERATING PROCEDURE

### Every Sunday 6:00 AM IST (evolution_weekly.yml)
1. `ml_provider.py --train` — RandomForest conviction model
2. `ml_regime_classifier.py --train` — Regime classifier (Phase 2+)
3. `evolution_tracker.py` — Phase 4+ only (G8+G9+G15 fixes required first)
4. `discovery_engine.py` — Phase 4+ only (6+ months data required)

---

## EMERGENCY PROCEDURES

```sql
-- Kill switch ON (immediate stop):
UPDATE system_config SET value = 'true' WHERE key = 'kill_switch_active';

-- Kill switch OFF:
UPDATE system_config SET value = 'false' WHERE key = 'kill_switch_active';

-- Regime override:
UPDATE system_config SET value = 'RISK OFF' WHERE key = 'regime_override';
-- Remove override:
DELETE FROM system_config WHERE key = 'regime_override';
```

```bash
# Force single pipeline step:
cd backend
python run_pipeline.py --step signals
python run_pipeline.py --step ai_enrich
python run_pipeline.py --step alerts
python run_pipeline.py --step post_trade
```

---

## REPOSITORY STRUCTURE

```
tradeos-v6/
├── .github/workflows/
│   ├── pipeline_daily.yml          ← 6 PM Mon-Fri [no changes to YML]
│   ├── pipeline_morning.yml        ← 7 AM Mon-Fri [G11+G12 confirmed OK]
│   └── evolution_weekly.yml        ← Sunday 6 AM [add P2+ ML regime + P4 conditionals]
│
└── backend/
    ├── run_pipeline.py             ← Phase 2: rebuild all_steps (see above)
    ├── config.py                   ← All env vars, is_kill_switch_active(), cfg_float()
    │
    ├── ingestion/
    │   ├── fetch_chartink.py       ← P0 ✅
    │   ├── ingest_bhavcopy.py      ← P0 ✅ [already wired Step 02]
    │   ├── ingest_sheets.py        ← P0 ✅ [deprecated P4]
    │   ├── ingest_fii_dii.py       ← P1 ✅ [⚠️ G10: verify cols]
    │   ├── ingest_nse_events.py    ← P1 ✅
    │   ├── ingest_global_cues.py   ← P1 ✅ [⚠️ G16: apply Patch 14]
    │   ├── compute_indicators.py   ← P2 🔲 NEW (stub in tradeos_sql_and_scripts.md)
    │   ├── ingest_asm_gsm.py       ← P2 🔲 NEW (stub in tradeos_sql_and_scripts.md)
    │   └── data_quality_monitor.py ← P2 ✅ NEW — standalone file generated
    │
    ├── signals/
    │   ├── generate_signals.py     ← P0 ✅ [⚠️ G1: apply Patch 01]
    │   └── independent_scanner.py  ← P1 ✅
    │
    ├── ai/
    │   ├── ai_enrich.py            ← P1 ✅ [⚠️ G3+G5+G6+G13+G17+G18: Patches 03-08]
    │   ├── ai_router.py            ← P1 ✅
    │   ├── post_trade_analysis.py  ← P1 ✅ [⚠️ G2: apply Patch 02]
    │   └── providers/
    │       ├── ml_provider.py           ← P1 ✅ [expand: regime model P2]
    │       ├── ml_regime_classifier.py  ← P2 ✅ NEW — standalone file generated
    │       ├── claude_provider.py       ← P1 ✅ [⚠️ G5: Patch 04]
    │       ├── openai_provider.py       ← P1 ✅ [⚠️ G5: Patch 04]
    │       ├── gemini_provider.py       ← P1 ✅ [⚠️ G5: Patch 04]
    │       ├── deepseek_provider.py     ← P1 ✅
    │       ├── grok_provider.py         ← P1 ✅ [⚠️ G5: Patch 04]
    │       └── copilot_provider.py      ← P1 ✅
    │
    ├── alerts/
    │   └── send_alerts.py          ← P1 ✅ [G12 confirmed wired]
    │
    ├── history/
    │   ├── append_history.py       ← P0 ✅ [⚠️ G14: apply Patch 13]
    │   ├── evolution_tracker.py    ← P4 ⚠️ [G8+G9+G15: Patches 09+10+15]
    │   └── discovery_engine.py     ← P4 🔲 NEW
    │
    ├── control/
    │   ├── kill_switch.py          ← P0 ✅
    │   ├── telegram_bot.py         ← P3 ⚠️ UPDATE: add inline buttons
    │   ├── shadow_trade_logger.py  ← P3 ✅ NEW — standalone file generated
    │   ├── risk_manager.py         ← P3 ✅ NEW — standalone file generated (prior session)
    │   └── execution_engine.py     ← P3 ✅ NEW — standalone file generated
    │
    └── kite/
        ├── kite_token_refresh.py   ← P2 ✅ [manual 8:30 AM daily]
        └── kite_client.py          ← P2 ✅
```

**Legend:** ✅ Live · ⚠️ Live with gap · 🔲 Not yet built

---

## SUPABASE TABLES — COMPLETE REFERENCE

| Table | Writes | Reads | Phase | State |
|-------|--------|-------|-------|-------|
| `chartink_raw_data` | fetch_chartink | compute_indicators(P2), discovery_engine(P4) | 0 | ✅ |
| `stock_data_daily` | ingest_bhavcopy ✅, ingest_sheets, compute_indicators(P2) | generate_signals, ai_enrich | 0 | ✅ (computed cols 0 until P2) |
| `master_shortlist` | ingest_sheets | generate_signals, ai_enrich, send_alerts | 0 | ✅ |
| `open_positions` | ingest_sheets (P3: Kite sync) | generate_signals, ai_enrich (after G18), risk_manager | 0 | ✅ (⚠️ G18: not in AI) |
| `closed_positions` | ingest_sheets | ml_provider, post_trade_analysis, evolution_tracker | 0 | ✅ |
| `sector_strength` | ingest_sheets | generate_signals, ai_enrich (after G13) | 0 | ✅ (⚠️ G13) |
| `industry_strength` | ingest_sheets | generate_signals, ai_enrich (after G13) | 0 | ✅ (⚠️ G13) |
| `market_regime` | ingest_sheets (P2: ml_regime_classifier) | generate_signals, ai_enrich (after G17), risk_manager | 0 | ✅ (⚠️ G17: not in AI) |
| `signal_log` | generate_signals, ai_enrich | send_alerts, ml_provider, evolution_tracker | 0 | ✅ (⚠️ G1: 9 cols, G2: outcome) |
| `msl_history` | append_history | ml_provider, evolution_tracker | 0 | ✅ |
| `event_calendar` | ingest_sheets, ingest_nse_events | generate_signals, ai_enrich (after G6) | 0/1 | ✅ (⚠️ G6) |
| `lessons` | post_trade_analysis | ai_router, evolution_tracker | 1 | ✅ (⚠️ G7: no confidence) |
| `nse_holidays` | ingest_sheets | generate_signals | 0 | ✅ |
| `nifty_total_market` | ingest_sheets | compute_indicators (rs_vs_nifty) | 0 | ✅ |
| `system_config` | manual SQL | every script | 0 | ✅ |
| `strategy_config` | ingest_sheets, evolution_proposals | generate_signals | 0 | ✅ |
| `regime_history` | append_history (after G14) | evolution_tracker (P4) | — | ⚠️ G14: orphan |
| `fii_dii_flow` | ingest_fii_dii | generate_signals, ai_enrich (direct), send_alerts | 1 | ✅ (⚠️ G10: verify) |
| `global_cues` | ingest_global_cues | ai_enrich (after G17), send_alerts | 1 | ✅ (⚠️ G17 patch + G16 cols) |
| `ai_context` | ai_enrich | send_alerts | 1 | ✅ |
| `ai_model_performance` | ai_router | evolution_tracker (after G15) | 1 | ⚠️ G15: written not read |
| `ml_training_log` | ml_provider | evolution_tracker (feature importance) | 1 | ✅ |
| `scanner_signals` | independent_scanner | send_alerts | 1 | ✅ |
| `safety_lists` | ingest_asm_gsm | generate_signals, risk_manager | 2 | 🔲 |
| `data_anomalies` | data_quality_monitor | frontend | 2 | 🔲 |
| `shadow_trades` | shadow_trade_logger | you via Supabase | 3 | 🔲 |
| `evolution_proposals` | evolution_tracker | you via Supabase / Telegram | 4 | ⚠️ G8+G9 bugs in writer |
| `discovery_proposals` | discovery_engine | you via Supabase | 4 | 🔲 |

---

## PRIORITY CHECKLIST

### Before Phase 2 — Apply All Patches
- [ ] SQL: G1 signal_log 9 cols
- [ ] Patch 01: generate_signals.py — 9 ML cols to sig dict
- [ ] Patch 02: post_trade_analysis.py — outcome_pnl_pct
- [ ] SQL: G7 lessons 5 quality cols
- [ ] Patch 03: ai_enrich.py — G3 zero-data guard
- [ ] Patch 04: 4 provider files — JSON fence strip
- [ ] Patch 05: ai_enrich.py — G6 event_calendar
- [ ] Patch 06: ai_enrich.py — G13 sector/industry
- [ ] Patch 07: ai_enrich.py — G17 market_regime + global_cues
- [ ] Patch 08: ai_enrich.py — G18 open_positions portfolio
- [ ] Patch 09: evolution_tracker.py — G8 kill switch
- [ ] Patch 10: evolution_tracker.py — G9 rich lessons
- [ ] Patch 11: SQL G7 lessons
- [ ] Patch 12: ai_enrich.py + SQL — G4 ai_strategy_validation
- [ ] Patch 13: append_history.py — G14 regime snapshot
- [ ] Patch 14: ingest_global_cues.py — G16 S&P500 + chg%
- [ ] Patch 15: evolution_tracker.py — G15 provider performance
- [ ] Manual verify: ingest_fii_dii.py column names (G10)

### Phase 2 Gate (Month 3-4)
- [ ] Sheet formula audit completed
- [ ] compute_indicators.py built + tested
- [ ] ingest_asm_gsm.py built + tested
- [ ] data_quality_monitor.py built + tested
- [ ] ml_regime_classifier.py built + tested
- [ ] Phase 1 stable 30+ days
- [ ] `UPDATE system_config SET value = '2' WHERE key = 'autonomy_phase';`

### Phase 3 Gate (Month 5-6)
- [ ] Phase 2 stable 30+ days
- [ ] Win rate ≥ 50%
- [ ] Kill switch tested
- [ ] shadow_trade_logger.py 2-week run reviewed
- [ ] `UPDATE system_config SET value = '3' WHERE key = 'autonomy_phase';`

### Phase 4 Gate (Month 7+)
- [ ] 90 days Phase 3 clean
- [ ] Win rate ≥ 55%, Max DD ≤ 8%
- [ ] G8+G9+G15 evolution_tracker verified working
- [ ] 6+ months chartink_raw_data accumulated
- [ ] `UPDATE system_config SET value = '4' WHERE key = 'autonomy_phase';`

---

*TradeOS v6 · v3.0 · G1-G18 registered · G11+G12 resolved · 15 patches defined · 5 new scripts · 4 new tables*
*Google Sheet ID: 1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw*
