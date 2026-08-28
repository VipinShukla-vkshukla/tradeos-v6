'use client';

// TradeOS v7 — Preflight
//
// Runs every readiness check the moment the page opens and answers ONE
// question above the fold: can I trust today's output?
//
// Design rules:
//   - Verdict first. Everything else is drill-down.
//   - Blockers sort to the top. You should never have to hunt for the problem.
//   - Every failure carries its exact remedy - a health page that says "stale"
//     without saying "run step 13" has only relocated the problem.
//   - Green checks stay collapsed. Attention is the scarce resource.
//
// Auto-refreshes every 60s so it can be left open on a second screen.

import { useCallback, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { ChevronLeft } from 'lucide-react';
import { KiteConnect } from '@/components/core/KiteConnect';
import { Header } from '@/components/core/Header';
import { Sidebar } from '@/components/core/Sidebar';

type Sev = 'OK' | 'WARN' | 'BLOCK' | 'INFO';
interface Check {
  id: string; group: string; label: string; severity: Sev;
  value: string; expected: string; detail?: string; fix?: string;
}
interface Health {
  verdict: 'READY' | 'DEGRADED' | 'BLOCKED';
  generated_at: string;
  trade_date: string | null;
  is_weekend: boolean;
  counts: { ok: number; warn: number; block: number; info: number };
  checks: Check[];
  message?: string;
}

const SEV_ORDER: Record<Sev, number> = { BLOCK: 0, WARN: 1, INFO: 2, OK: 3 };

const SEV_STYLE: Record<Sev, { dot: string; text: string; chip: string }> = {
  BLOCK: { dot: 'bg-rose-500',    text: 'text-rose-300',    chip: 'border-rose-500/40 bg-rose-500/10 text-rose-300' },
  WARN:  { dot: 'bg-amber-500',   text: 'text-amber-300',   chip: 'border-amber-500/40 bg-amber-500/10 text-amber-300' },
  OK:    { dot: 'bg-emerald-500', text: 'text-emerald-300', chip: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' },
  INFO:  { dot: 'bg-zinc-500',    text: 'text-zinc-400',    chip: 'border-zinc-700 bg-zinc-800/40 text-zinc-400' },
};

const VERDICT = {
  READY:    { label: 'READY',    blurb: 'All checks passed. Today’s output is trustworthy.',            cls: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' },
  DEGRADED: { label: 'DEGRADED', blurb: 'Usable, but something is not as it should be. Read the warnings.', cls: 'border-amber-500/40 bg-amber-500/10 text-amber-300' },
  BLOCKED:  { label: 'BLOCKED',  blurb: 'Do not act on today’s signals until the blockers below are cleared.', cls: 'border-rose-500/40 bg-rose-500/10 text-rose-300' },
};

export default function Preflight() {
  const [h, setH] = useState<Health | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [showOk, setShowOk] = useState(false);
  const [ranAt, setRanAt] = useState<Date | null>(null);

  const run = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      const j = (await r.json()) as Health;
      setH(j); setRanAt(new Date());
      if (!r.ok && j.message) setErr(j.message);
    } catch (e) {
      setErr((e as Error).message);
    } finally { setLoading(false); }
  }, []);

  // Runs on open — this is the "one click" being no clicks at all.
  useEffect(() => { void run(); }, [run]);
  useEffect(() => {
    const t = setInterval(() => void run(), 60000);
    return () => clearInterval(t);
  }, [run]);

  const grouped = useMemo(() => {
    if (!h) return [];
    const visible = h.checks.filter((c) => showOk || c.severity !== 'OK');
    const byGroup = new Map<string, Check[]>();
    for (const c of visible) {
      if (!byGroup.has(c.group)) byGroup.set(c.group, []);
      byGroup.get(c.group)!.push(c);
    }
    return [...byGroup.entries()]
      .map(([g, cs]) => ({
        group: g,
        checks: cs.sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]),
        worst: Math.min(...cs.map((c) => SEV_ORDER[c.severity])),
      }))
      .sort((a, b) => a.worst - b.worst);
  }, [h, showOk]);

  const actions = useMemo(
    () => (h?.checks ?? []).filter((c) => c.fix && c.severity !== 'OK'),
    [h]
  );

  const v = h ? VERDICT[h.verdict] : null;

  return (
    <div className="h-screen bg-dashboard flex flex-col overflow-hidden">
      <Header />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 min-w-0 overflow-auto p-4">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-muted-foreground hover:text-foreground">
              <ChevronLeft className="h-4 w-4" />
            </Link>
            <div>
              <h1 className="text-sm font-semibold tracking-tight text-foreground">Preflight</h1>
              <p className="text-[11px] text-muted-foreground">
                {h?.trade_date ? `trade date ${h.trade_date}` : 'checking…'}
                {h?.is_weekend && ' · weekend'}
                {ranAt && ` · checked ${ranAt.toLocaleTimeString()}`}
              </p>
            </div>
          </div>
          <button
            onClick={() => void run()} disabled={loading}
            className="rounded bg-sky-600 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {loading ? 'Checking…' : 'Re-check'}
          </button>
        </div>

        {err && (
          <div className="mb-5 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
            {err}
          </div>
        )}

        {/* ── Verdict ─────────────────────────────────────────────────── */}
        {loading && !h ? (
          <div className="h-28 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/40" />
        ) : h && v ? (
          <div className={`rounded-xl border p-5 ${v.cls}`}>
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="font-mono text-3xl font-bold tracking-tight">{v.label}</div>
                <p className="mt-1 text-xs opacity-90">{v.blurb}</p>
              </div>
              <div className="flex gap-4 text-center">
                {([['block','Blockers'],['warn','Warnings'],['ok','Passed']] as const).map(([k, lbl]) => (
                  <div key={k}>
                    <div className="font-mono text-xl leading-none">{h.counts[k]}</div>
                    <div className="mt-1 text-[10px] uppercase tracking-wide opacity-70">{lbl}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {/* ── Step one of the morning: the broker session ──────────────── */}
        {/* Placed above the check list because it is the only item that is
            both required daily and impossible for the system to do itself.
            Re-runs the health check on success so the Broker row updates
            without a manual refresh. */}
        <section className="mt-5">
          <h2 className="mb-2 text-[11px] uppercase tracking-wide text-zinc-500">broker session</h2>
          <KiteConnect onChange={() => void run()} />
        </section>

        {/* ── What to do ──────────────────────────────────────────────── */}
        {actions.length > 0 && (
          <section className="mt-5">
            <h2 className="mb-2 text-[11px] uppercase tracking-wide text-zinc-500">
              do this — {actions.length} item{actions.length > 1 ? 's' : ''}
            </h2>
            <div className="space-y-2">
              {actions
                .sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity])
                .map((c) => (
                  <div key={c.id} className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3">
                    <div className="flex items-start gap-2">
                      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${SEV_STYLE[c.severity].dot}`} />
                      <div className="min-w-0 flex-1">
                        <div className="text-xs font-medium text-zinc-200">{c.label}</div>
                        <div className="mt-0.5 text-[11px] leading-relaxed text-zinc-400">{c.fix}</div>
                        {c.fix?.startsWith('python') && (
                          <code className="mt-1.5 block rounded bg-zinc-950 px-2 py-1 font-mono text-[11px] text-sky-300">
                            {c.fix}
                          </code>
                        )}
                      </div>
                    </div>
                  </div>
                ))}
            </div>
          </section>
        )}

        {/* ── All checks ──────────────────────────────────────────────── */}
        <section className="mt-6">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-[11px] uppercase tracking-wide text-zinc-500">checks</h2>
            <button onClick={() => setShowOk((s) => !s)} className="text-[11px] text-sky-400 hover:text-sky-300">
              {showOk ? 'hide passing' : `show all (${h?.counts.ok ?? 0} passing)`}
            </button>
          </div>

          <div className="space-y-4">
            {grouped.map(({ group, checks }) => (
              <div key={group} className="overflow-hidden rounded-lg border border-zinc-800">
                <div className="border-b border-zinc-800 bg-zinc-900/80 px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
                  {group}
                </div>
                <div className="divide-y divide-zinc-800/70">
                  {checks.map((c) => (
                    <div key={c.id} className="flex items-start gap-3 bg-zinc-900/30 px-3 py-2.5">
                      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${SEV_STYLE[c.severity].dot}`} />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline gap-x-2">
                          <span className="text-xs text-zinc-200">{c.label}</span>
                          <span className={`font-mono text-[11px] ${SEV_STYLE[c.severity].text}`}>{c.value}</span>
                          <span className="text-[10px] text-zinc-600">expected {c.expected}</span>
                        </div>
                        {c.detail && <div className="mt-0.5 text-[11px] text-zinc-500">{c.detail}</div>}
                      </div>
                      <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${SEV_STYLE[c.severity].chip}`}>
                        {c.severity}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        <p className="mt-8 text-[10px] leading-relaxed text-zinc-600">
          Re-checks automatically every 60 seconds. Every check here corresponds to a failure
          mode that was live in this system at some point — stale sector ranks, signals with no
          stop, positions with no plan, an expired broker session, a config blob whose keys the
          code never read. A check that cannot fail is not a check.
        </p>
        </main>
      </div>
    </div>
  );
}
