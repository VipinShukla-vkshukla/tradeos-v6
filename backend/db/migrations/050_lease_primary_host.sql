-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 050: preferred lease primary
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Migration 023 made the daemon lease a pure race: whichever machine's claim
-- is unexpired holds it, no host preferred. That held until 2026-08-06, when
-- the Oracle server's own lease lapsed from starvation — an expired Kite
-- token turned its per-cycle broker calls into 7s timeouts, stretching the
-- loop past the 120s TTL — and a laptop start claimed the lease in the gap.
-- Nothing was wrong with the race; it did exactly what migration 023 built it
-- to do. But the operator wants the server to be the durable default and the
-- laptop to be cover for an actual gap, not a competitor on an ordinary
-- morning.
--
-- intraday_lease_primary_host names that machine. When set, lease.acquire()
-- and lease.renew() (backend/intraday/lease.py) let it claim UNCONDITIONALLY
-- — at startup, and mid-run if it ever lapses — which is what let the
-- 2026-08-06 stall go unnoticed for as long as it did: the server had no way
-- to reclaim without a restart. Every other host keeps migration 023's
-- original behaviour verbatim: claim only when nobody holds an unexpired
-- lease. Empty reproduces the original pure race exactly — this key existing
-- must not change anything for a deployment that never sets it, the same
-- reasoning behind every other cold-start-must-be-permissive default here.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_lease_primary_host', 'tradeos-vcn',
   'Hostname prefix of the daemon that always wins the intraday lease when reachable, checked against socket.gethostname(). Empty means no preference: the original migration-023 pure race, whichever unexpired claim exists wins.',
   'Intraday', 'intraday/lease.py', 'string', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Lease primary set to tradeos-vcn. The server now reclaims the';
  RAISE NOTICE 'lease unconditionally, at startup or mid-run; the laptop only';
  RAISE NOTICE 'ever takes over a lease nobody currently holds.';
  RAISE NOTICE '';
  RAISE NOTICE 'Change it:  UPDATE system_config SET value=''newhost'' WHERE key=''intraday_lease_primary_host'';';
  RAISE NOTICE 'Disable it: UPDATE system_config SET value='''' WHERE key=''intraday_lease_primary_host'';';
END $$;
