'use client';

// Engines — matches Engines.dc.html exactly: filter chips, Swing/Intraday
// sections, a card grid with a sparkline, a 4-stat row (setups detected →
// taken → hit rate → avg net), and entry/exit condition lines. Replaces the
// old Performance tab's KPI/chart/trade-log layout entirely — that content
// wasn't in the Canvas design, and the closed-trade history it showed is
// already covered by Positions & P&L's own closed-trades table.

import { useEffect, useState } from 'react';
import { queries } from '@/lib/supabase';
import { EngineDetailDialog } from '@/components/tabs/performance/EngineDetailDialog';
import type { EngineStats } from '@/types/database';

interface EngineMeta { label: string | null; description: string | null; lifecycle: string | null }
interface CardData {
  code: string; framework: 'SWING' | 'INTRADAY'; meta: EngineMeta | undefined;
  setups: number; taken: number; hit: number | null; avgNet: number | null;
  sparkline: number[]; trendPp: number | null;
}

// Rolling hit-rate over a trailing window, computed from the last N resolved
// trades in chronological order — a real trend line, not a fabricated one.
// "Session" in the Canvas copy becomes "trade" here since that's the unit
// this data actually resolves in; the shape of the trend is what matters.
function rollingHitRate(results: boolean[], window = 5, points = 14): number[] {
  const tail = results.slice(-points - window + 1);
  const out: number[] = [];
  for (let i = window - 1; i < tail.length; i++) {
    const slice = tail.slice(Math.max(0, i - window + 1), i + 1);
    out.push(slice.filter(Boolean).length / slice.length);
  }
  return out.length ? out : results.length ? [results.filter(Boolean).length / results.length] : [];
}

