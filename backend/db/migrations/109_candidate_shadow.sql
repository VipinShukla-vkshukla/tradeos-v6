-- 109_candidate_shadow.sql
-- 24-Aug-2026 (Stage D6, docs/TRADEOS_ROADMAP.md Track D, branch
-- feat/intraday-evolution)
--
-- New, isolated table only -- reads context the trusted loop already
-- builds, calls no code that writes anywhere real. A bug in the
-- templating logic can pollute only its own shadow log, never
-- intraday_setups, execution.paper_broker or allocation.allocator --
-- same structural guarantee migration 105 (Stage D3) gives its own
-- shadow table, for the identical reason.
CREATE TABLE IF NOT EXISTS intraday_candidate_shadow (
  id            BIGSERIAL PRIMARY KEY,
  trade_date    DATE NOT NULL,
  proposal_id   BIGINT NOT NULL,
  feature_name  TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  direction     TEXT NOT NULL,
  entry         NUMERIC,
  stop          NUMERIC,
  target        NUMERIC,
  confidence    NUMERIC,
  rationale     TEXT,
  detected_at   TIMESTAMPTZ NOT NULL,
  meta          JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- One row per (day, candidate, symbol) -- a candidate whose daily-bar
  -- condition stays true all session must not be re-logged every slow-
  -- timer tick as though it were a fresh detection. The exact "one setup
  -- counted eleven times" shape this project has paid for before
  -- (RNG's n=11; F-54/this branch's own same-day-calibration bug, caught
  -- and fixed live earlier this session).
  UNIQUE (trade_date, proposal_id, symbol)
);

CREATE INDEX IF NOT EXISTS intraday_candidate_shadow_proposal_idx
  ON intraday_candidate_shadow (proposal_id, trade_date);

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_candidate_shadow_enabled', 'false',
   'Stage D6: run tools.discover_engines Pass B candidates the operator '
   'has explicitly approved (status=APPROVED, via the EXISTING swing/'
   'brain/backtester_and_change_manager.py::approve_proposal() -- see '
   'tools.approve_candidate) as templated shadow detectors. Ships FALSE '
   '-- reads contexts the trusted loop already builds, writes ONLY to '
   'intraday_candidate_shadow, never intraday_setups, paper_broker or the '
   'allocator.',
   'Master controls', 'intraday/candidate_shadow.py', 'bool', 'false', 'LOW'),

  ('candidate_target_r', '2.0',
   'Stage D6: fixed R-multiple target for every templated candidate. '
   'Deliberately simple -- a hand-tuned engine''s target logic (e.g. '
   'GDB''s day-high-aware target) is exactly the kind of judgment a '
   'template cannot honestly claim to reuse; see intraday/candidate_'
   'template.py''s own docstring.',
   'Master controls', 'intraday/candidate_template.py', 'float', '2.0', 'LOW'),

  ('candidate_max_risk_pct', '1.10',
   'Stage D6: max_risk_pct passed to risk_from_structure() for every '
   'templated candidate -- same default GDB itself ships with '
   '(gdb_max_risk_pct).',
   'Master controls', 'intraday/candidate_template.py', 'float', '1.10', 'LOW'),

  ('candidate_stop_buffer_pct', '0.10',
   'Stage D6: buffer below the swing low the structural stop sits at, '
   'same default and same reasoning as GDB''s own gdb_stop_buffer_pct.',
   'Master controls', 'intraday/candidate_template.py', 'float', '0.10', 'LOW'),

  ('candidate_lookback_bars', '12',
   'Stage D6: how many recent minute bars a templated candidate scans '
   'for its VWAP-reclaim trigger -- same default as GDB''s own '
   'gdb_lookback_bars.',
   'Master controls', 'intraday/candidate_template.py', 'int', '12', 'LOW'),

  ('candidate_min_bars_below', '2',
   'Stage D6: minimum bars spent below VWAP before a reclaim counts as a '
   'real flush rather than a touch-and-go -- same default as GDB''s own '
   'gdb_min_bars_below.',
   'Master controls', 'intraday/candidate_template.py', 'int', '2', 'LOW')
ON CONFLICT (key) DO NOTHING;
