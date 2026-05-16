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
  locked_profit: number;
  lifecycle: string;                 // HOLD | ADD | REDUCE | EXIT | DEVELOPING
  sl_type: string;
  initial_sl_atr: number;
  high_water_mark: number;
  active_sl: number;
  exit_signal?: string;
  action_required?: string;
  event_risk?: string;
  upcoming_news?: string;
  target_1?: number;
  target_2?: number;
  target_3?: number;
  target_price?: number;
  target_pct?: number;
  target_hit?: boolean;
  status: string;
  synced_at: string;
  kite_qty?: number;
  signal_id?: number;
  signal_date?: string;
  signal_subtype?: string;
  original_qty?: number;
  current_qty?: number;
  partial_bookings?: unknown;
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
  pnl_pct: number;
  high_water_mark?: number;
  max_favorable_excursion?: number;
  lifecycle_at_entry?: string;
  entry_timing_type?: string;
  reentry_mode?: string;
  expected_r_at_entry?: number;
  exit_reason?: string;
  signal_date?: string;
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
