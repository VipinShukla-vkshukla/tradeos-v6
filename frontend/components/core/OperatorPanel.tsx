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
 * GROUPED BY SCOPE, NOT BY WHEN IT WAS BUILT
 * -------------------------------------------
 * This used to be two stacked panels: everything that existed before 05-Aug,
 * then a second "Phase 4" block bolted on below it. That put two controls of
 * the SAME switch (Runners / Runner cap enforced) in two different places, and
 * put controls with different scopes (an intraday-only overlay, a swing-only
 * governance rule, a shared allocator setting) in one undifferentiated list —
 * which is unreadable once system_config has 260 keys in it.
 *
 * So the layout now answers one question first — which book does this affect,
 * or is it shared — and answers "was this always here or is it Phase 4"
 * second, with a small <P4/> tag on the control itself rather than a separate
 * section. A control keeps its scope even if the operator forgets the date it
 * shipped; grouping by build date does not survive a second phase.
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
 * a click is hard, but because those two — plus the allocator's swing veto,
 * same treatment — are the only controls here whose mistake is discovered by
 * losing money rather than by reading the screen.
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
  // Everything the go-live pass added or found. Rendered inline within
  // whichever scope group actually owns each key (see the file header) and
  // marked with <P4/> rather than collected in a section of their own.
  'autonomy_phase',
  'alloc_shadow_enabled', 'alloc_live_intraday', 'alloc_live_swing',
  'alloc_max_slots', 'alloc_basket_recheck', 'alloc_hurdle_min_sample',
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

// Marks a control introduced in the Phase 4 go-live pass (05-Aug-2026), inline
// on the control itself. Replaces the earlier design of a separate "Phase 4"
// section — that put a swing-only governance rule next to an intraday-only
// overlay just because both shipped the same day, which is not a scope.
function P4() {
  return (
    <span
      title="Added in the Phase 4 go-live pass, 05-Aug-2026"
      className="text-[9px] px-1 py-0 rounded font-semibold bg-indigo-500/20 text-indigo-400 leading-tight shrink-0"
    >
      P4
    </span>
  );
}

function Row({ label, hint, tag, children }: {
  label: string; hint?: string; tag?: React.ReactNode; children: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-border/20 last:border-0">
      <div className="min-w-0">
        <div className="text-xs font-medium flex items-center gap-1.5">{label}{tag}</div>
        {hint && <div className="text-[10px] text-muted-foreground mt-0.5 max-w-[46ch]">{hint}</div>}
      </div>
      <div className="shrink-0 pt-0.5">{children}</div>
    </div>
  );
}

