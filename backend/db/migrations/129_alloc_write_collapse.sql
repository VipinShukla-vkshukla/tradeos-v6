-- ═══════════════════════════════════════════════════════════════════════════
-- Migration 129: allocation_decisions write-time collapse (unarmed by default)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 08-Sep-2026. allocation_decisions is the single biggest table (86 MB,
-- 212k rows live) and its growth is explosive -- 18k-34k SWING rows/trading
-- day since 24-Aug-2026 -- because a candidate gets a fresh row every 15s
-- cycle even when nothing about it changed.
--
-- Two prior attempts at this never touched the real bulk of it. The 27-Aug
-- mean-collapse dedup (alloc_hurdle_dedup_swing, reverted the same day --
-- see docs/FINDINGS.md) and the 29-Aug material-change replay tool
-- (tools/material_change_replay_audit.py, built, never armed) both treat
-- every TAKE row as sacrosanct. Checked live this session: TAKE is NOT a
-- one-time event. NETWEB, 2026-08-31, logged 1,348 separate "TAKE" rows for
-- ONE position held that session -- identical entry/stop/target on every
-- single one (confirmed: count(distinct entry)=1, count(distinct stop)=1,
-- count(distinct target)=1), spanning the full 6.3-hour session. The
-- allocator re-affirms an already-open position's TAKE verdict every cycle
-- exactly like it re-affirms a hovering DECLINE/DEFER candidate -- and
-- because both prior mechanisms exempted TAKE unconditionally, neither ever
-- saw this.
--
-- MEASURED, live 14-day SWING replay (208,247 raw rows), the rule actually
-- shipped here -- verdict change, regime_bucket change, |edge move| >=
-- alloc_write_collapse_edge_threshold, or alloc_write_collapse_heartbeat_s
-- elapsed since the row was first written, applied uniformly including to
-- TAKE (the transition INTO TAKE is itself a verdict change and is always
-- kept -- that row remains the permanent entry record):
--
--     physical rows:        4,848 / 208,247   (97.7% fewer)
--     hurdle bar delta:     p75 +0.00000   p95 +0.00000
--     verdict reconciliation (repeat_count-weighted vs raw): EXACT --
--         DECLINE 158,506 / 158,506 · DEFER 35,962 / 35,962 · TAKE 13,779 / 13,779
--     native_rank drift inside a collapsed run: 0 (all 3,728 groups checked)
--
-- This is not the same class of change as the reverted dedup. That one
-- discarded rows outright, which changes the multiset hurdle() takes a
-- percentile of. This keeps the exact same multiset -- every collapsed
-- observation's edge survives via repeat_count -- and hurdle.py::
-- _empirical_base() expands each row back into repeat_count copies before
-- any percentile is computed. repeat_count defaults to 1, so any row
-- written while this switch is off (or by any framework other than SWING --
-- INTRADAY's own 14-day replay only reached 61.9% reduction with a non-zero
-- bar delta, not shipped here) is mathematically identical to today's
-- behaviour: one copy of one edge value, exactly as now.

ALTER TABLE public.allocation_decisions
  ADD COLUMN IF NOT EXISTS repeat_count integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS first_decided_at timestamptz;

UPDATE public.allocation_decisions
   SET first_decided_at = decided_at
 WHERE first_decided_at IS NULL;

COMMENT ON COLUMN public.allocation_decisions.repeat_count IS
  'How many original 15s-cycle observations this row represents. 1 for '
  'every row written today (collapse off) or by any framework other than '
  'SWING. hurdle.py::_empirical_base() expands a row into repeat_count '
  'copies of its edge before taking a percentile -- this reconstructs the '
  'exact original population, not an approximation of it.';

COMMENT ON COLUMN public.allocation_decisions.first_decided_at IS
  'When this row (or the episode it collapsed) was FIRST written -- the '
  'heartbeat clock for alloc_write_collapse_swing_enabled. decided_at stays '
  'the LATEST observation time, which is what candidate_monitor.py and '
  'every existing "most recent verdict" reader already expect.';

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_write_collapse_swing_enabled', 'false',
   'Collapse repeated allocation_decisions writes for an unchanged SWING '
   'candidate -- including a TAKE that stays open -- into repeat_count on '
   'one physical row, instead of one row per 15s cycle. Off reproduces '
   'exact current behaviour: repeat_count stays 1 on every row, hurdle() '
   'reads an identical population to today. Measured 08-Sep-2026: 97.7% '
   'fewer physical rows, 0.00000 hurdle-bar delta, exact verdict-count '
   'reconciliation, on a 14-day live replay. See docs/FINDINGS.md. '
   'INTRADAY is deliberately excluded -- its own replay only reached 61.9% '
   'with a non-zero bar delta.',
   'Storage', 'allocation/allocator.py', 'bool', 'false', 'CRITICAL'),

  ('alloc_write_collapse_edge_threshold', '0.03',
   'Edge movement (R) that forces a new physical allocation_decisions row '
   'even when verdict and regime_bucket have not changed. Measured value -- '
   'see alloc_write_collapse_swing_enabled''s own description for the '
   'live-replay result this was chosen from.',
   'Storage', 'allocation/allocator.py', 'float', '0.03', 'CRITICAL'),

  ('alloc_write_collapse_heartbeat_s', '1800',
   'Seconds since a row was first written that force a new physical row '
   'even with no other change, so a candidate hovering unchanged for hours '
   'still contributes more than one point to the day''s population. '
   'Measured value -- see alloc_write_collapse_swing_enabled''s own '
   'description.',
   'Storage', 'allocation/allocator.py', 'int', '1800', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;
