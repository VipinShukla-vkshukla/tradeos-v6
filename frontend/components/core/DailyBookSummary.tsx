'use client';

// Book heroes — matches Main.dc.html's Row A exactly: one card per book,
// badge + product/mode in the head, entries-used-today (swing) or the
// square-off reminder (intraday) on the right, a big book value with sleeve
// + today's realized underneath, and a stat column of open positions /
// unrealized / open risk / realized all-time / round-trip cost.
//
// Book value = sleeve (system_config swing_capital/intraday_capital, same
// keys OperatorPanel.tsx already reads) + all-time realized P&L for that
// book + live unrealized P&L for that book's open positions.
//
// Open risk uses the identical formula PositionsTab's KPI strip computes
// (max(0, (price - stop) * qty), summed) — imported as a literal copy of
// that logic, not a second definition of "risk" that could drift from it.
//
// Round-trip cost is EMPIRICAL, not the modelled cost_model.round_trip() —
// deliberately. That Python model is config-driven (rates live in
// system_config so they can be corrected without a deploy) and reimplementing
// it here risks exactly the divergent-cost-model problem this project has
// already been burned by once (see CLAUDE.md: "three divergent R:R models
// giving three answers for one stock on one day"). closed_positions.charges
// is the same number the backend actually charged, so charges/invested_value
// averaged over this book's recent closed trades is the true observed cost,
// not a re-derived guess. Trades before migration 025 have no reconstructable
// charges and are excluded rather than treated as zero-cost.

import { useEffect, useState } from 'react';
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
  openCount: number;
  openRisk: number;
  entriesToday: number;
  entryCap: number;
  roundTripCostPct: number | null;
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

// Same shape as PositionsTab's own openRisk KPI — a stop at or above entry
// (breakeven or better) contributes nothing, which is the point of moving to
// breakeven in the first place.
function computeOpenRisk(open: OpenPosition[]): number {
  return open.reduce((sum, p) => {
    const qty = p.current_qty ?? p.actual_qty ?? 0;
    const stop = p.active_sl ?? p.planned_stop;
    const px = p.current_price ?? p.entry_price;
    if (!qty || !stop || !px) return sum;
    return sum + Math.max(0, (px - stop) * qty);
  }, 0);
}

async function loadBook(cfg: Record<string, string>, fw: Framework): Promise<BookSummary> {
  const sleeve = computeSleeve(cfg, fw);
  const today = new Date().toISOString().slice(0, 10);

  // Narrow selects — this fetch runs on a timer (see the interval below), and
  // open_positions/closed_positions carry ~50 columns each. Pulling every
  // column just to sum eight numeric fields was confirmed, against a day of
  // real edge_logs traffic, to be the largest single contributor to this
  // dashboard's Supabase egress. List every field loadBook/computeOpenRisk
  // actually reads, nothing else.
  const [openRes, closedRes] = await Promise.all([
    queries.getOpenPositionsByFramework(fw,
      'entry_date,current_qty,actual_qty,active_sl,planned_stop,current_price,entry_price,unrealized_pnl,current_value,invested_value'),
    queries.getAllClosedPositionsByFramework(fw,
      'entry_date,exit_date,realized_pnl,charges,invested_value'),
  ]);

  const open = (openRes.data ?? []) as OpenPosition[];
  const closed = (closedRes.data ?? []) as ClosedPosition[];

  const unrealized = open.reduce((sum, p) => {
    const u = p.unrealized_pnl ?? ((p.current_value ?? 0) - (p.invested_value ?? 0));
    return sum + (u ?? 0);
  }, 0);
  const realizedCum = closed.reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);
  const todayRealized = closed
    .filter((p) => (p.exit_date ?? '').slice(0, 10) === today)
    .reduce((sum, p) => sum + (p.realized_pnl ?? 0), 0);

  const entriesToday = [...open, ...closed]
    .filter((p) => (p.entry_date ?? '').slice(0, 10) === today).length;

  const capKey = fw === 'SWING' ? 'swing_max_new_per_day' : 'intraday_max_new_per_day';
  const capDefault = fw === 'SWING' ? 2 : 4;
  const capParsed = parseInt(cfg[capKey] ?? '', 10);
  const entryCap = Number.isFinite(capParsed) && capParsed > 0 ? capParsed : capDefault;

  const withCharges = closed
    .slice(0, 30) // most recent — cost rates drift far less than 30 trades' worth
    .filter((p) => p.charges != null && p.invested_value);
  const roundTripCostPct = withCharges.length
    ? withCharges.reduce((s, p) => s + (p.charges! / p.invested_value) * 100, 0) / withCharges.length
    : null;

  return {
    sleeve, realizedCum, unrealized,
    bookValue: sleeve + realizedCum + unrealized,
    todayRealized,
    openCount: open.length,
    openRisk: computeOpenRisk(open),
    entriesToday, entryCap,
    roundTripCostPct,
  };
}

function MiniRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-semibold">{children}</span>
    </div>
  );
}

function BookHero({ book, s }: { book: Framework; s: BookSummary | null }) {
  const isSwing = book === 'SWING';
  const badgeClass = isSwing ? 'badge-swing' : 'badge-intraday';
  const borderClass = isSwing ? 'border-t-swing' : 'border-t-intraday';
  const gradClass = isSwing ? 'from-swing/10' : 'from-intraday/10';

  if (!s) {
    return (
      <div className={`panel flex-1 border-t-2 ${borderClass} overflow-hidden`}>
        <div className="p-4 h-24 animate-pulse bg-panel-hover/40" />
        <div className="p-4 h-32 animate-pulse bg-panel-hover/20" />
      </div>
    );
  }

  return (
    <div className={`panel flex-1 border-t-2 ${borderClass} overflow-hidden`}>
      <div className={`px-4 py-3 flex items-center justify-between border-b border-border bg-gradient-to-b ${gradClass} to-transparent`}>
        <div className="text-sm font-semibold flex items-center gap-2">
          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide ${badgeClass}`}>
            {isSwing ? 'Swing' : 'Intraday'}
          </span>
          {isSwing ? 'CNC · LIVE' : 'MIS · PAPER'}
        </div>
        <span className="text-[11px] text-muted-foreground">
          {isSwing ? `${s.entriesToday}/${s.entryCap} entries used today` : 'flat by 15:15'}
        </span>
      </div>
      <div className="p-4 grid grid-cols-[1.3fr_1fr] gap-4">
        <div>
          <div className="text-[32px] font-bold leading-none">{formatCurrency(s.bookValue, { compact: true })}</div>
          <div className="text-[11px] text-muted-foreground mt-1.5">
            Sleeve {formatCurrency(s.sleeve, { compact: true })} ·{' '}
            <span className={s.todayRealized >= 0 ? 'text-profit' : 'text-loss'}>
              {formatCurrency(s.todayRealized, { showSign: true })}
            </span>{' '}realized today
          </div>
        </div>
        <div className="flex flex-col gap-2.5 justify-center">
          <MiniRow label="Open positions">{s.openCount}</MiniRow>
          <MiniRow label="Unrealized">
            <span className={s.unrealized >= 0 ? 'text-profit' : 'text-loss'}>
              {formatCurrency(s.unrealized, { showSign: true, compact: true })}
            </span>
          </MiniRow>
          <MiniRow label="Open risk">{formatCurrency(s.openRisk, { compact: true })}</MiniRow>
          <MiniRow label="Realized all-time">
            <span className={s.realizedCum >= 0 ? 'text-profit' : 'text-loss'}>
              {formatCurrency(s.realizedCum, { showSign: true, compact: true })}
            </span>
          </MiniRow>
          <MiniRow label="Round-trip cost">
            {s.roundTripCostPct != null ? `~${s.roundTripCostPct.toFixed(2)}%` : '—'}
          </MiniRow>
        </div>
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
        // Named keys only — this used to be an unfiltered `select=*` over
        // ~600 system_config rows, on this same timer, to read five of them.
        const cfgRes = await queries.getSystemConfig([
          'capital_snapshot', 'swing_capital', 'intraday_capital',
          'swing_max_new_per_day', 'intraday_max_new_per_day',
        ]);
        if (cfgRes.error) throw cfgRes.error;
        const cfg: Record<string, string> = {};
        for (const row of cfgRes.data ?? []) cfg[row.key] = row.value;

        const [sw, id] = await Promise.all([loadBook(cfg, 'SWING'), loadBook(cfg, 'INTRADAY')]);
        if (cancelled) return;
        setSwing(sw);
        setIntraday(id);
        setError(null);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'could not load book summary');
      }
    }
    load();
    // 3 minutes, not 1 — this is a summary card, not a live ticker (the
    // Positions & P&L tab shows the same data on demand, faster if needed).
    // Combined with the narrow selects above, this is the other half of the
    // egress fix: same accuracy, a third of the poll frequency.
    const interval = setInterval(load, 180_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, []);

  if (error) {
    return <div className="panel p-4 text-sm text-loss">{error}</div>;
  }

  return (
    <div className="flex flex-col md:flex-row gap-4">
      <BookHero book="SWING" s={swing} />
      <BookHero book="INTRADAY" s={intraday} />
    </div>
  );
}

export default DailyBookSummary;
