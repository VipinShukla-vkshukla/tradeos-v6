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
import Link from 'next/link';
import { ChevronLeft } from 'lucide-react';
import { EngineCard } from '@/components/control/EngineCard';
import {
  gateFunnel,
  type EngineConfig, type EnginePerf, type Gates, type Lifecycle, type Stock,
} from '@/lib/engineGates';
import { OperatorPanel } from '@/components/core/OperatorPanel';
import { Header } from '@/components/core/Header';
import { Sidebar } from '@/components/core/Sidebar';
import type { RegimeState } from '@/types/database';
import { isSupabaseConfigured, queries } from '@/lib/supabase';

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
  const [regime, setRegime] = useState<RegimeState>('NEUTRAL');
  const [aiStatus, setAiStatus] = useState<'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED'>('AVAILABLE');

  // ── Advanced tuning (collapsed) ──────────────────────────────────────
  const ADVANCED_KEYS = [
    'sizing_max_cost_r', 'sizing_max_mult', 'sizing_min_mult',
    'exit_runners_enabled', 'exit_deterioration_enabled', 'exit_runner_cap_enforced',
    'overlay_expiry_enabled', 'overlay_vol_scaling_enabled', 'overlay_liquidity_enabled',
    'overlay_liquidity_strict', 'governance_freeze_enabled', 'governance_require_oos',
    'rank_weight_tier', 'rank_weight_conviction', 'intraday_structure_gate',
    'intraday_news_gate_enabled', 'intraday_gtt_enabled', 'intraday_quote_mode_range',
    'intraday_quote_mode_vwap', 'intraday_allow_shorts', 'intraday_strategies_enabled',
    'storage_rolloff_enabled', 'storage_staging_rolloff_enabled', 'storage_fail_pct',
  ];
  const [advOpen, setAdvOpen] = useState(false);
  const [advCfg, setAdvCfg] = useState<Record<string, string>>({});
  const [advBusy, setAdvBusy] = useState<string | null>(null);
  const [advMsg, setAdvMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getMarketRegime().then(({ data }) => {
      if (data?.[0]) setRegime((data[0].computed_regime ?? data[0].regime) as RegimeState);
    });
  }, []);

  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getSystemConfig(['ai_active_provider', 'ai_provider', 'ai_fallback_mode']).then(({ data }) => {
      if (!data) return;
      const cfg = Object.fromEntries(data.map((r) => [r.key, r.value]));
      const provider = cfg['ai_active_provider'] || cfg['ai_provider'];
      if (!provider || provider === 'disabled') setAiStatus('DISABLED');
      else if (cfg['ai_fallback_mode'] === 'true') setAiStatus('DEGRADED');
      else setAiStatus('AVAILABLE');
    });
  }, []);

  const loadAdv = useCallback(async () => {
    if (!isSupabaseConfigured()) return;
    const { data } = await queries.getSystemConfig(ADVANCED_KEYS);
    if (data) {
      const m: Record<string, string> = {};
      for (const r of data) m[r.key] = r.value;
      setAdvCfg(m);
    }
  }, []);

  const writeAdv = useCallback(async (key: string, value: string) => {
    setAdvBusy(key);
    setAdvMsg(null);
    try {
      const res = await fetch(`/api/config/${encodeURIComponent(key)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value, reason: 'Control Room advanced tuning' }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.message ?? 'save failed');
      setAdvCfg((prev) => ({ ...prev, [key]: value }));
      setAdvMsg(`${key} updated`);
      setTimeout(() => setAdvMsg(null), 3000);
    } catch (err) {
      setAdvMsg((err as Error).message);
    } finally {
      setAdvBusy(null);
    }
  }, []);

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
  useEffect(() => { if (advOpen) void loadAdv(); }, [advOpen, loadAdv]);

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
    <div className="h-screen bg-dashboard flex flex-col overflow-hidden">
      <Header marketRegime={regime} aiProviderStatus={aiStatus} />
      <div className="flex flex-1 min-h-0">
        <Sidebar />
        <main className="flex-1 min-w-0 overflow-auto p-4">
          <div className="flex items-center justify-between gap-4 mb-4">
            <div className="flex items-center gap-3">
              <Link href="/" className="text-muted-foreground hover:text-foreground">
                <ChevronLeft className="h-4 w-4" />
              </Link>
              <div>
                <h1 className="text-sm font-semibold tracking-tight text-foreground">Strategy Control Room</h1>
                <p className="text-[11px] text-muted-foreground">
                  {tradeDate ? `universe ${tradeDate} · ${stocks.length} stocks` : 'loading universe…'}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-5">
              {[
                { label: 'active', value: totals.active, tone: 'text-emerald-400' },
                { label: 'shadow', value: totals.shadow, tone: 'text-amber-400' },
                { label: 'retired', value: totals.retired, tone: 'text-muted-foreground' },
                { label: 'reach', value: totals.reach, tone: 'text-sky-400' },
              ].map((s) => (
                <div key={s.label} className="text-right">
                  <div className={`font-mono text-lg leading-none ${s.tone}`}>{s.value}</div>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground/70">{s.label}</div>
                </div>
              ))}
              <button
                onClick={() => void load()}
                className="rounded border border-border px-3 py-1.5 text-[11px] text-muted-foreground hover:text-foreground"
              >
                Refresh
              </button>
            </div>
          </div>

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

        {/* ── Advanced tuning (collapsed by default) ───────────────────── */}
        <section className="mt-6">
          <button
            onClick={() => setAdvOpen((v) => !v)}
            className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-muted-foreground hover:text-foreground"
          >
            <span>{advOpen ? '▼' : '▶'}</span>
            Advanced tuning — sizing multipliers, vol bands, scanner params, overlays, governance, storage
          </button>
          {advOpen && (
            <div className="mt-3 rounded-lg border border-border/50 p-3 space-y-3">
              {advMsg && (
                <div className="text-xs text-emerald-300">{advMsg}</div>
              )}
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                {ADVANCED_KEYS.map((key) => {
                  const val = advCfg[key] ?? '';
                  const isBool = val === 'true' || val === 'false' || key.endsWith('_enabled') || key.endsWith('_strict') || key.endsWith('_required');
                  return (
                    <div key={key} className="flex flex-col gap-1">
                      <label className="text-[10px] text-muted-foreground font-mono">{key}</label>
                      {isBool ? (
                        <button
                          disabled={advBusy === key}
                          onClick={() => writeAdv(key, val === 'true' ? 'false' : 'true')}
                          className={`rounded border px-2 py-1 text-xs font-mono ${
                            val === 'true'
                              ? 'border-emerald-500/40 text-emerald-300'
                              : 'border-border/50 text-muted-foreground'
                          }`}
                        >
                          {val === 'true' ? 'ON' : 'OFF'}
                        </button>
                      ) : (
                        <input
                          type="text"
                          defaultValue={val}
                          disabled={advBusy === key}
                          onBlur={(e) => {
                            const v = e.target.value.trim();
                            if (v && v !== val) writeAdv(key, v);
                          }}
                          className="bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums"
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </section>

        {/* ── Sector rotation strip ───────────────────────────────────── */}
        {topSectors.length > 0 && (
          <section className="mb-6">
            <h2 className="mb-2 text-[11px] uppercase tracking-wide text-muted-foreground">
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
                    className="min-w-[142px] shrink-0 rounded-lg border border-border bg-panel-hover/60 p-2.5"
                  >
                    <div className="flex items-baseline justify-between">
                      <span className="font-mono text-[11px] text-muted-foreground">#{s.rank}</span>
                      <span
                        className={`font-mono text-[10px] ${
                          d > 0 ? 'text-profit' : d < 0 ? 'text-loss' : 'text-muted-foreground/60'
                        }`}
                      >
                        {d > 0 ? `▲${d}` : d < 0 ? `▼${Math.abs(d)}` : '—'}
                      </span>
                    </div>
                    <div className="mt-0.5 truncate text-xs capitalize text-foreground">{s.sector}</div>
                    <div className="mt-1.5 h-1 overflow-hidden rounded bg-border">
                      <div
                        className="h-full bg-gradient-to-r from-sky-500 to-emerald-500"
                        style={{ width: `${Math.round((s.rotation_score ?? 0) * 100)}%` }}
                      />
                    </div>
                    <div className="mt-1 flex justify-between text-[9px] text-muted-foreground/70">
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
          <h2 className="mb-3 text-[11px] uppercase tracking-wide text-muted-foreground">
            engines · drag a gate to preview its effect before committing
          </h2>

          {loading ? (
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 9 }).map((_, i) => (
                <div key={i} className="h-64 animate-pulse rounded-xl border border-border bg-panel-hover/40" />
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
      </div>

      {toast && (
        <div className="fixed bottom-5 right-5 z-30 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-4 py-2 text-xs text-emerald-300 shadow-lg">
          {toast}
        </div>
      )}
    </div>
  );
}
