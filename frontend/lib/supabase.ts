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
