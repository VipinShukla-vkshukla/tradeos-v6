'use client';

import { useState, useEffect } from 'react';
import { TrendingUp, TrendingDown, Target, BarChart3, Trophy, Activity } from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonChart, SkeletonTable } from '@/components/core/DataGuard';
import { WinRateTrendChart } from '@/components/tabs/performance/WinRateTrendChart';
import { SignalTypeBreakdown } from '@/components/tabs/performance/SignalTypeBreakdown';
import { EngineLeaderboard } from '@/components/tabs/performance/EngineLeaderboard';
import { formatCurrency, formatPercent } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import type { PerformanceMetricsRow, ClosedPosition, EngineStatsEntry } from '@/types/database';
import type { WeeklyPerformance, EngineStats } from '@/types/database';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
} from 'recharts';

// ─── Map PerformanceMetricsRow → WeeklyPerformance (what charts expect) ───
function toWeeklyPerformance(rows: PerformanceMetricsRow[]): WeeklyPerformance[] {
  return [...rows]
    .sort((a, b) => a.metric_date.localeCompare(b.metric_date))
    .map((r) => ({
      week_start: r.metric_date,
      win_rate: (r.win_rate_overall ?? 0) / 100,
      total_trades: r.signals_generated ?? 0,
      pnl: 0,
      pnl_percent: r.avg_fwd_ret_5d ?? 0,
    }));
}

// ─── Parse engine_stats JSONB → EngineStats[] (what charts expect) ────────
function toEngineStats(rows: PerformanceMetricsRow[]): EngineStats[] {
  for (const row of rows) {
    let parsed: EngineStatsEntry[] | null = null;
    if (Array.isArray(row.engine_stats)) parsed = row.engine_stats;
    else if (typeof row.engine_stats === 'string') {
      try { parsed = JSON.parse(row.engine_stats); } catch { continue; }
    }
    if (parsed && parsed.length > 0) {
      return parsed.map((e) => ({
        engine_name: e.engine_name,
        total_signals: e.total_signals,
        executed_signals: e.total_signals,
        win_count: e.win_count,
        loss_count: e.loss_count,
        win_rate: e.win_rate / 100,
        avg_pnl_percent: e.avg_pnl_pct ?? 0,
        total_pnl: e.total_pnl ?? 0,
        last_signal_date: row.metric_date,
      }));
    }
  }
  return [];
}

