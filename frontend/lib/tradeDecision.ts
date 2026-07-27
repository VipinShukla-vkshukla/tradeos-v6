/**
 * Port of backend/analysis/trade_decision.py `decide()`.
 *
 * WHY A PORT AND NOT A GUESS
 * --------------------------
 * The Telegram alert and this dashboard describe the same candidate on the same
 * day. If the dashboard invents its own "looks good / too extended" rule they
 * will eventually disagree, and the moment they disagree neither can be
 * trusted. So the branch ORDER and the thresholds here are the Python ones,
 * deliberately, including the parts that look redundant.
 *
 * DELIBERATELY OMITTED: the portfolio-constraint branch. decide() consults
 * check_new_entry() for sector caps and position sizing, which needs live
 * capital and the open book. Reproducing that client-side would be a second
 * implementation of the sizing rules — exactly the divergence this file exists
 * to prevent — so the verdict here is the PRICE verdict only, and anything the
 * portfolio layer would block is labelled as still subject to it.
 *
 * Keep in sync with backend/analysis/trade_decision.py.
 */

import type { Signal } from '@/types/database';

export type Action = 'BUY' | 'CHASE' | 'WAIT' | 'SKIP' | 'NO_DATA';

export interface PriceVerdict {
  action: Action;
  /** one line, the same shape the alert uses */
  headline: string;
  reason: string;
  rr: number | null;
  riskPct: number | null;
  upPct: number | null;
  distZonePct: number | null;
  /** highest price at which R:R still meets minRR — the limit worth resting */
  maxEntry: number | null;
  /** R:R if filled at the entry zone low, i.e. the plan at its best price */
  rrAtZoneLow: number | null;
}

/**
 * Highest entry price at which reward:risk still meets minRR.
 *   (tgt - px) / (px - stop) >= minRR  =>  px <= (tgt + minRR*stop) / (1 + minRR)
 * Mirrors max_entry_for_rr() in analysis/trade_decision.py.
 */
export function maxEntryForRR(stop: number, target: number, minRR: number): number | null {
  if (!stop || !target || target <= stop || minRR <= 0) return null;
  return (target + minRR * stop) / (1 + minRR);
}

const num = (v: unknown): number | null => {
  const f = typeof v === 'number' ? v : parseFloat(String(v ?? ''));
  return Number.isFinite(f) && f > 0 ? f : null;
};

