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
  'swing_max_new_per_day', 'swing_alert_top_n',
  'intraday_max_new_per_day', 'intraday_alert_top_n',
  'intraday_trading_mode', 'intraday_auto_exit', 'intraday_auto_entry',
  'intraday_live_auto_entry', 'intraday_strategies_enabled',
  'intraday_max_order_value', 'intraday_max_orders_per_day',
  'intraday_max_notional_per_day', 'intraday_structure_gate',
  'intraday_news_gate_enabled', 'exit_runners_enabled', 'exit_deterioration_enabled',
  // Risk denominators. Not editable here — they are what the caps above MEAN.
  'risk_pct_per_trade', 'portfolio_max_total_risk_pct', 'intraday_max_position_pct',
  'max_position_pct',

  // ── Phase 4 (05-Aug-2026) ──────────────────────────────────────────────
  // Everything the go-live pass added or found. alloc_live_swing carries the
  // same weight as {fw}_live_auto_entry above — it is the one switch here
  // that decides whether the allocator can refuse a real-money entry — and
  // is treated with the same two-click confirm.
  'autonomy_phase',
  'alloc_shadow_enabled', 'alloc_live_intraday', 'alloc_live_swing',
  'alloc_max_slots', 'alloc_basket_recheck', 'alloc_hurdle_min_sample',
  // 044. one_framework_per_symbol is the rule that keeps a single name out of
  // both books at once — with it off, the 15:15 intraday square-off can sell
  // into a multi-week swing thesis on the same shares. alloc_hurdle_cold_start
  // is shown because an accidental 0.0 here refuses every proposal whose
  // expected R merely fails to beat its own round trip, which is how the
  // intraday book went a full session without a trade.
  'one_framework_per_symbol', 'alloc_hurdle_cold_start',
  'alloc_hurdle_lookback_days',
  'overlay_expiry_enabled', 'overlay_vol_scaling_enabled',
  'overlay_liquidity_enabled', 'overlay_liquidity_strict',
  'governance_freeze_enabled', 'governance_require_oos',
  'rank_weight_tier', 'rank_weight_conviction',
  'storage_rolloff_enabled', 'storage_staging_rolloff_enabled', 'storage_fail_pct',
  'sizing_max_cost_r', 'exit_runner_cap_enforced', 'intraday_quote_mode',
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
            // New POSITIONS is a different question from orders placed, and the
            // stricter of the two binds. Raising Orders/day to 4 while this sat
            // at 2 changed nothing — and it was invisible, so there was no way
            // to see why.
            // Both frameworks, same two concepts, same labels — one mental
            // model covers both. What differs is HOW top-N is chosen, which the
            // note below explains rather than leaving to be discovered.
            [`${fw}_max_new_per_day`, 'New positions/day'],
            [`${fw}_alert_top_n`, 'Alert top N'],
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
          {' '}<b>Orders/day</b> is the safety rail; <b>New positions/day</b> is how
          many the strategy opens. The stricter one wins.
          {fw === 'swing' ? (
            <> <b>Alert top N</b> ranks last night&apos;s plans once, so the
            day&apos;s top {cfg['swing_alert_top_n'] ?? 5} is fixed at 09:15 —
            the feed still watches every plan.</>
          ) : (
            <> <b>Alert top N</b> is a running best-of: a setup only exists once
            price makes it, so alerts flow until {cfg['intraday_alert_top_n'] ?? 5}{' '}
            are sent, then only for one that beats the weakest so far. The bar
            rises through the session.</>
          )}
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

        {/* ── RISK EXPOSURE ─────────────────────────────────────────────────
            Every control below caps position VALUE. None of them caps RISK,
            and the two are not the same number: risk is value x stop width,
            and the stop varies from 5% to 9% across this book. So "Per order
            4,000" is between 200 and 360 of risk depending on the trade, and
            nothing on this panel used to say so.

            This strip is READ-ONLY on purpose. It is not another control; it
            is the translation that makes the controls legible. The per-trade
            budget it reports is risk_pct_per_trade, which is what actually
            sizes a position (portfolio_constraints.py sizes on
            risk_budget // risk_per_share) — the caps below are a blunter
            ceiling on top of it. */}
        {/* Full width, like the kill switch above it. It was the first child of
            a two-column grid whose other children were the swing and intraday
            cards — three items in two columns, so it sat BESIDE swing and
            pushed intraday onto its own row. The two books then could not be
            read against each other, which is the whole point of showing them.
            Risk exposure describes BOTH books, so it spans both columns. */}
        <RiskExposure cfg={cfg} />

        {/* The two books, side by side and equal width, so the same control
            lines up across frameworks and a difference is visible rather than
            remembered. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 items-start">
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

        <Phase4Panel cfg={cfg} bool={bool} set={set} busy={busy}
          confirm={confirm} setConfirm={setConfirm} />

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

/**
 * What the caps above actually put at stake, in rupees.
 *
 * Answers the question the panel could not: "risk of 200 a day — where did
 * that come from?" It comes from risk_pct_per_trade x capital, it is enforced
 * in the sizing model, and it was invisible here.
 *
 * Capital is read from the same NEXT_PUBLIC_TOTAL_CAPITAL the rest of the
 * dashboard uses, falling back to 20000 so the strip degrades to "approximately
 * right" rather than blank.
 */
function RiskExposure({ cfg }: { cfg: Cfg }) {
  const num = (k: string, d: number) => {
    const v = parseFloat(cfg[k] ?? '');
    return Number.isFinite(v) ? v : d;
  };
  const capital = num('__capital__', Number(process.env.NEXT_PUBLIC_TOTAL_CAPITAL) || 20000);
  const perTradePct = num('risk_pct_per_trade', 1);
  const heatPct = num('portfolio_max_total_risk_pct', 6);
  const perTrade = (capital * perTradePct) / 100;
  const heatCap = (capital * heatPct) / 100;

  // What a day costs if every cap is hit. The stricter of orders/day and new
  // positions/day binds, then the notional cap on top.
  const daily = (fw: 'swing' | 'intraday') => {
    const per = num(`${fw}_max_order_value`, fw === 'swing' ? 4000 : 6000);
    const orders = num(`${fw}_max_orders_per_day`, 5);
    const news = num(`${fw}_max_new_per_day`, fw === 'swing' ? 3 : 5);
    const notional = num(`${fw}_max_notional_per_day`, 20000);
    // Intraday sizes on min(capital x max_position_pct, per order), so the
    // panel's per-order figure is not necessarily what binds.
    const real = fw === 'intraday'
      ? Math.min(per, (capital * num('intraday_max_position_pct', 25)) / 100)
      : per;
    return Math.min(real * orders, real * news, notional);
  };
  const swingDay = daily('swing');
  const intraDay = daily('intraday');
  const both = swingDay + intraDay;

  const rs = (n: number) =>
    `₹${n.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;

  const cells: Array<[string, string, string]> = [
    ['Risk per trade', rs(perTrade),
      `risk_pct_per_trade ${perTradePct}% of ${rs(capital)} — this is the number the sizing model enforces`],
    ['Max open heat', rs(heatCap),
      `portfolio_max_total_risk_pct ${heatPct}% — total money at stake across every open position at once`],
    ['New exposure/day', rs(both),
      `swing ${rs(swingDay)} + intraday ${rs(intraDay)}. Each book is capped separately; nothing caps the pair`],
  ];

  return (
    <div className="rounded-lg border border-border/50 p-3">
      <div className="text-sm font-semibold mb-0.5">Risk exposure</div>
      <div className="text-[10px] text-muted-foreground mb-2">
        The caps below bound position <b>value</b>. Risk is value × stop width, and the stop
        varies 5–9% — so one notional cap is a range of risk, not a number. These are the
        rupee figures those settings imply.
      </div>
      {/* Stacks on a narrow screen. It is full-width now, so three fixed
          columns would squeeze the rupee figures on a laptop half-window. */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {cells.map(([label, value, hint]) => (
          <div key={label} className="rounded border border-border/40 p-2">
            <div className="text-[10px] text-muted-foreground">{label}</div>
            <div className="font-mono text-base tabular-nums leading-tight">{value}</div>
            <div className="text-[9px] text-muted-foreground mt-0.5 leading-snug">{hint}</div>
          </div>
        ))}
      </div>
      <div className="text-[10px] text-muted-foreground mt-2">
        A cap that is larger than what the other caps allow can never bind.{' '}
        <code className="font-mono">tradeos settings</code> reports which of these controls
        currently does nothing, and what the week&apos;s evidence says each should be.
      </div>
    </div>
  );
}

/**
 * The Phase 4 controls added in the go-live pass, 05-Aug-2026.
 *
 * WHY THIS IS A SEPARATE COMPONENT RATHER THAN MORE ROWS INLINE
 * ---------------------------------------------------------------
 * OperatorPanel already reads and writes through one KEYS array, one `set`,
 * one re-read-after-write. This reuses all three via props rather than
 * duplicating them — a second copy of `writeKey`/`load` here would be a
 * second place for the "does the write stick" question to be answered
 * differently, which is the exact class of drift this project keeps finding.
 *
 * alloc_live_swing GETS THE SAME TWO-CLICK CONFIRM AS GOING LIVE
 * -----------------------------------------------------------------
 * It is the one switch on this panel that can make the allocator refuse a
 * real-money entry the greedy path would have taken. Everything else here is
 * shadow, paper, or a pure risk-reducing gate (an overlay can only make a
 * trade smaller or refuse it, never invent one) — this is the one that spends
 * differently, not just less.
 */
function Phase4Panel({ cfg, bool, set, busy, confirm, setConfirm }: {
  cfg: Cfg;
  bool: (k: string) => boolean;
  set: (key: string, value: string, reason: string) => Promise<void>;
  busy: string | null;
  confirm: string | null;
  setConfirm: (v: string | null) => void;
}) {
  const num = (k: string) => cfg[k] ?? '';
  const allocLiveSwing = bool('alloc_live_swing');
  const convictionLive = Number(cfg['rank_weight_tier'] ?? 0) > 0
                       || Number(cfg['rank_weight_conviction'] ?? 0) > 0;

  return (
    <div className="rounded-lg border border-border/50 p-3">
      <div className="text-sm font-semibold mb-1 flex items-center gap-2">
        <Shield className="h-4 w-4 text-muted-foreground" />
        Phase 4 — allocation &amp; governance
        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-500/20 text-blue-400">
          autonomy phase {cfg['autonomy_phase'] ?? '?'}
        </span>
      </div>

      {/* ── Allocator ──────────────────────────────────────────────────── */}
      <div className="text-[11px] font-medium text-muted-foreground mt-2 mb-1">Allocator</div>
      <Row label="Shadow recording"
        hint="Scores every proposal and records the verdict. Changes nothing on its own — this is what the promotion evidence is built from.">
        <Toggle on={bool('alloc_shadow_enabled')} disabled={busy !== null}
          onChange={(v) => set('alloc_shadow_enabled', String(v),
            v ? 'Operator panel' : 'Operator panel — WARNING: stops recording promotion evidence')} />
      </Row>
      <Row label="Live — intraday (paper)"
        hint="The allocator can refuse an intraday setup. Costs nothing to be wrong: this book is simulated.">
        <Toggle on={bool('alloc_live_intraday')} disabled={busy !== null}
          onChange={(v) => set('alloc_live_intraday', String(v), 'Operator panel')} />
      </Row>
      <Row label="Live — swing (real money)"
        hint="The allocator can refuse a swing entry decide() already approved. It can only SUBTRACT trades greedy would have taken, never add one — but read tools.allocator_report and tools.quote_parity after a session before turning this on.">
        {confirm === 'alloc_live_swing' ? (
          <div className="flex gap-1">
            <button className="text-[10px] px-2 py-1 rounded bg-red-500 text-white"
              onClick={() => set('alloc_live_swing', 'true', 'Operator panel: allocator live on swing')}>
              Confirm
            </button>
            <button className="text-[10px] px-2 py-1 rounded border border-border"
              onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        ) : (
          <Toggle on={allocLiveSwing} danger disabled={busy !== null}
            onChange={(v) => v
              ? setConfirm('alloc_live_swing')
              : set('alloc_live_swing', 'false', 'Operator panel: allocator off swing')} />
        )}
      </Row>
      <div className="grid grid-cols-2 gap-2 mt-1">
        <div>
          <div className="text-[10px] text-muted-foreground">Max slots/cycle</div>
          <input type="number" defaultValue={num('alloc_max_slots')} disabled={busy !== null}
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['alloc_max_slots']) set('alloc_max_slots', v, 'Operator panel'); }}
            className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground">Hurdle min sample</div>
          <input type="number" defaultValue={num('alloc_hurdle_min_sample')} disabled={busy !== null}
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['alloc_hurdle_min_sample']) set('alloc_hurdle_min_sample', v, 'Operator panel'); }}
            className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
        </div>
      </div>
      <Row label="Basket recheck"
        hint="Recheck sector caps across the allocator's OWN simultaneous picks, not just against what is already held.">
        <Toggle on={bool('alloc_basket_recheck')} disabled={busy !== null}
          onChange={(v) => set('alloc_basket_recheck', String(v), 'Operator panel')} />
      </Row>

      {/* ── 044. The two settings that took the intraday book to zero ─────── */}
      <div className="text-[10px] text-muted-foreground mt-1">
        Cold-start bar <span className="font-normal">— blank means permissive</span>
      </div>
      <input type="text" placeholder="(blank — permissive)"
        defaultValue={num('alloc_hurdle_cold_start')} disabled={busy !== null}
        onBlur={(e) => { const v = e.target.value.trim();
          if (v !== (cfg['alloc_hurdle_cold_start'] ?? ''))
            set('alloc_hurdle_cold_start', v, 'Operator panel'); }}
        className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
      {(cfg['alloc_hurdle_cold_start'] ?? '').trim() !== '' && (
        <div className="mt-1 text-[10px] text-amber-400">
          A hard floor is set. The edge this is compared against already has costs
          subtracted, so a value at or above 0 refuses every proposal whose expected R
          does not beat its own round trip — which on the intraday book is all of them.
          This is what emptied that book for a session on 05-Aug.
        </div>
      )}

      <Row label="One book per symbol"
        hint="Whichever book reaches a name first owns it until it closes. Off, the swing and intraday books can both hold the same stock — the 15:15 square-off then sells into a multi-week thesis, the account carries one idea across two sizing models that cannot see each other, and the same move is scored twice by the learning loop.">
        <Toggle on={bool('one_framework_per_symbol')} danger={!bool('one_framework_per_symbol')}
          disabled={busy !== null}
          onChange={(v) => set('one_framework_per_symbol', String(v),
            v ? 'Operator panel' : 'Operator panel — WARNING: both books may hold one name')} />
      </Row>

      {/* ── Overlays: every one of these can only reduce a trade or refuse it ── */}
      <div className="text-[11px] font-medium text-muted-foreground mt-3 mb-1">
        Structural overlays <span className="font-normal">— none of these can enlarge a trade</span>
      </div>
      <Row label="Expiry day-type sizing"
        hint="Sizes intraday down on settlement-dominated sessions. The day-type flag is a heuristic (~1-3 session error on the monthly flag) — tolerable because it only ever reduces size.">
        <Toggle on={bool('overlay_expiry_enabled')} disabled={busy !== null}
          onChange={(v) => set('overlay_expiry_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Volatility exposure scaling"
        hint="Scales BOOK-LEVEL exposure down as India VIX rises. Bands are set from this account's own observed distribution.">
        <Toggle on={bool('overlay_vol_scaling_enabled')} disabled={busy !== null}
          onChange={(v) => set('overlay_vol_scaling_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Liquidity gate"
        hint="Refuses a name that cannot be exited at plan, measured against its own traded value.">
        <Toggle on={bool('overlay_liquidity_enabled')} disabled={busy !== null}
          onChange={(v) => set('overlay_liquidity_enabled', String(v), 'Operator panel')} />
      </Row>
      {bool('overlay_liquidity_enabled') && (
        <Row label="  ↳ strict on unknown liquidity"
          hint="A name with no recorded traded value is refused rather than waved through.">
          <Toggle on={bool('overlay_liquidity_strict')} disabled={busy !== null}
            onChange={(v) => set('overlay_liquidity_strict', String(v), 'Operator panel')} />
        </Row>
      )}

      {/* ── Governance ─────────────────────────────────────────────────── */}
      <div className="text-[11px] font-medium text-muted-foreground mt-3 mb-1">Governance</div>
      <Row label="Quarterly freeze"
        hint="Parameter changes are refused inside a freeze window — the primary defence against fitting noise at 5-10 closed observations a week.">
        <Toggle on={bool('governance_freeze_enabled')} disabled={busy !== null}
          onChange={(v) => set('governance_freeze_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Require out-of-sample confirmation"
        hint="A proposal must be confirmed in a LATER window than it was fitted in. Fit in N, confirm in N+1, act in N+2.">
        <Toggle on={bool('governance_require_oos')} disabled={busy !== null}
          onChange={(v) => set('governance_require_oos', String(v), 'Operator panel')} />
      </Row>
      <Row label="Conviction layer weight"
        hint={convictionLive
          ? '⚠ NON-ZERO: an unmeasured AI tier is back in the ranking. Restore only once tier-by-tier forward returns exist from the unbiased record.'
          : 'Both weights are 0 — annotation only, pending tier-by-tier forward returns from the unbiased record.'}>
        <div className="flex gap-1">
          <input type="number" step="0.1" defaultValue={num('rank_weight_tier')} disabled={busy !== null}
            title="rank_weight_tier"
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['rank_weight_tier']) set('rank_weight_tier', v, 'Operator panel'); }}
            className={`w-14 bg-panel border rounded px-1.5 py-1 text-xs font-mono tabular-nums
              ${convictionLive ? 'border-red-500/50' : 'border-border/50'}`} />
          <input type="number" step="0.1" defaultValue={num('rank_weight_conviction')} disabled={busy !== null}
            title="rank_weight_conviction"
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['rank_weight_conviction']) set('rank_weight_conviction', v, 'Operator panel'); }}
            className={`w-14 bg-panel border rounded px-1.5 py-1 text-xs font-mono tabular-nums
              ${convictionLive ? 'border-red-500/50' : 'border-border/50'}`} />
        </div>
      </Row>

      {/* ── Storage ────────────────────────────────────────────────────── */}
      <div className="text-[11px] font-medium text-muted-foreground mt-3 mb-1">Storage</div>
      <Row label="Nightly roll-off"
        hint="Archives history out of stock_data_daily so the database never reaches the ceiling that stops it accepting writes.">
        <Toggle on={bool('storage_rolloff_enabled')} disabled={busy !== null}
          onChange={(v) => set('storage_rolloff_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Staging prune"
        hint="Prunes raw_prices and chartink_raw_data past 120 days — 33% of the database and 45% of its growth, read one day deep by everything that uses them.">
        <Toggle on={bool('storage_staging_rolloff_enabled')} disabled={busy !== null}
          onChange={(v) => set('storage_staging_rolloff_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Health FAILS above"
        hint="Percentage of the 500 MB ceiling at which tools.health fails rather than warns. Writes stop at 100%.">
        <div className="flex items-center gap-1">
          <input type="number" defaultValue={num('storage_fail_pct')} disabled={busy !== null}
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['storage_fail_pct']) set('storage_fail_pct', v, 'Operator panel'); }}
            className="w-14 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
          <span className="text-[10px] text-muted-foreground">%</span>
        </div>
      </Row>

      {/* ── Sizing & exits ─────────────────────────────────────────────── */}
      <div className="text-[11px] font-medium text-muted-foreground mt-3 mb-1">Sizing &amp; exits</div>
      <Row label="Friction gate (max cost, in R)"
        hint={Number(cfg['sizing_max_cost_r'] ?? 0) > 0
          ? 'ON — refuses a trade whose round-trip friction exceeds this multiple of its own risk. Measured 04-Aug-2026: this account’s CNC clips run 0.605-2.363R, so a cap below ~0.7 refuses nearly every delivery trade at current position sizes.'
          : '0 = off. Measured friction at current CNC clip sizes (0.6-2.4R) means any cap worth setting would refuse nearly every swing trade — the clip size is the problem, not the gate. See migration 042.'}>
        <input type="number" step="0.05" defaultValue={num('sizing_max_cost_r')} disabled={busy !== null}
          onBlur={(e) => { const v = e.target.value.trim();
            if (v && v !== cfg['sizing_max_cost_r']) set('sizing_max_cost_r', v, 'Operator panel'); }}
          className="w-16 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
      </Row>
      <Row label="Runner cap enforced"
        hint="Caps concurrent runners at exit_max_runners. Was computed and silently discarded until migration 031 — this switch is what makes it real.">
        <Toggle on={bool('exit_runner_cap_enforced')} disabled={busy !== null}
          onChange={(v) => set('exit_runner_cap_enforced', String(v), 'Operator panel')} />
      </Row>
      <Row label="Quote mode (live day range/volume)"
        hint="Feeds live day range, volume and VWAP into the breakout conditions instead of a value up to 300s stale. Cross-check with tools.quote_parity after a session — day high/low must never read BEHIND the historical value.">
        <Toggle on={bool('intraday_quote_mode')} disabled={busy !== null}
          onChange={(v) => set('intraday_quote_mode', String(v), 'Operator panel')} />
      </Row>
    </div>
  );
}

export default OperatorPanel;
