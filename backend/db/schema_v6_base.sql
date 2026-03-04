-- ============================================================
-- TradeOS v6 — Base Schema
-- Run this FIRST in Supabase SQL Editor
-- ============================================================

-- ── Extensions ───────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── stock_data_daily ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_data_daily (
    date                    DATE        NOT NULL,
    symbol                  TEXT        NOT NULL,
    company_name            TEXT,
    index_membership        TEXT,
    sector                  TEXT,
    industry                TEXT,
    market_cap              NUMERIC,
    market_cap_category     TEXT,
    current_price           NUMERIC,
    open                    NUMERIC,
    high                    NUMERIC,
    low                     NUMERIC,
    close                   NUMERIC,
    high_52w                NUMERIC,
    low_52w                 NUMERIC,
    high_30d                NUMERIC,
    low_30d                 NUMERIC,
    close_30d               NUMERIC,
    price_6m_ago            NUMERIC,
    price_12m_ago           NUMERIC,
    ret_1w                  NUMERIC,
    ret_1m                  NUMERIC,
    ret_3m                  NUMERIC,
    ret_6m                  NUMERIC,
    ret_12m                 NUMERIC,
    sma_10                  NUMERIC,
    sma_20                  NUMERIC,
    sma_50                  NUMERIC,
    sma_200                 NUMERIC,
    ema_10                  NUMERIC,
    ema_20                  NUMERIC,
    ema_50                  NUMERIC,
    rsi_daily               NUMERIC,
    rsi_weekly              NUMERIC,
    rsi_monthly             NUMERIC,
    adx                     NUMERIC,
    di_plus                 NUMERIC,
    di_minus                NUMERIC,
    volume                  BIGINT,
    avg_vol_20d             BIGINT,
    avg_vol_50d             BIGINT,
    vwap                    NUMERIC,
    vwap_20d                NUMERIC,
    vwap_50d                NUMERIC,
    pct_change              NUMERIC,
    atr_14                  NUMERIC,
    atr_pct                 NUMERIC,
    ha_high                 NUMERIC,
    ha_low                  NUMERIC,
    ha_close                NUMERIC,
    supertrend              NUMERIC,
    macd_line               NUMERIC,
    macd_signal             NUMERIC,
    macd_hist               NUMERIC,
    psar                    NUMERIC,
    bb_upper                NUMERIC,
    bb_lower                NUMERIC,
    stoch                   NUMERIC,
    ttm_net_profit          NUMERIC,
    net_profit_yearly       NUMERIC,
    eps                     NUMERIC,
    quarterly_net_profit    NUMERIC,
    quarterly_variance      NUMERIC,
    above_sma50             BOOLEAN,
    sma50_gt_200            BOOLEAN,
    above_st                BOOLEAN,
    wk_hi_high              BOOLEAN,
    wk_hi_low               BOOLEAN,
    dist_sma50              NUMERIC,
    vol_ratio               NUMERIC,
    value_cr                NUMERIC,
    delivery_pct            NUMERIC,
    consol_range            NUMERIC,
    breakout_setup          BOOLEAN,
    bk_trigger              BOOLEAN,
    upcoming_events         TEXT,
    upcoming_event_type     TEXT,
    in_master_shortlist     BOOLEAN,
    -- Phase 1 additions
    rs_vs_nifty             NUMERIC,    -- relative strength vs Nifty
    fii_sector_flow         NUMERIC,    -- FII flow in this sector
    asm_flag                BOOLEAN DEFAULT FALSE,
    fo_ban_flag             BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, symbol)
);

