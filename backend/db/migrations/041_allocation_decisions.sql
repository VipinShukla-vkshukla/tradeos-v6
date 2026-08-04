-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 041: the allocation record (Stage 10)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHAT MAKES PHASE 4 DIFFERENT IN KIND FROM PHASE 3
--
-- Phase 3 recorded what the system DID. This table records what it DECIDED,
-- which includes the fifty-odd proposals it turned down every day. That
-- distinction is the whole programme: "the allocator beat greedy" is a claim
-- about the trades it did NOT take, and it is unanswerable without a row per
-- refusal.
--
-- EVERY VERDICT, INCLUDING DECLINE. A table holding only TAKEs would reproduce
-- the selection bias the entire statistical framework exists to escape.
--
-- shadow DEFAULTS TRUE. A row written while shadow is true describes a decision
-- that changed nothing — the live path did whatever it would have done anyway.
-- That is the point: months of shadow rows are what the promotion gate is
-- denominated in, and the gate counts DISAGREEMENTS with the greedy baseline,
-- not sessions. Sessions of agreement carry no discriminating information.
--
-- EVERY INPUT IS STORED, NOT JUST THE ANSWER. §19: a decision that cannot be
-- re-derived is a defect. edge, its components, the hurdle AND the inputs that
-- produced the hurdle, the prior's sample size and whether it was below floor.
-- A verdict whose hurdle cannot be reconstructed is an opinion, not evidence.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.allocation_decisions (
    id            bigserial PRIMARY KEY,
    decided_at    timestamptz NOT NULL DEFAULT now(),
    trade_date    date        NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Kolkata')::date,

    symbol        text NOT NULL,
    framework     text NOT NULL,          -- SWING | INTRADAY
    product       text NOT NULL,          -- CNC | MIS
    source        text,                   -- engine or family

    verdict       text NOT NULL,          -- TAKE | DEFER | DECLINE
    reason        text,                   -- in words, for the operator

    entry         numeric,
    stop          numeric,
    target        numeric,
    quantity      integer,

    -- score components, so the number can be re-derived rather than trusted
    edge          numeric,
    e_r           numeric,
    cost_r        numeric,
    r_target      numeric,
    friction      numeric,
    hold_days     numeric,
    prior_n       integer,
    prior_below_floor boolean,

    hurdle        numeric,
    hurdle_inputs jsonb,
    native_rank   numeric,

    -- resolved afterwards by the outcome pass, so a decision can be scored
    outcome_r     numeric,
    outcome_note  text,

    shadow        boolean NOT NULL DEFAULT true,
    meta          jsonb
);

