-- 086 — ORB's two documented, half-built gaps closed. 19-Aug-2026.
--
-- Closed after reading the engine's OWN docstring against established
-- Opening Range Breakout practice, per the operator's explicit request to
-- validate the strategy before enhancing the engine, rather than parameter-
-- tune outcome data. Two gaps named in the code's own comments and never
-- built; a third (regime awareness) named and deliberately NOT built here —
-- see intraday/strategies/orb.py's own module docstring for why.

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('orb_retest_confirmation_enabled', 'true',
        'The RETEST arm of ORB''s own documented "retest OR strength" rule, '
        'built 19-Aug-2026 - only the strength arm (break distance) ever '
        'existed; retest was named in the docstring and skipped on the belief '
        'it needed bar history this engine was not given, which was checked '
        'and found false (ctx.bars was always there). ALTERNATIVE, not a '
        'relaxation: a strength-confirmed break is unaffected either way; '
        'this only rescues a weak break that shows a genuine post-breakout '
        'retest-and-hold, which trading practice treats as the HIGHER-'
        'confidence signal, not a consolation prize. false restores the '
        'strength-only gate exactly. intraday/strategies/orb.py::'
        '_retest_and_held.',
        'Master controls', 'intraday/strategies/orb.py',
        'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, min_value, max_value, default_value,
                           risk_level)
VALUES ('orb_retest_tolerance_pct', '0.15',
        'How close price must come back to the opening-range high, as a '
        'percent of that level, to count as a genuine RETEST rather than a '
        'pullback that never approached the level at all. Read by '
        'intraday/strategies/orb.py::_retest_and_held.',
        'Master controls', 'intraday/strategies/orb.py',
        'float', 0, 2, '0.15', 'SAFE')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('orb_measured_move_target_enabled', 'true',
        'Targets the LARGER of ORB''s existing flat R-multiple '
        '(orb_target_r) and a measured-move projection of the opening '
        'range''s own height beyond the breakout - classic ORB practice, and '
        'matching squeeze.py''s (VCE) own measured-move target already in '
        'this codebase. max() only, so it can widen a target the flat '
        'multiple already set, never shrink one. In practice this rarely '
        'wins at the shipped orb_target_r=2.0 because ORB''s stop sits at '
        'the range low, so risk already exceeds range height by '
        'construction - documented, not a defect. false restores the flat '
        'multiple exactly. intraday/strategies/orb.py::evaluate.',
        'Master controls', 'intraday/strategies/orb.py',
        'bool', 'true', 'SAFE')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();