// ─── Monthly P&L breakdown chart (from closed_positions) ─────────────────
function MonthlyPnLChart({ data }: { data: ClosedPosition[] }) {
  const monthly: Record<string, number> = {};
  for (const p of data) {
    if (!p.exit_date) continue;
    const month = p.exit_date.slice(0, 7); // YYYY-MM
    monthly[month] = (monthly[month] ?? 0) + (p.realized_pnl ?? 0);
  }
  const chartData = Object.entries(monthly)
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-12)
    .map(([month, pnl]) => ({
      month: new Date(month + '-01').toLocaleDateString('en-IN', { month: 'short', year: '2-digit' }),
      pnl,
    }));

  if (chartData.length === 0) return (
    <div className="h-48 flex items-center justify-center text-sm text-muted-foreground">
      No monthly P&L data yet
    </div>
  );

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.4} vertical={false} />
          <XAxis dataKey="month" fontSize={11} tickLine={false} axisLine={false} stroke="var(--text-muted)" />
          <YAxis fontSize={11} tickLine={false} axisLine={false} stroke="var(--text-muted)"
            tickFormatter={(v) => `₹${Math.abs(v) >= 1000 ? (v / 1000).toFixed(0) + 'K' : v}`} />
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
            formatter={(v: number) => [formatCurrency(v, { showSign: true }), 'Realized P&L']}
          />
          <Bar dataKey="pnl" radius={[3, 3, 0, 0]} maxBarSize={32}>
            {chartData.map((entry, i) => (
              <Cell key={i} fill={entry.pnl >= 0 ? 'var(--profit-green)' : 'var(--loss-red)'} fillOpacity={0.8} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Strategy breakdown (from closed_positions) ───────────────────────────
function StrategyBreakdown({ data }: { data: ClosedPosition[] }) {
  const byStrategy: Record<string, { wins: number; losses: number; pnl: number }> = {};
  for (const p of data) {
    const s = p.strategy ?? 'UNKNOWN';
    if (!byStrategy[s]) byStrategy[s] = { wins: 0, losses: 0, pnl: 0 };
    if ((p.realized_pnl ?? 0) > 0) byStrategy[s].wins++;
    else byStrategy[s].losses++;
    byStrategy[s].pnl += p.realized_pnl ?? 0;
  }

  const rows = Object.entries(byStrategy)
    .map(([strategy, v]) => ({ strategy, ...v, wr: v.wins + v.losses > 0 ? (v.wins / (v.wins + v.losses)) * 100 : 0 }))
    .sort((a, b) => b.pnl - a.pnl);

  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.strategy} className="flex items-center gap-3 p-2.5 rounded-lg bg-panel-hover">
          <div className="w-24 shrink-0">
            <div className="font-medium text-sm">{r.strategy}</div>
            <div className="text-xs text-muted-foreground">{r.wins + r.losses} trades</div>
          </div>
          <div className="flex-1">
            <div className="flex justify-between text-xs mb-1">
              <span className="text-muted-foreground">Win Rate</span>
              <span className={r.wr >= 50 ? 'text-profit' : 'text-loss'}>{r.wr.toFixed(0)}%</span>
            </div>
            <div className="h-1.5 bg-border rounded-full overflow-hidden">
              <div className={`h-full rounded-full ${r.wr >= 50 ? 'bg-profit' : 'bg-loss'}`} style={{ width: `${r.wr}%` }} />
            </div>
          </div>
          <div className={`text-right shrink-0 text-sm font-mono font-medium ${r.pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
            {formatCurrency(r.pnl, { compact: true, showSign: true })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Trading era ──────────────────────────────────────────────────────────
//
// WHY THIS EXISTS
//   The KPI row used to compute over every row in closed_positions and label
//   the result "Win Rate" and "Current Streak" — implying it described the
//   system. It did not. Of the first 70 closed trades, 69 were entered before
//   signal_log held a single row, so they measure manual trading from an era
//   that predates the signal engine entirely. Reading 38.6% / -₹9.8K as this
//   system's record would be wrong in both directions: unfair to the engine,
//   and a false comfort if it ever looked good.
//
//   The honest split is attribution, not a date: a trade belongs to the system
//   only if it can be traced to the signal that produced it. That is exactly
//   what signal_date records, and it is what post_trade_analysis and the Brain
//   learn from.
type Era = 'system' | 'legacy' | 'all';

// Below this many trades, a win rate is noise. Showing "0%" off one sample is
// as misleading as showing the legacy number — so we show the count instead.
const MIN_MEANINGFUL_SAMPLE = 20;

const ERA_META: Record<Era, { label: string; blurb: string }> = {
  system: { label: 'Since automation', blurb: 'Trades traceable to a signal the engine produced.' },
  legacy: { label: 'Legacy / manual',  blurb: 'Entered before the signal engine existed. Kept for reference.' },
  all:    { label: 'All time',         blurb: 'Both eras combined — mixes two different systems.' },
};

function inEra(p: ClosedPosition, era: Era): boolean {
  if (era === 'all') return true;
  const attributed = p.signal_date != null || p.signal_id != null;
  return era === 'system' ? attributed : !attributed;
}

// ─── Which book ────────────────────────────────────────────────────────────
//
// WHY THIS EXISTS
//   Every stat below read straight from closed_positions with no mode filter.
//   That was harmless while paper mode did not exist, and became wrong the
//   moment it did: a simulated win would raise the win rate on the same screen
//   that reports real money, and simulated P&L would be added to real P&L.
//
//   Paper exists precisely so its results can be COMPARED to live, which
//   requires them to be separable. Blending them destroys both numbers — the
//   live record stops being a record of what happened, and the paper record
//   stops being evidence about what would happen.
//
//   Default is LIVE, because the unqualified question "how am I doing" is
//   always about real money.
type Book = 'live' | 'paper' | 'all';

const BOOK_META: Record<Book, { label: string; blurb: string }> = {
  live:  { label: 'Real money',  blurb: 'Trades that actually filled at the broker.' },
  paper: { label: 'Paper',       blurb: 'Simulated fills. Same decisions, no money at risk.' },
  all:   { label: 'Both',        blurb: 'Mixes real and simulated — useful for volume, not for P&L.' },
};

function inBook(p: ClosedPosition, book: Book): boolean {
  if (book === 'all') return true;
  const isPaper = (p.mode ?? 'LIVE').toUpperCase() === 'PAPER';
  return book === 'paper' ? isPaper : !isPaper;
}

function BookToggle({ book, setBook, counts }: {
  book: Book; setBook: (b: Book) => void; counts: Record<Book, number>;
}) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-[11px] text-muted-foreground">Book:</span>
      {(['live', 'paper', 'all'] as Book[]).map((b) => (
        <button
          key={b}
          onClick={() => setBook(b)}
          title={BOOK_META[b].blurb}
          className={`text-[11px] px-2 py-1 rounded border transition-colors ${
            book === b
              ? (b === 'paper'
                  ? 'bg-blue-500/20 border-blue-500/40 text-blue-400'
                  : 'bg-emerald-500/20 border-emerald-500/40 text-emerald-400')
              : 'border-border/50 text-muted-foreground hover:border-border'
          }`}
        >
          {BOOK_META[b].label} ({counts[b]})
        </button>
      ))}
      {book === 'paper' && (
        <span className="text-[10px] text-blue-400">
          simulated — no money changed hands
        </span>
      )}
      {book === 'all' && (
        <span className="text-[10px] text-amber-400">
          real and simulated combined — P&amp;L here is not your account
        </span>
      )}
    </div>
  );
}

function EraToggle({ era, setEra, counts }: {
  era: Era; setEra: (e: Era) => void; counts: Record<Era, number>;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <div className="inline-flex rounded-lg border border-border p-0.5">
        {(['system', 'legacy', 'all'] as Era[]).map((e) => (
          <button
            key={e}
            onClick={() => setEra(e)}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
              era === e ? 'bg-accent text-accent-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {ERA_META[e].label}
            <span className="ml-1.5 font-mono opacity-60">{counts[e]}</span>
          </button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">{ERA_META[era].blurb}</p>
    </div>
  );
}

/** Shown instead of KPIs when the selected era has too few trades to mean anything. */
function BuildingRecord({ n }: { n: number }) {
  return (
    <div className="rounded-lg border border-warning/30 bg-warning/5 p-5">
      <h3 className="text-sm font-medium text-foreground">
        {n === 0 ? 'No system trades closed yet' : `${n} system trade${n === 1 ? '' : 's'} closed so far`}
      </h3>
      <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-muted-foreground">
        A win rate needs roughly {MIN_MEANINGFUL_SAMPLE} closed trades before it says
        anything about edge rather than luck. Showing one now would be noise dressed
        as a metric, so the numbers stay hidden until the sample supports them.
      </p>
      <p className="mt-2 max-w-2xl text-xs leading-relaxed text-muted-foreground">
        Meanwhile <strong className="text-foreground">screener_performance</strong> is
        accumulating forward returns on every shortlisted stock — a read on signal
        quality that costs nothing and needs no trades. The Control Room shows it
        per engine.
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        Switch to <em>Legacy</em> to see the pre-automation record.
      </p>
    </div>
  );
}

// ─── Main Tab ─────────────────────────────────────────────────────────────
export function PerformanceTab() {
  const [metrics, setMetrics] = useState<PerformanceMetricsRow[]>([]);
  const [closedAll, setClosedAll] = useState<ClosedPosition[]>([]);
  const [era, setEra] = useState<Era>('system');
  const [book, setBook] = useState<Book>('live');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        const [m, c] = await Promise.all([
          queries.getPerformanceMetrics('weekly', 26),
          queries.getClosedPositions(100),
        ]);
        if (m.error) throw m.error;
        if (c.error) throw c.error;
        setMetrics(m.data ?? []);
        setClosedAll(c.data ?? []);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  // Every KPI and chart below is scoped to the selected era. Mixing a
  // pre-automation record into a metric labelled "Win Rate" is what this whole
  // section exists to prevent.
  // Book first, then era. Both filters are explicit and both are shown,
  // because a stat whose population you cannot see is a stat you cannot
  // check.
  const inScope = closedAll.filter((p) => inBook(p, book));
  const closed = inScope.filter((p) => inEra(p, era));
  const eraCounts: Record<Era, number> = {
    system: inScope.filter((p) => inEra(p, 'system')).length,
    legacy: inScope.filter((p) => inEra(p, 'legacy')).length,
    all:    inScope.length,
  };
  const bookCounts: Record<Book, number> = {
    live:  closedAll.filter((p) => inBook(p, 'live')).length,
    paper: closedAll.filter((p) => inBook(p, 'paper')).length,
    all:   closedAll.length,
  };
  const tooFewToJudge = era === 'system' && closed.length < MIN_MEANINGFUL_SAMPLE;

  // Derived stats from closed_positions (source of truth for real trades)
  const wins = closed.filter((p) => (p.realized_pnl ?? 0) > 0);
  const losses = closed.filter((p) => (p.realized_pnl ?? 0) <= 0);
  const winRate = closed.length > 0 ? (wins.length / closed.length) * 100 : 0;
  const totalPnl = closed.reduce((s, p) => s + (p.realized_pnl ?? 0), 0);
  // Net of what the broker actually took. Charges are NULL on trades closed
  // before they were recorded, so `costed` says how much of this sample the
  // net figure can speak for — a net P&L quietly computed over a third of the
  // rows would be worse than not showing one.
  const costed = closed.filter((p) => p.charges != null);
  const totalCharges = costed.reduce((s, p) => s + (p.charges ?? 0), 0);
  const netPnl = totalPnl - totalCharges;
  const avgWin = wins.length > 0 ? wins.reduce((s, p) => s + (p.realized_pnl ?? 0), 0) / wins.length : 0;
  const avgLoss = losses.length > 0 ? Math.abs(losses.reduce((s, p) => s + (p.realized_pnl ?? 0), 0) / losses.length) : 1;
  const profitFactor = avgLoss > 0 ? (avgWin * wins.length) / (avgLoss * Math.max(losses.length, 1)) : 0;

  // Streak
  let streak = 0; let streakType: 'WIN' | 'LOSS' = 'WIN';
  const sorted = [...closed].sort((a, b) => (b.exit_date ?? '').localeCompare(a.exit_date ?? ''));
  for (const p of sorted) {
    const won = (p.realized_pnl ?? 0) > 0;
    if (streak === 0) { streakType = won ? 'WIN' : 'LOSS'; streak = 1; }
    else if ((won && streakType === 'WIN') || (!won && streakType === 'LOSS')) streak++;
    else break;
  }

  const weeklyTrend = toWeeklyPerformance(metrics);
  const engineStats = toEngineStats(metrics);
  const errObj = error ? new Error(error) : null;

  return (
    <div className="space-y-4">
      {!loading && (
        <div className="space-y-2">
          <BookToggle book={book} setBook={setBook} counts={bookCounts} />
          <EraToggle era={era} setEra={setEra} counts={eraCounts} />
        </div>
      )}

      {/* KPI Row — replaced by an honest empty state when the sample is too
          small to support a win rate. */}
      {!loading && tooFewToJudge ? (
        <BuildingRecord n={closed.length} />
      ) : (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? (
          <><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /></>
        ) : (
          <>
            <KPICard title="Win Rate" value={`${winRate.toFixed(1)}%`}
              description={`${closed.length} closed trades`} icon={<Target className="h-4 w-4" />}
              change={closed.length > 0 ? { value: `${wins.length}W / ${losses.length}L`, type: winRate >= 50 ? 'increase' : 'decrease' } : undefined} />
            {/* Headline is NET where costs are known. "Net profitable" over a
                gross number was a claim the account could not back — a 0.21%
                round trip is about a fifth of a 1% intraday winner. */}
            <KPICard
              title={costed.length === closed.length && closed.length > 0
                ? 'Realized P&L (net)' : 'Total Realized P&L'}
              value={formatCurrency(costed.length === closed.length && closed.length > 0
                ? netPnl : totalPnl, { compact: true })}
              description={costed.length > 0 && costed.length < closed.length
                ? `gross — charges known for only ${costed.length}/${closed.length}`
                : costed.length > 0 ? `after ${formatCurrency(totalCharges)} in charges`
                : 'gross — charges not recorded for these trades'}
              icon={<Activity className="h-4 w-4" />}
              change={totalPnl !== 0 ? { value: totalPnl > 0 ? 'Net profitable' : 'Net loss', type: totalPnl > 0 ? 'increase' : 'decrease' } : undefined} />
            <KPICard title="Profit Factor"
              value={closed.length > 0 ? profitFactor.toFixed(2) : '—'}
              description="Gross profit ÷ gross loss" icon={<BarChart3 className="h-4 w-4" />}
              change={profitFactor > 0 ? { value: profitFactor >= 1.5 ? 'Healthy' : profitFactor >= 1 ? 'Marginal' : 'Negative edge', type: profitFactor >= 1.5 ? 'increase' : profitFactor >= 1 ? 'neutral' : 'decrease' } : undefined} />
            <KPICard title="Current Streak"
              value={closed.length > 0 ? `${streak} ${streakType === 'WIN' ? 'Wins' : 'Losses'}` : '—'}
              icon={<Trophy className="h-4 w-4" />}
              change={closed.length > 0 ? { value: streakType === 'WIN' ? 'Keep going' : 'Review setups', type: streakType === 'WIN' ? 'increase' : 'decrease' } : undefined} />
          </>
        )}
      </div>
      )}

      {/* Monthly P&L + Win Rate Trend */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="Monthly Realized P&L" description={`12-month realised P&L — ${ERA_META[era].label.toLowerCase()}`}
          dataSource="supabase" tableName="closed_positions" isLoading={loading}>
          <DataGuard data={closed} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle={era === 'system' ? 'No system trades yet' : 'No closed trades'}
            emptyDescription={era === 'system' ? 'Appears once a signal-linked trade closes.' : 'Close a trade to see monthly P&L.'}>
            {(data) => <MonthlyPnLChart data={data} />}
          </DataGuard>
        </Panel>

        <Panel title="Win Rate Trend" description="Weekly rolling — from performance_metrics"
          dataSource="supabase" tableName="performance_metrics" isLoading={loading}>
          <DataGuard data={weeklyTrend} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle="No trend data" emptyDescription="Populated by your pipeline's performance_metrics step">
            {(data) => <WinRateTrendChart data={data} />}
          </DataGuard>
        </Panel>
      </div>

      {/* Strategy Breakdown + Engine Leaderboard */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="Strategy P&L Breakdown" description={`Realised P&L and win rate by strategy — ${ERA_META[era].label.toLowerCase()}`}
          dataSource="supabase" tableName="closed_positions" isLoading={loading}>
          <DataGuard data={closed} isLoading={loading} error={errObj}
            loadingContent={<SkeletonTable rows={4} cols={1} />}
            emptyTitle={era === 'system' ? 'No system trades yet' : 'No closed trades'}
            emptyDescription={era === 'system' ? 'Appears once a signal-linked trade closes.' : 'Appears after your first closed trade.'}>
            {(data) => <StrategyBreakdown data={data} />}
          </DataGuard>
        </Panel>

        <Panel title="Engine Leaderboard" description="From engine_stats in performance_metrics"
          dataSource="supabase" tableName="performance_metrics" isLoading={loading}>
          <DataGuard data={engineStats} isLoading={loading} error={errObj}
            loadingContent={<SkeletonTable rows={5} cols={4} />}
            emptyTitle="No engine stats yet"
            emptyDescription="engine_stats JSONB column needs to be populated by your pipeline's compute_performance step">
            {(data) => <EngineLeaderboard data={data} />}
          </DataGuard>
        </Panel>
      </div>

      {/* Signal type breakdown (only when engine_stats exist) */}
      {engineStats.length > 0 && (
        <Panel title="Signal Type Win Rate vs Avg P&L" description="Side-by-side comparison by engine"
          dataSource="supabase" tableName="performance_metrics" isLoading={loading}>
          <SignalTypeBreakdown data={engineStats} />
        </Panel>
      )}
    </div>
  );
}

export default PerformanceTab;
