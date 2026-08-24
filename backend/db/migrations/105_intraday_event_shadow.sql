-- 105_intraday_event_shadow.sql
-- 24-Aug-2026 (Stage D3, docs/TRADEOS_ROADMAP.md Track D, branch
-- feat/intraday-event-core)
--
-- New, parallel orchestration only -- reads the same live ticks, calls the
-- SAME existing intraday.strategies.registry.evaluate_all() the polling
-- loop already trusts, decides nothing that writes anywhere the trusted
-- loop or the learning loop reads. This table exists SPECIFICALLY so that
-- guarantee is structural, not a discipline someone has to remember: it
-- is a completely separate table from intraday_setups, never read by
-- allocation/scoring.py, control/position_lifecycle.py, execution/
-- paper_broker.py or anything else that can move a position or price a
-- future proposal. A bug in this brand-new orchestration layer can
-- pollute only its OWN shadow log, never the tables four separate past
-- cleanups (F-33, F-39, F-42, F-53) paid to keep clean.
CREATE TABLE IF NOT EXISTS intraday_event_shadow (
  id           BIGSERIAL PRIMARY KEY,
  trade_date   DATE NOT NULL,
  symbol       TEXT NOT NULL,
  strategy     TEXT NOT NULL,
  sub_engine   TEXT,
  direction    TEXT,
  entry        NUMERIC,
  stop         NUMERIC,
  target       NUMERIC,
  confidence   NUMERIC,
  rationale    TEXT,
  detected_at  TIMESTAMPTZ NOT NULL,
  meta         JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS intraday_event_shadow_symbol_date_idx
  ON intraday_event_shadow (symbol, trade_date);

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_event_core_enabled', 'false',
   'Stage D3: the tick-triggered shadow core. Ships FALSE -- reads ticks '
   'and re-runs the same detection engines the trusted polling loop already '
   'calls, writes ONLY to intraday_event_shadow, never intraday_setups, '
   'paper_broker, or the allocator. Decides nothing that writes anywhere '
   'the trusted loop reads.',
   'Master controls', 'intraday/event_core.py', 'bool', 'false', 'LOW'),

  ('intraday_event_core_interval_s', '2',
   'Stage D3: how often the main daemon loop checks for dirty (meaningfully '
   'moved) symbols and re-runs detection on them. Deliberately far tighter '
   'than intraday_eval_interval_s (15s default) -- the whole point of this '
   'stage is measuring how many seconds sooner a tick-triggered check can '
   'react versus the existing polling cadence.',
   'Master controls', 'intraday/event_core.py', 'int', '2', 'LOW'),

  ('intraday_event_core_dirty_pct', '0.05',
   'Stage D3: how much a symbol''s price must move (percent, since '
   'event_core last looked at it) before it is marked dirty and worth a '
   'fresh detection pass. Too low and ordinary tick noise re-triggers '
   'evaluation constantly; too high and the shadow core misses genuine '
   'moves. Checked on the websocket thread itself, so kept a cheap plain '
   'config lookup, not a database read.',
   'Master controls', 'intraday/price_feed.py', 'float', '0.05', 'LOW')
ON CONFLICT (key) DO NOTHING;
