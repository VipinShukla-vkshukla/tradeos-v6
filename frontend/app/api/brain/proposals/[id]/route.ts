// app/api/brain/proposals/[id]/route.ts
// PATCH /api/brain/proposals/:id  — approve | reject | rollback a brain_proposal
// Uses Supabase service role key (server-side only — never exposed to browser).

import { NextRequest, NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY; // server-only env var
  if (!url || !key) throw new Error('Supabase service role key not configured');
  return createClient(url, key);
}

export async function PATCH(
  req: NextRequest,
  { params }: { params: { id: string } }
) {
  try {
    const supabase = getServiceClient();
    const id = parseInt(params.id, 10);
    if (isNaN(id)) return NextResponse.json({ message: 'Invalid proposal id' }, { status: 400 });

    const body: {
      status: 'APPROVED' | 'REJECTED' | 'ROLLED_BACK';
      reviewed_by?: string;
      notes?: string;
      reason?: string;
    } = await req.json();

    const update: Record<string, unknown> = {
      status: body.status,
      reviewed_at: new Date().toISOString(),
    };

    if (body.reviewed_by) update.reviewed_by = body.reviewed_by;
    if (body.status === 'APPLIED') update.applied_at = new Date().toISOString();

    const { error } = await supabase
      .from('brain_proposals')
      .update(update)
      .eq('id', id);

    if (error) throw error;

    return NextResponse.json({ success: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Unknown error';
    return NextResponse.json({ message }, { status: 500 });
  }
}
