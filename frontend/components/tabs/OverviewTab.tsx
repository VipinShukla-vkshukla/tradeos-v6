'use client';

// Overview — the screen that didn't exist before this pass.
//
// Book value/P&L already lives in DailyBookSummary, mounted globally above
// every tab, so it isn't repeated here. What's missing without this screen:
// system health (was a header icon only), today's funnel for both books
// (how many names were even looked at vs. actually taken), what the
// learning loop is waiting on you to review, and the most recent alerts —
// all real queries, no invented aggregate table.

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Panel } from '@/components/core/Panel';
import { queries } from '@/lib/supabase';
import { formatDate, formatCurrency } from '@/lib/formatters';
import type { BrainProposal, Signal, BrainAnalysisLog } from '@/types/database';

// Real proposal_type enum — matches BrainEngineTab's own PROPOSAL_TYPE_META,
// reused here rather than inventing a Promote/Hold/Gate-tune taxonomy the
// actual column doesn't carry.
const PROPOSAL_TAG: Record<string, string> = {
  STRATEGY_ADD: 'bg-emerald-500/15 text-emerald-400',
  PARAMETER_TUNE: 'bg-sky-500/15 text-sky-400',
  CONFIG_CHANGE: 'bg-amber-500/15 text-amber-400',
  SCRIPT_PATCH: 'bg-violet-500/15 text-violet-400',
};
const PROPOSAL_LABEL: Record<string, string> = {
  STRATEGY_ADD: 'Strategy', PARAMETER_TUNE: 'Parameter', CONFIG_CHANGE: 'Config', SCRIPT_PATCH: 'Script Patch',
};

interface HealthSummary {
  verdict: 'READY' | 'DEGRADED' | 'BLOCKED';
  counts: { ok: number; warn: number; block: number; info: number };
  checks: { id: string; label: string; severity: string; detail?: string }[];
}

const VERDICT_STYLE: Record<string, string> = {
  READY: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  DEGRADED: 'border-amber-500/40 bg-amber-500/10 text-amber-300',
  BLOCKED: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
};

// Stage-box funnel — matches Main.dc.html's Row B2 exactly: bordered boxes
// joined by arrows, the last box highlighted green when it has a nonzero
// count, muted when it's a zero day.
function FunnelStages({ stages }: { stages: [string, number][] }) {
  const finalIdx = stages.length - 1;
  return (
    <div className="flex items-center flex-wrap gap-0">
      {stages.map(([label, val], i) => {
        const isFinal = i === finalIdx;
        const finalHit = isFinal && val > 0;
        return (
          <div key={label} className="flex items-center">
            <div className={`rounded-[9px] border px-3.5 py-2 text-center min-w-[92px] ${
              finalHit ? 'border-emerald-500/35 bg-emerald-500/[0.07]' : 'border-border bg-panel-hover'}`}>
              <div className={`text-[17px] font-bold ${finalHit ? 'text-profit' : ''}`}>{val}</div>
              <div className="text-[9px] text-muted-foreground uppercase tracking-wide mt-0.5">{label}</div>
            </div>
            {i < finalIdx && <span className="px-2 text-muted-foreground text-sm">→</span>}
          </div>
        );
      })}
    </div>
  );
}