export function priceVerdict(row: Signal, minRR = 1.0): PriceVerdict {
  const sym  = row.symbol ?? '?';
  const stop = num(row.planned_stop);
  const tgt  = num(row.planned_target);
  const zlo  = num(row.entry_zone_low);
  const zhi  = num(row.entry_zone_high) ?? (zlo ? zlo * 1.02 : null);
  const px   = num(row.current_price);
  const maxChase = row.ai_max_chase_pct != null && row.ai_max_chase_pct > 0
    ? row.ai_max_chase_pct : null;

  const none = { rr: null, riskPct: null, upPct: null, distZonePct: null,
                 maxEntry: null, rrAtZoneLow: null };

  if (!px || !stop || !tgt) {
    // A missing plan is not always missing data. compute_trade_levels records
    // why it declined — "risk_too_wide_8.2pct" means the ATR stop breached the
    // risk cap, which is the model working correctly.
    const src = (row.planned_stop_source ?? '').trim();
    if (src && !['atr', 'structure', 'atr_capped'].includes(src)) {
      const pretty = src.startsWith('risk_too_wide')
        ? `stop would sit ${src.split('_').pop()?.replace('pct', '')}% below entry, past the risk cap — too volatile to size sensibly`
        : src.replace(/_/g, ' ');
      return { action: 'SKIP', headline: `SKIP — ${pretty}`, reason: pretty, ...none };
    }
    const missing = [['price', px], ['stop', stop], ['target', tgt]]
      .filter(([, v]) => !v).map(([n]) => n).join(', ');
    return { action: 'NO_DATA', headline: `No decision — missing ${missing}`,
             reason: `missing ${missing}`, ...none };
  }

  if (px <= stop) {
    return { action: 'SKIP', headline: `SKIP — price ${px.toFixed(2)} is at or below the stop ${stop.toFixed(2)}`,
             reason: 'setup invalidated: stop broken', ...none };
  }
  if (px >= tgt) {
    return { action: 'SKIP', headline: `SKIP — price ${px.toFixed(2)} already at target ${tgt.toFixed(2)}`,
             reason: 'no upside left against this plan', ...none, rr: 0 };
  }

  const risk = px - stop, reward = tgt - px;
  const rr = reward / risk;
  const riskPct = (risk / px) * 100;
  const upPct = (reward / px) * 100;
  const anchor = zhi && px > zhi ? zhi : zlo;
  const distZonePct = anchor ? ((px - anchor) / anchor) * 100 : null;
  const maxEntry = maxEntryForRR(stop, tgt, minRR);
  const rrZlo = zlo && zlo > stop ? (tgt - zlo) / (zlo - stop) : null;
  const base = { rr: +rr.toFixed(2), riskPct: +riskPct.toFixed(2),
                 upPct: +upPct.toFixed(2),
                 distZonePct: distZonePct != null ? +distZonePct.toFixed(2) : null,
                 maxEntry: maxEntry != null ? +maxEntry.toFixed(2) : null,
                 rrAtZoneLow: rrZlo != null ? +rrZlo.toFixed(2) : null };

  if (rr < minRR) {
    // Not "no", but "not here". The price that makes it work is knowable, so
    // quote it and let a resting limit do the waiting. On 24 Jul this turned 20
    // of 32 dead SKIPs into actionable limit orders.
    if (maxEntry != null && maxEntry < px) {
      const gap = ((px - maxEntry) / px) * 100;
      return { action: 'WAIT',
               headline: `WAIT — R:R ${rr.toFixed(2)} here; buy at or below ${maxEntry.toFixed(2)} (${gap.toFixed(1)}% lower)`,
               reason: `Rest a limit at ₹${maxEntry.toFixed(2)} · stop ${stop.toFixed(2)} · target ${tgt.toFixed(2)}`,
               ...base };
    }
    return { action: 'SKIP',
             headline: `SKIP — R:R ${rr.toFixed(2)} below ${minRR} at ${px.toFixed(2)}`,
             reason: `reward ${upPct.toFixed(1)}% vs risk ${riskPct.toFixed(1)}% no longer justifies the trade`,
             ...base };
  }
  if (zlo && px < zlo) {
    const gap = ((zlo - px) / zlo) * 100;
    return { action: 'WAIT',
             headline: `WAIT — ${gap.toFixed(1)}% below the zone (${zlo.toFixed(2)})`,
             reason: `set an alert at ${zlo.toFixed(2)}; R:R would be ${rr.toFixed(2)}`,
             ...base };
  }
  if (zhi && px > zhi) {
    const chase = ((px - zhi) / zhi) * 100;
    if (maxChase != null && chase > maxChase) {
      return { action: 'SKIP',
               headline: `SKIP — ${chase.toFixed(1)}% above the zone, past the ${maxChase.toFixed(1)}% chase limit`,
               reason: 'entering here pays for a move that has already happened',
               ...base };
    }
    return { action: 'CHASE',
             headline: `CHASE OK — ${chase.toFixed(1)}% above zone, R:R still ${rr.toFixed(2)}`,
             reason: `stop ${stop.toFixed(2)} · target ${tgt.toFixed(2)}`, ...base };
  }
  return { action: 'BUY',
           headline: `BUY — in zone at ${px.toFixed(2)}, R:R ${rr.toFixed(2)}`,
           reason: `stop ${stop.toFixed(2)} (${riskPct.toFixed(1)}%) · target ${tgt.toFixed(2)} (+${upPct.toFixed(1)}%)`,
           ...base };
}

export const ACTION_STYLE: Record<Action, { chip: string; text: string; rank: number }> = {
  BUY:     { chip: 'bg-profit/20 text-profit',        text: 'text-profit',       rank: 0 },
  CHASE:   { chip: 'bg-blue-500/20 text-blue-400',    text: 'text-blue-400',     rank: 1 },
  WAIT:    { chip: 'bg-yellow-500/15 text-yellow-400',text: 'text-yellow-400',   rank: 2 },
  SKIP:    { chip: 'bg-muted text-muted-foreground',  text: 'text-muted-foreground', rank: 3 },
  NO_DATA: { chip: 'bg-muted text-muted-foreground',  text: 'text-muted-foreground', rank: 4 },
};
