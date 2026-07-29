'use client';

/**
 * Every closed trade, grouped by the day it closed.
 *
 * WHY A LOG AND NOT JUST THE AGGREGATES
 * -------------------------------------
 * The KPI row answers "how am I doing"; it cannot answer "what did it actually
 * do". Those are different questions and the second one is what you need when a
 * number looks wrong — a win rate of 100% over one trade tells you nothing until
 * you can see that the one trade was PATANJALI, PDL, in and out the same day,
 * +4.37R, exited on TARGET_HIT.
 *
 * Grouped by exit date because that is the unit you review in. A trading day is
 * the smallest span over which "did the system behave" is a meaningful question,
 * and a flat list sorted by date makes you do the grouping in your head.
 *
 * NET IS THE HEADLINE, GROSS IS SHOWN
 * -----------------------------------
 * Each row shows P&L after charges where they were recorded, with the cost
 * beside it. Trades closed before migration 025 have no charges and say so
 * rather than displaying a zero, because "cost nothing" and "cost unknown" must
 * not look the same.
 */

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { formatCurrency } from '@/lib/formatters';
import type { ClosedPosition } from '@/types/database';

function dayLabel(iso: string): string {
  if (!iso) return 'Unknown date';
  const d = new Date(iso + 'T00:00:00');
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString('en-IN', {
    weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
  });
}

function Chip({ text, tone }: { text: string; tone: 'paper' | 'live' | 'fw' | 'muted' }) {
  const cls = {
    paper: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
    live: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
    fw: 'bg-purple-500/15 text-purple-400 border-purple-500/30',
    muted: 'bg-muted/40 text-muted-foreground border-border/40',
  }[tone];
  return (
    <span className={`text-[9px] px-1.5 py-0.5 rounded border font-medium ${cls}`}>
      {text}
    </span>
  );
}

