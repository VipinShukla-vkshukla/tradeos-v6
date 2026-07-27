// lib/kiteServer.ts
//
// Server-only helpers for the Kite Connect session.
//
// SECURITY: KITE_API_SECRET must never reach the browser. Everything here runs
// in Node route handlers; nothing in this file may be imported by a client
// component. The browser only ever sees the login URL (which contains the
// api_key — not a secret, it appears in every Kite login URL) and a boolean
// "is the session valid".
//
// The access token is written to system_config, matching what
// backend/kite/token_manager.py reads. Migration 007 marks that row is_secret
// so RLS hides it from the anon key that ships in the dashboard bundle.

import crypto from 'crypto';
import { createClient } from '@supabase/supabase-js';

export const TOKEN_KEY = 'kite_access_token';
export const TOKEN_DATE_KEY = 'kite_access_token_date';

export function svcClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('SUPABASE_SERVICE_ROLE_KEY not configured');
  return createClient(url, key);
}

export function apiKey(): string {
  const k = process.env.KITE_API_KEY;
  if (!k) throw new Error('KITE_API_KEY not set in .env.local');
  return k;
}

function apiSecret(): string {
  const s = process.env.KITE_API_SECRET;
  if (!s) throw new Error('KITE_API_SECRET not set in .env.local');
  return s;
}

export function loginUrl(): string {
  return `https://kite.zerodha.com/connect/login?api_key=${apiKey()}&v=3`;
}

/**
 * Exchange a one-shot request_token for an access_token.
 *
 * Implements Kite's session flow directly rather than pulling in the Node SDK:
 * checksum = SHA256(api_key + request_token + api_secret), POSTed as form data.
 * One dependency-free function is easier to audit than a client library for the
 * single call we make.
 */
export async function exchangeRequestToken(requestToken: string): Promise<{
  access_token: string; user_id?: string; user_name?: string;
}> {
  const checksum = crypto
    .createHash('sha256')
    .update(apiKey() + requestToken + apiSecret())
    .digest('hex');

  const res = await fetch('https://api.kite.trade/session/token', {
    method: 'POST',
    headers: {
      'X-Kite-Version': '3',
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams({
      api_key: apiKey(),
      request_token: requestToken,
      checksum,
    }),
  });

  const json = await res.json().catch(() => ({}));
  if (!res.ok || json?.status === 'error') {
    // request_tokens are single-use and expire in minutes; that is nearly
    // always the cause, so say so rather than surfacing a bare API string.
    const msg = json?.message || `HTTP ${res.status}`;
    throw new Error(
      /token/i.test(msg)
        ? `${msg} — request_tokens are single-use and expire within minutes. Start the login again.`
        : msg
    );
  }
  return json.data;
}

/** Persist the session where both the dashboard and the Python pipeline read it. */
export async function storeToken(accessToken: string): Promise<void> {
  const sb = svcClient();
  const nowIso = new Date().toISOString();
  const rows = [
    { key: TOKEN_KEY, value: accessToken },
    { key: TOKEN_DATE_KEY, value: nowIso },
  ];
  for (const r of rows) {
    const { data } = await sb.from('system_config').select('key').eq('key', r.key);
    if (data && data.length) {
      await sb.from('system_config').update({ value: r.value, updated_at: nowIso }).eq('key', r.key);
    } else {
      await sb.from('system_config').insert({
        ...r,
        description: 'Kite Connect session — written by the dashboard',
        is_secret: true,
      });
    }
  }
}

/**
 * Zerodha invalidates tokens daily at ~07:30 IST.
 *
 * Both sides of the comparison must be REAL instants. Building the boundary by
 * mutating an IST-shifted pseudo-date yields 07:30Z, five and a half hours
 * early, which reports a freshly minted token as expired — a bug that shipped
 * once already. Construct it from the IST calendar date with an explicit
 * offset instead.
 */
export function expiryBoundary(): Date {
  const nowIst = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000);
  const istDate = nowIst.toISOString().slice(0, 10);
  let b = new Date(`${istDate}T07:30:00+05:30`);
  if (Date.now() < b.getTime()) b = new Date(b.getTime() - 86400000);
  return b;
}

export async function sessionStatus(): Promise<{
  configured: boolean; valid: boolean; issued_at: string | null; expires_hint: string;
}> {
  const configured = !!(process.env.KITE_API_KEY && process.env.KITE_API_SECRET);
  let issued: string | null = null;
  try {
    const sb = svcClient();
    const { data } = await sb.from('system_config').select('key,value')
      .in('key', [TOKEN_KEY, TOKEN_DATE_KEY]);
    const map = Object.fromEntries((data ?? []).map((r) => [r.key, r.value]));
    issued = map[TOKEN_DATE_KEY] ?? null;
    const valid = !!(map[TOKEN_KEY] && issued && new Date(issued) >= expiryBoundary());
    return { configured, valid, issued_at: issued, expires_hint: 'about 07:30 IST tomorrow' };
  } catch {
    return { configured, valid: false, issued_at: issued, expires_hint: 'about 07:30 IST tomorrow' };
  }
}

/** Accept a bare token or the whole redirect URL — picking it out by hand is the fiddly part. */
export function extractRequestToken(raw: string): string {
  const s = (raw || '').trim().replace(/^["']|["']$/g, '');
  if (s.includes('request_token=')) {
    try {
      return new URL(s).searchParams.get('request_token')?.trim() || s;
    } catch {
      return s.split('request_token=')[1]?.split('&')[0]?.trim() || s;
    }
  }
  return s;
}
