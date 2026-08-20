-- 089_hurdle_since_floor.sql
-- 20-Aug-2026 (F-42)
--
-- `alloc_hurdle_since` — the same hard-floor-date contract
-- `priors_intraday_since` already applies to the per-ENGINE prior, now
-- applied to the BAR's own arrival population.
--
-- allocation/hurdle.py::_empirical_base builds the bar from
-- `allocation_decisions.edge`, a column COMPUTED at write time using
-- whatever engine prior was in force that cycle. It inherited the exact
-- contamination priors_intraday_since exists to fix (F-33's 18-Aug
-- stop-clamping change, the 20-Aug sub_engine/meta-encoding fixes) but had
-- no floor of its own — only a 90-day ROLLING window
-- (alloc_hurdle_lookback_days), which reaches back to 22-May-2026 today and
-- swallows every one of those fixes whole.
--
-- UNSET DELIBERATELY on ship, same posture as priors_intraday_since — but
-- armed to 2026-08-20 in this same migration. Unlike the per-engine prior
-- (which needs ~30 TAKEN rows per engine and can take days), the bar's own
-- population clears its 40-row floor (alloc_hurdle_min_sample) from a
-- single trading day's volume (~1000+ INTRADAY rows/session) — so arming
-- this the same day it ships costs at most the remainder of today's
-- already-closed session, not a multi-day blind spot.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('alloc_hurdle_since',
        '2026-08-20',
        'Hard floor date (YYYY-MM-DD) for the BAR''s own arrival population '
        '(allocation_decisions.edge), combined with alloc_hurdle_lookback_days '
        'by taking the LATER of the two. Empty = no floor, the old '
        'rolling-window-only behaviour. Mirrors priors_intraday_since''s '
        'contract for the per-engine prior; exists because allocation_decisions.'
        'edge is computed from whatever engine prior was in force at write '
        'time, so it inherits the same pre/post-fix contamination the prior '
        'floor was built to stop. allocation/hurdle.py::_empirical_base.',
        'Master controls', 'allocation/hurdle.py',
        'string', '', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
