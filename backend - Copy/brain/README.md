# TradeOS v6 — Brain Engine v2
## Complete Reference Guide

---

## File Structure

The `brain/` folder lives under `backend/` as a sibling to `ai/`, `compute/`, `signals/`, and `alerts/`:

```
backend/
├── ai/                                    ← existing AI router + provider adapters
├── compute/                               ← existing MSL + screening engines
├── signals/                               ← existing signal generation
├── alerts/                                ← existing Telegram alerts + send logic
├── brain/                                 ← Brain Engine v2
│   ├── __init__.py                        ← package marker
│   ├── brain_engine.py                    ← orchestrator — only file you call directly
│   ├── brain_prompt.py                    ← master LLM system prompt (injected into every AI call)
│   ├── data_aggregator.py                 ← SELECT * data loader + forward return computation
│   ├── dynamic_registry.py                ← live discovery of engines/tables/fields/thresholds
│   ├── quant_analyzer.py                  ← 13 statistical analysis types across all loaded data
│   ├── backtester_and_change_manager.py   ← proposal validation + lifecycle + CLI
│   ├── llm_synthesizer.py                 ← LLM synthesis via ai_router with full dataset
│   ├── script_scanner.py                  ← AST-based scanner for hardcoded tunable values
│   ├── performance_tracker.py             ← daily/weekly/monthly metrics + Telegram digest
│   ├── position_manager.py                ← Telegram inline buttons + open/close positions
│   ├── scheduler.yaml                     ← APScheduler/cron config for all brain jobs
│   ├── scheduler_yaml.py                  ← APScheduler runner that reads scheduler.yaml
│   ├── schema.sql                         ← run ONCE in Supabase — creates all 8 tables
│   ├── DASHBOARD_SPEC.md                  ← full spec for the Intelligence Console frontend
│   └── README.md                          ← this file
├── config.py                              ← existing — get_supabase(), cfg(), kill switch
├── run_pipeline.py                        ← existing — add step_22 for daily perf tracking
└── .env                                   ← existing secrets
```

---

## What Each File Does

### `brain_engine.py` — Orchestrator
The single entry point for all brain runs. Coordinates the full pipeline:
`script_scanner → data_aggregator → quant_analyzer → backtester → llm_synthesizer → change_manager → performance_tracker → Telegram`

Supports four modes:

| Mode | What runs | When |
|---|---|---|
| `full` | Everything — quant + LLM + script scan + auto-apply + Telegram | Sunday weekly |
| `quant` | Quant + backtest + proposals, no LLM cost | Wednesday mid-week |
| `scan` | Script scan only — reports hardcoded values, zero proposals | Saturday weekly |
| `dry` | Full analysis, zero writes, zero Telegram | Manual testing |

Also enforces: kill switch check, `brain_enabled` flag, `brain_max_proposals_per_run` cap, proposal deduplication and priority sorting across quant + LLM sources.

---

### `brain_prompt.py` — Master LLM Persona
Contains `BRAIN_SYSTEM_PROMPT` — injected into every LLM call. Defines the brain's expertise across:
- NSE swing trading, market microstructure (FII/DII, ASM/GSM, F&O bans)
- All 10 screening engines (CTL, SBS, TPO, VBD, IAD, RSB, MOM, RVS, SEC, EAP)
- All 15 intelligence functions from `compute_msl`
- Statistical rigour rules (minimum sample sizes, confidence interval requirements)
- Anti-hallucination rules: every claim requires exact `n`, field name, and decimal win rate
- Self-improvement mandate: proposals must cite cycle-over-cycle win rate movement

Centralised here so updating the brain's expertise requires editing one file only.

---

### `data_aggregator.py` — Data Loader
Loads `SELECT *` from all relevant tables. The brain decides what correlates — no pre-filtering.

**Core tables always loaded:** `signal_log`, `msl_history`, `stock_data_daily`, `ai_context`, `system_config`, `lessons`, `open_positions`, `closed_positions`, `brain_proposals`, `config_change_log`, `performance_metrics`, `signal_daily_summary`

**Additional tables:** auto-discovered via `dynamic_registry` from `brain_script_registry`

