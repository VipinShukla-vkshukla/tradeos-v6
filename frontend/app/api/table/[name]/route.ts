// app/api/table/[name]/route.ts
//
// Generic write endpoint for any Supabase table.
// Uses SUPABASE_SERVICE_ROLE_KEY (server-side only) which bypasses RLS entirely.
//
// WHY THIS EXISTS:
// The browser uses NEXT_PUBLIC_SUPABASE_ANON_KEY which is subject to Row Level
// Security. By default, Supabase tables have RLS enabled with NO policies for
// anon writes → every INSERT/UPDATE/DELETE from the browser is silently blocked
// (returns no error, just 0 rows affected). pnpm dev vs production is completely
// irrelevant — this is purely a Supabase permissions issue.
//
// This route runs server-side with the service role key, which bypasses RLS.
// Never expose SUPABASE_SERVICE_ROLE_KEY to the browser.
//
// USAGE from the frontend:
//   // Update a row
//   await tableApi.update('system_config', { value: 'new' }, { key: 'ai_provider' });
//
//   // Insert a row
//   await tableApi.insert('config_change_log', { key: 'x', old_value: 'a', new_value: 'b' });
//
//   // Delete a row
//   await tableApi.delete('custom_views', { id: 'some-uuid' });

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!, // server-side only, bypasses RLS
);

// Tables that are allowed to be written via this endpoint.
// Add any table you want to make editable from the frontend.
// Keeping this explicit prevents unintended writes to sensitive tables.
const WRITABLE_TABLES = new Set([
  'system_config',
  'custom_views',
  'brain_proposals',
  'open_positions',
  'closed_positions',
  'signal_log',
  'config_change_log',
  'master_shortlist',
  'lessons',
  // Add more tables here as needed
]);

interface RouteParams {
  params: Promise<{ name: string }>;
}

// ── PATCH — update rows matching a filter ──────────────────────────────────
export async function PATCH(req: NextRequest, { params }: RouteParams) {
  const { name: table } = await params;

  if (!WRITABLE_TABLES.has(table)) {
    return NextResponse.json({ error: `Table '${table}' is not in the writable allowlist` }, { status: 403 });
  }

  try {
    const { data: updates, filter } = await req.json() as {
      data: Record<string, unknown>;
      filter: Record<string, unknown>;
    };

    if (!updates || !filter) {
      return NextResponse.json({ error: '`data` and `filter` are required' }, { status: 400 });
    }

    let query = supabase.from(table).update({
      ...updates,
      updated_at: new Date().toISOString(),
    });

    // Apply all filter conditions (e.g. { key: 'ai_provider' })
    for (const [col, val] of Object.entries(filter)) {
      query = (query as unknown as { eq: (c: string, v: unknown) => typeof query }).eq(col, val) as typeof query;
    }

    const { data, error } = await (query as unknown as { select: () => Promise<{ data: unknown; error: unknown }> }).select();

    if (error) throw error;
    return NextResponse.json({ data });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

// ── POST — insert a new row ────────────────────────────────────────────────
export async function POST(req: NextRequest, { params }: RouteParams) {
  const { name: table } = await params;

  if (!WRITABLE_TABLES.has(table)) {
    return NextResponse.json({ error: `Table '${table}' is not in the writable allowlist` }, { status: 403 });
  }

  try {
    const body = await req.json() as Record<string, unknown>;

    const { data, error } = await supabase
      .from(table)
      .insert(body)
      .select();

    if (error) throw error;
    return NextResponse.json({ data });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

// ── DELETE — delete rows matching a filter ─────────────────────────────────
export async function DELETE(req: NextRequest, { params }: RouteParams) {
  const { name: table } = await params;

  if (!WRITABLE_TABLES.has(table)) {
    return NextResponse.json({ error: `Table '${table}' is not in the writable allowlist` }, { status: 403 });
  }

  try {
    const { filter } = await req.json() as { filter: Record<string, unknown> };

    if (!filter || Object.keys(filter).length === 0) {
      return NextResponse.json({ error: 'filter is required (prevents accidental full-table delete)' }, { status: 400 });
    }

    let query = supabase.from(table).delete();
    for (const [col, val] of Object.entries(filter)) {
      query = (query as unknown as { eq: (c: string, v: unknown) => typeof query }).eq(col, val) as typeof query;
    }

    const { error } = await query;
    if (error) throw error;
    return NextResponse.json({ success: true });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
