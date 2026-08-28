'use client';

// Engine Detail — "what do this engine's winners have in common that its
// losers don't." Reuses the exact same factor-profile queries the Trade
// Detail dialog uses (lib/supabase.ts: getSwingEngineFactorProfile /
// getIntradayEngineFactorProfile) — same population, same MIN_SEGMENT bar,
// so a number here can never disagree with what a single trade's own
// drill-down shows for the same engine.

import { useEffect, useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { formatCurrency, formatPercent } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import type { EngineStats } from '@/types/database';

// The documented intraday engine codes (docs/0_SYSTEM_BLUEPRINT.md) — every
// other engine_name is swing. Not inferred from data because framework
// isn't a column on performance_metrics.engine_stats.
const INTRADAY_ENGINES = new Set(['ORB', 'GAP', 'PDL', 'VCE', 'PBK', 'VWR', 'RNG', 'SDN']);

const MIN_SEGMENT = 15;

function mean(vals: (number | null | undefined)[]): number | null {
  const v = vals.filter((x): x is number => x != null && Number.isFinite(x));
  return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
}

export function EngineDetailDialog({ engine, onOpenChange }: {
  engine: EngineStats | null; onOpenChange: (open: boolean) => void;
}) {
  const [rows, setRows] = useState<{ label: string; loserAvg: number | null; winnerAvg: number | null; unit?: string; n: { winners: number; losers: number } }[] | null>(null);
  const [framework, setFramework] = useState<'SWING' | 'INTRADAY' | null>(null);

  useEffect(() => {
    if (!engine) { setRows(null); return; }
    const fw = INTRADAY_ENGINES.has(engine.engine_name.toUpperCase()) ? 'INTRADAY' : 'SWING';
    setFramework(fw);
    let cancelled = false;

    if (fw === 'INTRADAY') {
      queries.getIntradayEngineFactorProfile(engine.engine_name).then((profile) => {
        if (cancelled) return;
        const n = { winners: profile.winners.length, losers: profile.losers.length };
        const metaNum = (r: { meta: Record<string, unknown> }, k: string) =>
          typeof r.meta[k] === 'number' ? (r.meta[k] as number) : null;
        setRows([
          { label: 'Confidence at detection',
            loserAvg: mean(profile.losers.map((r) => r.confidence)),
            winnerAvg: mean(profile.winners.map((r) => r.confidence)), n },
          { label: 'Volume ratio vs 20-day avg', unit: 'x',
            loserAvg: mean(profile.losers.map((r) => metaNum(r, 'volume_ratio'))),
            winnerAvg: mean(profile.winners.map((r) => metaNum(r, 'volume_ratio'))), n },
          { label: 'Daily ATR %', unit: '%',
            loserAvg: mean(profile.losers.map((r) => metaNum(r, 'atr_pct_daily'))),
            winnerAvg: mean(profile.winners.map((r) => metaNum(r, 'atr_pct_daily'))), n },
        ]);
      });
    } else {
      queries.getSwingEngineFactorProfile(engine.engine_name).then((profile) => {
        if (cancelled) return;
        const n = { winners: profile.winners.length, losers: profile.losers.length };
        const distFor = (r: { symbol: string; entry_date: string }, entryPrice: number) => {
          const ind = profile.indicators.get(`${r.symbol}|${r.entry_date}`);
          return ind?.high_52w ? ((entryPrice - ind.high_52w) / ind.high_52w) * 100 : null;
        };
        const rsiFor = (r: { symbol: string; entry_date: string }) =>
          profile.indicators.get(`${r.symbol}|${r.entry_date}`)?.rsi_daily ?? null;
        const volFor = (r: { symbol: string; entry_date: string }) =>
          profile.indicators.get(`${r.symbol}|${r.entry_date}`)?.vol_ratio ?? null;
        setRows([
          { label: 'Sector rank at entry (lower = stronger)',
            loserAvg: mean(profile.losers.map((r) => r.sector_rank_at_entry)),
            winnerAvg: mean(profile.winners.map((r) => r.sector_rank_at_entry)), n },
          { label: 'RSI (daily) at entry',
            loserAvg: mean(profile.losers.map(rsiFor)),
            winnerAvg: mean(profile.winners.map(rsiFor)), n },
          { label: 'Volume ratio vs 20-day avg', unit: 'x',
            loserAvg: mean(profile.losers.map(volFor)),
            winnerAvg: mean(profile.winners.map(volFor)), n },
          { label: 'Distance from 52-week high at entry', unit: '%',
            loserAvg: mean(profile.losers.map((r) => r.entry_price != null ? distFor(r, r.entry_price) : null)),
            winnerAvg: mean(profile.winners.map((r) => r.entry_price != null ? distFor(r, r.entry_price) : null)), n },
        ]);
      });
    }
    return () => { cancelled = true; };
  }, [engine]);

  return (
    <Dialog open={!!engine} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl max-h-[85vh] overflow-y-auto">
        {engine && (
          <>
            <DialogHeader>
              <div className="flex items-center gap-2">
                <DialogTitle className="font-mono">{engine.engine_name}</DialogTitle>
                {framework && (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                    framework === 'INTRADAY' ? 'badge-intraday' : 'badge-swing'}`}>
                    {framework}
                  </span>
                )}
              </div>
              <DialogDescription>
                {engine.total_signals} signals · {formatPercent(engine.win_rate * 100, { showSign: false })} win rate ·{' '}
                {formatCurrency(engine.total_pnl, { compact: true, showSign: true })} total
              </DialogDescription>
            </DialogHeader>

            <div className="panel">
              <div className="px-4 py-3 border-b border-border">
                <h3 className="text-sm font-medium">What separates this engine&apos;s winners from its losers</h3>
                <p className="text-xs text-muted-foreground">
                  {framework === 'INTRADAY'
                    ? 'same population tools/feature_edge_study.py studies — cost_verdict TAKEN, outcome resolved'
                    : 'closed_positions for this strategy, joined to stock_data_daily at entry_date'}
                </p>
              </div>
              <div className="px-4 py-2">
                {!rows ? (
                  <div className="text-xs text-muted-foreground py-3">Loading…</div>
                ) : (
                  rows.map((r) => {
                    const enough = r.n.winners >= MIN_SEGMENT && r.n.losers >= MIN_SEGMENT;
                    return (
                      <div key={r.label} className="py-2.5 border-t border-border/30 first:border-t-0">
                        <div className="flex items-baseline justify-between">
                          <span className="text-xs font-medium">{r.label}</span>
                          <span className="text-[10px] text-muted-foreground">
                            n={r.n.winners + r.n.losers} resolved
                          </span>
                        </div>
                        {!enough ? (
                          <div className="text-[11px] text-muted-foreground mt-1">
                            Not enough resolved history yet (winners n={r.n.winners}, losers n={r.n.losers} — needs {MIN_SEGMENT} each)
                          </div>
                        ) : (
                          <div className="flex items-center justify-between mt-1.5 text-xs">
                            <span className="text-loss">Losers avg {r.loserAvg?.toFixed(2) ?? '—'}{r.unit ?? ''}</span>
                            <span className="text-profit">Winners avg {r.winnerAvg?.toFixed(2) ?? '—'}{r.unit ?? ''}</span>
                          </div>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

export default EngineDetailDialog;
