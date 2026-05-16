// SURGICAL PATCH for components/core/Header.tsx
//
// Replace the existing RegimeBadge function and HeaderProps interface with these.
// Do NOT change anything else in the file.
//
// ─── REPLACE HeaderProps interface ───────────────────────────────────────────
//
// OLD:
//   interface HeaderProps {
//     marketRegime?: 'RISK_ON' | 'RISK_OFF' | 'NEUTRAL';
//     ...
//   }
//
// NEW:

import type { RegimeState } from '@/types/database';

interface HeaderProps {
  marketRegime?: RegimeState;         // 'TRENDING' | 'RISK ON' | 'NEUTRAL' | 'RECOVERING' | 'RISK OFF'
  pipelineLastRun?: string;
  aiProviderStatus?: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED';
  onSettingsClick?: () => void;
  onNotificationsClick?: () => void;
  onLayoutClick?: () => void;
  notificationCount?: number;
}

// ─── REPLACE RegimeBadge function ────────────────────────────────────────────
//
// OLD function handled only 3 states and used underscore notation.
// NEW function handles all 5 TradeOS v6 regime states with space notation.

function RegimeBadge({ regime }: { regime: RegimeState }) {
  const config: Record<
    RegimeState,
    { label: string; icon: typeof TrendingUp; className: string }
  > = {
    'TRENDING':   { label: 'TRENDING',   icon: TrendingUp,   className: 'badge-success' },
    'RISK ON':    { label: 'RISK ON',    icon: TrendingUp,   className: 'badge-success' },
    'NEUTRAL':    { label: 'NEUTRAL',    icon: Minus,        className: 'badge-warning' },
    'RECOVERING': { label: 'RECOVERING', icon: Minus,        className: 'badge-warning' },
    'RISK OFF':   { label: 'RISK OFF',   icon: TrendingDown, className: 'badge-destructive' },
  };

  const { label, icon: Icon, className } = config[regime] ?? config['NEUTRAL'];

  return (
    <div className={`flex items-center gap-1 text-xs font-medium px-2 py-1 rounded ${className}`}>
      <Icon className="w-3 h-3" />
      <span>{label}</span>
    </div>
  );
}

// ─── REPLACE the Header component's marketRegime prop usage ──────────────────
//
// In page.tsx, the Header is called with:
//   <Header marketRegime="NEUTRAL" ... />
//
// Replace this with a live Supabase query. Add to page.tsx:
//
//   import { queries } from '@/lib/supabase';
//   import type { RegimeState } from '@/types/database';
//
//   const [regime, setRegime] = useState<RegimeState>('NEUTRAL');
//
//   useEffect(() => {
//     queries.getMarketRegime().then(({ data }) => {
//       if (data?.[0]) {
//         const r = data[0];
//         setRegime((r.computed_regime ?? r.regime) as RegimeState);
//       }
//     });
//   }, []);
//
//   // Then pass:
//   <Header marketRegime={regime} ... />
