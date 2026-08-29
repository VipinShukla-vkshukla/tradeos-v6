'use client';

/**
 * Today's candidates as TRADE PLANS rather than as a watchlist.
 *
 * The old Signal Log answered "what did the scan find". The question that
 * actually costs money is "can I buy this right now, and at what levels" — and
 * that needs the entry zone, the stop, the target and where price sits between
 * them. Those columns exist on signal_output_daily and nothing rendered them.
 *
 * Rows are ordered by the verdict, not the score, because a high-scoring name
 * whose price has run 6% past its entry zone is less actionable than a
 * lower-scoring one sitting inside its zone — and sorting by score buries
 * exactly that.
 */

import { useMemo, useState } from 'react';
import { formatDate } from '@/lib/formatters';
import { priceVerdict, ACTION_STYLE, type Action } from '@/lib/tradeDecision';
import type { Signal } from '@/types/database';

/** Where price sits relative to [zone_low, zone_high]. */
function ZoneBar({ lo, hi, px }: { lo?: number | null; hi?: number | null; px?: number | null }) {
  if (!lo || !hi || !px || hi <= lo) return <span className="text-muted-foreground">—</span>;
  // Pad the domain so a price outside the zone is still visible on the track
  // instead of being clamped onto the end cap, which would make "just above"
  // and "miles above" look identical.
  const pad = (hi - lo) * 1.6;
  const min = Math.min(lo - pad, px), max = Math.max(hi + pad, px);
  const at = (v: number) => `${((v - min) / (max - min)) * 100}%`;
  const inside = px >= lo && px <= hi;
  return (
    <div className="relative h-4 w-full min-w-[90px]"
      title={`Zone ₹${lo.toFixed(2)}–₹${hi.toFixed(2)} · price ₹${px.toFixed(2)}`}>
      <div className="absolute inset-x-0 top-1.5 h-1 rounded bg-muted/50" />
      <div className="absolute top-1.5 h-1 rounded bg-foreground/25"
        style={{ left: at(lo), right: `calc(100% - ${at(hi)})` }} />
      <div className={`absolute top-0.5 h-3 w-0.5 -translate-x-1/2 ${inside ? 'bg-profit' : 'bg-loss'}`}
        style={{ left: at(px) }} />
    </div>
  );
}

