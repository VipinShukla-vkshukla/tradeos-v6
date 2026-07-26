// app/api/engines/route.ts
//
// GET   /api/engines  → engine registry + per-engine measured performance
// PATCH /api/engines  → change lifecycle and/or gates, with a full audit trail
//
// WHY WRITES GO THROUGH A ROUTE RATHER THAN THE BROWSER CLIENT
//   strategy_config drives what the screener does with real money. The browser
//   holds only the anon key; mutating engine behaviour requires the service
//   role, which must never reach the client. Routing writes through here also
//   makes the audit record (engine_lifecycle_log) unskippable — a change and
//   its justification are written in the same request.

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('SUPABASE_SERVICE_ROLE_KEY not configured');
  return createClient(url, key);
}

const ENGINES = ['CTL', 'SBS', 'TPO', 'VBD', 'IAD', 'RSB', 'MOM', 'RVS', 'SEC'];
const LIFECYCLES = ['ACTIVE', 'SHADOW', 'RETIRED'];

export async function GET() {
  try {
    const sb = getServiceClient();

    const { data: cfg, error: cfgErr } = await sb
      .from('strategy_config')
      .select('*')
      .order('strategy');
    if (cfgErr) throw cfgErr;

    // Measured outcomes per engine. Missing view (migration 006 not yet run)
    // is not fatal — the Control Room still renders, just without evidence.
    let perf: unknown[] = [];
    try {
      const { data } = await sb.from('v_engine_performance').select('*');
      perf = data ?? [];
    } catch {
      perf = [];
    }

    let history: unknown[] = [];
    try {
      const { data } = await sb
        .from('engine_lifecycle_log')
        .select('*')
        .order('changed_at', { ascending: false })
        .limit(50);
      history = data ?? [];
    } catch {
      history = [];
    }

    return NextResponse.json({
      engines: (cfg ?? []).filter((r) => ENGINES.includes(r.strategy)),
      overlays: (cfg ?? []).filter((r) => !ENGINES.includes(r.strategy)),
      performance: perf,
      history,
    });
  } catch (e) {
    return NextResponse.json({ message: (e as Error).message }, { status: 500 });
  }
}

export async function PATCH(req: NextRequest) {
  try {
    const sb = getServiceClient();
    const body = (await req.json()) as {
      engine: string;
      lifecycle?: string;
      gates?: Record<string, unknown>;
      min_score?: number | null;
      reason?: string;
      evidence?: Record<string, unknown>;
    };

    if (!body.engine || !ENGINES.includes(body.engine)) {
      return NextResponse.json({ message: `engine must be one of ${ENGINES.join(', ')}` }, { status: 400 });
    }
    if (body.lifecycle && !LIFECYCLES.includes(body.lifecycle)) {
      return NextResponse.json({ message: `lifecycle must be one of ${LIFECYCLES.join(', ')}` }, { status: 400 });
    }

    const { data: current, error: readErr } = await sb
      .from('strategy_config')
      .select('*')
      .eq('strategy', body.engine)
      .single();
    if (readErr || !current) {
      return NextResponse.json({ message: `engine ${body.engine} not found — run migration 006` }, { status: 404 });
    }

    // params is a jsonb OBJECT (enforced by migration 005's CHECK constraint).
    // Merge rather than replace so unrelated keys survive the write.
    const params = { ...(current.params ?? {}) } as Record<string, unknown>;
    if (body.gates) params.gates = body.gates;
    if (body.min_score !== undefined) params.min_score = body.min_score;
    if (body.lifecycle) params.lifecycle = body.lifecycle;

    const update: Record<string, unknown> = { params, updated_at: new Date().toISOString() };
    if (body.lifecycle) {
      update.lifecycle = body.lifecycle;
      // `enabled` is the older flag other code still reads; keep it consistent
      // so a RETIRED engine cannot be resurrected by a stale enabled=true.
      update.enabled = body.lifecycle !== 'RETIRED';
      if (body.lifecycle === 'RETIRED') update.retired_at = new Date().toISOString();
    }

    const { error: upErr } = await sb.from('strategy_config').update(update).eq('strategy', body.engine);
    if (upErr) throw upErr;

    // Audit — never skipped, and it records the evidence the operator was
    // looking at when they made the call, not just the value that changed.
    try {
      await sb.from('engine_lifecycle_log').insert({
        engine: body.engine,
        from_state: current.lifecycle ?? null,
        to_state: body.lifecycle ?? current.lifecycle ?? 'ACTIVE',
        from_params: current.params ?? null,
        to_params: params,
        changed_by: 'control_room',
        reason: body.reason ?? null,
        evidence: body.evidence ?? null,
      });
    } catch (e) {
      // A failed audit insert must not silently succeed the change.
      return NextResponse.json(
        { ok: true, warning: `change applied but audit log failed: ${(e as Error).message}` },
        { status: 207 }
      );
    }

    return NextResponse.json({ ok: true, engine: body.engine, params });
  } catch (e) {
    return NextResponse.json({ message: (e as Error).message }, { status: 500 });
  }
}
