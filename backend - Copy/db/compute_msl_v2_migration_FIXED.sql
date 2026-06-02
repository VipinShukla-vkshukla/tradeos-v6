-- ============================================================
-- TradeOS v6 — compute_msl_v2_migration_FIXED.sql
-- Corrects all schema issues identified in code review.
-- Run this INSTEAD of compute_msl_v2_migration.sql
-- ============================================================

-- ── ISSUE 1 FIXED: msl_computed must have SEPARATE columns for ──
--   composite_score (from screen_stocks — screener aggregate)
--   final_score     (from compute_msl — intelligence-weighted)
--   Both write to (date, symbol) PK. They MUST NOT use the same column.

-- ── ISSUE 2 FIXED: Pipeline ordering documented in comments ──────
--   CORRECT ORDER: screen_stocks (10.5) → compute_msl (10.6)
--   screen_stocks populates symbol list → compute_msl enriches them.
--   run_pipeline.py must reflect this order.

-- ── 1. msl_computed — complete v2 schema ─────────────────────────

CREATE TABLE IF NOT EXISTS msl_computed (
    date                  DATE     NOT NULL,
    symbol                TEXT     NOT NULL,

    -- ── From screen_stocks (screener selection metadata) ──────────
    -- These columns are written by screen_stocks.py (step 10.5)
    composite_score       NUMERIC,          -- screener aggregate (NOT same as final_score)
    base_score            NUMERIC,          -- per-engine average before convergence
    convergence_pts       NUMERIC,          -- bonus pts for multi-engine confirmation
    eap_adjustment        NUMERIC,          -- EAP overlay delta (+/-)
    engines_list          TEXT,             -- e.g. "CTL+IAD+MOM"
    priority_rank         INTEGER,          -- screener rank (1=best)
    sector_rank           INTEGER,
    screener_source       TEXT,             -- "screen_stocks_v1"

    -- ── From compute_msl (intelligence fields) ────────────────────
    -- These columns are written by compute_msl.py (step 10.6)
    -- NOTE: final_score here is the INTELLIGENCE score — different from composite_score
    final_score           NUMERIC,          -- intelligence-weighted composite (0-100)
    momentum_state        TEXT,
    momentum_phase        TEXT,
    velocity_state        TEXT,
    trend_maturity        TEXT,
    lifecycle             TEXT,
    struct_edge           TEXT,
    reentry_mode          TEXT,
    entry_zone_low        NUMERIC,
    entry_zone_high       NUMERIC,
    entry_timing_type     TEXT,
    price_location        TEXT,
    dist_fv_pct           NUMERIC,
    dist_entry_pct        NUMERIC,
    validity_score        NUMERIC,
    expected_r            NUMERIC,
    in_position           BOOLEAN,
    days_in_list          INTEGER,
    rank_vel_3d           NUMERIC,
    score_vel_5d          NUMERIC,
    holding_score         NUMERIC,
    momentum_score        NUMERIC,
    institutional_score   NUMERIC,
    breakout_readiness    NUMERIC,
    risk_score            NUMERIC,
    days_to_trigger_est   INTEGER,
    ma_alignment_score    INTEGER,
    ha_signal             TEXT,
    bb_squeeze            BOOLEAN,
    bb_position_pct       NUMERIC,
    bb_context            TEXT,
    bb_width_pct          NUMERIC,
    macd_direction        TEXT,
    psar_dual_confirmed   BOOLEAN,
    st_cushion_pct        NUMERIC,
    vwap_alignment        TEXT,
    volume_trend          TEXT,
    weekly_structure      TEXT,
    fundamental_quality   TEXT,
    stoch_context         TEXT,
    active_regime         TEXT,
    rsi_extended_thresh   NUMERIC,

    -- ── Shared identity fields ────────────────────────────────────
    company_name          TEXT,
    sector                TEXT,
    strategy_source       TEXT,             -- engines list from screener
    current_price         NUMERIC,

    -- ── Metadata ─────────────────────────────────────────────────
    compute_source        TEXT,
    computed_at           TIMESTAMPTZ,

    PRIMARY KEY (date, symbol)
);

COMMENT ON TABLE msl_computed IS
  'Joint output of screen_stocks (screener metadata) and compute_msl (intelligence fields). '
  'composite_score = screener aggregate. final_score = intelligence-weighted composite. '
  'These are different concepts and must NOT be confused. '
  'Serves as safe shadow target during transition from Sheet MSL.';

