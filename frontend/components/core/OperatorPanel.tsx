'use client';

/**
 * Every switch that decides what the system is allowed to do, in one place.
 *
 * WHY THESE AND NOT ALL 260 CONFIG KEYS
 * -------------------------------------
 * A panel exposing everything is a panel nobody reads carefully. These are the
 * switches that change whether money moves, plus the caps that bound how much.
 * Tuning thresholds stays in the Control Room table where it belongs — the
 * difference is that a wrong threshold produces a worse trade, while a wrong
 * switch here produces a trade you never intended.
 *
 * PERSISTENCE IS NOT A FEATURE HERE
 * ---------------------------------
 * Every control writes to system_config, which is the same row the Python reads
 * through cfg(). There is no local state, no cache, and nothing to sync: what
 * you set today is what tomorrow's daemon reads, because there is only one copy
 * and it lives in the database. The panel re-reads after every write for the
 * same reason the CLI does — "I set it" and "it is set" are different claims.
 *
 * DESTRUCTIVE CONTROLS ASK TWICE
 * ------------------------------
 * Going LIVE and clearing the kill switch both take a second click. Not because
 * a click is hard, but because those two are the only controls here whose
 * mistake is discovered by losing money rather than by reading the screen.
 */

import { useState, useEffect, useCallback } from 'react';
import { Shield, Power, AlertTriangle, RefreshCw, Check } from 'lucide-react';
import { Panel } from '@/components/core/Panel';
import { supabase } from '@/lib/supabase';

type Cfg = Record<string, string>;

const KEYS = [
  'master_kill_switch', 'intraday_autonomy_phase', 'intraday_gtt_enabled',
  'intraday_orders_enabled',
  'swing_trading_mode', 'swing_auto_exit', 'swing_auto_entry', 'swing_live_auto_entry',
  'swing_max_order_value', 'swing_max_orders_per_day', 'swing_max_notional_per_day',
  'intraday_trading_mode', 'intraday_auto_exit', 'intraday_auto_entry',
  'intraday_live_auto_entry', 'intraday_strategies_enabled',
  'intraday_max_order_value', 'intraday_max_orders_per_day',
  'intraday_max_notional_per_day', 'intraday_structure_gate',
  'intraday_news_gate_enabled', 'exit_runners_enabled', 'exit_deterioration_enabled',
];

