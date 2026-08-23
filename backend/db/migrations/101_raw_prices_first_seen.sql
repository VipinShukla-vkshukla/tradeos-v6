-- 101_raw_prices_first_seen.sql
-- 23-Aug-2026 (Stage D2e, docs/TRADEOS_ROADMAP.md Track D)
--
-- The operator's own catch: MILKYMIST first traded 18-Aug-2026, five days
-- before new_listings() (F-57) existed. Its bootstrap seed silently marked
-- it "already known" alongside 2,978 genuinely old symbols -- indistinguish-
-- able from RELIANCE from that point on. This RPC lets scanner.py ask
-- raw_prices (NSE's own bhavcopy, no Chartink/index dependency) whether a
-- symbol's OWN trading history actually starts recently -- real signal a
-- stock listed recently, not a guess.
--
-- Mirrors the existing get_symbol_history_summary(price_history_yf) RPC's
-- shape exactly, pointed at raw_prices instead.
CREATE OR REPLACE FUNCTION public.get_raw_prices_first_seen(p_symbols text[])
RETURNS TABLE(symbol text, first_date date, row_count bigint)
LANGUAGE sql
STABLE
AS $$
  SELECT
    symbol,
    MIN(date)::DATE  AS first_date,
    COUNT(*)::BIGINT AS row_count
  FROM raw_prices
  WHERE symbol = ANY(p_symbols)
  GROUP BY symbol;
$$;

COMMENT ON FUNCTION public.get_raw_prices_first_seen IS
  'Per-symbol earliest raw_prices date + row count, for the given symbol '
  'list. Used by intraday/scanner.py::_recently_listed() to distinguish a '
  'genuinely recent listing from a symbol merely untracked by stock_data_'
  'daily/nifty_total_market -- NOT reliable for a symbol whose true first '
  'trade predates raw_prices'' own 120-day retention window.';

-- intraday_recent_listing_window_days, NOT "buffer past raw_prices' own
-- window start" (the original, wrong design in this same migration,
-- corrected before it was ever committed). Caught live: ZEEMEDIA (a
-- long-listed company, not remotely a recent listing) has a genuine
-- ~4-week GAP in its raw_prices coverage starting 19-May-2026 -- well
-- inside a 120-day window, so "started partway through the retention
-- window" is NOT reliable evidence of a listing event; raw_prices has
-- real per-symbol coverage gaps unrelated to recency. A fixed, SHORT,
-- today-relative window is a materially stronger claim: real IPOs are
-- rare (a handful a month), so trading history older than 30 days is far
-- more likely a coverage gap on an established stock than a fresh listing.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_recent_listing_window_days', '30',
   'Stage D2e: a symbol''s earliest raw_prices row must fall within this '
   'many days of TODAY to be treated as a genuine recent listing. A symbol '
   'whose earliest row is older than this is treated as established, even '
   'if raw_prices'' own coverage of it has gaps -- real IPOs are rare '
   '(a handful a month), so old-but-gapped trading history is far more '
   'likely than a fresh listing.',
   'Master controls', 'intraday/scanner.py', 'int', '30', 'LOW')
ON CONFLICT (key) DO NOTHING;