function FunnelBlock({ book, bar, stages, note }: {
  book: 'SWING' | 'INTRADAY'; bar: number | null; stages: [string, number][]; note: string;
}) {
  return (
    <div className="py-3.5 border-t border-border first:border-t-0 first:pt-0">
      <div className="flex items-center gap-2 mb-2.5">
        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide ${
          book === 'SWING' ? 'badge-swing' : 'badge-intraday'}`}>
          {book === 'SWING' ? 'Swing' : 'Intraday'}
        </span>
        <span className="text-[11px] text-muted-foreground">bar (edge) {bar != null ? bar.toFixed(4) : '—'}</span>
      </div>
      <FunnelStages stages={stages} />
      <div className="text-[10.5px] text-muted-foreground mt-2.5">{note}</div>
    </div>
  );
}

export function OverviewTab() {
  const [health, setHealth] = useState<HealthSummary | null>(null);
  const [healthErr, setHealthErr] = useState(false);
  const [funnel, setFunnel] = useState<Awaited<ReturnType<typeof queries.getSignalFunnelDetail>> | null>(null);
  const [funnelDate, setFunnelDate] = useState<string | null>(null);
  const [pending, setPending] = useState<BrainProposal[]>([]);
  const [topSignals, setTopSignals] = useState<Signal[]>([]);
  const [feed, setFeed] = useState<{ key: string; dot: string; text: string; time: string }[]>([]);
  const [lastAnalysis, setLastAnalysis] = useState<BrainAnalysisLog | null>(null);
  const [shadow, setShadow] = useState<{ closed: number; wr: number; avgR: number | null; unrealized: number } | null>(null);

  useEffect(() => {
    fetch('/api/health', { cache: 'no-store' })
      .then((r) => r.json())
      .then((j: HealthSummary) => setHealth(j))
      .catch(() => setHealthErr(true));

    queries.getBrainProposals('PENDING').then(({ data }) => setPending(data ?? []));
    queries.getBrainAnalysisLog(1).then(({ data }) => setLastAnalysis(data?.[0] ?? null));

    // Top Alerts — both books mixed into one feed, most recent first: real
    // intraday_alerts rows plus today's closed swing positions (which have
    // no alerts table of their own, so a close is the closest equivalent).
    const today = new Date().toISOString().slice(0, 10);
    Promise.all([
      queries.getIntradayAlerts(10),
      queries.getAllClosedPositionsByFramework('SWING'),
    ]).then(([intraday, swing]) => {
      const alertItems = (intraday.data ?? []).map((a) => ({
        key: `alert-${a.id}`, time: a.ts,
        dot: a.urgency === 'HIGH' ? 'bg-rose-500' : a.urgency === 'MEDIUM' ? 'bg-amber-500' : 'bg-muted-foreground',
        text: `${a.kind} ${a.symbol} — ${a.headline}`,
      }));
      const closeItems = (swing.data ?? [])
        .filter((p) => (p.exit_date ?? '').slice(0, 10) === today)
        .map((p) => ({
          key: `close-${p.id}`, time: p.closed_at ?? p.exit_date ?? '',
          dot: (p.realized_pnl ?? 0) >= 0 ? 'bg-emerald-500' : 'bg-rose-500',
          text: `CLOSED ${p.symbol} ${(p.pnl_pct ?? 0) >= 0 ? '+' : ''}${(p.pnl_pct ?? 0).toFixed(2)}%`
            + (p.r_multiple != null ? ` (${p.r_multiple >= 0 ? '+' : ''}${p.r_multiple.toFixed(2)}R)` : '')
            + (p.exit_reason ? ` · ${p.exit_reason}` : ''),
        }));
      setFeed([...alertItems, ...closeItems].sort((a, b) => b.time.localeCompare(a.time)).slice(0, 8));
    });

    queries.getTradePlans(5).then(({ data, date }) => {
      setTopSignals(data ?? []);
      if (date) {
        setFunnelDate(date);
        const tradeDate = new Date().toISOString().slice(0, 10);
        queries.getSignalFunnelDetail(date, tradeDate).then(setFunnel);
      }
    });

    // Shadow / paper book — pooled across both frameworks, same engines/exits,
    // no capital. Same pattern PositionsTab uses per-book, reduced to one strip.
    Promise.all([
      queries.getAllClosedPositionsByFramework('SWING'),
      queries.getAllClosedPositionsByFramework('INTRADAY'),
      queries.getOpenPositionsByFramework('SWING'),
      queries.getOpenPositionsByFramework('INTRADAY'),
    ]).then(([swClosed, idClosed, swOpen, idOpen]) => {
      const closed = [...(swClosed.data ?? []), ...(idClosed.data ?? [])].filter((p) => (p.mode ?? 'LIVE').toUpperCase() === 'PAPER');
      const open = [...(swOpen.data ?? []), ...(idOpen.data ?? [])].filter((p) => (p.mode ?? 'LIVE').toUpperCase() === 'PAPER');
      const wins = closed.filter((p) => (p.realized_pnl ?? 0) > 0).length;
      const rValues = closed.map((p) => p.r_multiple).filter((r): r is number => r != null);
      const unrealized = open.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);
      setShadow({
        closed: closed.length,
        wr: closed.length ? (wins / closed.length) * 100 : 0,
        avgR: rValues.length ? rValues.reduce((s, r) => s + r, 0) / rValues.length : null,
        unrealized,
      });
    });
  }, []);

  const nonOk = (health?.checks ?? []).filter((c) => c.severity !== 'OK' && c.severity !== 'INFO');

  return (
    <div className="space-y-4">
      {shadow && (
        <div className="panel px-4 py-2.5 flex items-center gap-4 text-xs">
          <span className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Shadow paper book</span>
          <span className="text-muted-foreground">measurement only, both frameworks — same engines, same exits, no capital</span>
          <span className="ml-auto flex items-center gap-1.5">
            <span className="font-mono font-semibold">{shadow.closed}</span> closed ·
            <span className="text-muted-foreground">{shadow.wr.toFixed(0)}% win rate</span> ·
            <span className={shadow.avgR != null && shadow.avgR >= 0 ? 'text-profit' : 'text-loss'}>
              {shadow.avgR != null ? `${shadow.avgR >= 0 ? '+' : ''}${shadow.avgR.toFixed(2)}R` : '—'}
            </span> · unrealized
            <span className={shadow.unrealized >= 0 ? 'text-profit' : 'text-loss'}>{formatCurrency(shadow.unrealized, { showSign: true })}</span>
          </span>
        </div>
      )}

      <Panel title="System Health" description="Same checks as /health — verdict first, drill down there"
        dataSource="api" endpoint="/api/health" isLoading={!health && !healthErr}>
        {healthErr ? (
          <div className="text-sm text-muted-foreground">Could not reach /api/health.</div>
        ) : !health ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : (
          <div>
            <div className="flex items-center gap-3 mb-3">
              <span className={`text-xs font-semibold px-2 py-1 rounded border ${VERDICT_STYLE[health.verdict]}`}>
                {health.verdict}
              </span>
              <span className="text-xs text-muted-foreground">
                {health.counts.ok} ok · {health.counts.warn} warn · {health.counts.block} blocked
              </span>
              <Link href="/health" className="text-xs text-primary hover:underline ml-auto">Full preflight →</Link>
            </div>
            {nonOk.length > 0 ? (
              <div className="space-y-1.5">
                {nonOk.slice(0, 4).map((c) => (
                  <div key={c.id} className="text-xs flex items-start gap-2">
                    <span className={`shrink-0 mt-1 h-1.5 w-1.5 rounded-full ${
                      c.severity === 'BLOCK' ? 'bg-rose-500' : 'bg-amber-500'}`} />
                    <span><span className="font-medium">{c.label}</span>
                      {c.detail && <span className="text-muted-foreground"> — {c.detail}</span>}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-muted-foreground">All checks passing.</div>
            )}
          </div>
        )}
      </Panel>

      <Panel title="Today's Signal Funnel"
        description={funnelDate ? `plans from ${formatDate(funnelDate, 'dd MMM yyyy')} — detected → gated → allocator-scored → taken, both books` : 'Loading…'}
        dataSource="supabase" isLoading={!funnel}>
        {!funnel ? (
          <div className="text-sm text-muted-foreground">Loading…</div>
        ) : (
          <>
            <FunnelBlock book="SWING" bar={funnel.swing.bar}
              stages={[['Watched', funnel.swing.watched], ['Allocator-scored', funnel.swing.scored], ['Taken', funnel.swing.taken]]}
              note={`-${Math.max(0, funnel.swing.watched - funnel.swing.scored)} not yet in the buy zone or not scored today`
                + ` · -${Math.max(0, funnel.swing.scored - funnel.swing.taken)} allocator declined or a slot held for a stronger proposal`
                + ` (no separate "in buy zone" count is persisted — the daemon evaluates zones live and never writes one)`} />
            <FunnelBlock book="INTRADAY" bar={funnel.intraday.bar}
              stages={[
                ['Scanned', funnel.intraday.scanned], ['Detected', funnel.intraday.detected],
                ['AI-cleared', funnel.intraday.aiCleared], ['Conviction floor', funnel.intraday.convictionFloor],
                ['Taken', funnel.intraday.taken],
              ]}
              note={`-${Math.max(0, funnel.intraday.detected - funnel.intraday.aiCleared)} blocked on structure/event or AI-vetoed`
                + ` · -${Math.max(0, funnel.intraday.aiCleared - funnel.intraday.convictionFloor)} below the conviction floor`
                + ` · -${Math.max(0, funnel.intraday.convictionFloor - funnel.intraday.taken)} rejected on cost, liquidity or depth`} />
          </>
        )}
      </Panel>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="Pending Learning Review" description="brain_proposals awaiting a decision — never auto-applied"
          dataSource="supabase" tableName="brain_proposals" isLoading={false}>
          {pending.length === 0 ? (
            <div className="text-sm text-muted-foreground">Nothing pending review.</div>
          ) : (
            <div className="space-y-2">
              {pending.slice(0, 5).map((p) => (
                <div key={p.id} className="text-xs border-b border-border/30 pb-2 last:border-0 last:pb-0">
                  <div className="flex items-center gap-2">
                    <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded uppercase ${PROPOSAL_TAG[p.proposal_type] ?? 'bg-muted text-muted-foreground'}`}>
                      {PROPOSAL_LABEL[p.proposal_type] ?? p.proposal_type}
                    </span>
                    <span className="text-muted-foreground">· {p.target_key}</span>
                    {p.high_impact && <span className="text-[9px] px-1 rounded bg-amber-500/20 text-amber-400">high impact</span>}
                  </div>
                  {p.rationale && <div className="text-muted-foreground mt-0.5 line-clamp-2">{p.rationale}</div>}
                </div>
              ))}
              {pending.length > 5 && <div className="text-[10px] text-muted-foreground">+{pending.length - 5} more — see Brain Engine</div>}
            </div>
          )}
          {lastAnalysis && (
            <div className="text-[11px] text-muted-foreground mt-3 pt-2 border-t border-border/30">
              Last analysis: {formatDate(lastAnalysis.run_date, 'EEE dd MMM yyyy')} · {lastAnalysis.signals_analyzed} signals analyzed
              · {lastAnalysis.proposals_generated} proposal{lastAnalysis.proposals_generated === 1 ? '' : 's'} generated
            </div>
          )}
        </Panel>

        <Panel title="Top Alerts" description="Live feed, both books" dataSource="supabase" isLoading={false}>
          {feed.length === 0 ? (
            <div className="text-sm text-muted-foreground">No alerts or closes yet today.</div>
          ) : (
            <div className="space-y-2">
              {feed.map((f) => (
                <div key={f.key} className="text-xs flex items-center gap-2 border-b border-border/30 pb-2 last:border-0 last:pb-0">
                  <span className={`h-2 w-2 rounded-full shrink-0 ${f.dot}`} />
                  <span className="flex-1">{f.text}</span>
                  <span className="text-muted-foreground shrink-0">{f.time?.slice(11, 16) || f.time}</span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {topSignals.length > 0 && (
        <Panel title="Today's Top Swing Signals" description="Highest-ranked plans from the latest pipeline run"
          dataSource="supabase" tableName="signal_output_daily" isLoading={false}>
          <div className="space-y-1.5">
            {topSignals.map((s) => (
              <div key={`${s.symbol}-${s.date}`} className="text-xs flex items-center gap-2">
                <span className="font-mono font-semibold w-20">{s.symbol}</span>
                <span className="text-muted-foreground w-16">{s.strategy}</span>
                <span className="text-muted-foreground">{s.ai_conviction ?? '—'}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}

export default OverviewTab;
