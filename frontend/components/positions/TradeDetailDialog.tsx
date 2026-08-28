'use client';

// Trade Detail — click-through from any open/closed position row.
//
// Two questions this answers with numbers that already exist somewhere in
// the system, never a recomputation of them:
//   1. "Why did the allocator take this?" — the exact edge/hurdle row
//      allocation_decisions recorded, so this can never disagree with the
//      live gate that actually admitted the trade.
//   2. "What does this trade's own setup look like next to this engine's
//      resolved winners and losers?" — swing and intraday read genuinely
//      different raw material here (stock_data_daily indicators vs
//      intraday_setups.meta), because that is what each side actually
//      stores. There is no shared factor table to fake symmetry with.
//
// Either panel can come back empty — a pre-allocator trade, an engine with
// too little resolved history — and says so plainly rather than showing a
// zero that looks like a real number.

import { useEffect, useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { formatCurrency, formatDate } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import type { OpenPosition, ClosedPosition } from '@/types/database';

export type TradeDetailTarget =
  | { kind: 'open'; symbol: string }
  | { kind: 'closed'; id: number }
  | null;

const MIN_SEGMENT = 15; // tools/feature_edge_study.py's own bar for a trustworthy mean

function mean(vals: (number | null | undefined)[]): number | null {
  const v = vals.filter((x): x is number => x != null && Number.isFinite(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

function pct(n: number | null): string {
  return n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
}

// Bar position: value plotted between a "loser" and "winner" endpoint,
// whichever side is numerically larger. Handles both ascending factors
// (higher = better) and descending ones (lower = better) the same way.
function barPct(value: number, loserAvg: number, winnerAvg: number): number {
  if (loserAvg === winnerAvg) return 50;
  const p = ((loserAvg - value) / (loserAvg - winnerAvg)) * 100;
  return Math.max(0, Math.min(100, p));
}

function FactorBar({ label, value, loserAvg, winnerAvg, unit = '', n }: {
  label: string; value: number | null; loserAvg: number | null; winnerAvg: number | null;
  unit?: string; n: { winners: number; losers: number };
}) {
  const enough = n.winners >= MIN_SEGMENT && n.losers >= MIN_SEGMENT;
  if (!enough) {
    return (
      <div className="py-2.5 border-t border-border/30 first:border-t-0">
        <div className="text-xs font-medium">{label}</div>
        <div className="text-[11px] text-muted-foreground mt-1">
          Not enough resolved history yet (winners n={n.winners}, losers n={n.losers} — needs {MIN_SEGMENT} each)
        </div>
      </div>
    );
  }
  if (value == null || loserAvg == null || winnerAvg == null) {
    return (
      <div className="py-2.5 border-t border-border/30 first:border-t-0">
        <div className="text-xs font-medium">{label}</div>
        <div className="text-[11px] text-muted-foreground mt-1">No value recorded for this trade</div>
      </div>
    );
  }
  const p = barPct(value, loserAvg, winnerAvg);
  return (
    <div className="py-2.5 border-t border-border/30 first:border-t-0">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium">{label}</span>
        <span className="text-[10px] text-muted-foreground">n={n.winners + n.losers} resolved</span>
      </div>
      <div className="relative h-1.5 rounded-full mt-2"
        style={{ background: 'linear-gradient(90deg, rgba(239,68,68,.35), rgba(148,163,184,.25), rgba(34,197,94,.4))' }}>
        <div className="absolute -top-[7px]" style={{ left: `${p}%`, transform: 'translateX(-50%)' }}>
          <div className="h-3 w-3 rounded-full bg-foreground border-2 border-background mx-auto" />
          <div className="text-[9px] font-mono font-semibold mt-0.5 whitespace-nowrap text-center">
            {value.toFixed(2)}{unit}
          </div>
        </div>
      </div>
      <div className="flex justify-between text-[10px] text-muted-foreground mt-3">
        <span>Losers avg {loserAvg.toFixed(2)}{unit}</span>
        <span>Winners avg {winnerAvg.toFixed(2)}{unit}</span>
      </div>
    </div>
  );
}

interface ViewModel {
  symbol: string; strategy: string; framework: string; mode: string; direction: string;
  entryDate: string; entryPrice: number; exitOrCmpLabel: string; exitOrCmp: number | null;
  qty: number | null; stop: number | null; pnl: number | null; r: number | null;
  status: 'open' | 'win' | 'loss';
}

export function TradeDetailDialog({ target, onOpenChange }: {
  target: TradeDetailTarget; onOpenChange: (open: boolean) => void;
}) {
  const [vm, setVm] = useState<ViewModel | null>(null);
  const [alloc, setAlloc] = useState<{
    edge: number | null; e_r: number | null; cost_r: number | null; hurdle: number | null;
    hurdle_inputs: { cold_start?: boolean; base?: number | null; n?: number } | null;
    regime_bucket: string | null; verdict: string;
  } | 'none' | { mismatch: true; mostRecentVerdict: string; count: number } | null>(null);
  const [factors, setFactors] = useState<{
    rows: { label: string; value: number | null; loserAvg: number | null; winnerAvg: number | null; unit?: string; n: { winners: number; losers: number } }[];
  } | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!target) { setVm(null); setAlloc(null); setFactors(null); return; }
    let cancelled = false;
    setLoading(true);
    (async () => {
      // 1. The position itself.
      let position: OpenPosition | ClosedPosition | null = null;
      let isOpen = false;
      if (target.kind === 'open') {
        const { data } = await queries.getOpenPositionBySymbol(target.symbol);
        position = data?.[0] ?? null;
        isOpen = true;
      } else {
        const { data } = await queries.getClosedPositionById(target.id);
        position = data?.[0] ?? null;
      }
      if (!position || cancelled) { if (!cancelled) { setVm(null); setLoading(false); } return; }

      const framework = (position.framework ?? 'SWING').toUpperCase();
      const direction = (position.direction ?? 'LONG').toUpperCase();
      const entryDate = position.entry_date;
      const built: ViewModel = isOpen
        ? (() => {
            const p = position as OpenPosition;
            return {
              symbol: p.symbol, strategy: p.strategy, framework, mode: (p.mode ?? 'LIVE').toUpperCase(),
              direction, entryDate, entryPrice: p.entry_price,
              exitOrCmpLabel: 'CMP', exitOrCmp: p.current_price,
              qty: p.current_qty ?? p.actual_qty ?? null, stop: p.active_sl ?? p.planned_stop ?? null,
              pnl: p.unrealized_pnl, r: p.r_multiple_current ?? null, status: 'open',
            };
          })()
        : (() => {
            const p = position as ClosedPosition;
            return {
              symbol: p.symbol, strategy: p.strategy, framework, mode: (p.mode ?? 'LIVE').toUpperCase(),
              direction, entryDate, entryPrice: p.entry_price,
              exitOrCmpLabel: 'Exit', exitOrCmp: p.exit_price,
              qty: p.actual_qty ?? null, stop: p.planned_stop_at_entry ?? null,
              pnl: p.realized_pnl, r: p.r_multiple ?? null,
              status: (p.realized_pnl ?? 0) > 0 ? 'win' : 'loss',
            };
          })();
      if (cancelled) return;
      setVm(built);

      // 2. What the allocator recorded for this trade. A DECLINE/DEFER row
      // for the same (symbol, day, book) is not "why this was taken" — it is
      // a different evaluation that didn't lead to this position, and
      // showing its numbers under that heading would misrepresent what
      // happened. Only a genuine TAKE row is presented that way; anything
      // else is surfaced as an honest mismatch rather than silently guessed.
      const { data: decisions } = await queries.getAllocationDecisionFor(built.symbol, entryDate, framework);
      if (cancelled) return;
      const take = (decisions ?? []).find((d) => d.verdict === 'TAKE') ?? null;
      if (take) {
        setAlloc({
          edge: take.edge, e_r: take.e_r, cost_r: take.cost_r, hurdle: take.hurdle,
          hurdle_inputs: take.hurdle_inputs, regime_bucket: take.regime_bucket, verdict: take.verdict,
        });
      } else if (decisions && decisions.length) {
        setAlloc({ mismatch: true, mostRecentVerdict: decisions[0].verdict, count: decisions.length });
      } else {
        setAlloc('none');
      }

      // 3. Factor snapshot — different raw material per book, same question.
      if (framework === 'INTRADAY') {
        const [profile, ownSetup] = await Promise.all([
          queries.getIntradayEngineFactorProfile(built.strategy),
          queries.getMatchingIntradaySetup(built.symbol, entryDate, built.strategy),
        ]);
        if (cancelled) return;
        const own = ownSetup.data?.[0] ?? null;
        const ownConf = own?.confidence ?? null;
        const ownMeta = (own?.meta && typeof own.meta === 'object') ? own.meta as Record<string, unknown> : {};
        const ownVol = typeof ownMeta.volume_ratio === 'number' ? ownMeta.volume_ratio : null;
        const ownAtr = typeof ownMeta.atr_pct_daily === 'number' ? ownMeta.atr_pct_daily : null;
        const n = { winners: profile.winners.length, losers: profile.losers.length };
        const metaNum = (r: { meta: Record<string, unknown> }, k: string) =>
          typeof r.meta[k] === 'number' ? (r.meta[k] as number) : null;
        setFactors({ rows: [
          { label: 'Confidence at detection', value: ownConf,
            loserAvg: mean(profile.losers.map((r) => r.confidence)),
            winnerAvg: mean(profile.winners.map((r) => r.confidence)), n },
          { label: 'Volume ratio vs 20-day avg', value: ownVol, unit: 'x',
            loserAvg: mean(profile.losers.map((r) => metaNum(r, 'volume_ratio'))),
            winnerAvg: mean(profile.winners.map((r) => metaNum(r, 'volume_ratio'))), n },
          { label: 'Daily ATR %', value: ownAtr, unit: '%',
            loserAvg: mean(profile.losers.map((r) => metaNum(r, 'atr_pct_daily'))),
            winnerAvg: mean(profile.winners.map((r) => metaNum(r, 'atr_pct_daily'))), n },
        ] });
      } else {
        const [profile, ownIndicator] = await Promise.all([
          queries.getSwingEngineFactorProfile(built.strategy),
          queries.getEntryDayIndicators(built.symbol, entryDate),
        ]);
        if (cancelled) return;
        const ownRow = ownIndicator.data?.[0] ?? null;
        const ownDistFromHigh = ownRow?.high_52w
          ? ((built.entryPrice - ownRow.high_52w) / ownRow.high_52w) * 100 : null;
        const n = { winners: profile.winners.length, losers: profile.losers.length };
        const distFor = (r: { symbol: string; entry_date: string; }, entryPrice: number) => {
          const ind = profile.indicators.get(`${r.symbol}|${r.entry_date}`);
          return ind?.high_52w ? ((entryPrice - ind.high_52w) / ind.high_52w) * 100 : null;
        };
        const rsiFor = (r: { symbol: string; entry_date: string }) =>
          profile.indicators.get(`${r.symbol}|${r.entry_date}`)?.rsi_daily ?? null;
        const volFor = (r: { symbol: string; entry_date: string }) =>
          profile.indicators.get(`${r.symbol}|${r.entry_date}`)?.vol_ratio ?? null;
        setFactors({ rows: [
          { label: 'Sector rank at entry (lower = stronger)', value: position!.sector_rank_at_entry ?? null,
            loserAvg: mean(profile.losers.map((r) => r.sector_rank_at_entry)),
            winnerAvg: mean(profile.winners.map((r) => r.sector_rank_at_entry)), n },
          { label: 'RSI (daily) at entry', value: ownRow?.rsi_daily ?? null,
            loserAvg: mean(profile.losers.map(rsiFor)),
            winnerAvg: mean(profile.winners.map(rsiFor)), n },
          { label: 'Volume ratio vs 20-day avg', value: ownRow?.vol_ratio ?? null, unit: 'x',
            loserAvg: mean(profile.losers.map(volFor)),
            winnerAvg: mean(profile.winners.map(volFor)), n },
          { label: 'Distance from 52-week high at entry', value: ownDistFromHigh, unit: '%',
            loserAvg: mean(profile.losers.map((r) => distFor(r, position!.entry_price))),
            winnerAvg: mean(profile.winners.map((r) => distFor(r, position!.entry_price))), n },
        ] });
      }
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [target]);

  const open = target != null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl max-h-[85vh] overflow-y-auto">
        {!vm ? (
          <>
            <DialogHeader><DialogTitle>Trade detail</DialogTitle></DialogHeader>
            <div className="text-sm text-muted-foreground py-6 text-center">
              {loading ? 'Loading…' : 'Position not found.'}
            </div>
          </>
        ) : (
          <>
            <DialogHeader>
              <div className="flex items-center gap-2 flex-wrap">
                <DialogTitle className="font-mono">{vm.symbol}</DialogTitle>
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                  vm.framework === 'INTRADAY' ? 'badge-intraday' : 'badge-swing'}`}>
                  {vm.framework === 'INTRADAY' ? 'INTRADAY' : 'SWING'}
                </span>
                <span className="text-xs px-1.5 py-0.5 rounded bg-muted text-muted-foreground">{vm.strategy}</span>
                {vm.mode === 'PAPER' && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-400 font-medium">PAPER</span>
                )}
                <span className="text-xs text-muted-foreground">{vm.direction === 'SHORT' ? '▼ SHORT' : '▲ LONG'}</span>
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                  vm.status === 'open' ? 'bg-muted text-muted-foreground'
                    : vm.status === 'win' ? 'bg-profit/20 text-profit' : 'bg-loss/20 text-loss'}`}>
                  {vm.status === 'open' ? 'OPEN' : vm.status === 'win' ? 'CLOSED · WIN' : 'CLOSED · LOSS'}
                </span>
              </div>
              <DialogDescription>Entered {formatDate(vm.entryDate, 'dd MMM yyyy')}</DialogDescription>
            </DialogHeader>

            <div className="grid grid-cols-3 sm:grid-cols-5 gap-3 text-xs">
              <div><div className="text-muted-foreground">Entry</div><div className="font-mono mt-0.5">₹{vm.entryPrice.toFixed(2)}</div></div>
              <div><div className="text-muted-foreground">{vm.exitOrCmpLabel}</div><div className="font-mono mt-0.5">{vm.exitOrCmp != null ? `₹${vm.exitOrCmp.toFixed(2)}` : '—'}</div></div>
              <div><div className="text-muted-foreground">P&amp;L</div><div className={`font-mono mt-0.5 font-semibold ${(vm.pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>{formatCurrency(vm.pnl, { showSign: true })}</div></div>
              <div><div className="text-muted-foreground">R</div><div className={`font-mono mt-0.5 font-semibold ${(vm.r ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>{vm.r != null ? `${vm.r >= 0 ? '+' : ''}${vm.r.toFixed(2)}` : '—'}</div></div>
              <div><div className="text-muted-foreground">Qty · Stop</div><div className="font-mono mt-0.5">{vm.qty ?? '—'} · {vm.stop != null ? `₹${vm.stop.toFixed(2)}` : '—'}</div></div>
            </div>

            <div className="panel">
              <div className="px-4 py-3 border-b border-border">
                <h3 className="text-sm font-medium">Why the allocator took this</h3>
                <p className="text-xs text-muted-foreground">allocation_decisions — the row the allocator itself recorded</p>
              </div>
              <div className="p-4">
                {alloc === null ? (
                  <div className="text-xs text-muted-foreground">Loading…</div>
                ) : alloc === 'none' ? (
                  <div className="text-xs text-muted-foreground">
                    No allocator record found for this trade — likely entered before the allocator was wired in
                    (migration 044), or entered manually outside its review.
                  </div>
                ) : 'mismatch' in alloc ? (
                  <div className="text-xs text-muted-foreground">
                    {alloc.count} allocator record{alloc.count > 1 ? 's' : ''} {alloc.count > 1 ? 'exist' : 'exists'} for
                    this symbol on this day and book, but none carry a TAKE verdict — the most recent is{' '}
                    {alloc.mostRecentVerdict}. This position was likely entered through a scoring pass outside what
                    matched here, not shown as a guess.
                  </div>
                ) : (
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                    <div><div className="text-muted-foreground">Edge</div><div className={`font-mono mt-0.5 font-semibold ${(alloc.edge ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>{pct(alloc.edge)}R</div></div>
                    <div><div className="text-muted-foreground">Hurdle (the bar)</div><div className="font-mono mt-0.5">{alloc.hurdle != null ? alloc.hurdle.toFixed(4) : '—'}</div></div>
                    <div><div className="text-muted-foreground">Cost R</div><div className="font-mono mt-0.5">{alloc.cost_r != null ? alloc.cost_r.toFixed(4) : '—'}</div></div>
                    <div><div className="text-muted-foreground">Prior mean R (e_r)</div><div className="font-mono mt-0.5">{alloc.e_r != null ? alloc.e_r.toFixed(4) : '—'}</div></div>
                    <div><div className="text-muted-foreground">Regime bucket</div><div className="mt-0.5">{alloc.regime_bucket ?? '—'}</div></div>
                    <div><div className="text-muted-foreground">Bar base</div><div className="font-mono mt-0.5">{alloc.hurdle_inputs?.base != null ? alloc.hurdle_inputs.base.toFixed(4) : '—'}</div></div>
                    <div><div className="text-muted-foreground">Bucket sample (n)</div><div className="font-mono mt-0.5">{alloc.hurdle_inputs?.n ?? '—'}</div></div>
                    <div><div className="text-muted-foreground">Verdict</div><div className="mt-0.5">{alloc.verdict}{alloc.hurdle_inputs?.cold_start ? ' · cold start' : ''}</div></div>
                  </div>
                )}
              </div>
            </div>

            <div className="panel">
              <div className="px-4 py-3 border-b border-border">
                <h3 className="text-sm font-medium">This trade against {vm.strategy}&apos;s own resolved winners/losers</h3>
                <p className="text-xs text-muted-foreground">
                  {vm.framework === 'INTRADAY'
                    ? 'same population tools/feature_edge_study.py studies — cost_verdict TAKEN, outcome resolved'
                    : 'closed_positions for this strategy, joined to stock_data_daily at entry_date'}
                </p>
              </div>
              <div className="px-4">
                {!factors ? (
                  <div className="text-xs text-muted-foreground py-3">Loading…</div>
                ) : (
                  factors.rows.map((r) => <FactorBar key={r.label} {...r} />)
                )}
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
