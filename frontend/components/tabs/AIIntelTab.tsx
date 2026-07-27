'use client';

import { useState, useEffect, useMemo } from 'react';
import { Brain, BookOpen, Search, Filter, TrendingUp } from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonChart, SkeletonTable } from '@/components/core/DataGuard';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { formatDate, formatPercent } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import { TradePlanTable } from '@/components/signals/TradePlanTable';
import type { Signal, AIModelPerformance, Lesson } from '@/types/database';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, PieChart, Pie, Legend,
} from 'recharts';

// ─── Conviction badge ─────────────────────────────────────────────────────
function ConvictionBadge({ level }: { level?: string }) {
  const map: Record<string, string> = {
    HIGH: 'bg-green-500/20 text-green-400 border border-green-500/30',
    MEDIUM: 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30',
    LOW: 'bg-red-500/20 text-red-400 border border-red-500/30',
  };
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${map[level ?? ''] ?? 'bg-muted text-muted-foreground'}`}>
      {level ?? '—'}
    </span>
  );
}

// ─── Conviction distribution donut ────────────────────────────────────────
function ConvictionDonut({ signals }: { signals: Signal[] }) {
  const counted = signals.reduce<Record<string, number>>((acc, s) => {
    const k = s.ai_conviction ?? 'NONE';
    acc[k] = (acc[k] ?? 0) + 1;
    return acc;
  }, {});

  const data = [
    { name: 'HIGH', value: counted.HIGH ?? 0, fill: '#22c55e' },
    { name: 'MEDIUM', value: counted.MEDIUM ?? 0, fill: '#eab308' },
    { name: 'LOW', value: counted.LOW ?? 0, fill: '#ef4444' },
    { name: 'NONE', value: counted.NONE ?? 0, fill: '#6b7280' },
  ].filter((d) => d.value > 0);

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie data={data} cx="50%" cy="50%" innerRadius={50} outerRadius={80}
            dataKey="value" paddingAngle={3}>
            {data.map((entry, i) => <Cell key={i} fill={entry.fill} fillOpacity={0.85} />)}
          </Pie>
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
            formatter={(v: number, name: string) => [v, name + ' conviction']}
          />
          <Legend iconType="circle" iconSize={8} formatter={(v) => <span className="text-xs text-muted-foreground">{v}</span>} />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Strategy signal count bar chart ─────────────────────────────────────
function StrategySignalChart({ signals }: { signals: Signal[] }) {
  const byStrategy = signals.reduce<Record<string, { buy: number; watch: number; sell: number }>>((acc, s) => {
    const strat = s.strategy ?? 'UNKNOWN';
    if (!acc[strat]) acc[strat] = { buy: 0, watch: 0, sell: 0 };
    if (s.signal_type === 'BUY') acc[strat].buy++;
    else if (s.signal_type === 'SELL') acc[strat].sell++;
    else acc[strat].watch++;
    return acc;
  }, {});

  const data = Object.entries(byStrategy)
    .map(([name, v]) => ({ name: name.replace('+', '+'), ...v, total: v.buy + v.watch + v.sell }))
    .sort((a, b) => b.total - a.total)
    .slice(0, 8);

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.4} vertical={false} />
          <XAxis dataKey="name" fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)" />
          <YAxis fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)" />
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
          />
          <Bar dataKey="buy" stackId="a" fill="#22c55e" fillOpacity={0.8} name="BUY" radius={[0, 0, 0, 0]} />
          <Bar dataKey="watch" stackId="a" fill="#6b7280" fillOpacity={0.8} name="WATCH" />
          <Bar dataKey="sell" stackId="a" fill="#ef4444" fillOpacity={0.8} name="SELL" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Filter reason breakdown ──────────────────────────────────────────────
function FilterReasonChart({ signals }: { signals: Signal[] }) {
  const filtered = signals.filter((s) => s.filter_reason);
  const counts = filtered.reduce<Record<string, number>>((acc, s) => {
    const k = s.filter_reason!;
    acc[k] = (acc[k] ?? 0) + 1;
    return acc;
  }, {});

  const data = Object.entries(counts)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 6)
    .map(([name, value]) => ({ name: name.replace(/_/g, ' '), value }));

  if (data.length === 0) return (
    <div className="h-36 flex items-center justify-center text-sm text-muted-foreground">
      No filter reasons recorded
    </div>
  );

  return (
    <div className="h-36">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 0, right: 40, left: 0, bottom: 0 }}>
          <XAxis type="number" fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)" />
          <YAxis type="category" dataKey="name" fontSize={9} tickLine={false} axisLine={false}
            stroke="var(--text-muted)" width={110} />
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
          />
          <Bar dataKey="value" fill="var(--chart-3)" fillOpacity={0.7} radius={[0, 3, 3, 0]} name="Signals" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Signal log table ─────────────────────────────────────────────────────
function SignalTable({ signals }: { signals: Signal[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border">
            {['Date', 'Symbol / Sector', 'Strategy', 'Type', 'Subtype', 'Score', 'Conviction', 'Regime', 'Filter Reason'].map((h) => (
              <th key={h} className={`py-2.5 px-3 font-medium text-muted-foreground text-xs ${h === 'Date' || h === 'Symbol / Sector' ? 'text-left' : 'text-right'}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => (
            <tr key={s.id} className="border-b border-border/40 hover:bg-panel-hover transition-colors">
              <td className="py-2.5 px-3 text-xs text-muted-foreground whitespace-nowrap">{formatDate(s.date, 'dd MMM yy')}</td>
              <td className="py-2.5 px-3">
                <div className="font-semibold text-sm">{s.symbol}</div>
                <div className="text-xs text-muted-foreground">{s.sector}</div>
              </td>
              <td className="text-right py-2.5 px-3 text-xs text-muted-foreground">{s.strategy}</td>
              <td className="text-right py-2.5 px-3">
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${s.signal_type === 'BUY' ? 'bg-green-500/20 text-green-400' : s.signal_type === 'SELL' ? 'bg-red-500/20 text-red-400' : 'bg-muted text-muted-foreground'}`}>
                  {s.signal_type}
                </span>
              </td>
              <td className="text-right py-2.5 px-3 text-xs text-muted-foreground">{s.signal_subtype ?? '—'}</td>
              <td className="text-right py-2.5 px-3 font-mono text-xs">{s.score?.toFixed(1) ?? '—'}</td>
              <td className="text-right py-2.5 px-3"><ConvictionBadge level={s.ai_conviction} /></td>
              <td className="text-right py-2.5 px-3 text-xs text-muted-foreground">{s.regime ?? '—'}</td>
              <td className="text-right py-2.5 px-3 text-xs text-muted-foreground max-w-[140px] truncate" title={s.filter_reason ?? ''}>
                {s.filter_reason ?? '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Main Tab ─────────────────────────────────────────────────────────────
export function AIIntelTab() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [aiPerf, setAiPerf] = useState<AIModelPerformance[]>([]);
  const [lessons, setLessons] = useState<Lesson[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [convFilter, setConvFilter] = useState<string | null>(null);
  const [plans, setPlans] = useState<Signal[]>([]);
  const [planDate, setPlanDate] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        const [s, a, l, p] = await Promise.all([
          queries.getSignals(undefined, 200),
          queries.getAIModelPerformance(),
          queries.getLessons(),
          queries.getTradePlans(60),
        ]);
        if (s.error) throw s.error;
        setSignals(s.data ?? []);
        setAiPerf(a.data ?? []);
        setLessons(l.data ?? []);
        setPlans(p.data ?? []);
        setPlanDate(p.date);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const filtered = useMemo(() => signals
    .filter((s) => !convFilter || s.ai_conviction === convFilter)
    .filter((s) => !search || s.symbol.toLowerCase().includes(search.toLowerCase()) ||
      (s.sector ?? '').toLowerCase().includes(search.toLowerCase()) ||
      s.signal_type.toLowerCase().includes(search.toLowerCase())),
    [signals, convFilter, search]);

  const highConv = signals.filter((s) => s.ai_conviction === 'HIGH').length;
  const medConv = signals.filter((s) => s.ai_conviction === 'MEDIUM').length;
  const errObj = error ? new Error(error) : null;

  return (
    <div className="space-y-4">
      {/* Trade plans first — this is the only panel on the tab that answers
          "what do I do today". Conviction distributions and provider stats
          describe the run; they do not tell you whether anything is buyable. */}
      <Panel title="Today's Trade Plans"
        description="Executable levels from signal_output_daily — the same rows the Telegram alert reads"
        dataSource="supabase" tableName="signal_output_daily" isLoading={loading}>
        <DataGuard data={plans} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={5} cols={9} />}
          emptyTitle="No trade plans yet"
          emptyDescription="Plans appear after the pipeline runs final_snapshot.">
          {(data) => <TradePlanTable rows={data} date={planDate} />}
        </DataGuard>
      </Panel>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? (
          <><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /></>
        ) : (
          <>
            <KPICard title="Total Signals" value={signals.length.toString()}
              description="Last 200 from signal_log" icon={<Brain className="h-4 w-4" />} />
            <KPICard title="High Conviction" value={highConv.toString()}
              description="AI rated HIGH" icon={<TrendingUp className="h-4 w-4" />}
              change={highConv > 0 ? { value: `${((highConv / signals.length) * 100).toFixed(0)}% of total`, type: 'increase' } : undefined} />
            <KPICard title="Medium Conviction" value={medConv.toString()}
              description="AI rated MEDIUM" icon={<TrendingUp className="h-4 w-4" />} />
            <KPICard title="System Lessons" value={lessons.length.toString()}
              description="Active lessons loaded" icon={<BookOpen className="h-4 w-4" />} />
          </>
        )}
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Panel title="Conviction Distribution" description="AI conviction breakdown"
          dataSource="supabase" tableName="signal_log" isLoading={loading}>
          <DataGuard data={signals} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle="No signals" emptyDescription="Run generate_signals.py">
            {(data) => <ConvictionDonut signals={data} />}
          </DataGuard>
        </Panel>

        <Panel title="Signals by Strategy" description="BUY / WATCH / SELL count per strategy"
          dataSource="supabase" tableName="signal_log" isLoading={loading}>
          <DataGuard data={signals} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle="No signals" emptyDescription="Run generate_signals.py">
            {(data) => <StrategySignalChart signals={data} />}
          </DataGuard>
        </Panel>

        <Panel title="Top Filter Reasons" description="Why signals were filtered out"
          dataSource="supabase" tableName="signal_log" isLoading={loading}>
          <DataGuard data={signals.filter((s) => s.filter_reason)} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-36" />}
            emptyTitle="No filter reasons" emptyDescription="filter_reason column empty">
            {() => <FilterReasonChart signals={signals} />}
          </DataGuard>
        </Panel>
      </div>

      {/* AI Provider performance summary */}
      {aiPerf.length > 0 && (
        <Panel title="AI Provider Stats" description="Latest per provider from ai_model_performance"
          dataSource="supabase" tableName="ai_model_performance" isLoading={loading}>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.values(
              aiPerf.reduce<Record<string, AIModelPerformance>>((acc, r) => {
                if (!acc[r.provider] || r.date > acc[r.provider].date) acc[r.provider] = r;
                return acc;
              }, {})
            ).map((r) => (
              <div key={r.id} className="p-3 rounded-lg border border-border/50 bg-panel-hover">
                <div className="font-semibold capitalize">{r.provider}</div>
                <div className="text-xs text-muted-foreground font-mono mt-0.5">{r.model}</div>
                <div className="mt-2 space-y-1 text-xs">
                  <div className="flex justify-between"><span className="text-muted-foreground">Calls today</span><span>{r.calls_today ?? 0}</span></div>
                  {r.accuracy != null && <div className="flex justify-between"><span className="text-muted-foreground">Accuracy</span><span className="text-profit">{(r.accuracy * 100).toFixed(0)}%</span></div>}
                  {r.cost_today != null && <div className="flex justify-between"><span className="text-muted-foreground">Cost</span><span>${r.cost_today.toFixed(3)}</span></div>}
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* Signal log table with filters */}
      <Panel title={`Signal Log (${filtered.length} shown)`}
        description="Live from signal_log — filter by conviction or search symbol"
        dataSource="supabase" tableName="signal_log" isLoading={loading}
        toolbar={
          <div className="flex items-center gap-2">
            {['HIGH', 'MEDIUM', 'LOW'].map((c) => (
              <Button key={c} size="sm" variant={convFilter === c ? 'default' : 'outline'}
                className="h-6 text-xs" onClick={() => setConvFilter(convFilter === c ? null : c)}>
                {c}
              </Button>
            ))}
            <div className="relative w-36">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
              <Input value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder="Symbol..." className="pl-6 h-6 text-xs" />
            </div>
          </div>
        }>
        <DataGuard data={filtered} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={8} cols={9} />}
          emptyTitle="No signals match" emptyDescription="Try clearing filters">
          {(data) => <SignalTable signals={data} />}
        </DataGuard>
      </Panel>

      {/* Lessons */}
      {lessons.length > 0 && (
        <Panel title="System Lessons" description={`${lessons.length} active lessons from post-trade analysis`}
          dataSource="supabase" tableName="lessons" isLoading={loading}>
          <div className="space-y-2">
            {lessons.map((l) => (
              <div key={l.id} className="p-3 rounded-lg border border-border/50 bg-panel-hover">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap mb-1">
                      <span className="text-xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{l.scenario_type ?? 'GENERAL'}</span>
                      {l.confidence != null && <span className="text-xs text-muted-foreground">{(l.confidence * 100).toFixed(0)}% confidence</span>}
                      {l.times_applied != null && <span className="text-xs text-muted-foreground">Applied {l.times_applied}×</span>}
                    </div>
                    {l.trigger_event && <div className="font-medium text-sm">{l.trigger_event}</div>}
                    {l.corrective_rule && <div className="text-xs text-muted-foreground mt-1 italic">{l.corrective_rule}</div>}
                  </div>
                  <div className="text-xs text-muted-foreground shrink-0">{formatDate(l.date, 'dd MMM yy')}</div>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

export default AIIntelTab;