CREATE INDEX IF NOT EXISTS idx_alloc_decisions_date
    ON public.allocation_decisions (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_alloc_decisions_verdict
    ON public.allocation_decisions (verdict, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_alloc_decisions_symbol
    ON public.allocation_decisions (symbol, trade_date DESC);

COMMENT ON TABLE public.allocation_decisions IS
  'Every allocation verdict including DECLINE, with the inputs that produced '
  'it. The refusals are the point: the promotion gate is denominated in '
  'disagreements with the greedy baseline, which cannot be counted without a '
  'row per proposal the allocator turned down.';

COMMENT ON COLUMN public.allocation_decisions.shadow IS
  'True means this decision changed nothing and the live path proceeded as it '
  'always would. Defaults true, and stays true until the operator promotes the '
  'allocator per book on evidence they have read.';


-- ── Switches. Money moves behind two, and both default off. ────────────────
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('alloc_shadow_enabled', 'true',
   'Run the allocator and record its verdicts, changing nothing. This is the '
   'evidence-gathering mode and it is safe: the allocation package cannot '
   'import execution, so it has no path to an order however wrong it is.',
   'Master controls', 'allocation/allocator.py', 'bool', 'false', 'CRITICAL'),

  ('alloc_live_intraday', 'false',
   'Let the allocator decide which intraday setups are taken. OFF. Promote '
   'only after >=30 DISAGREEMENTS with the greedy baseline have been scored — '
   'not after N sessions, because sessions of agreement carry no information. '
   'Intraday is the paper book and therefore goes first.',
   'Master controls', 'intraday/engine.py', 'bool', 'false', 'CRITICAL'),

  ('alloc_live_swing', 'false',
   'Let the allocator decide which swing plans are taken. OFF, and last: this '
   'is the book holding real money. Requires the intraday promotion to have '
   'run >=5 clean sessions first.',
   'Master controls', 'control/candidate_monitor.py', 'bool', 'false', 'CRITICAL'),

  ('alloc_max_slots', '2',
   'Entries available per day, across both books. The scarcity term in the '
   'hurdle is computed against this.',
   'Master controls', 'allocation/hurdle.py', 'int', '2', 'CRITICAL'),

  ('alloc_time_weight', '0.6',
   'How much the bar rises with time remaining. Higher means more patience '
   'early in the session.',
   'Scoring', 'allocation/hurdle.py', 'float', '0.6', 'TUNING'),
  ('alloc_scarcity_weight', '0.5',
   'How much the bar rises as slots run out. The last slot is worth more than '
   'the first.',
   'Scoring', 'allocation/hurdle.py', 'float', '0.5', 'TUNING'),
  ('alloc_hurdle_percentile', '0.75',
   'The bar is this percentile of realised edge among resolved detections in '
   'the regime bucket: clear what the better quarter of arrivals delivered.',
   'Scoring', 'allocation/hurdle.py', 'float', '0.75', 'TUNING'),
  ('alloc_hurdle_min_sample', '40',
   'Observations needed before the empirical bar is trusted. Below it the '
   'cold-start value is used and shadow verdicts match current behaviour, '
   'which proves the plumbing before anything changes an opinion.',
   'Scoring', 'allocation/hurdle.py', 'int', '40', 'CRITICAL'),

  ('alloc_reserve_edge_multiple', '1.35',
   'How much better an untriggered swing plan must be before a slot is held '
   'for it. The swing book knows its whole field at 09:15; this is what that '
   'knowledge is worth.',
   'Scoring', 'allocation/policies.py', 'float', '1.35', 'TUNING'),
  ('alloc_reserve_min_p_trigger', '0.35',
   'Minimum probability a reserved plan actually triggers today. A reservation '
   'on a plan that will not trade is a slot the book forgot it had.',
   'Scoring', 'allocation/policies.py', 'float', '0.35', 'TUNING'),
  ('alloc_defer_max_drift_pct', '0.75',
   'How far price may move from where a proposal was deferred before it stops '
   'being the same proposal. Beyond this it is declined rather than carried.',
   'Scoring', 'allocation/allocator.py', 'float', '0.75', 'TUNING'),
  ('alloc_basket_recheck', 'true',
   'Recheck sector caps across the allocator OWN simultaneous selections. Two '
   'names can each pass individually and breach jointly; nothing checked that '
   'before, and the breach surfaced a cycle later as a position that should '
   'not exist.',
   'Master controls', 'allocation/allocator.py', 'bool', 'true', 'CRITICAL'),

  ('priors_min_sample_intraday', '30',
   'Detections needed before a per-engine prior is trusted. Below it the prior '
   'is NEUTRAL and flagged, never interpolated and never borrowed.',
   'Scoring', 'allocation/scoring.py', 'int', '30', 'CRITICAL'),
  ('priors_min_sample_swing', '30',
   'Resolved plans needed before a per-class swing prior is trusted.',
   'Scoring', 'allocation/scoring.py', 'int', '30', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Allocation record ready. Shadow is ON, both live switches OFF.';
  RAISE NOTICE '';
  RAISE NOTICE 'The allocator now records what it WOULD have done, changes nothing,';
  RAISE NOTICE 'and cannot reach an order path — allocation/ does not import';
  RAISE NOTICE 'execution/, and tools/health asserts that by inspection.';
  RAISE NOTICE '';
  RAISE NOTICE 'Promotion is denominated in DISAGREEMENTS, not sessions:';
  RAISE NOTICE '  >=30 scored disagreements with greedy  -> intraday paper';
  RAISE NOTICE '  >=5 clean intraday sessions            -> swing (real money)';
  RAISE NOTICE 'Expect months. Sessions where the two agree prove nothing.';
END $$;
