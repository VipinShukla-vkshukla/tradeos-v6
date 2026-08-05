'use client';

/**
 * Stage 10's operator surface: the four views the master spec requires.
 *
 *   1. Today's allocation ledger, ordered by edge, verdict against hurdle
 *      with the reason in words.
 *   2. The live hurdle with today's proposals plotted against it.
 *   3. The storage gauge, red above 80%, with the projected ceiling date.
 *   4. Shadow comparison, reducible to one number: is the allocator ahead of
 *      greedy or behind it.
 *
 * WHY THIS TAB EXISTS AND WHAT IT DOES NOT DO
 * --------------------------------------------
 * allocation_decisions is the record of what the allocator decided, including
 * every DECLINE — that is the entire point of Phase 4 over what came before.
 * This tab reads that record. It writes nothing: the allocator's own switches
 * (alloc_live_intraday, alloc_live_swing, alloc_shadow_enabled) live in
 * OperatorPanel, because a page that shows evidence and a page that acts on it
 * being the same page is how a wrong click gets made while reading.
 *
 * THE DISAGREEMENT COUNT MIRRORS tools/allocator_report.py EXACTLY
 * ------------------------------------------------------------------
 * Greedy is not reconstructed from a ranking. It is read from what actually
 * happened: a TAKE with no matching position is the allocator wanting
 * something greedy never placed; a DECLINE or DEFER with a matching position
 * is greedy taking something the allocator would have refused. Two
 * implementations of the same definition would drift the moment one of them
 * was tuned and the other was not, so this is the same arithmetic in
 * TypeScript that the Python tool runs — see the comment on each, not a
 * shared library, because the backend tool must keep working with no frontend
 * running at all.
 */

import { useState, useEffect, useMemo, useCallback } from 'react';
import {
  Scale, TrendingUp, TrendingDown, Minus, HardDrive, ListChecks, AlertTriangle,
} from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonTable } from '@/components/core/DataGuard';
import { queries } from '@/lib/supabase';

interface AllocRow {
  id: number; decided_at: string; trade_date: string; symbol: string;
  framework: string; product: string; source: string | null;
  verdict: string; reason: string | null;
  entry: number | null; stop: number | null; target: number | null;
  quantity: number | null; edge: number | null; e_r: number | null;
  cost_r: number | null; hurdle: number | null;
  prior_n: number | null; prior_below_floor: boolean | null;
  native_rank: number | null; shadow: boolean; outcome_r: number | null;
  outcome_note: string | null;
}
interface HistRow {
  trade_date: string; symbol: string; product: string; framework: string;
  verdict: string; outcome_r: number | null; shadow: boolean;
}
interface StorageRow {
  table_name: string; total_size: string; total_bytes: number;
  approx_rows: number; pct_of_free_tier: number;
}

const VERDICT_TONE: Record<string, string> = {
  TAKE: 'bg-profit/15 text-profit',
  DEFER: 'bg-amber-500/15 text-amber-500',
  DECLINE: 'bg-muted text-muted-foreground',
};
const PROMOTION_THRESHOLD = 30;

function todayIso(): string {
  // IST, not the browser's local date — a decision made at 09:16 IST must not
  // read as "yesterday" on a machine west of India.
  return new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Kolkata' });
}

