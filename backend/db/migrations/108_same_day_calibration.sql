-- 108_same_day_calibration.sql
-- 24-Aug-2026 (Stage D5, docs/TRADEOS_ROADMAP.md Track D, branch
-- feat/intraday-regression-shadow)
--
-- Stage 1 of D5 — CALIBRATION ONLY. tools/same_day_calibration.py writes
-- here; nothing else reads this table. No proposal, no config change, no
-- effect on any live decision — this is the log the roadmap's own Stage 1
-- describes: "the model computes predictions against already-resolved
-- history and logs its own predicted-vs-actual accuracy over time...
-- nothing here is visible outside this pipeline yet."
CREATE TABLE IF NOT EXISTS intraday_same_day_calibration (
  id                   BIGSERIAL PRIMARY KEY,
  trade_date           DATE NOT NULL,
  engine               TEXT NOT NULL,
  historical_n         INTEGER NOT NULL,
  historical_hit_rate  NUMERIC,
  today_wins           INTEGER NOT NULL,
  today_n              INTEGER NOT NULL,
  today_hit_rate       NUMERIC NOT NULL,
  today_mean_r         NUMERIC NOT NULL,
  flagged              BOOLEAN NOT NULL,
  multiplier           NUMERIC NOT NULL,
  reason               TEXT,
  evidence             JSONB,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (trade_date, engine)
);

CREATE INDEX IF NOT EXISTS intraday_same_day_calibration_flagged_idx
  ON intraday_same_day_calibration (flagged);

-- Two separate switches, same "capture vs. act" split every Track D stage
-- this session has used (D3's intraday_event_core_enabled, D4's
-- intraday_depth_mode_enabled / overlay_depth_enabled). Neither exists
-- yet for D5 — same_day_fit_multiplier() is called ONLY by tools/
-- same_day_calibration.py in this stage, never from the live entry path
-- (that is Stage 3, "armed", explicitly gated on Stage 1's own calibration
-- clearing a stated accuracy bar first) — but the weight is defined now,
-- at 0.0, so the function's own "weight <= 0 returns a no-op" contract is
-- real and testable today rather than assumed for a future stage.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_same_day_fit_weight', '0.0',
   'Stage D5 Stage 1 (calibration only): weight on the same-day self-'
   'monitor dampener. 0.0 makes same_day_fit_multiplier() an exact no-op. '
   'NOT yet wired into any live sizing decision -- only tools/same_day_'
   'calibration.py calls this function in this stage. Arming it for live '
   'use is Stage 3, gated on Stage 1''s own calibration log meeting a '
   'stated accuracy bar first.',
   'Master controls', 'allocation/scoring.py', 'float', '0.0', 'LOW'),

  ('intraday_same_day_fit_min_n', '5',
   'Stage D5: minimum TAKEN-and-resolved trades one engine must have on '
   'one day before the same-day monitor will test anything. Below this, '
   'a binomial p-value is real arithmetic answering a question nobody '
   'should trust -- same floor logic as priors_min_sample_intraday, at a '
   'much smaller n because this tests one DAY, not a multi-day prior.',
   'Master controls', 'allocation/scoring.py', 'int', '5', 'LOW'),

  ('intraday_same_day_fit_alpha', '0.05',
   'Stage D5: the one-sided binomial test''s significance threshold. '
   'Standard default, not tuned against any TradeOS-specific evidence yet '
   '-- there is none, which is exactly what this stage''s calibration log '
   'is for.',
   'Master controls', 'allocation/scoring.py', 'float', '0.05', 'LOW'),

  ('intraday_same_day_fit_max_dampen', '0.30',
   'Stage D5: the largest size cut the same-day monitor may ever apply, '
   'reached only at weight 1.0 on a day the binomial test calls a clear '
   'outlier. A DAMPENER ONLY -- the multiplier this produces is always '
   '<= 1.0, never a boost; see same_day_fit_multiplier()''s own docstring '
   'for why a same-session-good outlier is deliberately not acted on.',
   'Master controls', 'allocation/scoring.py', 'float', '0.30', 'LOW')
ON CONFLICT (key) DO NOTHING;
