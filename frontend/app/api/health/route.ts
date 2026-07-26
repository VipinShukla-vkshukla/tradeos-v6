// app/api/health/route.ts
//
// GET /api/health — every readiness check the framework needs, in one call.
//
// DESIGN TEST: would this have caught the bugs that were actually live?
//   sector_strength frozen 22 days      -> STALENESS
//   in_rule_engine 0/38 every day       -> SIGNAL SHAPE
//   planned_stop NULL on every signal   -> SIGNAL SHAPE
//   16 orphan rows in signal_log        -> SIGNAL SHAPE
//   positions with no stop or target    -> POSITIONS
//   closed trades with no signal_date   -> ATTRIBUTION
//   Kite token expired at 07:30         -> BROKER
//   msl_final_score_weights keys wrong  -> CONFIG
// All eight are covered below. A check that cannot fail is not a check.
//
// Severity: BLOCK = do not trade on this output. WARN = degraded but usable.
// OK = verified. Everything carries the exact remedy, because a health page
// that says "stale" without saying "run step 13" just relocates the problem.

import { NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function svc() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error('SUPABASE_SERVICE_ROLE_KEY not configured');
  return createClient(url, key);
}

type Sev = 'OK' | 'WARN' | 'BLOCK' | 'INFO';
interface Check {
  id: string;
  group: string;
  label: string;
  severity: Sev;
  value: string;
  expected: string;
  detail?: string;
  fix?: string;
}

const IST_OFFSET_MIN = 330;
function nowIST(): Date {
  const d = new Date();
  return new Date(d.getTime() + (IST_OFFSET_MIN + d.getTimezoneOffset()) * 60000);
}
function dayDiff(a: string, b: string): number {
  return Math.round((Date.parse(b) - Date.parse(a)) / 86400000);
}

