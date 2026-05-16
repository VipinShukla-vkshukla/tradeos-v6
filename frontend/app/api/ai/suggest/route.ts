// app/api/ai/suggest/route.ts
// POST /api/ai/suggest
// Reads ai_provider from system_config, calls the appropriate AI API
// to generate ViewWizard column/join suggestions.
//
// Add DEEPSEEK_API_KEY to .env.local (server-side only, no NEXT_PUBLIC_ prefix)

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

interface SuggestRequest {
  context: string;       // User's natural language request e.g. "show me winning signals by sector"
  baseTable: string;     // The primary table selected in ViewWizard
  availableTables: string[]; // All tables user could join from
  currentColumns?: string[]; // Already selected columns
}

export async function POST(req: NextRequest) {
  try {
    const body: SuggestRequest = await req.json();
    if (!body.context || !body.baseTable) {
      return NextResponse.json({ message: 'context and baseTable required' }, { status: 400 });
    }

    // Step 1: Read AI provider from system_config
    const supabase = getServiceClient();
    const { data: configRows } = await supabase
      .from('system_config')
      .select('key, value')
      .in('key', ['ai_provider', 'ai_active_provider']);

    const configMap: Record<string, string> = {};
    for (const row of (configRows || []) as Array<{ key: string; value: string }>) {
      configMap[row.key] = row.value;
    }

    const provider = configMap['ai_active_provider'] || configMap['ai_provider'] || 'deepseek';

    // Step 2: Get API key for provider
    const apiKey = getApiKey(provider);
    if (!apiKey) {
      return NextResponse.json(
        { message: `No API key configured for provider: ${provider}. Add ${getEnvKeyName(provider)} to .env.local` },
        { status: 503 }
      );
    }

    // Step 3: Build prompt
    const systemPrompt = `You are a data analyst assistant for TradeOS, an algorithmic trading system for Indian equity markets (NSE/BSE).

The user is building a custom data view using a drag-and-drop wizard. Based on their request, suggest:
1. Which columns to select from the base table
2. Whether any joins to other tables are needed
3. Any useful filters

Available tables: ${body.availableTables.join(', ')}
Base table selected: ${body.baseTable}
${body.currentColumns?.length ? `Currently selected columns: ${body.currentColumns.join(', ')}` : ''}

Respond ONLY with a JSON object in this exact shape:
{
  "suggestedColumns": ["col1", "col2"],
  "suggestedJoins": [
    { "table": "table_name", "on": "base_column = joined_column", "type": "LEFT" }
  ],
  "suggestedFilters": [
    { "column": "col", "operator": "eq", "value": "example" }
  ],
  "explanation": "One sentence explaining the suggestion"
}`;

    const userMessage = body.context;

    // Step 4: Call provider API
    const result = await callProvider(provider, apiKey, systemPrompt, userMessage);

    return NextResponse.json(result);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'AI suggestion failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}

function getApiKey(provider: string): string | undefined {
  const keyMap: Record<string, string | undefined> = {
    deepseek: process.env.DEEPSEEK_API_KEY,
    anthropic: process.env.ANTHROPIC_API_KEY,
    openai: process.env.OPENAI_API_KEY,
    gemini: process.env.GEMINI_API_KEY,
  };
  return keyMap[provider.toLowerCase()];
}

function getEnvKeyName(provider: string): string {
  const nameMap: Record<string, string> = {
    deepseek: 'DEEPSEEK_API_KEY',
    anthropic: 'ANTHROPIC_API_KEY',
    openai: 'OPENAI_API_KEY',
    gemini: 'GEMINI_API_KEY',
  };
  return nameMap[provider.toLowerCase()] || `${provider.toUpperCase()}_API_KEY`;
}

async function callProvider(
  provider: string,
  apiKey: string,
  systemPrompt: string,
  userMessage: string
): Promise<unknown> {
  // DeepSeek and OpenAI share the same API shape
  const isOpenAICompat = ['deepseek', 'openai'].includes(provider.toLowerCase());

  if (isOpenAICompat) {
    const baseUrl = provider.toLowerCase() === 'deepseek'
      ? 'https://api.deepseek.com'
      : 'https://api.openai.com';

    const model = provider.toLowerCase() === 'deepseek'
      ? 'deepseek-chat'
      : 'gpt-4o-mini';

    const response = await fetch(`${baseUrl}/v1/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: [
          { role: 'system', content: systemPrompt },
          { role: 'user', content: userMessage },
        ],
        max_tokens: 800,
        temperature: 0.3,
      }),
    });

    if (!response.ok) {
      const err = await response.text();
      throw new Error(`${provider} API error: ${err}`);
    }

    const data = await response.json() as {
      choices: Array<{ message: { content: string } }>;
    };

    const content = data.choices?.[0]?.message?.content || '{}';
    try {
      return JSON.parse(content.replace(/```json|```/g, '').trim());
    } catch {
      return { explanation: content, suggestedColumns: [], suggestedJoins: [], suggestedFilters: [] };
    }
  }

  if (provider.toLowerCase() === 'anthropic') {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 800,
        system: systemPrompt,
        messages: [{ role: 'user', content: userMessage }],
      }),
    });

    const data = await response.json() as {
      content: Array<{ text: string }>;
    };
    const content = data.content?.[0]?.text || '{}';
    try {
      return JSON.parse(content.replace(/```json|```/g, '').trim());
    } catch {
      return { explanation: content, suggestedColumns: [], suggestedJoins: [], suggestedFilters: [] };
    }
  }

  throw new Error(`Unsupported provider: ${provider}`);
}
