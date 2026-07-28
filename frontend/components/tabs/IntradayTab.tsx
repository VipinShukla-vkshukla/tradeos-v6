'use client';

/**
 * The live session view.
 *
 * Answers, in order: is the monitor actually running, what has it told me
 * today, what did it do at the broker, and how much autonomy is switched on.
 *
 * Built on the same Panel / KPICard / DataGuard primitives as the swing tabs
 * rather than a parallel set — the two modes should feel like one console
 * showing different horizons, not two products.
 *
 * Reads only what backend/intraday writes: notified alerts and broker writes.
 * There are no tick or per-evaluation tables to render because the subsystem
 * deliberately does not store them (~900k ticks/day would consume the entire
 * free tier in a week for data nobody queries).
 */

import { useState, useEffect, useMemo, useCallback } from 'react';
import {
  Activity, Bell, ShieldCheck, AlertTriangle, Radio, Clock, Ban,
} from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonKPI, SkeletonTable } from '@/components/core/DataGuard';
import { queries } from '@/lib/supabase';

interface Alert {
  id: number; ts: string; symbol: string; kind: string; urgency: string | null;
  headline: string; detail: string | null; ltp: number | null;
  r_multiple: number | null; acknowledged: boolean;
}
interface BrokerRow {
  id: number; ts: string; symbol: string; channel: string; action: string;
  side: string | null; ref_id: string | null; price: number | null;
  quantity: number | null; detail: string | null;
}
interface Beat { id: number; ts: string; summary: string | null; alerts_sent: number }
interface SetupRow {
  id: number; ts: string; trade_date: string; symbol: string; strategy: string;
  phase: string | null; entry: number | null; stop: number | null; target: number | null;
  risk_pct: number | null; reward_pct: number | null; rr: number | null;
  confidence: number | null; rationale: string | null; invalidation: string | null;
  cost_pct: number | null; cost_verdict: string | null; corroborated_by: string | null;
  outcome: string | null; outcome_pct: number | null;
}
interface ScoreRow {
  strategy: string; setups: number; taken: number; wins: number; losses: number;
  scratches: number; hit_rate_pct: number | null; avg_net_pct: number | null;
  avg_confidence: number | null; last_seen: string | null;
}

/** What each engine looks for — so the code names mean something on screen. */
const ENGINE_LABEL: Record<string, string> = {
  ORB: 'Opening range break',
  GAP: 'Gap held',
  PDL: "Prev-day high retest",
  VCE: 'Squeeze release',
  PBK: 'Trend pullback',
  VWR: 'VWAP reclaim',
  RNG: 'Range low',
};
const OUTCOME_TONE: Record<string, string> = {
  TARGET: 'bg-profit/15 text-profit',
  STOP: 'bg-loss/15 text-loss',
  TIMEOUT: 'bg-muted text-muted-foreground',
};

const URGENCY: Record<string, string> = {
  CRITICAL: 'bg-red-500/15 text-red-400',
  NORMAL: 'bg-blue-500/15 text-blue-400',
  INFO: 'bg-muted text-muted-foreground',
};
const ACTION_TONE: Record<string, string> = {
  PLACED: 'text-profit', MODIFIED: 'text-blue-400', CANCELLED: 'text-muted-foreground',
  BLOCKED: 'text-yellow-400', FAILED: 'text-loss', PLACE_FAILED: 'text-loss',
  MODIFY_FAILED: 'text-loss',
};

function timeIST(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString('en-IN', {
      hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata',
    });
  } catch { return ts.slice(11, 16); }
}

