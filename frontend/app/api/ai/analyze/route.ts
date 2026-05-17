// app/api/ai/analyze/route.ts
// POST /api/ai/analyze
// Sends a table profile + data sample to DeepSeek (via system_config ai_provider).
// Returns:
//   - narrative: plain-English insight about the data ("Win rate has dropped 12% over 4 weeks")
//   - anomalies: data points that look wrong or need attention
//   - chart_recommendations: additional chart ideas the rule engine might have missed
//   - questions: questions the data raises that the user should investigate
//
// This is the "smart" layer on top of autoViz.ts which is pure rule-based.

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';
import type { TableProfile } from '@/app/api/profile/route';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

export interface AnalyzeRequest {
  table: string;
  profile: TableProfile;
  sample_data: Record<string, unknown>[];  // 20-50 rows of actual data
  context?: string;                         // optional user question
}

export interface AnalyzeResponse {
  narrative: string;          // 2-4 sentence plain English insight
  anomalies: string[];        // data points that look wrong
  chart_recommendations: Array<{
    title: string;
    reasoning: string;
    x_column?: string;
    y_column?: string;
    chart_type: string;
  }>;
  questions: string[];        // follow-up questions the data raises
  provider_used: string;
}

function getApiKey(provider: string): string | undefined {
  return {
    deepseek: process.env.DEEPSEEK_API_KEY,
    anthropic: process.env.ANTHROPIC_API_KEY,
    openai: process.env.OPENAI_API_KEY,
    gemini: process.env.GEMINI_API_KEY,
  }[provider.toLowerCase()];
}

function buildSystemPrompt(tableName: string): string {
  return `You are a quantitative analyst assistant for TradeOS, an algorithmic swing trading system for Indian equity markets (NSE/BSE).

You are analyzing data from the "${tableName}" table. Your job is to:
1. Identify the most important insight from the data in 2-4 plain English sentences
2. Flag any anomalies or concerning patterns
3. Suggest additional chart types that would reveal hidden patterns
4. Ask follow-up questions that the data raises

Always respond ONLY with a valid JSON object in exactly this shape (no markdown, no preamble):
{
  "narrative": "2-4 sentence insight about what the data shows",
  "anomalies": ["anomaly 1", "anomaly 2"],
  "chart_recommendations": [
    {
      "title": "Chart title",
      "reasoning": "Why this chart would be useful",
      "x_column": "column_name",
      "y_column": "column_name",
      "chart_type": "line_chart|bar_chart|scatter_plot|donut_chart"
    }
  ],
  "questions": ["Question 1?", "Question 2?"]
}`;
}

function buildUserMessage(req: AnalyzeRequest): string {
  const { profile, sample_data, context } = req;

  const columnSummary = profile.columns
    .filter((c) => c.null_rate < 0.9)
    .slice(0, 15)
    .map((c) => {
      const parts = [`${c.name} (${c.inferred_type})`];
      if (c.min !== undefined) parts.push(`range: ${c.min?.toFixed(2)}–${c.max?.toFixed(2)}`);
      if (c.top_values?.length) parts.push(`values: ${c.top_values.slice(0, 4).map((v) => v.value).join(', ')}`);
      return parts.join(', ');
    })
    .join('\n');

  // Summarize sample data to avoid token bloat
  const dataSummary = JSON.stringify(sample_data.slice(0, 20), null, 0)
    .slice(0, 3000); // hard cap at 3000 chars

  return `Table: ${req.table}
Row count: ${profile.row_count}
Is time series: ${profile.is_time_series}
Primary date column: ${profile.primary_date_col ?? 'none'}

Column profiles:
${columnSummary}

Sample data (${Math.min(sample_data.length, 20)} rows):
${dataSummary}

${context ? `User question: ${context}` : 'Provide general analysis of what this data reveals.'}`;
}

async function callDeepSeek(apiKey: string, system: string, user: string): Promise<string> {
  const res = await fetch('https://api.deepseek.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model: 'deepseek-chat',
      messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
      max_tokens: 1000,
      temperature: 0.3,
    }),
  });
  const data = await res.json() as { choices: Array<{ message: { content: string } }> };
  return data.choices?.[0]?.message?.content ?? '{}';
}

async function callAnthropic(apiKey: string, system: string, user: string): Promise<string> {
  const res = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 1000,
      system,
      messages: [{ role: 'user', content: user }],
    }),
  });
  const data = await res.json() as { content: Array<{ text: string }> };
  return data.content?.[0]?.text ?? '{}';
}

async function callOpenAI(apiKey: string, system: string, user: string, model = 'gpt-4o-mini'): Promise<string> {
  const res = await fetch('https://api.openai.com/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      model,
      messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
      max_tokens: 1000,
      temperature: 0.3,
    }),
  });
  const data = await res.json() as { choices: Array<{ message: { content: string } }> };
  return data.choices?.[0]?.message?.content ?? '{}';
}

export async function POST(req: NextRequest) {
  try {
    const body: AnalyzeRequest = await req.json();
    if (!body.table || !body.profile) {
      return NextResponse.json({ message: 'table and profile required' }, { status: 400 });
    }

    // Read provider from system_config
    const supabase = getServiceClient();
    const { data: cfg } = await supabase
      .from('system_config')
      .select('key, value')
      .in('key', ['ai_provider', 'ai_active_provider']) as { data: Array<{ key: string; value: string }> | null };

    const cfgMap = Object.fromEntries((cfg ?? []).map((r) => [r.key, r.value]));
    const provider = cfgMap['ai_active_provider'] ?? cfgMap['ai_provider'] ?? 'deepseek';
    const apiKey = getApiKey(provider);

    if (!apiKey) {
      return NextResponse.json({
        message: `No API key for provider "${provider}". Add ${provider.toUpperCase()}_API_KEY to .env.local`,
      }, { status: 503 });
    }

    const systemPrompt = buildSystemPrompt(body.table);
    const userMessage = buildUserMessage(body);

    let raw = '';
    if (provider === 'anthropic') raw = await callAnthropic(apiKey, systemPrompt, userMessage);
    else if (provider === 'openai') raw = await callOpenAI(apiKey, systemPrompt, userMessage);
    else raw = await callDeepSeek(apiKey, systemPrompt, userMessage);  // default deepseek

    const clean = raw.replace(/```json|```/g, '').trim();
    let parsed: AnalyzeResponse;
    try {
      parsed = JSON.parse(clean);
    } catch {
      parsed = {
        narrative: raw.slice(0, 500),
        anomalies: [],
        chart_recommendations: [],
        questions: [],
        provider_used: provider,
      };
    }
    parsed.provider_used = provider;

    return NextResponse.json(parsed);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Analysis failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}
