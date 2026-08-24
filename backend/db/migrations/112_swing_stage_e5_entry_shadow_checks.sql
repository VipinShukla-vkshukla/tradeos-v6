-- 112_swing_stage_e5_entry_shadow_checks.sql
-- 24-Aug-2026 (Track E, Stage E5, pieces 2-3, shadow-only)
--
-- Both switches ship OFF. F-75 quantified pieces 2 and 3 and found the
-- evidence too thin (n=16 closed positions, zero CAUTION-bucket rows) to
-- set a confident hard-refusal threshold without risking a bar no real
-- winner can clear — the exact "check that cannot PASS" failure mode
-- this project's own rules warn about. Rather than leave both blocked on
-- a sample that will not grow on its own, both ship as SHADOW checks in
-- analysis/entry_ranking.py::entry_refusals() — logged, never enforced —
-- so the next quantify pass has real accumulated data instead of the
-- same 16 rows.
--
-- entry_refuse_low_rr_retention / entry_rr_retention_floor: F-74's own
-- finding (HAL's R:R collapsed from 7.63-14.09 at the zone low to 1.17
-- at the actual fill) as a checkable predicate. A plan whose live R:R
-- has retained less than entry_rr_retention_floor of its zone-low value
-- is shadow-logged; armed, it becomes a real refusal.
--
-- entry_refuse_broken_trend: control.exit_rules.assess_trend() — already
-- fixed this session (F-75's weekly_structure vocabulary correction) —
-- had only ever been called on an ALREADY-HELD position. This calls it
-- on the CANDIDATE itself: a plan whose own trend evidence already reads
-- BROKEN, the same bar the exit-side rules trust to cut a losing
-- position, is shadow-logged before ever taking the entry.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('entry_refuse_low_rr_retention', 'false',
   'Refuse a plan whose live R:R has retained less than '
   'entry_rr_retention_floor of its zone-low value (stop/target fixed, '
   'price having run toward target before the trade is taken). Ships '
   'OFF; shadow-logged via analysis/entry_ranking.py::entry_refusals(). '
   'F-74/F-75.',
   'Entry gate', 'analysis/entry_ranking.py', 'bool', 'false', 'MEDIUM'),

  ('entry_rr_retention_floor', '0.20',
   'rr_live / rr_at_zone_low below this triggers the R:R-retention '
   'refusal (shadow while entry_refuse_low_rr_retention is off). '
   'Starting point, not a calibrated bar — n=16 at the time this shipped. '
   'Set to 0.20 rather than the F-74 sample''s exact worst/best split '
   'point specifically so HAL''s own anchor case (retention 0.153 at its '
   'real entry-day zone) actually lights up the shadow log rather than '
   'sitting just above a tighter floor — re-set once the shadow log has '
   'accumulated real data.',
   'Entry gate', 'analysis/entry_ranking.py', 'float', '0.20', 'LOW'),

  ('entry_refuse_broken_trend', 'false',
   'Refuse a plan whose own control.exit_rules.assess_trend() verdict '
   'already reads BROKEN (with real evidence) before the entry is even '
   'taken. Ships OFF; shadow-logged via entry_refusals(). F-75.',
   'Entry gate', 'analysis/entry_ranking.py', 'bool', 'false', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