**Forward return proxy (outcome labelling):**
- `WIN` = any of `ret_fwd_5d/10d/15d/20d` reaches `+perf_win_threshold_pct` (default 3%)
- `LOSS` = any horizon hits `-perf_stop_threshold_pct` (default 5%)
- When `closed_positions` is populated, actual P&L overrides the proxy

Returns a `dataset` dict of DataFrames passed to all downstream modules.

---

### `dynamic_registry.py` — Live Discovery
Replaces every hardcoded list in the brain with live Supabase discovery. Instantiated once per brain run and passed to all analysis functions.

| Was hardcoded | Now discovered from | How to extend |
|---|---|---|
| `ENGINES` list | `system_config.regime_engine_weights` JSON inner keys | Add to JSON in system_config |
| `CATEGORICAL_FIELDS` | Actual `signal_log` columns (low-cardinality strings at runtime) | Add column to signal_log |
| `THRESHOLD_FIELD_MAP` | `signal_threshold_*` key names parsed by naming convention | Add `signal_threshold_*` key to system_config |
| `KNOWN_TABLES` | `pg_tables` via `get_public_tables()` RPC | Create table in Supabase |
| Regime names | `system_config.regime_engine_weights` JSON outer keys | Add to JSON in system_config |

**Invariant:** never add to a hardcoded list in any brain file. If you do, the script scanner will detect it and generate a `SCRIPT_PATCH` proposal automatically.

---

### `quant_analyzer.py` — Statistical Engine
Runs 13 analysis types across all loaded DataFrames. Enforces `MIN_SAMPLES = 15` before generating any statistic. Has `_ENGINES_FALLBACK` and `_CAT_FIELDS_FALLBACK` only as crash guards — normal operation uses `DynamicRegistry` exclusively.

| # | Analysis | What it finds |
|---|---|---|
| 1 | Threshold sensitivity | All `signal_threshold_*` keys — optimal floor/ceiling values |
| 2 | Engine performance | All 10 engines vs baseline win rate |
| 3 | Score correlation | ALL numeric fields vs forward return (Pearson r) |
| 4 | Categorical field analysis | `bb_context`, `vwap_alignment`, `lifecycle`, etc. |
| 5 | Signal type quality | PRIME/BREAKOUT/STAGED/REENTRY win rates |
| 6 | Regime accuracy | Does regime classification predict signal success? |
| 7 | Lesson reliability | Which AI lessons are proven vs statistical noise |
| 8 | Self-improvement note mining | Recurring observations across AI analysis runs |
| 9 | Regime × Engine interaction | Engine performance within each regime separately |
| 10 | Multi-field combination | 2-field combos that outperform either field alone |
| 11 | Temporal decay | Does signal quality degrade over time in the lookback window? |
| 12 | AI conviction accuracy | Does HIGH/MEDIUM/LOW actually differentiate outcomes? |
| 13 | Brain impact measurement | Did past applied proposals actually improve win rates? |

---

### `backtester_and_change_manager.py` — Proposal Validator + Lifecycle
Two responsibilities in one file — import from it as:
```python
from brain.backtester_and_change_manager import run_backtests, save_proposals, ...
```

**Backtester:** validates every quant finding before it becomes a proposal. Applies safety caps:
- Max change per cycle: ±30% of current value
- Min backtest cohort: 10 signals
- High-impact flag: triggered when proposed change would reduce signal pool by >50% (forces manual approval)

**Change Manager:** owns the full proposal lifecycle:

```
PENDING ──→ AUTO_APPLIED  (conf ≥ 0.90 + wr_delta ≥ 5pp + not high_impact)
        ──→ APPROVED       (manual: CLI or dashboard)
                ──→ APPLIED ──→ ROLLED_BACK
        ──→ REJECTED
        ──→ EXPIRED        (30 days elapsed, no action)
```

Auto-apply eligible types: `THRESHOLD_CHANGE`, `ENGINE_WEIGHT`, `REGIME_WEIGHT`
Always manual: `SCRIPT_PATCH`, `CODE_SUGGESTION`, `INSIGHT`

---

