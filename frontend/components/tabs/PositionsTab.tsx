'use client';

import { useState, useEffect, useMemo } from 'react';
import {
  AlertTriangle, TrendingUp, DollarSign, CheckCircle,
  ArrowUpRight, ArrowDownRight, ChevronDown, ChevronRight,
  Bell, Target, Clock, Link as LinkIcon,
} from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonTable } from '@/components/core/DataGuard';
import { formatCurrency, formatDate } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import { RLadder } from '@/components/positions/RLadder';
import type { OpenPosition, ClosedPosition } from '@/types/database';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine,
} from 'recharts';

// ─── Priority classification ──────────────────────────────────────────────
//
// Reads only columns control/position_lifecycle.py actually writes. The
// previous version keyed off `lifecycle`, `event_risk`, `target_1..3` and
// `initial_sl_atr` — none of which has a writer anywhere in the pipeline, so
// every position fell through to "Holding — HOLD" no matter what state it was
// really in. PPLPHARMA sat at 1.55R with a live BOOK_PARTIAL instruction on it
// and this tab rendered it as an ordinary green row.
type AttentionLevel = 'CRITICAL' | 'WARNING' | 'WATCH' | 'OK';

function getAttentionLevel(p: OpenPosition): AttentionLevel {
  if (p.sl_breach_alerted) return 'CRITICAL';
  if (p.exit_signal) return 'CRITICAL';
  // action_required carries the exit engine's verdict verbatim, e.g.
  // "BOOK 7 — 1.55R >= 1.5R — book 7/15 @ 192.05 (+10.41%)".
  if (p.action_required && !/^HOLD/i.test(p.action_required)) return 'CRITICAL';
  if (p.sl_proximity_alerted) return 'WARNING';
  if (p.target_hit) return 'WARNING';
  if ((p.r_multiple_current ?? 0) <= -0.7) return 'WARNING';   // 70% of planned risk gone
  if ((p.r_multiple_current ?? 0) >= 1.0) return 'WATCH';      // breakeven rung in reach
  return 'OK';
}

function attentionReason(p: OpenPosition, policy: ExitPolicy): string {
  if (p.sl_breach_alerted) return 'Stop breached — exit now';
  if (p.exit_signal) return `Exit signal: ${p.exit_signal}`;
  if (p.action_required && !/^HOLD/i.test(p.action_required)) return p.action_required;
  if (p.sl_proximity_alerted) return 'Within 2% of the stop';
  const r = p.r_multiple_current;
  if (r == null) return 'Awaiting first price update';
  if (r <= -0.7) return `${r.toFixed(2)}R — ${(Math.abs(r) * 100).toFixed(0)}% of planned risk used`;
  if (r >= policy.exit_trail_after_r) return `${r.toFixed(2)}R — stop is trailing the high`;
  if (r >= policy.exit_partial_book_r) return `${r.toFixed(2)}R — past the book-half rung`;
  if (r >= 1) return `${r.toFixed(2)}R — ${(policy.exit_partial_book_r - r).toFixed(2)}R to book half`;
  return `${r.toFixed(2)}R — working, no rule triggered`;
}

type ExitPolicy = {
  exit_partial_book_r: number; exit_partial_book_pct: number;
  exit_trail_r: number; exit_trail_after_r: number; exit_target_r: number;
  exit_time_stop_days: number; exit_time_stop_min_r: number;
  source: 'defaults' | 'system_config';
};

const FALLBACK_POLICY: ExitPolicy = {
  exit_partial_book_r: 1.5, exit_partial_book_pct: 50, exit_trail_r: 1.5,
  exit_trail_after_r: 2.0, exit_target_r: 3.0, exit_time_stop_days: 15,
  exit_time_stop_min_r: 0.5, source: 'defaults',
};

