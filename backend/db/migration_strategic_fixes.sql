-- ============================================================
-- TradeOS v6 — Strategic Fixes Migration
-- Run once in Supabase SQL Editor
-- Safe to re-run: all statements use IF NOT EXISTS / IF EXISTS
-- ============================================================

-- ── 1. fii_dii_flow — align schema to what ingest_fii_dii.py writes ─────────
-- Schema had fii_net, dii_net etc. Script was writing fii_net_cr, dii_net_cr.
-- Fix: script now uses schema column names. Add any missing columns.
ALTER TABLE fii_dii_flow
    ADD COLUMN IF NOT EXISTS fii_gross_buy_cr   NUMERIC,   -- alias view col for legacy queries
    ADD COLUMN IF NOT EXISTS fii_gross_sell_cr  NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_net_cr         NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_gross_buy_cr   NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_gross_sell_cr  NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_net_cr         NUMERIC;
-- Note: patch_ingest_fii_dii.py writes to the canonical columns (fii_net etc.)
-- The _cr aliases above are kept so any ad-hoc queries still work.
-- Backfill aliases from canonical columns if rows already exist:
UPDATE fii_dii_flow SET
    fii_gross_buy_cr  = fii_gross_buy,
    fii_gross_sell_cr = fii_gross_sell,
    fii_net_cr        = fii_net,
    dii_gross_buy_cr  = dii_gross_buy,
    dii_gross_sell_cr = dii_gross_sell,
    dii_net_cr        = dii_net
WHERE fii_net_cr IS NULL AND fii_net IS NOT NULL;


-- ── 2. lessons — add retirement and quality tracking columns ─────────────────
ALTER TABLE lessons
    ADD COLUMN IF NOT EXISTS is_active       BOOLEAN  DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS times_applied   INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS times_worked    INTEGER  DEFAULT 0,
    ADD COLUMN IF NOT EXISTS confidence      NUMERIC  DEFAULT 1.0;

-- Index for fast active-lesson queries (ai_enrich reads these frequently)
CREATE INDEX IF NOT EXISTS idx_lessons_active
    ON lessons (is_active, impacted_sector, date DESC);


-- ── 3. evolution_proposals — add missing columns ──────────────────────────────
-- Schema was missing week_of (evolution_tracker.py writes this) and impact cols
ALTER TABLE evolution_proposals
    ADD COLUMN IF NOT EXISTS week_of              TEXT,
    ADD COLUMN IF NOT EXISTS confidence           NUMERIC,
    ADD COLUMN IF NOT EXISTS impact_measured_at   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS performance_delta    NUMERIC,
    ADD COLUMN IF NOT EXISTS notes                TEXT;
-- Backfill week_of from proposed_date for existing rows
UPDATE evolution_proposals
    SET week_of = proposed_date::TEXT
WHERE week_of IS NULL AND proposed_date IS NOT NULL;


-- ── 4. market_regime — add ML prediction columns ─────────────────────────────
-- ml_regime_classifier.py writes these; schema may not have them yet
ALTER TABLE market_regime
    ADD COLUMN IF NOT EXISTS predicted_regime    TEXT,
    ADD COLUMN IF NOT EXISTS regime_confidence   NUMERIC,
    ADD COLUMN IF NOT EXISTS regime_predicted_at TIMESTAMPTZ;


-- ── 5. shadow_trades — new table for Phase 3 shadow mode ─────────────────────
CREATE TABLE IF NOT EXISTS shadow_trades (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT,
    symbol          TEXT NOT NULL,
    strategy        TEXT,
    action          TEXT,           -- APPROVED | REJECTED | DEFERRED
    entry_price     NUMERIC,
    qty             INTEGER,
    approved_at     TIMESTAMPTZ DEFAULT NOW(),
    would_execute   BOOLEAN DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_created ON shadow_trades(created_at DESC);


-- ── 6. open_positions — add columns used by sl_monitor + kite_reconcile ──────
ALTER TABLE open_positions
    ADD COLUMN IF NOT EXISTS kite_qty            INTEGER,     -- actual qty from Kite holdings
    ADD COLUMN IF NOT EXISTS reconcile_status    TEXT,        -- MATCHED | QTY_MISMATCH | KITE_ONLY | DB_ONLY
    ADD COLUMN IF NOT EXISTS last_reconciled_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS sl_breach_alerted   BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS sl_proximity_alerted BOOLEAN DEFAULT FALSE;


-- ── 7. data_anomalies — ensure check_name index for fast lookups ──────────────
CREATE INDEX IF NOT EXISTS idx_anomalies_check
    ON data_anomalies (check_name, date DESC);

-- ── Verify ──────────────────────────────────────────────────────────────────
SELECT
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='lessons' AND column_name='is_active')    AS lessons_is_active,
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='evolution_proposals' AND column_name='week_of') AS evol_week_of,
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name='market_regime' AND column_name='predicted_regime') AS regime_predicted,
    (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_name='shadow_trades')                           AS shadow_trades_exists;

