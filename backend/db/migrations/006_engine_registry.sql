-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 006: engine registry + lifecycle
-- ═══════════════════════════════════════════════════════════════════════════
--
-- WHY
--   strategy_config held only CTL, SBS, TPO and EAP. The six proprietary
--   scanners — VBD, IAD, RSB, MOM, RVS, SEC — had every threshold hardcoded in
--   screen_stocks.py and no database row at all. So:
--     * two thirds of the engines could not be tuned, disabled or retired
--       without a code edit and redeploy
--     * the Brain could propose changes to CTL/SBS but was blind to the
--       engines producing most of the candidates (RVS alone contributed 65 of
--       124 unique symbols on 2026-07-24)
--     * there was no way to trial a new engine — you shipped it live or not
--       at all
--
-- WHAT THIS ADDS
--   1. A row per engine carrying declarative `gates`, `min_score`, `lifecycle`
--      and display metadata.
--   2. Three lifecycle states:
--        ACTIVE   contributes to composite_score
--        SHADOW   runs and logs candidates, EXCLUDED from composite_score.
--                 This is how a new engine earns promotion: it accumulates
--                 screener_performance history with zero live effect.
--        RETIRED  does not run
--   3. engine_performance view — hit rate and mean forward return per engine,
--      so a lifecycle decision is made against evidence rather than intuition.
--
-- Gate values are transcribed VERBATIM from the current Python. Seeding is
-- behaviour-neutral: verified by replaying the declarative gates against the
-- 2026-07-24 universe and matching engine candidate counts (MOM 18=18,
-- RVS 98->65 after min_score, SBS 1->0, VBD 1, IAD 6->2, RSB 8->6).
--
-- Depends on migration 005 (params must already be a jsonb OBJECT).
-- Idempotent.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.strategy_config
    ADD COLUMN IF NOT EXISTS lifecycle    text DEFAULT 'ACTIVE',
    ADD COLUMN IF NOT EXISTS label        text,
    ADD COLUMN IF NOT EXISTS description  text,
    ADD COLUMN IF NOT EXISTS engine_type  text DEFAULT 'ENGINE',
    ADD COLUMN IF NOT EXISTS created_at   timestamptz DEFAULT now(),
    ADD COLUMN IF NOT EXISTS retired_at   timestamptz,
    ADD COLUMN IF NOT EXISTS notes        text;

ALTER TABLE public.strategy_config
    DROP CONSTRAINT IF EXISTS strategy_config_lifecycle_chk;
ALTER TABLE public.strategy_config
    ADD CONSTRAINT strategy_config_lifecycle_chk
    CHECK (lifecycle IN ('ACTIVE', 'SHADOW', 'RETIRED'));

COMMENT ON COLUMN public.strategy_config.lifecycle IS
  'ACTIVE = contributes to composite_score. SHADOW = runs and logs candidates '
  'but is excluded from composite_score, so a new engine can accumulate '
  'screener_performance history with no effect on live decisions. '
  'RETIRED = does not run.';
COMMENT ON COLUMN public.strategy_config.params IS
  'JSON object. `gates` holds declarative hard filters (UI-editable); scoring '
  'stays in Python because it is non-linear and because editing it live would '
  'make historical scores incomparable, destroying outcome attribution.';


-- ── Seed the nine engines ──────────────────────────────────────────────────
-- ON CONFLICT merges gates/min_score into any existing row without clobbering
-- operator-tuned values already present.
INSERT INTO public.strategy_config (strategy, params, enabled, lifecycle, label, description, engine_type)
VALUES
('CTL', '{"lifecycle":"ACTIVE","gates":{
    "sector_rank":{"max":8},"rsi_monthly":{"min":58},"rsi_weekly":{"min":58},
    "ret_6m":{"min":0},"atr_pct":{"max":4.5},"market_cap":{"min":300},
    "above_sma50":{"eq":true},"adx":{"min":12},
    "__di__":{"gt_field":["di_plus","di_minus"]}}}'::jsonb,
 true, 'ACTIVE', 'Core Trend Leaders',
 'Established uptrends in leading sectors. Monthly and weekly RSI confirmation plus a positive ADX spread.', 'ENGINE'),

