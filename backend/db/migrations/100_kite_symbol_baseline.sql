-- 100_kite_symbol_baseline.sql
-- 23-Aug-2026 (Stage D2d, docs/TRADEOS_ROADMAP.md Track D)
--
-- Population C ("Kite-only" candidates in intraday/scanner.py::
-- unreferenced_candidates()) was defined as "every Kite-known mainboard/
-- ETF symbol not in nifty_total_market or stock_data_daily" -- live-
-- measured at 2,081 names. That was never actually "new listings"; it
-- was almost entirely small/micro-cap NSE names that simply never made
-- it into either reference table, old and known, just untracked. Quoting
-- ~2,100 names every 45s to catch an IPO that happens a handful of times
-- a MONTH is the operator's own, correct objection.
--
-- This table lets the code ask the right question instead: "was this
-- symbol ever seen in Kite's instrument master before?" -- a diff against
-- a persisted baseline, not a diff against two unrelated reference
-- tables. A symbol appears here the first time scanner.py's new_
-- listings() ever observes it; from then on it is no longer "new".
CREATE TABLE IF NOT EXISTS kite_symbol_baseline (
  symbol          TEXT PRIMARY KEY,
  first_seen_date DATE NOT NULL DEFAULT CURRENT_DATE
);
