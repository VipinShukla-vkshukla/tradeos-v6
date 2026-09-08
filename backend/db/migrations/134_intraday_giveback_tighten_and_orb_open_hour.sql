-- 09-Sep-2026. Acting on the "Why intraday trades close at nominal profit"
-- diagnostic (docs/FINDINGS.md, 08-Sep-2026, section 2) — three of its four
-- concrete recommendations, the ones the evidence actually supported acting
-- on now (VWR's confidence decomposition and the ORB open-hour restriction
-- are code changes, shipped alongside this migration; the fourth,
-- decomposing VWR's volume_ratio component, stayed unchanged — its own
-- decomposition was non-monotonic and thinner, not safe to encode yet).
--
-- Re-measured live before writing this migration (49 closed INTRADAY
-- positions that reached >=0.5R MFE): kept-fraction median 0.509, average
-- 0.447, p25 only 0.154, p75 0.720. The guard's own 50% tolerance is
-- barely better than the MEDIAN outcome, and the bottom quartile gives
-- back far more than the guard should allow — a separate, deeper gap
-- (single-share positions structurally cannot reach BOOK_PARTIAL's
-- move-to-breakeven rung, so the ORIGINAL stop, not the giveback guard,
-- is what still cuts them) named here but NOT fixed by this migration —
-- see docs/FINDINGS.md, 09-Sep-2026, for the full trace.
--
-- Tightened 50 -> 30 (locks in at least 70% of peak once giveback_min_r is
-- reached, instead of 50%). Directionally supported by the same evidence
-- migration 059 used to arm the guard in the first place; not re-derived
-- from a fresh calibration study of its own, so treat this as a first
-- tightening pass, not a final number.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_giveback_pct', '30.0',
   'Tightened from 50.0, 09-Sep-2026 — re-measured kept-fraction on 49 '
   'closed INTRADAY positions reaching >=0.5R MFE: median 0.509, average '
   '0.447, p25 0.154 -- the 50% tolerance was barely better than the '
   'median outcome. 30 locks in at least 70% of peak once giveback_min_r '
   'is reached. See docs/FINDINGS.md, 09-Sep-2026 ("Why intraday trades '
   'close at nominal profit", follow-on fix) and migration 059 for the '
   'original calibration this refines.',
   'Intraday exits', 'intraday/exit_policy.py', 'float', '0.0', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
  description = EXCLUDED.description;

-- ORB open-hour restriction (intraday/strategies/orb.py) needs no new
-- system_config row -- cfg_float("orb_min_minutes_since_open", 45.0) falls
-- back to the Python-side default, same as every other orb_*/vwr_* tunable
-- with no row of its own. 45 minutes after the 09:15 open is 10:00, the
-- exact OPEN/MID split _hour_bucket() (tools/feature_edge_study.py) found
-- ORB inverted across (0% win, n=210, vs 18%).
