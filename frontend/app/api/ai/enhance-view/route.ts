// app/api/ai/enhance-view/route.ts
//
// AI-powered view enhancement — IMPROVED VERSION
//
// Key fixes over v1:
//   1. Pre-analysis step: server identifies working vs broken KPIs BEFORE calling AI
//   2. Confirmed numeric columns (with real stats) passed explicitly to AI
//   3. Null_rate bug fixed: ((c.null_rate ?? 0) * 100) — was (c.null_rate ?? 0 * 100)
//   4. AI explicitly told to PRESERVE working items, only fix broken ones
//   5. Post-AI validation: every column reference checked against real profile
//   6. Fallback logic: invalid AI KPIs/charts replaced with current-plan equivalents
//   7. Broader sample data (all columns, more rows) so AI sees real data shape

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SERVICE_KEY  = process.env.SUPABASE_SERVICE_ROLE_KEY!;

// ── Types ──────────────────────────────────────────────────────────────────
interface ColumnProfile {
  name: string;
  inferred_type: string;
  cardinality?: number;
  null_rate?: number;
  is_primary_date?: boolean;
}

interface NumericStats {
  min: number;
  max: number;
  avg: number;
  nonNullCount: number;
  sample: number[];
}

interface PreAnalysis {
  columnNames: Set<string>;
  numericColumns: Record<string, NumericStats>;
  workingKPIColumns: string[];   // current KPIs that show real values
  brokenKPIColumns: string[];    // current KPIs showing "—" (no numeric data)
  hasDateColumn: boolean;
  primaryDateColumn: string | null;
}

// ── System-config reader ───────────────────────────────────────────────────
async function getAIConfig(): Promise<Record<string, string>> {
  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);
  const { data } = await supabase.from('system_config').select('key, value');
  return Object.fromEntries((data ?? []).map((r: { key: string; value: string }) => [r.key, r.value]));
}

// ── Pre-analysis: understand the data BEFORE asking AI ────────────────────
// This is the most important addition. We compute exactly which columns have
// real numeric data in the sample, which current KPIs are working vs broken,
// and pass confirmed stats to the AI so it can make informed decisions.
function preAnalyze(
  profile: Record<string, unknown>,
  currentPlan: Record<string, unknown>,
  sampleData: Record<string, unknown>[],
): PreAnalysis {
  const columns = (profile.columns as ColumnProfile[]) ?? [];
  const columnNames = new Set(columns.map((c) => c.name));

  // Compute real numeric stats from sampleData for every column
  const numericColumns: Record<string, NumericStats> = {};
  for (const col of columns) {
    const values = sampleData
      .map((r) => Number(r[col.name]))
      .filter((v) => !isNaN(v) && isFinite(v));
    if (values.length > 0) {
      numericColumns[col.name] = {
        min: Math.min(...values),
        max: Math.max(...values),
        avg: values.reduce((a, b) => a + b, 0) / values.length,
        nonNullCount: values.length,
        sample: values.slice(0, 5),
      };
    }
  }

  // Check which KPIs in the current plan actually have data
  const kpis = (currentPlan.kpis as Array<{ column: string }>) ?? [];
  const workingKPIColumns: string[] = [];
  const brokenKPIColumns: string[]  = [];
  for (const kpi of kpis) {
    if (numericColumns[kpi.column]) workingKPIColumns.push(kpi.column);
    else brokenKPIColumns.push(kpi.column);
  }

  const primaryDateCol = columns.find((c) => c.is_primary_date);

  return {
    columnNames,
    numericColumns,
    workingKPIColumns,
    brokenKPIColumns,
    hasDateColumn: !!primaryDateCol,
    primaryDateColumn: primaryDateCol?.name ?? null,
  };
}

