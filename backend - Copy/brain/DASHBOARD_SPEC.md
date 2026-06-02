# TradeOS Intelligence Console — Dashboard Specification v2
## For Frontend AI Tool Implementation

---

## Stack Recommendation
- **Framework**: Next.js 14 (App Router) or React + Vite
- **UI Components**: shadcn/ui + Tailwind CSS
- **Charts**: Recharts (primary), Tremor (KPI cards)
- **Tables**: TanStack Table v8 (sorting, filtering, pagination)
- **Realtime**: Supabase Realtime JS client (live signal updates)
- **Backend calls**: All writes go through FastAPI endpoints (never direct Supabase
  for mutation — this preserves audit trail). Reads can use Supabase JS client directly.

---

## API Endpoints (FastAPI — build these)

```
# Performance
GET  /api/performance/metrics?grain=daily&days=90
GET  /api/performance/engine-stats?days=90
GET  /api/performance/score-correlation?days=90
GET  /api/performance/regime-accuracy?days=90

# Brain Engine
GET  /api/brain/proposals?status=PENDING
POST /api/brain/proposals/{id}/approve
POST /api/brain/proposals/{id}/reject
POST /api/brain/proposals/{id}/rollback
GET  /api/brain/analysis-log?limit=20
GET  /api/brain/config-changes?days=90

# Positions (all writes → position_manager.py functions)
GET  /api/positions/open
POST /api/positions/open          body: {signal_id, entry_price_actual?}
PUT  /api/positions/{id}/price    body: {actual_price}
POST /api/positions/{id}/close    body: {exit_price, exit_reason, quantity?}
GET  /api/positions/closed?days=90

# Config (writes always log to config_change_log)
GET  /api/config
PUT  /api/config/{key}            body: {value, reason}  → logs to config_change_log
                                  changed_by = "dashboard_manual"

# MSL / Signals
GET  /api/signals?date=&type=
GET  /api/msl/current
GET  /api/ai-context?symbol=__FINAL_PICKS__&limit=7
```

**Config write endpoint must always log:**
```python
@app.put("/api/config/{key}")
async def update_config(key: str, body: ConfigUpdate):
    sb = get_supabase()
    # Get old value first
    old = sb.table("system_config").select("value").eq("key", key).execute()
    old_val = old.data[0]["value"] if old.data else None
    # Write new value
    sb.table("system_config").upsert({"key": key, "value": body.value}).execute()
    # ALWAYS log lineage — this is the non-negotiable rule
    sb.table("config_change_log").insert({
        "key":        key,
        "old_value":  old_val,
        "new_value":  body.value,
        "changed_by": "dashboard_manual",
        "reason":     body.reason or "Manual update via dashboard",
    }).execute()
    return {"ok": True}
```

---

## Self-Awareness Pattern (apply to every panel)

Every chart/table component gets a `DataGuard` wrapper:

```jsx
// DataGuard.jsx — wrap every panel with this
function DataGuard({ data, minRows = 10, coverage = 100, children, name }) {
  if (!data || data.length === 0)
    return <EmptyState message={`No ${name} data yet. Run the pipeline to populate.`} />
  if (data.length < minRows)
    return <LowDataWarning n={data.length} required={minRows} metric={name} />
  if (coverage < 20)
    return <LowCoverageWarning pct={coverage} message="Most signals lack confirmed outcomes. Results unreliable." />
  return children
}
```

---

## Tab 1: System Performance

### KPI Row (top of page)
```
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│  Overall Win %  │ │  Avg Fwd Ret    │ │  Score→Return r │ │  Active Signals │
│  67.3%  +4.2pp  │ │   +4.1% D+10   │ │   0.41 ↑        │ │   12 open       │
│  vs last week   │ │   (D+10 cohort) │ │   (improving)   │ │   3 PRIME       │
└─────────────────┘ └─────────────────┘ └─────────────────┘ └─────────────────┘
```
Data source: `performance_metrics` (weekly grain, last 2 rows for delta)

### Win Rate Trend (Line Chart)
- X axis: week (last 26 weeks)
- Y axis: win_rate_overall %
- Secondary line: win_rate_prime %
- Confidence band: lighter fill when outcome_coverage_pct < 40%
- Data: `SELECT metric_date, win_rate_overall, win_rate_prime, outcome_coverage_pct FROM performance_metrics WHERE grain='weekly' ORDER BY metric_date`

### Signal Type Breakdown (Grouped Bar Chart)
- Groups: PRIME / BREAKOUT / STAGED / REENTRY
- Bars per group: Win Rate % + Count (dual axis)
- Bar opacity = outcome_coverage_pct (semitransparent when low sample)
- Data: `performance_metrics.win_rate_*` fields by week

### Engine Leaderboard (Ranked Table)
Columns: Engine | Win Rate | Count | Avg Return | vs Baseline | Trend (▲▼)
- Parse `engine_stats` JSONB from `performance_metrics`
- Sort by win_rate desc
- Color: green >60%, yellow 40-60%, red <40%
- Trend: compare this week vs last week same engine