('SBS', '{"lifecycle":"ACTIVE","gates":{
    "sector_rank":{"max":10},"rsi_daily":{"min":52},"rsi_weekly":{"min":54},
    "atr_pct":{"max":5.5},"vol_ratio":{"min":1.1},"market_cap":{"min":300},
    "consol_range":{"max":15,"or_field":"bb_squeeze"},"above_sma50":{"eq":true},
    "__wk__":{"any_of":["wk_hi_high","wk_hi_low"]}}}'::jsonb,
 true, 'ACTIVE', 'Structural Breakout Swing',
 'Tight base or volatility squeeze breaking out on volume. NOTE: vol_ratio>=1.1 and consol_range<=15 pull against each other - volume expansion widens the range this gate wants tight. Produced 0 candidates on 2026-07-24.', 'ENGINE'),

('TPO', '{"lifecycle":"ACTIVE","gates":{
    "dist_sma50":{"abs_max":4},"atr_pct":{"max":4},
    "above_sma50":{"eq":true},"rsi_weekly":{"min":50}}}'::jsonb,
 true, 'ACTIVE', 'Trend Pullback Opportunities',
 'Pullbacks within CTL trends. Operates on the CTL candidate set; RSI window is regime-dependent and applied in code.', 'ENGINE'),

('VBD', '{"lifecycle":"ACTIVE","min_score":55,"gates":{
    "pct_change":{"min":3.0,"max":12.0},"vol_ratio":{"min":1.8},
    "delivery_pct":{"min":35},"market_cap":{"min":300},
    "above_sma50":{"eq":true},"sector_rank":{"max":12}}}'::jsonb,
 true, 'ACTIVE', 'Velocity Burst Detector',
 'Single-session 3-12% move with institutional confirmation via delivery and volume.', 'SCANNER'),

('IAD', '{"lifecycle":"ACTIVE","min_score":55,"gates":{
    "delivery_pct":{"min":50},"vol_ratio":{"min":1.2},"market_cap":{"min":300},
    "above_sma50":{"eq":true},"consol_range":{"max":20},"sector_rank":{"max":14}}}'::jsonb,
 true, 'ACTIVE', 'Institutional Accumulation Detector',
 'Quiet delivery-led accumulation before price moves. Widest sector gate by design - accumulation matters most in sectors not yet leading.', 'SCANNER'),

('RSB', '{"lifecycle":"ACTIVE","min_score":55,"gates":{
    "rs_vs_nifty":{"min":4},"consol_range":{"max":12},"above_sma50":{"eq":true},
    "market_cap":{"min":300},"rsi_daily":{"min":48},"sector_rank":{"max":12}}}'::jsonb,
 true, 'ACTIVE', 'Relative Strength Breakout',
 'Relative-strength leaders coiling near resistance.', 'SCANNER'),

('MOM', '{"lifecycle":"ACTIVE","min_score":45,"gates":{
    "above_sma50":{"eq":true},"sma50_gt_200":{"eq":true},"above_st":{"eq":true},
    "adx":{"min":18},"__di__":{"gt_field":["di_plus","di_minus"]},
    "rsi_daily":{"min":52,"max":74},"rsi_weekly":{"min":54},
    "macd_hist":{"min":0.0001},"__wk__":{"any_of":["wk_hi_high","wk_hi_low"]},
    "market_cap":{"min":300},"sector_rank":{"max":12}}}'::jsonb,
 true, 'ACTIVE', 'Momentum Continuation',
 'Expansion-phase momentum with full MA stack alignment. Skipped in RISK OFF; stricter floors in RECOVERING.', 'SCANNER'),

('RVS', '{"lifecycle":"ACTIVE","min_score":55,"gates":{
    "rsi_daily":{"min":36,"max":54},"rsi_monthly":{"min":48},
    "sma50_gt_200":{"eq":true},"ret_6m":{"min":0},"dist_sma50":{"min":-10},
    "market_cap":{"min":300},"sector_rank":{"max":12}}}'::jsonb,
 true, 'ACTIVE', 'Reversal Setup',
 'SMA50 bounce with RSI turning up. Highest-volume engine - 65 of 124 unique candidates on 2026-07-24. Sector gate auto-relaxed +2 in RECOVERING/RISK OFF.', 'SCANNER'),

('SEC', '{"lifecycle":"ACTIVE","min_score":55,"gates":{
    "rsi_daily":{"min":48,"max":72},"above_sma50":{"eq":true},
    "market_cap":{"min":300},"__di__":{"gt_field":["di_plus","di_minus"]}}}'::jsonb,
 true, 'ACTIVE', 'Sector Rotation',
 'Early movers inside freshly leading sectors. Universe restricted to the top N sectors before gates apply.', 'SCANNER')