export function TradePlanTable({ rows, date }: { rows: Signal[]; date: string | null }) {
  const [showAll, setShowAll] = useState(false);

  const scored = useMemo(() => rows.map((r) => ({ row: r, v: priceVerdict(r) }))
    .sort((a, b) => ACTION_STYLE[a.v.action].rank - ACTION_STYLE[b.v.action].rank
      || (b.row.final_score ?? b.row.score ?? 0) - (a.row.final_score ?? a.row.score ?? 0)),
    [rows]);

  const counts = useMemo(() => {
    const c: Record<Action, number> = { BUY: 0, CHASE: 0, WAIT: 0, SKIP: 0, NO_DATA: 0 };
    for (const s of scored) c[s.v.action]++;
    return c;
  }, [scored]);

  const actionable = counts.BUY + counts.CHASE;
  const shown = showAll ? scored : scored.filter((s) => s.v.action !== 'SKIP' && s.v.action !== 'NO_DATA');

  return (
    <div className="space-y-3">
      {/* The summary line is the point of the panel: on most days the honest
          answer is that nothing is buyable, and that deserves to be stated
          rather than left for the reader to infer from 43 rows. */}
      <div className="flex items-center gap-3 flex-wrap text-xs">
        <span className={actionable > 0 ? 'text-profit font-semibold' : 'text-muted-foreground font-semibold'}>
          {actionable > 0
            ? `${actionable} of ${scored.length} buyable at current prices`
            : `Nothing is buyable at current prices (${scored.length} screened)`}
        </span>
        {/* NO_DATA included so the chips sum to the screened total — a header
            that says 43 above four chips adding to 41 reads as a bug. */}
        {(['BUY', 'CHASE', 'WAIT', 'SKIP', 'NO_DATA'] as Action[]).filter((a) => counts[a] > 0).map((a) => (
          <span key={a} className={`px-1.5 py-0.5 rounded ${ACTION_STYLE[a].chip}`}>
            {counts[a]} {a}
          </span>
        ))}
        {date && <span className="text-muted-foreground ml-auto">as of {formatDate(date, 'dd MMM')}</span>}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border/30 bg-panel-hover/50">
              {['Symbol', 'Verdict', 'Price vs entry zone', 'CMP', 'Buy below', 'Stop', 'Target', 'Risk', 'R:R here'].map((h) => (
                <th key={h} className={`py-2 px-3 font-medium text-muted-foreground whitespace-nowrap ${
                  ['Symbol', 'Verdict', 'Price vs entry zone'].includes(h) ? 'text-left' : 'text-right'}`}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map(({ row: s, v }) => {
              const st = ACTION_STYLE[v.action];
              return (
                <tr key={s.symbol} className="border-b border-border/20 hover:bg-panel-hover">
                  <td className="py-2 px-3">
                    <div className="font-semibold">{s.symbol}</div>
                    <div className="text-muted-foreground truncate max-w-[130px]">{s.sector}</div>
                  </td>
                  <td className="py-2 px-3">
                    <span className={`px-1.5 py-0.5 rounded ${st.chip}`}>{v.action}</span>
                    <div className="text-muted-foreground mt-0.5 max-w-[230px]" title={v.headline}>
                      {v.reason}
                    </div>
                  </td>
                  <td className="py-2 px-3 w-[130px]">
                    <ZoneBar lo={s.entry_zone_low} hi={s.entry_zone_high} px={s.current_price} />
                    {/* distZonePct is measured from the zone LOW while price is
                        inside the zone, so printing it bare as "1.3% above" on a
                        BUY row reads as "above the zone" — the opposite of what
                        BUY means. The verdict already knows which side price is
                        on; let it choose the wording. */}
                    <div className="text-[10px] text-muted-foreground">
                      {v.action === 'BUY' ? 'inside zone'
                        : v.action === 'WAIT' ? 'below zone — not yet'
                        : v.action === 'CHASE' && v.distZonePct != null
                          ? <span className="text-loss">{v.distZonePct.toFixed(1)}% above zone</span>
                        : v.distZonePct != null ? `${v.distZonePct > 0 ? '+' : ''}${v.distZonePct.toFixed(1)}% vs zone`
                        : '—'}
                    </div>
                  </td>
                  <td className="text-right py-2 px-3 font-mono">₹{s.current_price?.toFixed(2) ?? '—'}</td>
                  {/* The limit price that makes this trade worth taking. Without
                      it a failing R:R just reads as "no", when the real answer
                      is almost always "not at this price". */}
                  <td className="text-right py-2 px-3 font-mono">
                    {v.maxEntry == null ? <span className="text-muted-foreground">—</span> : (
                      <span
                        className={s.current_price != null && s.current_price <= v.maxEntry
                          ? 'text-profit' : 'text-yellow-400'}
                        title={v.rrAtZoneLow != null
                          ? `At the zone low this plan is ${v.rrAtZoneLow.toFixed(2)}R:R`
                          : undefined}>
                        ₹{v.maxEntry.toFixed(2)}
                        {s.current_price != null && s.current_price > v.maxEntry && (
                          <span className="text-muted-foreground ml-1">
                            ({(((s.current_price - v.maxEntry) / s.current_price) * 100).toFixed(1)}%↓)
                          </span>
                        )}
                      </span>
                    )}
                  </td>
                  <td className="text-right py-2 px-3 font-mono text-loss">₹{s.planned_stop?.toFixed(2) ?? '—'}</td>
                  <td className="text-right py-2 px-3 font-mono text-profit">₹{s.planned_target?.toFixed(2) ?? '—'}</td>
                  <td className="text-right py-2 px-3 font-mono text-muted-foreground">
                    {v.riskPct != null ? `${v.riskPct.toFixed(1)}%` : '—'}
                  </td>
                  {/* R:R measured FROM THE CURRENT PRICE, not from the entry
                      zone — the same definition final_snapshot persists. Below
                      1.0 means buying here risks more than it stands to make. */}
                  <td className={`text-right py-2 px-3 font-mono font-semibold ${
                    v.rr == null ? 'text-muted-foreground' : v.rr >= 1.5 ? 'text-profit'
                      : v.rr >= 1 ? 'text-yellow-400' : 'text-loss'}`}>
                    {v.rr == null ? '—' : `${v.rr.toFixed(2)}`}
                  </td>
                </tr>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td colSpan={9} className="py-6 text-center text-muted-foreground">
                  Every candidate screened out at current prices — nothing to act on.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {counts.SKIP + counts.NO_DATA > 0 && (
        <button className="text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowAll((x) => !x)}>
          {showAll ? 'Hide' : 'Show'} {counts.SKIP + counts.NO_DATA} screened out
        </button>
      )}
    </div>
  );
}

export default TradePlanTable;