export function IntradayTab() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [broker, setBroker] = useState<BrokerRow[]>([]);
  const [beat, setBeat] = useState<Beat | null>(null);
  const [gates, setGates] = useState<Record<string, string>>({});
  const [setups, setSetups] = useState<SetupRow[]>([]);
  const [scores, setScores] = useState<ScoreRow[]>([]);
  const [showRejected, setShowRejected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [a, b, h, g, su, sc] = await Promise.all([
        queries.getIntradayAlerts(60),
        queries.getIntradayBrokerLog(40),
        queries.getIntradayHeartbeat(),
        queries.getIntradayGates(),
        queries.getIntradaySetups(60),
        queries.getIntradayScorecard(),
      ]);
      if (a.error) throw a.error;
      setAlerts((a.data ?? []) as Alert[]);
      setBroker((b.data ?? []) as BrokerRow[]);
      setBeat(((h.data ?? [])[0] ?? null) as Beat | null);
      setGates(g);
      setSetups((su.data ?? []) as SetupRow[]);
      setScores((sc.data ?? []) as ScoreRow[]);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load intraday data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    // The daemon writes on its own schedule, so poll rather than subscribe —
    // one query every 30s is far cheaper than holding realtime channels open
    // for three low-volume tables.
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  // ── Liveness ────────────────────────────────────────────────────────────
  // Staleness is measured against the heartbeat interval (15 min), not against
  // the clock, so a daemon that is correctly asleep outside market hours is not
  // reported as broken.
  const beatAgeMin = useMemo(() => {
    if (!beat?.ts) return null;
    return (Date.now() - new Date(beat.ts).getTime()) / 60000;
  }, [beat]);
  const live = beatAgeMin != null && beatAgeMin < 20;

  // Is the daemon SUPPOSED to be running right now? Outside 09:00-15:40 IST on
  // a weekday it is not, and flagging a correctly-idle monitor in amber is the
  // same cry-wolf failure as warning about a bhavcopy four hours before the
  // pipeline that fetches it. A warning that fires every evening teaches you to
  // ignore the one that fires at 10:15 on a Tuesday.
  const expectedUp = useMemo(() => {
    const ist = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000);
    if (ist.getDay() === 0 || ist.getDay() === 6) return false;
    const mins = ist.getHours() * 60 + ist.getMinutes();
    return mins >= 9 * 60 && mins <= 15 * 60 + 40;
  }, [beat]);
  const idleOffHours = !live && !expectedUp;

  const phase = parseFloat(gates['intraday_autonomy_phase'] ?? '2');
  const gttOn = (gates['intraday_gtt_enabled'] ?? 'false') === 'true';
  const ordersOn = (gates['intraday_orders_enabled'] ?? 'false') === 'true';

  const today = new Date().toISOString().slice(0, 10);
  const todays = alerts.filter((a) => (a.ts ?? '').slice(0, 10) === today);
  const critical = todays.filter((a) => a.urgency === 'CRITICAL').length;
  const blocked = broker.filter((b) => b.action === 'BLOCKED').length;
  const taken = setups.filter((s) => s.cost_verdict === 'TAKEN').length;
  const rejected = setups.filter((s) => s.cost_verdict === 'REJECTED_COST').length;
  const visibleSetups = showRejected
    ? setups : setups.filter((s) => s.cost_verdict !== 'REJECTED_COST');
  const errObj = error ? new Error(error) : null;

  return (
    <div className="space-y-4">
      {/* Monitor state — the first question is always "is it running" */}
      <div className={`rounded-xl border p-4 ${live
        ? 'border-emerald-500/30 bg-emerald-500/5'
        : idleOffHours
          ? 'border-border/60 bg-panel-hover/30'
          : 'border-amber-500/40 bg-amber-500/5'}`}>
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div className="flex items-start gap-3">
            <Radio className={`h-5 w-5 mt-0.5 ${live ? 'text-emerald-400'
              : idleOffHours ? 'text-muted-foreground' : 'text-amber-400'}`} />
            <div>
              <div className="font-semibold text-sm">
                {live ? 'Intraday monitor is live'
                  : idleOffHours ? 'Intraday monitor is idle — outside market hours'
                  : 'Intraday monitor is not reporting'}
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                {beat
                  ? <>Last heartbeat {timeIST(beat.ts)} IST
                      {beatAgeMin != null && ` (${Math.round(beatAgeMin)} min ago)`}
                      {beat.summary && ` · ${beat.summary}`}</>
                  : <>No heartbeat recorded yet. Start it with <code
                      className="px-1 rounded bg-muted">python -m intraday.run</code> during
                      market hours.</>}
              </div>
              {idleOffHours && (
                <div className="text-xs text-muted-foreground mt-1">
                  Expected — the daemon runs 09:00–15:40 IST on trading days. Broker-side GTT
                  stops, once enabled, keep protecting positions while it is off.
                </div>
              )}
              {!live && !idleOffHours && beat && (
                <div className="text-xs text-amber-400 mt-1">
                  The market is open and the monitor should be reporting. Silence is only
                  meaningful when the daemon is alive — with no heartbeat, &quot;no
                  alerts&quot; cannot be distinguished from &quot;not running&quot;.
                </div>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1.5 text-[11px]">
            <span className="px-2 py-1 rounded bg-muted text-muted-foreground">
              Phase {phase.toFixed(1)}
            </span>
            <span className={`px-2 py-1 rounded ${gttOn
              ? 'bg-profit/15 text-profit' : 'bg-muted text-muted-foreground'}`}
              title="Phase 2.5 — stop-loss GTTs resting at Zerodha, surviving this process dying">
              GTT {gttOn ? 'on' : 'off'}
            </span>
            <span className={`px-2 py-1 rounded ${ordersOn
              ? 'bg-red-500/15 text-red-400' : 'bg-muted text-muted-foreground'}`}
              title="Phase 3 — automatic order placement">
              Orders {ordersOn ? 'LIVE' : 'off'}
            </span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? (
          <><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /><SkeletonKPI /></>
        ) : (
          <>
            <KPICard title="Alerts Today" value={todays.length.toString()}
              icon={<Bell className="h-4 w-4" />}
              description={todays.length === 0
                ? (live ? 'Nothing has changed — silence is the normal state' : 'Monitor not running')
                : `${critical} need action now`}
              change={critical > 0
                ? { value: `${critical} critical`, type: 'decrease' } : undefined} />
            <KPICard title="Broker Writes" value={broker.filter((b) => b.action === 'PLACED' || b.action === 'MODIFIED').length.toString()}
              icon={<ShieldCheck className="h-4 w-4" />}
              description={gttOn ? 'GTT stops placed or raised' : 'GTT disabled — stops are notional'} />
            <KPICard title="Blocked" value={blocked.toString()}
              icon={<Ban className="h-4 w-4" />}
              description={blocked > 0 ? 'Orders stopped by a risk rail' : 'No rail triggered'}
              change={blocked > 0 ? { value: 'review below', type: 'decrease' } : undefined} />
            <KPICard title="Autonomy" value={phase >= 3 ? 'Phase 3' : phase >= 2.5 ? 'Phase 2.5' : 'Phase 2'}
              icon={<Activity className="h-4 w-4" />}
              description={phase >= 3 ? 'Orders can be placed automatically'
                : phase >= 2.5 ? 'Broker-side stops, no order placement'
                : 'Monitor and notify only'} />
          </>
        )}
      </div>

      {/* Alert feed — what the monitor has told you */}
      <Panel title="Live Alert Feed"
        description="One row per change in what you should do — not a fixed-interval digest"
        dataSource="supabase" tableName="intraday_alerts" isLoading={loading}>
        <DataGuard data={alerts} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={5} cols={4} />}
          emptyTitle="No alerts yet"
          emptyDescription="Alerts appear when a position or candidate changes state — a stop approached, a partial triggered, a limit reached. Silence means nothing changed.">
          {(data) => (
            <div className="space-y-1.5">
              {data.map((a) => (
                <div key={a.id}
                  className="flex items-start gap-3 rounded-lg border border-border/40 p-2.5 hover:bg-panel-hover">
                  <span className="text-[10px] text-muted-foreground font-mono mt-0.5 shrink-0 w-11">
                    {timeIST(a.ts)}
                  </span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 ${
                    URGENCY[a.urgency ?? 'INFO'] ?? URGENCY.INFO}`}>
                    {a.kind.replace(/_/g, ' ')}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs">
                      <span className="font-semibold">{a.symbol}</span>
                      <span className="text-muted-foreground"> — {a.headline}</span>
                    </div>
                    {a.detail && (
                      <div className="text-[10px] text-muted-foreground mt-0.5 whitespace-pre-line">
                        {a.detail}
                      </div>
                    )}
                  </div>
                  <div className="text-right shrink-0 font-mono text-[10px]">
                    {a.ltp != null && <div>₹{a.ltp.toFixed(2)}</div>}
                    {a.r_multiple != null && (
                      <div className={a.r_multiple >= 0 ? 'text-profit' : 'text-loss'}>
                        {a.r_multiple >= 0 ? '+' : ''}{a.r_multiple.toFixed(2)}R
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </DataGuard>
      </Panel>

      {/* Setups — what the seven engines found, including cost rejections */}
      <Panel title="Intraday Setups"
        description="Every setup the engines detected today, including the ones costs ruled out"
        dataSource="supabase" tableName="intraday_setups" isLoading={loading}>
        <DataGuard data={visibleSetups} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={4} cols={7} />}
          emptyTitle="No setups today"
          emptyDescription="Engines evaluate only during PRIME, DRIFT and AFTERNOON, and only when the index gate allows longs. Nothing found is a normal result.">
          {(data) => (
            <div className="space-y-3">
              <div className="flex items-center gap-3 flex-wrap text-xs">
                <span className={taken > 0 ? 'text-profit font-semibold' : 'text-muted-foreground font-semibold'}>
                  {taken} of {setups.length} cleared the cost gate
                </span>
                {rejected > 0 && (
                  <span className="px-1.5 py-0.5 rounded bg-muted text-muted-foreground">
                    {rejected} rejected on cost
                  </span>
                )}
                {rejected > 0 && (
                  <button className="ml-auto text-muted-foreground hover:text-foreground"
                    onClick={() => setShowRejected((v) => !v)}>
                    {showRejected ? 'Hide' : 'Show'} cost rejections
                  </button>
                )}
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border/30 bg-panel-hover/50">
                      {['Time', 'Symbol', 'Engine', 'Entry', 'Stop', 'Target', 'R:R', 'Cost', 'Result'].map((h) => (
                        <th key={h} className={`py-2 px-3 font-medium text-muted-foreground whitespace-nowrap ${
                          ['Time', 'Symbol', 'Engine'].includes(h) ? 'text-left' : 'text-right'}`}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.map((s) => (
                      <tr key={s.id} className={`border-b border-border/20 hover:bg-panel-hover ${
                        s.cost_verdict === 'REJECTED_COST' ? 'opacity-60' : ''}`}>
                        <td className="py-2 px-3 font-mono text-muted-foreground">{timeIST(s.ts)}</td>
                        <td className="py-2 px-3 font-semibold">{s.symbol}</td>
                        <td className="py-2 px-3">
                          <span title={s.rationale ?? ''}>{s.strategy}</span>
                          <div className="text-[10px] text-muted-foreground">
                            {ENGINE_LABEL[s.strategy] ?? s.phase}
                            {s.corroborated_by && ` · +${s.corroborated_by}`}
                          </div>
                        </td>
                        <td className="text-right py-2 px-3 font-mono">₹{s.entry?.toFixed(2) ?? '—'}</td>
                        <td className="text-right py-2 px-3 font-mono text-loss">₹{s.stop?.toFixed(2) ?? '—'}</td>
                        <td className="text-right py-2 px-3 font-mono text-profit">₹{s.target?.toFixed(2) ?? '—'}</td>
                        <td className="text-right py-2 px-3 font-mono">{s.rr?.toFixed(1) ?? '—'}</td>
                        <td className={`text-right py-2 px-3 font-mono ${
                          s.cost_verdict === 'REJECTED_COST' ? 'text-loss' : 'text-muted-foreground'}`}
                          title={s.cost_verdict === 'REJECTED_COST'
                            ? 'Costs ate too much of the target for this to be worth taking'
                            : undefined}>
                          {s.cost_pct != null ? `${s.cost_pct.toFixed(2)}%` : '—'}
                        </td>
                        <td className="text-right py-2 px-3">
                          {s.outcome
                            ? <span className={`px-1.5 py-0.5 rounded ${OUTCOME_TONE[s.outcome] ?? ''}`}>
                                {s.outcome}{s.outcome_pct != null && ` ${s.outcome_pct > 0 ? '+' : ''}${s.outcome_pct.toFixed(2)}%`}
                              </span>
                            : <span className="text-muted-foreground">pending</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </DataGuard>
      </Panel>

      {/* Engine scorecard — the evidence a lifecycle state should rest on */}
      <Panel title="Engine Scorecard"
        description="Hit rate and net expectancy per engine — counting setups detected, not trades taken"
        dataSource="supabase" tableName="v_intraday_engine_scorecard" isLoading={loading}>
        <DataGuard data={scores} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={4} cols={6} />}
          emptyTitle="No resolved setups yet"
          emptyDescription="Outcomes are resolved after the close with: python -m intraday.outcomes">
          {(data) => (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border/30 bg-panel-hover/50">
                    {['Engine', 'Looks for', 'Setups', 'Taken', 'Hit rate', 'Avg net'].map((h) => (
                      <th key={h} className={`py-2 px-3 font-medium text-muted-foreground ${
                        ['Engine', 'Looks for'].includes(h) ? 'text-left' : 'text-right'}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.map((r) => (
                    <tr key={r.strategy} className="border-b border-border/20 hover:bg-panel-hover">
                      <td className="py-2 px-3 font-semibold">{r.strategy}</td>
                      <td className="py-2 px-3 text-muted-foreground">{ENGINE_LABEL[r.strategy] ?? '—'}</td>
                      <td className="text-right py-2 px-3 font-mono">{r.setups}</td>
                      <td className="text-right py-2 px-3 font-mono text-muted-foreground">{r.taken}</td>
                      <td className={`text-right py-2 px-3 font-mono ${
                        (r.hit_rate_pct ?? 0) >= 50 ? 'text-profit' : 'text-muted-foreground'}`}>
                        {r.hit_rate_pct != null ? `${r.hit_rate_pct.toFixed(0)}%` : '—'}
                      </td>
                      <td className={`text-right py-2 px-3 font-mono font-semibold ${
                        r.avg_net_pct == null ? 'text-muted-foreground'
                          : r.avg_net_pct >= 0 ? 'text-profit' : 'text-loss'}`}
                        title="Already net of the round trip — a gross win under 0.21% is a loss">
                        {r.avg_net_pct != null
                          ? `${r.avg_net_pct > 0 ? '+' : ''}${r.avg_net_pct.toFixed(2)}%` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DataGuard>
      </Panel>

      {/* Broker log — including what was refused, which is the useful half */}
      <Panel title="Broker Activity"
        description="Every GTT and order attempt, including the ones a risk rail refused"
        dataSource="supabase" tableName="intraday_broker_log" isLoading={loading}>
        <DataGuard data={broker} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={4} cols={6} />}
          emptyTitle="No broker activity"
          emptyDescription="Nothing has been sent to Zerodha. At Phase 2.0 that is expected — the monitor notifies but never writes.">
          {(data) => (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border/30 bg-panel-hover/50">
                    {['Time', 'Symbol', 'Channel', 'Action', 'Qty', 'Price', 'Detail'].map((h) => (
                      <th key={h} className={`py-2 px-3 font-medium text-muted-foreground ${
                        ['Qty', 'Price'].includes(h) ? 'text-right' : 'text-left'}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.map((b) => (
                    <tr key={b.id} className="border-b border-border/20 hover:bg-panel-hover">
                      <td className="py-2 px-3 font-mono text-muted-foreground">{timeIST(b.ts)}</td>
                      <td className="py-2 px-3 font-semibold">{b.symbol}</td>
                      <td className="py-2 px-3 text-muted-foreground">{b.channel}</td>
                      <td className={`py-2 px-3 font-medium ${ACTION_TONE[b.action] ?? ''}`}>
                        {b.action}{b.side ? ` ${b.side}` : ''}
                      </td>
                      <td className="py-2 px-3 text-right font-mono">{b.quantity ?? '—'}</td>
                      <td className="py-2 px-3 text-right font-mono">
                        {b.price != null ? `₹${b.price.toFixed(2)}` : '—'}
                      </td>
                      <td className="py-2 px-3 text-muted-foreground max-w-[260px] truncate"
                        title={b.detail ?? ''}>{b.detail ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </DataGuard>
      </Panel>

      <div className="flex items-start gap-2 text-[11px] text-muted-foreground">
        <Clock className="h-3.5 w-3.5 mt-0.5 shrink-0" />
        <span>
          The monitor runs 09:00–15:40 IST on trading days and is not meant to run around the
          clock: NSE trades 31 of the 168 hours in a week, and the Kite session expires daily
          at 07:30 regardless. Ticks are held in memory and never stored — only decisions are.
        </span>
      </div>

      {!loading && alerts.length === 0 && broker.length === 0 && (
        <div className="flex items-start gap-2 text-[11px] text-amber-400">
          <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>
            Both tables are empty. If you have run the daemon, check that migration
            011_intraday_subsystem.sql has been applied.
          </span>
        </div>
      )}
    </div>
  );
}

export default IntradayTab;
