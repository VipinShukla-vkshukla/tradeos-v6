'use client';

// TradeOS v6 — System Navigation
//
// Links the console to Preflight and the Control Room, and surfaces the live
// readiness verdict in the header so a BLOCKED state is visible from any tab
// rather than only on a page you have to remember to open.
//
// WHY THIS IS A HEADER COMPONENT AND NOT A TAB
//   Tabs live in useLayoutStore, which is persisted to localStorage. Its merge
//   step maps over the PERSISTED list and restores canonical names for tabs it
//   recognises — but it never adds a core tab that appears in CORE_TABS and is
//   absent from the saved list. So anyone who had used the console before a
//   new core tab shipped would simply never see it, with no error and nothing
//   to click. (That merge bug is fixed separately in layoutStore.) Putting
//   navigation in the header sidesteps persisted state entirely: it is always
//   present, for everyone, on first load.

import { useCallback, useEffect, useState } from 'react';
import { Activity, SlidersHorizontal, ShieldCheck, ShieldAlert, ShieldX } from 'lucide-react';

type Verdict = 'READY' | 'DEGRADED' | 'BLOCKED' | 'UNKNOWN';

const VERDICT_STYLE: Record<Verdict, { cls: string; Icon: typeof ShieldCheck; label: string }> = {
  READY:    { cls: 'bg-success/10 text-success border-success/30',           Icon: ShieldCheck, label: 'Ready' },
  DEGRADED: { cls: 'bg-warning/10 text-warning border-warning/30',           Icon: ShieldAlert, label: 'Degraded' },
  BLOCKED:  { cls: 'bg-destructive/10 text-destructive border-destructive/30', Icon: ShieldX,   label: 'Blocked' },
  UNKNOWN:  { cls: 'bg-muted text-muted-foreground border-border',           Icon: Activity,    label: 'Checking' },
};

export function SystemNav() {
  const [verdict, setVerdict] = useState<Verdict>('UNKNOWN');
  const [counts, setCounts] = useState<{ block: number; warn: number } | null>(null);

  const poll = useCallback(async () => {
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      const j = await r.json();
      setVerdict((j?.verdict as Verdict) ?? 'UNKNOWN');
      if (j?.counts) setCounts({ block: j.counts.block, warn: j.counts.warn });
    } catch {
      setVerdict('UNKNOWN');
    }
  }, []);

  useEffect(() => {
    void poll();
    // 2 min: the underlying data only changes when the pipeline runs or a
    // position moves. Polling harder would just burn Supabase round-trips.
    const t = setInterval(() => void poll(), 120000);
    return () => clearInterval(t);
  }, [poll]);

  const v = VERDICT_STYLE[verdict];
  const Icon = v.Icon;

  return (
    <div className="flex items-center gap-1.5">
      {/* Readiness verdict — the whole point of putting this in the header */}
      <a
        href="/health"
        title={
          counts
            ? `Preflight: ${counts.block} blocker(s), ${counts.warn} warning(s)`
            : 'Open Preflight'
        }
        className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors hover:opacity-80 ${v.cls}`}
      >
        <Icon className="h-3.5 w-3.5" />
        <span className="hidden sm:inline">{v.label}</span>
        {counts && counts.block > 0 && (
          <span className="rounded-full bg-destructive px-1.5 text-[10px] font-bold text-white">
            {counts.block}
          </span>
        )}
      </a>

      <a
        href="/control"
        title="Strategy Control Room — engine tuning and lifecycle"
        className="flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        <SlidersHorizontal className="h-3.5 w-3.5" />
        <span className="hidden md:inline">Control Room</span>
      </a>
    </div>
  );
}
