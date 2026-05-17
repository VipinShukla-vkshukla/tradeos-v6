// app/api/profile/route.ts
// GET /api/profile?table=signal_log
// GET /api/profile           (profiles all tables)
//
// For each table, returns column-level stats:
//   - data_type (numeric / categorical / boolean / datetime / text / jsonb)
//   - cardinality (how many distinct values)
//   - null_rate
//   - min / max / mean (numerics)
//   - top_values (categoricals)
//   - is_time_series (has date/timestamp column)
//   - row_count
//
// This is what lets the frontend decide WHAT to draw, not just how to draw it.

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

// Canonical TradeOS tables — ordered by analytical importance
const ALL_TABLES = [
  'signal_log', 'master_shortlist', 'open_positions', 'closed_positions',
  'brain_proposals', 'performance_metrics', 'market_regime', 'lessons',
  'ai_model_performance', 'brain_analysis_log', 'fii_dii_flow',
  'sector_strength', 'system_config', 'msl_history', 'regime_history',
];

export interface ColumnProfile {
  name: string;
  pg_type: string;               // raw postgres type
  inferred_type:                 // our classification
    | 'numeric' | 'categorical' | 'boolean' | 'datetime' | 'text' | 'jsonb' | 'array';
  is_primary_date: boolean;      // is this the main time axis?
  null_rate: number;             // 0–1
  cardinality: number;           // distinct value count
  // Numeric stats
  min?: number;
  max?: number;
  mean?: number;
  // Categorical stats
  top_values?: Array<{ value: string; count: number }>;
  // Sample values
  samples?: unknown[];
}

export interface TableProfile {
  table: string;
  row_count: number;
  is_time_series: boolean;       // has a date/timestamp column with data
  primary_date_col: string | null;
  columns: ColumnProfile[];
  // Derived signals for the viz engine
  has_numeric_cols: boolean;
  has_categorical_cols: boolean;
  has_jsonb_cols: boolean;
  recommended_viz: string[];     // 'line_chart' | 'bar_chart' | 'donut' | 'scatter' | 'table' | 'kpi_cards'
  profiled_at: string;
}

// Map postgres types to our classification
function inferType(pgType: string): ColumnProfile['inferred_type'] {
  if (['numeric', 'double precision', 'real', 'integer', 'bigint', 'smallint', 'decimal'].includes(pgType)) return 'numeric';
  if (pgType === 'boolean') return 'boolean';
  if (['timestamp with time zone', 'timestamp without time zone', 'timestamptz'].includes(pgType)) return 'datetime';
  if (pgType === 'date') return 'datetime';
  if (pgType === 'jsonb' || pgType === 'json') return 'jsonb';
  if (pgType === 'ARRAY') return 'array';
  return 'text';  // text, varchar, etc → could be categorical or free text
}

