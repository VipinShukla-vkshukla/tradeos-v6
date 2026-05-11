# TradeOS v6 — Brain Engine v2
## Complete Setup & Reference Guide

---

## Complete File Structure

Place the entire `brain/` folder under `backend/` as a sibling to your
existing `ai/`, `compute/`, `signals/` folders:

```
backend/
├── ai/                                    ← existing
├── compute/                               ← existing (changes needed — see Pipeline Changes)
├── signals/                               ← existing
├── alerts/                                ← existing (changes needed — see Pipeline Changes)
├── brain/                                 ← NEW — drop all files here
│   ├── __init__.py
│   ├── brain_engine.py                    ← orchestrator (main entry point)
│   ├── brain_prompt.py                    ← master LLM persona (trading + quant expertise)
│   ├── data_aggregator.py                 ← SELECT * everywhere, forward returns, outcome proxy
│   ├── dynamic_registry.py                ← auto-discovers engines, tables, fields, thresholds
│   ├── quant_analyzer.py                  ← 12 analysis types, all fields (apply PATCH_1 first)
│   ├── backtester_and_change_manager.py   ← validation + proposal lifecycle + CLI + Telegram
│   ├── llm_synthesizer.py                 ← full-data LLM synthesis via your ai_router
│   ├── script_scanner.py                  ← auto-discovers hardcoded values (apply PATCH_2 first)
│   ├── performance_tracker.py             ← daily/weekly/monthly metrics (apply PATCH_3 first)
│   ├── position_manager.py                ← Telegram buttons + open/close positions
│   ├── scheduler.yaml                     ← full YAML schedule config (all jobs)
│   ├── scheduler_yaml.py                  ← YAML scheduler runner (APScheduler)
│   ├── schema.sql                         ← run ONCE in Supabase (step 1)
│   ├── schema_rpc.sql                     ← run ONCE after schema.sql (step 1b)
│   └── README.md                          ← this file
├── config.py                              ← existing
├── run_pipeline.py                        ← existing (add step_22 — see Pipeline Changes)
└── .env                                   ← existing
```

> **Patch files** (`PATCH_1` through `PATCH_4`) and `KNOCK_ON_CHANGES.py` are
> **read-and-discard instruction documents** — apply the changes they describe,
> then delete them. They do not belong in the `brain/` folder.

> **`backtester_and_change_manager.py`** is a single combined file. Import from it as:
> ```python
> from brain.backtester_and_change_manager import run_backtests, save_proposals, ...
> ```

---

## New Tables Created by schema.sql

| Table | Purpose |
|---|---|
| `brain_proposals` | Every proposed change before it touches system_config |
| `config_change_log` | Full lineage of every system_config write, ever |
| `brain_analysis_log` | One row per brain run with full findings |
| `brain_script_registry` | Auto-populated map of every script's tables/keys/hardcoded values |
| `open_positions` | Managed via Telegram buttons + dashboard |
| `closed_positions` | Actual trade outcomes — enriches brain P&L analysis |
| `performance_metrics` | Pre-aggregated daily/weekly/monthly performance |
| `script_change_log` | Full versioned history of every script patch the brain applies |

---

## First Run — Step by Step

### Step 1: Run SQL in Supabase
Open Supabase → SQL Editor. Run **both** files in order:

```
1. schema.sql      → creates 8 tables + 15 system_config keys
2. schema_rpc.sql  → creates get_public_tables() RPC (enables dynamic table discovery)
```

Verify:
```sql
SELECT key FROM system_config WHERE key LIKE 'brain_%';
-- Expected: 6 rows

SELECT * FROM get_public_tables() LIMIT 5;
-- Expected: list of your table names
```

### Step 2: Install Dependencies
```bash
pip install apscheduler pyyaml pandas numpy scipy loguru
```

### Step 3: Apply the Four Brain Patches
Four brain files need targeted edits after being placed in your folder.
Read each PATCH file, apply the changes to the corresponding brain file, then discard.

| Patch File | Target Brain File | What It Changes |
|---|---|---|
| `PATCH_1_quant_analyzer.py` | `brain/quant_analyzer.py` | Removes 3 hardcoded lists; wires DynamicRegistry |
| `PATCH_2_script_scanner.py` | `brain/script_scanner.py` | Replaces `KNOWN_TABLES` with live DB discovery |
| `PATCH_3_performance_tracker.py` | `brain/performance_tracker.py` | Replaces `ENGINES` list with dynamic discovery |
| `PATCH_4_brain_engine.py` | `brain/brain_engine.py` | Instantiates DynamicRegistry; passes it through |

### Step 4: Apply Pipeline Script Changes
Five changes to your existing pipeline scripts.
Documented in `KNOCK_ON_CHANGES.py` — read, apply, discard.