async function writeKey(key: string, value: string, reason: string) {
  const res = await fetch(`/api/config/${encodeURIComponent(key)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value, reason }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).message ?? 'write failed');
}

function Toggle({ on, onChange, disabled, danger }: {
  on: boolean; onChange: (v: boolean) => void; disabled?: boolean; danger?: boolean;
}) {
  return (
    <button
      role="switch"
      aria-checked={on}
      disabled={disabled}
      onClick={() => onChange(!on)}
      className={`relative h-5 w-9 rounded-full transition-colors shrink-0
        ${on ? (danger ? 'bg-red-500' : 'bg-emerald-500') : 'bg-muted'}
        ${disabled ? 'opacity-40 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform
        ${on ? 'translate-x-4' : 'translate-x-0.5'}`} />
    </button>
  );
}

function Row({ label, hint, children }: {
  label: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-border/20 last:border-0">
      <div className="min-w-0">
        <div className="text-xs font-medium">{label}</div>
        {hint && <div className="text-[10px] text-muted-foreground mt-0.5 max-w-[46ch]">{hint}</div>}
      </div>
      <div className="shrink-0 pt-0.5">{children}</div>
    </div>
  );
}

export function OperatorPanel() {
  const [cfg, setCfg] = useState<Cfg>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      // supabase is null when the env vars are absent — a panel that throws
      // on load tells you nothing about why.
      if (!supabase) {
        setErr('Supabase is not configured — check NEXT_PUBLIC_SUPABASE_URL.');
        return;
      }
      const { data } = await supabase.from('system_config')
        .select('key,value').in('key', KEYS);
      const m: Cfg = {};
      for (const r of data ?? []) m[r.key] = r.value;
      setCfg(m);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'could not read config');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const set = useCallback(async (key: string, value: string, reason: string) => {
    setBusy(key);
    try {
      await writeKey(key, value, reason);
      // Re-read rather than assume. The write may be rejected by RLS or a
      // constraint, and a panel that shows the value it *sent* rather than the
      // value that *stuck* is how a setting appears applied when it is not.
      await load();
      setSaved(key);
      setTimeout(() => setSaved((s) => (s === key ? null : s)), 1800);
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'write failed');
    } finally {
      setBusy(null);
      setConfirm(null);
    }
  }, [load]);

  const bool = (k: string) => (cfg[k] ?? 'false').toLowerCase() === 'true';
  const mode = (fw: 'swing' | 'intraday') =>
    (cfg[`${fw}_trading_mode`] ?? 'PAPER').toUpperCase();
  const killed = bool('master_kill_switch');

  if (loading) {
    return (
      <Panel title="Operator Controls" description="Loading…" isLoading>
        <div className="h-24" />
      </Panel>
    );
  }

  const framework = (fw: 'swing' | 'intraday', title: string) => {
    const m = mode(fw);
    const live = m === 'LIVE';
    const confirmKey = `${fw}_live`;
    return (
      <div className="rounded-lg border border-border/50 p-3">
        <div className="flex items-center gap-2 mb-2">
          <span className="text-sm font-semibold">{title}</span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
            live ? 'bg-red-500/20 text-red-400' : 'bg-blue-500/20 text-blue-400'}`}>
            {live ? '🔴 LIVE' : '📄 PAPER'}
          </span>
        </div>

        <Row label="Real money"
          hint={live
            ? 'Orders are sent to Zerodha. Exits and partial books execute without asking.'
            : 'Decisions run identically; fills are simulated. Nothing reaches the broker.'}>
          {confirm === confirmKey ? (
            <div className="flex gap-1">
              <button className="text-[10px] px-2 py-1 rounded bg-red-500 text-white"
                onClick={() => set(`${fw}_trading_mode`, 'LIVE', 'Operator panel: go live')}>
                Confirm LIVE
              </button>
              <button className="text-[10px] px-2 py-1 rounded border border-border"
                onClick={() => setConfirm(null)}>Cancel</button>
            </div>
          ) : (
            <Toggle on={live} danger disabled={busy !== null}
              onChange={(v) => v
                ? setConfirm(confirmKey)
                : set(`${fw}_trading_mode`, 'PAPER', 'Operator panel: back to paper')} />
          )}
        </Row>

        <Row label="Auto-exit"
          hint="Stops, targets and partial books execute automatically.">
          <Toggle on={bool(`${fw}_auto_exit`)} disabled={busy !== null}
            onChange={(v) => set(`${fw}_auto_exit`, String(v), 'Operator panel')} />
        </Row>

        <Row label="Auto-entry"
          hint={`Take ${fw} setups automatically. Honoured in PAPER; LIVE needs the separate switch below.`}>
          <Toggle on={bool(`${fw}_auto_entry`)} disabled={busy !== null}
            onChange={(v) => set(`${fw}_auto_entry`, String(v), 'Operator panel')} />
        </Row>

        <Row label="Auto-entry with real money"
          hint="Separate on purpose: promoting to live must not silently also promote &quot;simulate an entry&quot; into &quot;spend money&quot;.">
          <Toggle on={bool(`${fw}_live_auto_entry`)} danger disabled={busy !== null}
            onChange={(v) => set(`${fw}_live_auto_entry`, String(v), 'Operator panel')} />
        </Row>

        {fw === 'intraday' && (
          <Row label="Engines" hint="Off stops finding setups. Open positions are still monitored.">
            <Toggle on={bool('intraday_strategies_enabled')} disabled={busy !== null}
              onChange={(v) => set('intraday_strategies_enabled', String(v), 'Operator panel')} />
          </Row>
        )}

        <div className="grid grid-cols-3 gap-2 mt-2 pt-2 border-t border-border/20">
          {[
            [`${fw}_max_order_value`, 'Per order'],
            [`${fw}_max_orders_per_day`, 'Orders/day'],
            [`${fw}_max_notional_per_day`, 'Notional/day'],
          ].map(([k, lbl]) => (
            <div key={k}>
              <div className="text-[10px] text-muted-foreground">{lbl}</div>
              <input
                type="number"
                defaultValue={cfg[k] ?? ''}
                disabled={busy !== null}
                onBlur={(e) => {
                  const v = e.target.value.trim();
                  if (v && v !== cfg[k]) set(k, v, 'Operator panel: cap change');
                }}
                className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1
                           text-xs font-mono tabular-nums"
              />
            </div>
          ))}
        </div>
        <div className="text-[10px] text-muted-foreground mt-1">
          Caps bound every order regardless of what sizing computes — the one control
          that does not depend on capital, risk percent or ATR being right.
        </div>
      </div>
    );
  };

  return (
    <Panel
      title="Operator Controls"
      description="Everything that decides whether money moves. Saved to system_config — tomorrow's run reads exactly this."
      dataSource="supabase" tableName="system_config"
    >
      <div className="space-y-3">
        {err && (
          <div className="text-xs text-loss flex items-center gap-1.5">
            <AlertTriangle className="h-3.5 w-3.5" />{err}
          </div>
        )}

        {/* Kill switch — first, largest, unmissable */}
        <div className={`rounded-lg border p-3 ${killed
          ? 'border-red-500/50 bg-red-500/10' : 'border-border/50'}`}>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-semibold flex items-center gap-2">
                <Power className={`h-4 w-4 ${killed ? 'text-red-400' : 'text-muted-foreground'}`} />
                Master kill switch
              </div>
              <div className="text-[10px] text-muted-foreground mt-0.5">
                {killed
                  ? 'ACTIVE — nothing trades. The daemon exits at its next check.'
                  : 'Clear. Stops every order and halts the intraday daemon when set.'}
              </div>
            </div>
            {confirm === 'kill_off' ? (
              <div className="flex gap-1">
                <button className="text-[10px] px-2 py-1 rounded bg-amber-500 text-zinc-950"
                  onClick={() => set('master_kill_switch', 'false', 'Operator panel: resume')}>
                  Confirm resume
                </button>
                <button className="text-[10px] px-2 py-1 rounded border border-border"
                  onClick={() => setConfirm(null)}>Cancel</button>
              </div>
            ) : (
              <Toggle on={killed} danger disabled={busy !== null}
                onChange={(v) => v
                  ? set('master_kill_switch', 'true', 'Operator panel: STOP')
                  : setConfirm('kill_off')} />
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {framework('swing', 'Swing')}
          {framework('intraday', 'Intraday')}
        </div>

        {/* Shared gates */}
        <div className="rounded-lg border border-border/50 p-3">
          <div className="text-sm font-semibold mb-1 flex items-center gap-2">
            <Shield className="h-4 w-4 text-muted-foreground" />Safety gates
          </div>
          <Row label="Broker-side GTT stops"
            hint="Stops rest at Zerodha and fire whether or not anything of yours is running. The real answer to “what if the daemon is down”.">
            <Toggle on={bool('intraday_gtt_enabled')} disabled={busy !== null}
              onChange={(v) => set('intraday_gtt_enabled', String(v), 'Operator panel')} />
          </Row>
          <Row label="Order placement (phase 3)"
            hint="Master gate above both frameworks. Off means alerts only, everywhere.">
            <Toggle on={bool('intraday_orders_enabled')} danger disabled={busy !== null}
              onChange={(v) => set('intraday_orders_enabled', String(v), 'Operator panel')} />
          </Row>
          <Row label="Structure gate (intraday)"
            hint="Blocks setups whose swing structure is a downtrend — buying a break there is buying a lower high.">
            <Toggle on={bool('intraday_structure_gate')} disabled={busy !== null}
              onChange={(v) => set('intraday_structure_gate', String(v), 'Operator panel')} />
          </Row>
          <Row label="Event gate (intraday)"
            hint="Blocks results day, F&O ban and ASM names.">
            <Toggle on={bool('intraday_news_gate_enabled')} disabled={busy !== null}
              onChange={(v) => set('intraday_news_gate_enabled', String(v), 'Operator panel')} />
          </Row>
          <Row label="Runners (swing)"
            hint="At the target, assess the trend and either bank it or let it run. Off reverts to an unconditional exit at 3R.">
            <Toggle on={bool('exit_runners_enabled')} disabled={busy !== null}
              onChange={(v) => set('exit_runners_enabled', String(v), 'Operator panel')} />
          </Row>
          <Row label="Deterioration exit (swing)"
            hint="Exit a profitable position whose thesis broke, before the trail catches it.">
            <Toggle on={bool('exit_deterioration_enabled')} disabled={busy !== null}
              onChange={(v) => set('exit_deterioration_enabled', String(v), 'Operator panel')} />
          </Row>
        </div>

        <div className="flex items-center justify-between text-[10px] text-muted-foreground">
          <span className="flex items-center gap-1.5">
            {saved ? (<><Check className="h-3 w-3 text-profit" />saved to system_config</>)
                   : 'Changes save immediately and are read back to confirm.'}
          </span>
          <button onClick={load} className="flex items-center gap-1 hover:text-foreground">
            <RefreshCw className="h-3 w-3" />refresh
          </button>
        </div>
      </div>
    </Panel>
  );
}

export default OperatorPanel;
