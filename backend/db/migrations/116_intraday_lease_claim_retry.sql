-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 116: intraday daemon startup lock — bounded
-- retry (docs/FINDINGS.md F-83)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   claim_startup_lock() (migration 077) was, and remains, a single-shot
--   compare-and-swap: refused once, a caller gave up for good. The systemd
--   timer only tries once a day; `tradeos vcn fix` (option 9) only tries
--   once per invocation. Confirmed live, 25-Aug-2026: the Oracle daemon,
--   refused because the operator's laptop held a live lease, exited and
--   stayed down — and when the laptop's monitor was stopped moments
--   later, nothing was left running on Oracle to notice the lease had
--   freed up. It sat idle until the next day's 09:00 timer.
--
--   intraday/lease.py::claim_startup_lock_with_retry() wraps the SAME,
--   UNMODIFIED compare-and-swap in a bounded poll loop — it changes WHEN
--   the question is asked, never what it is allowed to answer. It still
--   never consults intraday_lease_primary_host against a live holder,
--   the exact deference migration 077 deliberately left out after the
--   2026-08-10 incident (two daemons, 62 seconds, six real orders).
--
-- Additive only. Safe to re-run. Defaults preserve the shape of the
-- retry the code already falls back to (TTL + 30s, polled every 10s) —
-- these rows exist so it is a tunable switch, not a hardcoded number,
-- matching every other threshold in this codebase.
INSERT INTO public.system_config (key, value, description) VALUES
  ('intraday_startup_claim_retry_seconds', '150',
   'How long claim_startup_lock_with_retry() keeps retrying a refused '
   'startup claim before giving up, in seconds. Defaults to the lease TTL '
   '(120s) plus a 30s margin — long enough to catch "already stopped, just '
   'has not aged out yet", short enough that a genuinely still-active '
   'other daemon is reported as a refusal rather than a silent hang.'),
  ('intraday_startup_claim_poll_seconds', '10',
   'How often claim_startup_lock_with_retry() re-checks during the retry '
   'window, in seconds.')
ON CONFLICT (key) DO NOTHING;
