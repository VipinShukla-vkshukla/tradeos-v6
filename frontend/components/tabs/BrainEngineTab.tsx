'use client';

import { useState, useEffect, useMemo } from 'react';
import {
  CheckCircle, XCircle, Clock, AlertTriangle, RotateCcw,
  ChevronDown, ChevronRight, Cpu, TrendingUp, Activity,
  Shield, FileCode, Settings2, Lightbulb, BarChart2,
} from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonTable, SkeletonChart } from '@/components/core/DataGuard';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import { formatDate, formatCurrency } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import { brainApi } from '@/lib/api';
import type { BrainProposal, BrainAnalysisLog } from '@/types/database';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, LineChart, Line, ReferenceLine,
} from 'recharts';

// ─── Proposal type icons and labels ───────────────────────────────────────
const PROPOSAL_TYPE_META: Record<string, { icon: React.ElementType; label: string; color: string }> = {
  SCRIPT_PATCH:    { icon: FileCode,   label: 'Script Patch',  color: 'text-violet-400' },
  PARAMETER_TUNE:  { icon: Settings2,  label: 'Parameter',     color: 'text-blue-400' },
  CONFIG_CHANGE:   { icon: Settings2,  label: 'Config',        color: 'text-amber-400' },
  STRATEGY_ADD:    { icon: TrendingUp, label: 'Strategy',      color: 'text-green-400' },
};

