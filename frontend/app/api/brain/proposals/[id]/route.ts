// app/api/ai/enhance-view/route.ts
//
// AI-powered view enhancement.
// Takes the current rule-based AutoVizPlan + table profile + sample data,
// asks the configured AI provider to produce a better plan, and returns it.
//
// Unlike /api/ai/analyze (which writes a narrative), this route rewrites the
// entire visualization plan — which KPIs to surface, what chart types to use,
// what columns to put on which axes, and why.
//
// The returned plan is a superset of AutoVizPlan with an extra
// `ai_improvements` field listing what the AI changed and why.

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SERVICE_KEY  = process.env.SUPABASE_SERVICE_ROLE_KEY!;

// ── System-config reader ───────────────────────────────────────────────────
async function getAIConfig(): Promise<Record<string, string>> {
  const supabase = createClient(SUPABASE_URL, SERVICE_KEY);
  const { data } = await supabase.from('system_config').select('key, value');
  return Object.fromEntries((data ?? []).map((r: { key: string; value: string }) => [r.key, r.value]));
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
        model: 'claude-opus-4-5',
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
): string {
  // Derive column list and sample values for AI to understand the actual data
  const columns = (profile.columns as Array<{
    name: string; inferred_type: string; cardinality?: number;
    null_rate?: number; is_primary_date?: boolean;
  }>) ?? [];

  const columnSummary = columns.map((c) => {
    const samples = sampleData
      .slice(0, 5)
      .map((r) => r[c.name])
      .filter((v) => v !== null && v !== undefined)
      .map(String)
      .join(', ');
    return `  • ${c.name} [${c.inferred_type}]${c.is_primary_date ? ' ← primary date' : ''} | cardinality: ${c.cardinality ?? '?'} | null_rate: ${(c.null_rate ?? 0 * 100).toFixed(0)}% | samples: ${samples || 'N/A'}`;
  }).join('\n');

  return `You are a data visualization expert analyzing a trading/financial database table.

TABLE: ${table}
CONTEXT (tab this view belongs to): ${tabContext}
TOTAL ROWS: ${profile.row_count ?? '?'}

COLUMNS WITH ACTUAL DATA SAMPLES:
${columnSummary}

CURRENT RULE-BASED VIZ PLAN (generated automatically, may have issues):
${JSON.stringify(currentPlan, null, 2)}

KNOWN PROBLEMS TO FIX:
- KPI cards showing "—" means the KPI column wasn't in the chart query. Only pick KPI columns that appear in sampleData with actual numeric values.
- Chart axes must reference columns that actually exist in the table.
- Prefer columns with low null_rate (< 0.5) for KPIs and primary axes.
- For financial data: surface P&L, rates, scores, percentages as KPIs.
- For time-series data: always use the is_primary_date column on the x-axis.

YOUR TASK:
Return an improved visualization plan as a single JSON object. Do NOT wrap in markdown fences.
The JSON must strictly follow this schema:

{
  "table": "${table}",
  "suggested_title": "Human-friendly title for this table/view",
  "suggested_description": "One line: row count, time range, what this data tracks",
  "kpis": [
    {
      "column": "exact_column_name",
      "title": "Display Name",
      "format": "currency_inr | percent | number | text",
      "aggregate": "sum | avg | max | min | count | latest",
      "reasoning": "why this is a meaningful KPI"
    }
  ],
  "charts": [
    {
      "id": "${table}_chart_1",
      "chart_type": "line_chart | area_chart | bar_chart | stacked_bar | donut_chart | scatter_plot | data_table",
      "title": "Chart title",
      "description": "What this chart shows",
      "x_axis": { "column": "col_name", "label": "Label", "format": "date | number | text" },
      "y_axes": [{ "column": "col_name", "label": "Label", "format": "currency_inr | percent | number" }],
      "color_by": "col_name or null",
      "group_by": "col_name or null",
      "aggregate": "sum | avg | count | null",
      "order_by": "col_name or null",
      "limit": 200,
      "priority": 1,
      "reasoning": "why this chart type was chosen"
    }
  ],
  "ai_improvements": [
    "Fixed: KPI 'delivery_pct' was not being fetched — moved to dedicated KPI query",
    "Improved: Changed bar chart to area chart because data is time-series",
    "Added: Donut chart for signal_type distribution"
  ]
}

RULES:
1. Maximum 4 KPIs (most meaningful numeric columns with actual data).
2. Maximum 5 charts (quality over quantity). Always end with a data_table at priority 99.
3. Every column reference MUST exist in the columns list above.
4. KPIs must only use columns where sampleData has actual numeric values (not null/undefined).
5. For the data_table chart, y_axes lists the 8 most useful display columns (exclude id/uuid/jsonb).
6. ai_improvements must list every change from the current plan and why.

Return ONLY the JSON, no preamble, no explanation outside the JSON.`;
}

// ── Route handler ──────────────────────────────────────────────────────────
export async function POST(req: NextRequest) {
  try {
    const { table, profile, currentPlan, sampleData, tabContext = 'unknown' } = await req.json();

    if (!table || !profile || !currentPlan) {
      return NextResponse.json({ error: 'table, profile, and currentPlan are required' }, { status: 400 });
    }

    const cfg = await getAIConfig();
    const provider = cfg['ai_active_provider'] || cfg['ai_provider'] || 'anthropic';

    if (!provider || provider === 'disabled') {
      return NextResponse.json({ error: 'AI provider is disabled in system_config' }, { status: 503 });
    }

    const prompt = buildPrompt(table, profile, currentPlan, sampleData ?? [], tabContext);
    const rawResponse = await callAI(prompt, cfg);

    // Strip any accidental markdown fences
    const cleaned = rawResponse
      .replace(/^```json\s*/i, '')
      .replace(/^```\s*/i, '')
      .replace(/\s*```$/i, '')
      .trim();

    let enhancedPlan: Record<string, unknown>;
    try {
      enhancedPlan = JSON.parse(cleaned);
    } catch {
      return NextResponse.json(
        { error: 'AI returned invalid JSON', raw: rawResponse.slice(0, 500) },
        { status: 502 }
      );
    }

    // Safety: ensure the plan has the required top-level fields
    if (!enhancedPlan.kpis || !enhancedPlan.charts) {
      return NextResponse.json(
        { error: 'AI plan is missing required fields (kpis, charts)', raw: cleaned.slice(0, 500) },
        { status: 502 }
      );
    }

    return NextResponse.json({
      ...enhancedPlan,
      provider_used: provider,
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : 'Unknown error';
    console.error('[enhance-view]', msg);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
