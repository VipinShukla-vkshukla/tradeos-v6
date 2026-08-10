-- 10-Aug-2026. One switch, closing the absorbing state that migration 057's
-- absolute edge floor created on the PAPER book.
--
-- MEASURED (tools/allocator_replay.py, 10 real sessions, walk-forward):
--     OLD policy: 20 trades, -0.84R total
--     NEW policy:  0 trades,  0.00R
--
-- Zero is the floor working -- refusing a book whose measured net expectancy
-- is negative is the entire point, and -0.84R is what it saved. But zero
-- trades also means zero new rows with cost_verdict = TAKEN, and
-- priors_intraday_taken_only builds every prior FROM those rows. So the prior
-- freezes on the day the floor engages and no amount of engine improvement can
-- ever appear in it: the book is not allowed to try. Once negative, silent
-- forever. That is this project's own "a check that cannot pass" failure with
-- a gate in place of a check.
--
-- The floor exists to protect CAPITAL. A paper book has none. So a live book
-- is blocked and a paper book explores, and BOTH verdicts are written for
-- every proposal -- the allocator's DECLINE to allocation_decisions exactly as
-- it would be live, the exploration entry to intraday_setups. The
-- live-equivalent P&L ("what the floor would have saved") therefore stays
-- computable at any time from data already on disk, so the simulation measures
-- BOTH systems at once rather than measuring the blocked one and learning
-- nothing. It is scoped strictly to floor-driven declines: an ordinary
-- opportunity-cost DECLINE, and any DEFER, are refused in paper exactly as
-- they are live.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_floor_blocks_paper', 'false',
   'When false (default), a proposal refused ONLY by '
   'alloc_edge_absolute_floor is still taken by a PAPER book, recorded as an '
   'exploration entry, so the gate-passed population priors are built from '
   'keeps growing and the book can demonstrate improvement. The allocator''s '
   'DECLINE is still written to allocation_decisions unchanged, so the '
   'live-equivalent result is always recoverable. A LIVE book is never '
   'affected by this switch -- the floor blocks real capital under every '
   'setting. Set true to make the paper book mirror the live block exactly '
   'and accept that its prior stops updating while the floor is engaged.',
   'Master controls', 'intraday/engine.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