function rungsFor(policy: ExitPolicy) {
  return [
    { r: -1, label: 'stop', hint: 'Planned stop — the full 1R of risk sized at entry' },
    { r: 0, label: 'entry', hint: 'Break-even' },
    { r: policy.exit_partial_book_r,
      label: `book ${policy.exit_partial_book_pct.toFixed(0)}%`,
      hint: `At ${policy.exit_partial_book_r}R the engine books ${policy.exit_partial_book_pct}% and moves the stop to entry` },
    { r: policy.exit_trail_after_r, label: 'trail',
      hint: `Past ${policy.exit_trail_after_r}R the stop trails ${policy.exit_trail_r}R below the high-water mark` },
    { r: policy.exit_target_r, label: 'target',
      hint: `Hard target — the position is closed at ${policy.exit_target_r}R` },
  ];
}

const LEVEL_STYLE: Record<AttentionLevel, { border: string; bg: string; dot: string; text: string }> = {
  CRITICAL: { border: 'border-red-500/40',    bg: 'bg-red-500/8',    dot: 'bg-red-500',    text: 'text-red-400' },
  WARNING:  { border: 'border-yellow-500/40', bg: 'bg-yellow-500/8', dot: 'bg-yellow-500', text: 'text-yellow-400' },
  WATCH:    { border: 'border-blue-500/40',   bg: 'bg-blue-500/8',   dot: 'bg-blue-500',   text: 'text-blue-400' },
  OK:       { border: 'border-border/50',     bg: '',                dot: 'bg-green-500',  text: 'text-green-400' },
};

