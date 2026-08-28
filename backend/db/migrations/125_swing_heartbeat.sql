-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 125: swing_heartbeat
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 28-Aug-2026. The Overview dashboard's "Today's Signal Funnel" for SWING
-- showed 100 Watched / 15 Allocator-scored against the operator's own live
-- log line reading "77 watched · 26 buyable now" — because the funnel was
-- never backed by what the daemon actually computes. "Watched" came from
-- master_shortlist's row count (last night's evening-pipeline shortlist,
-- ~100 names) and "Allocator-scored" from a distinct-symbol count over a
-- 500-row recent-first allocation_decisions window — two numbers that were
-- never claimed to equal _log_swing_state()'s in-memory watched/buyable
-- counts, just the closest persisted proxies available at the time.
--
-- _log_swing_state() (intraday/engine.py) already computes the real numbers
-- every ~15s cycle — held, watched (len(self.candidates)), buyable now
-- (decide() says BUY_NOW/CHASE_LIMIT at the live price), ready to enter
-- (buyable AND in today's top-ranked field AND budget left), taken — and
-- until now only ever logged them. This table is intraday_heartbeat's own
-- pattern (one row, overwritten, proof of life) applied to swing's
-- equivalent state, so the dashboard can read the exact numbers the log
-- prints instead of reconstructing an approximation from unrelated tables.
--
-- No config switch — a pure read/write observability addition, no live-money
-- behaviour gated by it.

CREATE TABLE IF NOT EXISTS public.swing_heartbeat (
    id           integer PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    held         integer NOT NULL DEFAULT 0,
    watched      integer NOT NULL DEFAULT 0,
    buyable_now  integer NOT NULL DEFAULT 0,
    ready_now    integer NOT NULL DEFAULT 0,
    taken        integer NOT NULL DEFAULT 0,
    entries_cap  integer NOT NULL DEFAULT 0,
    eligible     text
);

COMMENT ON TABLE public.swing_heartbeat IS
  'Proof of life for swing, mirroring intraday_heartbeat. One row, '
  'overwritten — this is state, not history, so it must never grow. '
  'Written by _log_swing_state() every cycle it has candidates to report.';