### Score Predictiveness Scatter
- X: score_adjusted (from signal_log, last 90d)
- Y: max_fwd_return
- Color points by signal_type
- Show Pearson r in corner
- Warning banner if r < 0.15: "Score is not predicting returns — brain needs recalibration"
- DataGuard: minRows=30

---

## Tab 2: Positions & P&L

### Open Positions Table
Columns: Symbol | Entry Date | Entry Price | Signal Price | Diff | Strategy |
         Stop Loss | Target | Regime | AI Conviction | Days Held | Unrealized P&L* | Actions

*Unrealized P&L: fetch current price from stock_data_daily WHERE date = MAX(date) AND symbol = row.symbol

Actions column per row:
- [💰 Update Price] → opens price input modal → PUT /api/positions/{id}/price
- [🔴 Exit] → opens exit modal with price input → POST /api/positions/{id}/close
- [📝 Note] → text input → PATCH open_positions.notes

Price Update Modal:
```
┌──────────────────────────────────────┐
│  Update Entry Price — TIMKEN         │
│                                      │
│  Signal price:  ₹3,595               │
│  Actual fill:   [    3,610    ]  ₹   │
│                                      │
│  This updates entry_price_actual.    │
│  P&L calculations will use this.     │
│                                      │
│           [Cancel]  [Update Price]   │
└──────────────────────────────────────┘
```

### Closed Positions Table
Columns: Symbol | Entry | Exit | Days | Entry Price | Exit Price | P&L % | P&L ₹ | Exit Reason | Strategy
- Color P&L: green positive, red negative
- Filter: date range, strategy, exit_reason
- Summary row: total trades, avg P&L %, win rate, total ₹ P&L

### P&L Charts
- Monthly P&L bar chart (from closed_positions, group by month)
- Cumulative P&L line (running sum of pnl_abs)
- Win/Loss pie by strategy
- DataGuard: minRows=5 for meaningful charts

---

## Tab 3: AI Intelligence

### Conviction Accuracy (Line Chart)
- Three lines: HIGH conviction WR, MEDIUM conviction WR, LOW conviction WR
- X: week; Y: win rate %
- Expected shape: HIGH > MEDIUM > LOW (if AI is calibrated)
- If HIGH ≈ LOW: show banner "AI conviction not differentiating — check provider"
- Data: `performance_metrics.ai_high_conv_wr / ai_medium_conv_wr / ai_low_conv_wr`

### Market Intel Timeline
- X: date; Y: Nifty close (from stock_data_daily WHERE symbol='NIFTY 50')
- Overlay: colored dots for each ai_context.__MARKET_INTEL__ entry
  - Green dot = FULL sizing, Yellow = HALF, Red = QUARTER/AVOID
- Hover: show tone summary
- Shows whether AI macro calls aligned with market direction

### AI Provider Comparison (Bar Chart)
- Parse `provider` field from signal_log + outcome_win
- Bar per provider: win rate + count
- Only show providers with n ≥ 10
- DataGuard: hide if no provider diversity

### Self-Improvement Feed (Card List)
- Source: mine ai_context.__FINAL_PICKS__.conviction_reason.self_improvement_notes
- Card: pattern_observed | suggested_rule | sectors | confidence | recurrence count
- Filter: by confidence, by sector
- Action: [Mark Implemented] → updates a notes_status field (add to open_positions or lessons table)
- Sort: recurrence_count desc (recurring AI observations = most important)

### Lesson Library (Searchable Table)
- Source: lessons table (SELECT *)
- Columns: scenario_type | corrective_rule (truncated) | confidence | times_applied | strategy | created_at
- Inline edit: confidence field (double-click to edit → PUT /api/lessons/{id})
- Filter: by strategy, confidence range, scenario_type
- Retire button: sets confidence to 0.0 with confirmation dialog

---

## Tab 4: Brain Engine

### Brain Run Timeline (Dot Timeline)
- Each dot = one brain_analysis_log row
- Dot color: green = had auto-applies, yellow = pending proposals, grey = no proposals
- Dot size = proposals_generated
- X: run_date; hover shows: coverage_pct, proposals_generated, auto_applied, elapsed_sec
- DataGuard: show onboarding message if < 2 runs

### Proposal Queue (Action Table)
Columns: ID | Type | Target Key | Current → Proposed | Confidence | WR Delta | Source | Age | Actions

```
ID  │ Type             │ Target Key                      │ Cur  → New  │ Conf │ WR Δ  │ Actions
────┼──────────────────┼─────────────────────────────────┼─────────────┼──────┼───────┼─────────────────
42  │ THRESHOLD_CHANGE │ signal_threshold_prime_rsi_min  │ 48  → 52   │ 82%  │ +6.3pp│ [✅ Approve] [❌ Reject]
43  │ ENGINE_WEIGHT    │ regime_engine_weights           │ JSON → JSON │ 71%  │ +4.1pp│ [✅ Approve] [❌ Reject]
44  │ SCRIPT_PATCH     │ compute/screen_stocks.py        │ code change │ 70%  │  n/a  │ [View Diff] [Approve]
45  │ INSIGHT          │ FII flow + RSI confluence       │ observation │ 75%  │  n/a  │ [View] [Archive]
```

