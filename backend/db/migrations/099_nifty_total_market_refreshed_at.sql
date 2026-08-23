-- 099_nifty_total_market_refreshed_at.sql
-- 23-Aug-2026 (Stage D2c, docs/TRADEOS_ROADMAP.md Track D)
--
-- nifty_total_market had no freshness column at all -- confirmed live,
-- 23-Aug, when checking whether "static" (the operator's own word for it)
-- could be verified rather than assumed. Adding one so the new weekly
-- refresher (swing/ingestion/ingest_nifty_total_market.py) has somewhere
-- honest to record when a row was last confirmed against NSE's own
-- constituent lists, and so anyone auditing this table later can tell
-- a fresh row from a stale one without re-deriving it from git history.
-- Nullable, no default -- an unset value HONESTLY means "never refreshed
-- by the new job", not a fabricated timestamp.
ALTER TABLE nifty_total_market
  ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMPTZ;
