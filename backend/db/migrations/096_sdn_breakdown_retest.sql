-- 096_sdn_breakdown_retest.sql
-- 22-Aug-2026 (F-49)
--
-- BRKD (SDN's range-breakdown condition) now computes a retest-and-held
-- signal, the short mirror of ORB's own (F-37) -- but INFORMATIONAL ONLY,
-- per the operator's explicit instruction: never a second gate on top of
-- _range_breakdown's existing checks, only a data point stamped into
-- meta.retest_confirmed for allocation.policies._confirmation_key's
-- existing PRIORITY tie-break (F-48, alloc_intraday_confirmation_priority)
-- to read. No enable switch needed -- the stamp is inert by construction
-- unless that tie-break is armed, which it already is.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('intraday_short_breakdown_retest_tolerance_pct', '0.15',
        'How close price must come back to BRKD''s broken range-low, as a '
        'percent, to count as a retest (mirrors orb_retest_tolerance_pct). '
        'Feeds meta.retest_confirmed, which only ever influences ranking '
        'priority (F-48) -- never gates a BRKD setup on its own. '
        'intraday/strategies/short_distribution.py::_retest_and_held_short.',
        'Master controls', 'intraday/strategies/short_distribution.py',
        'float', '0.15', 'LOW')
ON CONFLICT (key) DO NOTHING;