COMMENT ON COLUMN msl_computed.composite_score IS
  'Written by screen_stocks.py. Screener aggregate: avg engine score + convergence bonus + EAP adjustment.';
COMMENT ON COLUMN msl_computed.final_score IS
  'Written by compute_msl.py. Intelligence-weighted composite: momentum + validity + institutional + breakout - risk penalty.';


-- ── 2. master_shortlist — add all new columns ─────────────────────

ALTER TABLE master_shortlist
    -- Screener provenance (from screen_stocks)
    ADD COLUMN IF NOT EXISTS force_include       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS engines_list        TEXT,
    ADD COLUMN IF NOT EXISTS convergence_pts     NUMERIC,
    ADD COLUMN IF NOT EXISTS eap_adjustment      NUMERIC,
    -- Intelligence scores (from compute_msl)
    ADD COLUMN IF NOT EXISTS holding_score       NUMERIC,
    ADD COLUMN IF NOT EXISTS momentum_score      NUMERIC,
    ADD COLUMN IF NOT EXISTS institutional_score NUMERIC,
    ADD COLUMN IF NOT EXISTS breakout_readiness  NUMERIC,
    ADD COLUMN IF NOT EXISTS risk_score          NUMERIC,
    ADD COLUMN IF NOT EXISTS days_to_trigger_est INTEGER,
    -- New signal contexts
    ADD COLUMN IF NOT EXISTS ma_alignment_score  INTEGER,
    ADD COLUMN IF NOT EXISTS ha_signal           TEXT,
    ADD COLUMN IF NOT EXISTS bb_squeeze          BOOLEAN,
    ADD COLUMN IF NOT EXISTS bb_position_pct     NUMERIC,
    ADD COLUMN IF NOT EXISTS bb_context          TEXT,
    ADD COLUMN IF NOT EXISTS bb_width_pct        NUMERIC,
    ADD COLUMN IF NOT EXISTS macd_direction      TEXT,
    ADD COLUMN IF NOT EXISTS psar_dual_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS st_cushion_pct      NUMERIC,
    ADD COLUMN IF NOT EXISTS vwap_alignment      TEXT,
    ADD COLUMN IF NOT EXISTS volume_trend        TEXT,
    ADD COLUMN IF NOT EXISTS weekly_structure    TEXT,
    ADD COLUMN IF NOT EXISTS fundamental_quality TEXT,
    ADD COLUMN IF NOT EXISTS stoch_context       TEXT,
    ADD COLUMN IF NOT EXISTS active_regime       TEXT,
    ADD COLUMN IF NOT EXISTS rsi_extended_thresh NUMERIC;

COMMENT ON COLUMN master_shortlist.force_include IS
  'Manual override: if TRUE, symbol always included regardless of screener output.';
COMMENT ON COLUMN master_shortlist.holding_score IS
  '0-100: Should I STAY in this position? Separate from validity_score (should I ENTER?).';
COMMENT ON COLUMN master_shortlist.bb_context IS
  'Bollinger Band context: SQUEEZE_BULLISH | RIDING_UPPER | UPPER_HALF | MIDDLE | NEAR_LOWER | SQUEEZE_AT_LOW';
COMMENT ON COLUMN master_shortlist.vwap_alignment IS
  'Multi-timeframe VWAP: ABOVE_ALL | ABOVE_ALL_EXTENDED | ABOVE_20D | BELOW_ALL | MIXED';


-- ── 3. signal_log — add new compute_msl context fields ────────────
-- generate_signals can pass these to send_alerts and ai_enrich

ALTER TABLE signal_log
    ADD COLUMN IF NOT EXISTS filter_reason       TEXT,          -- from generate_signals v2
    ADD COLUMN IF NOT EXISTS holding_score       NUMERIC,       -- from compute_msl
    ADD COLUMN IF NOT EXISTS ma_alignment_score  INTEGER,
    ADD COLUMN IF NOT EXISTS bb_context          TEXT,
    ADD COLUMN IF NOT EXISTS vwap_alignment      TEXT,
    ADD COLUMN IF NOT EXISTS weekly_structure    TEXT,
    ADD COLUMN IF NOT EXISTS psar_dual_confirmed BOOLEAN,
    ADD COLUMN IF NOT EXISTS momentum_score      NUMERIC,
    ADD COLUMN IF NOT EXISTS institutional_score NUMERIC,
    ADD COLUMN IF NOT EXISTS breakout_readiness  NUMERIC,
    ADD COLUMN IF NOT EXISTS risk_score          NUMERIC,
    ADD COLUMN IF NOT EXISTS days_to_trigger_est INTEGER,
    ADD COLUMN IF NOT EXISTS volume_trend        TEXT,
    ADD COLUMN IF NOT EXISTS fundamental_quality TEXT,
    ADD COLUMN IF NOT EXISTS active_regime       TEXT;


