-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 119: candidate_monitor.py demoted to a lease-gated
-- fallback, Phase 2b of the swing framework evolution blueprint
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. docs/PHASE4_RED_TEAM.md's "C2 — 'One allocator' is false"
-- finding, closed for the second of its two named entry paths (Phase 2a
-- closed the third — control/execution_engine.py). control/
-- candidate_monitor.py runs on a GitHub Actions cron, entirely outside the
-- daemon's lease, evaluating the same buyability question on a FLAT
-- min_rr_to_enter bar that never scales for regime — looser than the
-- daemon's regime_min_rr() whenever the regime bar is above 1.0, and with
-- no order-placement code at all. BLUEJET, 26-Aug: never once reached the
-- daemon's own allocator scoring (zero rows in allocation_decisions, ever)
-- yet fired repeated BUY_NOW alerts from this monitor all morning — for a
-- trade this monitor could never actually place.
--
-- candidate_monitor.py now checks intraday.lease.observe() (read-only)
-- before sending: while the daemon's lease is healthy, this monitor keeps
-- evaluating and writing candidate_watch for audit, but suppresses its own
-- alerts — the daemon already covers the identical job, better (real-time
-- prices, regime-scaled bar, allocator-aware, 15s vs. a jittery 30-min
-- cron). Only when the lease looks stale or absent does it fire, and it
-- says so plainly ("daemon appears down, degraded monitor") so the
-- operator is never silently blind during a real outage. No new config
-- keys were needed for the gate itself (the lease check has no threshold
-- to tune), but this row documents the switch that was already present and
-- unchanged — candidate_watch_enabled — for anyone auditing this change
-- against system_config, and confirms nothing about it needed to move.
--
-- No-op if the row already exists — this migration exists for the audit
-- trail, not to change a value.
UPDATE public.system_config
   SET description = description || E'\n\n26-Aug-2026: this monitor is now '
                     'gated by intraday.lease.observe() when true — see '
                     'control/candidate_monitor.py and migration 119. '
                     'Disabling this switch stops the monitor entirely, '
                     'lease-healthy or not; it does not affect the '
                     'lease gate itself.'
 WHERE key = 'candidate_watch_enabled';
