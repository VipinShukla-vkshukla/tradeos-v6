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
import fs from 'fs';
import path from 'path';
import { createClient } from '@supabase/supabase-js';

export const TOKEN_KEY = 'kite_access_token';
export const TOKEN_DATE_KEY = 'kite_access_token_date';
/** The api_key a stored token was minted under. See storeToken(). */
export const TOKEN_APIKEY_KEY = 'kite_access_token_api_key';

export function svcClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('SUPABASE_SERVICE_ROLE_KEY not configured');
  return createClient(url, key);
}

/**
 * Read a value from .env.local directly, bypassing process.env.
 *
 * Next.js gives real environment variables precedence over .env.local, which is
 * correct for deployments and wrong here. A stale Windows OS variable pinned
 * KITE_API_KEY to a retired Kite Connect app; the dev server inherited it at
 * launch and every "Connect" ran the ENTIRE login flow under the old app —
 * login URL, request_token, exchange, all consistent, all successful. The
 * dashboard reported "Broker session active" and the Python pipeline, reading
 * the correct key from backend/.env, rejected every call.
 *
 * .env.local is safe to prefer: it is gitignored and never exists in CI, so
 * there is no deployment where this rule can shadow a real secret.
 */
function fromEnvLocal(name: string): string | undefined {
  try {
    const file = path.join(process.cwd(), '.env.local');
    if (!fs.existsSync(file)) return undefined;
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
      if (!m || m[1] !== name) continue;
      return m[2].trim().replace(/^["']|["']$/g, '');
    }
  } catch {
    /* fall through to process.env */
  }
  return undefined;
}

export function apiKey(): string {
  const k = fromEnvLocal('KITE_API_KEY') ?? process.env.KITE_API_KEY;
  if (!k) throw new Error('KITE_API_KEY not set in .env.local');
  return k;
}

function apiSecret(): string {
  const s = fromEnvLocal('KITE_API_SECRET') ?? process.env.KITE_API_SECRET;
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
    // Record WHICH app minted this token. Without it, a token from a retired
    // Kite Connect app passes every freshness check — it is not stale, it is
    // wrong — and the only symptom is TokenException on calls the dashboard
    // never makes. backend/kite/token_manager.py reads this same row.
    { key: TOKEN_APIKEY_KEY, value: apiKey() },
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
  configured: boolean; valid: boolean; issued_at: string | null;
  expires_hint: string; issue?: string;
}> {
  let configured = false;
  try { configured = !!(apiKey() && apiSecret()); } catch { configured = false; }
  let issued: string | null = null;
  const hint = 'about 07:30 IST tomorrow';
  try {
    const sb = svcClient();
    const { data } = await sb.from('system_config').select('key,value')
      .in('key', [TOKEN_KEY, TOKEN_DATE_KEY, TOKEN_APIKEY_KEY]);
    const map = Object.fromEntries((data ?? []).map((r) => [r.key, r.value]));
    issued = map[TOKEN_DATE_KEY] ?? null;
    const fresh = !!(map[TOKEN_KEY] && issued && new Date(issued) >= expiryBoundary());

    // Freshness is not enough. A token minted by a DIFFERENT Kite Connect app
    // is perfectly fresh and completely unusable, and reporting it as "Broker
    // session active" is worse than reporting nothing — it sends you looking
    // for the problem everywhere except where it is.
    const issuer = map[TOKEN_APIKEY_KEY];
    let current: string | null = null;
    try { current = apiKey(); } catch { /* not configured */ }
    if (fresh && issuer && current && issuer !== current) {
      return {
        configured, valid: false, issued_at: issued, expires_hint: hint,
        issue: `This session was created by a different Kite app (${issuer.slice(0, 8)}…) `
             + `but the dashboard now uses ${current.slice(0, 8)}…. Reconnect to mint a new one.`,
      };
    }
    // A token with no recorded issuer predates that bookkeeping. It may be from
    // the old app, so say the check is inconclusive rather than implying it passed.
    const issue = fresh && !issuer
      ? 'This session predates issuer tracking — if broker calls fail, reconnect once to re-mint it.'
      : undefined;
    return { configured, valid: fresh, issued_at: issued, expires_hint: hint, issue };
  } catch {
    return { configured, valid: false, issued_at: issued, expires_hint: hint };
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