// Profile a single table
async function profileTable(
  supabase: ReturnType<typeof getClient>,
  table: string
): Promise<TableProfile | null> {
  try {
    // 1. Get column metadata
    const { data: cols, error: colErr } = await supabase
	  .rpc('get_table_columns', { p_table_name: table }) as {
		data: Array<{ column_name: string; data_type: string; is_nullable: string }> | null;
		error: unknown;
	  };

    if (colErr || !cols || cols.length === 0) return null;

    // 2. Get row count + sample (50 rows is enough for profiling)
    const { data: sample, error: sErr, count } = await supabase
      .from(table as never)
      .select('*', { count: 'exact' })
      .limit(50) as { data: Record<string, unknown>[] | null; error: unknown; count: number | null };

    if (sErr || !sample) return null;
    const rowCount = count ?? sample.length;
    if (rowCount === 0) return null;

    // 3. Profile each column
    const colProfiles: ColumnProfile[] = [];
    let primaryDateCol: string | null = null;

    for (const col of cols) {
      const inferredType = inferType(col.data_type);
      const values = sample.map((r) => r[col.column_name]).filter((v) => v !== null && v !== undefined);
      const nullRate = 1 - values.length / sample.length;
      const unique = new Set(values.map(String));
      const cardinality = unique.size;

      const profile: ColumnProfile = {
        name: col.column_name,
        pg_type: col.data_type,
        inferred_type: inferredType === 'text' && cardinality <= 15 ? 'categorical' : inferredType,
        is_primary_date: false,
        null_rate: nullRate,
        cardinality,
        samples: values.slice(0, 5),
      };

      // Numeric stats
      if (inferredType === 'numeric') {
        const nums = values.map(Number).filter((n) => !isNaN(n));
        if (nums.length > 0) {
          profile.min = Math.min(...nums);
          profile.max = Math.max(...nums);
          profile.mean = nums.reduce((a, b) => a + b, 0) / nums.length;
        }
      }

      // Categorical top values
      if (profile.inferred_type === 'categorical' || inferredType === 'boolean') {
        const counts: Record<string, number> = {};
        values.forEach((v) => { const k = String(v); counts[k] = (counts[k] ?? 0) + 1; });
        profile.top_values = Object.entries(counts)
          .sort(([, a], [, b]) => b - a)
          .slice(0, 8)
          .map(([value, count]) => ({ value, count }));
      }

      // Identify primary date column (prefer 'date', then 'created_at', then first datetime)
      if (inferredType === 'datetime') {
        if (col.column_name === 'date' || col.column_name === 'metric_date' || col.column_name === 'run_date') {
          profile.is_primary_date = true;
          primaryDateCol = col.column_name;
        } else if (!primaryDateCol && col.column_name === 'created_at') {
          profile.is_primary_date = true;
          primaryDateCol = col.column_name;
        }
      }

      colProfiles.push(profile);
    }

    // If no primary date set but we have a datetime col, use first
    if (!primaryDateCol) {
      const firstDate = colProfiles.find((c) => c.inferred_type === 'datetime');
      if (firstDate) { firstDate.is_primary_date = true; primaryDateCol = firstDate.name; }
    }

    const hasNumeric = colProfiles.some((c) => c.inferred_type === 'numeric');
    const hasCategorical = colProfiles.some((c) => c.inferred_type === 'categorical');
    const hasJsonb = colProfiles.some((c) => c.inferred_type === 'jsonb');
    const isTimeSeries = !!primaryDateCol;

    // Recommend viz types based on column profile
    const recommended: string[] = [];
    if (isTimeSeries && hasNumeric) recommended.push('line_chart');
    if (hasCategorical && hasNumeric) recommended.push('bar_chart');
    if (hasCategorical && !hasNumeric) recommended.push('donut_chart');
    const numericCols = colProfiles.filter((c) => c.inferred_type === 'numeric');
    if (numericCols.length >= 2) recommended.push('scatter_plot');
    if (rowCount <= 200) recommended.push('table');
    recommended.push('kpi_cards');  // always useful

    return {
      table,
      row_count: rowCount,
      is_time_series: isTimeSeries,
      primary_date_col: primaryDateCol,
      columns: colProfiles,
      has_numeric_cols: hasNumeric,
      has_categorical_cols: hasCategorical,
      has_jsonb_cols: hasJsonb,
      recommended_viz: recommended,
      profiled_at: new Date().toISOString(),
    };
  } catch {
    return null;
  }
}

export async function GET(req: NextRequest) {
  try {
    const supabase = getClient();
    const { searchParams } = new URL(req.url);
    const targetTable = searchParams.get('table');

    if (targetTable) {
      // Profile a single table
      const profile = await profileTable(supabase, targetTable);
      if (!profile) return NextResponse.json({ message: `Table ${targetTable} not found or empty` }, { status: 404 });
      return NextResponse.json(profile);
    }

    // Profile all tables in parallel (with concurrency limit)
    const results: TableProfile[] = [];
    const BATCH = 4;
    for (let i = 0; i < ALL_TABLES.length; i += BATCH) {
      const batch = ALL_TABLES.slice(i, i + BATCH);
      const profiles = await Promise.all(batch.map((t) => profileTable(supabase, t)));
      profiles.forEach((p) => { if (p) results.push(p); });
    }

    return NextResponse.json(results);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Profile failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}
