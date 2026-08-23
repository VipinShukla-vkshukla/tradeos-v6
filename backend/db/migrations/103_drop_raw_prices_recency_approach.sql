-- 103_drop_raw_prices_recency_approach.sql
-- 24-Aug-2026 (Stage D2f, docs/TRADEOS_ROADMAP.md Track D)
--
-- Migration 101 (F-58) built a raw_prices-based recency check for
-- Population C's bootstrap gap. The operator scrapped it directly:
-- "you cannot use raw prices count to identify the new listings, it has
-- n number of different records... unnecessarily complicating the
-- things" -- after live-testing had already found real coverage-gap
-- false positives in it (ZEEMEDIA, a long-listed company, misclassified
-- as a fresh IPO). Migration 101 itself is committed history and is not
-- rewritten -- this migration is the correction of record, same pattern
-- migration 098 used to correct 097's stale text after IT was committed.
--
-- Replaced by migration 102 (ipo_listings, NSE's own confirmed IPO
-- archive) and intraday/scanner.py::recent_ipo_candidates().
DROP FUNCTION IF EXISTS public.get_raw_prices_first_seen(text[]);

DELETE FROM system_config WHERE key = 'intraday_recent_listing_window_days';
