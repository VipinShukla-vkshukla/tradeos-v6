'use client';

// TradeOS v7 — Strategy Control Room
//
// A standalone surface at /control, deliberately separate from the tabbed
// console. The console answers "what happened". This answers "what should the
// system do differently, and what will that change".
//
// The organising idea: NEVER SHOW A CONTROL WITHOUT ITS CONSEQUENCE.
// Every threshold sits beside a live funnel computed against today's real
// universe, and beside the engine's measured hit rate. Changing a number shows
// which symbols enter and leave before anything is committed. Tuning stops
// being a guess and becomes an experiment you can read the result of.
//
// Three things it is built to make routine:
//   INTEGRATE   ship a new engine in SHADOW — it logs candidates and
//               accumulates screener_performance history while contributing
//               nothing to composite_score
//   MODIFY      drag a gate, read the impact, commit with a reason that lands
//               in engine_lifecycle_log alongside the evidence
//   DECOMMISSION retire an engine in one click, reversibly, with the audit
//               trail intact

import { useCallback, useEffect, useMemo, useState } from 'react';
import { EngineCard } from '@/components/control/EngineCard';
import {
  gateFunnel,
  type EngineConfig, type EnginePerf, type Gates, type Lifecycle, type Stock,
} from '@/lib/engineGates';
import { OperatorPanel } from '@/components/core/OperatorPanel';

interface SectorRow {
  sector: string;
  rank: number;
  stock_count: number;
  rotation_score: number | null;
  rank_delta_5d: number | null;
  sector_state: string | null;
  qualified: boolean | null;
}

