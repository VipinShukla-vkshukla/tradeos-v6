-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 023: single-active-daemon lease
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Running the intraday monitor on a laptop AND a server sounds like insurance.
-- It is the opposite. Every guard that prevents duplicate action is IN-MEMORY,
-- per process:
--
--   order_manager._recent   stops the same SELL being sent twice
--   order_manager._blocked  latches a configuration rejection
--   engine._recorded        stops the same setup being recorded repeatedly
--   Notifier._last          stops the same alert being sent repeatedly
--
-- None can see another machine. Two daemons would each place the same exit
-- order, each open the same paper position, each send every alert twice, and
-- push conflicting GTT updates. Redundancy that duplicates side effects is
-- worse than none, because what it creates is silent and expensive while what
-- it prevents is loud and recoverable.
--
-- So: one ACTIVE daemon holds a short lease and renews it; any other starts in
-- STANDBY, computes everything, commits nothing, and promotes itself
-- automatically when the lease stops being renewed.
--
-- THE REAL SAFETY NET IS NOT THIS
-- If both daemons are down, positions still have GTT stops resting at Zerodha.
-- That is the answer to "what if the cloud fails" — a stop that needs no daemon
-- at all. This table only stops two processes colliding.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.intraday_daemon_lease (
    id          integer PRIMARY KEY,
    holder      text,
    hostname    text,
    acquired_at timestamptz,
    expires_at  timestamptz,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.intraday_daemon_lease IS
  'Single row. Whoever holds an unexpired lease is the only daemon permitted to '
  'place orders, send alerts or write positions.';

INSERT INTO public.intraday_daemon_lease (id, holder, hostname, expires_at)
VALUES (1, '', 'none', now())
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_lease_ttl_seconds', '120',
   'How long a daemon''s claim survives without renewal. Long enough that a slow cycle does not drop it, short enough that a dead process is replaced in about two minutes — a position needing an exit cannot wait ten.',
   'Intraday', 'intraday/lease.py', 'int', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Daemon lease ready. Run the monitor on BOTH machines safely:';
  RAISE NOTICE '  whichever starts first is ACTIVE; the other is a hot STANDBY';
  RAISE NOTICE '  and takes over within ~2 minutes if the active one stops.';
END $$;