// A sub-group label WITHIN a scope card — "Exits", "Allocator", and so on.
// Scope (swing / intraday / shared) is the card; this is the second-level
// grouping inside it, same visual weight everywhere so the eye learns it once.
function SubHead({ children }: { children: React.ReactNode }) {
  return <div className="text-[11px] font-medium text-muted-foreground mt-3 mb-1">{children}</div>;
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

  const numField = (k: string, reason = 'Operator panel') => (
    <input
      type="number"
      defaultValue={cfg[k] ?? ''}
      disabled={busy !== null}
      onBlur={(e) => {
        const v = e.target.value.trim();
        if (v && v !== cfg[k]) set(k, v, reason);
      }}
      className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1
                 text-xs font-mono tabular-nums"
    />
  );

  const framework = (fw: 'swing' | 'intraday', title: string) => {
    const m = mode(fw);
    const live = m === 'LIVE';
    const confirmKey = `${fw}_live`;
    const allocLiveKey = `alloc_live_${fw}`;
    const allocLiveOn = bool(allocLiveKey);
    const convictionLive = fw === 'swing' &&
      (Number(cfg['rank_weight_tier'] ?? 0) > 0 || Number(cfg['rank_weight_conviction'] ?? 0) > 0);

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
              {numField(k, 'Operator panel: cap change')}
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

        {fw === 'swing' && (
          <>
            <SubHead>Exits</SubHead>
            <Row label="Broker-side GTT stops"
              hint="Stops rest at Zerodha and fire whether or not anything of yours is running — the real answer to “what if the daemon is down”. CNC/delivery only: Kite GTTs cannot be placed on an MIS tranche, which is why this key is named intraday_gtt_enabled but does not gate intraday.">
              <Toggle on={bool('intraday_gtt_enabled')} disabled={busy !== null}
                onChange={(v) => set('intraday_gtt_enabled', String(v), 'Operator panel')} />
            </Row>
            <Row label="Runners"
              hint="At the target, assess the trend and either bank it or let it run. Off reverts to an unconditional exit at 3R.">
              <Toggle on={bool('exit_runners_enabled')} disabled={busy !== null}
                onChange={(v) => set('exit_runners_enabled', String(v), 'Operator panel')} />
            </Row>
            <Row label="  ↳ runner cap enforced" tag={<P4 />}
              hint="Caps concurrent runners at exit_max_runners — only matters while Runners above is on. Was computed and silently discarded until migration 031; this switch is what makes it real.">
              <Toggle on={bool('exit_runner_cap_enforced')} disabled={busy !== null}
                onChange={(v) => set('exit_runner_cap_enforced', String(v), 'Operator panel')} />
            </Row>
            <Row label="Deterioration exit"
              hint="Exit a profitable position whose thesis broke, before the trail catches it.">
              <Toggle on={bool('exit_deterioration_enabled')} disabled={busy !== null}
                onChange={(v) => set('exit_deterioration_enabled', String(v), 'Operator panel')} />
            </Row>

            <SubHead>Governance &amp; sizing</SubHead>
            <Row label="Friction gate (max cost, in R)" tag={<P4 />}
              hint={Number(cfg['sizing_max_cost_r'] ?? 0) > 0
                ? 'ON — refuses a trade whose round-trip friction exceeds this multiple of its own risk. Measured 04-Aug-2026: this account’s CNC clips run 0.605-2.363R, so a cap below ~0.7 refuses nearly every delivery trade at current position sizes. Swing/CNC sizing only — intraday prices its own cost independently (below).'
                : '0 = off. Measured friction at current CNC clip sizes (0.6-2.4R) means any cap worth setting would refuse nearly every swing trade — the clip size is the problem, not the gate. Swing/CNC sizing only. See migration 042.'}>
              {numField('sizing_max_cost_r', 'Operator panel')}
            </Row>
            <Row label="Conviction layer weight" tag={<P4 />}
              hint={(convictionLive
                ? '⚠ NON-ZERO: an unmeasured AI tier is back in the ranking. Restore only once tier-by-tier forward returns exist from the unbiased record.'
                : 'Both weights are 0 — annotation only, pending tier-by-tier forward returns from the unbiased record.'
              ) + ' Feeds the swing composite (analysis.entry_ranking) only — intraday setups are not ranked by this.'}>
              <div className="flex gap-1">
                <input type="number" step="0.1" defaultValue={cfg['rank_weight_tier'] ?? ''} disabled={busy !== null}
                  title="rank_weight_tier"
                  onBlur={(e) => { const v = e.target.value.trim();
                    if (v && v !== cfg['rank_weight_tier']) set('rank_weight_tier', v, 'Operator panel'); }}
                  className={`w-14 bg-panel border rounded px-1.5 py-1 text-xs font-mono tabular-nums
                    ${convictionLive ? 'border-red-500/50' : 'border-border/50'}`} />
                <input type="number" step="0.1" defaultValue={cfg['rank_weight_conviction'] ?? ''} disabled={busy !== null}
                  title="rank_weight_conviction"
                  onBlur={(e) => { const v = e.target.value.trim();
                    if (v && v !== cfg['rank_weight_conviction']) set('rank_weight_conviction', v, 'Operator panel'); }}
                  className={`w-14 bg-panel border rounded px-1.5 py-1 text-xs font-mono tabular-nums
                    ${convictionLive ? 'border-red-500/50' : 'border-border/50'}`} />
              </div>
            </Row>
            <Row label="Quarterly freeze" tag={<P4 />}
              hint="Parameter changes are refused inside a freeze window — the primary defence against fitting noise at 5-10 closed observations a week. Governs swing engine parameters only (swing/brain/backtester_and_change_manager) — there is no equivalent auto-tune loop for the seven intraday engines yet.">
              <Toggle on={bool('governance_freeze_enabled')} disabled={busy !== null}
                onChange={(v) => set('governance_freeze_enabled', String(v), 'Operator panel')} />
            </Row>
            <Row label="Require out-of-sample confirmation" tag={<P4 />}
              hint="A proposal must be confirmed in a LATER window than it was fitted in. Fit in N, confirm in N+1, act in N+2. Same swing-only scope as the freeze above.">
              <Toggle on={bool('governance_require_oos')} disabled={busy !== null}
                onChange={(v) => set('governance_require_oos', String(v), 'Operator panel')} />
            </Row>

            <SubHead>Allocator</SubHead>
            <Row label="Live — swing (real money)" tag={<P4 />}
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
                <Toggle on={allocLiveOn} danger disabled={busy !== null}
                  onChange={(v) => v
                    ? setConfirm('alloc_live_swing')
                    : set('alloc_live_swing', 'false', 'Operator panel: allocator off swing')} />
              )}
            </Row>
          </>
        )}

        {fw === 'intraday' && (
          <>
            <SubHead>Entry gates</SubHead>
            <Row label="Structure gate"
              hint="Blocks setups whose recent swing-high/swing-low structure is a downtrend — buying a break there is buying a lower high. (&quot;Swing&quot; here is the price-structure term, not the Swing book — this gate is intraday-only.)">
              <Toggle on={bool('intraday_structure_gate')} disabled={busy !== null}
                onChange={(v) => set('intraday_structure_gate', String(v), 'Operator panel')} />
            </Row>
            <Row label="Event gate"
              hint="Blocks results day, F&O ban and ASM names.">
              <Toggle on={bool('intraday_news_gate_enabled')} disabled={busy !== null}
                onChange={(v) => set('intraday_news_gate_enabled', String(v), 'Operator panel')} />
            </Row>

            <SubHead>Structural overlay</SubHead>
            <Row label="Expiry day-type sizing" tag={<P4 />}
              hint="Sizes intraday down on settlement-dominated sessions. The day-type flag is a heuristic (~1-3 session error on the monthly flag) — tolerable because it only ever reduces size. Intraday-only: a 1-3 week swing position is not exposed to same-day settlement flows the way an intraday breakout is.">
              <Toggle on={bool('overlay_expiry_enabled')} disabled={busy !== null}
                onChange={(v) => set('overlay_expiry_enabled', String(v), 'Operator panel')} />
            </Row>
            <Row label="Quote mode (live day range/volume)" tag={<P4 />}
              hint="Feeds live day range, volume and VWAP into the breakout conditions instead of a value up to 300s stale. Cross-check with tools.quote_parity after a session — day high/low must never read BEHIND the historical value.">
              <Toggle on={bool('intraday_quote_mode')} disabled={busy !== null}
                onChange={(v) => set('intraday_quote_mode', String(v), 'Operator panel')} />
            </Row>

            <SubHead>Allocator</SubHead>
            <Row label="Live — intraday (paper)" tag={<P4 />}
              hint="The allocator can refuse an intraday setup. Costs nothing to be wrong: this book is simulated.">
              <Toggle on={allocLiveOn} disabled={busy !== null}
                onChange={(v) => set('alloc_live_intraday', String(v), 'Operator panel')} />
            </Row>
          </>
        )}
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
                  : 'Clear. Stops every order and halts the intraday daemon when set. Applies to BOTH books.'}
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
        {/* Full width, like the kill switch above it — it describes BOTH
            books, so it spans both columns rather than sitting beside one. */}
        <RiskExposure cfg={cfg} />

        {/* The two books, side by side and equal width, so the same control
            lines up across frameworks and a difference is visible rather than
            remembered. Everything scoped to ONE book — old or Phase 4 — lives
            in that book's card; <P4/> marks which rows are new rather than
            moving them to a section of their own. */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 items-start">
          {framework('swing', 'Swing')}
          {framework('intraday', 'Intraday')}
        </div>

        {/* Everything that is neither swing-only nor intraday-only: the
            master order gate, the allocator's own mechanics, the two
            structural overlays that apply to both books, and storage. */}
        <SharedControls cfg={cfg} bool={bool} set={set} busy={busy} />

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
 * Controls that are neither swing-only nor intraday-only.
 *
 * WHY THESE FOUR AND NOT THE OTHERS
 * ----------------------------------
 * Order placement is the master gate above both frameworks by name and by
 * code (execution/gates.py). The allocator's shadow/max-slots/hurdle-sample/
 * basket-recheck settings score and rank proposals from BOTH books in one
 * pass (allocation/allocator.py) — there is no per-book copy of any of them.
 * Volatility exposure scaling and the liquidity gate are the two Stage 9
 * overlays that apply to both books' sizing (the third, expiry-day sizing,
 * is intraday-only and lives in that card instead — see its hint there for
 * why). Storage is account-wide by construction.
 *
 * Everything here used to sit in "Safety gates" (a grab-bag that also held
 * book-specific items) plus a separate "Phase 4" section (grouped by ship
 * date, not scope). Both are gone; this is what was left once every
 * genuinely single-book control moved into that book's own card.
 */
