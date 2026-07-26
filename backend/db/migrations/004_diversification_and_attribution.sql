-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 004: diversification, gate semantics, attribution
-- ═══════════════════════════════════════════════════════════════════════════

-- ── PART 1: sector influence ───────────────────────────────────────────────
--
-- strategy_config PINS the engine sector gates, so widening the Python
-- defaults alone changes nothing: cfg_ctl.get("max_sector_rank", 8) returns
-- the stored 4.0. These are the values that actually bind.
--
-- Why widen. Sector rank was applied five times to the same stock (engine hard
-- gate, in-engine linear bonus, final_score sector band, validity_score, and
-- generate_signals' industry bonus), compounding into 76% of master_shortlist
-- sitting in two sectors on 2026-07-24, with all 11 entry signals from those
-- same two. The in-engine bonus is now a single bounded function
-- (screener_sector_bonus_max, default 8 pts against ~120-point engine scores),
-- so the gate no longer needs to do double duty as a ranking device.
--
-- Migration 001 also made the ranking trustworthy: sub-scale sectors are
-- pushed below the qualified set, so a rank<=4 gate now discards ~80% of a
-- legitimately-ranked market rather than filtering noise.
UPDATE public.strategy_config
   SET params = params || '{"max_sector_rank": 8}'::jsonb,
       updated_at = now()
 WHERE strategy = 'CTL';

UPDATE public.strategy_config
   SET params = params || '{"max_sector_rank": 10}'::jsonb,
       updated_at = now()
 WHERE strategy = 'SBS';


INSERT INTO public.system_config (key, value, description) VALUES
  -- Sector influence, now applied at exactly one point per stage
  ('screener_sector_bonus_max', '8',
   'Maximum points a top-ranked sector adds to an engine score. Engine scores top out near 120, so this is a nudge. Replaces the per-engine linear bonuses (CTL gave up to +24, SBS +28) that double-counted a gate the stock had already passed.'),
  ('screener_sector_gate_ctl', '8',
   'Sector-rank ceiling for CTL. Overridden by strategy_config.CTL.max_sector_rank when present.'),
  ('screener_sector_gate_sbs', '10',  'Sector-rank ceiling for SBS.'),
  ('screener_sector_gate_vbd', '12',  'Sector-rank ceiling for VBD.'),
  ('screener_sector_gate_iad', '14',  'Sector-rank ceiling for IAD. Widest — institutional accumulation is most valuable in sectors NOT yet leading.'),
  ('screener_sector_gate_rsb', '12',  'Sector-rank ceiling for RSB.'),
  ('screener_sector_gate_mom', '12',  'Sector-rank ceiling for MOM.'),
  ('screener_sector_gate_rvs', '12',  'Sector-rank ceiling for RVS. Auto-relaxed by +2 in RECOVERING / RISK OFF.'),
  ('screener_min_sectors', '5',
   'Shortlist FLOOR: guarantee at least this many distinct sectors appear. max_per_sector is only a ceiling and cannot pull an under-represented sector in once leadership has filled the list.'),

  -- Portfolio construction. Concentration limits are capital-weighted because
  -- risk is rupees, not a count of tickers.
  ('portfolio_sector_cap_TRENDING',   '40.0', 'Max %% of capital in one sector during TRENDING.'),
  ('portfolio_sector_cap_RISK_ON',    '40.0', 'Max %% of capital in one sector during RISK ON.'),
  ('portfolio_sector_cap_NEUTRAL',    '30.0', 'Max %% of capital in one sector during NEUTRAL.'),
  ('portfolio_sector_cap_RECOVERING', '25.0', 'Max %% of capital in one sector during RECOVERING.'),
  ('portfolio_sector_cap_RISK_OFF',   '25.0', 'Max %% of capital in one sector during RISK OFF.'),
  ('portfolio_max_per_industry',      '2',
   'Hard count cap per INDUSTRY. industry is the honest correlation unit (83 groups vs 23 sectors); two names in one industry are close to one position held twice.'),
  ('portfolio_max_total_risk_pct',    '6.0',
   'Total open risk across all positions, as %% of capital. Sum of (current_price - active_sl) * qty. Positions whose stop is at or above entry consume none of it.'),
  ('portfolio_min_position_pct',      '3.0',
   'Minimum position as %% of capital. A residual sector cap can otherwise admit a token position that pays full brokerage/STT/slippage and occupies one of 7 slots without moving returns.'),

  -- Gate semantics: missing data must weaken a setup, never strengthen it
  ('signal_max_unknown_indicators', '2',
   'Max indicators that may be NULL and still allow a typed entry signal. Gate checks were written as (x == 0 or threshold), and a NULL read as 0.0 short-circuited them to True — so partial data failures produced MORE signals, not fewer.'),
  ('signal_buy_candidate_min_known', '3',
   'Of rsi_daily/rsi_weekly/adx/vol_ratio/delivery_pct, how many must be non-NULL for the untyped BUY_CANDIDATE fall-through. It applies no thresholds of its own and was the most exposed to the null-as-pass defect; it is also the most common label in production (11/11 entry signals on 2026-07-24).'),

  -- Breakout thresholds, recalibrated against the observed distribution
  ('signal_breakout_readiness_high', '58.0',
   'Was hardcoded 70. Measured across all 75 master_shortlist rows on 2026-07-24: <20:32, 20-40:37, 40-60:4, 60-70:2, >=70:0, max 68. Nothing ever reached 70.'),
  ('signal_breakout_readiness_mid',  '45.0', 'Was hardcoded 60; only 2 of 75 rows qualified.'),
  ('signal_breakout_readiness_vol',  '62.0', 'Was hardcoded 75; unreachable.')
ON CONFLICT (key) DO NOTHING;


-- ── PART 2: msl_final_score_weights key mismatch ───────────────────────────
--
-- The stored blob used keys that compute_final_score never reads:
--     stored : holding_score, validity_score, breakout_readiness, risk_score_inv
--     read   : momentum, validity, trend_structure, institutional, breakout,
--              sector, weekly_structure, base_score
-- Every w.get(...) fell through to its hardcoded default, so this config has
-- been silently inert since it was written. Rewritten with the keys the code
-- actually reads, and with the sector weight reduced from 0.08 to 0.05 —
-- sector influence is now applied deliberately at the screener and portfolio
-- layers rather than four times over.
UPDATE public.system_config
   SET value = '{"momentum":0.22,"validity":0.20,"trend_structure":0.15,'
               '"institutional":0.14,"breakout":0.13,"sector":0.05,'
               '"weekly_structure":0.07,"base_score":0.04}',
       description = 'Weights for compute_msl.compute_final_score. Keys MUST match '
                     'those read in get_final_score_weights() — a previous blob used '
                     'holding_score/validity_score/risk_score_inv, none of which are '
                     'read, so it was silently ignored. Must sum to 1.0.',
       updated_at = now()
 WHERE key = 'msl_final_score_weights';

INSERT INTO public.system_config (key, value, description)
SELECT 'msl_final_score_weights',
       '{"momentum":0.22,"validity":0.20,"trend_structure":0.15,'
       '"institutional":0.14,"breakout":0.13,"sector":0.05,'
       '"weekly_structure":0.07,"base_score":0.04}',
       'Weights for compute_msl.compute_final_score. Keys must match the code.'
WHERE NOT EXISTS (SELECT 1 FROM public.system_config WHERE key = 'msl_final_score_weights');


-- ── PART 3: backfill signal attribution on historical trades ───────────────
--
-- EXPECTED YIELD: ~1 of 70 rows. Measured before writing this.
--
--   signal_log spans          2026-03-09 .. 2026-07-24
--   closed_positions spans    2025-12-31 .. 2026-07-13
--   trades entered BEFORE signal_log existed:  69 of 70
--   trades entered AFTER:                       1  (VIJAYA, 2026-07-13, on a
--                                                   WATCH signal, P&L 0.00%)
--
-- So the 70 closed trades are a PRE-AUTOMATION baseline, not a track record of
-- the current signal engine — which has produced essentially zero closed
-- trades to date. This backfill is still correct to run (it is idempotent and
-- recovers what exists), but its real purpose is forward-looking: from now on
-- position_lifecycle stamps signal_id/signal_date at creation, so attribution
-- no longer depends on inference.
--
-- Preference order is deliberate: an actionable BUY-family signal first, and
-- only then any signal at all (including WATCH). A trade taken against a WATCH
-- is still informative — it measures discretionary overrides against the
-- system.
WITH ranked AS (
    SELECT cp.id                AS cp_id,
           sl.id                AS sig_id,
           sl.date              AS sig_date,
           sl.signal_type,
           sl.entry_timing_type,
           sl.lifecycle,
           sl.regime,
           sl.sector_rank_at_entry,
           ROW_NUMBER() OVER (
               PARTITION BY cp.id
               ORDER BY
                 CASE WHEN sl.signal_type IN
                      ('PRIME_SETUP','BREAKOUT_SETUP','REENTRY_SETUP',
                       'BUY_CANDIDATE','STAGED_ENTRY','MOMENTUM_CONTINUATION')
                      THEN 0 ELSE 1 END,
                 sl.date DESC
           ) AS rn
      FROM public.closed_positions cp
      JOIN public.signal_log sl
        ON sl.symbol = cp.symbol
       AND sl.date  <= cp.entry_date
       AND sl.date  >= cp.entry_date - 10
     WHERE cp.signal_date IS NULL
)
UPDATE public.closed_positions cp
   SET signal_id            = r.sig_id,
       signal_date          = r.sig_date,
       entry_signal_type    = COALESCE(cp.entry_signal_type, r.signal_type),
       entry_timing_type    = COALESCE(cp.entry_timing_type, r.entry_timing_type),
       lifecycle_at_entry   = COALESCE(NULLIF(cp.lifecycle_at_entry, 'Not Found'), r.lifecycle),
       regime_at_entry      = COALESCE(cp.regime_at_entry, r.regime),
       sector_rank_at_entry = COALESCE(cp.sector_rank_at_entry, r.sector_rank_at_entry)
  FROM ranked r
 WHERE r.cp_id = cp.id AND r.rn = 1;

-- Derived outcome metrics for every historical row that lacks them.
UPDATE public.closed_positions
   SET hold_days = (exit_date - entry_date)
 WHERE hold_days IS NULL AND exit_date IS NOT NULL AND entry_date IS NOT NULL;

UPDATE public.closed_positions
   SET source = COALESCE(source, 'manual')
 WHERE source IS NULL;


-- ── PART 4: attribution view ───────────────────────────────────────────────
-- One place for the dashboard and the Brain to ask "which signal types,
-- sectors, regimes and entry timings actually made money".
CREATE OR REPLACE VIEW public.v_trade_attribution
WITH ("security_invoker" = 'on') AS
SELECT
    cp.id,
    cp.symbol,
    cp.sector,
    cp.strategy,
    cp.entry_date,
    cp.exit_date,
    cp.hold_days,
    cp.entry_price,
    cp.exit_price,
    cp.pnl_pct,
    cp.realized_pnl,
    cp.r_multiple,
    cp.exit_reason,
    cp.max_favorable_excursion,
    cp.max_adverse_excursion,
    -- capture ratio: how much of the best unrealised gain was actually kept
    CASE WHEN cp.max_favorable_excursion > 0
         THEN round((cp.pnl_pct / cp.max_favorable_excursion * 100)::numeric, 1)
    END                                            AS capture_pct,
    cp.signal_id,
    cp.signal_date,
    cp.entry_signal_type,
    cp.entry_timing_type,
    cp.lifecycle_at_entry,
    cp.regime_at_entry,
    cp.sector_rank_at_entry,
    cp.source,
    (cp.entry_date - cp.signal_date)               AS signal_to_entry_days,
    sl.score_adjusted                              AS signal_score,
    sl.ai_conviction,
    sl.momentum_state,
    sl.trend_maturity,
    sl.implied_rr                                  AS signal_implied_rr,
    (cp.pnl_pct > 0)                               AS is_win
  FROM public.closed_positions cp
  LEFT JOIN public.signal_log sl ON sl.id = cp.signal_id;

COMMENT ON VIEW public.v_trade_attribution IS
  'Realised outcomes joined to the signal that produced them. Feeds the '
  'performance dashboard and the Brain. Before migration 004, signal_date was '
  'NULL on all 70 closed trades so this join was impossible.';
