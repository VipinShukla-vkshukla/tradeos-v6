CREATE TABLE public.ai_context (
    date date NOT NULL,
    symbol text NOT NULL,
    conviction text,
    conviction_reason text,
    risks jsonb,
    catalyst text,
    suggested_action text,
    strategy_validation text,
    conflicts text,
    ai_note text,
    provider text,
    fallback_used boolean DEFAULT false,
    confidence numeric,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.ai_model_performance (
    id bigint NOT NULL DEFAULT nextval('ai_model_performance_id_seq'::regclass),
    date date NOT NULL DEFAULT CURRENT_DATE,
    provider text NOT NULL,
    model text,
    calls_today integer DEFAULT 0,
    cost_today numeric DEFAULT 0,
    accuracy numeric,
    avg_confidence numeric,
    fallback_used boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.chartink_raw_data (
    id bigint NOT NULL DEFAULT nextval('chartink_raw_data_id_seq'::regclass),
    date date NOT NULL,
    symbol text NOT NULL,
    sector text,
    industry text,
    market_cap numeric,
    market_cap_cat text,
    daily_open numeric,
    daily_high numeric,
    daily_low numeric,
    daily_close numeric,
    week52_high numeric,
    week52_low numeric,
    high_30d numeric,
    sma_10 numeric,
    sma_20 numeric,
    sma_50 numeric,
    sma_200 numeric,
    ema_10 numeric,
    ema_20 numeric,
    ema_50 numeric,
    rsi_daily numeric,
    rsi_weekly numeric,
    rsi_monthly numeric,
    adx_14 numeric,
    adx_plus_di numeric,
    adx_minus_di numeric,
    volume bigint,
    avg_vol_20 numeric,
    avg_vol_50 numeric,
    vwap_daily numeric,
    vwap_20d numeric,
    vwap_50d numeric,
    pct_change numeric,
    atr_14 numeric,
    atr_pct numeric,
    ha_high numeric,
    ha_low numeric,
    ha_close numeric,
    supertrend numeric,
    macd_line numeric,
    macd_signal numeric,
    macd_histogram numeric,
    parabolic_sar numeric,
    upper_bb numeric,
    lower_bb numeric,
    stochastic numeric,
    ttm_net_profit numeric,
    net_profit_yr numeric,
    eps numeric,
    qtr_net_profit numeric,
    qtr_var_profit numeric,
    ingested_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.closed_positions (
    id bigint NOT NULL DEFAULT nextval('closed_positions_id_seq'::regclass),
    symbol text,
    company_name text,
    sector text,
    strategy text,
    entry_date date,
    entry_price numeric,
    proposed_qty numeric,
    actual_qty numeric,
    invested_value numeric,
    exit_date date,
    exit_price numeric,
    exit_value numeric,
    realized_pnl numeric,
    pnl_pct numeric,
    high_water_mark numeric,
    max_favorable_excursion numeric,
    lifecycle_at_entry text,
    entry_timing_type text,
    reentry_mode text,
    expected_r_at_entry numeric,
    exit_reason text,
    signal_date date
);

CREATE TABLE public.data_anomalies (
    id bigint NOT NULL DEFAULT nextval('data_anomalies_id_seq'::regclass),
    date date,
    check_name text,
    severity text,
    value text,
    message text,
    affected text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.event_calendar (
    id bigint NOT NULL DEFAULT nextval('event_calendar_id_seq'::regclass),
    event_category text,
    event_name text,
    event_type text,
    start_date date,
    end_date date,
    affected_sectors text,
    event_bias text,
    event_intensity text,
    strategy_impact text,
    notes text,
    event_status text,
    is_active boolean,
    priority integer,
    is_risk_on_accelerator text
);

CREATE TABLE public.evolution_proposals (
    id bigint NOT NULL DEFAULT nextval('evolution_proposals_id_seq'::regclass),
    proposed_date date,
    strategy text,
    param_name text,
    current_value text,
    proposed_value text,
    evidence text,
    expected_improvement text,
    trades_analyzed integer,
    status text DEFAULT 'PENDING'::text,
    approved_by text,
    approved_at timestamp with time zone,
    applied_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    week_of text,
    confidence numeric,
    impact_measured_at timestamp with time zone,
    performance_delta numeric,
    notes text
);

CREATE TABLE public.fii_dii_flow (
    date date NOT NULL,
    fii_gross_buy_cr numeric,
    fii_gross_sell_cr numeric,
    fii_net_cr numeric,
    dii_gross_buy_cr numeric,
    dii_gross_sell_cr numeric,
    dii_net_cr numeric,
    source text DEFAULT 'NSE'::text,
    created_at timestamp with time zone DEFAULT now(),
    fii_gross_buy numeric,
    fii_gross_sell numeric,
    fii_net numeric,
    dii_gross_buy numeric,
    dii_gross_sell numeric,
    dii_net numeric,
    fii_net_5d numeric,
    fii_net_10d numeric,
    fii_net_20d numeric,
    fii_flag text
);

CREATE TABLE public.global_cues (
    date date NOT NULL,
    session text NOT NULL,
    gift_nifty numeric,
    gift_nifty_chg_pct numeric,
    gap_signal text,
    usd_inr numeric,
    usd_inr_chg_pct numeric,
    brent_crude numeric,
    brent_chg_pct numeric,
    gold_price numeric,
    us_dow_close numeric,
    us_nasdaq_close numeric,
    sector_impacts jsonb,
    created_at timestamp with time zone DEFAULT now(),
    us_dow_chg_pct numeric,
    us_nasdaq_chg_pct numeric,
    sp500_close numeric,
    sp500_chg_pct numeric,
    us_10yr_yield numeric,
    us_10yr_chg_bps numeric,
    silver_price numeric,
    silver_chg_pct numeric
);

CREATE TABLE public.industry_strength (
    id bigint NOT NULL DEFAULT nextval('industry_strength_id_seq'::regclass),
    date date NOT NULL,
    industry text NOT NULL,
    stock_count integer,
    avg_rsi_daily numeric,
    avg_rsi_weekly numeric,
    avg_rsi_monthly numeric,
    avg_ret_6m numeric,
    breadth_sma50 numeric,
    rank integer,
    top5_flag boolean,
    industry_state text,
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.lessons (
    id bigint NOT NULL DEFAULT nextval('lessons_id_seq'::regclass),
    date date,
    scenario_type text,
    trigger_event text,
    linked_event_type text,
    impacted_sector text,
    scenario_context text,
    what_expected text,
    what_happened text,
    what_failed text,
    root_cause text,
    corrective_rule text,
    source text DEFAULT 'MANUAL'::text,
    created_at timestamp with time zone DEFAULT now(),
    is_active boolean DEFAULT true,
    times_applied integer DEFAULT 0,
    times_worked integer DEFAULT 0,
    confidence numeric DEFAULT 1.0,
    linked_symbols text[] DEFAULT '{}'::text[]
);

CREATE TABLE public.macro_indicators (
    id bigint NOT NULL DEFAULT nextval('macro_indicators_id_seq'::regclass),
    indicator_date date NOT NULL,
    indicator_name text NOT NULL,
    indicator_value numeric,
    previous_value numeric,
    change_bps numeric,
    source text,
    release_date date,
    ingested_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.market_news (
    id bigint NOT NULL DEFAULT nextval('market_news_id_seq'::regclass),
    news_date date NOT NULL DEFAULT CURRENT_DATE,
    headline text NOT NULL,
    source text,
    category text,
    impact_type text,
    parsed_sectors text[],
    parsed_symbols text[],
    magnitude text,
    raw_url text,
    ingested_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.market_regime (
    date date NOT NULL,
    regime text,
    nifty_price numeric,
    nifty_50dma numeric,
    nifty_200dma numeric,
    nifty_weekly_rsi numeric,
    india_vix numeric,
    gift_nifty numeric,
    avg_sector_breadth numeric,
    ctl_enabled boolean,
    sbs_enabled boolean,
    tpo_enabled boolean,
    eap_enabled boolean,
    max_positions integer,
    raw_data jsonb,
    created_at timestamp with time zone DEFAULT now(),
    predicted_regime text,
    regime_confidence numeric,
    regime_predicted_at timestamp with time zone,
    regime_score double precision,
    nifty_1d_chg_pct double precision,
    nifty_5d_chg_pct double precision,
    nifty_20d_chg_pct double precision,
    advance_decline_ratio double precision,
    above_200dma_pct double precision
);

CREATE TABLE public.master_shortlist (
    date date NOT NULL,
    symbol text NOT NULL,
    company_name text,
    sector text,
    strategy_source text,
    current_price numeric,
    final_score numeric,
    days_in_list integer,
    rank_vel_3d numeric,
    score_vel_5d numeric,
    momentum_state text,
    momentum_phase text,
    velocity_state text,
    trend_maturity text,
    fv_low numeric,
    fv_high numeric,
    price_location text,
    dist_fv_pct numeric,
    struct_edge text,
    entry_timing_type text,
    entry_ready boolean,
    in_position boolean,
    reentry_mode text,
    lifecycle text,
    expected_r numeric,
    validity_score numeric,
    exec_eligibility text,
    entry_zone_low numeric,
    entry_zone_high numeric,
    entry_mode text,
    dist_entry_pct numeric,
    entry_action text,
    opp_type text,
    base_rank integer,
    base_score numeric,
    event_bias text,
    event_sectors text,
    upcoming_news text,
    is_ipo boolean,
    trade_allowed boolean,
    suggested text,
    pos_type text,
    notes text,
    position_state text,
    ai_conviction text,
    ai_conviction_reason text,
    ai_risks jsonb,
    ai_suggested_action text,
    ai_note text,
    ai_provider text,
    ai_fallback_used boolean,
    created_at timestamp with time zone DEFAULT now(),
    ai_shortlist_rank integer,
    ai_shortlist_reason text
);

CREATE TABLE public.ml_training_log (
    id bigint NOT NULL DEFAULT nextval('ml_training_log_id_seq'::regclass),
    trained_at timestamp with time zone DEFAULT now(),
    trades_used integer,
    features_used jsonb,
    accuracy numeric,
    precision_score numeric,
    recall_score numeric,
    model_version text,
    notes text
);

CREATE TABLE public.msl_history (
    snapshot_date date NOT NULL,
    symbol text NOT NULL,
    company_name text,
    priority_rank integer,
    sector text,
    strategy_source text,
    close_price numeric,
    price_location text,
    dist_fv_pct numeric,
    entry_timing_type text,
    momentum_phase text,
    velocity_state text,
    trend_maturity text,
    struct_edge text,
    in_position boolean,
    reentry_mode text,
    lifecycle text,
    expected_r numeric,
    validity_score numeric,
    final_score numeric
);

CREATE TABLE public.nifty_total_market (
    symbol text NOT NULL,
    company_name text,
    industry text,
    isin text,
    nifty_200 boolean,
    nifty_500 boolean
);

CREATE TABLE public.nifty_upcoming_events (
    id bigint NOT NULL DEFAULT nextval('nifty_upcoming_events_id_seq'::regclass),
    symbol text,
    company_name text,
    purpose text,
    details text,
    event_date date,
    days_to_event integer,
    source text DEFAULT 'SHEET'::text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.nse_holidays (
    date date NOT NULL,
    occasion text
);

CREATE TABLE public.open_positions (
    symbol text NOT NULL,
    company_name text,
    sector text,
    strategy text,
    entry_date date,
    entry_price numeric,
    proposed_qty numeric,
    actual_qty numeric,
    invested_value numeric,
    current_price numeric,
    current_value numeric,
    unrealized_pnl numeric,
    pnl_pct numeric,
    locked_profit numeric,
    lifecycle text,
    sl_type text,
    initial_sl_atr numeric,
    high_water_mark numeric,
    active_sl numeric,
    exit_signal text,
    action_required text,
    event_risk text,
    upcoming_news text,
    target_1 numeric,
    target_2 numeric,
    target_3 numeric,
    status text DEFAULT 'ACTIVE'::text,
    synced_at timestamp with time zone DEFAULT now(),
    kite_qty integer,
    reconcile_status text,
    last_reconciled_at timestamp with time zone,
    sl_breach_alerted boolean DEFAULT false,
    sl_proximity_alerted boolean DEFAULT false,
    target_price numeric,
    target_pct numeric,
    target_hit boolean DEFAULT false,
    target_hit_at timestamp with time zone,
    trailing_sl_pct numeric,
    signal_id bigint,
    signal_date date,
    signal_subtype text,
    original_qty integer,
    current_qty integer,
    partial_bookings jsonb
);

CREATE TABLE public.order_history (
    id bigint NOT NULL DEFAULT nextval('order_history_id_seq'::regclass),
    order_date date NOT NULL DEFAULT CURRENT_DATE,
    symbol text NOT NULL,
    signal_id bigint,
    broker_order_id text,
    order_type text,
    qty_requested integer,
    qty_executed integer,
    price_requested numeric,
    price_executed numeric,
    slippage_pct numeric,
    status text,
    rejection_reason text,
    kite_response jsonb,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.raw_prices (
    date date NOT NULL,
    symbol text NOT NULL,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    prev_close numeric,
    volume bigint,
    value_cr numeric,
    delivery_pct numeric,
    delivery_qty bigint,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.regime_history (
    date date,
    regime text,
    nifty_price numeric,
    nifty_50dma numeric,
    nifty_200dma numeric,
    nifty_weekly_rsi numeric,
    india_vix numeric,
    avg_sector_breadth numeric,
    ctl_enabled boolean,
    sbs_enabled boolean,
    tpo_enabled boolean,
    eap_enabled boolean,
    regime_score double precision,
    nifty_1d_chg_pct double precision,
    nifty_5d_chg_pct double precision,
    nifty_20d_chg_pct double precision,
    advance_decline_ratio double precision,
    above_200dma_pct double precision
);

CREATE TABLE public.safety_lists (
    symbol text NOT NULL,
    list_type text NOT NULL,
    stage text,
    reason text,
    effective_date date,
    source text DEFAULT 'NSE'::text,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.scanner_signals (
    id bigint NOT NULL DEFAULT nextval('scanner_signals_id_seq'::regclass),
    date date,
    symbol text,
    pattern text,
    confidence numeric,
    details jsonb,
    in_msl boolean,
    created_at timestamp with time zone DEFAULT now(),
    pattern_type text
);

CREATE TABLE public.sector_strength (
    date date NOT NULL,
    sector text NOT NULL,
    stock_count integer,
    avg_rsi_daily numeric,
    avg_rsi_weekly numeric,
    avg_rsi_monthly numeric,
    avg_ret_6m numeric,
    breadth_sma50 numeric,
    rank integer,
    top4_flag boolean,
    sector_state text,
    fii_flow_sector numeric
);

CREATE TABLE public.shadow_trades (
    id bigint NOT NULL DEFAULT nextval('shadow_trades_id_seq'::regclass),
    signal_id bigint,
    symbol text NOT NULL,
    strategy text,
    action text,
    entry_price numeric,
    qty integer,
    approved_at timestamp with time zone DEFAULT now(),
    would_execute boolean DEFAULT false,
    notes text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.signal_log (
    id bigint NOT NULL DEFAULT nextval('signal_log_id_seq'::regclass),
    date date,
    symbol text,
    company_name text,
    sector text,
    strategy text,
    signal_type text,
    position_state text,
    score numeric,
    in_rule_engine boolean,
    in_scanner boolean,
    eap_action text,
    regime text,
    regime_warning boolean DEFAULT false,
    asm_flag boolean DEFAULT false,
    fo_ban_flag boolean DEFAULT false,
    ai_conviction text,
    ai_suggested_action text,
    ai_provider text,
    ai_fallback_used boolean DEFAULT false,
    fii_flag text,
    outcome text,
    outcome_pnl_pct numeric,
    created_at timestamp with time zone DEFAULT now(),
    industry text,
    industry_rank integer,
    industry_top5 boolean DEFAULT false,
    industry_state text,
    industry_avg_rsi numeric,
    ai_note text,
    ai_conviction_reason text,
    ai_confidence double precision,
    rsi_daily double precision,
    rsi_weekly double precision,
    adx double precision,
    vol_ratio double precision,
    delivery_pct double precision,
    atr_pct double precision,
    ret_6m double precision,
    dist_sma50 double precision,
    days_in_list integer,
    ai_strategy_validation text,
    signal_subtype text,
    score_adjusted numeric,
    sheet_conflict boolean DEFAULT false,
    sheet_conflict_type text,
    rsi_monthly double precision,
    rs_vs_nifty double precision,
    consol_range double precision,
    ret_1m double precision,
    ret_3m double precision,
    above_sma50 boolean,
    breakout_setup boolean,
    validity_score double precision,
    expected_r_msl double precision,
    trend_maturity text,
    velocity_state text,
    momentum_phase text,
    days_to_trigger_est integer,
    sector_rank_at_entry integer,
    scanner_patterns text,
    execution_status text,
    kite_order_id text,
    execution_price numeric,
    executed_at timestamp with time zone
);

CREATE TABLE public.stock_data_daily (
    date date NOT NULL,
    symbol text NOT NULL,
    company_name text,
    index_membership text,
    sector text,
    industry text,
    market_cap numeric,
    market_cap_category text,
    current_price numeric,
    open numeric,
    high numeric,
    low numeric,
    close numeric,
    high_52w numeric,
    low_52w numeric,
    high_30d numeric,
    low_30d numeric,
    close_30d numeric,
    price_6m_ago numeric,
    price_12m_ago numeric,
    ret_1w numeric,
    ret_1m numeric,
    ret_3m numeric,
    ret_6m numeric,
    ret_12m numeric,
    sma_10 numeric,
    sma_20 numeric,
    sma_50 numeric,
    sma_200 numeric,
    ema_10 numeric,
    ema_20 numeric,
    ema_50 numeric,
    rsi_daily numeric,
    rsi_weekly numeric,
    rsi_monthly numeric,
    adx numeric,
    di_plus numeric,
    di_minus numeric,
    volume bigint,
    avg_vol_20d bigint,
    avg_vol_50d bigint,
    vwap numeric,
    vwap_20d numeric,
    vwap_50d numeric,
    pct_change numeric,
    atr_14 numeric,
    atr_pct numeric,
    ha_high numeric,
    ha_low numeric,
    ha_close numeric,
    supertrend numeric,
    macd_line numeric,
    macd_signal numeric,
    macd_hist numeric,
    psar numeric,
    bb_upper numeric,
    bb_lower numeric,
    stoch numeric,
    ttm_net_profit numeric,
    net_profit_yearly numeric,
    eps numeric,
    quarterly_net_profit numeric,
    quarterly_variance numeric,
    above_sma50 boolean,
    sma50_gt_200 boolean,
    above_st boolean,
    wk_hi_high boolean,
    wk_hi_low boolean,
    dist_sma50 numeric,
    vol_ratio numeric,
    value_cr numeric,
    delivery_pct numeric,
    consol_range numeric,
    breakout_setup boolean,
    bk_trigger boolean,
    upcoming_events text,
    upcoming_event_type text,
    in_master_shortlist boolean,
    rs_vs_nifty numeric,
    fii_sector_flow numeric,
    asm_flag boolean DEFAULT false,
    fo_ban_flag boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    kite_price numeric,
    predicted_regime text
);

CREATE TABLE public.strategy_config (
    strategy text NOT NULL,
    params jsonb NOT NULL,
    enabled boolean DEFAULT true,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE public.system_config (
    key text NOT NULL,
    value text,
    description text,
    updated_at timestamp with time zone DEFAULT now()
);

-- ============================================================
-- TradeOS v6 — compute_indicators v2.1 SQL Migration
-- schema_compute_indicators_v21.sql
--
-- Run in Supabase SQL Editor before activating compute_indicators.
-- All statements are idempotent (IF NOT EXISTS / ON CONFLICT DO NOTHING).
-- ============================================================


-- ── 1. compute_meta column on stock_data_daily ───────────────────────────────
-- Stores per-symbol reconciliation result as queryable JSONB.
-- No extra table needed — inline with the row.

ALTER TABLE stock_data_daily
  ADD COLUMN IF NOT EXISTS compute_meta JSONB;

CREATE INDEX IF NOT EXISTS idx_sdd_compute_meta
  ON stock_data_daily USING gin (compute_meta)
  WHERE compute_meta IS NOT NULL;


-- ── 2. system_config: global reconciliation switch ───────────────────────────

INSERT INTO system_config (key, value, description) VALUES
  ('compute_indicators_reconcile', 'true',
   'Hybrid mode: compare computed vs sheet per field. Set false when confident in all fields.')
ON CONFLICT (key) DO NOTHING;


-- ── 3. system_config: field-level trust seeds (all start at RECONCILE) ───────
-- Upgrade individual fields to COMPUTE_ALWAYS as you verify them.
-- Set SHEET_ALWAYS if a field uses a genuinely different formula in the Sheet.

INSERT INTO system_config (key, value, description) VALUES
  -- Level 1 (simplest — likely to match first)
  ('compute_trust_vol_ratio',       'RECONCILE', 'volume/avg_vol_20 — verify against sheet'),
  ('compute_trust_atr_pct',         'RECONCILE', 'atr_14/close*100 — verify against sheet'),
  ('compute_trust_current_price',   'RECONCILE', 'alias for close — should match exactly'),
  -- Level 2 — SMA / flags
  ('compute_trust_dist_sma50',      'RECONCILE', '(close-sma50)/sma50*100'),
  ('compute_trust_above_sma50',     'RECONCILE', 'close>sma50 boolean'),
  ('compute_trust_sma50_gt_200',    'RECONCILE', 'sma50>sma200 boolean'),
  ('compute_trust_above_st',        'RECONCILE', 'close>supertrend boolean'),
  ('compute_trust_bk_trigger',      'RECONCILE', 'close>52w_high*0.98'),
  ('compute_trust_price_location',  'RECONCILE', '% position in 52w range'),
  ('compute_trust_breakout_setup',  'RECONCILE', 'close>sma50 AND vol_ratio>1.5 AND consol<8'),
  ('compute_trust_wk_hi_high',      'RECONCILE', 'last5d high > prior5d high'),
  ('compute_trust_wk_hi_low',       'RECONCILE', 'last5d low > prior5d low'),
  -- Level 2 — historical (may diverge early due to session count differences)
  ('compute_trust_low_30d',         'RECONCILE', '30-session rolling low'),
  ('compute_trust_close_30d',       'RECONCILE', 'close 30 sessions ago'),
  ('compute_trust_consol_range',    'RECONCILE', '(high30d-low30d)/low30d*100 — wide tolerance'),
  ('compute_trust_ret_1w',          'RECONCILE', '5-session return — wide tolerance'),
  ('compute_trust_ret_1m',          'RECONCILE', '20-session return — wide tolerance'),
  ('compute_trust_ret_3m',          'RECONCILE', '60-session return — wide tolerance'),
  ('compute_trust_ret_6m',          'RECONCILE', '120-session return — needs 6mo of chartink data'),
  ('compute_trust_ret_12m',         'RECONCILE', '240-session return — needs 12mo of chartink data'),
  ('compute_trust_price_6m_ago',    'RECONCILE', 'close 120 sessions ago'),
  ('compute_trust_price_12m_ago',   'RECONCILE', 'close 240 sessions ago'),
  -- Level 3
  ('compute_trust_rs_vs_nifty',     'RECONCILE', 'ret_1m - nifty_ret_1m — wide tolerance'),
  -- Previously SHEET_ONLY — now computed from Supabase
  ('compute_trust_upcoming_events',     'RECONCILE', 'from nifty_upcoming_events.details'),
  ('compute_trust_upcoming_event_type', 'RECONCILE', 'from nifty_upcoming_events.purpose'),
  ('compute_trust_in_master_shortlist', 'RECONCILE', 'from master_shortlist symbol presence'),
  ('compute_trust_index_membership',    'RECONCILE', 'from nifty_total_market booleans')
ON CONFLICT (key) DO NOTHING;


-- ── 4. Useful diagnostic queries ─────────────────────────────────────────────

-- 4a. See all trust overrides (non-RECONCILE fields)
-- SELECT key, value FROM system_config
-- WHERE key LIKE 'compute_trust_%' AND value != 'RECONCILE'
-- ORDER BY value, key;

-- 4b. Symbols with most diverged fields today (investigate these manually)
-- SELECT symbol,
--        (compute_meta->'summary'->>'diverged')::int AS diverged_count,
--        compute_meta->'diverged' AS diverged_detail
-- FROM stock_data_daily
-- WHERE date = CURRENT_DATE
--   AND compute_meta IS NOT NULL
--   AND (compute_meta->'summary'->>'diverged')::int > 0
-- ORDER BY diverged_count DESC
-- LIMIT 20;

-- 4c. Which fields diverge most across all symbols today
-- SELECT field_key,
--        COUNT(*)                               AS symbols_affected,
--        AVG((value->>'delta_pct')::numeric)   AS avg_delta_pct,
--        MAX((value->>'delta_pct')::numeric)   AS max_delta_pct
-- FROM stock_data_daily,
--      jsonb_each(compute_meta->'diverged') AS t(field_key, value)
-- WHERE date = CURRENT_DATE
-- GROUP BY field_key
-- ORDER BY avg_delta_pct DESC;

-- 4d. Graduate a verified field to COMPUTE_ALWAYS
-- UPDATE system_config
-- SET value = 'COMPUTE_ALWAYS'
-- WHERE key = 'compute_trust_vol_ratio';

-- 4e. Mark a field as SHEET_ALWAYS (genuinely different formula/basis)
-- UPDATE system_config
-- SET value = 'SHEET_ALWAYS'
-- WHERE key = 'compute_trust_consol_range';

-- 4f. Turn off all reconciliation when fully confident
-- UPDATE system_config
-- SET value = 'false'
-- WHERE key = 'compute_indicators_reconcile';


-- ── 5. MATRIX reference ──────────────────────────────────────────────────────
-- compute_indicators now reads:
--   chartink_raw_data   (source data)
--   stock_data_daily    (sheet baseline for reconciliation)
--   nifty_upcoming_events (upcoming_events, upcoming_event_type)
--   master_shortlist    (in_master_shortlist flag)
--   nifty_total_market  (index_membership)
--   market_regime       (nifty_return for rs_vs_nifty)
--   system_config       (reconcile flag + field trust levels)
-- compute_indicators writes:
--   stock_data_daily    (computed + reconciled values + compute_meta)

NOTIFY pgrst, 'reload schema';
