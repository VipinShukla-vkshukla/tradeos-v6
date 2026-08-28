-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 126: intraday_heartbeat.scanned / .held
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 28-Aug-2026. Same shape as migration 125's swing_heartbeat, on the
-- intraday side of the Overview funnel. "Scanned" showed intraday_universe's
-- row count (~40, today's official pre-screened universe) — the operator's
-- own log line reads "171 scanned", because _log_intraday_state() (intraday/
-- engine.py) computes scanned as len(self._contexts), the POOLED set of
-- symbols the engine is actually pulling bars for this cycle: today's
-- universe + bench/backup names + carried positions (both frameworks) +
-- live swing candidates + the index. Two genuinely different numbers, and
-- the dashboard was showing the narrower one under the log line's own name.
--
-- Extends the existing intraday_heartbeat (migration 011) rather than
-- adding a new table — same one-row-overwritten pattern, written from the
-- same function that already writes summary/alerts_sent via notifier.
-- heartbeat(), just a second, independent upsert call from
-- _log_intraday_state() so this isn't gated by the notifier's own cadence.
--
-- No config switch — a pure read/write observability addition, no live-money
-- behaviour gated by it.

ALTER TABLE public.intraday_heartbeat
    ADD COLUMN IF NOT EXISTS scanned integer,
    ADD COLUMN IF NOT EXISTS held    integer;

COMMENT ON COLUMN public.intraday_heartbeat.scanned IS
  'len(self._contexts) as of the last cycle — matches "_log_intraday_state"''s own log line exactly. NOT the same as intraday_universe''s row count: this is the pooled set of symbols the engine is currently pulling bars for (today''s universe + bench/backup names + carried positions + live swing candidates + index), which is why it does not equal the ~40-name daily universe and changes through the session.';
COMMENT ON COLUMN public.intraday_heartbeat.held IS
  'INTRADAY-framework open position count as of the last cycle, same source as _log_intraday_state.';