-- ── master_shortlist ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS master_shortlist (
    date                    DATE        NOT NULL,
    symbol                  TEXT        NOT NULL,
    company_name            TEXT,
    sector                  TEXT,
    strategy_source         TEXT,
    current_price           NUMERIC,
    final_score             NUMERIC,
    days_in_list            INTEGER,
    rank_vel_3d             NUMERIC,
    score_vel_5d            NUMERIC,
    momentum_state          TEXT,
    momentum_phase          TEXT,
    velocity_state          TEXT,
    trend_maturity          TEXT,
    fv_low                  NUMERIC,
    fv_high                 NUMERIC,
    price_location          NUMERIC,
    dist_fv_pct             NUMERIC,
    struct_edge             TEXT,
    entry_timing_type       TEXT,
    entry_ready             BOOLEAN,
    in_position             BOOLEAN,
    reentry_mode            TEXT,
    lifecycle               TEXT,
    expected_r              NUMERIC,
    validity_score          NUMERIC,
    exec_eligibility        TEXT,
    entry_zone_low          NUMERIC,
    entry_zone_high         NUMERIC,
    entry_mode              TEXT,
    dist_entry_pct          NUMERIC,
    entry_action            TEXT,
    opp_type                TEXT,
    base_rank               INTEGER,
    base_score              NUMERIC,
    event_bias              TEXT,
    event_sectors           TEXT,
    upcoming_news           TEXT,
    is_ipo                  BOOLEAN,
    trade_allowed           BOOLEAN,
    suggested               TEXT,
    pos_type                TEXT,
    notes                   TEXT,
    -- computed fields
    position_state          TEXT,       -- WATCHING / BUY_CANDIDATE / OPEN_POSITION
    -- Phase 1 additions
    ai_conviction           TEXT,
    ai_conviction_reason    TEXT,
    ai_risks                JSONB,
    ai_suggested_action     TEXT,
    ai_note                 TEXT,
    ai_provider             TEXT,
    ai_fallback_used        BOOLEAN,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, symbol)
);

-- ── open_positions ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS open_positions (
    symbol                  TEXT        NOT NULL,
    company_name            TEXT,
    sector                  TEXT,
    strategy                TEXT,
    entry_date              DATE,
    entry_price             NUMERIC,
    proposed_qty            NUMERIC,
    actual_qty              NUMERIC,
    invested_value          NUMERIC,
    current_price           NUMERIC,
    current_value           NUMERIC,
    unrealized_pnl          NUMERIC,
    pnl_pct                 NUMERIC,
    locked_profit           NUMERIC,
    lifecycle               TEXT,
    sl_type                 TEXT,
    initial_sl_atr          NUMERIC,
    high_water_mark         NUMERIC,
    active_sl               NUMERIC,
    exit_signal             TEXT,
    action_required         TEXT,
    event_risk              TEXT,
    upcoming_news           TEXT,
    target_1                NUMERIC,
    target_2                NUMERIC,
    target_3                NUMERIC,
    status                  TEXT DEFAULT 'ACTIVE',
    synced_at               TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (symbol)
);

-- ── closed_positions ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS closed_positions (
    id                      BIGSERIAL   PRIMARY KEY,
    symbol                  TEXT,
    company_name            TEXT,
    sector                  TEXT,
    strategy                TEXT,
    entry_date              DATE,
    entry_price             NUMERIC,
    proposed_qty            NUMERIC,
    actual_qty              NUMERIC,
    invested_value          NUMERIC,
    exit_date               DATE,
    exit_price              NUMERIC,
    exit_value              NUMERIC,
    realized_pnl            NUMERIC,
    pnl_pct                 NUMERIC,
    high_water_mark         NUMERIC,
    max_favorable_excursion NUMERIC,
    lifecycle_at_entry      TEXT,
    entry_timing_type       TEXT,
    reentry_mode            TEXT,
    expected_r_at_entry     NUMERIC,
    exit_reason             TEXT,
    UNIQUE (symbol, entry_date, exit_date)
);

-- ── sector_strength ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sector_strength (
    date                    DATE        NOT NULL,
    sector                  TEXT        NOT NULL,
    stock_count             INTEGER,
    avg_rsi_daily           NUMERIC,
    avg_rsi_weekly          NUMERIC,
    avg_rsi_monthly         NUMERIC,
    avg_ret_6m              NUMERIC,
    breadth_sma50           NUMERIC,
    rank                    INTEGER,
    top4_flag               BOOLEAN,
    sector_state            TEXT,
    -- Phase 1 additions
    fii_flow_sector         NUMERIC,
    PRIMARY KEY (date, sector)
);

