// app/api/config/[key]/route.ts
// PATCH /api/config/:key  — update a system_config value
// Uses Supabase service role key (server-side only).

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('Supabase service role key not configured');
  return createClient(url, key);
}

export async function PATCH(
  req: NextRequest,
  // Next.js 15+ makes route params a PROMISE. Reading params.key directly gave
  // `undefined`, so every update ran `.eq('key', undefined)` — which PostgREST
  // accepts, matches nothing, and reports as success. The route has been
  // answering {"success":true} while changing nothing, for every config edit
  // made from the dashboard.
  { params }: { params: Promise<{ key: string }> }
) {
  try {
    const supabase = getServiceClient();
    const { key } = await params;
    const configKey = decodeURIComponent(key ?? '');

    if (!configKey) {
      return NextResponse.json({ message: 'config key missing from the route' },
                               { status: 400 });
    }

    const body: { value: string; reason?: string } = await req.json();
    if (body.value === undefined) {
      return NextResponse.json({ message: 'value is required' }, { status: 400 });
    }

    // Read current value for change log
    const { data: current } = await supabase
      .from('system_config')
      .select('value')
      .eq('key', configKey)
      .single();

    // Update system_config, and REQUIRE a row back. A PATCH that matches
    // nothing is not a success — it is the caller believing a setting changed
    // when it did not, which for a kill switch or a live-trading toggle is the
    // most expensive kind of wrong this API can be.
    const { data: updated, error } = await supabase
      .from('system_config')
      .update({ value: body.value, updated_at: new Date().toISOString() })
      .eq('key', configKey)
      .select('key');

    if (error) throw error;
    if (!updated || updated.length === 0) {
      return NextResponse.json(
        { message: `no config row named '${configKey}' — nothing was changed` },
        { status: 404 },
      );
    }

    // Write to config_change_log
    await supabase.from('config_change_log').insert({
      key: configKey,
      old_value: current?.value ?? '',
      new_value: body.value,
      changed_by: 'dashboard',
      reason: body.reason ?? 'Manual update via TradeOS dashboard',
      changed_at: new Date().toISOString(),
    });

    return NextResponse.json({ success: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Unknown error';
    return NextResponse.json({ message }, { status: 500 });
  }
}
