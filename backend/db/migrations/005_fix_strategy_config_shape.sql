-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 005: repair strategy_config.params shape
-- ═══════════════════════════════════════════════════════════════════════════
--
-- RUN THIS IMMEDIATELY AFTER 004. Migration 004 corrupted two rows.
--
-- WHAT WENT WRONG
--   004 did:  UPDATE strategy_config SET params = params || '{"max_sector_rank": 8}'::jsonb
--
--   That assumed params was a jsonb OBJECT. It is not — every row holds a
--   double-encoded jsonb STRING whose contents are JSON text:
--       params = "{\"max_sector_rank\": 4.0, \"min_monthly_rsi\": 58.0, ...}"
--
--   In Postgres, `jsonb_string || jsonb_object` does not merge; it builds an
--   ARRAY. So CTL and SBS became:
--       ["{\"max_sector_rank\": 4.0, ...}", {"max_sector_rank": 8}]
--
--   Both consumers then break, because a list has no .get():
--     screen_stocks._parse()      -> json.loads only when isinstance(str);
--                                    a list passes through, then
--                                    cfg_ctl.get("max_sector_rank") raises
--                                    AttributeError
--     generate_signals            -> same pattern, same failure
--   Result: run_ctl / run_sbs raise, and both are fatal steps.
--
-- WHAT THIS FIXES
--   1. Unwraps the arrays 004 created, merging the patch into the real object.
--   2. Normalises EVERY row from double-encoded string to a genuine jsonb
--      object. That double-encoding is a pre-existing latent bug — the Python
--      side papered over it with `json.loads(v) if isinstance(v, str)` in two
--      separate places. With params stored properly, both those shims become
--      no-ops rather than load-bearing.
--
-- Idempotent: re-running is a no-op once every row is a jsonb object.
-- ═══════════════════════════════════════════════════════════════════════════

UPDATE public.strategy_config
   SET params = CASE
         -- Array: element 0 is the original (a jsonb string holding JSON text),
         -- element 1 is the patch object 004 tried to merge.
         WHEN jsonb_typeof(params) = 'array'
              THEN (params ->> 0)::jsonb || (params -> 1)
         -- Plain double-encoded string: unwrap to a real object.
         WHEN jsonb_typeof(params) = 'string'
              THEN (params #>> '{}')::jsonb
         ELSE params
       END,
       updated_at = now()
 WHERE jsonb_typeof(params) IN ('array', 'string');

-- Re-apply the widened gates now that params is a genuine object, so the
-- intent of 004 survives regardless of which state a row was left in.
UPDATE public.strategy_config
   SET params = params || '{"max_sector_rank": 8}'::jsonb, updated_at = now()
 WHERE strategy = 'CTL' AND jsonb_typeof(params) = 'object';

UPDATE public.strategy_config
   SET params = params || '{"max_sector_rank": 10}'::jsonb, updated_at = now()
 WHERE strategy = 'SBS' AND jsonb_typeof(params) = 'object';

-- Guard: params must be a JSON object from here on. Prevents any future
-- write from silently reintroducing the string or array form.
ALTER TABLE public.strategy_config
    DROP CONSTRAINT IF EXISTS strategy_config_params_is_object;
ALTER TABLE public.strategy_config
    ADD CONSTRAINT strategy_config_params_is_object
    CHECK (jsonb_typeof(params) = 'object');

COMMENT ON COLUMN public.strategy_config.params IS
  'Strategy parameters as a JSON OBJECT. Rows were previously stored as a '
  'double-encoded jsonb string, which made `params || patch` produce an array '
  'instead of merging (see migration 005). The CHECK constraint now enforces '
  'the object form.';

-- Verification — every row should read object / 8 / 10.
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT strategy, jsonb_typeof(params) AS t,
                  params ->> 'max_sector_rank' AS msr
             FROM public.strategy_config ORDER BY strategy
  LOOP
    RAISE NOTICE 'strategy_config % : type=% max_sector_rank=%', r.strategy, r.t, r.msr;
  END LOOP;
END $$;
