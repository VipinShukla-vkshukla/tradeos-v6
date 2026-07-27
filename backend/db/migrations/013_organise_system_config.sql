-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 013: give system_config a shape
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 207 flat key/value rows, 62 of them with no description at all. It works, but
-- nothing in the table tells you which keys matter, which are dead, what a safe
-- range is, or which subsystem owns one. Tuning a threshold means grepping the
-- codebase to find out what it does and what happens if you get it wrong.
--
-- ADDITIVE ONLY — NO CALLER CHANGES
-- ---------------------------------
-- config.cfg() runs `SELECT key, value`. Every column added here is invisible
-- to it, and no existing row's key or value is modified. Nothing in the
-- pipeline needs to know this migration happened; the Control Room can group
-- and validate, and everything else carries on reading two columns.
--
-- The categories come from the actual prefix distribution, not from an
-- idealised taxonomy: signal_* is 46 keys, screener_* 15, intraday_* 15, and so
-- on. Grouping by how the keys are really named means the mapping stays true as
-- keys are added, instead of drifting from a scheme nobody remembers.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.system_config
    ADD COLUMN IF NOT EXISTS category    text,
    ADD COLUMN IF NOT EXISTS subsystem   text,
    ADD COLUMN IF NOT EXISTS value_type  text,
    ADD COLUMN IF NOT EXISTS min_value   numeric,
    ADD COLUMN IF NOT EXISTS max_value   numeric,
    ADD COLUMN IF NOT EXISTS default_value text,
    ADD COLUMN IF NOT EXISTS risk_level  text,
    ADD COLUMN IF NOT EXISTS deprecated  boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.system_config.category   IS 'Grouping for the Control Room UI.';
COMMENT ON COLUMN public.system_config.subsystem  IS 'Which package owns this key — where to look when it misbehaves.';
COMMENT ON COLUMN public.system_config.value_type IS 'bool | int | float | string | json — lets the UI render the right control.';
COMMENT ON COLUMN public.system_config.risk_level IS
  'How much damage a wrong value does. SAFE: cosmetic or advisory. TUNING: '
  'changes what gets recommended. CRITICAL: changes how much money moves, or '
  'whether the system trades at all. The Control Room confirms twice on CRITICAL.';


-- ── Category, from the real prefix distribution ────────────────────────────
UPDATE public.system_config SET category = CASE
    WHEN key LIKE 'signal!_%'   ESCAPE '!' THEN 'Signal generation'
    WHEN key LIKE 'screener!_%' ESCAPE '!' THEN 'Screening'
    WHEN key LIKE 'intraday!_%' ESCAPE '!' THEN 'Intraday'
    WHEN key LIKE 'gtt!_%'      ESCAPE '!' THEN 'Intraday'
    WHEN key LIKE 'score!_%'    ESCAPE '!' THEN 'Scoring'
    WHEN key LIKE 'exit!_%'     ESCAPE '!' THEN 'Exit policy'
    WHEN key LIKE 'portfolio!_%' ESCAPE '!' THEN 'Portfolio & sizing'
    WHEN key LIKE 'capital!_%'  ESCAPE '!' THEN 'Portfolio & sizing'
    WHEN key LIKE 'risk!_%'     ESCAPE '!' THEN 'Portfolio & sizing'
    WHEN key LIKE 'position!_%' ESCAPE '!' THEN 'Positions'
    WHEN key LIKE 'brain!_%'    ESCAPE '!' THEN 'Brain / self-tuning'
    WHEN key LIKE 'calibration!_%' ESCAPE '!' THEN 'Brain / self-tuning'
    WHEN key LIKE 'lesson!_%'   ESCAPE '!' THEN 'Brain / self-tuning'
    WHEN key LIKE 'ai!_%'       ESCAPE '!' THEN 'AI / ML'
    WHEN key LIKE 'ml!_%'       ESCAPE '!' THEN 'AI / ML'
    WHEN key LIKE 'email!_%'    ESCAPE '!' THEN 'Alerting'
    WHEN key LIKE 'telegram%'              THEN 'Alerting'
    WHEN key LIKE 'discord%'               THEN 'Alerting'
    WHEN key LIKE 'alert!_%'    ESCAPE '!' THEN 'Alerting'
    WHEN key LIKE 'kite!_%'     ESCAPE '!' THEN 'Broker'
    WHEN key LIKE 'compute!_%'  ESCAPE '!' THEN 'Compute'
    WHEN key LIKE 'sector%' OR key LIKE 'industry!_%' ESCAPE '!' THEN 'Sector & industry'
    WHEN key LIKE 'fii!_%'      ESCAPE '!' THEN 'Flows'
    WHEN key LIKE 'perf!_%' OR key LIKE 'post!_%' ESCAPE '!' THEN 'Performance'
    WHEN key IN ('master_kill_switch','autonomy_phase','quality_gate_enabled',
                 'pipeline_evening_hour','block_new_entries')                THEN 'Master controls'
    ELSE 'Uncategorised'
