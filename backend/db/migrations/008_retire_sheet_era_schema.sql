-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 008: retire Sheet-era schema
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Evidence: a column-population audit over the latest date of every pipeline
-- table. master_shortlist carried 19 columns that were 100% NULL across all
-- 100 rows; msl_computed carried 53 of 74 dead and had not been written since
-- 2026-04-29.
--
-- Two different causes, handled differently:
--
--   BROKEN WRITER — repaired in code, NOT dropped here:
--     ma_levels      compute_ma_alignment produced it on every row, then the
--                    merge in compute_msl skipped it by name
--                    (`field != "ma_levels"`).
--     trade_allowed  compute_msl computes it from the safety lists, then
--                    PRESERVE_FIELDS discarded the computed value in favour of
--                    a Google Sheet value that has not existed since
--                    ingest_sheets was retired.
--
--   GENUINELY ORPHANED — dropped below. These were populated by Sheet tabs
--   that no longer exist. Nothing in the pipeline can ever write them again,
--   so they are guaranteed NULL for every future row.
--
-- BACKUP FIRST. Every dropped column is copied into a timestamped table so the
-- data (all NULL today, but this is trading infrastructure) is recoverable.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. Backup ──────────────────────────────────────────────────────────────
-- Built with dynamic SQL against information_schema rather than a hardcoded
-- column list. A fixed SELECT fails outright the moment ONE of the columns is
-- already gone — "ERROR: column fv_low does not exist" — which makes the whole
-- migration un-runnable on a database that is partially migrated, and forces
-- hand-editing exactly when you least want to be hand-editing schema DDL.
-- Backing up whatever still exists is both safe and re-runnable.
DO $$
DECLARE
  cols text;
BEGIN
  SELECT string_agg(quote_ident(column_name), ', ')
    INTO cols
    FROM information_schema.columns
   WHERE table_schema = 'public'
     AND table_name   = 'master_shortlist'
     AND column_name IN ('date','symbol','fv_low','fv_high','opp_type','pos_type',
                         'is_ipo','suggested','notes','entry_ready','entry_action',
                         'entry_mode','exec_eligibility','event_bias','event_sectors',
                         'upcoming_news');

  IF cols IS NULL THEN
    RAISE NOTICE 'master_shortlist: no Sheet-era columns remain — backup skipped';
  ELSIF to_regclass('public._backup_msl_sheet_era_20260727') IS NOT NULL THEN
    RAISE NOTICE 'backup table already exists — skipping';
  ELSE
    EXECUTE format(
      'CREATE TABLE public._backup_msl_sheet_era_20260727 AS SELECT %s FROM public.master_shortlist',
      cols);
    RAISE NOTICE 'backed up: %', cols;
  END IF;
END $$;

-- msl_computed may already be renamed by a previous partial run.
DO $$
BEGIN
  IF to_regclass('public.msl_computed') IS NOT NULL
     AND to_regclass('public._backup_msl_computed_20260727') IS NULL THEN
    CREATE TABLE public._backup_msl_computed_20260727 AS
      SELECT * FROM public.msl_computed;
    RAISE NOTICE 'msl_computed backed up';
  ELSE
    RAISE NOTICE 'msl_computed backup skipped (already backed up or table gone)';
  END IF;
END $$;

COMMENT ON TABLE public._backup_msl_sheet_era_20260727 IS
  'Pre-drop snapshot for migration 008. Safe to delete once a few weeks of '
  'clean runs confirm nothing referenced these columns.';


-- ── 2. Drop orphaned Sheet-era columns ─────────────────────────────────────
-- price_location is deliberately NOT dropped: compute_price_location still
-- writes it and generate_signals reads it. Only the columns with no writer go.
ALTER TABLE public.master_shortlist
    DROP COLUMN IF EXISTS fv_low,
    DROP COLUMN IF EXISTS fv_high,
    DROP COLUMN IF EXISTS opp_type,
    DROP COLUMN IF EXISTS pos_type,
    DROP COLUMN IF EXISTS is_ipo,
    DROP COLUMN IF EXISTS suggested,
    DROP COLUMN IF EXISTS entry_ready,
    DROP COLUMN IF EXISTS entry_action,
    DROP COLUMN IF EXISTS entry_mode,
    DROP COLUMN IF EXISTS exec_eligibility,
    DROP COLUMN IF EXISTS event_bias,
    DROP COLUMN IF EXISTS event_sectors,
    DROP COLUMN IF EXISTS upcoming_news;

-- signal_log: written by nothing, read by nothing.
--   sheet_conflict / sheet_conflict_type were the Sheet-vs-computed divergence
--   flags; there is no Sheet left to diverge from.
--   scanner_patterns and in_scanner belonged to an external scanner that was
--   never wired — generate_signals hardcodes in_scanner=False, patterns=None.
--   in_scanner is the worst of the group: generate_signals hardcoded it to
--   False on every row, AND ml_provider carries it as a model FEATURE. A
--   feature that is constant across every training sample contributes exactly
--   zero information while consuming a slot and diluting importance rankings.
ALTER TABLE public.signal_log
    DROP COLUMN IF EXISTS sheet_conflict,
    DROP COLUMN IF EXISTS sheet_conflict_type,
    DROP COLUMN IF EXISTS scanner_patterns,
    DROP COLUMN IF EXISTS in_scanner,
    DROP COLUMN IF EXISTS ai_strategy_validation;

-- stock_data_daily
--   fii_sector_flow  never written; sector-level FII lives on sector_strength
--   kite_price       never written; live price comes from the broker at read
--                    time, it is not an EOD column
ALTER TABLE public.stock_data_daily
    DROP COLUMN IF EXISTS fii_sector_flow,
    DROP COLUMN IF EXISTS kite_price;


-- ── 3. Retire msl_computed ─────────────────────────────────────────────────
-- With screener_mode='full' and compute_msl_mode='full', both screen_stocks
-- and compute_msl write straight to master_shortlist. msl_computed has been a
-- write-once artefact of the shadow-mode transition since 2026-04-29.
--
-- Renamed rather than dropped: the shadow/hybrid code paths still reference it
-- and are removed in the same commit, so keeping the object one migration
-- longer means a mistake is a rename away from recovery instead of a restore.
ALTER TABLE IF EXISTS public.msl_computed
    RENAME TO _retired_msl_computed_20260727;

COMMENT ON TABLE public._retired_msl_computed_20260727 IS
  'Retired in migration 008. Was the shadow-mode target for screen_stocks '
  'before screener_mode moved to full. Last written 2026-04-29 with 53 of 74 '
  'columns NULL. Drop once a few weeks of clean runs confirm nothing reads it.';


-- ── 4. Verification ────────────────────────────────────────────────────────
DO $$
DECLARE n_msl int; n_sig int; n_sdd int;
BEGIN
  SELECT count(*) INTO n_msl FROM information_schema.columns
   WHERE table_schema='public' AND table_name='master_shortlist';
  SELECT count(*) INTO n_sig FROM information_schema.columns
   WHERE table_schema='public' AND table_name='signal_log';
  SELECT count(*) INTO n_sdd FROM information_schema.columns
   WHERE table_schema='public' AND table_name='stock_data_daily';
  RAISE NOTICE 'master_shortlist: % columns (was 113)', n_msl;
  RAISE NOTICE 'signal_log:       % columns (was 118)', n_sig;
  RAISE NOTICE 'stock_data_daily: % columns (was 88)',  n_sdd;
  RAISE NOTICE 'ma_levels and trade_allowed are KEPT — their writers are repaired in code.';
END $$;
