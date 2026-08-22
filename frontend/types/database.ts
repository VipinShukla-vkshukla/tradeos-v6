// TradeOS v6 — Database Types
// Aligned to actual Supabase schema as of May 2026
// Every type here maps 1:1 to a real table column.

// ---------------------------------------------------------------------------
// open_positions
// ---------------------------------------------------------------------------
export interface OpenPosition {
  symbol: string;                    // PK (no numeric id)
  company_name: string;
  sector: string;
  strategy: string;                  // CTL | SBS | TPO | EAP
  entry_date: string;                // date
  entry_price: number;
  proposed_qty: number;
  actual_qty: number;
  invested_value: number;
  current_price: number;
  current_value: number;
  unrealized_pnl: number;
  pnl_pct: number;
  sl_type: string;
  high_water_mark: number;
  active_sl: number;                 // CURRENT stop — moves to breakeven, then trails
  exit_signal?: string | null;
  action_required?: string | null;   // set by position_lifecycle: "BOOK 7 — 1.55R >= 1.5R…"
  target_price?: number | null;
  target_hit?: boolean;
  status: string;
  synced_at: string;
  kite_qty?: number | null;
  signal_id?: number | null;
  signal_date?: string | null;
  original_qty?: number | null;
  current_qty?: number | null;
  partial_bookings?: unknown;

  // ── Risk model (migration 006, written by control/position_lifecycle) ─────
  // planned_stop is the stop the trade was SIZED on and never changes; active_sl
  // is where the stop is now. Showing only one of them hides the single most
  // important thing about a live trade — whether risk has been taken off.
  planned_stop?: number | null;
  planned_target?: number | null;
  planned_risk_pct?: number | null;
  expected_r_at_entry?: number | null;

  // Progress in R. This is the unit the exit rules are written in — the partial
  // book fires at 1.5R, not at a percentage — so it is the honest way to show
  // where a position stands.
  r_multiple_current?: number | null;
  max_favorable_excursion?: number | null;  // best % move seen while open
  max_adverse_excursion?: number | null;    // worst % move seen while open

  // Exit-state machine flags. Together they say which rung of the ladder the
  // trade has climbed, which no price column can express.
  breakeven_moved?: boolean | null;
  trail_activated?: boolean | null;
  partial_booked_qty?: number | null;
  partial_booked_price?: number | null;
  partial_booked_at?: string | null;
  time_stop_date?: string | null;

  sl_breach_alerted?: boolean | null;
  sl_proximity_alerted?: boolean | null;

  // Entry context — what the system believed when it committed capital.
  entry_signal_type?: string | null;
  regime_at_entry?: string | null;
  sector_rank_at_entry?: number | null;

  // PAPER positions are managed by the same exit engine and feed the same
  // learning loop; only the fill was simulated. Shown distinctly because a
  // simulated P&L sitting unlabelled next to a real one is the single most
  // misleading thing this dashboard could display.
  mode?: string | null;              // LIVE | PAPER
  framework?: string | null;         // SWING | INTRADAY
  product?: string | null;
  intraday_strategy?: string | null;
  reconcile_status?: string | null;
  last_reconciled_at?: string | null;
  kite_avg_price?: number | null;
  source?: string | null;

  ai_recommended_action?: string | null;
  ai_action_reason?: string | null;
  ai_action_confidence?: number | null;
  ai_action_urgency?: string | null;
  ai_action_updated_at?: string | null;
}

