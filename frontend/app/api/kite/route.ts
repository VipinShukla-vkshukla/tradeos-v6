// app/api/kite/route.ts
//
// GET  /api/kite  → session status + login URL for the Connect button
// POST /api/kite  → exchange a request_token (or a pasted redirect URL)
//
// The POST path is the manual fallback for when the Kite app's redirect URL
// does not point at /api/kite/callback. Both paths converge on the same
// exchange + store, so the session is identical either way.

import { NextRequest, NextResponse } from 'next/server';
import {
  loginUrl, sessionStatus, exchangeRequestToken, storeToken, extractRequestToken,
} from '@/lib/kiteServer';

export async function GET() {
  try {
    const status = await sessionStatus();
    return NextResponse.json({
      ...status,
      login_url: status.configured ? loginUrl() : null,
      // Surfaced so the UI can tell the user exactly what to register in the
      // Kite developer console for the zero-paste flow.
      callback_url: '/api/kite/callback',
    });
  } catch (e) {
    return NextResponse.json({ message: (e as Error).message }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  try {
    const body = (await req.json()) as { request_token?: string };
    const token = extractRequestToken(body.request_token ?? '');

    if (!token || token.length < 10) {
      return NextResponse.json(
        {
          message:
            token
              ? `"${token}" is ${token.length} characters; Kite requires at least 10. Paste the full redirect URL you landed on after logging in.`
              : 'Paste the redirect URL you landed on after logging in.',
        },
        { status: 400 }
      );
    }

    const data = await exchangeRequestToken(token);
    await storeToken(data.access_token);
    return NextResponse.json({
      ok: true,
      user_id: data.user_id ?? null,
      user_name: data.user_name ?? null,
    });
  } catch (e) {
    return NextResponse.json({ message: (e as Error).message }, { status: 400 });
  }
}
