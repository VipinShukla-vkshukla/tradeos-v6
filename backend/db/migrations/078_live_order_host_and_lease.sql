-- 14-Aug-2026. Two guards in execution/order_manager.py::preflight(), which is
-- where every order path in this codebase already converges.
--
-- NUMBERED 078, NOT 077. Migration 077 (single_daemon_lock) is on the
-- unmerged branch fix/single-daemon-lease. This work branched from main, so
-- taking 077 here would produce two different migrations with one number the
-- moment both land. They are independent and may be applied in either order.
--
-- 1. WHICH MACHINE. live_order_host records the identity of the machine
--    permitted to place LIVE orders. The obvious guard is "is my public IP in
--    Zerodha's allowlist" and it is the wrong one: orders leave from ONE
--    machine, the Oracle VCN, whose static IP is correctly allowlisted, while
--    the laptop has a dynamic ISP address allowlisted nowhere by design. An IP
--    comparison run on the laptop is therefore alarming and irrelevant — it
--    describes a machine that has never sent an order — and on the VCN it is
--    nearly always trivially true. Identity is the question a machine can
--    answer about ITSELF, offline, correctly, from anywhere.
--
--    Seeded to the value intraday_lease_primary_host already holds, because
--    the machine that runs the daemon and the machine whose IP is allowlisted
--    are the same machine by construction. The code falls back to that key
--    when this row is absent, so the guard is live the moment the code deploys
--    rather than waiting on this file.
--
-- 2. WHICH PROCESS. F-11: position_lifecycle.main(manage=True) sells real
--    shares through place() and consults no lease, no lock and no daemon. Run
--    from the pipeline while the daemon is live, both processes evaluate the
--    same exit and both send it — every duplicate guard in this system
--    (_recent, _blocked, engine._recorded, Notifier._last) is in-memory and
--    per-process and cannot see the other.
--
--    ENTRIES AND EXITS ARE NOT HELD TO THE SAME BAR. A BUY requires this
--    process to HOLD the lease. A SELL is refused ONLY while a DIFFERENT
--    process holds an UNEXPIRED lease — the one state where something else is
--    demonstrably alive and will exit the position itself. A lease that is
--    free, lapsed or unreadable lets the exit through. That asymmetry is what
--    makes a handover safe: when an active daemon dies its lease is not
--    transferred, it LAPSES, and for up to lease_ttl_seconds the row still
--    names a holder that no longer exists. A symmetric check would spend that
--    window refusing exits on behalf of a dead process.
--
-- BOTH DEFAULT ON, in code as well as here. A guard inert until a migration
-- runs is no guard at all on the day it ships.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('live_order_host_check', 'true',
   'When true, execution/order_manager.py::preflight() refuses to place a '
   'LIVE order from any machine whose hostname is not in live_order_host. '
   'PAPER is exempt: a simulated fill never reaches the broker, so no address '
   'is involved, and refusing paper on the laptop would refuse the one thing '
   'a laptop is for. Replaces the IP-vs-allowlist check that was proposed and '
   'rejected -- an IP comparison is meaningless from any host but the one '
   'that places orders. See tests/test_preflight_host_and_lease.py.',
   'Execution', 'execution/order_manager.py', 'bool', 'true', 'HIGH'),

  ('live_order_host', 'tradeos-vcn',
   'Comma-separated hostnames permitted to place LIVE orders. Each entry is a '
   'case-insensitive PREFIX match on socket.gethostname(), the same idiom as '
   'lease._is_primary(). A list rather than a single value because Zerodha''s '
   'console accepts several allowlisted IPs -- but listing a second host here '
   'is only honest if that host is ALSO allowlisted at the broker. EMPTY '
   'MEANS THE CHECK IS ABSENT, not that everything is denied: a blank key '
   'must not brick live trading on a fresh install. When this row is missing '
   'the code falls back to intraday_lease_primary_host, which names the same '
   'machine.',
   'Execution', 'execution/order_manager.py', 'string', '', 'HIGH'),

  ('live_order_lease_check', 'true',
   'When true, execution/order_manager.py::preflight() refuses an order from '
   'a process that is not managing the book, closing F-11 -- '
   'position_lifecycle.main(manage=True) places live exits with no lease '
   'check at all, on a path run.py''s startup lock does not cover. Applies to '
   'BOTH modes: two processes writing the same paper position poisons the '
   'learning loop exactly as two live orders empty an account. ASYMMETRIC BY '
   'DESIGN -- a BUY needs this process to hold the lease; a SELL is refused '
   'only while a DIFFERENT process holds an UNEXPIRED one, so a lapsed lease '
   'during a handover never stands between the operator and an exit. Reads '
   'the lease through lease.observe(), which is the only function in that '
   'module that does not write. See tests/test_preflight_host_and_lease.py.',
   'Execution', 'execution/order_manager.py', 'bool', 'true', 'HIGH')
ON CONFLICT (key) DO NOTHING;
