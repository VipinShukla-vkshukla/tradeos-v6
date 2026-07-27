'use client';

// lib/useLiveAlerts.ts
//
// Live position and data alerts inside the dashboard, alongside Telegram and
// Discord rather than instead of them. Telegram remains the away-from-desk
// channel; this is the at-desk one, where you can act in the same glance.
//
// SOURCE OF TRUTH
//   The backend already writes everything needed. position_lifecycle sets
//   open_positions.exit_signal and action_required when the exit policy fires;
//   the quality monitor and event monitor write data_anomalies. Subscribing to
//   those two tables means the dashboard shows exactly what the pipeline
//   decided — it never re-derives an opinion of its own, so the rail and the
//   Telegram message can never disagree.
//
// DEDUPLICATION
//   Realtime replays and reconnects re-deliver rows. Each alert carries a
//   stable id derived from its content, and fired notification ids are held in
//   a ref so a reconnect cannot re-notify for something already seen.

import { useCallback, useEffect, useRef, useState } from 'react';
import { supabase } from '@/lib/supabase';

export type AlertKind =
  | 'EXIT_STOP' | 'TARGET_HIT' | 'BOOK_PARTIAL' | 'EXIT_TIME'
  | 'TRAIL_UPDATE' | 'ANOMALY' | 'POSITION_OPENED' | 'POSITION_CLOSED';

export interface LiveAlert {
  id: string;
  kind: AlertKind;
  severity: 'critical' | 'action' | 'info';
  symbol: string | null;
  title: string;
  body: string;
  at: string;
}

const KIND_META: Record<AlertKind, { severity: LiveAlert['severity']; icon: string }> = {
  EXIT_STOP:       { severity: 'critical', icon: '🔴' },
  TARGET_HIT:      { severity: 'action',   icon: '🎯' },
  BOOK_PARTIAL:    { severity: 'action',   icon: '💰' },
  EXIT_TIME:       { severity: 'action',   icon: '⏰' },
  TRAIL_UPDATE:    { severity: 'info',     icon: '📈' },
  POSITION_OPENED: { severity: 'info',     icon: '📂' },
  POSITION_CLOSED: { severity: 'info',     icon: '📕' },
  ANOMALY:         { severity: 'critical', icon: '⚠️' },
};

function hash(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  return Math.abs(h).toString(36);
}

function alertFromPosition(row: Record<string, unknown>): LiveAlert | null {
  const sym = (row.symbol as string) ?? null;
  const exitSignal = (row.exit_signal as string) || '';
  const action = (row.action_required as string) || '';
  if (!sym || (!exitSignal && !action)) return null;

  let kind: AlertKind | null = null;
  if (/STOP_LOSS_HIT|TRAIL_SL_HIT/i.test(exitSignal)) kind = 'EXIT_STOP';
  else if (/TARGET_HIT/i.test(exitSignal)) kind = 'TARGET_HIT';
  else if (/TIME_EXIT/i.test(exitSignal)) kind = 'EXIT_TIME';
  else if (/^BOOK\b/i.test(action)) kind = 'BOOK_PARTIAL';
  else if (/^EXIT\b/i.test(action)) kind = 'EXIT_STOP';
  if (!kind) return null;

  const ltp = row.current_price != null ? `₹${Number(row.current_price).toFixed(2)}` : '';
  const r = row.r_multiple_current != null ? `${Number(row.r_multiple_current).toFixed(2)}R` : '';
  const pnl = row.pnl_pct != null ? `${Number(row.pnl_pct) >= 0 ? '+' : ''}${Number(row.pnl_pct).toFixed(2)}%` : '';

  return {
    id: hash(`${sym}|${kind}|${exitSignal}|${action}`),
    kind,
    severity: KIND_META[kind].severity,
    symbol: sym,
    title: `${KIND_META[kind].icon} ${kind.replace(/_/g, ' ')} — ${sym}`,
    body: [ltp, pnl, r].filter(Boolean).join(' · ') || action || exitSignal,
    at: new Date().toISOString(),
  };
}

function alertFromAnomaly(row: Record<string, unknown>): LiveAlert | null {
  if ((row.severity as string) !== 'ERROR') return null;
  const msg = (row.message as string) || (row.check_name as string) || 'Data anomaly';
  return {
    id: hash(`anom|${row.check_name}|${row.date}|${msg}`),
    kind: 'ANOMALY',
    severity: 'critical',
    symbol: null,
    title: `⚠️ ${row.check_name ?? 'Data anomaly'}`,
    body: msg,
    at: new Date().toISOString(),
  };
}

export function useLiveAlerts(max = 40) {
  const [alerts, setAlerts] = useState<LiveAlert[]>([]);
  const [connected, setConnected] = useState(false);
  const [notifyPermission, setNotifyPermission] =
    useState<NotificationPermission>('default');
  const notified = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (typeof Notification !== 'undefined') setNotifyPermission(Notification.permission);
  }, []);

  const requestNotifications = useCallback(async () => {
    if (typeof Notification === 'undefined') return 'denied' as NotificationPermission;
    const p = await Notification.requestPermission();
    setNotifyPermission(p);
    return p;
  }, []);

  const push = useCallback((a: LiveAlert | null) => {
    if (!a) return;
    setAlerts((prev) => (prev.some((x) => x.id === a.id) ? prev : [a, ...prev].slice(0, max)));

    // Only interrupt for things that need a decision. A trailing-stop update
    // is useful context in the rail but is not worth an OS-level popup.
    if (a.severity === 'info') return;
    if (notified.current.has(a.id)) return;
    notified.current.add(a.id);

    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      try {
        const n = new Notification(a.title, { body: a.body, tag: a.id, requireInteraction: a.severity === 'critical' });
        n.onclick = () => { window.focus(); n.close(); };
      } catch { /* some browsers block construction outside a SW; the rail still shows it */ }
    }
  }, [max]);

  useEffect(() => {
    if (!supabase) return;

    const ch = supabase
      .channel('tradeos-live-alerts')
      .on('postgres_changes',
        { event: '*', schema: 'public', table: 'open_positions' },
        (p) => {
          const row = (p.new ?? {}) as Record<string, unknown>;
          if (p.eventType === 'DELETE') return;
          push(alertFromPosition(row));
        })
      .on('postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'data_anomalies' },
        (p) => push(alertFromAnomaly((p.new ?? {}) as Record<string, unknown>)))
      .subscribe((status) => setConnected(status === 'SUBSCRIBED'));

    return () => { void supabase?.removeChannel(ch); };
  }, [push]);

  // Seed from current state so opening the dashboard mid-session shows what is
  // already outstanding, not just what changes from this moment on.
  useEffect(() => {
    if (!supabase) return;
    void (async () => {
      const { data } = await supabase!
        .from('open_positions')
        .select('symbol,exit_signal,action_required,current_price,pnl_pct,r_multiple_current')
        .eq('status', 'ACTIVE');
      for (const row of data ?? []) {
        const a = alertFromPosition(row as Record<string, unknown>);
        if (a) {
          // Seeded alerts populate the rail but must not fire a notification —
          // they may be hours old.
          notified.current.add(a.id);
          setAlerts((prev) => (prev.some((x) => x.id === a.id) ? prev : [a, ...prev].slice(0, max)));
        }
      }
    })();
  }, [max]);

  const dismiss = useCallback((id: string) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id));
  }, []);

  return { alerts, connected, notifyPermission, requestNotifications, dismiss };
}
