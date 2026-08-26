'use client';

// Daily Summary Dashboard — swing framework evolution blueprint, 26-Aug-2026.
//
// "Is the swing sleeve up or down, separately from intraday's, based on
// what TradeOS actually did" — no existing view answers this. PositionsTab's
// totalUnrealized/totalRealized are pooled across both books and
// totalRealized is all-time with no date filter (confirmed before building
// this — not a duplicate of anything already there).
//
// Book value = sleeve (system_config swing_capital/intraday_capital, same
// keys OperatorPanel.tsx already reads, same totalCapital fallback chain)
// + all-time realized P&L for that book + live unrealized P&L for that
// book's open positions. "Today's change" needs a prior-day baseline to be
// exact — tools/snapshot_book_value.py writes one row per (date, framework)
// daily; until at least one snapshot exists this falls back to showing
// today's realized P&L alone, labelled honestly rather than presenting an
// approximation as a precise delta.

import { useEffect, useState } from 'react';
import { TrendingUp, TrendingDown } from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { queries } from '@/lib/supabase';
import { formatCurrency } from '@/lib/formatters';
import type { OpenPosition, ClosedPosition } from '@/types/database';

type Framework = 'SWING' | 'INTRADAY';

interface BookSummary {
  sleeve: number;
  realizedCum: number;
  unrealized: number;
  bookValue: number;
  todayRealized: number;
  changeSinceSnapshot: number | null;   // null = no snapshot baseline yet
  snapshotDate: string | null;
}

function computeSleeve(cfg: Record<string, string>, fw: Framework): number {
  let totalCapital = 20000;
  try {
    const v = Number(JSON.parse(cfg['capital_snapshot'] ?? '{}').configured);
    if (Number.isFinite(v) && v > 0) totalCapital = v;
  } catch { /* snapshot not persisted yet — keep the fallback, same as OperatorPanel.tsx */ }
  const key = fw === 'SWING' ? 'swing_capital' : 'intraday_capital';
  const v = parseFloat(cfg[key] ?? '');
  return Number.isFinite(v) ? v : totalCapital;
}

async function loadBook(cfg: Record<string, string>, fw: Framework): Promise<BookSummary> {
  const sleeve = computeSleeve(cfg, fw);
  const today = new Date().toISOString().slice(0, 10);

  const [openRes, closedRes, snapRes] = await Promise.all([
    queries.getOpenPositionsByFramework(fw),
    queries.getAllClosedPositionsByFramework(fw),
    queries.getBookValueSnapshots(fw, 2),
  ]);

  const open = (openRes.data ?? []) as OpenPosition[];
  const closed = (closedRes.data ?? []) as ClosedPosition[];
  const snaps = snapRes.data ?? [];

  const unrealized = open.reduce((sum, p) => {
    const u = p.unrealized_pnl ?? ((p.current_value ?? 0) - (p.invested_value ?? 0));
    return sum + (u ?? 0);
  }, 0);
  const realizedCum = closed.reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);
  const todayRealized = closed
    .filter((p) => (p.exit_date ?? '').slice(0, 10) === today)
    .reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);

  const bookValue = sleeve + realizedCum + unrealized;

  // Most recent snapshot NOT from today (today's own snapshot, if it
  // already ran, is not a "prior day" baseline to diff against).
  const baseline = snaps.find((s) => s.date !== today) ?? null;
  const changeSinceSnapshot = baseline ? bookValue - baseline.book_value : null;

  return {
    sleeve, realizedCum, unrealized, bookValue, todayRealized,
    changeSinceSnapshot, snapshotDate: baseline?.date ?? null,
  };
}

function BookColumn({ title, mode, s }: { title: string; mode: string; s: BookSummary | null }) {
  if (!s) {
    return (
      <div className="space-y-3">
        <div className="text-sm font-semibold">{title}</div>
        <div className="grid grid-cols-2 gap-3">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="panel p-4 h-20 animate-pulse bg-panel-hover" />
          ))}
        </div>
      </div>
    );
  }

  // "Today's change" — the precise delta once a snapshot baseline exists;
  // today's realized P&L alone, honestly labelled, until one does.
  const hasBaseline = s.changeSinceSnapshot !== null;
  const changeValue = hasBaseline ? s.changeSinceSnapshot! : s.todayRealized;
  const changeType = changeValue > 0 ? 'increase' : changeValue < 0 ? 'decrease' : 'neutral';
  const changeIcon = changeValue >= 0
    ? <TrendingUp className="w-3.5 h-3.5" />
    : <TrendingDown className="w-3.5 h-3.5" />;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">{title}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded border border-border/60 text-muted-foreground uppercase tracking-wide">
          {mode}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <KPICard
          title="Book value"
          value={formatCurrency(s.bookValue, { compact: true })}
          description={`Sleeve ${formatCurrency(s.sleeve, { compact: true })}`}
        />
        <KPICard
          title={hasBaseline ? 'Change since snapshot' : "Today's realized"}
          value={formatCurrency(Math.abs(changeValue), { compact: true, showSign: false })}
          change={{
            value: hasBaseline
              ? `${changeValue >= 0 ? 'up' : 'down'} since ${s.snapshotDate}`
              : 'no snapshot baseline yet',
            type: changeType,
          }}
          icon={changeIcon}
        />
        <KPICard
          title="Unrealized (live)"
          value={formatCurrency(s.unrealized, { compact: true, showSign: true })}
          description="Open positions, marked now"
        />
        <KPICard
          title="Realized (all-time)"
          value={formatCurrency(s.realizedCum, { compact: true, showSign: true })}
          description="Closed positions, this book"
        />
      </div>
    </div>
  );
}

export function DailyBookSummary() {
  const [swing, setSwing] = useState<BookSummary | null>(null);
  const [intraday, setIntraday] = useState<BookSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const cfgRes = await queries.getSystemConfig();
        if (cfgRes.error) throw cfgRes.error;
        const cfg: Record<string, string> = {};
        for (const row of cfgRes.data ?? []) cfg[row.key] = row.value;

        const [sw, id] = await Promise.all([loadBook(cfg, 'SWING'), loadBook(cfg, 'INTRADAY')]);
        if (cancelled) return;
        setSwing(sw);
        setIntraday(id);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'could not load daily summary');
      }
    }
    load();
    const interval = setInterval(load, 60_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  return (
    <Panel title="Daily Summary" description="Book value, split by framework — is each sleeve up or down, and why.">
      {error ? (
        <div className="text-sm text-loss">{error}</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <BookColumn title="Swing" mode="CNC" s={swing} />
          <BookColumn title="Intraday" mode="MIS" s={intraday} />
        </div>
      )}
    </Panel>
  );
}

export default DailyBookSummary;
