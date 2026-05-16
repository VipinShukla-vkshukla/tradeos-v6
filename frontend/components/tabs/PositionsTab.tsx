'use client';

import { useState, useEffect, useMemo } from 'react';
import {
  AlertTriangle, TrendingUp, DollarSign, CheckCircle,
  ArrowUpRight, ArrowDownRight, ChevronDown, ChevronRight,
  Bell, Target, Clock,
} from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonTable } from '@/components/core/DataGuard';
import { formatCurrency, formatDate } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import type { OpenPosition, ClosedPosition } from '@/types/database';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from 'recharts';

// ─── Priority classification ──────────────────────────────────────────────
type AttentionLevel = 'CRITICAL' | 'WARNING' | 'WATCH' | 'OK';

function getAttentionLevel(p: OpenPosition): AttentionLevel {
  if (p.sl_breach_alerted) return 'CRITICAL';
  if (p.exit_signal && p.exit_signal !== 'NONE' && p.exit_signal !== '') return 'CRITICAL';
  if (p.action_required && p.action_required !== 'HOLD' && p.action_required !== '') return 'CRITICAL';
  if (p.sl_proximity_alerted) return 'WARNING';
  if (p.target_hit) return 'WARNING';
  if ((p.pnl_pct ?? 0) < -8) return 'WARNING';
  if ((p.pnl_pct ?? 0) > 15) return 'WATCH';
  return 'OK';
}

function attentionReason(p: OpenPosition): string {
  if (p.sl_breach_alerted) return '🚨 Stop loss breached — exit now';
  if (p.exit_signal && p.exit_signal !== 'NONE') return `Exit signal: ${p.exit_signal}`;
  if (p.action_required && p.action_required !== 'HOLD') return `Action: ${p.action_required}`;
  if (p.sl_proximity_alerted) return `⚠ SL proximity alert — within 2% of stop`;
  if (p.target_hit) return `🎯 Target hit — consider booking profits`;
  if ((p.pnl_pct ?? 0) < -8) return `Down ${Math.abs(p.pnl_pct ?? 0).toFixed(1)}% — review thesis`;
  if ((p.pnl_pct ?? 0) > 15) return `Up ${(p.pnl_pct ?? 0).toFixed(1)}% — consider trailing stop`;
  return `Holding — ${p.lifecycle ?? 'HOLD'}`;
}

const LEVEL_STYLE: Record<AttentionLevel, { border: string; bg: string; dot: string; text: string }> = {
  CRITICAL: { border: 'border-red-500/40',    bg: 'bg-red-500/8',    dot: 'bg-red-500',    text: 'text-red-400' },
  WARNING:  { border: 'border-yellow-500/40', bg: 'bg-yellow-500/8', dot: 'bg-yellow-500', text: 'text-yellow-400' },
  WATCH:    { border: 'border-blue-500/40',   bg: 'bg-blue-500/8',   dot: 'bg-blue-500',   text: 'text-blue-400' },
  OK:       { border: 'border-border/50',     bg: '',                dot: 'bg-green-500',  text: 'text-green-400' },
};

