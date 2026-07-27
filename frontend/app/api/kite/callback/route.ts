// app/api/kite/callback/route.ts
//
// Zerodha redirects here after a successful login, carrying ?request_token=...
// This is what makes the daily session genuinely one click: you press Connect,
// authenticate at Zerodha, and land back on the dashboard with the session
// already stored. No copy-paste, no terminal.
//
// REGISTER THIS in the Kite developer console as the app's Redirect URL:
//     http://127.0.0.1:3000/api/kite/callback
//
// Zerodha permits plain http for loopback addresses, which is why this works
// for a local dashboard without a tunnel or certificate.
//
// The request_token in the query string is single-use and worthless without
// KITE_API_SECRET, which never leaves the server — so a token leaking into a
// browser history entry cannot be replayed into a session by anyone else.

import { NextRequest, NextResponse } from 'next/server';
import { exchangeRequestToken, storeToken } from '@/lib/kiteServer';

export async function GET(req: NextRequest) {
  const url = new URL(req.url);
  const requestToken = url.searchParams.get('request_token');
  const status = url.searchParams.get('status');
  const home = new URL('/health', url.origin);

  // Zerodha sends status=error and omits the token when the user cancels or
  // the app is misconfigured.
  if (status && status !== 'success') {
    home.searchParams.set('kite', 'error');
    home.searchParams.set(
      'kite_msg',
      url.searchParams.get('error_type') || 'Login was not completed at Zerodha.'
    );
    return NextResponse.redirect(home);
  }

  if (!requestToken) {
    home.searchParams.set('kite', 'error');
    home.searchParams.set(
      'kite_msg',
      'No request_token in the redirect. Check that the Redirect URL registered on your Kite app is exactly http://127.0.0.1:3000/api/kite/callback'
    );
    return NextResponse.redirect(home);
  }

  try {
    const data = await exchangeRequestToken(requestToken);
    await storeToken(data.access_token);
    home.searchParams.set('kite', 'connected');
    if (data.user_name) home.searchParams.set('kite_user', data.user_name);
    return NextResponse.redirect(home);
  } catch (e) {
    home.searchParams.set('kite', 'error');
    home.searchParams.set('kite_msg', (e as Error).message.slice(0, 200));
    return NextResponse.redirect(home);
  }
}