### `llm_synthesizer.py` — LLM Analysis Layer
Builds the full synthesis prompt from quant findings + every table's schema, stats, and sample rows, then calls your existing `ai_router`. The LLM sees all fields from all tables — no pre-filtering.

Anti-hallucination enforced at two layers:
1. Prompt level via `brain_prompt.py` rules
2. Validation layer: rejects proposals missing reasoning chains, rejects unknown proposal types, caps LLM confidence by sample size (`n<20 → 0.60`, `n<50 → 0.75`, `n<100 → 0.85`)

Valid proposal types the LLM can generate: `THRESHOLD_CHANGE`, `ENGINE_WEIGHT`, `REGIME_WEIGHT`, `SCRIPT_PATCH`, `CODE_SUGGESTION`, `INSIGHT`

---

### `script_scanner.py` — AST Hardcode Detector
Points at all `.py` files under `backend/` and for each one:
1. Extracts all hardcoded numeric/string values that are candidates for `cfg()` migration
2. Identifies which Supabase tables the script reads/writes
3. Identifies which `system_config` keys the script already uses
4. Generates a unified diff showing the proposed `cfg()` migration
5. Registers everything in `brain_script_registry` (one row per script)

Auto-discovers new `.py` files — no registration step needed. New scripts appear in the next brain scan cycle.

**Script patching — two phases:**

Phase 1 (default — `brain_script_patching_enabled = false`): brain detects hardcoded values, generates `SCRIPT_PATCH` proposals with unified diffs stored in `brain_proposals`. You review and apply manually.

Phase 2 (opt-in — set to `true` in system_config): brain auto-applies `cfg()` migrations, backs up originals to `script_change_log`, commits to git with proposal ID in message. Rollback = `git revert` + restore from `script_change_log.backup_content`.

---

### `performance_tracker.py` — Metrics Engine
Computes and stores performance at three grains. Can be called as `step_22` at end of daily pipeline or standalone.

**Daily** (D+5 horizon — earliest available, runs Mon–Fri 20:30 IST):
- Signal count and type distribution
- Forward return proxy win rate at D+5
- Score predictiveness: Pearson r of `score_adjusted` vs D+5 return
- AI conviction accuracy vs D+5 outcome

**Weekly** (D+10 horizon — primary decision horizon, runs Sunday 19:30 IST):
- Win rate by signal type at D+10
- Engine leaderboard with delta vs prior week
- Regime accuracy — does regime classification predict success?
- Brain impact: proposals applied this week + avg win-rate delta from those changes
- AI provider accuracy comparison

**Monthly** (D+20 horizon — complete picture, runs last calendar day of month):
- Full D+20 win rates
- Score correlation trend — is `final_score` becoming more predictive over time?
- Before/after win rate for each applied brain proposal
- Cumulative P&L from `closed_positions` (actual trades, not proxy)

All metrics stored in `performance_metrics` table and fed into `brain_analysis_log` so the brain sees its own track record.

---

### `position_manager.py` — Telegram Position Tracking
Manages open and closed positions via Telegram inline buttons. Integrates into `send_alerts.py`.

**Flow:**
1. Signal created in `signal_log`
2. `send_alerts.py` calls `build_signal_keyboard()` — attaches inline buttons to alert
3. User taps **✅ Entered at ₹X** on Telegram
4. Telegram webhook → `/telegram/callback` → creates `open_positions` row
5. User taps **🔴 Exit** → creates `closed_positions` row with actual P&L

**Price reconciliation:** signal price = `signal_log.current_price` (close at signal date). Actual fill price stored as `entry_price_actual`. Brain uses actual price for P&L when available.

Button states: Enter, Skip, Watch, Update Price, Exit

---

### `scheduler.yaml` + `scheduler_yaml.py` — Local Scheduler
`scheduler.yaml` defines all jobs (schedule, command, timeout, failure handler).
`scheduler_yaml.py` reads it via APScheduler — use when running on a VPS/local machine instead of GitHub Actions. More reliable than GH Actions for time-sensitive jobs.

```bash
python -m brain.scheduler_yaml              # start blocking scheduler (all jobs)
python -m brain.scheduler_yaml --list       # list all jobs + next run times
python -m brain.scheduler_yaml --cron       # print equivalent crontab (UTC)
python -m brain.scheduler_yaml --run <job>  # run one job immediately
```

