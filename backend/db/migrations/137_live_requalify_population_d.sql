-- Population D: EQ names that pass every static gate but were outranked
-- into the bench cut by build_universe()'s yesterday-based score.
-- Confirmed live 09/10-Sep-2026 (docs/FINDINGS.md) as why GRAPHITE, JSL,
-- SARDAEN, PTCIL, PPLPHARMA were invisible to IGN all session despite
-- qualifying outright. Fully vetted like the bench itself (unlike A/B/C),
-- so this switch ships armed True.
--
-- ON CONFLICT DO NOTHING so a later operator retune is never silently
-- re-armed by a re-run (matches migration 132/133's IGN-era keys).
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_live_requalify_unranked_enabled', 'true',
   'Stage D2 Population D: admit EQ names that pass every static '
   'universe gate (scanner._qualifies()) but were outranked out of '
   'today''s bench by build_universe()''s yesterday-based score. Fully '
   'vetted like the bench itself, so armed True by default -- see the '
   '09/10-Sep-2026 FINDINGS.md entry on GRAPHITE/JSL/SARDAEN/PTCIL/'
   'PPLPHARMA. scanner.unranked_qualifying_candidates(), wired into '
   'IntradayEngine.live_requalify_universe() alongside Population A/B/C, '
   'each gated on its own independent switch.',
   'Master controls', 'intraday/scanner.py', 'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