END
WHERE category IS NULL;

-- ── Owning package ─────────────────────────────────────────────────────────
UPDATE public.system_config SET subsystem = CASE category
    WHEN 'Signal generation'   THEN 'signals/'
    WHEN 'Screening'           THEN 'signals/screen_stocks.py'
    WHEN 'Intraday'            THEN 'intraday/'
    WHEN 'Scoring'             THEN 'compute/compute_msl.py'
    WHEN 'Exit policy'         THEN 'control/position_lifecycle.py'
    WHEN 'Portfolio & sizing'  THEN 'analysis/portfolio_constraints.py'
    WHEN 'Positions'           THEN 'control/'
    WHEN 'Brain / self-tuning' THEN 'brain/'
    WHEN 'AI / ML'             THEN 'ai/'
    WHEN 'Alerting'            THEN 'alerts/send_alerts.py'
    WHEN 'Broker'              THEN 'kite/'
    WHEN 'Compute'             THEN 'compute/'
    WHEN 'Sector & industry'   THEN 'compute/'
    WHEN 'Flows'               THEN 'ingestion/'
    WHEN 'Performance'         THEN 'brain/performance_tracker.py'
    WHEN 'Master controls'     THEN 'config.py'
    ELSE NULL
END
WHERE subsystem IS NULL;

-- ── Value type, inferred from the value itself ─────────────────────────────
UPDATE public.system_config SET value_type = CASE
    WHEN lower(value) IN ('true','false')            THEN 'bool'
    WHEN value ~ '^-?[0-9]+$'                        THEN 'int'
    WHEN value ~ '^-?[0-9]*\.[0-9]+$'                THEN 'float'
    WHEN value ~ '^\s*[\{\[]'                        THEN 'json'
    ELSE 'string'
END
WHERE value_type IS NULL;

-- ── Risk level ─────────────────────────────────────────────────────────────
-- CRITICAL is reserved for keys that change how much money moves or whether the
-- system acts at all. Marking too many things critical is the same failure as
-- marking none: the distinction stops carrying information.
UPDATE public.system_config SET risk_level = CASE
    WHEN key IN ('master_kill_switch','autonomy_phase','intraday_autonomy_phase',
                 'intraday_orders_enabled','intraday_auto_exit','intraday_gtt_enabled',
                 'intraday_max_order_value','intraday_max_orders_per_day',
                 'intraday_max_notional_per_day','block_new_entries')  THEN 'CRITICAL'
    WHEN category IN ('Portfolio & sizing','Exit policy','Positions','Broker')  THEN 'CRITICAL'
    WHEN category IN ('Signal generation','Screening','Scoring','Intraday',
                      'AI / ML','Brain / self-tuning')                 THEN 'TUNING'
    ELSE 'SAFE'
END
WHERE risk_level IS NULL;

