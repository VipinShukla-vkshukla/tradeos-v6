'use client';

/**
 * The R-ladder — a position's journey in the unit its exit rules are written in.
 *
 * WHY NOT A PRICE BAR
 * -------------------
 * Every exit rule in control/position_lifecycle.py is expressed in R (risk
 * multiples), not rupees and not percent: book half at 1.5R, arm the trail at
 * 2.0R, hard target at 3.0R, time-stop anything under 0.5R after 15 sessions.
 * A card showing "₹192.05, +10.4%" cannot answer the only question that
 * matters — what will the system do next, and how far away is it. In R the
 * answer is immediate: 1.55R is past the 1.5R rung, so half comes off.
 *
 * The same +10.4% means completely different things on two positions with
 * different stops. R normalises exactly that difference away, which is why the
 * backend uses it and why showing anything else forces the reader to do the
 * conversion in their head.
 *
 * WHAT IS DRAWN
 *   · the rungs, at their configured R values, labelled with the rule
 *   · a filled bar from 0R (entry) to current R
 *   · MFE as a ghost marker — the best this trade ever looked
 *   · MAE as a ghost marker — the worst drawdown survived
 * MFE matters because the gap between MFE and where a trade actually closes is
 * the cost of the exit policy, and it is invisible on a normal P&L display.
 */

interface RLadderProps {
  currentR: number | null | undefined;
  /** % move at the best point while open — converted to R with riskPct */
  mfePct?: number | null;
  maePct?: number | null;
  /** initial risk as % of entry, i.e. (entry - planned_stop) / entry * 100 */
  riskPct: number | null;
  rungs: { r: number; label: string; hint: string }[];
  breakevenMoved?: boolean | null;
  trailActivated?: boolean | null;
  partialBooked?: boolean;
  /** domain padding so markers at the extremes stay inside the track */
  min?: number;
  max?: number;
}

export function RLadder({
  currentR, mfePct, maePct, riskPct, rungs,
  breakevenMoved, trailActivated, partialBooked,
  min = -1.15, max = 3.25,
}: RLadderProps) {
  const span = max - min;
  const pos = (r: number) => `${Math.min(100, Math.max(0, ((r - min) / span) * 100))}%`;

  const r = currentR ?? 0;
  // MFE/MAE arrive as percentages; R is the native unit here, so convert using
  // the same initial risk the backend sized on. Without a usable riskPct the
  // conversion is a guess, so the ghosts are simply omitted rather than drawn
  // at a made-up position.
  const toR = (pct: number | null | undefined) =>
    pct != null && riskPct && riskPct > 0 ? pct / riskPct : null;
  const mfeR = toR(mfePct);
  const maeR = toR(maePct);

  const zeroPos = pos(0);
  const barFrom = r >= 0 ? zeroPos : pos(r);
  const barTo   = r >= 0 ? pos(r) : zeroPos;
  const barColor = r >= 0 ? 'bg-profit/35' : 'bg-loss/35';

  return (
    <div className="pt-1">
      {/* rung labels */}
      <div className="relative h-4 text-[10px] text-muted-foreground">
        {rungs.map((g) => (
          <div key={g.label} className="absolute -translate-x-1/2 whitespace-nowrap"
            style={{ left: pos(g.r) }} title={g.hint}>
            {g.label}
          </div>
        ))}
      </div>

      {/* track */}
      <div className="relative h-7">
        <div className="absolute inset-x-0 top-3 h-1.5 rounded-full bg-muted/40" />

        {/* filled span from entry to current */}
        <div className={`absolute top-3 h-1.5 rounded-full ${barColor}`}
          style={{ left: barFrom, right: `calc(100% - ${barTo})` }} />

        {/* MAE ghost — deepest drawdown survived */}
        {maeR != null && maeR < -0.02 && (
          <div className="absolute top-[7px] h-4 w-px bg-loss/40"
            style={{ left: pos(maeR) }}
            title={`Worst drawdown while open: ${maePct?.toFixed(2)}% (${maeR.toFixed(2)}R)`} />
        )}

        {/* MFE ghost — peak, and the gap to `current` is what giving back costs */}
        {mfeR != null && mfeR > 0.02 && (
          <div className="absolute top-[7px] h-4 w-px bg-profit/50"
            style={{ left: pos(mfeR) }}
            title={`Best while open: ${mfePct?.toFixed(2)}% (${mfeR.toFixed(2)}R)`} />
        )}

        {/* rung ticks */}
        {rungs.map((g) => (
          <div key={g.label}
            className={`absolute top-2 h-3.5 w-px ${g.r === 0 ? 'bg-foreground/50' : 'bg-border'}`}
            style={{ left: pos(g.r) }} />
        ))}

        {/* current position marker */}
        <div className="absolute top-1.5 -translate-x-1/2" style={{ left: pos(r) }}>
          <div className={`h-4 w-4 rounded-full border-2 border-background shadow
            ${r >= 0 ? 'bg-profit' : 'bg-loss'}`}
            title={`Now: ${r.toFixed(2)}R`} />
        </div>
      </div>

      {/* state chips — which rungs have actually fired */}
      <div className="flex items-center gap-1.5 flex-wrap text-[10px] mt-0.5">
        <span className={`font-mono font-semibold ${r >= 0 ? 'text-profit' : 'text-loss'}`}>
          {r >= 0 ? '+' : ''}{r.toFixed(2)}R
        </span>
        {mfeR != null && (
          <span className="text-muted-foreground" title="Peak R reached while open">
            peak {mfeR >= 0 ? '+' : ''}{mfeR.toFixed(2)}R
          </span>
        )}
        {breakevenMoved && (
          <span className="px-1.5 py-px rounded bg-profit/15 text-profit"
            title="Stop has been raised to entry — this trade can no longer lose money">
            risk-free
          </span>
        )}
        {partialBooked && (
          <span className="px-1.5 py-px rounded bg-blue-500/15 text-blue-400"
            title="Half the position has already been booked at the 1.5R rung">
            partial booked
          </span>
        )}
        {trailActivated && (
          <span className="px-1.5 py-px rounded bg-yellow-500/15 text-yellow-400"
            title="Past 2R — the stop now trails 1.5R below the high-water mark">
            trailing
          </span>
        )}
      </div>
    </div>
  );
}

export default RLadder;
