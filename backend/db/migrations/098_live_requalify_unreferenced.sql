-- 098_live_requalify_unreferenced.sql
-- 23-Aug-2026 (Stage D2b, docs/TRADEOS_ROADMAP.md Track D)
--
-- Migration 097 armed the live re-qualification check for ONE population:
-- names already in stock_data_daily that failed only yesterday's ATR band
-- (scanner.movement_rejected_candidates()). The operator armed
-- intraday_live_requalify_enabled for exactly that, reviewed population,
-- on 23-Aug.
--
-- This migration adds a SECOND, separate switch for a materially different,
-- wider population: scanner.unreferenced_candidates() — names with NO
-- stock_data_daily row at all (found via nifty_total_market and, for
-- genuinely new listings, Kite's own instrument master). These names never
-- ran through _qualifies() and so carry weaker vetting (no delivery% or
-- ASM/F&O-ban check is possible without a bhavcopy-derived row to read them
-- from — see scanner.live_requalify()'s own docstring). Bundling this wider,
-- unreviewed population under the switch already armed for the narrower one
-- would silently widen a gate the operator turned on for something else —
-- this project's own rule against exactly that. Ships FALSE.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_live_requalify_unreferenced_enabled', 'false',
   'Stage D2b: admit a name that has NO stock_data_daily row at all '
   '(sourced from nifty_total_market or, for brand-new listings, Kite''s '
   'own instrument master) once its live numbers clear the same '
   'movement/turnover/price floor. Separate from '
   'intraday_live_requalify_enabled on purpose -- this population never ran '
   'through the swing pipeline''s own price/delivery/ASM gates, so it is a '
   'materially wider, less-vetted admission than that switch already '
   'covers. Arm only once the log (same "compute and log, act only when '
   'armed" shape) shows real, sensible admissions over a stated period.',
   'Master controls', 'intraday/scanner.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;

-- Migration 097's own description text for intraday_live_requalify_interval_s
-- repeated the "1,800+-row" stock_data_daily figure that migration 097's own
-- header comment also carried -- traced to a misreading of ingest_bhavcopy.py
-- (that figure describes raw_prices, the bhavcopy ingest; stock_data_daily is
-- swing's own sheet-baseline table, 499 rows on 21-Aug, only ENRICHED by that
-- ingest with value_cr/delivery_pct for rows already there). The operator
-- caught this. Corrected in place because it is a description string read by
-- anyone auditing system_config later, not a ledger entry -- FINDINGS.md's
-- own append-only rule governs the historical record, not this field.
UPDATE system_config
SET description = 'How often the live re-qualification check runs, in '
                  'seconds. Its own timer, separate from the 300s full '
                  'bench rebuild -- this is a handful of REST quote calls '
                  'against a small, pre-filtered candidate list, not the '
                  'expensive ~500-row stock_data_daily historical scan.'
WHERE key = 'intraday_live_requalify_interval_s';