-- ═══════════════════════════════════════════════════════════════
-- FIX 1: fii_dii_flow backfill
-- ═══════════════════════════════════════════════════════════════
--
-- ROOT CAUSE of your error:
--   The UPDATE referenced fii_net in the WHERE clause, but fii_net
--   does not exist yet — the old script wrote fii_net_cr (the bug).
--   The new canonical columns (fii_net etc.) must be ADDED first,
--   then backfilled FROM the _cr columns (not the other way around).
--
-- STEP 1: Add canonical columns (new names the fixed script will write to)
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE fii_dii_flow
    ADD COLUMN IF NOT EXISTS fii_gross_buy  NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_gross_sell NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_net        NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_gross_buy  NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_gross_sell NUMERIC,
    ADD COLUMN IF NOT EXISTS dii_net        NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_net_5d     NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_net_10d    NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_net_20d    NUMERIC,
    ADD COLUMN IF NOT EXISTS fii_flag       TEXT;

-- STEP 2: Backfill canonical columns FROM existing _cr data
-- (copies whatever was already written by the old broken script)
-- ─────────────────────────────────────────────────────────────────────────
UPDATE fii_dii_flow SET
    fii_gross_buy  = fii_gross_buy_cr,
    fii_gross_sell = fii_gross_sell_cr,
    fii_net        = fii_net_cr,
    dii_gross_buy  = dii_gross_buy_cr,
    dii_gross_sell = dii_gross_sell_cr,
    dii_net        = dii_net_cr
WHERE fii_net IS NULL
  AND fii_net_cr IS NOT NULL;

-- Also backfill rolling/flag columns if they exist under old names
-- (run ONLY if these columns exist — check your schema first)
-- UPDATE fii_dii_flow SET
--     fii_net_5d  = fii_net_5d_cumulative,
--     fii_flag    = fii_signal
-- WHERE fii_net_5d IS NULL AND fii_net_5d_cumulative IS NOT NULL;

NOTIFY pgrst, 'reload schema';

-- Verify:
-- SELECT date, fii_net_cr, fii_net, fii_flag FROM fii_dii_flow ORDER BY date DESC LIMIT 5;
-- You should see fii_net = fii_net_cr for all backfilled rows.


-- ═══════════════════════════════════════════════════════════════
-- FIX 2: data_anomalies table + index
-- ═══════════════════════════════════════════════════════════════
--
-- ROOT CAUSE of your error:
--   The CREATE INDEX ran before the CREATE TABLE. Table must exist first.
--
-- STEP 1: Create the table
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.data_anomalies (
    id         BIGSERIAL PRIMARY KEY,
    date       DATE,
    check_name TEXT,
    severity   TEXT,       -- 'OK' | 'WARN' | 'ERROR'
    value      TEXT,
    message    TEXT,
    affected   TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- STEP 2: Create the index (now safe — table exists)
-- ─────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_anomalies_check
    ON data_anomalies (check_name, date DESC);

CREATE INDEX IF NOT EXISTS idx_anomalies_date
    ON data_anomalies (date DESC);

NOTIFY pgrst, 'reload schema';

-- Verify:
-- SELECT table_name FROM information_schema.tables WHERE table_name = 'data_anomalies';
-- Should return one row.

-- ═══════════════════════════════════════════════════════════════
-- TradeOS v6 — Remaining SQL Patches
-- Run in Supabase SQL Editor after migration_strategic_fixes.sql
-- ═══════════════════════════════════════════════════════════════


-- ── G14: regime_history table ────────────────────────────────────────────────
-- Required by append_history.py (_snapshot_regime) and evolution_tracker.py
-- (Phase 4 drift analysis). Already referenced in schema but may not exist.

CREATE TABLE IF NOT EXISTS public.regime_history (
    id                  BIGSERIAL PRIMARY KEY,
    date                DATE,
    regime              TEXT,
    nifty_price         NUMERIC,
    nifty_50dma        NUMERIC,
    nifty_200dma       NUMERIC,
    nifty_weekly_rsi   NUMERIC,
    india_vix          NUMERIC,
    avg_sector_breadth NUMERIC,
    ctl_enabled        BOOLEAN,
    sbs_enabled        BOOLEAN,
    tpo_enabled        BOOLEAN,
    eap_enabled        BOOLEAN
);