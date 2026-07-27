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

  // system_config  ← NOT 'config'
  getSystemConfig: () =>
    queryTable<ConfigEntry>('system_config', {
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
