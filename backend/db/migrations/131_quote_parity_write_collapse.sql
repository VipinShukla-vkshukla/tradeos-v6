-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 131: intraday_quote_parity write-time collapse (unarmed by default)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 10-Sep-2026. intraday_quote_parity logs a comparison every 300s per
-- symbol per field (day_high, day_low, vwap, prev_close, volume) so long
-- as intraday_quote_mode_range/_vwap stay on -- the standing correctness
-- check for those two live-quote switches (tools/health.py::
-- check_quote_parity(), docs/FINDINGS.md). 150,686 rows over 5 trading
-- days live at the time this was measured.
--
-- Measured against that real data before shipping, not estimated, using
-- tools/replay_quote_parity_collapse.py (calls the ACTUAL production
-- range_verdict()/vwap_verdict() functions, not a reimplementation):
--   - volume:      never scored by anything (SCORED excludes it, has
--                   since it was added -- websocket cumulative volume and
--                   a sum of completed bars are different quantities).
--                   Collapse drops it entirely: 31,399 -> 0.
--   - prev_close:   static all session -- yesterday's close cannot change
--                   intraday, so one comparison per symbol per day is the
--                   complete answer. 25,090 -> 360 (once per symbol per
--                   trading day in the window).
--   - day_high/low: range_verdict() asks "was any value EVER bad" over
--                   the whole window -- a boolean existence test, not an
--                   average. The set of DISTINCT diff_pct values fully
--                   determines that answer, so collapsing an EXACT-match
--                   repeat is provably lossless, not merely likely safe.
--   - vwap:         continuously recalculating, so exact-match barely
--                   helps -- a tolerance (0.04%, HALF the tightest engine
--                   gate any real engine trades on -- 0.08%,
--                   vwr_stop_buffer_pct) collapses it instead.
--
-- FIRST MEASUREMENT CAUGHT ITS OWN DEFAULT BEING WRONG. The heartbeat
-- default was initially copied from the allocator's own 1800s (30 min)
-- without re-deriving it for this table -- measured result: only 86.0%
-- fewer rows, because a 30-minute heartbeat forces a write up to 12 times
-- a 6.25-hour session even when nothing changed, which turned out to be
-- the BINDING constraint, not the material-change threshold. Swept
-- 1800/3600/7200/14400/21600s against the real data: verdict stayed an
-- exact MATCH at every value tried (day_high/day_low/vwap all provably or
-- empirically safe regardless of heartbeat length, since the heartbeat
-- only adds EXTRA confirmations of an already-collapsed value, never
-- removes a real one). Settled on 7200s (2 hours) -- still 2-3 "still
-- alive" confirmations across a real session, and check_quote_parity()
-- separately already fails outright if NOTHING was logged in 2 full days,
-- so this heartbeat's only job is a same-day-resolution reassurance signal
-- layered on top of that, not the sole line of defence against a dead
-- logger.
--
-- FINAL RESULT, this configuration, verified against the same real 5-day
-- population: 150,686 -> 10,085 rows (93.3% fewer), range_verdict() and
-- vwap_verdict() both an exact pass/fail MATCH between the raw and
-- collapsed populations. See docs/FINDINGS.md, 10-Sep-2026.
--
-- Ships OFF. Switch-off leaves every write byte-identical to today: every
-- built comparison gets logged, nothing is filtered — see intraday/
-- engine.py::apply_live_quotes()'s own comment at the collapse_on check.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('quote_parity_write_collapse_enabled', 'false',
   'Skip an intraday_quote_parity write when it is a duplicate of the last '
   'one logged for that (symbol, field) today -- volume is dropped '
   'entirely (never scored by anything), prev_close collapses to once per '
   'symbol per day (static all session), day_high/day_low collapse on an '
   'EXACT match (provably lossless for a boolean "was any value ever '
   'bad" check), vwap collapses within quote_parity_vwap_collapse_'
   'tolerance. Off reproduces exact current behaviour: every comparison '
   'built gets logged. Measured 10-Sep-2026: 150,686 -> 10,085 rows '
   '(93.3% fewer), identical range_verdict()/vwap_verdict() output on the '
   'same real data (tools/replay_quote_parity_collapse.py). See '
   'docs/FINDINGS.md.',
   'Storage', 'intraday/engine.py', 'bool', 'false', 'CRITICAL'),

  ('quote_parity_vwap_collapse_tolerance', '0.04',
   'Percentage-point vwap diff_pct movement that forces a new '
   'intraday_quote_parity row even when quote_parity_write_collapse_'
   'enabled is on. HALF the tightest engine gate (0.08%, '
   'vwr_stop_buffer_pct) any engine that reads vwap actually trades on -- '
   'see VWAP_ENGINE_TOLERANCES in tools/quote_parity.py.',
   'Storage', 'intraday/engine.py', 'float', '0.04', 'CRITICAL'),

  ('quote_parity_heartbeat_s', '7200',
   'Seconds since the last logged value for a (symbol, field) that force '
   'a fresh intraday_quote_parity row even with no change, when '
   'quote_parity_write_collapse_enabled is on -- so a quiet, unchanged '
   'market stays distinguishable from parity logging having silently '
   'stopped. Layered on top of check_quote_parity()''s own "zero rows in '
   '2 days" failure, not a substitute for it. Does not apply to '
   'prev_close, which never needs re-checking within a session. Swept '
   '1800-21600s against real data before choosing this -- verdict stayed '
   'an exact match throughout; 7200 balances a same-day liveness signal '
   'against row count. See docs/FINDINGS.md, 10-Sep-2026.',
   'Storage', 'intraday/engine.py', 'int', '7200', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;