function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) {
    return <div className="text-[9px] text-muted-foreground mt-2.5">not enough resolved history for a trend</div>;
  }
  const w = 110, h = 28;
  const step = w / (points.length - 1);
  const trendPp = (points[points.length - 1] - points[0]) * 100;
  const up = trendPp >= 0;
  return (
    <div className="flex items-center justify-between mt-2.5">
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`}>
        <polyline points={points.map((p, i) => `${(i * step).toFixed(1)},${(h - p * h).toFixed(1)}`).join(' ')}
          fill="none" stroke={up ? '#22c55e' : '#ef4444'} strokeWidth="1.6" />
      </svg>
      <span className={`text-[10px] font-bold ${up ? 'text-profit' : 'text-loss'}`}>
        {up ? '▲' : '▼'} {up ? '+' : ''}{trendPp.toFixed(1)}pt
      </span>
    </div>
  );
}

function EngineCardTile({ c, exitText, onSelect }: { c: CardData; exitText: string; onSelect: (e: EngineStats) => void }) {
  const borderColor = c.framework === 'SWING' ? 'border-t-swing' : 'border-t-intraday';
  const lifecycleClass = c.meta?.lifecycle === 'SHADOW' ? 'bg-amber-500/20 text-amber-400'
    : c.meta?.lifecycle === 'RETIRED' ? 'bg-muted text-muted-foreground'
    : 'bg-emerald-500/20 text-emerald-400';
  const openDetail = () => onSelect({
    engine_name: c.code, total_signals: c.taken, executed_signals: c.taken,
    win_count: Math.round((c.hit ?? 0) * c.taken), loss_count: c.taken - Math.round((c.hit ?? 0) * c.taken),
    win_rate: c.hit ?? 0, avg_pnl_percent: c.avgNet ?? 0, total_pnl: 0, last_signal_date: '',
  });
  return (
    <div className={`panel border-t-2 ${borderColor} p-3.5 cursor-pointer hover:bg-panel-hover/50`}
      onClick={openDetail} title="View factor breakdown — what separates winners from losers">
      <div className="flex items-start justify-between">
        <div>
          <span className="font-mono font-bold text-[15px]">{c.code}</span>
          <div className="text-[10px] text-muted-foreground mt-0.5">{c.meta?.label ?? c.code}</div>
        </div>
        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded uppercase ${lifecycleClass}`}>
          {c.meta?.lifecycle ?? 'ACTIVE'}
        </span>
      </div>

      {c.meta?.description && (
        <div className="text-[11px] text-muted-foreground mt-2 leading-snug min-h-[30px] line-clamp-2">
          {c.meta.description}
        </div>
      )}

      <div className="grid grid-cols-4 gap-1 pt-2.5 mt-2.5 border-t border-border">
        <div><div className="text-[8.5px] text-muted-foreground uppercase">Setups</div><div className="text-xs font-bold mt-0.5">{c.setups}</div></div>
        <div><div className="text-[8.5px] text-muted-foreground uppercase">Taken</div><div className="text-xs font-bold mt-0.5">{c.taken}</div></div>
        <div><div className="text-[8.5px] text-muted-foreground uppercase">Hit</div><div className="text-xs font-bold mt-0.5">{c.hit != null ? `${(c.hit * 100).toFixed(0)}%` : '—'}</div></div>
        <div><div className="text-[8.5px] text-muted-foreground uppercase">Avg net</div>
          <div className={`text-xs font-bold mt-0.5 ${(c.avgNet ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
            {c.avgNet != null ? `${c.avgNet >= 0 ? '+' : ''}${c.avgNet.toFixed(2)}%` : '—'}
          </div>
        </div>
      </div>

      <Sparkline points={c.sparkline} />

      {c.meta?.description && (
        <div className="text-[9.5px] leading-snug text-muted-foreground mt-2 pt-2 border-t border-border">
          <b className="text-secondary-foreground mr-1">Entry</b>{c.meta.description}
        </div>
      )}
      <div className="text-[9.5px] leading-snug text-muted-foreground mt-1">
        <b className="text-secondary-foreground mr-1">Exit</b>{exitText}
      </div>
    </div>
  );
}

export function PerformanceTab() {
  const [cards, setCards] = useState<CardData[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<'ALL' | 'SWING' | 'INTRADAY'>('ALL');
  const [selectedEngine, setSelectedEngine] = useState<EngineStats | null>(null);
  const [swingExitText, setSwingExitText] = useState('swing ladder — loading…');

  useEffect(() => {
    async function load() {
      setLoading(true);
      const [sc, isc, swingStats, intradayStats, exitPolicy] = await Promise.all([
        queries.getStrategyConfig(),
        queries.getIntradayStrategyConfig(),
        queries.getSwingEngineGridStats(),
        queries.getIntradayEngineGridStats(),
        queries.getExitPolicy(),
      ]);
      setSwingExitText(
        `swing ladder — book ${exitPolicy.exit_partial_book_pct}%@${exitPolicy.exit_partial_book_r}R, `
        + `trail>${exitPolicy.exit_trail_after_r}R, target ${exitPolicy.exit_target_r}R`
      );
      const metaByCode = new Map<string, EngineMeta>();
      for (const r of [...(sc.data ?? []), ...(isc.data ?? [])]) {
        metaByCode.set(r.strategy, { label: r.label, description: r.description, lifecycle: r.lifecycle });
      }

      const built: CardData[] = [];
      for (const [code, count] of swingStats.setups) {
        const closed = swingStats.closedByStrategy.get(code) ?? [];
        const wins = closed.filter((r) => (r.realized_pnl ?? 0) > 0).length;
        const results = closed.map((r) => (r.realized_pnl ?? 0) > 0);
        const avgNet = closed.length ? closed.reduce((s, r) => s + (r.pnl_pct ?? 0), 0) / closed.length : null;
        built.push({
          code, framework: 'SWING', meta: metaByCode.get(code),
          setups: count, taken: closed.length + (swingStats.takenOpen.get(code) ?? 0),
          hit: closed.length ? wins / closed.length : null, avgNet,
          sparkline: rollingHitRate(results), trendPp: null,
        });
      }
      for (const [code, count] of intradayStats.setups) {
        const taken = intradayStats.takenByStrategy.get(code) ?? [];
        const resolved = taken.filter((r) => r.outcome === 'TARGET' || r.outcome === 'STOP');
        const wins = resolved.filter((r) => r.outcome === 'TARGET').length;
        const results = resolved.map((r) => r.outcome === 'TARGET');
        const avgNet = resolved.length ? resolved.reduce((s, r) => s + (r.outcome_pct ?? 0), 0) / resolved.length : null;
        built.push({
          code, framework: 'INTRADAY', meta: metaByCode.get(code),
          setups: count, taken: taken.length,
          hit: resolved.length ? wins / resolved.length : null, avgNet,
          sparkline: rollingHitRate(results), trendPp: null,
        });
      }
      built.sort((a, b) => a.code.localeCompare(b.code));
      setCards(built);
    }
    load().catch((e) => console.error('Engines load failed:', e)).finally(() => setLoading(false));
  }, []);

  const swingCards = cards.filter((c) => c.framework === 'SWING');
  const intradayCards = cards.filter((c) => c.framework === 'INTRADAY');
  const visible = filter === 'ALL' ? cards : filter === 'SWING' ? swingCards : intradayCards;

  return (
    <div>
      <div className="mb-1">
        <h1 className="text-[19px] font-semibold">Engines</h1>
        <p className="text-xs text-muted-foreground mt-0.5">
          Detected → taken → outcome, both books — sparkline is rolling hit-rate trend, last resolved trades
        </p>
      </div>

      <div className="flex gap-2 my-3.5 mb-5">
        {([['ALL', `All ${cards.length}`], ['SWING', `Swing ${swingCards.length}`], ['INTRADAY', `Intraday ${intradayCards.length}`]] as const).map(([id, label]) => (
          <button key={id} onClick={() => setFilter(id)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              filter === id ? 'badge-swing' : 'border-border text-muted-foreground hover:text-foreground'}`}>
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
          {Array.from({ length: 8 }).map((_, i) => <div key={i} className="h-48 rounded-xl border border-border bg-panel-hover/40 animate-pulse" />)}
        </div>
      ) : (
        <>
          {(filter === 'ALL' || filter === 'SWING') && swingCards.length > 0 && (
            <>
              <div className="flex items-center gap-2.5 my-5 mt-6 first:mt-0">
                <span className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Swing · {swingCards.length} engines</span>
                <span className="flex-1 h-px bg-border" />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
                {swingCards.map((c) => <EngineCardTile key={c.code} c={c} exitText={swingExitText} onSelect={setSelectedEngine} />)}
              </div>
            </>
          )}
          {(filter === 'ALL' || filter === 'INTRADAY') && intradayCards.length > 0 && (
            <>
              <div className="flex items-center gap-2.5 my-5">
                <span className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Intraday · {intradayCards.length} engines</span>
                <span className="flex-1 h-px bg-border" />
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
                {intradayCards.map((c) => (
                  <EngineCardTile key={c.code} c={c}
                    exitText="setup target, minute-based time stop, invalidation check, square-off"
                    onSelect={setSelectedEngine} />
                ))}
              </div>
            </>
          )}
          {visible.length === 0 && (
            <div className="text-sm text-muted-foreground py-8 text-center">No engines registered yet.</div>
          )}
        </>
      )}

      <EngineDetailDialog engine={selectedEngine} onOpenChange={(o) => { if (!o) setSelectedEngine(null); }} />
    </div>
  );
}

export default PerformanceTab;