| Priority | File | Change | Required? | Effort |
|---|---|---|---|---|
| P1 | `compute/screen_stocks.py` | `REGIME_ENGINE_WEIGHTS` → `cfg()` | **YES** | 10 min |
| P2 | `compute/compute_msl.py` | Score weights → `cfg()` | **YES** | 20 min |
| P3 | `alerts/send_alerts.py` | Add Telegram inline keyboards | YES (positions) | 30 min |
| P4 | `run_pipeline.py` | Add `step_22_performance_tracking` | Recommended | 15 min |
| P5 | `compute/post_trade_analysis.py` | Lesson thresholds → `cfg()` | Recommended | 10 min |

### Step 5: Dry Run (safe — zero writes)
```bash
cd backend
python -m brain.brain_engine --mode dry --days 60
```
Expected output:
- DynamicRegistry summary: engines, tables, categorical fields, threshold mappings discovered
- Signal count and outcome coverage %
- Proposals printed without saving
- Zero DB writes, zero Telegram

### Step 6: Script Scan (writes only to brain_script_registry)
```bash
python -m brain.brain_engine --mode scan
```
Scans all `.py` files, prints report of hardcoded values found per script.
Check `brain_script_registry` in Supabase — one row per script.

### Step 7: Quant-Only Run (writes proposals, no LLM cost)
```bash
python -m brain.brain_engine --mode quant
```
Review proposals:
```bash
python -m brain.backtester_and_change_manager list
```

### Step 8: Approve One Proposal (test lineage end-to-end)
```bash
python -m brain.backtester_and_change_manager approve <id>
python -m brain.backtester_and_change_manager history
```
Verify `config_change_log` has a row with old/new values and `changed_by='manual'`.

### Step 9: Full Run (LLM synthesis enabled)
```bash
python -m brain.brain_engine --mode full
```
Uses your active `ai_provider` from `system_config` (currently deepseek).

### Step 10: Start Scheduler
```bash
# Option A — APScheduler (recommended, blocking process)
python -m brain.scheduler_yaml

# Option B — Print crontab equivalent and add to cron
python -m brain.scheduler_yaml --cron

# Option C — Run a specific job immediately
python -m brain.scheduler_yaml --run brain_full_weekly
```

### Step 11: Integrate Position Manager into send_alerts.py
See the integration block at the bottom of `brain/position_manager.py`.
Two additions:
1. Call `build_signal_keyboard()` when sending signal alerts
2. Add a Telegram webhook endpoint to handle button callbacks (Enter/Skip/Watch/Update Price/Exit)

---

## How DynamicRegistry Works

`dynamic_registry.py` replaces every hardcoded list in the brain with live discovery.
It is instantiated once per brain run and passed through to every analysis function.

| Was hardcoded | Now discovered from | How to add new ones |
|---|---|---|
| `ENGINES` list | `system_config.regime_engine_weights` JSON inner keys | Add to JSON in system_config |
| `CATEGORICAL_FIELDS` | Actual `signal_log` columns (low-cardinality strings detected at runtime) | Add column to signal_log table |
| `THRESHOLD_FIELD_MAP` | `signal_threshold_*` key names parsed via naming convention | Add `signal_threshold_*` key to system_config |
| `KNOWN_TABLES` | `pg_tables` via `get_public_tables()` RPC | Create table in Supabase |
| Regime names | `system_config.regime_engine_weights` JSON outer keys | Add to JSON in system_config |

**The invariant going forward:** never add to a hardcoded list in any brain file.
If you forget, the brain's script scanner will detect it and generate a `SCRIPT_PATCH` proposal.

---

## How the Brain Modifies Scripts (SCRIPT_PATCH)

**Phase 1 — current default (`brain_script_patching_enabled = false`):**
- Brain detects hardcoded values via `script_scanner.py`
- Generates `SCRIPT_PATCH` proposals with unified diffs stored in `brain_proposals`
- You review in the proposal queue (dashboard or CLI)
- You apply manually using the diff, mark applied via dashboard or CLI

**Phase 2 — enable when confident (`brain_script_patching_enabled = true`):**
- Brain auto-applies `cfg()` migrations to Python scripts
- Each patch = one git commit with proposal ID in message
- Rollback = `git revert` + restore from `script_change_log.backup_content`
- `brain_script_auto_patch = false` keeps manual-approval even with patching enabled

To enable Phase 2:
```sql
UPDATE system_config SET value = 'true' WHERE key = 'brain_script_patching_enabled';
```

---

## Performance Measurement — What "Better" Means

**Daily** (horizon: D+5, runs after every pipeline via step_22):
- Win rate of signals from 7–9 calendar days ago
- Score→Return Pearson r at D+5
- Signal count by type

**Weekly** (horizon: D+10, runs every Sunday):
- Win rate at D+10 (primary decision horizon)
- Engine leaderboard with delta vs previous week
- Regime accuracy — does regime classification predict signal success?
- Brain impact: proposals applied this week + avg win-rate delta from those changes
- AI conviction accuracy (HIGH vs MEDIUM vs LOW differentiation)

