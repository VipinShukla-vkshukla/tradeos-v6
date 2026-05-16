// TradeOS API Types

import type {
  Position,
  Signal,
  EngineStats,
  PerformanceMetrics,
  BrainProposal,
  ConfigChange,
  Lesson,
  AIContext,
  ConfigEntry,
  WeeklyPerformance,
  SectorPerformance,
  MasterStock,
  PipelineRun,
  MarketRegime,
} from './database';

// Performance API
export interface PerformanceMetricsResponse {
  metrics: PerformanceMetrics;
  weekly_trend: WeeklyPerformance[];
  engine_stats: EngineStats[];
  sector_performance: SectorPerformance[];
}

export interface ScoreCorrelationPoint {
  signal_id: number;
  conviction_score: number;
  actual_pnl_percent: number;
  engine_name: string;
  symbol: string;
  signal_date: string;
}

// Position API
export interface ClosePositionRequest {
  exit_price: number;
  exit_date?: string;
  notes?: string;
}

export interface UpdatePriceRequest {
  current_price: number;
}

export interface PositionWithCurrentPrice extends Position {
  current_price?: number;
  unrealized_pnl?: number;
  unrealized_pnl_percent?: number;
  days_held?: number;
}

// Brain Engine API
export interface ApproveProposalRequest {
  approved: boolean;
  notes?: string;
}

export interface RollbackProposalRequest {
  confirm: boolean;
  reason?: string;
}

export interface BrainAnalysisLog {
  id: number;
  analysis_date: string;
  analysis_type: string;
  findings: string;
  recommendations: string[];
  proposals_generated: number;
  created_at: string;
}

export interface BrainHealthMetrics {
  proposals_pending: number;
  proposals_approved_30d: number;
  proposals_auto_applied: number;
  avg_impact_score: number;
  last_analysis_date: string;
  config_drift_score: number;
}

// Signal API
export interface SignalFilter {
  status?: Signal['status'];
  direction?: Signal['direction'];
  engine_name?: string;
  symbol?: string;
  date_from?: string;
  date_to?: string;
  min_conviction?: number;
}

// AI API
export interface AISuggestRequest {
  task: 'view_improve' | 'chart_build' | 'view_wizard' | 'analyze_data';
  context: Record<string, unknown>;
  current_config?: Record<string, unknown>;
}

export interface AISuggestResponse {
  suggestion: string;
  config?: Record<string, unknown>;
  explanation: string;
  confidence: number;
  provider: string;
  model: string;
}

export interface AIProviderStatus {
  provider: string;
  status: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED';
  latency_ms?: number;
  last_checked: string;
  error_message?: string;
}

// Config API
export interface UpdateConfigRequest {
  value: unknown;
  reason?: string;
}

// Schema API (for dynamic views)
export interface TableSchema {
  name: string;
  columns: ColumnSchema[];
  row_count: number;
  foreign_keys: ForeignKeyRelation[];
}

export interface ColumnSchema {
  name: string;
  data_type: string;
  is_nullable: boolean;
  is_primary_key: boolean;
  default_value?: string;
  description?: string;
}

export interface ForeignKeyRelation {
  column: string;
  references_table: string;
  references_column: string;
  relationship_type: 'one_to_one' | 'one_to_many' | 'many_to_one';
}

export interface SchemaRelationships {
  tables: TableSchema[];
  relationships: ForeignKeyRelation[];
}

// Export types for re-export
export type {
  Position,
  Signal,
  EngineStats,
  PerformanceMetrics,
  BrainProposal,
  ConfigChange,
  Lesson,
  AIContext,
  ConfigEntry,
  WeeklyPerformance,
  SectorPerformance,
  MasterStock,
  PipelineRun,
  MarketRegime,
};