-- ── 4. system_config — all new control keys ───────────────────────

INSERT INTO system_config (key, value, description) VALUES
('compute_msl_mode',         'shadow', 'compute_msl write mode: shadow|hybrid|full'),
('screener_mode',            'shadow', 'screen_stocks write mode: shadow|hybrid|full'),
('screener_max_symbols',     '30',     'Max symbols per screener run'),
('screener_max_per_sector',  '5',      'Sector diversification cap')
ON CONFLICT (key) DO NOTHING;


-- ── 5. Reload schema cache ────────────────────────────────────────
NOTIFY pgrst, 'reload schema';


-- ── 6. CRITICAL: Pipeline ordering fix ───────────────────────────
-- run_pipeline.py must have steps in this exact order:
--   ("10_compute_indicators",  step_compute_indicators,  False),
--   ("10.5_screen_stocks",     step_screen_stocks,       False),  ← FIRST
--   ("10.6_compute_msl",       step_compute_msl,         False),  ← SECOND
--   ("11_regime_predict",      step_regime_predict,      False),
--
-- Rationale: screen_stocks writes symbol list to msl_computed.
--            compute_msl reads msl_computed to enrich those symbols.
--            Reversing this means compute_msl enriches yesterday's symbols.


-- ── 7. Validation queries (run after next pipeline) ───────────────

-- Check both tables have data:
SELECT 'msl_computed' AS tbl, COUNT(*) AS rows, MAX(date) AS latest FROM msl_computed
UNION ALL
SELECT 'master_shortlist', COUNT(*), MAX(date) FROM master_shortlist
WHERE date = CURRENT_DATE;

-- Verify composite_score and final_score are separate (both populated):
SELECT symbol,
       composite_score,  -- from screen_stocks
       final_score,      -- from compute_msl
       engines_list,     -- from screen_stocks
       momentum_state    -- from compute_msl
FROM msl_computed
WHERE date = CURRENT_DATE
ORDER BY composite_score DESC NULLS LAST
LIMIT 10;

-- If composite_score is NULL: screen_stocks hasn't run yet today
-- If final_score is NULL: compute_msl hasn't run yet today
-- Both should be populated after the full pipeline runs


-- ── 8. Transition advancement commands ───────────────────────────

-- Both scripts advance together:
-- UPDATE system_config SET value='hybrid' WHERE key IN ('screener_mode','compute_msl_mode');
-- UPDATE system_config SET value='full'   WHERE key IN ('screener_mode','compute_msl_mode');
-- UPDATE system_config SET value='shadow' WHERE key IN ('screener_mode','compute_msl_mode');

-- Force-include a specific symbol (manual override):
-- UPDATE master_shortlist SET force_include=TRUE
-- WHERE symbol='POWERINDIA' AND date=CURRENT_DATE;


-- ── 9. Shadow validation query ────────────────────────────────────

SELECT
    c.symbol,
    c.priority_rank              AS screener_rank,
    ROUND(c.composite_score,1)   AS composite_score,
    ROUND(c.final_score,1)       AS intelligence_score,
    c.engines_list,
    ROUND(m.final_score,1)       AS sheet_score,
    m.momentum_state             AS sheet_momentum,
    c.momentum_state             AS computed_momentum,
    c.lifecycle                  AS computed_lifecycle,
    c.holding_score              AS holding_score,
    c.bb_context,
    c.vwap_alignment,
    CASE WHEN m.symbol IS NULL   THEN '🆕 Screener only'
         WHEN c.symbol IS NULL   THEN '📋 Sheet only'
         ELSE '✅ Both'
    END AS status
FROM msl_computed c
FULL OUTER JOIN master_shortlist m
    ON c.symbol = m.symbol AND c.date = m.date
WHERE COALESCE(c.date, m.date) = CURRENT_DATE
ORDER BY COALESCE(c.priority_rank, 99);