// ---------------------------------------------------------------------------
// closed_positions
// ---------------------------------------------------------------------------
export interface ClosedPosition {
  id: number;
  symbol: string;
  company_name: string;
  sector: string;
  strategy: string;
  entry_date: string;
  entry_price: number;
  proposed_qty: number;
  actual_qty: number;
  invested_value: number;
  exit_date: string;
  exit_price: number;
  exit_value: number;
  realized_pnl: number;
  // GROSS. Charges are recorded separately because the 70 trades closed before
  // migration 025 have no reconstructable cost — netting them into this column
  // would make old and new rows incomparable while looking identical.
  charges?: number | null;
  pnl_pct: number;
  high_water_mark?: number;
  max_favorable_excursion?: number;
  lifecycle_at_entry?: string;
  entry_timing_type?: string;
  reentry_mode?: string;
  expected_r_at_entry?: number;
  exit_reason?: string;
  // ── Attribution (migration 003/004) ──────────────────────────────────────
  // signal_id/signal_date link a closed trade back to the signal that produced
  // it. NULL means the trade was taken outside the system — which is true of
  // 69 of the first 70 rows, all of which predate signal_log entirely. The
  // Performance tab splits on this so a pre-automation record is never
  // presented as the current system's.
  signal_id?: number | null;
  signal_date?: string | null;
  entry_signal_type?: string | null;
  regime_at_entry?: string | null;
  hold_days?: number | null;
  r_multiple?: number | null;
  max_adverse_excursion?: number | null;
  exit_reason_detail?: string | null;
  source?: string | null;
  planned_stop_at_entry?: number | null;
  planned_target_at_entry?: number | null;
  sector_rank_at_entry?: number | null;
  closed_at?: string | null;
  partial_bookings?: unknown;
  mode?: string | null;
  framework?: string | null;
  intraday_strategy?: string | null;
  // The CONDITION, not just the family — migration 094. `strategy` is SDN
  // for all three of its conditions alike; this is what actually separates
  // an 83%-win-rate condition from an 8%-win one that happened to share an
  // engine label. NULL for SWING (sub_engine is intraday-only vocabulary)
  // and for any row closed before this column existed.
  sub_engine?: string | null;
}

// ---------------------------------------------------------------------------
// signal_log
// ---------------------------------------------------------------------------
export interface Signal {
  id: number;
  date: string;
  symbol: string;
  company_name: string;
  sector: string;
  strategy: string;
  signal_type: string;               // BUY | SELL | HOLD | WATCH
  signal_subtype?: string;           // PRIME_SETUP | BREAKOUT_SETUP | REENTRY_SETUP | STAGED_ENTRY
  position_state?: string;
  score?: number;
  score_adjusted?: number;
  final_score?: number;              // post-AI ranking score on signal_output_daily
  screener_score?: number;
  regime: string;
  regime_warning?: boolean;
  asm_flag?: boolean;
  fo_ban_flag?: boolean;
  ai_conviction?: string;            // HIGH | MEDIUM | LOW
  ai_conviction_reason?: string;
  ai_suggested_action?: string;
  ai_note?: string;
  ai_provider?: string;
  ai_fallback_used?: boolean;
  ai_confidence?: number;
  fii_flag?: string;
  filter_reason?: string;
  entry_timing_type?: string;
  lifecycle?: string;
  momentum_state?: string;
  momentum_phase?: string;
  velocity_state?: string;
  trend_maturity?: string;
  struct_edge?: string;
  holding_score?: number;
  momentum_score?: number;
  institutional_score?: number;
  breakout_readiness?: number;
  risk_score?: number;
  validity_score?: number;
  days_in_list?: number;
  rsi_daily?: number;
  rsi_weekly?: number;
  rsi_monthly?: number;
  adx?: number;
  vol_ratio?: number;
  delivery_pct?: number;
  atr_pct?: number;
  ret_6m?: number;
  dist_sma50?: number;
  industry?: string;
  industry_top5?: boolean;
  created_at: string;

  // ── Trade plan (written by compute_msl → generate_signals → final_snapshot) ─
  // These were the break in the chain: final_snapshot never copied them into
  // signal_output_daily, so send_alerts had no levels to quote and every alert
  // read as a static watchlist entry. They are the difference between "BHEL
  // looks good" and "buy BHEL at 407, stop 384, target 452".
  planned_stop?: number | null;
  planned_target?: number | null;
  planned_risk_pct?: number | null;
  implied_rr?: number | null;
  planned_stop_source?: string | null;   // why a plan is missing, when it is
  entry_zone_low?: number | null;
  entry_zone_high?: number | null;
  current_price?: number | null;
  dist_entry_pct?: number | null;        // how far price is from the zone
  expected_r?: number | null;
  ai_tier?: string | null;               // TIER_1 | TIER_2 | TIER_3
  ai_max_chase_pct?: number | null;
  sector_rank_at_entry?: number | null;
}

// ---------------------------------------------------------------------------
// market_regime  (table name matches — field names differ from old frontend type)
// ---------------------------------------------------------------------------
export type RegimeState =
  | 'TRENDING'
  | 'RISK ON'
  | 'NEUTRAL'
  | 'RECOVERING'
  | 'RISK OFF';