**Monthly** (horizon: D+20, runs on last calendar day of month):
- Full D+20 win rate (complete picture)
- Score correlation trend — is `final_score` becoming more predictive over time?
- Cumulative P&L from `closed_positions` (actual trades, not proxy)
- Before/After win rate for each applied brain proposal
- AI provider comparison across all providers used

All metrics stored in `performance_metrics` table.
Weekly Telegram digest sent automatically every Sunday at 20:00 IST.

---

## Proposal Lifecycle

```
PENDING ──→ AUTO_APPLIED (conf ≥ 0.90 + wr_delta ≥ 5pp + not high_impact)
        ──→ APPROVED (manual: CLI or dashboard) ──→ APPLIED ──→ ROLLED_BACK
        ──→ REJECTED
        ──→ EXPIRED (30 days elapsed)
```

Types that can auto-apply: `THRESHOLD_CHANGE`, `ENGINE_WEIGHT`, `REGIME_WEIGHT`
Types always manual: `SCRIPT_PATCH`, `CODE_SUGGESTION`, `INSIGHT`

---

## Scheduled Jobs

| Job | Schedule (IST) | Mode | Purpose |
|---|---|---|---|
| `daily_pipeline` | Mon–Fri 18:00 | — | Main 21-step pipeline |
| `performance_tracking` | Mon–Fri 20:30 | — | Daily metrics to performance_metrics |
| `brain_full_weekly` | Sunday 19:30 | full | Quant + LLM + auto-apply + Telegram |
| `brain_quant_midweek` | Wednesday 19:30 | quant | Fast mid-cycle scan, no LLM cost |
| `weekly_perf_digest` | Sunday 20:00 | — | Telegram performance summary |
| `script_scan_weekly` | Saturday 10:00 | scan | Detect new hardcoded values in scripts |
| `expire_proposals` | Sunday 21:00 | — | Mark stale PENDING proposals EXPIRED |

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
python -m brain.backtester_and_change_manager approve 42
python -m brain.backtester_and_change_manager reject 42
python -m brain.backtester_and_change_manager rollback 42
python -m brain.backtester_and_change_manager history
python -m brain.backtester_and_change_manager history --n 50

# Scheduler
python -m brain.scheduler_yaml                   # start blocking scheduler
python -m brain.scheduler_yaml --list            # list all jobs + schedules
python -m brain.scheduler_yaml --cron            # print crontab equivalent
python -m brain.scheduler_yaml --run <job>       # run one job immediately

# Performance tracking (manual trigger)
python -c "from brain.performance_tracker import run_performance_tracking; run_performance_tracking()"
python -c "from brain.performance_tracker import send_weekly_telegram_summary; send_weekly_telegram_summary()"
```

---

## Safety Guarantees (Hardcoded — Cannot Be Overridden)

| Guard | Value | Enforced In |
|---|---|---|
| Max threshold change per cycle | ±30% of current value | `backtester_and_change_manager.py` |
| Min signals for any finding | 15 | `quant_analyzer.py` |
| Min signals in backtest cohort | 10 | `backtester_and_change_manager.py` |
| High-impact flag (>50% signal pool reduction) | always manual | `backtester_and_change_manager.py` |
| SCRIPT_PATCH / CODE_SUGGESTION / INSIGHT | never auto-apply | `backtester_and_change_manager.py` |
| Engine weight bounds | 0.3× – 2.0× | `backtester_and_change_manager.py` |
| LLM confidence cap by sample size | n<20→0.60, n<50→0.75, n<100→0.85 | `brain_prompt.py` |
| LLM reasoning chain required per proposal | rejects without it | `llm_synthesizer.py` |
| Master kill switch | halts all brain activity | `brain_engine.py` |

---

## File Reference

| File | Role | Entry Point? |
|---|---|---|
| `brain_engine.py` | Orchestrator — the only file you need to call | Yes |
| `brain_prompt.py` | Master LLM persona injected into every AI call | No |
| `data_aggregator.py` | SELECT * data load + forward return computation | No |
| `dynamic_registry.py` | Live discovery of engines/tables/fields/thresholds | No |
| `quant_analyzer.py` | 12 statistical analyses across all loaded data | No |
| `backtester_and_change_manager.py` | Validation + proposal lifecycle + CLI | Yes (CLI) |
| `llm_synthesizer.py` | LLM call via ai_router for cross-variable synthesis | No |
| `script_scanner.py` | AST-based scanner for hardcoded values | No |
| `performance_tracker.py` | Daily/weekly/monthly metric computation | Callable |
| `position_manager.py` | Telegram button handling + open/close position ops | Callable |
| `scheduler.yaml` | YAML schedule config for all jobs | Config |
| `scheduler_yaml.py` | APScheduler runner that reads scheduler.yaml | Yes |
| `schema.sql` | DB schema — run ONCE in Supabase | SQL |
| `schema_rpc.sql` | RPC helper for table discovery — run ONCE after schema.sql | SQL |
