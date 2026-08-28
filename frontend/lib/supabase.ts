// TradeOS v6 — Supabase Client
// Read-only queries + realtime subscriptions.
// All table names in this file are verified against the actual Supabase schema.

import { createClient } from '@supabase/supabase-js';
import type {
  OpenPosition,
  ClosedPosition,
  Signal,
  MarketRegime,
  AIModelPerformance,
  BrainProposal,
  BrainAnalysisLog,
  Lesson,
  ConfigEntry,
  ConfigChange,
  MasterStock,
  PerformanceMetricsRow,
  MslHistoryEntry,
} from '@/types/database';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

export const isSupabaseConfigured = (): boolean => !!(supabaseUrl && supabaseAnonKey);

export const supabase = isSupabaseConfigured()
  ? createClient(supabaseUrl!, supabaseAnonKey!)
  : null;

export function getSupabaseWarning(): string | null {
  if (!isSupabaseConfigured()) {
    return 'Supabase not configured. Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY to .env.local to see live data.';
  }
  return null;
}

/**
 * Decompose a swing `strategy` value into the real registered engine codes
 * that produced it — CTL, SEC, MOM, etc., the rows in strategy_config.
 *
 * signal_log.strategy/closed_positions.strategy/open_positions.strategy is
 * NOT one engine's code. screen_stocks.py writes it as `"+".join(sorted(
 * engines))` — every engine that independently corroborated a candidate that
 * day, e.g. "CTL+MOM+SEC" (see screen_stocks.py::run_sector_rotation and
 * allocation/scoring.py::swing_family(), the backend's own authoritative
 * decomposition, used to bucket combos for the live hurdle). Treating each
 * distinct combo string as its own engine — what this file did before — is
 * why the Engines screen showed ~23 "engines" instead of the real ~9: dozens
 * of corroboration patterns each got their own card with no strategy_config
 * row to source a label from, while the actual engines never got full credit
 * for their detections (a CTL+MOM+SEC day counted once for the combo, not
 * once each for CTL, MOM and SEC, all three of which genuinely fired).
 *
 * This mirrors swing_family()'s own paren-stripping (a legacy "CTL (Legacy)"
 * annotation is metadata about when the row was labelled, not a different
 * engine) but goes one level finer: swing_family() collapses everything down
 * to a handful of scoring buckets (CONTINUATION/MOM/RVS/...) because a prior
 * needs statistical mass; this screen's job is "how is each REGISTERED
 * engine doing", so it stays at the single-engine grain.
 */
function swingBaseEngines(raw: string | null | undefined): string[] {
  if (!raw) return [];
  const stripParen = (p: string) => p.replace(/\s*\([^)]*\)\s*$/, '').trim();
  const parts = raw.split('+').map(stripParen).filter(Boolean).map((p) => p.toUpperCase());
  return [...new Set(parts)];
}

// ---------------------------------------------------------------------------
// Generic query helper
// ---------------------------------------------------------------------------
export async function queryTable<T>(
  table: string,
  options?: {
    select?: string;
    filter?: Record<string, unknown>;
    order?: { column: string; ascending?: boolean };
    limit?: number;
    offset?: number;
  }
): Promise<{ data: T[] | null; error: Error | null; count: number | null }> {
  if (!supabase) {
    return { data: null, error: new Error('Supabase not configured'), count: null };
  }

  let query = supabase
    .from(table)
    .select(options?.select || '*', { count: 'exact' });

  if (options?.filter) {
    Object.entries(options.filter).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (Array.isArray(value)) {
        query = query.in(key, value);
      } else if (typeof value === 'object') {
        const f = value as Record<string, unknown>;
        if ('gt' in f) query = query.gt(key, f.gt);
        if ('gte' in f) query = query.gte(key, f.gte);
        if ('lt' in f) query = query.lt(key, f.lt);
        if ('lte' in f) query = query.lte(key, f.lte);
        if ('ilike' in f) query = query.ilike(key, f.ilike as string);
      } else {
        query = query.eq(key, value);
      }
    });
  }

  if (options?.order) {
    query = query.order(options.order.column, { ascending: options.order.ascending ?? true });
  }
  if (options?.limit) query = query.limit(options.limit);
  if (options?.offset) {
    query = query.range(
      options.offset,
      options.offset + (options.limit || 10) - 1
    );
  }

  const { data, error, count } = await query;
  return { data: data as T[] | null, error, count };
}

// ---------------------------------------------------------------------------
// Realtime helpers
// ---------------------------------------------------------------------------
export function subscribeToChannel(
  channelName: string,
  table: string,
  callback: (payload: { eventType: string; new: unknown; old: unknown }) => void,
  filter?: string
) {
  if (!supabase) return null;

  return supabase
    .channel(channelName)
    .on(
      'postgres_changes',
      { event: '*', schema: 'public', table, filter },
      (payload) =>
        callback({ eventType: payload.eventType, new: payload.new, old: payload.old })
    )
    .subscribe();
}

export async function unsubscribeFromChannel(channelName: string) {
  if (!supabase) return;
  await supabase.channel(channelName).unsubscribe();
}

