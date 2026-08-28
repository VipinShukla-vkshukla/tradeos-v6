'use client';

// Top bar — brand, regime, AI status, clock. Matches the Canvas mockup's
// topbar exactly: no icon-button cluster, no layout/notifications/settings —
// those belonged to features (ViewWizard, ThemeEditor) that no longer exist.
// Health and Control Room live in the sidebar now, not here.

import { useState, useEffect } from 'react';
import { TrendingUp, TrendingDown, Minus, Cpu } from 'lucide-react';
import type { RegimeState } from '@/types/database';
import { formatISTDateTime } from '@/lib/formatters';

interface HeaderProps {
  marketRegime?: RegimeState;
  aiProviderStatus?: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED';
}

function RegimeBadge({ regime }: { regime: RegimeState }) {
  const config: Record<RegimeState, { label: string; icon: typeof TrendingUp; className: string }> = {
    'TRENDING':   { label: 'TRENDING',   icon: TrendingUp,   className: 'badge-success' },
    'RISK ON':    { label: 'RISK ON',    icon: TrendingUp,   className: 'badge-success' },
    'NEUTRAL':    { label: 'NEUTRAL',    icon: Minus,        className: 'badge-warning' },
    'RECOVERING': { label: 'RECOVERING', icon: Minus,        className: 'badge-warning' },
    'RISK OFF':   { label: 'RISK OFF',   icon: TrendingDown, className: 'badge-danger' },
  };
  const { label, icon: Icon, className } = config[regime] ?? config['NEUTRAL'];
  return (
    <div className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-md ${className}`}>
      <Icon className="w-3.5 h-3.5" />
      <span>{label}</span>
    </div>
  );
}

function AIBadge({ status }: { status: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED' }) {
  const config = {
    AVAILABLE:   { label: 'AI Ready',    className: 'border border-border text-muted-foreground' },
    DEGRADED:    { label: 'AI Degraded', className: 'badge-warning' },
    UNAVAILABLE: { label: 'AI Offline',  className: 'badge-danger' },
    DISABLED:    { label: 'AI Disabled', className: 'bg-muted text-muted-foreground' },
  };
  const { label, className } = config[status];
  return (
    <div className={`flex items-center gap-1.5 text-xs font-medium px-2.5 py-1 rounded-md ${className}`}>
      <Cpu className="h-3.5 w-3.5" />
      <span>{label}</span>
    </div>
  );
}

export function Header({ marketRegime = 'NEUTRAL', aiProviderStatus = 'AVAILABLE' }: HeaderProps) {
  const [currentTime, setCurrentTime] = useState<string>('');

  useEffect(() => {
    setCurrentTime(formatISTDateTime('dd MMM yyyy, HH:mm'));
    const interval = setInterval(() => setCurrentTime(formatISTDateTime('dd MMM yyyy, HH:mm')), 60000);
    return () => clearInterval(interval);
  }, []);

  return (
    <header className="h-14 shrink-0 bg-header border-b border-border px-4 flex items-center justify-between">
      <div className="flex items-center gap-4">
        <h1 className="text-[15px] font-semibold tracking-tight text-foreground">
          TRADEOS INTELLIGENCE CONSOLE
        </h1>
        <div className="flex items-center gap-2">
          <RegimeBadge regime={marketRegime} />
          <AIBadge status={aiProviderStatus} />
        </div>
      </div>
      <span className="font-mono text-xs text-muted-foreground">{currentTime} IST</span>
    </header>
  );
}

export default Header;