export interface MarketRegime {
  date: string;                      // PK (no numeric id)
  regime: RegimeState;               // computed_regime preferred; fallback to regime
  computed_regime?: RegimeState;
  regime_score?: number;
  regime_score_computed?: number;
  regime_confidence?: number;
  nifty_price?: number;
  nifty_50dma?: number;
  nifty_200dma?: number;
  nifty_weekly_rsi?: number;
  india_vix?: number;
  avg_sector_breadth?: number;
  advance_decline_ratio?: number;
  above_200dma_pct?: number;
  nifty_1d_chg_pct?: number;
  nifty_5d_chg_pct?: number;
  nifty_20d_chg_pct?: number;
  vix_5d_delta?: number;
  midcap_breadth?: number;
  smallcap_breadth?: number;
  banknifty_price?: number;
  banknifty_weekly_rsi?: number;
  nifty_pcr?: number;
  fii_net?: number;
  created_at: string;
}

// ---------------------------------------------------------------------------
// ai_model_performance
// ---------------------------------------------------------------------------
export interface AIModelPerformance {
  id: number;
  date: string;
  provider: string;
  model: string;                     // NB: column is 'model', not 'model_name'
  calls_today: number;               // NB: not 'total_calls'
  cost_today?: number;               // NB: not 'cost_per_call'
  accuracy?: number;
  avg_confidence?: number;
  fallback_used?: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------
// brain_proposals
// ---------------------------------------------------------------------------
export interface BrainProposal {
  id: number;
  analysis_run_id?: string;
  proposal_type: string;             // PARAMETER_TUNE | CONFIG_CHANGE | SCRIPT_PATCH | STRATEGY_ADD
  target_key: string;                // NB: not 'target_component'
  current_value?: string;
  proposed_value?: string;
  rationale?: string;                // NB: not 'description'
  evidence?: unknown;
  backtest_result?: unknown;
  confidence?: number;               // NB: not 'confidence_score'
  status: string;                    // PENDING | APPROVED | REJECTED | APPLIED | ROLLED_BACK
  source?: string;
  priority?: number;
  high_impact?: boolean;
  created_at: string;
  expires_at?: string;
  reviewed_at?: string;
  reviewed_by?: string;              // NB: not 'applied_by'
  applied_at?: string;
  rollback_value?: string;
  script_diff?: string;
}

// ---------------------------------------------------------------------------
// config_change_log
// ---------------------------------------------------------------------------
export interface ConfigChange {
  id: number;
  key: string;                       // NB: not 'config_key'
  old_value: string;
  new_value: string;
  changed_by: string;                // NB: not 'change_source'
  proposal_id?: number;
  reason?: string;                   // NB: not 'change_reason'
  changed_at: string;                // NB: not 'created_at'
  rolled_back_at?: string;
  rolled_back_by?: string;
}

// ---------------------------------------------------------------------------
// lessons
// ---------------------------------------------------------------------------
export interface Lesson {
  id: number;
  date: string;
  scenario_type?: string;            // NB: not 'lesson_type'
  trigger_event?: string;
  linked_event_type?: string;
  impacted_sector?: string;
  scenario_context?: string;         // NB: closest to 'content'
  what_expected?: string;
  what_happened?: string;
  what_failed?: string;
  root_cause?: string;
  corrective_rule?: string;
  observation?: string;
  source?: string;
  is_active: boolean;
  times_applied?: number;
  times_worked?: number;
  confidence?: number;               // NB: numeric 0-1, not enum
  linked_symbols?: string[];         // NB: not 'symbols'
  created_at: string;
}

// ---------------------------------------------------------------------------
// system_config
// ---------------------------------------------------------------------------
export interface ConfigEntry {
  key: string;
  value: string;
  description?: string;
  updated_at: string;                // NB: not 'last_modified'
}

// ---------------------------------------------------------------------------
// master_shortlist  (projected subset — 97 cols total, only UI-relevant ones)
// ---------------------------------------------------------------------------
export interface MasterStock {
  date: string;
  symbol: string;
  company_name: string;
  sector: string;
  strategy_source: string;
  current_price: number;
  final_score: number;
  composite_score?: number;
  priority_rank?: number;
  sector_rank?: number;
  days_in_list?: number;
  momentum_state?: string;
  momentum_phase?: string;
  velocity_state?: string;
  trend_maturity?: string;
  struct_edge?: string;
  entry_timing_type?: string;
  entry_ready?: boolean;
  in_position?: boolean;
  reentry_mode?: string;
  lifecycle?: string;
  expected_r?: number;
  validity_score?: number;
  exec_eligibility?: string;
  ai_conviction?: string;
  ai_conviction_reason?: string;
  ai_suggested_action?: string;
  ai_shortlist_rank?: number;
  active_regime?: string;
  fii_flag?: string;
  trade_allowed?: boolean;
  compute_source?: string;
  computed_at?: string;
}

// ---------------------------------------------------------------------------
// performance_metrics  (engine_stats lives as JSONB column here, no standalone table)
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// View-model shapes for the performance charts.
//
// WinRateTrendChart, EngineLeaderboard, SignalTypeBreakdown and PerformanceTab
// all import these from '@/types/database' — but they were never defined here
// or anywhere else, so every one of those imports resolved to nothing. TypeScript
// erases types at runtime and next.config sets ignoreBuildErrors, so the charts
// still rendered; the props were simply unchecked.
//
// These are not table rows. They are what PerformanceTab's toWeeklyPerformance
// and toEngineStats produce from performance_metrics, which is why they belong
// beside the row types rather than in them.
// ---------------------------------------------------------------------------
export interface WeeklyPerformance {
  week_start: string;
  win_rate: number;        // 0–1, not a percentage
  total_trades: number;
  pnl: number;
  pnl_percent: number;
}

export interface EngineStats {
  engine_name: string;
  total_signals: number;
  executed_signals: number;
  win_count: number;
  loss_count: number;
  win_rate: number;        // 0–1, not a percentage
  avg_pnl_percent: number;
  total_pnl: number;
  last_signal_date: string;
  // Rendered by EngineLeaderboard but not produced by toEngineStats — the
  // pipeline's engine_stats JSONB does not carry it today. Optional so the
  // column degrades to a dash rather than the type lying about what exists.
  sharpe_ratio?: number;
}

export interface EngineStatsEntry {
  engine_name: string;
  total_signals: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  avg_pnl_pct: number;
  total_pnl: number;
}

export interface PerformanceMetricsRow {
  id: number;
  metric_date: string;
  grain: string;                     // 'daily' | 'weekly' | 'monthly'
  signals_generated: number;
  signals_prime: number;
  signals_breakout: number;
  signals_staged: number;
  signals_reentry: number;
  win_rate_overall: number;
  win_rate_prime: number;
  win_rate_breakout: number;
  win_rate_staged: number;
  win_rate_reentry: number;
  avg_fwd_ret_5d?: number;
  avg_fwd_ret_10d?: number;
  avg_fwd_ret_20d?: number;
  engine_stats?: EngineStatsEntry[]; // parsed from JSONB
  score_return_corr?: number;
  market_regime?: string;
  nifty_5d_ret?: number;
  india_vix_avg?: number;
  fii_net_5d?: number;
  brain_proposals_applied?: number;
  created_at: string;
}

// ---------------------------------------------------------------------------
// brain_analysis_log
// ---------------------------------------------------------------------------
export interface BrainAnalysisLog {
  id: number;
  run_id: string;
  run_date: string;
  period_days: number;
  run_mode: string;
  signals_analyzed: number;
  signals_with_outcomes: number;
  coverage_pct: number;
  proposals_generated: number;
  proposals_auto_applied: number;
  llm_insights?: string;
  execution_time_sec?: number;
  errors?: unknown;
  created_at: string;
}

// ---------------------------------------------------------------------------
// msl_history (for trajectory / trend analysis)
// ---------------------------------------------------------------------------
export interface MslHistoryEntry {
  snapshot_date: string;
  symbol: string;
  company_name: string;
  priority_rank: number;
  sector: string;
  strategy_source: string;
  close_price: number;
  final_score: number;
  momentum_state?: string;
  momentum_phase?: string;
  velocity_state?: string;
  trend_maturity?: string;
  struct_edge?: string;
  lifecycle?: string;
  holding_score?: number;
  risk_score?: number;
  breakout_readiness?: number;
  in_position: boolean;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Shared response wrappers
// ---------------------------------------------------------------------------
export interface ApiResponse<T> {
  data: T;
  success: boolean;
  message?: string;
  timestamp: string;
}

export interface PaginatedResponse<T> {
  data: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// ---------------------------------------------------------------------------
// MISSING TABLE NOTE: pipeline_runs does not exist in this schema.
// If needed, create the table or derive from brain_analysis_log.run_date.
// The PipelineRun type below is a stub — do not query it until the table exists.
// ---------------------------------------------------------------------------
export interface PipelineRun {
  id: number;
  pipeline_name: string;
  run_status: 'RUNNING' | 'SUCCESS' | 'FAILED' | 'PARTIAL';
  started_at: string;
  completed_at?: string;
  duration_ms?: number;
  records_processed?: number;
  error_message?: string;
}