// ---------------------------------------------------------------------------
// Typed query shortcuts — all table names verified against actual schema
// ---------------------------------------------------------------------------
export const queries = {
  // market_regime  ← table exists, date is PK (no numeric id)
  getMarketRegime: () =>
    queryTable<MarketRegime>('market_regime', {
      select: 'date, regime, computed_regime, regime_confidence, regime_score_computed, nifty_price, india_vix, avg_sector_breadth, above_200dma_pct, nifty_5d_chg_pct, created_at',
      order: { column: 'date', ascending: false },
      limit: 1,
    }),

  // open_positions  ← NOT 'positions'
  getOpenPositions: () =>
    queryTable<OpenPosition>('open_positions', {
      order: { column: 'entry_date', ascending: false },
    }),

  // ── Intraday subsystem ───────────────────────────────────────────────────
  // Reads the same three tables backend/intraday writes. Deliberately narrow:
  // the subsystem stores decisions rather than observations (no ticks, no
  // per-cycle evaluations), so these are the only rows that exist.

  /** Alerts actually sent — one row per state change, newest first. */
  getIntradayAlerts: (limit = 60) =>
    queryTable<{
      id: number; ts: string; symbol: string; kind: string; urgency: string | null;
      headline: string; detail: string | null; ltp: number | null;
      r_multiple: number | null; acknowledged: boolean;
    }>('intraday_alerts', { order: { column: 'ts', ascending: false }, limit }),

  /** Every attempted broker write, including the blocked and failed ones. */
  getIntradayBrokerLog: (limit = 40) =>
    queryTable<{
      id: number; ts: string; symbol: string; channel: string; action: string;
      side: string | null; ref_id: string | null; price: number | null;
      quantity: number | null; detail: string | null;
    }>('intraday_broker_log', { order: { column: 'ts', ascending: false }, limit }),

  /**
   * Daemon liveness.
   *
   * Without this, "no alerts today" is ambiguous — a daemon working correctly
   * on a quiet session and one that died at 09:20 produce identical silence.
   * The single row is overwritten, never appended.
   */
  getIntradayHeartbeat: () =>
    queryTable<{ id: number; ts: string; summary: string | null; alerts_sent: number }>(
      'intraday_heartbeat', { limit: 1 },
    ),

  /** Today's detected setups, including the ones rejected on cost. */
  getIntradaySetups: (limit = 50) =>
    queryTable<{
      id: number; ts: string; trade_date: string; symbol: string; strategy: string;
      phase: string | null; entry: number | null; stop: number | null;
      target: number | null; risk_pct: number | null; reward_pct: number | null;
      rr: number | null; confidence: number | null; rationale: string | null;
      invalidation: string | null; cost_pct: number | null; cost_verdict: string | null;
      corroborated_by: string | null; outcome: string | null; outcome_pct: number | null;
    }>('intraday_setups', { order: { column: 'ts', ascending: false }, limit }),

  /**
   * Per-engine hit rate and net expectancy.
   *
   * Counts setups DETECTED rather than trades taken, so an engine that fires
   * often and resolves badly is visible even when cost rejection kept you out.
   */
  getIntradayScorecard: () =>
    queryTable<{
      strategy: string; setups: number; taken: number; wins: number; losses: number;
      scratches: number; hit_rate_pct: number | null; avg_net_pct: number | null;
      avg_confidence: number | null; last_seen: string | null;
    }>('v_intraday_engine_scorecard', { limit: 20 }),

  /** What was watchable today, and why. */
  getIntradayUniverse: (limit = 50) =>
    queryTable<{
      trade_date: string; symbol: string; close: number; value_cr: number;
      atr_pct: number; delivery_pct: number | null; sector: string;
      score: number; reason: string;
    }>('intraday_universe', { order: { column: 'score', ascending: false }, limit }),

  // ── Allocator (Phase 4, Stage 10) ────────────────────────────────────────
  // Four views the master spec requires: today's ledger ordered by edge, the
  // live hurdle against today's proposals, storage headroom, and shadow vs
  // greedy reduced to one number. All four read allocation_decisions and
  // v_storage_usage directly — no bespoke API route, same pattern as every
  // other tab in this file.

  /** Every verdict recorded today, newest first — the raw material for both
   *  the ledger (sort by edge client-side) and the hurdle-vs-proposals view. */
  getAllocationToday: (tradeDate: string) =>
    queryTable<{
      id: number; decided_at: string; trade_date: string; symbol: string;
      framework: string; product: string; source: string | null;
      verdict: string; reason: string | null;
      entry: number | null; stop: number | null; target: number | null;
      quantity: number | null; edge: number | null; e_r: number | null;
      cost_r: number | null; hurdle: number | null;
      // A null hurdle is ambiguous on its own: it is either a permissive cold
      // start or an infinite bar with no slots left. hurdle_inputs.cold_start
      // is what tells the two apart, so the tab must fetch it.
      hurdle_inputs: { cold_start?: boolean; pooled_across_buckets?: boolean;
                       base?: number | null; n?: number } | null;
      regime_bucket: string | null;
      prior_n: number | null; prior_below_floor: boolean | null;
      native_rank: number | null; shadow: boolean; outcome_r: number | null;
      outcome_note: string | null;
    }>('allocation_decisions', {
      filter: { trade_date: tradeDate },
      order: { column: 'decided_at', ascending: false },
      limit: 500,
    }),

  /** Every scored verdict in a window, for the shadow-vs-greedy reduction.
   *  Mirrors tools/allocator_report.py's own definition, computed client-side
   *  against getAllocationMatchablePositions so the two can never drift: a
   *  TAKE with no matching position, or a DECLINE/DEFER with one, disagrees. */
  getAllocationHistory: (sinceIso: string, limit = 2000) =>
    queryTable<{
      trade_date: string; symbol: string; product: string; framework: string;
      verdict: string; outcome_r: number | null; shadow: boolean;
    }>('allocation_decisions', {
      filter: { trade_date: { gte: sinceIso } },
      order: { column: 'trade_date', ascending: false },
      limit,
    }),

  /** (symbol, product, entry_date) for every position opened in the window —
   *  open or since closed. The ground truth for "did greedy take it", read
   *  narrow on purpose rather than reusing the full-row position queries. */
  getAllocationMatchablePositions: async (sinceIso: string) => {
    const [open, closed] = await Promise.all([
      queryTable<{ symbol: string; product: string | null; entry_date: string }>(
        'open_positions', { select: 'symbol,product,entry_date', filter: { entry_date: { gte: sinceIso } } },
      ),
      queryTable<{ symbol: string; product: string | null; entry_date: string }>(
        'closed_positions', { select: 'symbol,product,entry_date', filter: { entry_date: { gte: sinceIso } } },
      ),
    ]);
    return [...(open.data ?? []), ...(closed.data ?? [])];
  },

  // ── Trade Detail drill-down ──────────────────────────────────────────────
  // Powers the click-through from any open/closed position row: the exact
  // edge/hurdle this trade cleared (straight from allocation_decisions — no
  // recomputation, so it can never drift from what the allocator actually
  // decided) and how this trade's own entry-time factors compare to the
  // engine's own resolved winners/losers. Swing and intraday read different
  // raw material for the factor side (stock_data_daily indicators vs
  // intraday_setups.meta) because that is genuinely what each side stores —
  // there is no shared factor table, and inventing matching column names for
  // both books would just be a second, fabricated schema.

  getOpenPositionBySymbol: (symbol: string) =>
    queryTable<OpenPosition>('open_positions', { filter: { symbol }, limit: 1 }),

  getClosedPositionById: (id: number) =>
    queryTable<ClosedPosition>('closed_positions', { filter: { id }, limit: 1 }),

  /** The allocator's own verdict for this trade — same row getAllocationToday
   *  reads, scoped to one symbol/day/framework so a single trade can be found
   *  even outside today. Multiple rows are possible (a DEFER retried later in
   *  the day); newest first — caller picks the TAKE row. */
  getAllocationDecisionFor: (symbol: string, tradeDate: string, framework: string) =>
    queryTable<{
      id: number; decided_at: string; trade_date: string; symbol: string;
      framework: string; product: string; verdict: string; reason: string | null;
      entry: number | null; stop: number | null; target: number | null;
      edge: number | null; e_r: number | null; cost_r: number | null;
      hurdle: number | null;
      hurdle_inputs: { cold_start?: boolean; pooled_across_buckets?: boolean;
                       base?: number | null; n?: number } | null;
      regime_bucket: string | null; prior_n: number | null; direction: string | null;
    }>('allocation_decisions', {
      filter: { symbol, trade_date: tradeDate, framework },
      order: { column: 'decided_at', ascending: false },
      limit: 25,
    }),

  /** stock_data_daily row for one symbol on the day it was entered — the
   *  swing factor source (rsi_daily, vol_ratio, high_52w). Not the position's
   *  OWN entry-time columns (there are none, aside from sector_rank_at_entry)
   *  — the market's state that day, same table screen_stocks/compute_msl read. */
  getEntryDayIndicators: (symbol: string, date: string) =>
    queryTable<{
      symbol: string; date: string; close: number | null;
      rsi_daily: number | null; vol_ratio: number | null;
      high_52w: number | null; low_52w: number | null;
    }>('stock_data_daily', {
      select: 'symbol,date,close,rsi_daily,vol_ratio,high_52w,low_52w',
      filter: { symbol, date },
      limit: 1,
    }),

  /**
   * Winner vs loser mean of every SWING factor this codebase actually stores
   * at entry, for one strategy. sector_rank_at_entry lives on the position
   * row itself; rsi_daily/vol_ratio/high_52w need a join to stock_data_daily
   * on (symbol, entry_date) that Supabase's client can't express as one
   * filter, so this fetches both sides and joins in JS — the same two-fetch
   * pattern getAllocationMatchablePositions above already uses.
   */
  getSwingEngineFactorProfile: async (strategy: string) => {
    const closed = await queryTable<{
      symbol: string; entry_date: string; realized_pnl: number | null;
      sector_rank_at_entry: number | null; entry_price: number | null;
    }>('closed_positions', {
      select: 'symbol,entry_date,realized_pnl,sector_rank_at_entry,entry_price',
      filter: { strategy, framework: 'SWING' },
      limit: 500,
    });
    const rows = closed.data ?? [];
    type Ind = { rsi_daily: number | null; vol_ratio: number | null; high_52w: number | null; close: number | null };
    const indicators = new Map<string, Ind>();
    if (!rows.length) return { winners: [] as typeof rows, losers: [] as typeof rows, indicators };

    const symbols = [...new Set(rows.map((r) => r.symbol))];
    const dates = [...rows.map((r) => r.entry_date)].sort();
    const { data: indicatorRows } = await queryTable<Ind & { symbol: string; date: string }>(
      'stock_data_daily', {
        select: 'symbol,date,close,rsi_daily,vol_ratio,high_52w',
        filter: { symbol: symbols, date: { gte: dates[0], lte: dates[dates.length - 1] } },
        limit: 2000,
      },
    );
    for (const r of indicatorRows ?? []) indicators.set(`${r.symbol}|${r.date}`, r);

    return {
      winners: rows.filter((r) => (r.realized_pnl ?? 0) > 0),
      losers: rows.filter((r) => (r.realized_pnl ?? 0) <= 0),
      indicators,
    };
  },

  /**
   * Same question for INTRADAY, reusing tools/feature_edge_study.py's own
   * population definition exactly (cost_verdict TAKEN, outcome resolved) so
   * this can never disagree with what that tool already found. volume_ratio
   * and atr_pct_daily live in the meta JSON column; confidence is a
   * top-level column — matching feature_edge_study.NUMERIC_FEATURES exactly
   * rather than inventing a different factor set for the dashboard.
   */
  getIntradayEngineFactorProfile: async (strategy: string) => {
    const { data } = await queryTable<{
      id: number; symbol: string; trade_date: string; confidence: number | null;
      outcome: string | null; meta: Record<string, unknown> | string | null;
    }>('intraday_setups', {
      select: 'id,symbol,trade_date,confidence,outcome,meta',
      filter: { strategy, cost_verdict: 'TAKEN' },
      limit: 500,
    });
    const resolved = (data ?? []).filter((r) => r.outcome === 'TARGET' || r.outcome === 'STOP');
    const withMeta = resolved.map((r) => ({
      ...r,
      meta: (typeof r.meta === 'string'
        ? (() => { try { return JSON.parse(r.meta as string); } catch { return {}; } })()
        : r.meta) as Record<string, unknown>,
    }));
    return {
      winners: withMeta.filter((r) => r.outcome === 'TARGET'),
      losers: withMeta.filter((r) => r.outcome === 'STOP'),
    };
  },

  /** Best-effort match back to the detection row that produced an intraday
   *  position — no FK exists (intraday has no signal_log row to key off, the
   *  same reason closed_positions.signal_id is always null for it), so this
   *  matches on (symbol, trade_date, strategy). Ambiguous only if the same
   *  engine fired twice on one symbol in one day, which same-day dedup
   *  (_setup_is_new) already prevents. */
  getMatchingIntradaySetup: (symbol: string, tradeDate: string, strategy: string) =>
    queryTable<{
      id: number; symbol: string; trade_date: string; confidence: number | null;
      outcome: string | null; meta: Record<string, unknown> | string | null;
    }>('intraday_setups', {
      select: 'id,symbol,trade_date,confidence,outcome,meta',
      filter: { symbol, trade_date: tradeDate, strategy, cost_verdict: 'TAKEN' },
      limit: 5,
    }),

  /** Storage headroom, from the view tools/health reads. Red above 80% is a
   *  FAIL there, not a warning — the dashboard uses the same threshold.
   *  Limit 200 rather than a display-sized number: tools.health pages through
   *  every row to sum pct_of_free_tier, and summing only a "top N" slice here
   *  would silently under-report the true total the moment the schema grows
   *  past that N — the exact silent-undercount failure this project keeps
   *  finding elsewhere. ~51 tables exist today; 200 is headroom, not a cap. */

  /**
   * Today's funnel, both books — how many names were even looked at, how
   * many produced a ranked plan/detected setup, how many were actually
   * entered. Uses queryTable's exact count (Supabase's `count: 'exact'`
   * head) rather than fetching rows, so a wide day doesn't pull real data
   * just to count it. `signal_output_daily`/`master_shortlist`/
   * `intraday_universe`/`intraday_setups` — no invented aggregate table.
   */
  /**
   * The full detected → taken path, both books — backs "Today's Signal
   * Funnel" on Overview. `planDate` is the evening pipeline's plan date
   * (signal_output_daily/master_shortlist are written the evening BEFORE
   * the session they're for); `tradeDate` is today, what the live daemon
   * stamps on allocation_decisions/open_positions/intraday_setups.
   *
   * Swing stops at 3 real stages (Watched → Allocator-scored → Taken), not
   * the mockup's 4 — the backend has no persisted "entered the buy zone"
   * count independent of the allocator call itself; evaluate_candidates
   * decides that in memory every 15s and never writes a row for it.
   * Inventing a number for a box the schema can't back is the exact
   * silent-default failure CLAUDE.md warns about, so the box is dropped
   * rather than guessed.
   *
   * "Allocator-scored" reads the SAME 500-row recent-first window
   * getAllocationToday() uses for the Allocator tab's own live hurdle —
   * not a fresh unbounded query. allocation_decisions gets a new row every
   * ~15s per live candidate (2,700+ swing rows on an ordinary session), so
   * counting distinct symbols over the FULL day would mean fetching a
   * table that grows all session long, on every Overview load. Reusing the
   * capped window trades a small undercount (~15-20%, confirmed against a
   * full-table COUNT DISTINCT) for a bounded, already-employed query
   * rather than adding a second unbounded fetch pattern.
   *
   * Intraday's 5 stages ARE all real: intraday_setups.cost_verdict records
   * the exact gate a detection stopped at (BLOCKED_STRUCTURE/BLOCKED_EVENT
   * → VETOED_AI → BELOW_CONVICTION → TAKEN/REJECTED_COST, in that order —
   * see engine.py's evaluate_intraday_setups), so each stage is a real
   * subtraction, not an estimate.
   */
  getSignalFunnelDetail: async (planDate: string, tradeDate: string) => {
    const head = (table: string, filter: Record<string, unknown>) =>
      queryTable(table, { select: 'symbol', filter, limit: 1 }).then((r) => r.count ?? 0);

    const [watched, openSwing, closedSwing, scanned, setupsRes, allocRes] = await Promise.all([
      head('master_shortlist', { date: planDate }),
      head('open_positions', { entry_date: tradeDate, framework: 'SWING' }),
      head('closed_positions', { entry_date: tradeDate, framework: 'SWING' }),
      head('intraday_universe', { trade_date: tradeDate }),
      queryTable<{ cost_verdict: string | null }>('intraday_setups', {
        select: 'cost_verdict', filter: { trade_date: tradeDate }, limit: 2000,
      }),
      queryTable<{ symbol: string; framework: string; hurdle: number | null; decided_at: string }>(
        'allocation_decisions', {
          select: 'symbol,framework,hurdle,decided_at', filter: { trade_date: tradeDate },
          order: { column: 'decided_at', ascending: false }, limit: 500,
        },
      ),
    ]);

    const setupRows = setupsRes.data ?? [];
    const gateBlocked = setupRows.filter((r) => r.cost_verdict === 'BLOCKED_STRUCTURE' || r.cost_verdict === 'BLOCKED_EVENT').length;
    const vetoedAi = setupRows.filter((r) => r.cost_verdict === 'VETOED_AI').length;
    const belowConviction = setupRows.filter((r) => r.cost_verdict === 'BELOW_CONVICTION').length;
    const taken = setupRows.filter((r) => r.cost_verdict === 'TAKEN').length;
    const detected = setupRows.length;
    const aiCleared = Math.max(0, detected - gateBlocked - vetoedAi);
    const convictionFloor = Math.max(0, aiCleared - belowConviction);

    const allocRows = allocRes.data ?? [];
    const swingSymbols = new Set(allocRows.filter((r) => r.framework === 'SWING').map((r) => r.symbol));
    const bars: Record<string, number | null> = {};
    for (const r of allocRows) if (!(r.framework in bars)) bars[r.framework] = r.hurdle;

    return {
      swing: { watched, scored: swingSymbols.size, taken: openSwing + closedSwing, bar: bars.SWING ?? null },
      intraday: { scanned, detected, aiCleared, convictionFloor, taken, bar: bars.INTRADAY ?? null },
    };
  },

  /** Engine registry — real label/description/lifecycle per engine, both
   *  books. This is what backs an engine card's "conditions" text: it's
   *  transcribed from the actual declarative gates (migration 006), not
   *  copy invented for a screen. */
  getStrategyConfig: () =>
    queryTable<{ strategy: string; label: string | null; description: string | null; lifecycle: string | null; engine_type: string | null }>(
      'strategy_config', { select: 'strategy,label,description,lifecycle,engine_type' },
    ),
  getIntradayStrategyConfig: () =>
    queryTable<{ strategy: string; label: string | null; description: string | null; lifecycle: string | null; phases: string | null }>(
      'intraday_strategy_config', { select: 'strategy,label,description,lifecycle,phases' },
    ),

  /**
   * Engine grid stats, both books — setups detected, taken, hit rate, avg
   * net%, and a resolved-outcome sequence for the sparkline (win/loss per
   * resolved trade, chronological). Bulk-fetched and grouped client-side —
   * 19 engines × per-engine queries would be dozens of round trips; this is
   * 3 queries total regardless of how many engines exist. Swing "setups" is
   * signal_log (every signal the evening pipeline produced, taken or not);
   * intraday "setups" is intraday_setups (every detection).
   *
   * Every row is attributed to EVERY engine named in its (possibly combo)
   * strategy string via swingBaseEngines() — see that function's docstring.
   * A "CTL+MOM+SEC" close counts once for CTL, once for MOM and once for
   * SEC, full credit each, not a fraction split three ways: all three
   * engines genuinely fired independently that day (screen_stocks.py scores
   * each one before joining the string), so each earns the whole outcome as
   * its own evidence — the same attribution screen_stocks.py's own
   * engines_list comment describes for forward-return measurement.
   */
  getSwingEngineGridStats: async () => {
    const [signals, closed, open] = await Promise.all([
      queryTable<{ strategy: string }>('signal_log', { select: 'strategy', limit: 5000 }),
      queryTable<{ strategy: string; realized_pnl: number | null; pnl_pct: number | null; exit_date: string | null }>(
        'closed_positions', { select: 'strategy,realized_pnl,pnl_pct,exit_date', filter: { framework: 'SWING' }, order: { column: 'exit_date', ascending: true }, limit: 2000 },
      ),
      queryTable<{ strategy: string }>('open_positions', { select: 'strategy', filter: { framework: 'SWING' }, limit: 500 }),
    ]);
    const setups = new Map<string, number>();
    for (const r of signals.data ?? []) {
      for (const code of swingBaseEngines(r.strategy)) setups.set(code, (setups.get(code) ?? 0) + 1);
    }
    const takenOpen = new Map<string, number>();
    for (const r of open.data ?? []) {
      for (const code of swingBaseEngines(r.strategy)) takenOpen.set(code, (takenOpen.get(code) ?? 0) + 1);
    }
    const byStrategy = new Map<string, { realized_pnl: number | null; pnl_pct: number | null; exit_date: string | null }[]>();
    for (const r of closed.data ?? []) {
      for (const code of swingBaseEngines(r.strategy)) {
        if (!byStrategy.has(code)) byStrategy.set(code, []);
        byStrategy.get(code)!.push(r);
      }
    }
    return { setups, takenOpen, closedByStrategy: byStrategy };
  },

  getIntradayEngineGridStats: async () => {
    const { data } = await queryTable<{
      strategy: string; cost_verdict: string | null; outcome: string | null;
      outcome_pct: number | null; trade_date: string;
    }>('intraday_setups', {
      select: 'strategy,cost_verdict,outcome,outcome_pct,trade_date',
      order: { column: 'trade_date', ascending: true },
      limit: 8000,
    });
    const rows = data ?? [];
    const setups = new Map<string, number>();
    const takenByStrategy = new Map<string, typeof rows>();
    for (const r of rows) {
      if (!r.strategy) continue;
      setups.set(r.strategy, (setups.get(r.strategy) ?? 0) + 1);
      if (r.cost_verdict === 'TAKEN') {
        if (!takenByStrategy.has(r.strategy)) takenByStrategy.set(r.strategy, []);
        takenByStrategy.get(r.strategy)!.push(r);
      }
    }
    return { setups, takenByStrategy };
  },

  getStorageUsage: () =>
    queryTable<{
      table_name: string; total_size: string; total_bytes: number;
      approx_rows: number; pct_of_free_tier: number;
    }>('v_storage_usage', { order: { column: 'total_bytes', ascending: false }, limit: 200 }),

  /** The intraday_* gates from system_config, so the UI shows the live phase. */
  getIntradayGates: async () => {
    const { data } = await queryTable<{ key: string; value: string }>(
      'system_config', { select: 'key,value', limit: 500 },
    );
    const g: Record<string, string> = {};
    for (const r of data ?? []) {
      if (r.key.startsWith('intraday_') || r.key.startsWith('gtt_')) g[r.key] = r.value;
    }
    return g;
  },

  /**
   * The live exit policy, read from the same system_config rows the backend
   * reads through cfg_float().
   *
   * The R thresholds are drawn on every position card, so a hardcoded copy here
   * would quietly start lying the moment a value is tuned in the Control Room —
   * the dashboard would show a rung at 1.5R while the pipeline booked at 2.0R.
   * Defaults mirror control/position_lifecycle.load_exit_policy() and apply only
   * when a key is genuinely absent.
   */
  getExitPolicy: async () => {
    const keys = [
      'exit_partial_book_r', 'exit_partial_book_pct', 'exit_trail_r',
      'exit_trail_after_r', 'exit_target_r', 'exit_time_stop_days',
      'exit_time_stop_min_r', 'exit_move_to_breakeven',
    ];
    const defaults: Record<string, number> = {
      exit_partial_book_r: 1.5, exit_partial_book_pct: 50, exit_trail_r: 1.5,
      exit_trail_after_r: 2.0, exit_target_r: 3.0, exit_time_stop_days: 15,
      exit_time_stop_min_r: 0.5,
    };
    const { data, error } = await queryTable<{ key: string; value: string }>(
      'system_config', { select: 'key,value', limit: 500 },
    );
    const values: Record<string, number> = { ...defaults };
    let source: 'defaults' | 'system_config' = 'defaults';
    if (!error && data) {
      let hit = 0;
      for (const row of data) {
        if (!keys.includes(row.key)) continue;
        const n = parseFloat(row.value);
        if (Number.isFinite(n)) { values[row.key] = n; hit++; }
      }
      if (hit) source = 'system_config';
    }
    return { ...values, source } as Record<string, number> &
      { source: 'defaults' | 'system_config' };
  },

  // closed_positions  ← NOT 'positions'
  getClosedPositions: (limit = 50, offset = 0) =>
    queryTable<ClosedPosition>('closed_positions', {
      order: { column: 'exit_date', ascending: false },
      limit,
      offset,
    }),

  // ── Daily Summary Dashboard, 26-Aug-2026 ────────────────────────────────
  // No existing query filters open_positions/closed_positions by framework
  // (confirmed before adding these — every other position read here fetches
  // everything and filters client-side; kept the same pattern rather than
  // inventing a server-side aggregate this codebase doesn't otherwise use).

  /**
   * Open (ACTIVE) positions for one book — SWING or INTRADAY.
   *
   * `select` defaults to every column (unchanged for every existing caller —
   * PositionsTab's table+detail dialog genuinely reads most of them). Pass a
   * narrow column list from a caller that only needs a handful — see
   * DailyBookSummary, which used to pull the full ~50-column row on a 60s
   * timer purely to sum five numeric fields. Confirmed against a day of real
   * edge_logs traffic that this was a meaningful chunk of frontend egress.
   */
  getOpenPositionsByFramework: (framework: 'SWING' | 'INTRADAY', select?: string) =>
    queryTable<OpenPosition>('open_positions', {
      select,
      filter: { framework, status: 'ACTIVE' },
      order: { column: 'entry_date', ascending: false },
    }),

  /**
   * ALL closed positions for one book — not the 50-row page
   * getClosedPositions() above returns. This is a client-side SUM(realized_pnl)
   * input, not a table to render, so it needs the whole history rather than
   * a display page. 2000 is a generous ceiling for how young this system's
   * trade history is; revisit if it's ever actually hit.
   *
   * `select` — see getOpenPositionsByFramework's docstring, same reasoning.
   */
  getAllClosedPositionsByFramework: (framework: 'SWING' | 'INTRADAY', select?: string) =>
    queryTable<ClosedPosition>('closed_positions', {
      select,
      filter: { framework },
      order: { column: 'exit_date', ascending: false },
      limit: 2000,
    }),

  /** Recent daily book-value snapshots for one book (tools/snapshot_book_value.py), newest first. */
  getBookValueSnapshots: (framework: 'SWING' | 'INTRADAY', limit = 30) =>
    queryTable<{
      date: string; framework: string; sleeve: number;
      realized_pnl_cum: number; unrealized_pnl: number; book_value: number;
      created_at: string;
    }>('book_value_snapshots', {
      filter: { framework },
      order: { column: 'date', ascending: false },
      limit,
    }),

  // signal_log  ← NOT 'signals'
  getSignals: (filter?: Record<string, unknown>, limit = 100) =>
    queryTable<Signal>('signal_log', {
      order: { column: 'date', ascending: false },
      filter,
      limit,
    }),

  /**
   * The ranked, tiered output for the latest pipeline run.
   *
   * signal_output_daily rather than signal_log: it is the FINAL artefact of the
   * run — the only table carrying ai_tier, expected_r and dist_entry_pct — and
   * it is the table alerts/send_alerts.py reads. Showing the dashboard anything
   * else guarantees it can disagree with the Telegram message for the same
   * symbol on the same day.
   */
  getTradePlans: async (limit = 40) => {
    const latest = await queryTable<{ date: string }>('signal_output_daily', {
      select: 'date', order: { column: 'date', ascending: false }, limit: 1,
    });
    const day = latest.data?.[0]?.date;
    if (!day) return { data: [] as Signal[], date: null, error: latest.error };
    const { data, error } = await queryTable<Signal>('signal_output_daily', {
      select: 'date,symbol,company_name,sector,industry,strategy,signal_type,'
            + 'ai_tier,ai_conviction,ai_conviction_reason,ai_suggested_action,'
            + 'score,final_score,expected_r,entry_zone_low,entry_zone_high,'
            + 'current_price,dist_entry_pct,planned_stop,planned_target,'
            + 'planned_risk_pct,implied_rr,ai_max_chase_pct,regime,'
            + 'sector_rank_at_entry,position_state,filter_reason',
      filter: { date: day },
      order: { column: 'final_score', ascending: false },
      limit,
    });
    return { data: data ?? [], date: day, error };
  },

  // AI provider stats.
  //
  // This used to query `ai_model_performance` (the comment here even claimed
  // "table exists"). It does not exist in the project — the request 404s, so
  // the AI Provider Stats panel has always rendered empty.
  //
  // ai_context is the table that actually records which provider answered:
  // one row per symbol per day carrying provider, fallback_used and
  // confidence. Aggregate it into the shape the panel already expects rather
  // than inventing a new table for data we are already writing.
  getAIModelPerformance: async () => {
    const { data, error } = await queryTable<{
      date: string; symbol: string; provider: string | null;
      fallback_used: boolean | null; confidence: number | null;
    }>('ai_context', {
      select: 'date,symbol,provider,fallback_used,confidence',
      order: { column: 'date', ascending: false },
      limit: 1000,
    });
    if (error || !data) return { data: null, error, count: null };

    // Latest row per provider, plus that provider's call count and mean
    // confidence for the most recent date it appears on.
    const byProvider = new Map<string, AIModelPerformance>();
    const tally = new Map<string, { calls: number; conf: number[]; date: string }>();

    for (const r of data) {
      const p = r.provider || 'unknown';
      const t = tally.get(p);
      if (!t) {
        tally.set(p, { calls: 1, conf: r.confidence != null ? [r.confidence] : [], date: r.date });
      } else if (t.date === r.date) {
        t.calls += 1;
        if (r.confidence != null) t.conf.push(r.confidence);
      }
    }

    let i = 0;
    for (const [provider, t] of tally) {
      byProvider.set(provider, {
        id: ++i,
        date: t.date,
        provider,
        model: provider,
        calls_today: t.calls,
        avg_confidence: t.conf.length
          ? Number((t.conf.reduce((a, b) => a + b, 0) / t.conf.length).toFixed(3))
          : undefined,
        fallback_used: data.some((r) => (r.provider || 'unknown') === provider && r.fallback_used),
        created_at: t.date,
      });
    }
    return { data: [...byProvider.values()], error: null, count: byProvider.size };
  },

  // brain_proposals  ← NOT 'proposals'
  getBrainProposals: (status?: string) =>
    queryTable<BrainProposal>('brain_proposals', {
      filter: status ? { status } : undefined,
      order: { column: 'created_at', ascending: false },
      limit: 50,
    }),

  // brain_analysis_log  ← table exists
  getBrainAnalysisLog: (limit = 10) =>
    queryTable<BrainAnalysisLog>('brain_analysis_log', {
      order: { column: 'run_date', ascending: false },
      limit,
    }),

  // lessons  ← table exists, field names differ from old frontend type
  getLessons: (filter?: Record<string, unknown>) =>
    queryTable<Lesson>('lessons', {
      filter: { ...filter, is_active: true },
      order: { column: 'created_at', ascending: false },
    }),

  /**
   * system_config  ← NOT 'config'
   *
   * ~600 rows, `select=*` on every one when `keys` is omitted — real cost
   * for a caller that only needs a handful. Confirmed against a day of
   * edge_logs: this exact `select=*&order=key.asc` shape was the single
   * largest frontend egress contributor, because DailyBookSummary called it
   * on a 60s timer just to read swing_capital/intraday_capital and two
   * daily-cap keys. Pass `keys` and only `key,value` for those rows comes
   * back — DataManagementTab's config browser is the one legitimate caller
   * of the unfiltered form, since showing the whole table is its job.
   */
  getSystemConfig: (keys?: string[]) =>
    queryTable<ConfigEntry>('system_config', {
      select: keys ? 'key,value' : undefined,
      filter: keys ? { key: keys } : undefined,
      order: { column: 'key', ascending: true },
    }),

  // config_change_log  ← NOT 'config_changes'
  getConfigChanges: (limit = 30) =>
    queryTable<ConfigChange>('config_change_log', {
      order: { column: 'changed_at', ascending: false },
      limit,
    }),

  // master_shortlist  ← table exists, project subset of 97 cols
  getMasterShortlist: (dateFilter?: string) =>
    queryTable<MasterStock>('master_shortlist', {
      select: [
        'date', 'symbol', 'company_name', 'sector', 'strategy_source',
        'current_price', 'final_score', 'composite_score', 'priority_rank',
        'sector_rank', 'days_in_list', 'momentum_state', 'momentum_phase',
        'velocity_state', 'trend_maturity', 'struct_edge', 'entry_timing_type',
        'entry_ready', 'in_position', 'lifecycle', 'expected_r',
        'validity_score', 'exec_eligibility', 'ai_conviction',
        'ai_conviction_reason', 'ai_suggested_action', 'ai_shortlist_rank',
        'active_regime', 'fii_flag', 'trade_allowed', 'compute_source',
      ].join(', '),
      filter: dateFilter ? { date: dateFilter } : undefined,
      order: { column: 'priority_rank', ascending: true },
    }),

  // performance_metrics  ← table exists; engine_stats is a JSONB column inside it
  getPerformanceMetrics: (grain: 'daily' | 'weekly' | 'monthly' = 'weekly', limit = 26) =>
    queryTable<PerformanceMetricsRow>('performance_metrics', {
      filter: { grain },
      order: { column: 'metric_date', ascending: false },
      limit,
    }),

  // msl_history  ← for trajectory analysis
  getMslHistory: (symbol?: string, limit = 30) =>
    queryTable<MslHistoryEntry>('msl_history', {
      filter: symbol ? { symbol } : undefined,
      order: { column: 'snapshot_date', ascending: false },
      limit,
    }),

  // NOTE: 'pipeline_runs' table does not exist in this schema.
  // Use brain_analysis_log for run history instead.
};

export default supabase;
