-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 016: storage guard for the 500 MB free tier
-- ═══════════════════════════════════════════════════════════════════════════
--
-- MEASURED, NOT ESTIMATED
-- -----------------------
-- stock_data_daily holds 48,967 rows across 86 columns — roughly 190 MB before
-- indexes, and indexes on this table are not small. It grows by ~499 rows every
-- trading day, about 2 MB a day or 40 MB a month.
--
-- At ~98 trading days of history the database is already around half the free
-- tier. Straight-lining that:
--
--     250 trading days  ->  ~480 MB   (roughly one year from now)
--     400 trading days  ->  ~775 MB   (over the limit)
--
-- Hitting the ceiling does not degrade gracefully. Writes start failing, which
-- means the evening pipeline fails, which means no signals — and it happens on
-- an ordinary Tuesday with no warning.
--
-- WHY RETENTION AND NOT COMPRESSION
-- ---------------------------------
-- The longest lookback anything actually needs is the 200-day moving average.
-- Everything beyond ~250 trading days of full daily detail is being stored for
-- no reader. Rather than compress rows nobody queries, keep a generous window
-- of full detail and archive the essentials of anything older.
--
-- The archive keeps OHLCV plus the handful of columns backtests use, at 9
-- columns instead of 86 — about a tenth the size — so long-horizon analysis
-- stays possible without carrying 77 indicator columns per row forever.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── Slim archive for pre-window history ────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stock_data_archive (
    symbol  text    NOT NULL,
    date    date    NOT NULL,
    open    numeric,
    high    numeric,
    low     numeric,
    close   numeric,
    volume  numeric,
    atr_pct numeric,
    PRIMARY KEY (symbol, date)
);

COMMENT ON TABLE public.stock_data_archive IS
  'OHLCV + ATR for dates rolled out of stock_data_daily. Nine columns instead '
  'of 86 — indicators are recomputable from price, so storing them forever is '
  'storing a derivation rather than a fact.';

CREATE INDEX IF NOT EXISTS idx_stock_archive_date ON public.stock_data_archive (date DESC);


-- ── The roll-off ───────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.archive_stock_data(keep_days integer DEFAULT 250)
RETURNS TABLE(archived bigint, deleted bigint, cutoff date)
LANGUAGE plpgsql
AS $$
DECLARE cut date; a bigint; d bigint;
BEGIN
  -- Cutoff by TRADING days present in the table, not calendar days — holidays
  -- and weekends would otherwise silently shorten the retained window below
  -- what the 200-day average needs.
  SELECT dt INTO cut FROM (
      SELECT DISTINCT date AS dt FROM public.stock_data_daily ORDER BY date DESC
      OFFSET GREATEST(keep_days - 1, 0) LIMIT 1
  ) q;

  IF cut IS NULL THEN
    RETURN QUERY SELECT 0::bigint, 0::bigint, NULL::date;   -- not enough history yet
    RETURN;
  END IF;

  INSERT INTO public.stock_data_archive (symbol, date, open, high, low, close, volume, atr_pct)
  SELECT symbol, date, open, high, low, close, volume, atr_pct
    FROM public.stock_data_daily
   WHERE date < cut
  ON CONFLICT (symbol, date) DO NOTHING;
  GET DIAGNOSTICS a = ROW_COUNT;

  DELETE FROM public.stock_data_daily WHERE date < cut;
  GET DIAGNOSTICS d = ROW_COUNT;

  RETURN QUERY SELECT a, d, cut;
END $$;

COMMENT ON FUNCTION public.archive_stock_data IS
  'Archive-then-delete, in that order and in one transaction, so a failure '
  'leaves the data in stock_data_daily rather than nowhere. Safe to re-run.';


-- ── Visibility, so the ceiling is never a surprise ─────────────────────────
CREATE OR REPLACE VIEW public.v_storage_usage AS
SELECT
    c.relname                                        AS table_name,
    pg_size_pretty(pg_total_relation_size(c.oid))    AS total_size,
    pg_total_relation_size(c.oid)                    AS total_bytes,
    c.reltuples::bigint                              AS approx_rows,
    round(100.0 * pg_total_relation_size(c.oid)
          / (500 * 1024 * 1024), 1)                  AS pct_of_free_tier
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r'
 ORDER BY pg_total_relation_size(c.oid) DESC;

COMMENT ON VIEW public.v_storage_usage IS
  'Real sizes including indexes and TOAST, as a percentage of the 500 MB free '
  'tier. Read by the health dashboard so the ceiling is visible months before '
  'it is reached — hitting it fails WRITES, which means no evening pipeline, '
  'on an ordinary day with no warning.';


DO $$
DECLARE total_mb numeric; big text;
BEGIN
  SELECT round(sum(pg_total_relation_size(c.oid)) / 1024.0 / 1024.0, 1)
    INTO total_mb
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relkind = 'r';

  SELECT string_agg(t, ', ') INTO big FROM (
    SELECT c.relname || ' ' || pg_size_pretty(pg_total_relation_size(c.oid)) AS t
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind = 'r'
     ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 5
  ) q;

  -- Two placeholders, two arguments. The earlier version passed three against
  -- '% MB of 500 MB (% %%)' — %% is a literal percent sign, not a placeholder,
  -- so PL/pgSQL counted two and refused the third.
  RAISE NOTICE 'Database is % MB of 500 MB (% percent)', total_mb, round(total_mb / 5.0, 1);
  RAISE NOTICE 'Largest: %', big;
  RAISE NOTICE 'Roll off history with:  SELECT * FROM archive_stock_data(250);';
  RAISE NOTICE 'Nothing is deleted by this migration — the function is opt-in.';
END $$;