export default function ControlRoom() {
  const [engines, setEngines] = useState<EngineConfig[]>([]);
  const [perf, setPerf] = useState<EnginePerf[]>([]);
  const [stocks, setStocks] = useState<Stock[]>([]);
  const [sectorRank, setSectorRank] = useState<Record<string, number>>({});
  const [sectors, setSectors] = useState<SectorRow[]>([]);
  const [tradeDate, setTradeDate] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [eRes, uRes] = await Promise.all([
        fetch('/api/engines'),
        fetch('/api/engines/universe'),
      ]);
      if (!eRes.ok) throw new Error((await eRes.json()).message ?? 'engine load failed');
      if (!uRes.ok) throw new Error((await uRes.json()).message ?? 'universe load failed');
      const e = await eRes.json();
      const u = await uRes.json();
      setEngines(e.engines ?? []);
      setPerf(e.performance ?? []);
      setStocks(u.stocks ?? []);
      setSectorRank(u.sectorRank ?? {});
      setSectors(u.sectors ?? []);
      setTradeDate(u.trade_date ?? '');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const handleSave = useCallback(
    async (engine: string, patch: { lifecycle?: Lifecycle; gates?: Gates; reason: string; evidence: unknown }) => {
      const res = await fetch('/api/engines', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine, ...patch }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.message ?? 'save failed');
      setToast(json.warning ? `${engine}: ${json.warning}` : `${engine} updated`);
      setTimeout(() => setToast(null), 4000);
      await load();
    },
    [load]
  );

  /**
   * Engine input universes.
   *
   * Most engines screen all ~499 stocks. TPO does not — run_tpo iterates over
   * `ctl_results`, so it can only ever see what CTL already passed. Feeding it
   * the full universe made its card read 75 candidates against a real engine
   * output of 9. Anything that depends on another engine has to be screened
   * against that engine's survivors, or the preview is fiction.
   */
  const DEPENDS_ON: Record<string, string> = { TPO: 'CTL' };

  const universes = useMemo(() => {
    const out: Record<string, { stocks: Stock[]; upstream: { engine: string; size: number } | null }> = {};
    const survivorsOf = (name: string): Stock[] => {
      const e = engines.find((x) => x.strategy === name);
      if (!e) return stocks;
      return gateFunnel(stocks, (e.params?.gates ?? {}) as Gates, sectorRank).survivors;
    };
    for (const e of engines) {
      const dep = DEPENDS_ON[e.strategy];
      if (dep) {
        const s = survivorsOf(dep);
        out[e.strategy] = { stocks: s, upstream: { engine: dep, size: s.length } };
      } else {
        out[e.strategy] = { stocks, upstream: null };
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engines, stocks, sectorRank]);

  // Portfolio-level view of what the engine set currently produces.
  const totals = useMemo(() => {
    const active = engines.filter((e) => e.lifecycle === 'ACTIVE');
    const shadow = engines.filter((e) => e.lifecycle === 'SHADOW');
    const retired = engines.filter((e) => e.lifecycle === 'RETIRED');
    const union = new Set<string>();
    for (const e of active) {
      for (const s of gateFunnel(stocks, (e.params?.gates ?? {}) as Gates, sectorRank).survivors) {
        union.add(s.symbol);
      }
    }
    return { active: active.length, shadow: shadow.length, retired: retired.length, reach: union.size };
  }, [engines, stocks, sectorRank]);

  const topSectors = useMemo(
    () => [...sectors].filter((s) => s.rank).sort((a, b) => a.rank - b.rank).slice(0, 8),
    [sectors]
  );

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      {/* ── Header ────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-20 border-b border-zinc-800 bg-zinc-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-6 py-3">
          <div>
            <h1 className="text-sm font-semibold tracking-tight">Strategy Control Room</h1>
            <p className="text-[11px] text-zinc-500">
              {tradeDate ? `universe ${tradeDate} · ${stocks.length} stocks` : 'loading universe…'}
            </p>
          </div>
          <div className="flex items-center gap-5">
            {[
              { label: 'active', value: totals.active, tone: 'text-emerald-400' },
              { label: 'shadow', value: totals.shadow, tone: 'text-amber-400' },
              { label: 'retired', value: totals.retired, tone: 'text-zinc-500' },
              { label: 'reach', value: totals.reach, tone: 'text-sky-400' },
            ].map((s) => (
              <div key={s.label} className="text-right">
                <div className={`font-mono text-lg leading-none ${s.tone}`}>{s.value}</div>
                <div className="text-[10px] uppercase tracking-wide text-zinc-600">{s.label}</div>
              </div>
            ))}
            <button
              onClick={() => void load()}
              className="rounded border border-zinc-800 px-3 py-1.5 text-[11px] text-zinc-400 hover:text-zinc-200"
            >
              Refresh
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-6 py-6">
        {/* Operator controls first. Engine tuning changes which trades are
            proposed; these decide whether any of them can happen at all. */}
        <div className="mb-6">
          <OperatorPanel />
        </div>

        {error && (
          <div className="mb-5 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-300">
            {error}
            <div className="mt-1 text-rose-400/70">
              If this mentions a missing table or view, run migration 006_engine_registry.sql.
            </div>
          </div>
        )}

        {/* ── Sector rotation strip ───────────────────────────────────── */}
        {topSectors.length > 0 && (
          <section className="mb-6">
            <h2 className="mb-2 text-[11px] uppercase tracking-wide text-zinc-500">
              {/* NOT a gate. Nothing in the pipeline refuses a stock for its
                  sector rank — it is a weighted SCORE input: 5% of final_score
                  (100/82/64/46/25 by rank band) and up to 6 of 100 points of
                  validity_score. Saying "gates" invites the belief that a
                  bottom-ranked sector cannot produce a signal, which is false
                  and would hide a real concentration. Swing only: the intraday
                  engines do not read sector strength at all. */}
              sector rotation · swing scoring input, ~5% of final_score
            </h2>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {topSectors.map((s) => {
                const d = s.rank_delta_5d ?? 0;
                return (
                  <div
                    key={s.sector}
                    className="min-w-[142px] shrink-0 rounded-lg border border-zinc-800 bg-zinc-900/60 p-2.5"
                  >
                    <div className="flex items-baseline justify-between">
                      <span className="font-mono text-[11px] text-zinc-500">#{s.rank}</span>
                      <span
                        className={`font-mono text-[10px] ${
                          d > 0 ? 'text-emerald-400' : d < 0 ? 'text-rose-400' : 'text-zinc-600'
                        }`}
                      >
                        {d > 0 ? `▲${d}` : d < 0 ? `▼${Math.abs(d)}` : '—'}
                      </span>
                    </div>
                    <div className="mt-0.5 truncate text-xs capitalize text-zinc-200">{s.sector}</div>
                    <div className="mt-1.5 h-1 overflow-hidden rounded bg-zinc-800">
                      <div
                        className="h-full bg-gradient-to-r from-sky-500 to-emerald-500"
                        style={{ width: `${Math.round((s.rotation_score ?? 0) * 100)}%` }}
                      />
                    </div>
                    <div className="mt-1 flex justify-between text-[9px] text-zinc-600">
                      <span>n={s.stock_count}</span>
                      <span>rot {((s.rotation_score ?? 0) * 100).toFixed(0)}%</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* ── Engines ─────────────────────────────────────────────────── */}
        <section>
          <h2 className="mb-3 text-[11px] uppercase tracking-wide text-zinc-500">
            engines · drag a gate to preview its effect before committing
          </h2>

          {loading ? (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 9 }).map((_, i) => (
                <div key={i} className="h-64 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900/40" />
              ))}
            </div>
          ) : (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {engines.map((e) => (
                <EngineCard
                  key={e.strategy}
                  engine={e}
                  perf={perf.filter((p) => p.engine === e.strategy)}
                  stocks={universes[e.strategy]?.stocks ?? stocks}
                  upstream={universes[e.strategy]?.upstream ?? null}
                  sectorRank={sectorRank}
                  onSave={handleSave}
                />
              ))}
            </div>
          )}

          {!loading && engines.length === 0 && !error && (
            <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-4 text-xs text-amber-300">
              No engines registered. Run <code className="font-mono">006_engine_registry.sql</code> to
              seed all nine.
            </div>
          )}
        </section>
      </main>

      {toast && (
        <div className="fixed bottom-5 right-5 z-30 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-xs text-emerald-300 shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
