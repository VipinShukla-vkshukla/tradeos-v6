-- 102_ipo_listings.sql
-- 24-Aug-2026 (Stage D2f, docs/TRADEOS_ROADMAP.md Track D)
--
-- Replaces the raw_prices-based recency heuristic (F-58, migration 101 —
-- scrapped per the operator's own direction: "you cannot use raw prices
-- count to identify the new listings, it has n number of different
-- records... unnecessarily complicating the things") with NSE's own
-- authoritative IPO archive: https://www.nseindia.com/api/public-past-
-- issues?index=equity — 1,411 real records back to 2003, real NSE
-- symbols (no fuzzy company-name matching needed, unlike groww.in/ipo,
-- which was checked and found to expose no symbol at all), real listing
-- dates. MILKYMIST confirmed present: listed 18-Aug-2026, EQ, matching
-- both the operator's own pasted NSE table and the earlier raw_prices
-- finding exactly.
CREATE TABLE IF NOT EXISTS ipo_listings (
  symbol            TEXT PRIMARY KEY,
  company_name      TEXT,
  security_type     TEXT,       -- EQ (mainboard), SME, BE, IV, bond-series codes, etc.
  issue_price        NUMERIC,
  price_range_low    NUMERIC,
  price_range_high   NUMERIC,
  issue_start_date   DATE,
  issue_end_date     DATE,
  listing_date       DATE,       -- NULL when NSE has not listed it yet
  source             TEXT DEFAULT 'NSE',
  refreshed_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ipo_listings_listing_date_idx ON ipo_listings (listing_date);

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_ipo_recency_days', '45',
   'Stage D2f: a mainboard (EQ) name in ipo_listings (NSE''s own confirmed '
   'IPO archive) must have listed within this many days to be surfaced as a '
   'Population C candidate. 45 days measured 17 real mainboard listings live '
   '24-Aug-2026 -- matches real IPO cadence, not an artifact of a proxy '
   'signal. Independent of, and redundant with, new_listings()''s Kite-diff '
   '-- either source alone missing a name does not silently drop it.',
   'Master controls', 'intraday/scanner.py', 'int', '45', 'LOW')
ON CONFLICT (key) DO NOTHING;