---

### `schema.sql` — Database Schema
Run **once** in Supabase SQL Editor. Creates all 8 brain tables plus 15 `system_config` seed keys.

| Table | Purpose |
|---|---|
| `brain_proposals` | Every proposed change before it touches `system_config` |
| `config_change_log` | Full lineage of every `system_config` write, ever |
| `script_change_log` | Full versioned history of every script patch the brain applies |
| `brain_analysis_log` | One row per brain run with full findings |
| `brain_script_registry` | Auto-populated map of every script's tables/keys/hardcoded values |
| `open_positions` | Managed via Telegram buttons + dashboard |
| `closed_positions` | Actual trade outcomes — enriches brain P&L analysis |
| `performance_metrics` | Pre-aggregated daily/weekly/monthly performance |

After running `schema.sql`, enable RPC-based table discovery with a single function — add this to Supabase SQL Editor:
```sql
CREATE OR REPLACE FUNCTION get_public_tables()
RETURNS TABLE(table_name text) AS $$
  SELECT table_name::text FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  ORDER BY table_name;
$$ LANGUAGE sql SECURITY DEFINER;
```

---

### `DASHBOARD_SPEC.md` — Intelligence Console Frontend Spec
Full specification for building the TradeOS web dashboard. Stack: Next.js 14 / React + Vite, shadcn/ui + Tailwind, Recharts, TanStack Table, Supabase Realtime. Covers all FastAPI endpoints, UI components, and real-time update patterns for signals, proposals, positions, and performance.

---

## First-Time Setup

### Step 1: Run SQL in Supabase
```sql
-- Paste and run schema.sql in Supabase SQL Editor
-- Then add the get_public_tables() RPC function above
```

Verify:
```sql
SELECT key FROM system_config WHERE key LIKE 'brain_%';   -- expect 6 rows
SELECT * FROM get_public_tables() LIMIT 5;                 -- expect table list
```

### Step 2: Install Brain Dependencies
The following are required by the brain but not in the base `requirements.txt` — add them:
```
apscheduler==3.10.4
pyyaml==6.0.2
scipy==1.14.0
```

### Step 3: Dry Run (zero writes — safe first test)
```bash
cd backend
python -m brain.brain_engine --mode dry --days 60
```
Expected output: DynamicRegistry summary, signal count + outcome coverage %, proposals printed but not saved, zero DB writes, zero Telegram.

### Step 4: Script Scan (writes only to `brain_script_registry`)
```bash
python -m brain.brain_engine --mode scan
```
Check `brain_script_registry` in Supabase — one row per `.py` file.

### Step 5: Quant-Only Run (writes proposals, no LLM cost)
```bash
python -m brain.brain_engine --mode quant
```
Review proposals:
```bash
python -m brain.backtester_and_change_manager list
```

### Step 6: Approve One Proposal (test full lineage)
```bash
python -m brain.backtester_and_change_manager approve <id>
python -m brain.backtester_and_change_manager history
```
Verify `config_change_log` has a row with old/new values and `changed_by='manual'`.

### Step 7: Full Run (LLM enabled)
```bash
python -m brain.brain_engine --mode full
```
Uses the active `ai_provider` from `system_config`.

### Step 8: Integrate Position Manager
Add to `send_alerts.py`:
1. Call `build_signal_keyboard()` when sending each signal alert
2. Add a Telegram webhook endpoint to handle button callbacks (Enter/Skip/Watch/Update Price/Exit)

See the integration block at the bottom of `brain/position_manager.py`.

---

## Scheduled Jobs

### GitHub Actions (`.github/workflows/brain_scheduler.yml`)
Six jobs run automatically on separate cron triggers. Each job runs independently — a failure in one does not affect others.

| Job | IST Schedule | UTC Cron | Mode | Timeout |
|---|---|---|---|---|
| `performance_tracking` | Mon–Fri 20:30 | `0 15 * * 1-5` | — | 15 min |
| `brain_quant_midweek` | Wednesday 19:30 | `0 14 * * 3` | quant | 20 min |
| `script_scan_weekly` | Saturday 10:00 | `30 4 * * 6` | scan | 10 min |
| `brain_full_weekly` | Sunday 19:30 | `0 14 * * 0` | full | 45 min |
| `weekly_perf_digest` | Sunday 20:00 | `30 14 * * 0` | — | 5 min |
| `expire_proposals` | Sunday 21:00 | `30 15 * * 0` | — | 2 min |

