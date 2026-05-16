// app/api/views/[id]/route.ts
// GET    /api/views/:id — fetch one view
// PUT    /api/views/:id — update a view
// DELETE /api/views/:id — delete a view

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

export async function GET(
  _req: NextRequest,
  { params }: { params: { id: string } }
) {
  try {
    const supabase = getServiceClient();
    const { data, error } = await supabase
      .from('custom_views')
      .select('*')
      .eq('id', params.id)
      .single();

    if (error) throw error;
    if (!data) return NextResponse.json({ message: 'Not found' }, { status: 404 });
    return NextResponse.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}

export async function PUT(
  req: NextRequest,
  { params }: { params: { id: string } }
) {
  try {
    const supabase = getServiceClient();
    const body = await req.json();

    const update: Record<string, unknown> = {
      updated_at: new Date().toISOString(),
    };
    if (body.name !== undefined) update.name = body.name;
    if (body.description !== undefined) update.description = body.description;
    if (body.columns !== undefined) update.columns = JSON.stringify(body.columns);
    if (body.joins !== undefined) update.joins = JSON.stringify(body.joins);
    if (body.filters !== undefined) update.filters = JSON.stringify(body.filters);
    if (body.sorts !== undefined) update.sorts = JSON.stringify(body.sorts);
    if (body.viewType !== undefined) update.view_type = body.viewType;
    if (body.chartConfig !== undefined) update.chart_config = JSON.stringify(body.chartConfig);
    if (body.tabId !== undefined) update.tab_id = body.tabId;

    const { data, error } = await supabase
      .from('custom_views')
      .update(update)
      .eq('id', params.id)
      .select()
      .single();

    if (error) throw error;
    return NextResponse.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}

export async function DELETE(
  _req: NextRequest,
  { params }: { params: { id: string } }
) {
  try {
    const supabase = getServiceClient();
    const { error } = await supabase
      .from('custom_views')
      .delete()
      .eq('id', params.id);

    if (error) throw error;
    return NextResponse.json({ success: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Failed';
    return NextResponse.json({ message }, { status: 500 });
  }
}
