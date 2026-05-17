// lib/autoViz.ts
// The intelligence layer between data profiling and rendering.
//
// Given a TableProfile, this module decides:
//   - What charts to render
//   - Which columns go on which axes
//   - What chart type fits best
//   - What title/description to generate
//   - What KPI cards to surface
//
// No AI call needed here — pure rule-based logic on column profiles.
// AI is used separately (api/ai/analyze) for narrative insights.

import type { TableProfile, ColumnProfile } from '@/app/api/profile/route';

export type ChartType =
  | 'line_chart'
  | 'area_chart'
  | 'bar_chart'
  | 'stacked_bar'
  | 'donut_chart'
  | 'scatter_plot'
  | 'kpi_row'
  | 'data_table'
  | 'heatmap';

export interface AxisConfig {
  column: string;
  label: string;
  format?: 'currency_inr' | 'percent' | 'number' | 'date' | 'text';
}

export interface ChartDecision {
  id: string;
  chart_type: ChartType;
  title: string;
  description: string;
  x_axis?: AxisConfig;
  y_axes: AxisConfig[];
  color_by?: string;        // column to use for color grouping
  aggregate?: 'sum' | 'avg' | 'count' | 'max' | 'min';
  group_by?: string;        // column to group by before aggregating
  order_by?: string;
  limit?: number;
  priority: number;         // lower = shown first
  reasoning: string;        // why this chart was chosen
}

export interface KPIDecision {
  column: string;
  title: string;
  format: 'currency_inr' | 'percent' | 'number' | 'text';
  aggregate: 'sum' | 'avg' | 'max' | 'min' | 'count' | 'latest';
  reasoning: string;
}

export interface AutoVizPlan {
  table: string;
  kpis: KPIDecision[];
  charts: ChartDecision[];
  suggested_title: string;
  suggested_description: string;
}

// ─── Format detector ──────────────────────────────────────────────────────
function detectFormat(col: ColumnProfile): AxisConfig['format'] {
  const n = col.name.toLowerCase();
  if (n.includes('pct') || n.includes('percent') || n.includes('rate') || n.includes('_pct')) return 'percent';
  if (n.includes('pnl') || n.includes('value') || n.includes('price') || n.includes('_cr') || n.includes('cost') || n.includes('invested')) return 'currency_inr';
  if (col.inferred_type === 'datetime') return 'date';
  if (col.inferred_type === 'numeric') return 'number';
  return 'text';
}

// ─── Human-readable label from column name ────────────────────────────────
function toLabel(col: string): string {
  return col
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .replace(/Pct$/, '%')
    .replace(/Pnl/i, 'P&L')
    .replace(/Cr$/, '(Cr)');
}