// ─── Position card ────────────────────────────────────────────────────────
function PositionCard({ p }: { p: OpenPosition }) {
  const level = getAttentionLevel(p);
  const s = LEVEL_STYLE[level];
  const pnl = p.unrealized_pnl ?? 0;
  const pct = p.pnl_pct ?? 0;
  const slDist = p.active_sl && p.current_price
    ? ((p.current_price - p.active_sl) / p.current_price * 100) : null;
  const t1Dist = p.target_1 && p.current_price
    ? ((p.target_1 - p.current_price) / p.current_price * 100) : null;

  return (
    <div className={`border rounded-xl p-4 ${s.border} ${s.bg}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 flex-1 min-w-0">
          <div className={`mt-1.5 h-2 w-2 rounded-full shrink-0 ${s.dot}`} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-bold">{p.symbol}</span>
              <span className="text-xs text-muted-foreground">{p.company_name}</span>
              <span className="text-xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{p.strategy}</span>
            </div>
            <div className={`text-xs mt-1 font-medium ${s.text}`}>{attentionReason(p)}</div>
          </div>
        </div>
        <div className="text-right shrink-0">
          <div className={`font-bold text-lg font-mono ${pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
            {pnl >= 0 ? '+' : ''}{formatCurrency(pnl)}
          </div>
          <div className={`text-sm font-mono ${pct >= 0 ? 'text-profit' : 'text-loss'}`}>
            {pct >= 0 ? '+' : ''}{pct.toFixed(1)}%
          </div>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3 mt-3 pt-3 border-t border-border/20 text-xs">
        <div>
          <div className="text-muted-foreground">Entry</div>
          <div className="font-mono mt-0.5">₹{p.entry_price?.toFixed(0) ?? '—'}</div>
        </div>
        <div>
          <div className="text-muted-foreground">CMP</div>
          <div className="font-mono mt-0.5">₹{p.current_price?.toFixed(0) ?? '—'}</div>
        </div>
        <div>
          <div className="text-muted-foreground">Stop Loss</div>
          <div className={`font-mono mt-0.5 ${slDist != null && slDist < 3 ? 'text-loss font-semibold' : ''}`}>
            {p.active_sl ? `₹${p.active_sl.toFixed(0)}` : '—'}
            {slDist != null && <span className="text-muted-foreground ml-1">({slDist.toFixed(1)}%)</span>}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground">Target 1</div>
          <div className={`font-mono mt-0.5 ${t1Dist != null && t1Dist < 3 ? 'text-profit font-semibold' : ''}`}>
            {p.target_1 ? `₹${p.target_1.toFixed(0)}` : '—'}
            {t1Dist != null && t1Dist > 0 && <span className="text-muted-foreground ml-1">({t1Dist.toFixed(1)}%)</span>}
          </div>
        </div>
      </div>

      {p.event_risk && p.event_risk !== 'NONE' && p.event_risk !== '' && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-yellow-400">
          <Bell className="h-3 w-3" />Event risk: {p.event_risk}
        </div>
      )}
    </div>
  );
}

// ─── Equity curve ─────────────────────────────────────────────────────────
function EquityCurve({ data }: { data: ClosedPosition[] }) {
  const sorted = [...data].filter((p) => p.exit_date)
    .sort((a, b) => (a.exit_date ?? '').localeCompare(b.exit_date ?? ''));
  let running = 0;
  const chartData = sorted.map((p) => {
    running += p.realized_pnl ?? 0;
    return { date: formatDate(p.exit_date!, 'dd MMM'), cumPnl: running, symbol: p.symbol };
  });
  const isProfit = running >= 0;
  return (
    <div className="h-44">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={isProfit ? '#22c55e' : '#ef4444'} stopOpacity={0.25} />
              <stop offset="95%" stopColor={isProfit ? '#22c55e' : '#ef4444'} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.3} vertical={false} />
          <XAxis dataKey="date" fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)"
            interval={Math.max(0, Math.floor(chartData.length / 6))} />
          <YAxis fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)"
            tickFormatter={(v) => `₹${Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}K` : v}`} />
          <ReferenceLine y={0} stroke="var(--border-default)" strokeDasharray="3 3" />
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
            formatter={(v: number) => [formatCurrency(v, { showSign: true }), 'Cumulative P&L']}
            labelFormatter={(_, payload) => payload?.[0]?.payload?.symbol ?? ''}
          />
          <Area type="monotone" dataKey="cumPnl" stroke={isProfit ? '#22c55e' : '#ef4444'}
            fill="url(#equityGrad)" strokeWidth={2} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── Closed positions grouped by month ───────────────────────────────────
function ClosedByMonth({ data }: { data: ClosedPosition[] }) {
  const groups: Record<string, { trades: ClosedPosition[]; pnl: number; wins: number }> = {};
  for (const p of [...data].sort((a, b) => (b.exit_date ?? '').localeCompare(a.exit_date ?? ''))) {
    const key = (p.exit_date ?? '').slice(0, 7);
    if (!groups[key]) groups[key] = { trades: [], pnl: 0, wins: 0 };
    groups[key].trades.push(p);
    groups[key].pnl += p.realized_pnl ?? 0;
    if ((p.realized_pnl ?? 0) > 0) groups[key].wins++;
  }
  const keys = Object.keys(groups).sort((a, b) => b.localeCompare(a));
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(keys[0] ? [keys[0]] : []));

  function toggle(k: string) {
    setExpanded((prev) => { const n = new Set(prev); n.has(k) ? n.delete(k) : n.add(k); return n; });
  }

  return (
    <div className="space-y-2">
      {keys.map((key) => {
        const g = groups[key];
        const label = new Date(key + '-01').toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
        const wr = g.trades.length > 0 ? g.wins / g.trades.length * 100 : 0;
        const isOpen = expanded.has(key);
        return (
          <div key={key} className="border border-border/50 rounded-lg overflow-hidden">
            <button className="w-full flex items-center justify-between px-4 py-3 hover:bg-panel-hover transition-colors"
              onClick={() => toggle(key)}>
              <div className="flex items-center gap-3">
                {isOpen ? <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  : <ChevronRight className="h-4 w-4 text-muted-foreground" />}
                <span className="font-medium">{label}</span>
                <span className="text-xs text-muted-foreground">{g.trades.length} trades</span>
                <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${wr >= 50 ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>
                  {wr.toFixed(0)}% WR
                </span>
              </div>
              <span className={`font-mono font-semibold ${g.pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                {formatCurrency(g.pnl, { showSign: true })}
              </span>
            </button>
            {isOpen && (
              <div className="border-t border-border/30 overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border/30 bg-panel-hover/50">
                      {['Symbol', 'Strategy', 'Entry', 'Exit', 'Qty', 'P&L', '%', 'Exit Date', 'Reason'].map((h) => (
                        <th key={h} className={`py-2 px-3 font-medium text-muted-foreground ${h === 'Symbol' ? 'text-left' : 'text-right'}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {g.trades.map((p) => {
                      const pnl = p.realized_pnl ?? 0;
                      const pct = p.pnl_pct ?? 0;
                      return (
                        <tr key={p.id} className="border-b border-border/20 hover:bg-panel-hover">
                          <td className="py-2.5 px-3">
                            <div className="flex items-center gap-1.5">
                              {pnl >= 0 ? <ArrowUpRight className="h-3 w-3 text-profit" /> : <ArrowDownRight className="h-3 w-3 text-loss" />}
                              <div>
                                <div className="font-semibold">{p.symbol}</div>
                                <div className="text-muted-foreground">{p.sector}</div>
                              </div>
                            </div>
                          </td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">{p.strategy}</td>
                          <td className="text-right py-2.5 px-3 font-mono">₹{p.entry_price?.toFixed(0)}</td>
                          <td className="text-right py-2.5 px-3 font-mono">₹{p.exit_price?.toFixed(0)}</td>
                          <td className="text-right py-2.5 px-3">{p.actual_qty}</td>
                          <td className={`text-right py-2.5 px-3 font-mono font-medium ${pnl >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {formatCurrency(pnl, { showSign: true })}
                          </td>
                          <td className={`text-right py-2.5 px-3 font-mono ${pct >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {pct >= 0 ? '+' : ''}{pct.toFixed(1)}%
                          </td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">{formatDate(p.exit_date ?? '', 'dd MMM')}</td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">{p.exit_reason ?? '—'}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Main Tab ─────────────────────────────────────────────────────────────
export function PositionsTab() {
  const [open, setOpen] = useState<OpenPosition[]>([]);
  const [closed, setClosed] = useState<ClosedPosition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        const [o, c] = await Promise.all([
          queries.getOpenPositions(),
          queries.getClosedPositions(200),
        ]);
        if (o.error) throw o.error;
        if (c.error) throw c.error;
        setOpen(o.data ?? []);
        setClosed(c.data ?? []);
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed');
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const sortedOpen = useMemo(() => {
    const priority: Record<AttentionLevel, number> = { CRITICAL: 0, WARNING: 1, WATCH: 2, OK: 3 };
    return [...open].sort((a, b) => priority[getAttentionLevel(a)] - priority[getAttentionLevel(b)]);
  }, [open]);

  const critical = sortedOpen.filter((p) => getAttentionLevel(p) === 'CRITICAL');
  const warning = sortedOpen.filter((p) => getAttentionLevel(p) === 'WARNING');
  const needsAttention = critical.length + warning.length;
  const totalUnrealized = open.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);
  const totalDeployed = open.reduce((s, p) => s + (p.current_value ?? 0), 0);
  const totalRealized = closed.reduce((s, p) => s + (p.realized_pnl ?? 0), 0);
  const closedWins = closed.filter((p) => (p.realized_pnl ?? 0) > 0).length;
  const wr = closed.length > 0 ? closedWins / closed.length * 100 : 0;
  const errObj = error ? new Error(error) : null;

  return (
    <div className="space-y-4">
      {/* Attention banner */}
      {!loading && needsAttention > 0 && (
        <div className={`rounded-xl border p-4 ${critical.length > 0 ? 'bg-red-500/8 border-red-500/30' : 'bg-yellow-500/8 border-yellow-500/30'}`}>
          <div className="flex items-center gap-2">
            <AlertTriangle className={`h-5 w-5 ${critical.length > 0 ? 'text-red-400' : 'text-yellow-400'}`} />
            <span className={`font-semibold ${critical.length > 0 ? 'text-red-400' : 'text-yellow-400'}`}>
              {critical.length > 0
                ? `${critical.length} position${critical.length > 1 ? 's' : ''} require immediate action`
                : `${warning.length} position${warning.length > 1 ? 's' : ''} need monitoring`}
            </span>
          </div>
          <div className="text-xs text-muted-foreground mt-1">
            {[...critical, ...warning].map((p) => p.symbol).join(' · ')}
          </div>
        </div>
      )}

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? (
          <><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /></>
        ) : (
          <>
            <KPICard title="Open Positions" value={open.length.toString()}
              description={needsAttention > 0 ? `${needsAttention} need attention` : 'All holding normally'}
              icon={<TrendingUp className="h-4 w-4" />}
              change={needsAttention > 0
                ? { value: `${needsAttention} need attention`, type: 'decrease' }
                : { value: 'All OK', type: 'increase' }} />
            <KPICard title="Unrealized P&L" value={formatCurrency(totalUnrealized, { showSign: true })}
              icon={<DollarSign className="h-4 w-4" />}
              description={totalDeployed > 0
                ? `${(totalUnrealized / totalDeployed * 100).toFixed(1)}% on ₹${(totalDeployed / 100000).toFixed(1)}L`
                : 'No open value'}
              change={totalUnrealized !== 0 ? {
                value: totalUnrealized > 0 ? 'Floating profit' : 'Floating loss',
                type: totalUnrealized > 0 ? 'increase' : 'decrease',
              } : undefined} />
            <KPICard title="Realized P&L" value={formatCurrency(totalRealized, { showSign: true, compact: true })}
              description={`${closed.length} closed trades`} icon={<CheckCircle className="h-4 w-4" />}
              change={totalRealized !== 0 ? {
                value: totalRealized > 0 ? 'Net profitable' : 'Net loss',
                type: totalRealized > 0 ? 'increase' : 'decrease',
              } : undefined} />
            <KPICard title="Win Rate" value={closed.length > 0 ? `${wr.toFixed(1)}%` : '—'}
              description={closed.length > 0 ? `${closedWins}W / ${closed.length - closedWins}L` : 'No closed trades'}
              icon={<Target className="h-4 w-4" />} />
          </>
        )}
      </div>

      {/* Open positions — attention priority order */}
      <Panel title="Open Positions" description="Sorted by priority — critical actions first"
        dataSource="supabase" tableName="open_positions" isLoading={loading}>
        <DataGuard data={sortedOpen} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={3} cols={1} />}
          emptyTitle="No open positions"
          emptyDescription="Positions appear when your pipeline writes to open_positions.">
          {(data) => (
            <div className="space-y-2">
              {data.map((p) => <PositionCard key={p.symbol} p={p} />)}
            </div>
          )}
        </DataGuard>
      </Panel>

      {/* Equity curve */}
      {!loading && closed.length > 2 && (
        <Panel title="Equity Curve" description="Cumulative realized P&L across all closed trades"
          dataSource="supabase" tableName="closed_positions" isLoading={loading}>
          <EquityCurve data={closed} />
        </Panel>
      )}

      {/* Closed by month */}
      <Panel title="Closed Trades" description="Grouped by month — click to expand"
        dataSource="supabase" tableName="closed_positions" isLoading={loading}>
        <DataGuard data={closed} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={4} cols={1} />}
          emptyTitle="No closed trades"
          emptyDescription="Closed trades appear when your pipeline writes to closed_positions.">
          {(data) => <ClosedByMonth data={data} />}
        </DataGuard>
      </Panel>
    </div>
  );
}

export default PositionsTab;
