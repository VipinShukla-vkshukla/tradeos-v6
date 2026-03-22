-- ============================================================
-- TradeOS v6 — Consolidated SQL Migrations
-- schema_v4_8_all_changes.sql
-- Run this file in Supabase SQL Editor in one pass.
-- All statements use IF NOT EXISTS / ON CONFLICT — safe to re-run.
-- ============================================================
-- Sections:
--   A. Phase 0/1 — Apply Now
--   B. Phase 2 — Apply at Phase 2 Activation
--   C. Phase 3 — Apply at Phase 3 Activation
-- ============================================================


-- ============================================================
-- A. PHASE 0/1 — APPLY NOW
-- These are safe to run immediately. NULL columns until the
-- corresponding scripts are deployed; nothing breaks today.
-- ============================================================

-- ── A1. signal_log — CC4 Phase 2 context columns ─────────────────────────
-- Written by generate_signals.py (CC4). NULL in Phase 0/1; populated Phase 2.
-- SQL source: Phase 0 Course Correction 1 + AG2 + AG4

ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS signal_subtype       TEXT,
  ADD COLUMN IF NOT EXISTS score_adjusted       NUMERIC,
  ADD COLUMN IF NOT EXISTS sheet_conflict       BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS sheet_conflict_type  TEXT,
  ADD COLUMN IF NOT EXISTS rsi_monthly          FLOAT,
  ADD COLUMN IF NOT EXISTS rs_vs_nifty          FLOAT,
  ADD COLUMN IF NOT EXISTS consol_range         FLOAT,
  ADD COLUMN IF NOT EXISTS ret_1m               FLOAT,
  ADD COLUMN IF NOT EXISTS ret_3m               FLOAT,
  ADD COLUMN IF NOT EXISTS above_sma50          BOOLEAN,
  ADD COLUMN IF NOT EXISTS breakout_setup       BOOLEAN,
  ADD COLUMN IF NOT EXISTS validity_score       FLOAT,
  ADD COLUMN IF NOT EXISTS expected_r_msl       FLOAT,
  ADD COLUMN IF NOT EXISTS trend_maturity       TEXT,
  ADD COLUMN IF NOT EXISTS velocity_state       TEXT,
  ADD COLUMN IF NOT EXISTS momentum_phase       TEXT,
  ADD COLUMN IF NOT EXISTS days_to_trigger_est  INT,
  -- AG2: point-in-time sector rank at signal fire (eliminates ML temporal leakage)
  ADD COLUMN IF NOT EXISTS sector_rank_at_entry INT,
  -- AG4: cross-reference with independent_scanner.py pattern results
  ADD COLUMN IF NOT EXISTS scanner_patterns     TEXT;

-- ── A2. signal_log — ai_strategy_validation (G4 patch) ───────────────────
-- Written by ai_enrich.py. Stores AI's validation of whether signal agrees
-- with rule engine findings.
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS ai_strategy_validation TEXT;

-- ── A3. global_cues — SG1+SG2 new commodity fields ───────────────────────
-- Written by ingest_global_cues.py (SG1+SG2).
-- SG1: US 10-year Treasury yield — primary FII EM outflow predictor.
-- SG2: Silver price — Jewellery + Metals Mining sector impact.
ALTER TABLE global_cues
  ADD COLUMN IF NOT EXISTS us_10yr_yield    NUMERIC,
  ADD COLUMN IF NOT EXISTS us_10yr_chg_bps  NUMERIC,
  ADD COLUMN IF NOT EXISTS silver_price     NUMERIC,
  ADD COLUMN IF NOT EXISTS silver_chg_pct   NUMERIC;

