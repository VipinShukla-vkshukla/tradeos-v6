'use client';

// components/core/KiteConnect.tsx
//
// The daily broker session, as one button.
//
// Without a valid Kite session the intraday monitor cannot check a single
// stop — and it deliberately SKIPS rather than falling back to the previous
// session's closes, so the failure is silent unless something says so. This
// component is that something.
//
// Two flows, depending on what the Kite app's Redirect URL is set to:
//   registered as /api/kite/callback  → press Connect, log in, land back here
//                                       with the session already stored
//   registered as anything else       → press Connect, log in, paste the URL
//                                       you landed on into the field
// Both end at the same server-side exchange.

import { useCallback, useEffect, useState } from 'react';

interface Status {
  configured: boolean;
  valid: boolean;
  issued_at: string | null;
  login_url: string | null;
  expires_hint: string;
  /** Set when the session looks fresh but may not work — see sessionStatus(). */
  issue?: string;
}

export function KiteConnect({ onChange }: { onChange?: () => void }) {
  const [st, setSt] = useState<Status | null>(null);
  const [paste, setPaste] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [showPaste, setShowPaste] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetch('/api/kite', { cache: 'no-store' });
      setSt(await r.json());
    } catch { /* rendered as "unknown" below */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Surface the result of the callback redirect, then strip the query so a
  // refresh does not replay a stale banner.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const k = q.get('kite');
    if (!k) return;
    if (k === 'connected') {
      setMsg({ kind: 'ok', text: `Connected${q.get('kite_user') ? ` as ${q.get('kite_user')}` : ''}. Session valid until about 07:30 IST tomorrow.` });
      void load(); onChange?.();
    } else if (k === 'error') {
      setMsg({ kind: 'err', text: q.get('kite_msg') || 'Kite login failed.' });
    }
    window.history.replaceState({}, '', window.location.pathname);
  }, [load, onChange]);

  const submitPaste = useCallback(async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await fetch('/api/kite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_token: paste }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.message ?? 'Exchange failed');
      setMsg({ kind: 'ok', text: `Connected${j.user_name ? ` as ${j.user_name}` : ''}.` });
      setPaste(''); setShowPaste(false);
      await load(); onChange?.();
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    } finally { setBusy(false); }
  }, [paste, load, onChange]);

  if (!st) {
    return <div className="h-20 animate-pulse rounded-lg border border-zinc-800 bg-zinc-900/40" />;
  }

  if (!st.configured) {
    return (
      <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-xs text-amber-300">
        KITE_API_KEY / KITE_API_SECRET are not set in <code className="font-mono">frontend/.env.local</code>.
        Add them there (the same values the backend uses) and restart the dev server.
      </div>
    );
  }

  const issued = st.issued_at ? new Date(st.issued_at) : null;

  return (
    <div className={`rounded-lg border p-4 ${st.valid ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-amber-500/40 bg-amber-500/5'}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={`h-2 w-2 rounded-full ${st.valid ? 'bg-emerald-400' : 'bg-amber-400'}`} />
            <span className="text-sm font-medium text-zinc-100">
              {st.valid ? 'Broker session active' : 'Broker session required'}
            </span>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-zinc-400">
            {st.valid
              ? <>Connected {issued ? `at ${issued.toLocaleTimeString()}` : ''} · expires {st.expires_hint}. Intraday stop monitoring is live.</>
              : <>Zerodha invalidates the session every morning around 07:30 IST. Until you reconnect, stop-loss monitoring skips rather than acting on the previous session&apos;s closes.</>}
          </p>
          {/* A caveat on an otherwise-green session. "Broker session active" was
              displayed for a token minted by a retired Kite app — fresh, stored,
              and rejected by every API call the pipeline made. */}
          {st.issue && (
            <p className="mt-1 text-[11px] leading-relaxed text-amber-400">{st.issue}</p>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {st.login_url && (
            <a
              href={st.login_url}
              className={`rounded px-3 py-1.5 text-[11px] font-medium ${st.valid ? 'border border-zinc-700 text-zinc-300 hover:text-zinc-100' : 'bg-amber-500 text-zinc-950 hover:bg-amber-400'}`}
            >
              {st.valid ? 'Reconnect' : 'Connect Kite'}
            </a>
          )}
          <button
            onClick={() => setShowPaste((v) => !v)}
            className="rounded border border-zinc-800 px-2 py-1.5 text-[11px] text-zinc-500 hover:text-zinc-300"
            title="Use this if your Kite app's Redirect URL does not point at this dashboard"
          >
            Paste URL
          </button>
        </div>
      </div>

      {showPaste && (
        <div className="mt-3 border-t border-zinc-800 pt-3">
          <p className="mb-1.5 text-[11px] text-zinc-500">
            After logging in, copy the address bar and paste it here.
          </p>
          <div className="flex gap-2">
            <input
              value={paste}
              onChange={(e) => setPaste(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void submitPaste(); }}
              placeholder="http://127.0.0.1:3000/?request_token=..."
              className="flex-1 rounded border border-zinc-800 bg-zinc-950 px-2 py-1.5 font-mono text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:border-sky-500/50 focus:outline-none"
            />
            <button
              onClick={() => void submitPaste()}
              disabled={busy || paste.length < 10}
              className="rounded bg-sky-600 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-40"
            >
              {busy ? 'Exchanging…' : 'Connect'}
            </button>
          </div>
          <p className="mt-2 text-[10px] leading-relaxed text-zinc-600">
            For zero-paste: set your Kite app&apos;s Redirect URL to{' '}
            <code className="font-mono text-zinc-400">http://127.0.0.1:3000/api/kite/callback</code>
          </p>
        </div>
      )}

      {msg && (
        <div className={`mt-3 rounded border px-2.5 py-1.5 text-[11px] ${msg.kind === 'ok' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-rose-500/30 bg-rose-500/10 text-rose-300'}`}>
          {msg.text}
        </div>
      )}
    </div>
  );
}
