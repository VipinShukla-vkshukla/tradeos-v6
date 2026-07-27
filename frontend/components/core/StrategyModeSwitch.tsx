'use client';

/**
 * Swing ⇄ Intraday, one click.
 *
 * Both strategies read the same Kite/yfinance prices and the same Supabase —
 * what differs is the horizon and therefore the question being asked. Swing:
 * "what should I hold for one to three weeks." Intraday: "what is happening
 * right now, and does it change anything." Folding both into one tab strip
 * buried the intraday panels behind swing tabs during the session, which is
 * precisely when they matter.
 *
 * Deliberately at the top level rather than as a sixth tab: the mode changes
 * what the whole console is about, and a tab implies peer content.
 */

import { TrendingUp, Activity } from 'lucide-react';
import { useLayoutStore, type StrategyMode } from '@/store/layoutStore';

const MODES: { id: StrategyMode; label: string; icon: typeof TrendingUp; hint: string }[] = [
  { id: 'swing', label: 'Swing', icon: TrendingUp,
    hint: 'The 1-3 week book: signals, positions, engine performance' },
  { id: 'intraday', label: 'Intraday', icon: Activity,
    hint: 'Live session: alerts, broker orders, entry-limit watch' },
];

export function StrategyModeSwitch({ liveDot = false }: { liveDot?: boolean }) {
  const mode = useLayoutStore((s) => s.strategyMode);
  const setMode = useLayoutStore((s) => s.setStrategyMode);

  return (
    <div className="flex items-center gap-0.5 rounded-lg bg-muted/40 p-0.5 shrink-0"
      role="tablist" aria-label="Strategy mode">
      {MODES.map((m) => {
        const active = mode === m.id;
        const Icon = m.icon;
        return (
          <button
            key={m.id}
            role="tab"
            aria-selected={active}
            title={m.hint}
            onClick={() => setMode(m.id)}
            className={`relative flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium
              transition-colors ${active
                ? 'bg-panel text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'}`}
          >
            <Icon className="h-3.5 w-3.5" />
            {m.label}
            {/* Live indicator only on the intraday side, and only when the
                daemon has actually reported in — a dot that is always on says
                nothing about whether anything is running. */}
            {m.id === 'intraday' && liveDot && (
              <span className="ml-0.5 h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
            )}
          </button>
        );
      })}
    </div>
  );
}

export default StrategyModeSwitch;