// ── Post-AI validation: fix or discard anything referencing invalid columns ─
// Prevents AI-hallucinated column names from breaking the dashboard.
// For each invalid item, we fall back to the equivalent from currentPlan.
function validateAndRepair(
  aiPlan: Record<string, unknown>,
  currentPlan: Record<string, unknown>,
  analysis: PreAnalysis,
): Record<string, unknown> {
  const { columnNames, numericColumns } = analysis;
  const repairNotes: string[] = [];

  // ── Validate KPIs ────────────────────────────────────────────────────────
  const currentKPIs = (currentPlan.kpis as Array<Record<string, unknown>>) ?? [];
  const aiKPIs      = (aiPlan.kpis   as Array<Record<string, unknown>>) ?? [];
  const validatedKPIs: Array<Record<string, unknown>> = [];

  for (const kpi of aiKPIs) {
    const col = String(kpi.column ?? '');

    if (!columnNames.has(col)) {
      repairNotes.push(`Removed KPI '${col}': column does not exist in table`);
      continue;
    }
    if (!numericColumns[col]) {
      // Column exists but has no numeric data — try to swap in a working fallback
      const fallback = currentKPIs.find(
        (k) => numericColumns[String(k.column ?? '')] &&
               !validatedKPIs.find((v) => v.column === k.column)
      );
      if (fallback) {
        validatedKPIs.push(fallback);
        repairNotes.push(`Replaced KPI '${col}' (no numeric data) with current-plan KPI '${fallback.column}'`);
      } else {
        repairNotes.push(`Removed KPI '${col}': no numeric data found in sample`);
      }
      continue;
    }
    validatedKPIs.push(kpi);
  }

  // Restore any working KPIs that AI silently dropped (up to 4 total)
  for (const workingCol of analysis.workingKPIColumns) {
    if (validatedKPIs.length >= 4) break;
    if (validatedKPIs.find((k) => k.column === workingCol)) continue;
    const original = currentKPIs.find((k) => k.column === workingCol);
    if (original) {
      validatedKPIs.push(original);
      repairNotes.push(`Restored working KPI '${workingCol}' that AI dropped without justification`);
    }
  }

  // ── Validate charts ──────────────────────────────────────────────────────
  const currentCharts = (currentPlan.charts as Array<Record<string, unknown>>) ?? [];
  const aiCharts      = (aiPlan.charts   as Array<Record<string, unknown>>) ?? [];
  const validatedCharts: Array<Record<string, unknown>> = [];

  for (const chart of aiCharts) {
    const issues: string[] = [];
    let chartValid = true;

    // Check x_axis column
    const xAxis = chart.x_axis as Record<string, unknown> | undefined;
    if (xAxis?.column && !columnNames.has(String(xAxis.column))) {
      issues.push(`x_axis column '${xAxis.column}' not found`);
      chartValid = false;
    }

    // Check y_axes columns — filter out invalid ones
    const yAxes = (chart.y_axes as Array<Record<string, unknown>>) ?? [];
    const validYAxes = yAxes.filter((y) => {
      if (!columnNames.has(String(y.column ?? ''))) {
        issues.push(`y_axis column '${y.column}' not found`);
        return false;
      }
      return true;
    });

    if (yAxes.length > 0 && validYAxes.length === 0) {
      chartValid = false; // all y_axes were invalid
    } else if (validYAxes.length < yAxes.length) {
      chart.y_axes = validYAxes; // partial fix
    }

    // Nullify invalid group_by / color_by (non-fatal)
    if (chart.group_by && chart.group_by !== null && !columnNames.has(String(chart.group_by))) {
      repairNotes.push(`Cleared group_by '${chart.group_by}' on chart '${chart.title}': column not found`);
      chart.group_by = null;
    }
    if (chart.color_by && chart.color_by !== null && !columnNames.has(String(chart.color_by))) {
      chart.color_by = null;
    }

    if (chartValid) {
      validatedCharts.push(chart);
    } else {
      // Try to fall back to the current plan chart at the same priority slot
      const priority = Number(chart.priority ?? 99);
      const fallback = currentCharts.find(
        (c) => Math.abs(Number(c.priority ?? 99) - priority) <= 1 &&
               !validatedCharts.find((v) => v.id === c.id)
      );
      if (fallback) {
        validatedCharts.push(fallback);
        repairNotes.push(`Chart '${chart.title}' had invalid columns (${issues.join(', ')}) — restored current-plan chart`);
      } else {
        repairNotes.push(`Removed chart '${chart.title}': ${issues.join(', ')}`);
      }
    }
  }

  // Always ensure a data_table chart exists
  if (!validatedCharts.some((c) => c.chart_type === 'data_table')) {
    const fallbackTable = currentCharts.find((c) => c.chart_type === 'data_table');
    if (fallbackTable) {
      validatedCharts.push(fallbackTable);
      repairNotes.push('Restored data_table chart from current plan (AI omitted it)');
    }
  }

  const existingImprovements = (aiPlan.ai_improvements as string[]) ?? [];
  return {
    ...aiPlan,
    kpis:   validatedKPIs,
    charts: validatedCharts,
    ai_improvements: [
      ...existingImprovements,
      ...repairNotes.map((n) => `[Auto-repaired] ${n}`),
    ],
  };
}