-- Record today's value as the baseline, so the Control Room can show what
-- changed and offer a revert. Without it, a bad tune is only recoverable if you
-- happen to remember the number that was there before.
UPDATE public.system_config SET default_value = value WHERE default_value IS NULL;


-- ── Bounds for the keys where a wrong value does real damage ───────────────
-- Only where a defensible range exists. Inventing bounds for everything would
-- make the ones that matter unremarkable.
UPDATE public.system_config SET min_value = 0,    max_value = 100   WHERE key = 'risk_pct_per_trade';
UPDATE public.system_config SET min_value = 0.5,  max_value = 10    WHERE key = 'exit_target_r';
UPDATE public.system_config SET min_value = 0.25, max_value = 5     WHERE key = 'exit_partial_book_r';
UPDATE public.system_config SET min_value = 0,    max_value = 100   WHERE key = 'exit_partial_book_pct';
UPDATE public.system_config SET min_value = 0.25, max_value = 6     WHERE key = 'exit_trail_r';
UPDATE public.system_config SET min_value = 0.5,  max_value = 8     WHERE key = 'exit_trail_after_r';
UPDATE public.system_config SET min_value = 1,    max_value = 90    WHERE key = 'exit_time_stop_days';
UPDATE public.system_config SET min_value = 1,    max_value = 50    WHERE key = 'intraday_max_orders_per_day';
UPDATE public.system_config SET min_value = 100,  max_value = 1000000 WHERE key = 'intraday_max_order_value';
UPDATE public.system_config SET min_value = 100,  max_value = 5000000 WHERE key = 'intraday_max_notional_per_day';
UPDATE public.system_config SET min_value = 2,    max_value = 3     WHERE key = 'intraday_autonomy_phase';
UPDATE public.system_config SET min_value = 5,    max_value = 3600  WHERE key = 'intraday_eval_interval_s';
UPDATE public.system_config SET min_value = 0,    max_value = 100   WHERE key = 'capital_tolerance_pct';
UPDATE public.system_config SET min_value = 1,    max_value = 20    WHERE key = 'portfolio_max_per_industry';


-- ── A description for every key that lacks one ─────────────────────────────
-- Generic, but it names the owning package, which is the single most useful
-- thing to know when a key misbehaves.
UPDATE public.system_config
   SET description = 'Owned by ' || COALESCE(subsystem, 'unknown') ||
                     '. No description recorded — add one when you next touch it.'
 WHERE description IS NULL OR btrim(description) = '';


-- ── Read model for the Control Room ────────────────────────────────────────
CREATE OR REPLACE VIEW public.v_system_config_grouped AS
SELECT category, subsystem, risk_level, key, value, default_value,
       value_type, min_value, max_value, description, deprecated,
       (value IS DISTINCT FROM default_value) AS changed_from_default,
       updated_at
  FROM public.system_config
 WHERE NOT deprecated
 ORDER BY
   CASE category
     WHEN 'Master controls'    THEN 0
     WHEN 'Portfolio & sizing' THEN 1
     WHEN 'Exit policy'        THEN 2
     WHEN 'Intraday'           THEN 3
     WHEN 'Signal generation'  THEN 4
     ELSE 9
   END,
   category, key;

COMMENT ON VIEW public.v_system_config_grouped IS
  'system_config for the Control Room: grouped, typed, bounded, and flagging '
  'anything that differs from its recorded baseline. Additive — cfg() still '
  'reads the base table with SELECT key, value.';


DO $$
DECLARE n int; uncat int; crit int;
BEGIN
  SELECT count(*) INTO n     FROM public.system_config;
  SELECT count(*) INTO uncat FROM public.system_config WHERE category = 'Uncategorised';
  SELECT count(*) INTO crit  FROM public.system_config WHERE risk_level = 'CRITICAL';
  RAISE NOTICE '% keys categorised | % uncategorised | % CRITICAL', n, uncat, crit;
  RAISE NOTICE 'cfg() is unaffected — it selects key,value and never sees these columns.';
END $$;