// ─── Main plan generator ──────────────────────────────────────────────────
export function generateVizPlan(profile: TableProfile): AutoVizPlan {
  const charts: ChartDecision[] = [];
  const kpis: KPIDecision[] = [];

  const numericCols = profile.columns.filter((c) => c.inferred_type === 'numeric' && c.null_rate < 0.8);
  const categoricalCols = profile.columns.filter((c) => c.inferred_type === 'categorical' && c.cardinality >= 2 && c.cardinality <= 20);
  const dateCol = profile.columns.find((c) => c.is_primary_date);
  const boolCols = profile.columns.filter((c) => c.inferred_type === 'boolean');

  // ── KPI cards: surface the most meaningful numeric columns ───────────────
  const kpiCandidates = numericCols
    .filter((c) => {
      const n = c.name;
      // Prefer high-signal columns
      return n.includes('pnl') || n.includes('rate') || n.includes('score') ||
        n.includes('pct') || n.includes('count') || n.includes('signals') ||
        n.includes('coverage') || n.includes('confidence');
    })
    .slice(0, 4);

  if (kpiCandidates.length === 0) {
    // Fall back to first 4 numeric cols
    kpiCandidates.push(...numericCols.slice(0, 4));
  }

  kpiCandidates.forEach((col) => {
    const isRate = col.name.includes('rate') || col.name.includes('pct');
    const isPnl = col.name.includes('pnl') || col.name.includes('value');
    kpis.push({
      column: col.name,
      title: toLabel(col.name),
      format: detectFormat(col),
      aggregate: isPnl ? 'sum' : isRate ? 'avg' : 'latest',
      reasoning: `${col.name} is a key numeric metric with ${col.null_rate < 0.1 ? 'high' : 'moderate'} data density`,
    });
  });

  // ── Time series charts: numeric cols over the primary date ───────────────
  if (dateCol && numericCols.length > 0) {
    // Pick up to 3 most meaningful numeric cols for time series
    const tsCols = numericCols
      .filter((c) => !['id', 'ordinal', 'position'].some((x) => c.name.includes(x)))
      .slice(0, 3);

    if (tsCols.length > 0) {
      const isFinancial = tsCols.some((c) => c.name.includes('pnl') || c.name.includes('value'));
      charts.push({
        id: `${profile.table}_timeseries`,
        chart_type: isFinancial ? 'area_chart' : 'line_chart',
        title: `${tsCols.map((c) => toLabel(c.name)).join(' & ')} Over Time`,
        description: `Trend of key metrics from ${profile.table} over ${profile.row_count} data points`,
        x_axis: { column: dateCol.name, label: 'Date', format: 'date' },
        y_axes: tsCols.map((c) => ({ column: c.name, label: toLabel(c.name), format: detectFormat(c) })),
        order_by: dateCol.name,
        priority: 1,
        reasoning: `Table has a time axis (${dateCol.name}) and ${tsCols.length} numeric metric(s) — time series is the natural visualization`,
      });
    }
  }

  // ── Categorical breakdown: numeric by category ───────────────────────────
  if (categoricalCols.length > 0 && numericCols.length > 0) {
    const bestCat = categoricalCols.sort((a, b) => b.cardinality - a.cardinality)[0];
    const bestNum = numericCols.filter((c) => !c.name.includes('id'))[0];

    if (bestCat && bestNum) {
      const isMany = bestCat.cardinality > 6;
      charts.push({
        id: `${profile.table}_by_${bestCat.name}`,
        chart_type: isMany ? 'bar_chart' : 'donut_chart',
        title: `${toLabel(bestNum.name)} by ${toLabel(bestCat.name)}`,
        description: `${toLabel(bestNum.name)} broken down across ${bestCat.cardinality} ${bestCat.name} values`,
        x_axis: { column: bestCat.name, label: toLabel(bestCat.name) },
        y_axes: [{ column: bestNum.name, label: toLabel(bestNum.name), format: detectFormat(bestNum) }],
        group_by: bestCat.name,
        aggregate: bestNum.name.includes('pnl') || bestNum.name.includes('value') ? 'sum' : 'avg',
        priority: 2,
        reasoning: `${bestCat.name} has ${bestCat.cardinality} distinct values — ${isMany ? 'bar' : 'donut'} chart chosen based on cardinality`,
      });
    }
  }

  // ── Multi-category stacked bar (when 2+ categoricals and a numeric) ───────
  if (categoricalCols.length >= 2 && numericCols.length > 0) {
    const [cat1, cat2] = categoricalCols;
    const num = numericCols[0];
    if (cat1.cardinality <= 10 && cat2.cardinality <= 6) {
      charts.push({
        id: `${profile.table}_stacked_${cat1.name}`,
        chart_type: 'stacked_bar',
        title: `${toLabel(num.name)} by ${toLabel(cat1.name)} — stacked by ${toLabel(cat2.name)}`,
        description: `${cat2.cardinality} stacks across ${cat1.cardinality} ${cat1.name} groups`,
        x_axis: { column: cat1.name, label: toLabel(cat1.name) },
        y_axes: [{ column: num.name, label: toLabel(num.name), format: detectFormat(num) }],
        color_by: cat2.name,
        group_by: cat1.name,
        aggregate: 'count',
        priority: 3,
        reasoning: `Two categorical columns with manageable cardinality → stacked bar to show composition`,
      });
    }
  }

  // ── Boolean distribution: shows true/false ratio ─────────────────────────
  if (boolCols.length > 0) {
    const meaningful = boolCols.find((c) =>
      c.name.includes('flag') || c.name.includes('enabled') ||
      c.name.includes('active') || c.name.includes('hit') || c.name.includes('allowed')
    ) ?? boolCols[0];

    charts.push({
      id: `${profile.table}_bool_${meaningful.name}`,
      chart_type: 'donut_chart',
      title: `${toLabel(meaningful.name)} Distribution`,
      description: `True vs False ratio for ${meaningful.name} across ${profile.row_count} rows`,
      y_axes: [{ column: meaningful.name, label: toLabel(meaningful.name) }],
      group_by: meaningful.name,
      aggregate: 'count',
      priority: 4,
      reasoning: `Boolean column with meaningful name — donut shows true/false ratio at a glance`,
    });
  }

  // ── Scatter: correlate two numeric columns ────────────────────────────────
  if (numericCols.length >= 2) {
    const [x, y] = numericCols.filter((c) => !c.name.includes('id'));
    if (x && y && x !== y) {
      charts.push({
        id: `${profile.table}_scatter_${x.name}_vs_${y.name}`,
        chart_type: 'scatter_plot',
        title: `${toLabel(x.name)} vs ${toLabel(y.name)}`,
        description: `Correlation between ${x.name} and ${y.name} — look for patterns`,
        x_axis: { column: x.name, label: toLabel(x.name), format: detectFormat(x) },
        y_axes: [{ column: y.name, label: toLabel(y.name), format: detectFormat(y) }],
        color_by: categoricalCols[0]?.name,
        limit: 200,
        priority: 5,
        reasoning: `Two numeric columns → scatter plot to reveal correlation or clustering`,
      });
    }
  }

  // ── Always add a data table as fallback ───────────────────────────────────
  charts.push({
    id: `${profile.table}_table`,
    chart_type: 'data_table',
    title: `${toLabel(profile.table)} — Raw Data`,
    description: `${profile.row_count} rows from ${profile.table}`,
    y_axes: profile.columns
      .filter((c) => c.null_rate < 0.5 && c.inferred_type !== 'jsonb' && c.inferred_type !== 'array')
      .slice(0, 10)
      .map((c) => ({ column: c.name, label: toLabel(c.name), format: detectFormat(c) })),
    order_by: dateCol?.name,
    limit: 100,
    priority: 99,
    reasoning: 'Raw table always available as fallback for full data inspection',
  });

  // Generate human-readable title/description
  const suggested_title = toLabel(profile.table.replace(/_/g, ' '));
  const suggested_description = [
    `${profile.row_count} rows`,
    profile.is_time_series ? `time series from ${profile.primary_date_col}` : null,
    numericCols.length > 0 ? `${numericCols.length} metrics` : null,
    categoricalCols.length > 0 ? `${categoricalCols.length} dimensions` : null,
  ].filter(Boolean).join(' · ');

  return {
    table: profile.table,
    kpis,
    charts: charts.sort((a, b) => a.priority - b.priority),
    suggested_title,
    suggested_description,
  };
}

// ─── Build a live Supabase query from a ChartDecision ─────────────────────
export interface LiveQuerySpec {
  table: string;
  select: string;
  order?: string;
  limit?: number;
}

export function buildQuerySpec(table: string, decision: ChartDecision): LiveQuerySpec {
  const cols = new Set<string>();

  if (decision.x_axis) cols.add(decision.x_axis.column);
  decision.y_axes.forEach((y) => cols.add(y.column));
  if (decision.color_by) cols.add(decision.color_by);
  if (decision.group_by) cols.add(decision.group_by);

  return {
    table,
    select: cols.size > 0 ? Array.from(cols).join(', ') : '*',
    order: decision.order_by,
    limit: decision.limit ?? (decision.chart_type === 'data_table' ? 100 : 500),
  };
}
