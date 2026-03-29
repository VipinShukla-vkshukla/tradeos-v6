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
