'use client';

// components/core/AlertRail.tsx
//
// The in-dashboard alert channel. Mounted globally so it follows you across
// every tab and both new surfaces — an EXIT_STOP must not depend on which page
// you happen to be looking at.
//
// It complements Telegram and Discord rather than replacing them: those reach
// you away from the desk, this one reaches you while you are already looking
// at the position and can act in the same glance.

import { useState } from 'react';
import { useLiveAlerts, type LiveAlert } from '@/lib/useLiveAlerts';

const TONE: Record<LiveAlert['severity'], string> = {
  critical: 'border-rose-500/40 bg-rose-500/10 text-rose-200',
  action:   'border-amber-500/40 bg-amber-500/10 text-amber-200',
  info:     'border-zinc-700 bg-zinc-900/80 text-zinc-300',
};

export function AlertRail() {
  const { alerts, connected, notifyPermission, requestNotifications, dismiss } = useLiveAlerts();
  const [open, setOpen] = useState(true);

  const actionable = alerts.filter((a) => a.severity !== 'info');
  if (!alerts.length && notifyPermission === 'granted') return null;

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(23rem,calc(100vw-2rem))] flex-col gap-2">
      {/* Ask once, only when there is something worth being interrupted for. */}
      {notifyPermission !== 'granted' && actionable.length > 0 && (
        <div className="pointer-events-auto rounded-lg border border-sky-500/40 bg-sky-500/10 p-3 text-[11px] text-sky-200">
          <p className="mb-2 leading-relaxed">
            Turn on browser notifications so stop and target alerts reach you even
            when this tab is in the background.
          </p>
          <button
            onClick={() => void requestNotifications()}
            className="rounded bg-sky-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-sky-500"
          >
            Enable notifications
          </button>
        </div>
      )}

      {alerts.length > 0 && (
        <div className="pointer-events-auto overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/95 shadow-xl backdrop-blur">
          <button
            onClick={() => setOpen((v) => !v)}
            className="flex w-full items-center justify-between gap-2 border-b border-zinc-800 px-3 py-2 text-left"
          >
            <span className="flex items-center gap-2 text-[11px] font-medium text-zinc-200">
              <span
                className={`h-1.5 w-1.5 rounded-full ${connected ? 'bg-emerald-400' : 'bg-zinc-600'}`}
                title={connected ? 'Live' : 'Reconnecting'}
              />
              Alerts
              {actionable.length > 0 && (
                <span className="rounded-full bg-rose-500 px-1.5 text-[10px] font-bold text-white">
                  {actionable.length}
                </span>
              )}
            </span>
            <span className="text-[11px] text-zinc-500">{open ? 'hide' : 'show'}</span>
          </button>

          {open && (
            <div className="max-h-[22rem] divide-y divide-zinc-800/70 overflow-y-auto">
              {alerts.map((a) => (
                <div key={a.id} className={`flex items-start gap-2 border-l-2 px-3 py-2 ${TONE[a.severity]}`}>
                  <div className="min-w-0 flex-1">
                    <div className="text-[11px] font-medium leading-snug">{a.title}</div>
                    <div className="mt-0.5 font-mono text-[10px] opacity-80">{a.body}</div>
                  </div>
                  <button
                    onClick={() => dismiss(a.id)}
                    aria-label={`Dismiss ${a.title}`}
                    className="shrink-0 px-1 text-[13px] leading-none opacity-50 hover:opacity-100"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