function SharedControls({ cfg, bool, set, busy }: {
  cfg: Cfg;
  bool: (k: string) => boolean;
  set: (key: string, value: string, reason: string) => Promise<void>;
  busy: string | null;
}) {
  const num = (k: string) => cfg[k] ?? '';
  const numField = (k: string, reason = 'Operator panel') => (
    <input
      type="number"
      defaultValue={num(k)}
      disabled={busy !== null}
      onBlur={(e) => {
        const v = e.target.value.trim();
        if (v && v !== cfg[k]) set(k, v, reason);
      }}
      className="w-full mt-0.5 bg-panel border border-border/50 rounded px-1.5 py-1
                 text-xs font-mono tabular-nums"
    />
  );

  return (
    <div className="rounded-lg border border-border/50 p-3">
      <div className="text-sm font-semibold mb-1 flex items-center gap-2">
        <Shield className="h-4 w-4 text-muted-foreground" />
        Shared — both books
        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-blue-500/20 text-blue-400">
          autonomy phase {cfg['autonomy_phase'] ?? '?'}
        </span>
      </div>

      <Row label="Order placement — master gate"
        hint="Master gate above BOTH frameworks — off means alerts only, everywhere, regardless of any other switch on this page.">
        <Toggle on={bool('intraday_orders_enabled')} danger disabled={busy !== null}
          onChange={(v) => set('intraday_orders_enabled', String(v), 'Operator panel')} />
      </Row>

      {/* ── Allocator — shared mechanics. The LIVE toggle for each book lives
          in that book's own card; what is here scores and ranks BOTH books
          in one pass and has no per-book copy. ────────────────────────── */}
      <SubHead>Allocator — shared mechanics</SubHead>
      <Row label="Shadow recording" tag={<P4 />}
        hint="Scores every proposal and records the verdict. Changes nothing on its own — this is what the promotion evidence is built from.">
        <Toggle on={bool('alloc_shadow_enabled')} disabled={busy !== null}
          onChange={(v) => set('alloc_shadow_enabled', String(v),
            v ? 'Operator panel' : 'Operator panel — WARNING: stops recording promotion evidence')} />
      </Row>
      <div className="grid grid-cols-2 gap-2 mt-1">
        <div>
          <div className="text-[10px] text-muted-foreground flex items-center gap-1">Max slots/cycle<P4 /></div>
          {numField('alloc_max_slots')}
        </div>
        <div>
          <div className="text-[10px] text-muted-foreground flex items-center gap-1">Hurdle min sample<P4 /></div>
          {numField('alloc_hurdle_min_sample')}
        </div>
      </div>
      <div className="text-[10px] text-muted-foreground mt-1">
        <b>Max slots/cycle</b> is a shared ceiling on new entries today, counted across BOTH
        books together — not per-book — and it also raises the hurdle as it fills, so the
        last slot must clear a higher bar than the first. <b>Hurdle min sample</b> is how many
        resolved outcomes an engine needs before the allocator trusts ITS OWN edge over the
        shared cold-start prior.
      </div>
      <Row label="Basket recheck" tag={<P4 />}
        hint="Recheck sector caps across the allocator's OWN simultaneous picks, not just against what is already held.">
        <Toggle on={bool('alloc_basket_recheck')} disabled={busy !== null}
          onChange={(v) => set('alloc_basket_recheck', String(v), 'Operator panel')} />
      </Row>

      {/* ── Structural overlays that apply to both books. Expiry-day sizing
          is intraday-only and lives in that card — see its hint there. ── */}
      <SubHead>
        Structural overlays <span className="font-normal">— reduce or refuse a trade, never enlarge one</span>
      </SubHead>
      <Row label="Volatility exposure scaling" tag={<P4 />}
        hint="Scales exposure down on BOTH books as India VIX rises — swing sizing and intraday sizing each apply it independently. Bands are set from this account's own observed distribution.">
        <Toggle on={bool('overlay_vol_scaling_enabled')} disabled={busy !== null}
          onChange={(v) => set('overlay_vol_scaling_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Liquidity gate" tag={<P4 />}
        hint="Refuses a name that cannot be exited at plan, on BOTH books, measured against its own traded value.">
        <Toggle on={bool('overlay_liquidity_enabled')} disabled={busy !== null}
          onChange={(v) => set('overlay_liquidity_enabled', String(v), 'Operator panel')} />
      </Row>
      {bool('overlay_liquidity_enabled') && (
        <Row label="  ↳ strict on unknown liquidity" tag={<P4 />}
          hint="A name with no recorded traded value is refused rather than waved through.">
          <Toggle on={bool('overlay_liquidity_strict')} disabled={busy !== null}
            onChange={(v) => set('overlay_liquidity_strict', String(v), 'Operator panel')} />
        </Row>
      )}

      {/* ── Storage — account-wide, not per-book. ─────────────────────── */}
      <SubHead>Storage</SubHead>
      <Row label="Nightly roll-off" tag={<P4 />}
        hint="Archives history out of stock_data_daily so the database never reaches the ceiling that stops it accepting writes.">
        <Toggle on={bool('storage_rolloff_enabled')} disabled={busy !== null}
          onChange={(v) => set('storage_rolloff_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Staging prune" tag={<P4 />}
        hint="Prunes raw_prices and chartink_raw_data past 120 days — 33% of the database and 45% of its growth, read one day deep by everything that uses them.">
        <Toggle on={bool('storage_staging_rolloff_enabled')} disabled={busy !== null}
          onChange={(v) => set('storage_staging_rolloff_enabled', String(v), 'Operator panel')} />
      </Row>
      <Row label="Health FAILS above" tag={<P4 />}
        hint="Percentage of the 500 MB ceiling at which tools.health fails rather than warns. Writes stop at 100%.">
        <div className="flex items-center gap-1">
          <input type="number" defaultValue={num('storage_fail_pct')} disabled={busy !== null}
            onBlur={(e) => { const v = e.target.value.trim();
              if (v && v !== cfg['storage_fail_pct']) set('storage_fail_pct', v, 'Operator panel'); }}
            className="w-14 bg-panel border border-border/50 rounded px-1.5 py-1 text-xs font-mono tabular-nums" />
          <span className="text-[10px] text-muted-foreground">%</span>
        </div>
      </Row>
    </div>
  );
}

export default OperatorPanel;
