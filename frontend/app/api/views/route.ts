// app/api/views/route.ts
// GET /api/views  — fetch all custom views for current user
// POST /api/views — create a new custom view
// Views are persisted in the custom_views Supabase table (see supabase_migration.sql)

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

export async function GET() {
  try {
    const supabase = getServiceClient();
    const { data, error } = await supabase
      .from('custom_views')
      .select('*')
      .order('created_at', { ascending: false });

    if (error) throw error;
    return NextResponse.json(data || []);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to fetch views';
    return NextResponse.json({ message }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  try {
    const supabase = getServiceClient();
    const body = await req.json();

    const { data, error } = await supabase
      .from('custom_views')
      .insert({
        id: body.id || crypto.randomUUID(),
        name: body.name,
        description: body.description,
        base_table: body.baseTable || body.primaryTable,
        columns: JSON.stringify(body.columns || []),
        joins: JSON.stringify(body.joins || []),
        filters: JSON.stringify(body.filters || []),
        sorts: JSON.stringify(body.sorts || []),
        view_type: body.viewType || 'table',
        chart_config: body.chartConfig ? JSON.stringify(body.chartConfig) : null,
        tab_id: body.tabId || null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      })
      .select()
      .single();

    if (error) throw error;
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed to create view';
    return NextResponse.json({ message }, { status: 500 });
  }
}
