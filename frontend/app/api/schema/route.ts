// app/api/schema/route.ts
// GET /api/schema — returns all tables + columns from Supabase information_schema
// Used by ViewWizard to populate table/column pickers.
// Uses service role key to bypass RLS on information_schema.

import { NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

// All 29 TradeOS tables — hardcoded as the authoritative list.
// information_schema queries via supabase-js are unreliable across versions;
// this approach is faster and always accurate.
const TRADEOS_TABLES = [
  'ai_context', 'ai_model_performance', 'brain_analysis_log', 'brain_proposals',
  'brain_script_registry', 'chartink_raw_data', 'closed_positions', 'config_change_log',
  'data_anomalies', 'event_calendar', 'evolution_proposals', 'fii_dii_flow',
  'global_cues', 'industry_strength', 'lessons', 'macro_indicators', 'market_news',
  'market_regime', 'master_shortlist', 'ml_training_log', 'msl_computed', 'msl_history',
  'nifty_total_market', 'nifty_upcoming_events', 'nse_holidays', 'open_positions',
  'order_history', 'performance_metrics', 'price_history_yf', 'raw_prices',
  'regime_history', 'safety_lists', 'scanner_signals', 'screener_performance',
  'script_change_log', 'sector_strength', 'shadow_trades', 'signal_log',
  'stock_data_daily', 'strategy_config', 'system_config',
];

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase service role not configured');
  return createClient(url, key);
}

export async function GET() {
  try {
    const supabase = getServiceClient();

    // Query information_schema for column metadata
    const { data, error } = await supabase
      .from('information_schema.columns' as never)
      .select('table_name, column_name, data_type, is_nullable, column_default')
      .in('table_name', TRADEOS_TABLES)
      .eq('table_schema', 'public')
      .order('table_name')
      .order('ordinal_position');

    if (error) {
      // Fallback: try raw SQL via rpc if available
      console.error('Schema query error:', error);
      throw error;
    }

    // Group columns by table
    const tableMap: Record<string, {
      name: string;
      schema: string;
      columns: Array<{
        name: string;
        type: string;
        nullable: boolean;
      }>;
    }> = {};

    for (const row of (data || []) as Array<{
      table_name: string;
      column_name: string;
      data_type: string;
      is_nullable: string;
    }>) {
      if (!tableMap[row.table_name]) {
        tableMap[row.table_name] = {
          name: row.table_name,
          schema: 'public',
          columns: [],
        };
      }
      tableMap[row.table_name].columns.push({
        name: row.column_name,
        type: row.data_type,
        nullable: row.is_nullable === 'YES',
      });
    }

    const tables = Object.values(tableMap);

    // If information_schema query returned nothing (RLS blocked),
    // return the known schema subset that matters most for the wizard.
    if (tables.length === 0) {
      return NextResponse.json(getFallbackSchema());
    }

    return NextResponse.json(tables);
  } catch {
    // Always return something useful — the hardcoded fallback schema
    return NextResponse.json(getFallbackSchema());
  }
}