create table industry_strength (
  id              bigserial primary key,
  date            date not null,
  industry        text not null,
  stock_count     int,
  avg_rsi_daily   numeric,
  avg_rsi_weekly  numeric,
  avg_rsi_monthly numeric,
  avg_ret_6m      numeric,
  breadth_sma50   numeric,
  rank            int,
  top5_flag       boolean,
  industry_state  text,
  unique (date, industry)
);CREATE TABLE IF NOT EXISTS industry_strength (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE NOT NULL,
    industry        TEXT NOT NULL,
    stock_count     INTEGER,
    avg_rsi_daily   NUMERIC,
    avg_rsi_weekly  NUMERIC,
    avg_rsi_monthly NUMERIC,
    avg_ret_6m      NUMERIC,
    breadth_sma50   NUMERIC,
    rank            INTEGER,
    top5_flag       BOOLEAN,
    industry_state  TEXT,
    synced_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (date, industry)
);

CREATE INDEX IF NOT EXISTS idx_industry_strength_date ON industry_strength(date DESC);
CREATE INDEX IF NOT EXISTS idx_industry_strength_rank ON industry_strength(rank);

-- ── market_regime ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_regime (
    date                    DATE        PRIMARY KEY,
    regime                  TEXT,
    nifty_price             NUMERIC,
    nifty_50dma             NUMERIC,
    nifty_200dma            NUMERIC,
    nifty_weekly_rsi        NUMERIC,
    india_vix               NUMERIC,
    gift_nifty              NUMERIC,
    avg_sector_breadth      NUMERIC,
    ctl_enabled             BOOLEAN,
    sbs_enabled             BOOLEAN,
    tpo_enabled             BOOLEAN,
    eap_enabled             BOOLEAN,
    max_positions           INTEGER,
    raw_data                JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── event_calendar ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS event_calendar (
    id                      BIGSERIAL   PRIMARY KEY,
    event_category          TEXT,
    event_name              TEXT,
    event_type              TEXT,
    start_date              DATE,
    end_date                DATE,
    affected_sectors        TEXT,
    event_bias              TEXT,
    event_intensity         TEXT,
    strategy_impact         TEXT,
    notes                   TEXT,
    event_status            TEXT,
    is_active               BOOLEAN,
    priority                INTEGER,
    is_risk_on_accelerator  TEXT,
    UNIQUE (event_name, start_date)
);

-- ── lessons ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS lessons (
    id                      BIGSERIAL   PRIMARY KEY,
    date                    DATE,
    scenario_type           TEXT,
    trigger_event           TEXT,
    linked_event_type       TEXT,
    impacted_sector         TEXT,
    scenario_context        TEXT,
    what_expected           TEXT,
    what_happened           TEXT,
    what_failed             TEXT,
    root_cause              TEXT,
    corrective_rule         TEXT,
    source                  TEXT DEFAULT 'MANUAL',  -- MANUAL or AI
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ── msl_history ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS msl_history (
    snapshot_date           DATE        NOT NULL,
    symbol                  TEXT        NOT NULL,
    company_name            TEXT,
    priority_rank           INTEGER,
    sector                  TEXT,
    strategy_source         TEXT,
    close_price             NUMERIC,
    price_location          NUMERIC,
    dist_fv_pct             NUMERIC,
    entry_timing_type       TEXT,
    momentum_phase          TEXT,
    velocity_state          TEXT,
    trend_maturity          TEXT,
    struct_edge             TEXT,
    in_position             BOOLEAN,
    reentry_mode            TEXT,
    lifecycle               TEXT,
    expected_r              NUMERIC,
    validity_score          NUMERIC,
    final_score             NUMERIC,
    PRIMARY KEY (snapshot_date, symbol)
);

-- ── nse_holidays ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nse_holidays (
    date                    DATE        PRIMARY KEY,
    occasion                TEXT
);

-- ── nifty_upcoming_events ────────────────────────────────────
CREATE TABLE IF NOT EXISTS nifty_upcoming_events (
    id                      BIGSERIAL   PRIMARY KEY,
    symbol                  TEXT,
    company_name            TEXT,
    purpose                 TEXT,
    details                 TEXT,
    event_date              DATE,
    days_to_event           INTEGER,
    source                  TEXT DEFAULT 'SHEET',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (symbol, purpose, event_date)
);

-- ── nifty_bhavcopy ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_prices (
    date                    DATE        NOT NULL,
    symbol                  TEXT        NOT NULL,
    open                    NUMERIC,
    high                    NUMERIC,
    low                     NUMERIC,
    close                   NUMERIC,
    prev_close              NUMERIC,
    volume                  BIGINT,
    value_cr                NUMERIC,
    delivery_pct            NUMERIC,
    delivery_qty            BIGINT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (date, symbol)
);

-- ── system_config ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_config (
    key                     TEXT        PRIMARY KEY,
    value                   TEXT,
    description             TEXT,
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Default system config values
INSERT INTO system_config (key, value, description) VALUES
    ('max_positions_risk_on',      '8',    'Max open positions in RISK ON regime'),
    ('max_positions_neutral',      '7',    'Max open positions in NEUTRAL regime'),
    ('max_positions_risk_off',     '6',    'Max open positions in RISK OFF regime'),
    ('max_sector_concentration',   '25',   'Max % of portfolio in single sector'),
    ('block_buys_risk_off',        'false','Block BUY signals when regime is RISK OFF'),
    ('ai_provider',                'disabled', 'Active AI provider: claude/openai/gemini/deepseek/grok/copilot/ml/disabled'),
    ('ai_fallback_ml',             'true', 'Use ML model as fallback when AI unavailable'),
    ('ai_fallback_scraping',       'true', 'Use web scraping as fallback when AI unavailable'),
    ('ai_daily_budget_inr',        '200',  'Max INR to spend on AI per day (0=unlimited)'),
    ('ai_max_stocks_per_day',      '20',   'Max stocks to send to AI per day'),
    ('min_score_to_show',          '50',   'Minimum strategy score to show in signals'),
    ('show_watching_stocks',       'true', 'Show WATCHING state stocks in frontend'),
    ('telegram_alerts_enabled',    'false','Send Telegram alerts'),
    ('pipeline_run_time',          '18:00','Pipeline run time (IST)'),
    ('google_sheet_id',            '1yclJSWpRtnenZcd3M1lKbYOnh9CGEnRbMCwlTwv-1Dw', 'Google Sheet ID'),
    ('buy_candidate_threshold_pct','0.5',  'Entry vs current price tolerance for BUY_CANDIDATE state'),
    ('autonomy_phase',             '0',    'Current autonomy phase: 0=manual, 1=intelligence, 2=kite, 3=supervised, 4=autonomous'),
    ('master_kill_switch',         'false','Emergency: halt all pipeline activity'),
    ('fii_caution_threshold',      '-2000','FII net sell below this (Cr) for 5 days triggers CAUTION'),
    ('fii_accelerator_threshold',  '1000', 'FII net buy above this (Cr) for 3 days triggers ACCELERATOR')
ON CONFLICT (key) DO NOTHING;

-- ── nifty_total_market ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS nifty_total_market (
    symbol                  TEXT        PRIMARY KEY,
    company_name            TEXT,
    industry                TEXT,
    isin                    TEXT,
    nifty_200               BOOLEAN,
    nifty_500               BOOLEAN
);

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_stock_data_date   ON stock_data_daily(date);
CREATE INDEX IF NOT EXISTS idx_stock_data_symbol ON stock_data_daily(symbol);
CREATE INDEX IF NOT EXISTS idx_msl_date          ON master_shortlist(date);
CREATE INDEX IF NOT EXISTS idx_msl_score         ON master_shortlist(date, final_score DESC);
CREATE INDEX IF NOT EXISTS idx_msl_state         ON master_shortlist(date, position_state);
CREATE INDEX IF NOT EXISTS idx_msl_hist          ON msl_history(snapshot_date, symbol);
CREATE INDEX IF NOT EXISTS idx_closed_symbol     ON closed_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_events_active     ON event_calendar(is_active, start_date);
CREATE INDEX IF NOT EXISTS idx_events_sector     ON event_calendar(affected_sectors);
CREATE INDEX IF NOT EXISTS idx_upcoming_date     ON nifty_upcoming_events(event_date);
CREATE INDEX IF NOT EXISTS idx_upcoming_symbol   ON nifty_upcoming_events(symbol);

COMMENT ON TABLE stock_data_daily IS '78-column stock universe ingested from STOCK_DATA Sheet tab';
COMMENT ON TABLE master_shortlist IS '43-column ranked shortlist from MASTER_SHORTLIST Sheet tab';
COMMENT ON TABLE open_positions   IS 'Live positions from OPEN_POSITIONS Sheet tab (authoritative)';
COMMENT ON TABLE system_config    IS 'All system parameters configurable from frontend Settings tab';