Sunday jobs are time-gated 30–60 min apart to sequence: brain runs first, digest summarises its output, expire cleans up stale proposals last.

### Local / VPS (APScheduler)
```bash
python -m brain.scheduler_yaml    # blocking process, all jobs managed here
```

---

## CLI Reference

```bash
# Brain runs
python -m brain.brain_engine --mode dry          # analysis only, zero writes
python -m brain.brain_engine --mode scan         # script scan only
python -m brain.brain_engine --mode quant        # quant + proposals, no LLM
python -m brain.brain_engine --mode full         # everything
python -m brain.brain_engine --mode full --days 60   # override lookback window

# Proposal management
python -m brain.backtester_and_change_manager list
python -m brain.backtester_and_change_manager approve <id>
python -m brain.backtester_and_change_manager reject <id>
python -m brain.backtester_and_change_manager rollback <id>
python -m brain.backtester_and_change_manager history
python -m brain.backtester_and_change_manager history --n 50

# Scheduler (local)
python -m brain.scheduler_yaml                   # start blocking scheduler
python -m brain.scheduler_yaml --list            # list all jobs + schedules
python -m brain.scheduler_yaml --cron            # print crontab equivalent
python -m brain.scheduler_yaml --run <job>       # run one job immediately

# Performance (manual trigger)
python -c "from brain.performance_tracker import run_performance_tracking; run_performance_tracking()"
python -c "from brain.performance_tracker import send_weekly_telegram_summary; send_weekly_telegram_summary()"
```

---

## Safety Guarantees (Hardcoded — Cannot Be Overridden)

| Guard | Value | Enforced In |
|---|---|---|
| Max threshold change per cycle | ±30% of current value | `backtester_and_change_manager.py` |
| Min signals for any statistic | 15 | `quant_analyzer.py` |
| Min signals in backtest cohort | 10 | `backtester_and_change_manager.py` |
| High-impact flag (>50% signal pool reduction) | always manual | `backtester_and_change_manager.py` |
| `SCRIPT_PATCH` / `CODE_SUGGESTION` / `INSIGHT` | never auto-apply | `backtester_and_change_manager.py` |
| Engine weight bounds | 0.3× – 2.0× | `backtester_and_change_manager.py` |
| LLM confidence cap by sample size | n<20→0.60, n<50→0.75, n<100→0.85 | `brain_prompt.py` |
| LLM reasoning chain required per proposal | rejects without it | `llm_synthesizer.py` |
| Master kill switch | halts all brain activity immediately | `brain_engine.py` |

---

## How DynamicRegistry Eliminates Maintenance

The invariant going forward: **never add to a hardcoded list in any brain file.**

Adding new data points requires zero code changes:
- **New table** → create it in Supabase → auto-discovered on next brain run
- **New signal_log column** → add to table → auto-included in categorical/numeric analyses
- **New engine** → add to `regime_engine_weights` JSON in `system_config` → auto-picked up
- **New threshold key** → add `signal_threshold_*` key to `system_config` → auto-mapped
- **New config key** → add to `system_config` → brain reads it next analysis

If a hardcoded list is added by mistake, the script scanner will detect it on the next Saturday scan and generate a `SCRIPT_PATCH` proposal to migrate it automatically.

---

## Performance Measurement — What "Better" Means

The brain tracks improvement across three horizons and cites cycle-over-cycle changes in every proposal.

**D+5 (daily):** earliest signal quality signal. Win rate + score Pearson r updated every weekday.

**D+10 (weekly):** primary decision horizon. Engine leaderboard, regime accuracy, brain impact delta all computed here. This is the main signal the brain optimises against.

**D+20 (monthly):** complete picture. Includes actual closed-position P&L, cumulative config change impact, AI provider comparison. Used for strategic reassessment of threshold and weight calibration.