// ── AI provider dispatch ───────────────────────────────────────────────────
async function callAI(prompt: string, cfg: Record<string, string>): Promise<string> {
  const provider = (cfg['ai_active_provider'] || cfg['ai_provider'] || 'anthropic').toLowerCase();

  if (provider === 'anthropic') {
    const res = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': process.env.ANTHROPIC_API_KEY!,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-sonnet-4-20250514',
        max_tokens: 4096,
        messages: [{ role: 'user', content: prompt }],
      }),
      signal: AbortSignal.timeout(30_000),
    });
    const data = await res.json();
    return data.content?.[0]?.text ?? '';
  }

  if (provider === 'deepseek') {
    const res = await fetch('https://api.deepseek.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.DEEPSEEK_API_KEY}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'deepseek-chat',
        max_tokens: 4096,
        messages: [{ role: 'user', content: prompt }],
      }),
      signal: AbortSignal.timeout(30_000),
    });
    const data = await res.json();
    return data.choices?.[0]?.message?.content ?? '';
  }

  if (provider === 'openai') {
    const res = await fetch('https://api.openai.com/v1/chat/completions', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: cfg['openai_model'] || 'gpt-4o',
        max_tokens: 4096,
        messages: [{ role: 'user', content: prompt }],
      }),
      signal: AbortSignal.timeout(30_000),
    });
    const data = await res.json();
    return data.choices?.[0]?.message?.content ?? '';
  }

  throw new Error(`Unknown AI provider: ${provider}`);
}

// ── Prompt builder ─────────────────────────────────────────────────────────
function buildPrompt(
  table: string,
  profile: Record<string, unknown>,
  currentPlan: Record<string, unknown>,
  sampleData: Record<string, unknown>[],
  tabContext: string,
  analysis: PreAnalysis,
): string {
  const columns = (profile.columns as ColumnProfile[]) ?? [];

  // Full column summary with FIXED null_rate calculation
  // BUG FIX: original was (c.null_rate ?? 0 * 100) which due to JS operator
  // precedence evaluates as (c.null_rate ?? 0) — the *100 never applied.
  // Every column showed "0%" null_rate, so AI couldn't distinguish sparse columns.
  const columnSummary = columns.map((c) => {
    const stats = analysis.numericColumns[c.name];
    const samples = sampleData
      .slice(0, 8)
      .map((r) => r[c.name])
      .filter((v) => v !== null && v !== undefined)
      .map(String)
      .join(', ');

    const nullPct = ((c.null_rate ?? 0) * 100).toFixed(0);

    const statsStr = stats
      ? ` | HAS_DATA: min=${stats.min.toFixed(2)} avg=${stats.avg.toFixed(2)} max=${stats.max.toFixed(2)} (${stats.nonNullCount} non-null rows in sample)`
      : ' | NO_NUMERIC_DATA_IN_SAMPLE';

    return `  • ${c.name} [${c.inferred_type}]${c.is_primary_date ? ' ← PRIMARY DATE' : ''} | cardinality: ${c.cardinality ?? '?'} | null_rate: ${nullPct}%${statsStr} | samples: ${samples || 'N/A'}`;
  }).join('\n');

  // Confirmed numeric columns sorted by data richness
  const confirmedNumeric = Object.entries(analysis.numericColumns)
    .sort(([, a], [, b]) => b.nonNullCount - a.nonNullCount)
    .map(([col, s]) => `  ${col}: avg=${s.avg.toFixed(2)}, range=[${s.min.toFixed(2)}–${s.max.toFixed(2)}], ${s.nonNullCount} rows with data`)
    .join('\n');

  const workingStatus = analysis.workingKPIColumns.length > 0
    ? `WORKING (keep unless strictly better alternative exists): ${analysis.workingKPIColumns.join(', ')}`
    : 'None currently working.';

  const brokenStatus = analysis.brokenKPIColumns.length > 0
    ? `BROKEN — showing "—", must be replaced from CONFIRMED NUMERIC list: ${analysis.brokenKPIColumns.join(', ')}`
    : 'All current KPIs are working correctly — preserve them.';

  return `You are a data visualization expert improving a trading/financial dashboard view.
Your job is to FIX what is broken and PRESERVE what is working. Never make a view worse.

TABLE: ${table}
TAB CONTEXT: ${tabContext}
TOTAL ROWS: ${profile.row_count ?? '?'}

═══ CONFIRMED COLUMNS (ONLY use names from this list — anything else does not exist) ═══
${columnSummary}

═══ CONFIRMED NUMERIC COLUMNS (only these are safe for KPIs and Y-axes) ═══
${confirmedNumeric || '  (none found — use count aggregate only)'}

═══ PRIMARY DATE COLUMN ═══
${analysis.primaryDateColumn
  ? `"${analysis.primaryDateColumn}" — always use this on x-axis for time-series charts.`
  : 'No primary date column found.'}

═══ CURRENT KPI STATUS ═══
${workingStatus}
${brokenStatus}

═══ CURRENT PLAN (your baseline — improve on it, never regress from it) ═══
${JSON.stringify(currentPlan, null, 2)}

═══ RULES ═══
HARD RULES (violating these causes regressions):
1. Every "column" value in your JSON MUST appear verbatim in the CONFIRMED COLUMNS list.
2. Every KPI "column" MUST appear in CONFIRMED NUMERIC COLUMNS.
3. Never remove a working KPI unless you replace it with a column that has MORE data rows.
4. Never remove the data_table chart type.
5. Maximum 4 KPIs, maximum 5 charts total.

IMPROVEMENT RULES:
6. Replace broken KPIs with the most financially meaningful columns from CONFIRMED NUMERIC.
7. Use the PRIMARY DATE column on x-axis for all time-series charts.
8. Use area_chart for cumulative metrics, line_chart for rates/win-rates/percentages.
9. Use donut_chart for categorical columns with cardinality ≤ 15.
10. For data_table: y_axes lists up to 8 useful display columns (exclude id/uuid/raw jsonb).
11. In ai_improvements: document every change AND every item you kept and why.

Return ONLY the JSON. No markdown. No preamble.

{
  "table": "${table}",
  "suggested_title": "Human-friendly title",
  "suggested_description": "One line: row count, time range, what this tracks",
  "kpis": [
    {
      "column": "exact_name_from_confirmed_list",
      "title": "Display Name",
      "format": "currency_inr | percent | number | text",
      "aggregate": "sum | avg | max | min | count | latest",
      "reasoning": "why this KPI; confirm it appears in CONFIRMED NUMERIC COLUMNS"
    }
  ],
  "charts": [
    {
      "id": "${table}_chart_1",
      "chart_type": "line_chart | area_chart | bar_chart | stacked_bar | donut_chart | scatter_plot | data_table",
      "title": "Chart title",
      "description": "What this chart shows",
      "x_axis": { "column": "exact_name", "label": "Label", "format": "date | number | text" },
      "y_axes": [{ "column": "exact_name", "label": "Label", "format": "currency_inr | percent | number" }],
      "color_by": "exact_name or null",
      "group_by": "exact_name or null",
      "aggregate": "sum | avg | count | null",
      "order_by": "exact_name or null",
      "limit": 200,
      "priority": 1,
      "reasoning": "why this chart type; confirm all column names exist"
    }
  ],
  "ai_improvements": [
    "Kept: KPI 'score' — working, avg=72.3 across 887 rows",
    "Fixed: KPI 'delivery_pct' had no data — replaced with 'win_rate' (avg=0.61, 887 rows)",
    "Improved: Changed bar to area chart — time-series with primary date column present"
  ]
}`;
}

