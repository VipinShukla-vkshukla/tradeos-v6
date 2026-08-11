-- 11-Aug-2026. `priors_intraday_dedup` was read by allocation/scoring.py via
-- cfg_bool("priors_intraday_dedup", True) but registered NOWHERE -- not in
-- migration 057 that shipped the dedup, not in system_config, not in the
-- 124 CRITICAL keys tools/validate_config.py checks wiring for. It is the
-- switch controlling whether every intraday prior in the system is built
-- from distinct opportunities or from raw detection rows, and the only place
-- its value existed was a default argument in a function call.
--
-- That is this project's named dominant failure mode: a switch nobody
-- registered, defaulting ON, invisible to every tool built to make config
-- auditable. It also made the associated defect harder to see -- with the
-- key absent from system_config there was nothing to grep, nothing in the
-- dashboard, and no row to compare against the code's default.
--
-- Registering it changes no behaviour: true is already what the code
-- defaults to. It makes the switch visible, gives validate_config something
-- to check, and makes turning the dedup OFF (to reproduce a pre-057 prior)
-- an ordinary config change rather than a code edit.
--
-- See migration 057, which shipped the dedup this key controls, and
-- backend/tests/test_gated_priors.py, whose _FakeQuery now honours the
-- select string so the dedup's key columns cannot silently go unfetched.

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('priors_intraday_dedup', 'true',
   'Collapse intraday_setups rows to one observation per (symbol, engine, '
   'trade_date) before building a prior. A setup that lingers across many 15s '
   'cycles is written once per cycle, so without this one opportunity votes '
   'dozens of times and can flip an engine''s measured mean R on its own. '
   'ON since migration 057. Set false only to reconstruct a pre-057 prior for '
   'comparison -- see allocation/scoring.py::_intraday_priors_from_rows.',
   'Learning', 'allocation/scoring.py', 'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
