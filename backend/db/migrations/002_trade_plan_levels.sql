-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 002: persisted trade plan levels
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   compute_msl computed a stop and a target, derived an R-multiple from them,
--   stored ONLY the R-multiple (expected_r) and discarded the prices.
--   generate_signals then reconstructed prices from that R-multiple using a
--   DIFFERENT stop model (a flat 3% buffer instead of 1.5xATR), so the two
--   modules disagreed about the target by roughly 2.5x.
--
--   Practical effect, measured on 2026-07-24:
--     - 17 of 27 WATCH signals blocked with `insufficient_rr_*` at implied
--       R:R of 0.05x - 0.47x against a 1.0x minimum
--     - all 11 signals that passed had subtype `in_zone`
--   i.e. the entry gate could only ever admit a stock sitting exactly inside
--   its entry zone. Any stock that had begun to move was mathematically
--   incapable of passing.
--
--   Storing the LEVELS rather than a ratio removes the ability of two modules
--   to disagree. It also gives the position lifecycle manager the exact
--   active_sl and target_price to write when a position is opened, so the
--   stop you are alerted on is the stop the signal was actually validated
--   against.
--
-- See analysis/risk_model.py for the shared model these columns come from.
-- Additive only. Safe to re-run.
-- ═══════════════════════════════════════════════════════════════════════════

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['msl_computed', 'master_shortlist'] LOOP
    EXECUTE format($f$
      ALTER TABLE public.%I
        ADD COLUMN IF NOT EXISTS planned_entry       numeric,
        ADD COLUMN IF NOT EXISTS planned_stop        numeric,
        ADD COLUMN IF NOT EXISTS planned_target      numeric,
        ADD COLUMN IF NOT EXISTS planned_risk_pct    numeric,
        ADD COLUMN IF NOT EXISTS planned_stop_source text
    $f$, t);
  END LOOP;
END $$;

COMMENT ON COLUMN public.master_shortlist.planned_stop IS
  'Absolute stop price from analysis/risk_model.compute_trade_levels. Anchored to '
  'entry_zone_low, normally entry - 1.5*ATR*regime_multiplier, tightened to the '
  'supertrend level when that is closer but still outside daily noise. '
  'generate_signals Gate 4C and the position lifecycle manager both read THIS '
  'column — neither may recompute a stop of its own.';

COMMENT ON COLUMN public.master_shortlist.planned_target IS
  'Absolute target price, anchored to entry_zone_low + 3*ATR. Anchored to the '
  'SETUP rather than to live price, so chasing consumes remaining upside instead '
  'of inflating the target along with the entry.';

COMMENT ON COLUMN public.master_shortlist.planned_stop_source IS
  'atr | structure | atr_capped — which rule set the stop, for post-trade '
  'attribution of whether structural stops outperform ATR stops.';


-- ── signal_log: carry the levels onto the signal record ────────────────────
-- signal_log had no current_price, entry_zone_low/high, planned_stop or
-- planned_target at all. final_snapshot tried to read sl.get("entry_zone_low")
-- and always fell through to master_shortlist, which meant every
-- signal_output_daily row on 2026-07-24 had entry_zone_source =
-- 'master_shortlist' and nothing was ever sourced from the signal itself.
-- Carrying them here makes signal_log a self-contained decision record and
-- gives open_positions something concrete to link back to.
ALTER TABLE public.signal_log
    ADD COLUMN IF NOT EXISTS current_price       numeric,
    ADD COLUMN IF NOT EXISTS entry_zone_low      numeric,
    ADD COLUMN IF NOT EXISTS entry_zone_high     numeric,
    ADD COLUMN IF NOT EXISTS planned_entry       numeric,
    ADD COLUMN IF NOT EXISTS planned_stop        numeric,
    ADD COLUMN IF NOT EXISTS planned_target      numeric,
    ADD COLUMN IF NOT EXISTS planned_risk_pct    numeric,
    ADD COLUMN IF NOT EXISTS planned_stop_source text,
    ADD COLUMN IF NOT EXISTS implied_rr          numeric;

ALTER TABLE public.signal_output_daily
    ADD COLUMN IF NOT EXISTS planned_stop        numeric,
    ADD COLUMN IF NOT EXISTS planned_target      numeric,
    ADD COLUMN IF NOT EXISTS planned_risk_pct    numeric,
    ADD COLUMN IF NOT EXISTS implied_rr          numeric;

-- generate_signals upserts on (date, symbol); make that constraint explicit
-- rather than relying on it having been added ad hoc.
CREATE UNIQUE INDEX IF NOT EXISTS signal_log_date_symbol_uidx
    ON public.signal_log (date, symbol);


-- ── Risk model parameters ──────────────────────────────────────────────────
-- Registered in system_config so the Brain can calibrate them from realized
-- outcomes without a code change. Values match the module defaults.
INSERT INTO public.system_config (key, value, description) VALUES
  ('risk_stop_atr_mult',   '1.5',
   'Initial stop distance in ATRs below the entry anchor. Scaled by regime (see risk_model.REGIME_STOP_MULT).'),
  ('risk_target_atr_mult', '3.0',
   'Primary target distance in ATRs above the entry anchor. 3.0 with a 1.5 stop gives a 2.0 planned R:R.'),
  ('risk_max_risk_pct',    '8.0',
   'Reject a setup whose stop sits more than this far below entry — too wide to size sensibly.'),
  ('risk_min_risk_pct',    '1.5',
   'Floor on stop distance. A structural stop tighter than this sits inside daily noise and is ignored.'),
  ('risk_pct_per_trade',   '1.0',
   'Percent of total capital risked per trade. Position size = (capital * this) / risk_per_share.'),
  ('max_position_pct',     '20.0',
   'Cap on any single position as a percent of total capital.'),
  ('sector_rank_max_stale_days', '5',
   'generate_signals refuses to run if sector_strength is older than this. Sector leadership rotates faster than a week; stale ranks silently corrupt every gate.'),
  ('quality_gate_enabled', 'true',
   'When true, an ERROR in the pre-signal input quality checks halts the pipeline instead of emitting recommendations off known-bad data.')
ON CONFLICT (key) DO NOTHING;