// ─── Position card ────────────────────────────────────────────────────────
function PositionCard({ p, policy }: { p: OpenPosition; policy: ExitPolicy }) {
  const level = getAttentionLevel(p);
  const s = LEVEL_STYLE[level];
  const pnl = p.unrealized_pnl ?? 0;
  const pct = p.pnl_pct ?? 0;

  const entry  = p.entry_price ?? 0;
  const stop0  = p.planned_stop ?? null;
  const active = p.active_sl ?? null;
  const target = p.planned_target ?? p.target_price ?? null;

  // Initial risk as a % of entry — the denominator the R figures are built on.
  const riskPct = stop0 && entry && stop0 < entry ? ((entry - stop0) / entry) * 100 : null;
  const slDist = active && p.current_price
    ? ((p.current_price - active) / p.current_price) * 100 : null;

  // The stop having moved above the sizing stop is the single most consequential
  // fact about a live trade, and no price column states it. Say it outright.
  const stopMoved = stop0 != null && active != null && active > stop0 + 0.005;
  const heldDays = p.entry_date
    ? Math.max(0, Math.round((Date.now() - new Date(p.entry_date).getTime()) / 86_400_000))
    : null;
  const timeStopClose = heldDays != null
    && heldDays >= policy.exit_time_stop_days - 3
    && (p.r_multiple_current ?? 0) < policy.exit_time_stop_min_r;

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
              {p.regime_at_entry && (
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted/60 text-muted-foreground"
                  title="Market regime when this position was opened">
                  {p.regime_at_entry}
                </span>
              )}
              {p.signal_date && (
                <span className="text-[10px] text-muted-foreground inline-flex items-center gap-1"
                  title="This position is attributed to a signal — its outcome feeds engine scoring">
                  <LinkIcon className="h-2.5 w-2.5" />{formatDate(p.signal_date, 'dd MMM')}
                </span>
              )}
            </div>
            <div className={`text-xs mt-1 font-medium ${s.text}`}>{attentionReason(p, policy)}</div>
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

      <RLadder
        currentR={p.r_multiple_current}
        mfePct={p.max_favorable_excursion}
        maePct={p.max_adverse_excursion}
        riskPct={riskPct}
        rungs={rungsFor(policy)}
        breakevenMoved={p.breakeven_moved}
        trailActivated={p.trail_activated}
        partialBooked={(p.partial_booked_qty ?? 0) > 0}
      />

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-3 pt-3 border-t border-border/20 text-xs">
        <div>
          <div className="text-muted-foreground">Entry</div>
          <div className="font-mono mt-0.5">
            ₹{entry ? entry.toFixed(2) : '—'}
            {p.current_qty != null && (
              <span className="text-muted-foreground ml-1">×{p.current_qty}</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-muted-foreground">CMP</div>
          <div className="font-mono mt-0.5">₹{p.current_price?.toFixed(2) ?? '—'}</div>
        </div>
        <div>
          <div className="text-muted-foreground">
            Stop {stopMoved && <span className="text-profit">↑ moved</span>}
          </div>
          <div className={`font-mono mt-0.5 ${slDist != null && slDist < 3 ? 'text-loss font-semibold' : ''}`}>
            {active ? `₹${active.toFixed(2)}` : '—'}
            {slDist != null && <span className="text-muted-foreground ml-1">({slDist.toFixed(1)}%)</span>}
          </div>
          {stopMoved && (
            <div className="text-[10px] text-muted-foreground line-through">₹{stop0!.toFixed(2)}</div>
          )}
        </div>
        <div>
          <div className="text-muted-foreground">Target</div>
          <div className="font-mono mt-0.5">
            {target ? `₹${target.toFixed(2)}` : '—'}
            {target && p.current_price && (
              <span className="text-muted-foreground ml-1">
                ({((target - p.current_price) / p.current_price * 100).toFixed(1)}%)
              </span>
            )}
          </div>
        </div>
      </div>

      {timeStopClose && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-yellow-400">
          <Clock className="h-3 w-3" />
          {heldDays}d held and under {policy.exit_time_stop_min_r}R — time stop fires at {policy.exit_time_stop_days}d
        </div>
      )}
      {/* Only warn when the quantities genuinely disagree. reconcile_status can
          be stale — it describes the last time drift was DETECTED, not the
          current state — and a banner that says "Kite shows 15, book shows 15"
          under the heading "mismatch" teaches you to ignore the banner. */}
      {p.reconcile_status && p.reconcile_status !== 'MATCHED'
        && p.kite_qty != null && p.kite_qty !== (p.current_qty ?? p.actual_qty) && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-yellow-400">
          <Bell className="h-3 w-3" />
          Broker mismatch ({p.reconcile_status}) — Kite shows {p.kite_qty},
          book shows {p.current_qty ?? p.actual_qty}
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
                      {['Symbol', 'Strategy', 'Entry', 'Exit', 'Qty', 'P&L', '%', 'R', 'Held', 'Exit Date', 'Reason'].map((h) => (
                        <th key={h} className={`py-2 px-3 font-medium text-muted-foreground ${h === 'Symbol' ? 'text-left' : 'text-right'}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {g.trades.map((p) => {
                      const pnl = p.realized_pnl ?? 0;
                      // Derive the % when the column is null rather than
                      // coalescing to 0 — VIJAYA showed "+0.0%" against a
                      // -₹127.20 loss, which reads as a scratch trade.
                      const pct = p.pnl_pct ?? (p.entry_price && p.exit_price
                        ? ((p.exit_price - p.entry_price) / p.entry_price) * 100
                        : null);
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
                          <td className={`text-right py-2.5 px-3 font-mono ${
                            pct == null ? 'text-muted-foreground' : pct >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {pct == null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`}
                          </td>
                          {/* R is the comparable unit across trades of different
                              size and volatility; % alone makes a tight-stop
                              scalp look identical to a wide-stop swing. */}
                          <td className={`text-right py-2.5 px-3 font-mono ${
                            p.r_multiple == null ? 'text-muted-foreground'
                              : p.r_multiple >= 0 ? 'text-profit' : 'text-loss'}`}
                            title={p.r_multiple == null
                              ? 'No planned_stop recorded at entry — R cannot be derived'
                              : `Realised ${p.r_multiple.toFixed(2)}R`}>
                            {p.r_multiple == null ? '—'
                              : `${p.r_multiple >= 0 ? '+' : ''}${p.r_multiple.toFixed(2)}R`}
                          </td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">
                            {p.hold_days != null ? `${p.hold_days}d` : '—'}
                          </td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">{formatDate(p.exit_date ?? '', 'dd MMM')}</td>
                          <td className="text-right py-2.5 px-3 text-muted-foreground">
                            <span title={p.exit_reason_detail ?? undefined}>{p.exit_reason ?? '—'}</span>
                          </td>
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
  const [policy, setPolicy] = useState<ExitPolicy>(FALLBACK_POLICY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      try {
        const [o, c, pol] = await Promise.all([
          queries.getOpenPositions(),
          queries.getClosedPositions(200),
          queries.getExitPolicy(),
        ]);
        if (o.error) throw o.error;
        if (c.error) throw c.error;
        setOpen(o.data ?? []);
        setClosed(c.data ?? []);
        setPolicy({ ...FALLBACK_POLICY, ...pol });
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

  // ── Open risk ──────────────────────────────────────────────────────────
  // What the book actually loses if every stop fills at its current level.
  // This is the number that decides whether another position can be taken, and
  // it was nowhere on the dashboard. Positions whose stop is at or above entry
  // contribute nothing — that is the whole point of moving to breakeven.
  const openRisk = open.reduce((s, p) => {
    const qty  = p.current_qty ?? p.actual_qty ?? 0;
    const stop = p.active_sl ?? p.planned_stop;
    const px   = p.current_price ?? p.entry_price;
    if (!qty || !stop || !px) return s;
    return s + Math.max(0, (px - stop) * qty);
  }, 0);
  const riskFree = open.filter((p) => p.breakeven_moved).length;

  // Realised R only over trades the current system actually produced. Averaging
  // R across the pre-automation era would describe a different system — the
  // same reason the Performance tab splits on signal attribution.
  const attributed = closed.filter((p) => p.signal_date && p.r_multiple != null);
  const avgR = attributed.length
    ? attributed.reduce((s, p) => s + (p.r_multiple ?? 0), 0) / attributed.length
    : null;

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
              // Hardcoded lakhs rendered a ₹5,231 book as "₹0.0L". Capital is
              // now ₹20,000, so the compact formatter (which switches units by
              // magnitude) is the only one that stays readable at both scales.
              description={totalDeployed > 0
                ? `${(totalUnrealized / totalDeployed * 100).toFixed(1)}% on ${formatCurrency(totalDeployed, { compact: true })} deployed`
                : 'No open value'}
              change={totalUnrealized !== 0 ? {
                value: totalUnrealized > 0 ? 'Floating profit' : 'Floating loss',
                type: totalUnrealized > 0 ? 'increase' : 'decrease',
              } : undefined} />
            <KPICard title="Open Risk" value={formatCurrency(openRisk)}
              icon={<AlertTriangle className="h-4 w-4" />}
              description={openRisk > 0
                ? `Loss if every stop fills · ${riskFree} risk-free`
                : open.length > 0 ? 'Every stop is at or above entry' : 'Nothing at risk'}
              change={riskFree > 0
                ? { value: `${riskFree}/${open.length} risk-free`, type: 'increase' }
                : undefined} />
            <KPICard title="Avg R (attributed)"
              value={avgR != null ? `${avgR >= 0 ? '+' : ''}${avgR.toFixed(2)}R` : '—'}
              description={avgR != null
                ? `${attributed.length} signal-attributed closes`
                : 'No attributed closes with a recorded stop yet'}
              icon={<Target className="h-4 w-4" />}
              change={avgR != null
                ? { value: avgR > 0 ? 'Positive expectancy' : 'Negative expectancy',
                    type: avgR > 0 ? 'increase' : 'decrease' }
                : undefined} />
          </>
        )}
      </div>

      {/* Open positions — attention priority order */}
      <Panel title="Open Positions"
        description={`Progress shown in R against the live exit policy — book ${policy.exit_partial_book_pct.toFixed(0)}% at ${policy.exit_partial_book_r}R, trail past ${policy.exit_trail_after_r}R, target ${policy.exit_target_r}R`}
        dataSource="supabase" tableName="open_positions" isLoading={loading}>
        <DataGuard data={sortedOpen} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={3} cols={1} />}
          emptyTitle="No open positions"
          emptyDescription="Positions appear when your pipeline writes to open_positions.">
          {(data) => (
            <div className="space-y-2">
              {data.map((p) => <PositionCard key={p.symbol} p={p} policy={policy} />)}
              {policy.source === 'defaults' && (
                <div className="text-[10px] text-muted-foreground pt-1">
                  Exit thresholds could not be read from system_config — showing the
                  backend defaults. Rungs may not match a tuned policy.
                </div>
              )}
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