Approve → POST /api/brain/proposals/{id}/approve
Reject  → POST /api/brain/proposals/{id}/reject
View Diff → modal showing unified diff for SCRIPT_PATCH proposals

### Config Change Audit Log (Timeline Table)
Columns: Timestamp | Key | Old Value | New Value | Changed By | Proposal ID | Actions

- Changed By color: blue=brain_engine, orange=dashboard_manual, green=auto_apply, red=rollback
- [Rollback] button → confirmation dialog → POST /api/brain/proposals/{id}/rollback
- Filter: by key prefix, changed_by, date range
- Expand row: shows full rationale from proposal

### Brain Health KPIs
```
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│  Config Coverage     │ │  Acceptance Rate      │ │  Avg WR Improvement  │
│  68% of threshold    │ │  approved/reviewed    │ │  per applied proposal │
│  keys brain-reviewed │ │  8/11 = 72.7%         │ │  +4.8pp avg          │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

### Before/After Impact Chart (Bar Chart)
- For each applied proposal that has ≥ 14 days of post-apply signal history:
  - Two bars: Win Rate Before (last 30d before apply_date) vs After (30d after)
  - Label: key name + applied date
  - Color: green if after > before, red if after < before
- DataGuard: only render if ≥ 2 applied proposals with sufficient history
- This is the proof the brain is working

### Script Registry Table
- Source: brain_script_registry (SELECT *)
- Columns: Script Path | Purpose | Brain Coverage | Tables Read | Tables Written | Tunable Values Found | Last Scanned
- Coverage badge: FULL=green, PARTIAL=yellow, NONE=red
- Expandable row: shows hardcoded values detected with proposed cfg() key names

---

## Tab 5: Data Management

### Panel: Open Positions Manager
Full CRUD for open_positions table.
- Add new position: form with symbol, entry_date, entry_price, strategy, stop_loss, target_price
- Edit: inline editing for stop_loss, target_price, notes
- All writes → POST/PUT /api/positions/*

### Panel: Config Editor
- Load all system_config rows
- Group by prefix: signal_threshold_prime_*, signal_threshold_reentry_*, brain_*, etc.
- Inline edit: click value → text input → [Save] button
- [Save] shows confirmation: "Change {key} from X to Y? Reason (optional): [___]"
- On confirm → PUT /api/config/{key} (which logs to config_change_log)
- Show last_changed badge per row (from config_change_log join)
- Highlight keys modified by brain (changed_by='brain_engine' or 'auto_apply') in blue

### Panel: Lessons Manager
- Full lessons table with inline confidence editing
- [Add Lesson] form: corrective_rule, scenario_type, strategy, confidence
- [Retire] → sets confidence=0, adds retired_at timestamp

### Panel: Signal Log Explorer
- Read-only signal_log with full filtering
- Filters: date range, signal_type, regime, sector, score range
- Show forward return columns (ret_fwd_5d, 10d, 20d, outcome_label) where populated
- Export to CSV button

---

## Realtime Updates (Supabase Realtime)

Enable live updates for these tables:
```javascript
// In your layout/root component
const sb = createClient(SUPABASE_URL, SUPABASE_KEY)

// Live signal updates
sb.channel('signals')
  .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'signal_log' },
      payload => updateSignalList(payload.new))
  .subscribe()

// Brain proposal status changes
sb.channel('proposals')
  .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'brain_proposals' },
      payload => updateProposalStatus(payload.new))
  .subscribe()

// Position updates
sb.channel('positions')
  .on('postgres_changes', { event: '*', schema: 'public', table: 'open_positions' },
      payload => refreshPositions())
  .subscribe()
```

---

## Design Tokens (pass to frontend AI)

```
Background:  #0a0f1e  (dark navy)
Surface:     #111827  (card background)
Border:      #1f2937
Text Primary: #f9fafb
Text Muted:  #6b7280
Accent Blue: #3b82f6  (brain proposals, auto-apply)
Green:       #10b981  (wins, approvals, positive delta)
Yellow:      #f59e0b  (caution, manual review, low coverage)
Red:         #ef4444  (losses, rejections, warnings)
Orange:      #f97316  (manual dashboard changes in audit log)
```

---

## Implementation Priority Order
1. Tab 5 (Data Management) — immediate operational value
2. Tab 2 (Positions) — closes the feedback loop for P&L
3. Tab 4 Brain → Proposal Queue — needed to act on brain proposals
4. Tab 1 (Performance) — visibility into system quality
5. Tab 3 (AI Intelligence) — deeper insight layer
6. Tab 4 Brain → Before/After Impact — needs 4+ weeks of applied proposals