-- ── A4. system_config — Phase 2 signal threshold seeds ───────────────────
-- All signal thresholds live in system_config and are evolved by evolution_tracker.
-- Seeding now means no hardcoded values ever exist in code.
-- ON CONFLICT DO NOTHING = safe to re-run; never overwrites evolved values.
INSERT INTO system_config (key, value) VALUES
  -- PRIME_SETUP thresholds
  ('signal_threshold_prime_rsi_daily_min',   '48'),
  ('signal_threshold_prime_rsi_daily_max',   '82'),
  ('signal_threshold_prime_rsi_weekly_min',  '52'),
  ('signal_threshold_prime_rsi_weekly_max',  '78'),
  ('signal_threshold_prime_rsi_monthly_min', '48'),
  ('signal_threshold_prime_rsi_monthly_max', '80'),
  ('signal_threshold_prime_adx_min',         '18'),
  ('signal_threshold_prime_vol_min',         '1.15'),
  ('signal_threshold_prime_del_min',         '50'),
  ('signal_threshold_prime_consol_max',      '8'),
  ('signal_threshold_prime_sma50_max',       '12'),
  -- STAGED_ENTRY thresholds
  ('signal_threshold_staged_rsi_daily_min',   '44'),
  ('signal_threshold_staged_rsi_daily_max',   '82'),
  ('signal_threshold_staged_rsi_weekly_min',  '48'),
  ('signal_threshold_staged_rsi_weekly_max',  '80'),
  ('signal_threshold_staged_rsi_monthly_min', '44'),
  ('signal_threshold_staged_rsi_monthly_max', '82'),
  ('signal_threshold_staged_adx_min',         '15'),
  ('signal_threshold_staged_vol_min',         '0.80'),
  ('signal_threshold_staged_del_min',         '46'),
  -- PRE_BREAKOUT_WATCH thresholds
  ('signal_threshold_prebreak_rsi_daily_min',   '42'),
  ('signal_threshold_prebreak_rsi_daily_max',   '65'),
  ('signal_threshold_prebreak_rsi_weekly_min',  '44'),
  ('signal_threshold_prebreak_rsi_weekly_max',  '70'),
  ('signal_threshold_prebreak_rsi_monthly_min', '40'),
  ('signal_threshold_prebreak_rsi_monthly_max', '72'),
  ('signal_threshold_prebreak_adx_min',         '10'),
  ('signal_threshold_prebreak_adx_max',         '26'),
  ('signal_threshold_prebreak_consol_max',      '9'),
  ('signal_threshold_prebreak_vol_max',         '1.4'),
  -- REENTRY_SETUP thresholds
  ('signal_threshold_reentry_rsi_daily_min',   '38'),
  ('signal_threshold_reentry_rsi_daily_max',   '68'),
  ('signal_threshold_reentry_rsi_weekly_min',  '42'),
  ('signal_threshold_reentry_rsi_weekly_max',  '65'),
  ('signal_threshold_reentry_rsi_monthly_min', '46'),
  ('signal_threshold_reentry_rsi_monthly_max', '78'),
  ('signal_threshold_reentry_adx_min',         '14'),
  ('signal_threshold_reentry_vol_min',         '0.55'),
  ('signal_threshold_reentry_del_min',         '44'),
  ('signal_threshold_reentry_sma50_min',       '-8'),
  ('signal_threshold_reentry_ret6m_min',       '3')
ON CONFLICT (key) DO NOTHING;

-- ── A5. Verify rsi_weekly + rsi_monthly coverage (run and check output) ───
-- Target: both > 95%. If below 90%, check chartink_raw_data column mapping.
-- This is a READ-ONLY diagnostic query — no schema change, just validation.
SELECT
  COUNT(*)                                                   AS total_rows,
  COUNT(rsi_weekly)                                          AS has_rsi_weekly,
  COUNT(rsi_monthly)                                         AS has_rsi_monthly,
  ROUND(COUNT(rsi_weekly)::numeric  / COUNT(*) * 100, 1)    AS weekly_pct,
  ROUND(COUNT(rsi_monthly)::numeric / COUNT(*) * 100, 1)    AS monthly_pct
FROM stock_data_daily
WHERE date >= CURRENT_DATE - INTERVAL '7 days';

NOTIFY pgrst, 'reload schema';


-- ============================================================
-- B. PHASE 2 — APPLY AT PHASE 2 ACTIVATION
-- Run these when setting autonomy_phase = 2.
-- ============================================================

-- ── B1. safety_lists — ASM/GSM/FO_BAN per-symbol table ───────────────────
CREATE TABLE IF NOT EXISTS public.safety_lists (
  id          BIGSERIAL PRIMARY KEY,
  symbol      TEXT NOT NULL,
  list_type   TEXT NOT NULL,          -- 'ASM' | 'GSM' | 'FO_BAN'
  stage       TEXT,
  added_date  DATE DEFAULT CURRENT_DATE,
  UNIQUE (symbol, list_type)
);
CREATE INDEX IF NOT EXISTS idx_safety_lists_symbol ON safety_lists (symbol);