export function AllocatorTab() {
  const [today, setToday] = useState<AllocRow[]>([]);
  const [hist, setHist] = useState<HistRow[]>([]);
  const [positions, setPositions] = useState<{ symbol: string; product: string | null; entry_date: string }[]>([]);
  const [storage, setStorage] = useState<StorageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [lastLoad, setLastLoad] = useState<string>('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const since = new Date(Date.now() - 90 * 86400_000).toISOString().slice(0, 10);
      const [t, h, p, s] = await Promise.all([
        queries.getAllocationToday(todayIso()),
        queries.getAllocationHistory(since),
        queries.getAllocationMatchablePositions(since),
        queries.getStorageUsage(),
      ]);
      if (t.error) throw t.error;
      setToday(t.data ?? []);
      setHist(h.data ?? []);
      setPositions(p);
      setStorage(s.data ?? []);
      setLastLoad(new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'could not read the allocator record');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // ── Shadow comparison: reduced to one number ──────────────────────────────
  const shadowStats = useMemo(() => {
    // Ground truth: did a position with this (symbol, product, entry_date)
    // actually open? Same definition as tools/allocator_report.py.
    const opened = new Set(positions.map((p) => `${p.symbol}|${(p.product ?? 'CNC').toUpperCase()}|${p.entry_date}`));
    let disagreements = 0;
    let scored = 0;
    const bySource: Record<string, number> = {};
    for (const r of hist) {
      const key = `${r.symbol}|${r.product.toUpperCase()}|${r.trade_date}`;
      const greedyTook = opened.has(key);
      const allocTook = r.verdict === 'TAKE';
      if (allocTook === greedyTook) continue; // agreement — zero information
      disagreements += 1;
      if (r.outcome_r !== null) scored += 1;
      const f = r.framework ?? '?';
      bySource[f] = (bySource[f] ?? 0) + 1;
    }
    return { disagreements, scored, bySource, total: hist.length };
  }, [hist, positions]);

  // ── Today's ledger, sorted by edge ────────────────────────────────────────
  const ledger = useMemo(
    () => [...today].sort((a, b) => (b.edge ?? -Infinity) - (a.edge ?? -Infinity)),
    [today],
  );

  // ── Live hurdle per book, from whatever today's rows actually used ───────
  const hurdles = useMemo(() => {
    const byFw: Record<string, number> = {};
    for (const r of today) {
      if (r.hurdle !== null && !(r.framework in byFw)) byFw[r.framework] = r.hurdle;
    }
    return byFw;
  }, [today]);

  // ── Storage gauge ─────────────────────────────────────────────────────────
  // Summed across EVERY table the query returned (see the 200-row limit note
  // in lib/supabase.ts) — a "top N" sum would silently under-report the true
  // total the moment the schema grows past N.
  const storageTotal = useMemo(
    () => storage.reduce((s, r) => s + (r.total_bytes ?? 0), 0),
    [storage],
  );
  const storagePct = useMemo(
    () => storage.reduce((sum, r) => sum + (r.pct_of_free_tier ?? 0), 0),
    [storage],
  );

  const rs = (n: number | null) => n === null ? '—' : `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
  const pct = (n: number | null, d = 3) => n === null ? '—' : n.toFixed(d);

  return (
    <div className="space-y-4">
      {err && (
        <div className="text-xs text-loss flex items-center gap-1.5 px-1">
          <AlertTriangle className="h-3.5 w-3.5" />{err}
        </div>
      )}

      {/* ── KPI strip: shadow comparison reduced to one number, plus context ── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KPICard
          title="Disagreements (90d)"
          value={loading ? '—' : String(shadowStats.disagreements)}
          icon={<Scale className="h-4 w-4" />}
          description={`of ${PROMOTION_THRESHOLD} needed for promotion — agreement carries no information`}
        />
        <KPICard
          title="Scored"
          value={loading ? '—' : `${shadowStats.scored} / ${shadowStats.disagreements}`}
          icon={shadowStats.scored >= PROMOTION_THRESHOLD
            ? <TrendingUp className="h-4 w-4 text-profit" />
            : <Minus className="h-4 w-4" />}
          description="disagreements with a resolved outcome_r"
        />
        <KPICard
          title="Today's verdicts"
          value={loading ? '—' : String(today.length)}
          icon={<ListChecks className="h-4 w-4" />}
          description={`${today.filter((r) => r.verdict === 'TAKE').length} TAKE, last read ${lastLoad || '—'}`}
        />
        <KPICard
          title="Storage"
          value={loading ? '—' : `${storagePct.toFixed(1)}%`}
          icon={<HardDrive className={`h-4 w-4 ${storagePct >= 80 ? 'text-loss' : ''}`} />}
          description={storagePct >= 80 ? 'FAILS at 80% — see tools.health' : 'of the 500 MB free-tier ceiling'}
          className={storagePct >= 80 ? 'border-loss/50' : ''}
        />
      </div>

      {/* ── View 1 + 2: today's ledger, verdict against the live hurdle ──── */}
      <Panel
        title="Today's allocation ledger"
        description="Every proposal scored today, ordered by edge, with the hurdle it was measured against."
        dataSource="supabase" tableName="allocation_decisions"
        isLoading={loading} onRefresh={load}
      >
        {Object.keys(hurdles).length > 0 && (
          <div className="flex gap-3 px-3 py-2 mb-2 rounded border border-border/40 bg-panel/50 text-xs">
            <span className="text-muted-foreground">Live hurdle:</span>
            {Object.entries(hurdles).map(([fw, h]) => (
              <span key={fw} className="font-mono">{fw} <b>{h.toFixed(4)}</b></span>
            ))}
          </div>
        )}
        <DataGuard
          data={ledger} isLoading={loading}
          loadingContent={<SkeletonTable rows={6} cols={7} />}
          emptyDescription="No proposals scored yet today — the allocator writes on the daemon's 300s flush."
        >
          {(rows) => (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-muted-foreground text-left border-b border-border/40">
                    <th className="py-1.5 pr-3">Symbol</th>
                    <th className="py-1.5 pr-3">Book</th>
                    <th className="py-1.5 pr-3">Source</th>
                    <th className="py-1.5 pr-3">Verdict</th>
                    <th className="py-1.5 pr-3 text-right">Edge</th>
                    <th className="py-1.5 pr-3 text-right">Hurdle</th>
                    <th className="py-1.5 pr-3 text-right">Prior n</th>
                    <th className="py-1.5">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-b border-border/20 last:border-0">
                      <td className="py-1.5 pr-3 font-medium">{r.symbol}</td>
                      <td className="py-1.5 pr-3 text-muted-foreground">{r.framework}/{r.product}</td>
                      <td className="py-1.5 pr-3 text-muted-foreground">{r.source ?? '—'}</td>
                      <td className="py-1.5 pr-3">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${VERDICT_TONE[r.verdict] ?? ''}`}>
                          {r.verdict}
                        </span>
                        {r.shadow && <span className="ml-1 text-[9px] text-muted-foreground">shadow</span>}
                      </td>
                      <td className={`py-1.5 pr-3 text-right font-mono tabular-nums ${
                        (r.edge ?? 0) > 0 ? 'text-profit' : (r.edge ?? 0) < 0 ? 'text-loss' : ''}`}>
                        {pct(r.edge, 4)}
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-muted-foreground">
                        {pct(r.hurdle, 4)}
                      </td>
                      <td className="py-1.5 pr-3 text-right font-mono tabular-nums text-muted-foreground">
                        {r.prior_n ?? '—'}{r.prior_below_floor && <span className="text-amber-500"> ⚠</span>}
                      </td>
                      <td className="py-1.5 text-muted-foreground max-w-[36ch] truncate" title={r.reason ?? ''}>
                        {r.reason ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DataGuard>
      </Panel>

      {/* ── View 4 detail: disagreements by book, so the KPI number is legible ── */}
      <Panel
        title="Shadow comparison — disagreements by book"
        description="Agreement is zero information. Only these rows discriminate the allocator from greedy."
        dataSource="supabase" tableName="allocation_decisions"
      >
        {loading ? <SkeletonKPI /> : shadowStats.disagreements === 0 ? (
          <div className="text-xs text-muted-foreground py-2">
            No disagreements in the last 90 days. Either nothing has run since the
            allocator went live, or every proposal it saw agreed with what greedy did.
          </div>
        ) : (
          <div className="space-y-2">
            {Object.entries(shadowStats.bySource).map(([fw, n]) => (
              <div key={fw} className="flex items-center justify-between text-xs">
                <span>{fw}</span>
                <span className="font-mono tabular-nums">{n}</span>
              </div>
            ))}
            <div className="pt-2 border-t border-border/20 text-[10px] text-muted-foreground">
              {shadowStats.disagreements < PROMOTION_THRESHOLD
                ? `${PROMOTION_THRESHOLD - shadowStats.disagreements} more disagreement(s) needed before promotion is even eligible — this is denominated in information, not elapsed time.`
                : `Disagreement floor met. Counterfactual R is haircut by alloc_shortfall_r (an assumption, not a measurement — M1 in the readiness review) — read outcome_note per row via tools.allocator_report before promoting on count alone.`}
            </div>
          </div>
        )}
      </Panel>

      {/* ── View 3: storage gauge ─────────────────────────────────────────── */}
      <Panel
        title="Storage headroom"
        description="tools.health FAILS at 80%, not warns — writes stop at 100%, and the evening pipeline goes silent."
        dataSource="supabase" tableName="v_storage_usage"
        isLoading={loading} onRefresh={load}
      >
        <div className="mb-3">
          <div className="h-3 rounded-full bg-muted overflow-hidden">
            <div
              className={`h-full transition-all ${storagePct >= 80 ? 'bg-loss' : storagePct >= 60 ? 'bg-amber-500' : 'bg-profit'}`}
              style={{ width: `${Math.min(storagePct, 100)}%` }}
            />
          </div>
          <div className="flex justify-between text-[10px] text-muted-foreground mt-1">
            <span>{(storageTotal / 1048576).toFixed(0)} MB of 500 MB</span>
            <span>{storagePct.toFixed(1)}%{storagePct >= 80 && ' — FAILING'}</span>
          </div>
        </div>
        <DataGuard
          data={storage} isLoading={loading}
          loadingContent={<SkeletonTable rows={5} cols={3} />}
          emptyDescription="No usage rows — v_storage_usage may not exist yet (migration 016)."
        >
          {(rows) => (
            <table className="w-full text-xs">
              <tbody>
                {rows.slice(0, 8).map((r) => (
                  <tr key={r.table_name} className="border-b border-border/20 last:border-0">
                    <td className="py-1 pr-3 font-mono">{r.table_name}</td>
                    <td className="py-1 pr-3 text-right text-muted-foreground">
                      {r.approx_rows.toLocaleString('en-IN')} rows
                    </td>
                    <td className="py-1 text-right font-mono tabular-nums">{r.total_size}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </DataGuard>
        <div className="text-[10px] text-muted-foreground mt-2">
          Growth and the projected breach date are computed from rows added in the
          last 30 days, not estimated — see <code className="font-mono">tools.health</code>{' '}
          for the full projection with coverage percentage.
        </div>
      </Panel>
    </div>
  );
}

export default AllocatorTab;