// ── Route handler ──────────────────────────────────────────────────────────
export async function POST(req: NextRequest) {
  try {
    const { table, profile, currentPlan, sampleData, tabContext = 'unknown' } = await req.json();

    if (!table || !profile || !currentPlan) {
      return NextResponse.json(
        { error: 'table, profile, and currentPlan are required' },
        { status: 400 }
      );
    }

    const cfg = await getAIConfig();
    const provider = cfg['ai_active_provider'] || cfg['ai_provider'] || 'anthropic';

    if (!provider || provider === 'disabled') {
      return NextResponse.json(
        { error: 'AI provider is disabled in system_config' },
        { status: 503 }
      );
    }

    // ── Step 1: Pre-analyse before calling AI ─────────────────────────────
    // Know exactly what's working vs broken before the AI touches anything.
    // Results feed both the prompt and post-AI validation.
    const analysis = preAnalyze(profile, currentPlan, sampleData ?? []);

    // ── Step 2: Build enriched prompt ─────────────────────────────────────
    const prompt = buildPrompt(table, profile, currentPlan, sampleData ?? [], tabContext, analysis);

    // ── Step 3: Call AI ───────────────────────────────────────────────────
    const rawResponse = await callAI(prompt, cfg);

    const cleaned = rawResponse
      .replace(/^```json\s*/i, '')
      .replace(/^```\s*/i, '')
      .replace(/\s*```$/i, '')
      .trim();

    let aiPlan: Record<string, unknown>;
    try {
      aiPlan = JSON.parse(cleaned);
    } catch {
      return NextResponse.json(
        { error: 'AI returned invalid JSON', raw: rawResponse.slice(0, 500) },
        { status: 502 }
      );
    }

    if (!aiPlan.kpis || !aiPlan.charts) {
      return NextResponse.json(
        { error: 'AI plan missing required fields (kpis, charts)', raw: cleaned.slice(0, 500) },
        { status: 502 }
      );
    }

    // ── Step 4: Validate + repair ─────────────────────────────────────────
    // Every column reference checked against real profile.
    // Invalid items replaced with current-plan equivalents — AI can never
    // silently break a working view.
    const validatedPlan = validateAndRepair(aiPlan, currentPlan, analysis);

    return NextResponse.json({
      ...validatedPlan,
      provider_used: provider,
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Unknown error';
    console.error('[enhance-view]', msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
