-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 014: the intraday strategy framework
-- ═══════════════════════════════════════════════════════════════════════════
--
-- A dedicated engine family for intraday, separate from the swing engines in
-- strategy_config. They are not variations of each other: swing engines read
-- daily bars and ask "worth owning for a fortnight"; intraday engines read the
-- live session and ask "is there a move worth taking before 15:20, net of
-- costs". Sharing one table would force every swing engine to carry a session
-- phase it has no use for, and every intraday engine to carry a six-month
-- relative strength it cannot act on.
--
-- COSTS ARE A FIRST-CLASS INPUT HERE
-- ----------------------------------
-- Measured against the real Zerodha intraday schedule, a round trip costs about
-- 0.21% of position value and — because brokerage is "₹20 or 0.03%, whichever
-- is LOWER" — that figure barely improves with size until roughly ₹66,000 per
-- leg. The consequence is a hard constraint, not a preference:
--
--     0.30% target  ->  not tradeable at ANY size
--     0.50% target  ->  needs ~₹3.5L per position to keep 70% of the move
--     0.70%+ target ->  viable
--
-- So the cost rates live in system_config as tunable values, and every setup is
-- checked against them before it is ever shown.
-- ═══════════════════════════════════════════════════════════════════════════


-- ── Engine registry ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.intraday_strategy_config (
    strategy    text PRIMARY KEY,
    enabled     boolean NOT NULL DEFAULT true,
    lifecycle   text    NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE | SHADOW | RETIRED
    phases      text,                                -- session phases it may fire in
    label       text,
    description text,
    params      jsonb   NOT NULL DEFAULT '{}'::jsonb,
    notes       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.intraday_strategy_config IS
  'Intraday engines. SHADOW lets an engine produce setups that are recorded but '
  'never alerted, so a new idea earns its place on live data before it can cost '
  'anything.';


-- ── Setups produced ────────────────────────────────────────────────────────
-- One row per setup DETECTED, whether or not it was acted on. Recording the
-- rejected ones is the point: "how often did ORB fire and how did those resolve"
-- is unanswerable if only the taken trades are stored, and that question is the
-- only way an engine's lifecycle state can ever be justified.
CREATE TABLE IF NOT EXISTS public.intraday_setups (
    id           bigserial PRIMARY KEY,
    ts           timestamptz NOT NULL DEFAULT now(),
    trade_date   date        NOT NULL,
    symbol       text        NOT NULL,
    strategy     text        NOT NULL,
    phase        text,
    direction    text        NOT NULL DEFAULT 'LONG',
    entry        numeric,
    stop         numeric,
    target       numeric,
    risk_pct     numeric,
    reward_pct   numeric,
    rr           numeric,
    confidence   numeric,
    rationale    text,
    invalidation text,
    -- Cost verdict, stored so a rejection can be reviewed later rather than
    -- silently vanishing.
    cost_pct     numeric,
    cost_verdict text,                                -- TAKEN | REJECTED_COST | SHADOW
    corroborated_by text,
    meta         jsonb,
    outcome      text,                                -- filled post-hoc
    outcome_pct  numeric
);

CREATE INDEX IF NOT EXISTS idx_intraday_setups_date ON public.intraday_setups (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_intraday_setups_strategy ON public.intraday_setups (strategy, trade_date DESC);

COMMENT ON TABLE public.intraday_setups IS
  'Every setup detected, including those rejected on cost. Volume is bounded by '
  'design — a few dozen rows a day, not one per tick.';


-- ── Config: session, engines, and the cost schedule ────────────────────────
INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  -- Session
  ('intraday_min_runway_min', '45', 'Minimum minutes to square-off for a new entry. Below this a thesis has no room to resolve.', 'Intraday', 'intraday/session.py', 'int', 'TUNING'),

  -- Cost schedule (Zerodha equity intraday). Rates change; these are tunable
  -- precisely so a stale number does not silently bias every sizing decision.
  ('cost_brokerage_flat', '20',      'Brokerage cap per executed order, rupees.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_brokerage_pct',  '0.03',    'Brokerage percent per order; the LOWER of this and the flat cap applies.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_stt_sell_pct',   '0.025',   'STT, sell side only.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_exchange_pct',   '0.00297', 'NSE transaction charge on turnover.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_sebi_pct',       '0.0001',  'SEBI turnover fee.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_stamp_buy_pct',  '0.003',   'Stamp duty, buy side only.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_gst_pct',        '18',      'GST on brokerage + exchange + SEBI.', 'Intraday', 'intraday/cost_model.py', 'float', 'CRITICAL'),
  ('cost_slippage_bps',   '5',       'Assumed slippage per side, basis points. Not a charge, but ignoring it is how a backtest beats a live account.', 'Intraday', 'intraday/cost_model.py', 'float', 'TUNING'),
  ('intraday_cost_keep_ratio', '0.70', 'Fraction of the gross move that must survive costs for a setup to be taken.', 'Intraday', 'intraday/cost_model.py', 'float', 'TUNING'),

  -- ORB
  ('intraday_engine_orb_enabled', 'true', 'Opening Range Breakout engine.', 'Intraday', 'intraday/strategies/orb.py', 'bool', 'TUNING'),
  ('orb_window_min',          '15',  'Opening range window, minutes.', 'Intraday', 'intraday/strategies/orb.py', 'int', 'TUNING'),
  ('orb_min_range_frac',      '0.15','Minimum opening range as a fraction of daily ATR%. Below this the range is noise.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_max_range_frac',      '0.70','Maximum opening range as a fraction of daily ATR%. Above this the day has already moved.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_max_chase_pct',       '0.60','How far past the range high an entry is still allowed.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_min_volume_ratio',    '1.20','Time-adjusted volume vs 20d average required to confirm a break.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_require_above_pdh',   '1',   'Require the break to clear the previous day high. 0 disables.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_stop_buffer_pct',     '0.10','Buffer below the range low for the stop.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),
  ('orb_max_risk_pct',        '1.20','Cap on stop distance.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'CRITICAL'),
  ('orb_target_r',            '2.0', 'Target as a multiple of risk.', 'Intraday', 'intraday/strategies/orb.py', 'float', 'TUNING'),

  -- VWAP reclaim
  ('intraday_engine_vwr_enabled', 'true', 'VWAP reclaim engine — the drift-window strategy.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'bool', 'TUNING'),
  ('vwr_lookback_bars',      '12',  'Bars examined for the fade-and-reclaim shape.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'int', 'TUNING'),
  ('vwr_min_bars_below',     '3',   'Bars that must have closed below VWAP first. Without this it is not a reclaim.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'int', 'TUNING'),
  ('vwr_max_extension_pct',  '0.45','How far above VWAP an entry is still allowed.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'float', 'TUNING'),
  ('vwr_stop_buffer_pct',    '0.08','Buffer below the sub-VWAP swing low.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'float', 'TUNING'),
  ('vwr_max_risk_pct',       '0.90','Cap on stop distance.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'float', 'CRITICAL'),
  ('vwr_target_r',           '2.0', 'Fallback target multiple when the day high is too close.', 'Intraday', 'intraday/strategies/vwap_reclaim.py', 'float', 'TUNING'),

  -- Universe
  ('intraday_min_price',      '50',    'Minimum price. Penny names have spreads that eat any intraday edge.', 'Intraday', 'intraday/scanner.py', 'float', 'TUNING'),
  ('intraday_min_turnover_cr','25',    'Minimum 20-day average turnover in crores — the liquidity floor.', 'Intraday', 'intraday/scanner.py', 'float', 'TUNING'),
  ('intraday_max_universe',   '40',    'Symbols streamed at once. Bounded by websocket load, not by ambition.', 'Intraday', 'intraday/scanner.py', 'int', 'TUNING'),
  ('intraday_entry_approach_pct', '1.0', 'How close price must come to a swing max_entry before alerting.', 'Intraday', 'intraday/engine.py', 'float', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n int;
BEGIN
  SELECT count(*) INTO n FROM public.system_config WHERE category = 'Intraday';
  RAISE NOTICE 'Intraday config keys: %', n;
  RAISE NOTICE 'Engines register themselves on first run — intraday.strategies.registry.sync_to_db().';
  RAISE NOTICE 'Cost reality: ~0.21%% round trip. Targets under ~0.7%% are not tradeable at this account size.';
END $$;