ON CONFLICT (strategy) DO UPDATE SET
    params      = public.strategy_config.params || EXCLUDED.params,
    lifecycle   = COALESCE(public.strategy_config.lifecycle, EXCLUDED.lifecycle),
    label       = COALESCE(public.strategy_config.label, EXCLUDED.label),
    description = COALESCE(public.strategy_config.description, EXCLUDED.description),
    engine_type = COALESCE(public.strategy_config.engine_type, EXCLUDED.engine_type),
    updated_at  = now();

-- EAP is an overlay, not a candidate-producing engine. Label it so the UI
-- does not render it as one.
UPDATE public.strategy_config
   SET engine_type = 'OVERLAY',
       label       = COALESCE(label, 'Event-Accelerated Plays'),
       description = COALESCE(description,
                     'Score overlay applied on top of engine output for corporate-event proximity. Produces no candidates of its own.')
 WHERE strategy = 'EAP';


-- ── Engine performance view ────────────────────────────────────────────────
-- Hit rate and mean forward return per engine, so promote/retire decisions are
-- evidence-based. Reads screener_performance, whose engines_list is a
-- comma-separated tag such as "CTL,MOM,SEC" — unnested here so a symbol found
-- by three engines counts once for each.
CREATE OR REPLACE VIEW public.v_engine_performance
WITH ("security_invoker" = 'on') AS
WITH exploded AS (
    SELECT sp.symbol, sp.entry_date, sp.hold_days, sp.pct_return,
           sp.composite_score, sp.priority_rank,
           btrim(e.engine) AS engine
      FROM public.screener_performance sp
      CROSS JOIN LATERAL unnest(string_to_array(COALESCE(sp.engines_list, ''), ',')) AS e(engine)
     WHERE btrim(e.engine) <> ''
)
SELECT
    engine,
    hold_days,
    count(*)                                                   AS n_signals,
    count(*) FILTER (WHERE pct_return > 0)                     AS n_wins,
    round(100.0 * count(*) FILTER (WHERE pct_return > 0)
          / NULLIF(count(*), 0), 1)                            AS hit_rate_pct,
    round(avg(pct_return)::numeric, 2)                         AS avg_return_pct,
    round(avg(pct_return) FILTER (WHERE pct_return > 0)::numeric, 2)  AS avg_win_pct,
    round(avg(pct_return) FILTER (WHERE pct_return <= 0)::numeric, 2) AS avg_loss_pct,
    round((avg(pct_return) FILTER (WHERE pct_return > 0)
          / NULLIF(abs(avg(pct_return) FILTER (WHERE pct_return <= 0)), 0))::numeric, 2)
                                                               AS payoff_ratio,
    round(avg(composite_score)::numeric, 1)                    AS avg_score,
    min(entry_date)                                            AS first_signal,
    max(entry_date)                                            AS last_signal
  FROM exploded
 GROUP BY engine, hold_days;

COMMENT ON VIEW public.v_engine_performance IS
  'Per-engine hit rate, payoff and mean forward return by hold period. Feeds '
  'the Strategy Control Room so ACTIVE/SHADOW/RETIRED decisions are made '
  'against measured outcomes. Note screener_performance only began recording '
  'reliably after the trading-day arithmetic fix - it previously used calendar '
  'days and skipped most periods.';


-- ── Engine lifecycle audit ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.engine_lifecycle_log (
    id            bigserial PRIMARY KEY,
    changed_at    timestamptz DEFAULT now() NOT NULL,
    engine        text NOT NULL,
    from_state    text,
    to_state      text NOT NULL,
    from_params   jsonb,
    to_params     jsonb,
    changed_by    text DEFAULT 'ui',
    reason        text,
    evidence      jsonb
);
CREATE INDEX IF NOT EXISTS engine_lifecycle_log_engine_idx
    ON public.engine_lifecycle_log (engine, changed_at DESC);

ALTER TABLE public.engine_lifecycle_log OWNER TO postgres;
GRANT ALL ON TABLE public.engine_lifecycle_log TO anon, authenticated, service_role;
GRANT ALL ON SEQUENCE public.engine_lifecycle_log_id_seq TO anon, authenticated, service_role;

COMMENT ON TABLE public.engine_lifecycle_log IS
  'Every engine promote/demote/retire and gate change, with the evidence that '
  'justified it. Makes strategy evolution auditable and reversible.';