-- ── B2. data_anomalies — pipeline quality monitoring ─────────────────────
-- Written by: data_quality_monitor, sl_monitor, kite_reconcile,
--             ml_regime_predict, position_event_monitor, position_target_monitor.
-- Read by:    send_alerts.py morning brief (AG3 — Section 0 data alert).
CREATE TABLE IF NOT EXISTS public.data_anomalies (
  id         BIGSERIAL PRIMARY KEY,
  date       DATE,
  check_name TEXT,
  severity   TEXT,                    -- 'OK' | 'WARN' | 'ERROR' | 'INFO'
  value      TEXT,
  message    TEXT,
  affected   TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_anomalies_date     ON data_anomalies (date DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_severity ON data_anomalies (severity);

-- ── B3. market_news — scraped regulatory/policy/corporate news ────────────
-- Written by: ingest_market_news.py (step 00a).
-- Read by:    market_intelligence_engine.py.
CREATE TABLE IF NOT EXISTS public.market_news (
  id              BIGSERIAL PRIMARY KEY,
  news_date       DATE NOT NULL DEFAULT CURRENT_DATE,
  headline        TEXT NOT NULL,
  source          TEXT,
  category        TEXT,               -- DOMESTIC_REGULATORY | DOMESTIC_POLICY | CORPORATE | CENTRAL_BANK | INTERNATIONAL
  impact_type     TEXT,               -- ASM_CHANGE | RATE_DECISION | POLICY | BULK_DEAL | EARNINGS | MACRO | TRADING_HALT | DELISTING
  parsed_sectors  TEXT[],
  parsed_symbols  TEXT[],
  magnitude       TEXT,               -- 'HIGH' | 'MEDIUM' | 'LOW'
  raw_url         TEXT,
  ingested_at     TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (news_date, source, headline)
);
CREATE INDEX IF NOT EXISTS idx_market_news_date     ON market_news (news_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_news_category ON market_news (category);
CREATE INDEX IF NOT EXISTS idx_market_news_impact   ON market_news (impact_type);

-- ── B4. macro_indicators — CPI/WPI/GDP/IIP/repo rate (SG5) ──────────────
-- Written by: ingest_macro_indicators.py (step 00b).
-- Read by:    market_intelligence_engine.py Pass 1 context.
CREATE TABLE IF NOT EXISTS public.macro_indicators (
  id               BIGSERIAL PRIMARY KEY,
  indicator_date   DATE NOT NULL,
  indicator_name   TEXT NOT NULL,     -- 'CPI_YOY' | 'WPI_YOY' | 'GDP_QOQ' | 'GDP_YOY' | 'IIP_YOY' | 'REPO_RATE' | 'REV_REPO' | 'CRR' | 'US_10YR_YIELD' | 'SILVER_PRICE'
  indicator_value  NUMERIC,
  previous_value   NUMERIC,
  change_bps       NUMERIC,
  source           TEXT,              -- 'RBI_DBIE' | 'MOSPI' | 'ET_RSS' | 'YAHOO_FINANCE'
  release_date     DATE,
  ingested_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (indicator_date, indicator_name)
);
CREATE INDEX IF NOT EXISTS idx_macro_ind_date ON macro_indicators (indicator_date DESC);
CREATE INDEX IF NOT EXISTS idx_macro_ind_name ON macro_indicators (indicator_name);

-- ── B5. open_positions — SG6+SG7 target and trailing SL columns ──────────
-- Written by: position_target_monitor.py and sl_monitor.py.
ALTER TABLE open_positions
  ADD COLUMN IF NOT EXISTS target_price     NUMERIC,
  ADD COLUMN IF NOT EXISTS target_pct       NUMERIC,
  ADD COLUMN IF NOT EXISTS target_hit       BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS target_hit_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trailing_sl_pct  NUMERIC,
  ADD COLUMN IF NOT EXISTS high_water_mark  NUMERIC;

-- ── B6. market_regime — Phase 2 ML prediction columns ────────────────────
ALTER TABLE market_regime
  ADD COLUMN IF NOT EXISTS predicted_regime    TEXT,
  ADD COLUMN IF NOT EXISTS regime_confidence   NUMERIC,
  ADD COLUMN IF NOT EXISTS regime_predicted_at TIMESTAMPTZ;

-- ── B7. stock_data_daily — Phase 2 ASM/FO_BAN flag columns ───────────────
-- Mirrored from safety_lists by ingest_asm_gsm.py for fast signal-time lookup.
ALTER TABLE stock_data_daily
  ADD COLUMN IF NOT EXISTS asm_flag        BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS fo_ban_flag     BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS kite_price      NUMERIC,
  ADD COLUMN IF NOT EXISTS predicted_regime TEXT;

NOTIFY pgrst, 'reload schema';


-- ============================================================
-- C. PHASE 3 — APPLY AT PHASE 3 ACTIVATION
-- ============================================================

-- ── C1. signal_log — execution tracking columns ───────────────────────────
ALTER TABLE signal_log
  ADD COLUMN IF NOT EXISTS execution_status TEXT,   -- PENDING | APPROVED | REJECTED | DEFERRED | EXECUTED
  ADD COLUMN IF NOT EXISTS kite_order_id    TEXT,
  ADD COLUMN IF NOT EXISTS execution_price  NUMERIC,
  ADD COLUMN IF NOT EXISTS executed_at      TIMESTAMPTZ;

-- ── C2. open_positions — signal linkage (AG5) + partial booking (SG11) ───
ALTER TABLE open_positions
  ADD COLUMN IF NOT EXISTS signal_id        BIGINT,  -- FK to signal_log.id
  ADD COLUMN IF NOT EXISTS signal_date      DATE,
  ADD COLUMN IF NOT EXISTS signal_subtype   TEXT,    -- PRIME_SETUP | STAGED_ENTRY | REENTRY_SETUP
  ADD COLUMN IF NOT EXISTS original_qty     INT,
  ADD COLUMN IF NOT EXISTS current_qty      INT,
  ADD COLUMN IF NOT EXISTS partial_bookings JSONB;   -- [{date, qty_sold, price, pnl_pct}]

-- ── C3. order_history — broker order audit trail (AG5) ───────────────────
-- Records every Kite API order response.
-- Enables slippage analysis, partial fill tracking, and execution debugging.
CREATE TABLE IF NOT EXISTS public.order_history (
  id               BIGSERIAL PRIMARY KEY,
  order_date       DATE NOT NULL DEFAULT CURRENT_DATE,
  symbol           TEXT NOT NULL,
  signal_id        BIGINT,
  broker_order_id  TEXT,
  order_type       TEXT,              -- 'BUY' | 'SELL'
  qty_requested    INT,
  qty_executed     INT,
  price_requested  NUMERIC,
  price_executed   NUMERIC,
  slippage_pct     NUMERIC,
  status           TEXT,              -- 'COMPLETE' | 'REJECTED' | 'PARTIAL' | 'CANCELLED'
  rejection_reason TEXT,
  kite_response    JSONB,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_order_history_date   ON order_history (order_date DESC);
CREATE INDEX IF NOT EXISTS idx_order_history_symbol ON order_history (symbol);

NOTIFY pgrst, 'reload schema';


-- ============================================================
-- VERIFICATION QUERIES
-- Run these after applying each section to confirm success.
-- ============================================================

-- Verify A1: signal_log new columns
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'signal_log'
  AND column_name IN (
    'signal_subtype','score_adjusted','sheet_conflict','rsi_monthly',
    'rs_vs_nifty','sector_rank_at_entry','scanner_patterns','validity_score',
    'trend_maturity','velocity_state','momentum_phase','days_to_trigger_est'
  )
ORDER BY column_name;

-- Verify A3: global_cues new columns
SELECT column_name FROM information_schema.columns
WHERE table_name = 'global_cues'
  AND column_name IN ('us_10yr_yield','us_10yr_chg_bps','silver_price','silver_chg_pct');

-- Verify A4: system_config threshold seeds (should return 40 rows)
SELECT COUNT(*) AS threshold_keys_seeded
FROM system_config
WHERE key LIKE 'signal_threshold_%';

-- Verify B5: open_positions new columns
SELECT column_name FROM information_schema.columns
WHERE table_name = 'open_positions'
  AND column_name IN ('target_price','target_hit','trailing_sl_pct','high_water_mark');

-- Verify B4: macro_indicators table exists
SELECT COUNT(*) AS macro_table_exists FROM macro_indicators LIMIT 1;

-- Verify C3: order_history table exists
SELECT COUNT(*) AS order_history_exists FROM order_history LIMIT 1;
