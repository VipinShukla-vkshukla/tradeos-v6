'use client';

// Persistent left navigation — every screen reachable regardless of which
// book you're focused on. Matches the Canvas mockup's rail exactly: a fixed
// list of destinations, no custom-tab system (that belonged to the dropped
// ViewWizard/TabBar feature set).

import {
  LayoutDashboard, Briefcase, Activity, TrendingUp, Scale, Cpu, Brain,
  Database, HeartPulse, SlidersHorizontal,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useLayoutStore } from '@/store/layoutStore';
import { cn } from '@/lib/utils';

const NAV_ITEMS: { id: string; label: string; icon: React.ElementType }[] = [
  { id: 'overview',        label: 'Overview',        icon: LayoutDashboard },
  { id: 'positions',       label: 'Positions & P&L',  icon: Briefcase },
  { id: 'intraday',        label: 'Intraday',         icon: Activity },
  { id: 'performance',     label: 'Engines',          icon: TrendingUp },
  { id: 'allocator',       label: 'Allocator',        icon: Scale },
  { id: 'brain-engine',    label: 'Brain Engine',     icon: Cpu },
  { id: 'ai-intelligence', label: 'AI Intelligence',  icon: Brain },
  { id: 'data-management', label: 'Data Management',  icon: Database },
];

export function Sidebar({ intradayLive = false }: { intradayLive?: boolean }) {
  const activeTabId = useLayoutStore((s) => s.activeTabId);
  const setActiveTab = useLayoutStore((s) => s.setActiveTab);
  const pathname = usePathname();
  // /health and /control are real routes, not activeTabId values — on those
  // pages nothing in the fixed nav list should read as active.
  const onSeparateRoute = pathname === '/health' || pathname === '/control';

  return (
    <nav className="w-56 shrink-0 bg-header border-r border-border flex flex-col py-3 overflow-y-auto">
      <div className="flex flex-col gap-0.5">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
          const active = !onSeparateRoute && activeTabId === id;
          return (
            <div key={id} onClick={() => setActiveTab(id)}
              className={cn(
                'flex items-center gap-2.5 mx-2 px-2.5 py-2 rounded-lg cursor-pointer select-none text-sm transition-colors',
                active ? 'bg-primary/10 text-foreground shadow-[inset_2px_0_0_var(--accent-primary)]'
                       : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
              )}>
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1 truncate">{label}</span>
              {id === 'intraday' && intradayLive && (
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0" />
              )}
            </div>
          );
        })}
      </div>

      <div className="flex-1" />

      <div className="h-px bg-border mx-3 my-2" />
      <div className="flex flex-col gap-0.5">
        <Link href="/health" className={cn(
          'flex items-center gap-2.5 mx-2 px-2.5 py-2 rounded-lg text-sm transition-colors',
          pathname === '/health' ? 'bg-primary/10 text-foreground shadow-[inset_2px_0_0_var(--accent-primary)]'
                                  : 'text-muted-foreground hover:text-foreground hover:bg-white/5')}>
          <HeartPulse className="h-4 w-4 shrink-0" />Health
        </Link>
        <Link href="/control" className={cn(
          'flex items-center gap-2.5 mx-2 px-2.5 py-2 rounded-lg text-sm transition-colors',
          pathname === '/control' ? 'bg-primary/10 text-foreground shadow-[inset_2px_0_0_var(--accent-primary)]'
                                   : 'text-muted-foreground hover:text-foreground hover:bg-white/5')}>
          <SlidersHorizontal className="h-4 w-4 shrink-0" />Control Room
        </Link>
      </div>
    </nav>
  );
}

export default Sidebar;