// ─── Status badge ─────────────────────────────────────────────────────────
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    PENDING:     'bg-amber-500/20 text-amber-400 border border-amber-500/30',
    APPROVED:    'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
    REJECTED:    'bg-red-500/20 text-red-400 border border-red-500/30',
    APPLIED:     'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
    ROLLED_BACK: 'bg-purple-500/20 text-purple-400 border border-purple-500/30',
  };
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${map[status] ?? 'bg-muted text-muted-foreground'}`}>
      {status}
    </span>
  );
}

// ─── Expandable proposal card (for pending items) ─────────────────────────
function ProposalCard({
  proposal, onApprove, onReject,
}: {
  proposal: BrainProposal;
  onApprove: (id: number) => void;
  onReject: (id: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const meta = PROPOSAL_TYPE_META[proposal.proposal_type ?? ''] ?? PROPOSAL_TYPE_META.CONFIG_CHANGE;
  const Icon = meta.icon;
  const isPending = proposal.status === 'PENDING';

  return (
    <div className={`border rounded-lg overflow-hidden transition-all ${isPending ? 'border-yellow-500/30 bg-yellow-500/5' : 'border-border/50'}`}>
      {/* Card header — always visible */}
      <div className="flex items-start gap-3 p-4 cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <Icon className={`h-5 w-5 shrink-0 mt-0.5 ${meta.color}`} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-sm truncate">{proposal.target_key}</span>
            {proposal.high_impact && (
              <span className="text-xs px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-400 border border-orange-500/30 font-medium flex items-center gap-1">
                <Shield className="h-3 w-3" />HIGH IMPACT
              </span>
            )}
            <StatusBadge status={proposal.status} />
          </div>
          <div className="text-xs text-muted-foreground mt-1 line-clamp-2">{proposal.rationale}</div>

          {/* Change summary — always visible */}
          {proposal.current_value && proposal.proposed_value && (
            <div className="flex items-center gap-2 mt-2 text-xs">
              <span className="font-mono bg-red-500/10 text-red-400 px-1.5 py-0.5 rounded line-through">
                {proposal.current_value}
              </span>
              <span className="text-muted-foreground">→</span>
              <span className="font-mono bg-green-500/10 text-green-400 px-1.5 py-0.5 rounded">
                {proposal.proposed_value}
              </span>
              {proposal.confidence != null && (
                <span className="text-muted-foreground ml-1">{Math.round(proposal.confidence * 100)}% confidence</span>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-xs text-muted-foreground">{formatDate(proposal.created_at, 'dd MMM')}</span>
          {expanded ? <ChevronDown className="h-4 w-4 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 text-muted-foreground" />}
        </div>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-border/30 px-4 pb-4 pt-3 space-y-3">
          {proposal.rationale && (
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wide">Rationale</div>
              <div className="text-sm">{proposal.rationale}</div>
            </div>
          )}
          {proposal.evidence && typeof proposal.evidence === 'object' && (
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wide">Evidence</div>
              <pre className="text-xs bg-panel-hover rounded p-2 overflow-x-auto">
                {JSON.stringify(proposal.evidence, null, 2)}
              </pre>
            </div>
          )}
          {proposal.backtest_result && typeof proposal.backtest_result === 'object' && (
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1 uppercase tracking-wide">Backtest</div>
              <div className="flex gap-4 text-sm">
                {Object.entries(proposal.backtest_result as Record<string, unknown>).slice(0, 4).map(([k, v]) => (
                  <div key={k}>
                    <div className="text-xs text-muted-foreground">{k.replace(/_/g, ' ')}</div>
                    <div className="font-mono font-medium">{String(v)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {isPending && (
            <div className="flex gap-2 pt-1 border-t border-border/30">
              <Button size="sm" className="bg-green-500/20 text-green-400 hover:bg-green-500/30 border border-green-500/30"
                onClick={() => onApprove(proposal.id)}>
                <CheckCircle className="h-3.5 w-3.5 mr-1.5" />Approve
              </Button>
              <Button size="sm" variant="outline"
                className="text-red-400 border-red-500/30 hover:bg-red-500/10"
                onClick={() => onReject(proposal.id)}>
                <XCircle className="h-3.5 w-3.5 mr-1.5" />Reject
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Coverage trend (is the brain learning from more signals over time?) ──
function CoverageTrendChart({ logs }: { logs: BrainAnalysisLog[] }) {
  const data = [...logs]
    .sort((a, b) => a.run_date.localeCompare(b.run_date))
    .map((l) => ({
      date: formatDate(l.run_date, 'dd MMM'),
      coverage: Number((l.coverage_pct ?? 0).toFixed(1)),
      signals: l.signals_analyzed,
      proposals: l.proposals_generated,
    }));

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.4} vertical={false} />
          <XAxis dataKey="date" fontSize={10} tickLine={false} axisLine={false} stroke="var(--text-muted)" />
          <YAxis yAxisId="left" fontSize={10} tickLine={false} axisLine={false}
            stroke="var(--text-muted)" tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
          <YAxis yAxisId="right" orientation="right" fontSize={10} tickLine={false} axisLine={false}
            stroke="var(--text-muted)" />
          <Tooltip
            contentStyle={{ backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' }}
            formatter={(v: number, name: string) => [
              name === 'coverage' ? `${v}%` : v,
              name === 'coverage' ? 'Outcome Coverage' : name === 'signals' ? 'Signals Analyzed' : 'Proposals Generated',
            ]}
          />
          <ReferenceLine yAxisId="left" y={30} stroke="var(--warning)" strokeDasharray="3 3" opacity={0.5} />
          <Line yAxisId="left" type="monotone" dataKey="coverage" stroke="#22c55e" strokeWidth={2} dot={false} name="coverage" />
          <Line yAxisId="right" type="monotone" dataKey="proposals" stroke="#6366f1" strokeWidth={1.5} dot={{ r: 3 }} name="proposals" />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex items-center gap-4 mt-1 text-xs text-muted-foreground justify-center">
        <div className="flex items-center gap-1"><div className="h-2 w-2 rounded-full bg-green-500" /><span>Coverage %</span></div>
        <div className="flex items-center gap-1"><div className="h-2 w-2 rounded-full bg-indigo-500" /><span>Proposals generated</span></div>
        <div className="flex items-center gap-1 text-yellow-500/70"><span>─ ─</span><span>30% threshold</span></div>
      </div>
    </div>
  );
}

// ─── Proposal type breakdown ──────────────────────────────────────────────
function ProposalTypeBreakdown({ proposals }: { proposals: BrainProposal[] }) {
  const byType = proposals.reduce<Record<string, { total: number; applied: number; rejected: number }>>((acc, p) => {
    const t = p.proposal_type ?? 'UNKNOWN';
    if (!acc[t]) acc[t] = { total: 0, applied: 0, rejected: 0 };
    acc[t].total++;
    if (p.status === 'APPLIED') acc[t].applied++;
    if (p.status === 'REJECTED') acc[t].rejected++;
    return acc;
  }, {});

  const data = Object.entries(byType).map(([type, v]) => ({
    name: PROPOSAL_TYPE_META[type]?.label ?? type,
    total: v.total,
    applied: v.applied,
    rejected: v.rejected,
    pending: v.total - v.applied - v.rejected,
  }));

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
          <Bar dataKey="applied" stackId="a" fill="#22c55e" fillOpacity={0.8} name="Applied" />
          <Bar dataKey="pending" stackId="a" fill="#eab308" fillOpacity={0.8} name="Pending" />
          <Bar dataKey="rejected" stackId="a" fill="#ef4444" fillOpacity={0.8} name="Rejected" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ─── LLM insights parser ──────────────────────────────────────────────────
function LLMInsightsCard({ insights }: { insights: string }) {
  // Try to parse JSON, fall back to plain text display
  let parsed: Record<string, unknown> | null = null;
  try {
    const clean = insights.replace(/^"|"$/g, '').replace(/\\n/g, '\n').replace(/\\"/g, '"');
    parsed = JSON.parse(clean);
  } catch {
    parsed = null;
  }

  if (!insights) return null;

  if (parsed && typeof parsed === 'object') {
    return (
      <div className="space-y-3">
        {Object.entries(parsed).slice(0, 6).map(([key, val]) => (
          <div key={key} className="p-3 rounded-lg bg-panel-hover border border-border/30">
            <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1">
              {key.replace(/_/g, ' ')}
            </div>
            <div className="text-sm">
              {Array.isArray(val)
                ? <ul className="space-y-1">{val.map((v, i) => <li key={i} className="flex gap-2 text-xs"><span className="text-muted-foreground">•</span>{String(v)}</li>)}</ul>
                : typeof val === 'object'
                  ? <pre className="text-xs overflow-x-auto">{JSON.stringify(val, null, 2)}</pre>
                  : <span>{String(val)}</span>}
            </div>
          </div>
        ))}
      </div>
    );
  }

  // Plain text — split by newlines and display as readable paragraphs
  return (
    <div className="space-y-2">
      {insights.split('\n').filter(Boolean).map((line, i) => (
        <div key={i} className="flex gap-2 text-sm">
          {line.startsWith('-') || line.startsWith('•')
            ? <><span className="text-muted-foreground shrink-0">•</span><span>{line.replace(/^[-•]\s*/, '')}</span></>
            : <span className="text-muted-foreground">{line}</span>}
        </div>
      ))}
    </div>
  );
}

// ─── Applied changes timeline ─────────────────────────────────────────────
function AppliedTimeline({ proposals }: { proposals: BrainProposal[] }) {
  const applied = [...proposals]
    .filter((p) => p.status === 'APPLIED' && p.applied_at)
    .sort((a, b) => (b.applied_at ?? '').localeCompare(a.applied_at ?? ''))
    .slice(0, 10);

  return (
    <div className="space-y-2">
      {applied.map((p) => {
        const meta = PROPOSAL_TYPE_META[p.proposal_type ?? ''] ?? PROPOSAL_TYPE_META.CONFIG_CHANGE;
        const Icon = meta.icon;
        return (
          <div key={p.id} className="flex items-start gap-3 p-3 rounded-lg bg-panel-hover border border-border/30">
            <Icon className={`h-4 w-4 shrink-0 mt-0.5 ${meta.color}`} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="font-medium text-sm truncate">{p.target_key}</span>
                {p.confidence != null && (
                  <span className="text-xs text-muted-foreground">{Math.round(p.confidence * 100)}% confidence</span>
                )}
              </div>
              {p.current_value && p.proposed_value && (
                <div className="flex items-center gap-1 mt-0.5 text-xs">
                  <span className="font-mono text-red-400/70 line-through">{p.current_value}</span>
                  <span className="text-muted-foreground">→</span>
                  <span className="font-mono text-green-400">{p.proposed_value}</span>
                </div>
              )}
              {p.rationale && <div className="text-xs text-muted-foreground mt-0.5 truncate">{p.rationale}</div>}
            </div>
            <div className="text-xs text-muted-foreground shrink-0 whitespace-nowrap">
              {formatDate(p.applied_at ?? p.created_at, 'dd MMM')}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Main Tab ─────────────────────────────────────────────────────────────
export function BrainEngineTab() {
  const [proposals, setProposals] = useState<BrainProposal[]>([]);
  const [logs, setLogs] = useState<BrainAnalysisLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<{ type: 'ok' | 'err'; text: string } | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [p, l] = await Promise.all([
        queries.getBrainProposals(),
        queries.getBrainAnalysisLog(10),
      ]);
      if (p.error) throw p.error;
      if (l.error) throw l.error;
      setProposals(p.data ?? []);
      setLogs(l.data ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleApprove(id: number) {
    try {
      await brainApi.approve(id, { reviewed_by: 'dashboard' });
      setActionMsg({ type: 'ok', text: 'Approved — will apply on next pipeline run' });
      await load();
    } catch { setActionMsg({ type: 'err', text: 'Approve failed' }); }
    setTimeout(() => setActionMsg(null), 4000);
  }

  async function handleReject(id: number) {
    try {
      await brainApi.reject(id, { reviewed_by: 'dashboard' });
      setActionMsg({ type: 'ok', text: 'Rejected' });
      await load();
    } catch { setActionMsg({ type: 'err', text: 'Reject failed' }); }
    setTimeout(() => setActionMsg(null), 4000);
  }

  const pending = proposals.filter((p) => p.status === 'PENDING');
  const highImpact = pending.filter((p) => p.high_impact);
  const applied = proposals.filter((p) => p.status === 'APPLIED');
  const latestRun = logs[0];
  const latestInsights = latestRun?.llm_insights;
  const errObj = error ? new Error(error) : null;

  // Health assessment
  const coverage = latestRun?.coverage_pct ?? 0;
  const healthStatus = coverage >= 30 ? 'GOOD' : coverage >= 15 ? 'LOW' : 'CRITICAL';
  const healthColor = { GOOD: 'text-profit', LOW: 'text-yellow-400', CRITICAL: 'text-loss' }[healthStatus];

  return (
    <div className="space-y-4">
      {/* Action feedback */}
      {actionMsg && (
        <div className={`rounded px-4 py-2 text-sm border ${actionMsg.type === 'ok' ? 'bg-green-500/10 border-green-500/20 text-green-400' : 'bg-red-500/10 border-red-500/20 text-red-400'}`}>
          {actionMsg.text}
        </div>
      )}

      {/* ── SECTION 1: Decision Queue (most important) ─────────────────── */}
      {!loading && pending.length > 0 && (
        <div className="border border-yellow-500/30 rounded-xl bg-yellow-500/5 p-4">
          <div className="flex items-center gap-2 mb-3">
            <Clock className="h-5 w-5 text-yellow-400" />
            <h3 className="font-semibold text-yellow-400">
              {pending.length} Proposal{pending.length > 1 ? 's' : ''} Awaiting Your Decision
            </h3>
            {highImpact.length > 0 && (
              <span className="text-xs px-2 py-0.5 rounded bg-orange-500/20 text-orange-400 border border-orange-500/30">
                {highImpact.length} HIGH IMPACT
              </span>
            )}
          </div>
          <div className="space-y-2">
            {pending.map((p) => (
              <ProposalCard key={p.id} proposal={p} onApprove={handleApprove} onReject={handleReject} />
            ))}
          </div>
        </div>
      )}

      {/* Empty decision queue */}
      {!loading && pending.length === 0 && (
        <div className="flex items-center gap-3 p-4 rounded-xl border border-green-500/20 bg-green-500/5">
          <CheckCircle className="h-5 w-5 text-green-400 shrink-0" />
          <div>
            <div className="font-medium text-green-400">No pending decisions</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              All proposals have been reviewed. Last analysis: {latestRun ? formatDate(latestRun.run_date, 'dd MMM yyyy') : 'never'}
            </div>
          </div>
        </div>
      )}

      {/* ── SECTION 2: Brain health KPIs ──────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? (
          <><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /></>
        ) : (
          <>
            <KPICard
              title="Outcome Coverage"
              value={latestRun ? `${latestRun.coverage_pct?.toFixed(1) ?? 0}%` : '—'}
              description="% signals with known outcome"
              icon={<Activity className="h-4 w-4" />}
              change={latestRun ? {
                value: healthStatus === 'GOOD' ? 'Sufficient for learning' : healthStatus === 'LOW' ? 'More outcomes needed' : 'Too few outcomes — brain limited',
                type: healthStatus === 'GOOD' ? 'increase' : healthStatus === 'LOW' ? 'neutral' : 'decrease',
              } : undefined}
            />
            <KPICard
              title="Signals Analyzed"
              value={latestRun?.signals_analyzed?.toString() ?? '—'}
              description={`${latestRun?.signals_with_outcomes ?? 0} with outcomes`}
              icon={<BarChart2 className="h-4 w-4" />}
            />
            <KPICard
              title="Total Applied"
              value={applied.length.toString()}
              description="Proposals applied to system"
              icon={<CheckCircle className="h-4 w-4" />}
              change={{ value: `${proposals.filter((p) => p.status === 'REJECTED').length} rejected`, type: 'neutral' }}
            />
            <KPICard
              title="Last Analysis"
              value={latestRun ? formatDate(latestRun.run_date, 'dd MMM') : '—'}
              description={latestRun ? `${latestRun.run_mode} mode · ${latestRun.execution_time_sec?.toFixed(0) ?? '?'}s` : 'Never run'}
              icon={<Cpu className="h-4 w-4" />}
            />
          </>
        )}
      </div>

      {/* ── SECTION 3: What the brain learned (LLM insights) ──────────── */}
      {latestInsights && (
        <Panel
          title="Brain Insights — Latest Analysis"
          description={`${latestRun?.run_date} — what the brain engine concluded from ${latestRun?.signals_analyzed} signals`}
          dataSource="supabase" tableName="brain_analysis_log" isLoading={loading}>
          <LLMInsightsCard insights={latestInsights} />
        </Panel>
      )}

      {/* ── SECTION 4: Is coverage improving? + Proposal funnel ──────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel
          title="Coverage Trend"
          description="Outcome coverage % over time — needs to be above 30% for reliable learning"
          dataSource="supabase" tableName="brain_analysis_log" isLoading={loading}>
          <DataGuard data={logs} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle="No analysis history" emptyDescription="Run brain_engine.py to see trend">
            {(data) => <CoverageTrendChart logs={data} />}
          </DataGuard>
        </Panel>

        <Panel
          title="What Type of Changes Is Brain Making?"
          description="Proposals by type — applied vs pending vs rejected"
          dataSource="supabase" tableName="brain_proposals" isLoading={loading}>
          <DataGuard data={proposals} isLoading={loading} error={errObj}
            loadingContent={<SkeletonChart className="h-48" />}
            emptyTitle="No proposals yet" emptyDescription="Run brain_engine.py">
            {(data) => <ProposalTypeBreakdown proposals={data} />}
          </DataGuard>
        </Panel>
      </div>

      {/* ── SECTION 5: What has already changed in my system ─────────── */}
      {applied.length > 0 && (
        <Panel
          title="Changes Applied to Your System"
          description="Approved proposals that have been written into system_config or scripts — most recent first"
          dataSource="supabase" tableName="brain_proposals" isLoading={loading}>
          <AppliedTimeline proposals={proposals} />
        </Panel>
      )}

      {/* ── SECTION 6: All proposals (collapsed table for history) ────── */}
      <Panel
        title="Full Proposal History"
        description={`${proposals.length} total proposals — expand rows to see detail`}
        dataSource="supabase" tableName="brain_proposals" isLoading={loading}>
        <DataGuard data={proposals.filter((p) => p.status !== 'PENDING')} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={5} cols={6} />}
          emptyTitle="No historical proposals" emptyDescription="Only pending proposals exist — approve or reject them above">
          {(data) => (
            <div className="space-y-2">
              {data.map((p) => (
                <ProposalCard key={p.id} proposal={p} onApprove={handleApprove} onReject={handleReject} />
              ))}
            </div>
          )}
        </DataGuard>
      </Panel>
    </div>
  );
}

export default BrainEngineTab;
