-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 077: one daemon per account, refused at startup
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHAT HAPPENED
-- On 2026-08-10 at 09:36 two daemons swept the same LIVE account inside one
-- minute. intraday_broker_log id=860 latched an IP rejection account-wide, and
-- ids 865/866/867/871/874 PLACED six real orders 12-65 seconds later,
-- interleaved with nine echoes of the very latch that forbids them.
-- _blocked_account has one assignment site and no reset anywhere in the tree,
-- so one process cannot do that. A second, independent confirmation comes from
-- a different module global: id=878 reports "an identical BUY for AUBANK was
-- placed 22s ago", and 09:36:46 - 22s is exactly id=871 — so the process that
-- placed is the one that blocked, and it is not the process that echoed the
-- latch two seconds earlier.
--
-- WHY THAT IS THE EXPENSIVE FAILURE AND NOT A COSMETIC ONE
-- Every duplicate guard in execution/order_manager.py is a module global:
-- _recent (the 5-minute duplicate-order window), _blocked, _blocked_account,
-- and the daily caps read through _today_totals(). With two processes all four
-- are doubled. "PPLPHARMA sold twice this way" is already a CLAUDE.md landmine;
-- this is the mechanism that produces it.
--
-- WHY MIGRATION 023's LEASE DID NOT STOP IT
-- The lease is a ROLE, not a mutex. Three things let both sides act:
--   1. lease.acquire() reads the row, then upserts UNCONDITIONALLY. It never
--      re-asserts what it read, so two starts in the same window both claim.
--   2. The loser only finds out on its next renew — run.py checks the lease on
--      a 30s timer while the engine evaluates every 15s, so a demoted daemon
--      places orders for one or two more cycles. The 10-Aug window is 62s.
--   3. Migration 050's intraday_lease_primary_host makes the overlap
--      deliberate: a configured primary SKIPS the deference check and claims a
--      live, unexpired lease out from under a running daemon. That key was set
--      to 'tradeos-vcn' on 2026-08-06, four days before the incident.
-- Both acquire() and renew() also fail OPEN to ACTIVE on any database error.
--
-- WHAT THIS MIGRATION CHANGES
-- Nothing about the lease row itself. intraday/lease.py gains a startup claim
-- that is a compare-and-swap against the holder it just read — one UPDATE
-- statement, so Postgres serialises it and the loser gets zero rows back and
-- REFUSES TO START. An expired lease is still free to take, because a
-- legitimate restart after a crash must not be blocked; only a LIVE holder
-- refuses a second daemon. The startup claim deliberately does NOT honour
-- intraday_lease_primary_host: a preference for which machine should run is
-- not authority to barge in on one that already is.
--
-- THE BEHAVIOUR CHANGE THE OPERATOR MUST KNOW
-- Migration 023 promised a hot STANDBY that promotes itself automatically.
-- With this switch on there is no standby — the second daemon exits. Failover
-- becomes: the broker-side GTT stops (which is what lease.py already calls the
-- real safety net), plus a restart once the lease lapses. That is the trade
-- being made deliberately: an automatic takeover is worth less than a
-- guaranteed absence of two writers on one live book.
-- To start the server while the laptop is running, stop the laptop first — its
-- clean shutdown calls lease.release() and frees the row immediately.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── F-10: make the next occurrence attributable instead of inferred ────────
--
-- The 10-Aug timeline can PROVE two writers existed and cannot NAME them,
-- because this table records no process identity. Neither can §6's lone Azure
-- source address be attributed to a process. Two columns fix both.
ALTER TABLE public.intraday_broker_log
    ADD COLUMN IF NOT EXISTS host text,
    ADD COLUMN IF NOT EXISTS pid  integer;

COMMENT ON COLUMN public.intraday_broker_log.host IS
  'socket.gethostname() of the process that attempted this broker write. Null '
  'on every row written before migration 077.';
COMMENT ON COLUMN public.intraday_broker_log.pid IS
  'os.getpid() of the process that attempted this broker write. Together with '
  'host it distinguishes two daemons on one account, which the 2026-08-10 '
  'incident could only infer from the behaviour of per-process module globals.';

-- Attribution is always a "which host wrote this stretch" question.
CREATE INDEX IF NOT EXISTS idx_broker_log_host_ts
    ON public.intraday_broker_log (host, ts DESC);

-- ── The switch ─────────────────────────────────────────────────────────────
--
-- Defaults ON. An unguarded duplicate daemon is an unbounded-loss path — two
-- exit ladders and two entry sweeps on one set of shares, with every
-- process-local guard silently halved — and that is the same reasoning the
-- corporate-action guard ships on by default. A switch exists at all because
-- refusing to start is itself a way to have no daemon, and the operator must
-- be able to override that from the database when they know why.
INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_single_daemon_lock', 'true',
   'Refuse to start the intraday daemon while another one holds an unexpired lease. The claim is a compare-and-swap on intraday_daemon_lease.holder, so two simultaneous starts cannot both win. An EXPIRED lease is still claimable — a legitimate restart after a crash is never blocked. Off reverts to migration 023 behaviour, where a second daemon starts as a hot standby and may act for up to one renew interval after losing the lease.',
   'Intraday', 'intraday/lease.py', 'bool', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Single-daemon startup lock is ON.';
  RAISE NOTICE '  A second daemon now REFUSES TO START while the first holds an';
  RAISE NOTICE '  unexpired lease, and says who holds it and for how long.';
  RAISE NOTICE '  There is no hot standby any more — stopping the running daemon';
  RAISE NOTICE '  releases the lease immediately, so a handover is stop-then-start.';
  RAISE NOTICE '';
  RAISE NOTICE 'intraday_broker_log now records host and pid on every attempt.';
  RAISE NOTICE '';
  RAISE NOTICE 'Disable: UPDATE system_config SET value=''false'' WHERE key=''intraday_single_daemon_lock'';';
END $$;