export function TradeLog({ trades }: { trades: ClosedPosition[] }) {
  // Newest day first — the question is almost always about today.
  const byDay = new Map<string, ClosedPosition[]>();
  for (const t of trades) {
    const k = (t.exit_date ?? '').slice(0, 10) || 'unknown';
    if (!byDay.has(k)) byDay.set(k, []);
    byDay.get(k)!.push(t);
  }
  const days = [...byDay.keys()].sort((a, b) => b.localeCompare(a));

  // Only the most recent day starts open. A log that expands everything is a
  // wall of rows; one that collapses everything hides the thing you came for.
  //
  // Stored as EXPLICIT overrides rather than a set of open days, because
  // `days` changes whenever the book or era filter changes and a useState
  // initialiser runs once. Seeding a set with days.slice(0,1) captured the day
  // list from the first render — before the data had loaded, and never again
  // after — so switching from Real money to Paper left the newest paper day
  // collapsed while the log claimed to default it open.
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const isOpenDay = (d: string) => overrides[d] ?? (d === days[0]);
  const toggle = (d: string) =>
    setOverrides((prev) => ({ ...prev, [d]: !isOpenDay(d) }));

  if (trades.length === 0) {
    return (
      <div className="text-sm text-muted-foreground py-6 text-center">
        No closed trades in this selection.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {days.map((day) => {
        const rows = byDay.get(day)!;
        const gross = rows.reduce((s, r) => s + (r.realized_pnl ?? 0), 0);
        const costed = rows.filter((r) => r.charges != null);
        const charges = costed.reduce((s, r) => s + (r.charges ?? 0), 0);
        const allCosted = costed.length === rows.length;
        const shown = allCosted ? gross - charges : gross;
        const wins = rows.filter((r) => (r.realized_pnl ?? 0) > 0).length;
        const isOpen = isOpenDay(day);

        return (
          <div key={day} className="rounded-lg border border-border/50 overflow-hidden">
            <button
              onClick={() => toggle(day)}
              className="w-full flex items-center gap-2 px-3 py-2 bg-panel-hover hover:bg-panel-hover/70 transition-colors text-left"
            >
              {isOpen ? <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                      : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
              <span className="text-xs font-medium">{dayLabel(day)}</span>
              <span className="text-[10px] text-muted-foreground">
                {rows.length} trade{rows.length > 1 ? 's' : ''} · {wins}W/{rows.length - wins}L
              </span>
              <span className={`ml-auto text-sm font-mono font-medium ${
                shown >= 0 ? 'text-profit' : 'text-loss'}`}>
                {formatCurrency(shown, { showSign: true })}
              </span>
              {allCosted && charges > 0 && (
                <span className="text-[9px] text-muted-foreground">
                  net · {formatCurrency(charges)} charges
                </span>
              )}
            </button>

            {isOpen && (
              <div className="overflow-x-auto">
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-muted-foreground border-b border-border/30">
                      <th className="text-left  font-medium px-3 py-1.5">Ticker</th>
                      <th className="text-left  font-medium px-2 py-1.5">Strategy</th>
                      <th className="text-right font-medium px-2 py-1.5">Qty</th>
                      <th className="text-right font-medium px-2 py-1.5">Entry</th>
                      <th className="text-right font-medium px-2 py-1.5">Exit</th>
                      <th className="text-right font-medium px-2 py-1.5">P&amp;L</th>
                      <th className="text-right font-medium px-2 py-1.5">Charges</th>
                      <th className="text-right font-medium px-2 py-1.5">R</th>
                      <th className="text-right font-medium px-2 py-1.5">Held</th>
                      <th className="text-left  font-medium px-3 py-1.5">Exit reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((t) => {
                      const isPaper = (t.mode ?? 'LIVE').toUpperCase() === 'PAPER';
                      const qty = t.actual_qty ?? t.proposed_qty ?? 0;
                      const net = t.charges != null
                        ? (t.realized_pnl ?? 0) - t.charges : (t.realized_pnl ?? 0);
                      return (
                        <tr key={t.id} className="border-b border-border/20 last:border-0">
                          <td className="px-3 py-1.5">
                            <div className="flex items-center gap-1.5">
                              <span className="font-medium">{t.symbol}</span>
                              <Chip text={isPaper ? 'PAPER' : 'LIVE'} tone={isPaper ? 'paper' : 'live'} />
                              {t.framework && <Chip text={t.framework} tone="fw" />}
                            </div>
                          </td>
                          <td className="px-2 py-1.5 text-muted-foreground">
                            {t.intraday_strategy ?? t.strategy ?? '—'}
                          </td>
                          <td className="px-2 py-1.5 text-right font-mono">{qty}</td>
                          <td className="px-2 py-1.5 text-right font-mono">
                            {t.entry_price?.toFixed(2) ?? '—'}
                          </td>
                          <td className="px-2 py-1.5 text-right font-mono">
                            {t.exit_price?.toFixed(2) ?? '—'}
                          </td>
                          <td className={`px-2 py-1.5 text-right font-mono font-medium ${
                            net >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {formatCurrency(net, { showSign: true })}
                            {t.pnl_pct != null && (
                              <span className="text-muted-foreground font-normal">
                                {' '}({t.pnl_pct > 0 ? '+' : ''}{t.pnl_pct.toFixed(2)}%)
                              </span>
                            )}
                          </td>
                          {/* An em dash, not 0.00 — this trade closed before
                              charges were recorded, which is not the same as
                              having cost nothing. */}
                          <td className="px-2 py-1.5 text-right font-mono text-muted-foreground">
                            {t.charges != null ? formatCurrency(t.charges) : '—'}
                          </td>
                          <td className={`px-2 py-1.5 text-right font-mono ${
                            (t.r_multiple ?? 0) >= 0 ? 'text-profit' : 'text-loss'}`}>
                            {t.r_multiple != null ? `${t.r_multiple > 0 ? '+' : ''}${t.r_multiple.toFixed(2)}R` : '—'}
                          </td>
                          <td className="px-2 py-1.5 text-right text-muted-foreground">
                            {t.hold_days != null
                              ? (t.hold_days === 0 ? 'intraday' : `${t.hold_days}d`)
                              : '—'}
                          </td>
                          <td className="px-3 py-1.5 text-muted-foreground"
                              title={t.exit_reason_detail ?? undefined}>
                            {t.exit_reason ?? '—'}
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
