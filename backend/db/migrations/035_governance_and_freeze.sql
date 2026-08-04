-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 035: one governance door, and a clock (Stage 7)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY THIS STAGE EXISTS
-- ---------------------
-- Adaptation was running roughly an order of magnitude faster than information
-- arrives. This account closes 5-10 observations a week; the brain ran weekly
-- and could move a threshold on its own authority. Fitting weekly to 5-10
-- observations does not learn — it tracks the last fortnight, and meets every
-- new regime freshly mis-tuned to the one before it.
--
-- THE SECOND DOOR IS CLOSED IN CODE, NOT IN CONFIGURATION
-- --------------------------------------------------------
-- brain_auto_apply_policy held {"THRESHOLD_CHANGE": {"mode": "auto",
-- "min_confidence": 0.75, "min_wr_delta_pp": 5.0}} in the live database, so a
-- threshold could move with nobody reading it. evaluate_auto_apply() now
-- returns False before it looks at anything.
--
-- Deliberately not "set every mode to review". A JSON edit that re-opens the
-- door would look like tuning rather than like a governance change, and the
-- whole point is that the authority is gone rather than currently unused. The
-- key below is marked deprecated and kept so history stays readable; nothing
-- reads it any more.
--
-- FREEZE WINDOWS: PROPOSALS ACCUMULATE, DECISIONS HAPPEN IN BATCH
-- ---------------------------------------------------------------
-- A parameter is frozen for a quarter. Proposals still accumulate — the brain
-- keeps running and keeps writing to brain_proposals — they simply are not
-- decided until the boundary, and then all together.
--
-- This will feel like inaction during a drawdown. That is the intended
-- behaviour and the readiness review names it the assumption most likely to
-- fail in practice (H4), because it is behavioural rather than technical.
--
-- OUT-OF-SAMPLE CONFIRMATION IS A DATE, NOT AN INTENTION
-- ------------------------------------------------------
-- Fit in quarter N, confirm in N+1, act in N+2. Recorded per proposal so
-- "confirmed" is a fact with a window attached rather than a memory.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── The freeze calendar ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.freeze_calendar (
    quarter        text PRIMARY KEY,          -- '2026-Q3'
    starts_on      date NOT NULL,
    ends_on        date NOT NULL,
    decisions_due  date NOT NULL,             -- the batch review date
    notes          text,
    reviewed_at    timestamptz
);

COMMENT ON TABLE public.freeze_calendar IS
  'When parameters may move. Inside a window they may not, however good the '
  'evidence looks — the evidence is exactly what is unreliable at a 5-10 '
  'observation/week arrival rate. Proposals accumulate and are decided in one '
  'batch at decisions_due.';

INSERT INTO public.freeze_calendar (quarter, starts_on, ends_on, decisions_due, notes)
VALUES
  ('2026-Q3', '2026-07-01', '2026-09-30', '2026-10-01',
   'First frozen quarter. Also the shadow quarter for VCE and RNG (migration 034) '
   'and the first quarter in which signal_output_daily outcomes are resolved.'),
  ('2026-Q4', '2026-10-01', '2026-12-31', '2027-01-01',
   'Confirmation window for anything fitted in Q3. Nothing fitted in Q3 may be '
   'acted on before 2027-Q1.'),
  ('2027-Q1', '2027-01-01', '2027-03-31', '2027-04-01',
   'First quarter in which a Q3-fitted, Q4-confirmed change may be applied.')
ON CONFLICT (quarter) DO NOTHING;


-- ── Out-of-sample provenance, per proposal ─────────────────────────────────
ALTER TABLE public.brain_proposals
    ADD COLUMN IF NOT EXISTS fitted_quarter    text,
    ADD COLUMN IF NOT EXISTS confirmed_quarter text,
    ADD COLUMN IF NOT EXISTS actionable_from   date;

COMMENT ON COLUMN public.brain_proposals.fitted_quarter IS
  'The window whose data produced this proposal. A proposal fitted and applied '
  'in the same window is in-sample, and in-sample evidence at this sample rate '
  'is indistinguishable from noise.';

COMMENT ON COLUMN public.brain_proposals.confirmed_quarter IS
  'The LATER window in which the relationship held out of sample. NULL means '
  'unconfirmed, which means not actionable regardless of how strong the '
  'original fit was.';


INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('governance_freeze_enabled', 'true',
   'Whether parameter changes are refused inside a freeze window. This is the '
   'primary defence against fitting noise at 5-10 closed observations a week.',
   'Brain / self-tuning', 'swing/brain/backtester_and_change_manager.py',
   'bool', 'false', 'CRITICAL'),

  ('governance_require_oos', 'true',
   'Whether a proposal must carry a confirmed_quarter later than its '
   'fitted_quarter before it can be applied. Fit in N, confirm in N+1, act in '
   'N+2.',
   'Brain / self-tuning', 'swing/brain/backtester_and_change_manager.py',
   'bool', 'false', 'CRITICAL'),

  ('rank_weight_tier', '0',
   'Weight of the AI tier in entry ranking. ZERO from 04-Aug-2026: the '
   'conviction layer is annotation until tier-by-tier forward returns exist '
   'from the unbiased record. An unmeasured component at the top of the '
   'decision stack is unpriced risk.',
   'Scoring', 'analysis/entry_ranking.py', 'float', '0', 'CRITICAL'),

  ('rank_weight_conviction', '0',
   'Weight of AI conviction in entry ranking. ZERO from 04-Aug-2026, same '
   'reason as rank_weight_tier. The arithmetic is preserved so restoring it is '
   'a config change and historical scores stay reconstructable.',
   'Scoring', 'analysis/entry_ranking.py', 'float', '0', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


-- Retire the second door's key. Kept, not deleted — nothing in this repository
-- is deleted — but marked so nobody mistakes it for live policy.
UPDATE public.system_config
   SET deprecated  = true,
       description = 'RETIRED 04-Aug-2026 (Stage 7). Auto-apply authority was '
                     'removed in code; evaluate_auto_apply() refuses before '
                     'reading this. Kept so historical decisions stay readable. '
                     'Editing it has no effect. || ' || coalesce(description, '')
 WHERE key = 'brain_auto_apply_policy';


DO $$
DECLARE cur text;
BEGIN
  SELECT quarter INTO cur FROM public.freeze_calendar
   WHERE CURRENT_DATE BETWEEN starts_on AND ends_on;

  RAISE NOTICE '';
  RAISE NOTICE 'One governance door. Auto-apply is removed in code, not toggled off.';
  RAISE NOTICE 'Conviction is annotation: rank_weight_tier and rank_weight_conviction = 0.';
  RAISE NOTICE '';
  RAISE NOTICE 'Current freeze window: %', coalesce(cur, 'NONE — add one to freeze_calendar');
  RAISE NOTICE 'Proposals accumulate inside it and are decided in one batch at the boundary.';
  RAISE NOTICE '';
  RAISE NOTICE 'This will feel like inaction during a drawdown. That is the point.';
END $$;