// Fallback: key tables with their most important columns
// Used when information_schema is not accessible via anon/service key
function getFallbackSchema() {
  return [
    {
      name: 'open_positions',
      schema: 'public',
      columns: [
        { name: 'symbol', type: 'text', nullable: false },
        { name: 'company_name', type: 'text', nullable: true },
        { name: 'sector', type: 'text', nullable: true },
        { name: 'strategy', type: 'text', nullable: true },
        { name: 'entry_date', type: 'date', nullable: true },
        { name: 'entry_price', type: 'numeric', nullable: true },
        { name: 'actual_qty', type: 'numeric', nullable: true },
        { name: 'current_price', type: 'numeric', nullable: true },
        { name: 'unrealized_pnl', type: 'numeric', nullable: true },
        { name: 'pnl_pct', type: 'numeric', nullable: true },
        { name: 'lifecycle', type: 'text', nullable: true },
        { name: 'active_sl', type: 'numeric', nullable: true },
        { name: 'target_1', type: 'numeric', nullable: true },
        { name: 'synced_at', type: 'timestamp with time zone', nullable: true },
      ],
    },
    {
      name: 'closed_positions',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'symbol', type: 'text', nullable: false },
        { name: 'company_name', type: 'text', nullable: true },
        { name: 'sector', type: 'text', nullable: true },
        { name: 'strategy', type: 'text', nullable: true },
        { name: 'entry_date', type: 'date', nullable: true },
        { name: 'entry_price', type: 'numeric', nullable: true },
        { name: 'actual_qty', type: 'numeric', nullable: true },
        { name: 'exit_date', type: 'date', nullable: true },
        { name: 'exit_price', type: 'numeric', nullable: true },
        { name: 'realized_pnl', type: 'numeric', nullable: true },
        { name: 'pnl_pct', type: 'numeric', nullable: true },
        { name: 'exit_reason', type: 'text', nullable: true },
      ],
    },
    {
      name: 'signal_log',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'date', type: 'date', nullable: false },
        { name: 'symbol', type: 'text', nullable: false },
        { name: 'company_name', type: 'text', nullable: true },
        { name: 'sector', type: 'text', nullable: true },
        { name: 'strategy', type: 'text', nullable: true },
        { name: 'signal_type', type: 'text', nullable: true },
        { name: 'signal_subtype', type: 'text', nullable: true },
        { name: 'score', type: 'numeric', nullable: true },
        { name: 'regime', type: 'text', nullable: true },
        { name: 'ai_conviction', type: 'text', nullable: true },
        { name: 'ai_conviction_reason', type: 'text', nullable: true },
        { name: 'filter_reason', type: 'text', nullable: true },
        { name: 'holding_score', type: 'numeric', nullable: true },
        { name: 'breakout_readiness', type: 'numeric', nullable: true },
        { name: 'created_at', type: 'timestamp with time zone', nullable: true },
      ],
    },
    {
      name: 'master_shortlist',
      schema: 'public',
      columns: [
        { name: 'date', type: 'date', nullable: false },
        { name: 'symbol', type: 'text', nullable: false },
        { name: 'company_name', type: 'text', nullable: true },
        { name: 'sector', type: 'text', nullable: true },
        { name: 'strategy_source', type: 'text', nullable: true },
        { name: 'current_price', type: 'numeric', nullable: true },
        { name: 'final_score', type: 'numeric', nullable: true },
        { name: 'priority_rank', type: 'integer', nullable: true },
        { name: 'momentum_state', type: 'text', nullable: true },
        { name: 'entry_timing_type', type: 'text', nullable: true },
        { name: 'lifecycle', type: 'text', nullable: true },
        { name: 'ai_conviction', type: 'text', nullable: true },
        { name: 'active_regime', type: 'text', nullable: true },
        { name: 'trade_allowed', type: 'boolean', nullable: true },
      ],
    },
    {
      name: 'brain_proposals',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'proposal_type', type: 'text', nullable: true },
        { name: 'target_key', type: 'text', nullable: true },
        { name: 'current_value', type: 'text', nullable: true },
        { name: 'proposed_value', type: 'text', nullable: true },
        { name: 'rationale', type: 'text', nullable: true },
        { name: 'confidence', type: 'numeric', nullable: true },
        { name: 'status', type: 'text', nullable: true },
        { name: 'high_impact', type: 'boolean', nullable: true },
        { name: 'priority', type: 'integer', nullable: true },
        { name: 'created_at', type: 'timestamp with time zone', nullable: true },
      ],
    },
    {
      name: 'market_regime',
      schema: 'public',
      columns: [
        { name: 'date', type: 'date', nullable: false },
        { name: 'regime', type: 'text', nullable: true },
        { name: 'computed_regime', type: 'text', nullable: true },
        { name: 'regime_score_computed', type: 'numeric', nullable: true },
        { name: 'nifty_price', type: 'numeric', nullable: true },
        { name: 'india_vix', type: 'numeric', nullable: true },
        { name: 'above_200dma_pct', type: 'double precision', nullable: true },
        { name: 'nifty_5d_chg_pct', type: 'double precision', nullable: true },
      ],
    },
    {
      name: 'performance_metrics',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'metric_date', type: 'date', nullable: false },
        { name: 'grain', type: 'text', nullable: true },
        { name: 'win_rate_overall', type: 'numeric', nullable: true },
        { name: 'signals_generated', type: 'integer', nullable: true },
        { name: 'avg_fwd_ret_5d', type: 'numeric', nullable: true },
        { name: 'engine_stats', type: 'jsonb', nullable: true },
        { name: 'market_regime', type: 'text', nullable: true },
        { name: 'nifty_5d_ret', type: 'numeric', nullable: true },
      ],
    },
    {
      name: 'system_config',
      schema: 'public',
      columns: [
        { name: 'key', type: 'text', nullable: false },
        { name: 'value', type: 'text', nullable: true },
        { name: 'description', type: 'text', nullable: true },
        { name: 'updated_at', type: 'timestamp with time zone', nullable: true },
      ],
    },
    {
      name: 'lessons',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'date', type: 'date', nullable: false },
        { name: 'scenario_type', type: 'text', nullable: true },
        { name: 'trigger_event', type: 'text', nullable: true },
        { name: 'scenario_context', type: 'text', nullable: true },
        { name: 'what_happened', type: 'text', nullable: true },
        { name: 'corrective_rule', type: 'text', nullable: true },
        { name: 'is_active', type: 'boolean', nullable: true },
        { name: 'confidence', type: 'numeric', nullable: true },
        { name: 'created_at', type: 'timestamp with time zone', nullable: true },
      ],
    },
    {
      name: 'fii_dii_flow',
      schema: 'public',
      columns: [
        { name: 'date', type: 'date', nullable: false },
        { name: 'fii_net_cr', type: 'numeric', nullable: true },
        { name: 'dii_net_cr', type: 'numeric', nullable: true },
        { name: 'fii_net_5d', type: 'numeric', nullable: true },
        { name: 'fii_net_20d', type: 'numeric', nullable: true },
        { name: 'fii_flag', type: 'text', nullable: true },
      ],
    },
    {
      name: 'ai_model_performance',
      schema: 'public',
      columns: [
        { name: 'id', type: 'bigint', nullable: false },
        { name: 'date', type: 'date', nullable: false },
        { name: 'provider', type: 'text', nullable: true },
        { name: 'model', type: 'text', nullable: true },
        { name: 'calls_today', type: 'integer', nullable: true },
        { name: 'cost_today', type: 'numeric', nullable: true },
        { name: 'accuracy', type: 'numeric', nullable: true },
        { name: 'avg_confidence', type: 'numeric', nullable: true },
      ],
    },
  ];
}