export async function GET() {
  const checks: Check[] = [];
  const add = (c: Check) => checks.push(c);

  try {
    const sb = svc();
    const ist = nowIST();
    const todayIST = ist.toISOString().slice(0, 10);
    const dow = ist.getUTCDay(); // 0 Sun .. 6 Sat
    const isWeekend = dow === 0 || dow === 6;

    // ── Resolve the reference trading day from the data itself ─────────────
    const { data: sddLatest } = await sb
      .from('stock_data_daily').select('date').order('date', { ascending: false }).limit(1);
    const tradeDate: string | null = sddLatest?.[0]?.date ?? null;

    // Trading days elapsed since tradeDate, so "stale" means sessions missed,
    // not calendar days (a Monday check must not flag Friday's data as 3 late).
    const { data: sessionRows } = await sb
      .from('market_regime').select('date').gt('date', tradeDate ?? '1900-01-01')
      .lte('date', todayIST);
    const sessionsBehind = (sessionRows ?? []).length;

    // ═══ GROUP: DATA FRESHNESS ══════════════════════════════════════════════
    const tables: Array<[string, string, string, number]> = [
      // table, dateCol, label, blockIfStaleSessions
      ['stock_data_daily',    'date',          'Stock universe',      1],
      ['sector_strength',     'date',          'Sector strength',     1],
      ['industry_strength',   'date',          'Industry strength',   2],
      ['market_regime',       'date',          'Market regime',       1],
      ['master_shortlist',    'date',          'Master shortlist',    1],
      ['signal_log',          'date',          'Signals',             1],
      ['signal_output_daily', 'date',          'Decision snapshot',   1],
      ['msl_history',         'snapshot_date', 'MSL history',         2],
      ['fii_dii_flow',        'date',          'FII/DII flow',        3],
    ];

    for (const [tbl, col, label, blockAt] of tables) {
      try {
        const { data } = await sb.from(tbl).select(col).order(col, { ascending: false }).limit(1);
        // Double cast via unknown: supabase-js types a .select() built from a
        // runtime string as GenericStringError, which does not overlap our shape.
        const d: string | null =
          (data?.[0] as unknown as Record<string, string> | undefined)?.[col] ?? null;
        if (!d) {
          add({ id: `fresh_${tbl}`, group: 'Data freshness', label, severity: 'BLOCK',
                value: 'EMPTY', expected: tradeDate ?? 'latest session',
                fix: `Table ${tbl} has no rows. Run the evening pipeline.` });
          continue;
        }
        const { count } = await sb.from(tbl).select(col, { count: 'exact', head: true }).eq(col, d);
        const behind = tradeDate ? dayDiff(d, tradeDate) : 0;
        const sev: Sev = behind <= 0 ? 'OK' : behind >= blockAt ? 'BLOCK' : 'WARN';
        add({
          id: `fresh_${tbl}`, group: 'Data freshness', label, severity: sev,
          value: `${d} (${count ?? 0} rows)`,
          expected: tradeDate ?? '—',
          detail: behind > 0 ? `${behind} day(s) behind the stock universe` : 'current',
          fix: behind > 0
            ? `Stale vs stock_data_daily. This is the failure mode that froze sector_strength for 22 days. Run: python run_pipeline.py`
            : undefined,
        });
      } catch (e) {
        add({ id: `fresh_${tbl}`, group: 'Data freshness', label, severity: 'WARN',
              value: 'unreadable', expected: 'readable', detail: (e as Error).message.slice(0, 90) });
      }
    }

    // ═══ GROUP: SIGNAL SHAPE ════════════════════════════════════════════════
    if (tradeDate) {
      const { data: sigs } = await sb
        .from('signal_log')
        .select('symbol,signal_type,planned_stop,implied_rr,in_rule_engine,sector,sector_rank_at_entry')
        .eq('date', tradeDate);
      const S = sigs ?? [];
      const ENTRY = new Set(['PRIME_SETUP','BREAKOUT_SETUP','REENTRY_SETUP','BUY_CANDIDATE','STAGED_ENTRY','MOMENTUM_CONTINUATION']);
      const entries = S.filter((r) => ENTRY.has(r.signal_type as string));

      // Orphans: signal rows for symbols no longer in the shortlist. These are
      // indistinguishable from live signals downstream.
      const { data: mslRows } = await sb.from('master_shortlist').select('symbol').eq('date', tradeDate);
      const mslSet = new Set((mslRows ?? []).map((r) => r.symbol as string));
      const orphans = S.filter((r) => !mslSet.has(r.symbol as string));

      add({ id: 'sig_count', group: 'Signal integrity', label: 'Signals produced',
            severity: S.length === 0 ? 'BLOCK' : 'OK',
            value: `${S.length} (${entries.length} actionable)`, expected: '> 0',
            fix: S.length === 0 ? 'No signals. Check steps 17-20 in the pipeline log.' : undefined });

      add({ id: 'sig_orphans', group: 'Signal integrity', label: 'Orphan signal rows',
            severity: orphans.length === 0 ? 'OK' : 'WARN',
            value: `${orphans.length}`, expected: '0',
            detail: orphans.length ? orphans.slice(0, 6).map((r) => r.symbol).join(', ') : 'none',
            fix: orphans.length
              ? 'Rows for symbols no longer in the shortlist. They flow into the snapshot and alerts. Fixed in save_signals - re-run step 20.'
              : undefined });

      const withStop = S.filter((r) => r.planned_stop != null).length;
      add({ id: 'sig_plan', group: 'Signal integrity', label: 'Trade plan coverage',
            severity: S.length === 0 ? 'INFO' : withStop / S.length >= 0.8 ? 'OK' : withStop === 0 ? 'BLOCK' : 'WARN',
            value: `${withStop}/${S.length} have planned_stop`, expected: '>= 80%',
            fix: withStop / Math.max(S.length,1) < 0.8
              ? 'Signals without a stop cannot be sized or managed. Check compute_msl step 18.' : undefined });

      const inEngine = S.filter((r) => r.in_rule_engine).length;
      add({ id: 'sig_engines', group: 'Signal integrity', label: 'Rule engines contributing',
            severity: S.length === 0 ? 'INFO' : inEngine === 0 ? 'BLOCK' : 'OK',
            value: `${inEngine}/${S.length}`, expected: '> 0',
            detail: inEngine === 0 ? 'CTL/SBS/TPO produced nothing' : 'engines active',
            fix: inEngine === 0
              ? 'in_rule_engine 0 means sector_rank was empty - the exact symptom of stale sector_strength. Run step 13.'
              : undefined });

      const secs = new Set(entries.map((r) => (r.sector as string) ?? '?'));
      add({ id: 'sig_sectors', group: 'Signal integrity', label: 'Sector spread of entries',
            severity: entries.length === 0 ? 'INFO' : secs.size >= 3 ? 'OK' : 'WARN',
            value: `${secs.size} sector(s)`, expected: '>= 3',
            detail: [...secs].join(', '),
            fix: secs.size < 3 && entries.length > 2
              ? 'Entries concentrated. Check sector gates and the shortlist floor in /control.' : undefined });
    }

    // ═══ GROUP: POSITIONS ═══════════════════════════════════════════════════
    const { data: pos } = await sb.from('open_positions').select('*').eq('status', 'ACTIVE');
    const P = pos ?? [];
    add({ id: 'pos_count', group: 'Positions', label: 'Open positions', severity: 'INFO',
          value: `${P.length}`, expected: 'informational' });

    const noPlan = P.filter((p) => !p.planned_stop);
    add({ id: 'pos_plan', group: 'Positions', label: 'Positions with a stop',
          severity: P.length === 0 ? 'INFO' : noPlan.length === 0 ? 'OK' : 'BLOCK',
          value: `${P.length - noPlan.length}/${P.length}`, expected: 'all',
          detail: noPlan.length ? noPlan.map((p) => p.symbol).join(', ') : 'all managed',
          fix: noPlan.length
            ? 'Unmanaged: the exit policy falls back to a guessed 5% risk. Run: python -m control.position_lifecycle --reconcile-only'
            : undefined });

    const noSignal = P.filter((p) => !p.signal_id);
    add({ id: 'pos_attrib', group: 'Positions', label: 'Positions linked to a signal',
          severity: P.length === 0 ? 'INFO' : noSignal.length === 0 ? 'OK' : 'WARN',
          value: `${P.length - noSignal.length}/${P.length}`, expected: 'all',
          detail: noSignal.length ? noSignal.map((p) => p.symbol).join(', ') : 'all attributed',
          fix: noSignal.length ? 'Unlinked positions produce outcomes the Brain cannot learn from.' : undefined });

    // ═══ GROUP: ATTRIBUTION ═════════════════════════════════════════════════
    const { count: closedTotal } = await sb.from('closed_positions').select('id', { count: 'exact', head: true });
    const { count: closedAttrib } = await sb.from('closed_positions')
      .select('id', { count: 'exact', head: true }).not('signal_date', 'is', null);
    add({ id: 'attr_closed', group: 'Learning loop', label: 'Closed trades attributed',
          severity: (closedTotal ?? 0) === 0 ? 'INFO' : (closedAttrib ?? 0) === 0 ? 'WARN' : 'OK',
          value: `${closedAttrib ?? 0}/${closedTotal ?? 0}`, expected: 'growing',
          detail: 'Trades predating signal_log (2026-03-09) can never be attributed.',
          fix: (closedAttrib ?? 0) === 0 ? 'No outcome can be traced to a signal - the Brain has nothing to learn from.' : undefined });

    const { data: spLatest } = await sb.from('screener_performance')
      .select('entry_date').order('entry_date', { ascending: false }).limit(1);
    const spDate = spLatest?.[0]?.entry_date ?? null;
    const spBehind = spDate && tradeDate ? dayDiff(spDate, tradeDate) : 999;
    add({ id: 'attr_screener', group: 'Learning loop', label: 'Forward-return tracking',
          severity: !spDate ? 'WARN' : spBehind <= 12 ? 'OK' : 'WARN',
          value: spDate ?? 'none', expected: 'within ~10 sessions of the trade date',
          detail: 'This is the zero-risk performance baseline while you trade small.',
          fix: !spDate || spBehind > 12
            ? 'log_screener_outcomes is not recording. It used calendar-day arithmetic and skipped most periods; verify step 17.'
            : undefined });

    // ═══ GROUP: BROKER ══════════════════════════════════════════════════════
    const { data: tok } = await sb.from('system_config').select('key,value')
      .in('key', ['kite_access_token', 'kite_access_token_date']);
    const tokMap = Object.fromEntries((tok ?? []).map((r) => [r.key, r.value]));
    const issued = tokMap['kite_access_token_date'] ? new Date(tokMap['kite_access_token_date']) : null;
    // Zerodha invalidates daily at ~07:30 IST.
    //
    // Both sides of this comparison must be REAL instants. `ist` is a shifted
    // pseudo-Date whose UTC fields spell IST wall-clock, so calling
    // setUTCHours(7,30) on it produced 07:30Z — five and a half hours earlier
    // than 07:30 IST. Compared against `issued` (a correctly-parsed real
    // instant from a +05:30 timestamp), a token minted at 11:25 IST read as
    // expired. Build the boundary from the IST calendar date with an explicit
    // offset instead, so no pseudo-date ever enters the comparison.
    const istDate = ist.toISOString().slice(0, 10);
    let boundary = new Date(`${istDate}T07:30:00+05:30`);
    if (Date.now() < boundary.getTime()) {
      boundary = new Date(boundary.getTime() - 86400000);
    }
    const tokenValid = !!(tokMap['kite_access_token'] && issued && issued.getTime() >= boundary.getTime());
    add({ id: 'kite_token', group: 'Broker', label: 'Kite session',
          severity: tokenValid ? 'OK' : isWeekend ? 'WARN' : 'BLOCK',
          value: tokenValid ? `valid (issued ${issued!.toISOString().slice(0, 16).replace('T', ' ')})` : 'EXPIRED or missing',
          expected: 'issued after 07:30 IST today',
          detail: 'Without it, intraday stop monitoring cannot run - it skips rather than acting on stale prices.',
          fix: tokenValid ? undefined : 'python -m kite.token_manager --login-url' });

    // ═══ GROUP: CONFIG ══════════════════════════════════════════════════════
    const { data: cfgRows } = await sb.from('system_config').select('key,value');
    const C = Object.fromEntries((cfgRows ?? []).map((r) => [r.key, r.value]));

    add({ id: 'cfg_kill', group: 'Configuration', label: 'Kill switch',
          severity: String(C['master_kill_switch']).toLowerCase() === 'true' ? 'BLOCK' : 'OK',
          value: C['master_kill_switch'] ?? 'unset', expected: 'false',
          fix: String(C['master_kill_switch']).toLowerCase() === 'true'
            ? 'Kill switch is ON - every pipeline step aborts immediately.' : undefined });

    add({ id: 'cfg_gate', group: 'Configuration', label: 'Quality gate',
          severity: String(C['quality_gate_enabled']).toLowerCase() === 'true' ? 'OK' : 'WARN',
          value: C['quality_gate_enabled'] ?? 'unset', expected: 'true',
          fix: String(C['quality_gate_enabled']).toLowerCase() !== 'true'
            ? 'Disabled: bad input data will no longer halt the pipeline before signals.' : undefined });

    // Weight blob keys must match what compute_final_score reads - a mismatched
    // blob is silently ignored, which is exactly what happened before.
    let wOk = false, wSum = 0;
    try {
      const w = JSON.parse(C['msl_final_score_weights'] ?? '{}');
      const need = ['momentum','validity','trend_structure','institutional','breakout','sector','weekly_structure','base_score'];
      wOk = need.every((k) => k in w);
      wSum = Object.values(w).reduce((a: number, b) => a + Number(b), 0);
    } catch { /* handled below */ }
    add({ id: 'cfg_weights', group: 'Configuration', label: 'MSL score weights',
          severity: wOk && Math.abs(wSum - 1) < 0.01 ? 'OK' : 'WARN',
          value: wOk ? `keys ok, sum ${wSum.toFixed(2)}` : 'key mismatch',
          expected: 'code keys, sum 1.00',
          detail: 'Wrong keys are silently ignored and defaults are used instead.',
          fix: wOk ? undefined : 'Re-apply migration 004 part 2.' });

    // ═══ GROUP: ENGINES ═════════════════════════════════════════════════════
    const { data: eng } = await sb.from('strategy_config').select('strategy,lifecycle,params,enabled');
    const ENGINES = ['CTL','SBS','TPO','VBD','IAD','RSB','MOM','RVS','SEC'];
    const registered = (eng ?? []).filter((e) => ENGINES.includes(e.strategy as string));
    const active = registered.filter((e) => e.lifecycle === 'ACTIVE');
    const badShape = (eng ?? []).filter((e) => typeof e.params !== 'object' || Array.isArray(e.params));
    add({ id: 'eng_registered', group: 'Engines', label: 'Engines registered',
          severity: registered.length === 9 ? 'OK' : 'WARN',
          value: `${registered.length}/9`, expected: '9',
          fix: registered.length < 9 ? 'Run migration 006_engine_registry.sql.' : undefined });
    add({ id: 'eng_active', group: 'Engines', label: 'Engines active',
          severity: active.length === 0 ? 'BLOCK' : 'OK',
          value: `${active.length} active, ${registered.length - active.length} shadow/retired`,
          expected: '>= 1',
          detail: registered.filter((e) => e.lifecycle !== 'ACTIVE').map((e) => `${e.strategy}:${e.lifecycle}`).join(', ') || 'all active' });
    add({ id: 'eng_shape', group: 'Engines', label: 'strategy_config shape',
          severity: badShape.length === 0 ? 'OK' : 'BLOCK',
          value: badShape.length === 0 ? 'all objects' : `${badShape.length} malformed`,
          expected: 'jsonb object',
          detail: 'An array here crashes run_ctl/run_sbs with AttributeError.',
          fix: badShape.length ? 'Run migration 005_fix_strategy_config_shape.sql.' : undefined });

    // ═══ GROUP: PIPELINE ════════════════════════════════════════════════════
    if (tradeDate) {
      const { data: anom } = await sb.from('data_anomalies')
        .select('check_name,severity,message').eq('date', tradeDate);
      const errs = (anom ?? []).filter((a) => a.severity === 'ERROR');
      const warns = (anom ?? []).filter((a) => a.severity === 'WARN');
      add({ id: 'pipe_anomalies', group: 'Pipeline', label: 'Data anomalies',
            severity: errs.length ? 'BLOCK' : warns.length ? 'WARN' : 'OK',
            value: `${errs.length} error, ${warns.length} warn`, expected: '0 errors',
            detail: (errs[0]?.message ?? warns[0]?.message ?? 'clean').slice(0, 120) });
    }
    add({ id: 'pipe_session', group: 'Pipeline', label: 'Sessions since last data',
          severity: sessionsBehind === 0 ? 'OK' : sessionsBehind === 1 ? 'WARN' : 'BLOCK',
          value: `${sessionsBehind}`, expected: '0',
          detail: isWeekend ? 'Weekend - the previous session is expected' : `latest data ${tradeDate}`,
          fix: sessionsBehind > 1 ? 'The pipeline has not run for multiple sessions. Check cron-job.org.' : undefined });

    // ── Verdict ─────────────────────────────────────────────────────────────
    const blocks = checks.filter((c) => c.severity === 'BLOCK');
    const warns = checks.filter((c) => c.severity === 'WARN');
    const verdict = blocks.length ? 'BLOCKED' : warns.length ? 'DEGRADED' : 'READY';

    return NextResponse.json({
      verdict,
      generated_at: new Date().toISOString(),
      trade_date: tradeDate,
      is_weekend: isWeekend,
      counts: {
        ok: checks.filter((c) => c.severity === 'OK').length,
        warn: warns.length,
        block: blocks.length,
        info: checks.filter((c) => c.severity === 'INFO').length,
      },
      checks,
    });
  } catch (e) {
    return NextResponse.json(
      { verdict: 'BLOCKED', message: (e as Error).message, checks },
      { status: 500 }
    );
  }
}
