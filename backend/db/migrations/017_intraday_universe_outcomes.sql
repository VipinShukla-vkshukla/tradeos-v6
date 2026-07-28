-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 017: intraday universe + the learning loop
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Two holes in the intraday framework, both of which the swing system has
-- already taught us to care about.
--
-- 1. THE WRONG UNIVERSE
--    The engines only saw symbols the SWING pipeline shortlisted — seven names
--    chosen for a one-to-three-week thesis. That predicts almost nothing about
--    whether a stock moves enough TODAY, with enough liquidity, to pay for a
--    0.21% round trip. Intraday now selects its own universe on its own
--    criteria; measured against 2026-07-27 data, 40 of 499 symbols qualify and
--    only two of them appear on the swing shortlist.
--
-- 2. NO OUTCOMES
--    The swing framework has 70 closed trades and exactly ONE attributed to a
--    signal, which is why its ML model is untrainable and why no engine's
--    lifecycle state rests on evidence. Intraday produces setups roughly ten
--    times faster, so the same omission would compound ten times faster.
--    Outcomes are therefore resolved from day one, for every setup DETECTED
--    including the cost-rejected ones — "ORB fired 40 times and 12 would have
--    worked" is the statement that can justify enabling or retiring an engine;
--    "we took 3 and 1 worked" is not.
-- ═══════════════════════════════════════════════════════════════════════════


CREATE TABLE IF NOT EXISTS public.intraday_universe (
    trade_date   date    NOT NULL,
    symbol       text    NOT NULL,
    close        numeric,
    value_cr     numeric,
    atr_pct      numeric,
    delivery_pct numeric,
    sector       text,
    score        numeric,
    reason       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, symbol)
);

COMMENT ON TABLE public.intraday_universe IS
  'What was watchable each day and why. Small — ~40 rows per session — and it '
  'is the only way to answer later whether a missed move was even in scope.';

CREATE INDEX IF NOT EXISTS idx_intraday_universe_date ON public.intraday_universe (trade_date DESC);


-- ── Per-engine evidence ────────────────────────────────────────────────────
CREATE OR REPLACE VIEW public.v_intraday_engine_scorecard AS
SELECT
    strategy,
    count(*)                                            AS setups,
    count(*) FILTER (WHERE cost_verdict = 'TAKEN')      AS taken,
    count(*) FILTER (WHERE outcome = 'TARGET')          AS wins,
    count(*) FILTER (WHERE outcome = 'STOP')            AS losses,
    count(*) FILTER (WHERE outcome = 'TIMEOUT')         AS scratches,
    round(100.0 * count(*) FILTER (WHERE outcome = 'TARGET')
          / NULLIF(count(*) FILTER (WHERE outcome IS NOT NULL), 0), 1) AS hit_rate_pct,
    round(avg(outcome_pct) FILTER (WHERE outcome IS NOT NULL)::numeric, 3) AS avg_net_pct,
    round(avg(confidence)::numeric, 2)                  AS avg_confidence,
    max(trade_date)                                     AS last_seen
  FROM public.intraday_setups
 GROUP BY strategy
 ORDER BY setups DESC;

COMMENT ON VIEW public.v_intraday_engine_scorecard IS
  'Hit rate and net expectancy per engine, counting setups DETECTED rather than '
  'trades taken — so an engine that fires often and resolves badly is visible '
  'even when cost rejection kept you out of them. avg_net_pct is already net of '
  'the round trip, because a gross win under 0.21% is a loss.';


-- ── Universe + outcome config ──────────────────────────────────────────────
INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_min_atr_pct', '1.20',
   'Minimum daily ATR%. THE cost-model filter: below this a stock cannot produce a move that survives a 0.21% round trip, so streaming it wastes a slot.',
   'Intraday', 'intraday/scanner.py', 'float', 'TUNING'),
  ('intraday_max_atr_pct', '8.00',
   'Maximum daily ATR%. Above this, stops wide enough to survive the noise are too wide to size.',
   'Intraday', 'intraday/scanner.py', 'float', 'TUNING'),
  ('intraday_min_delivery_pct', '20',
   'Minimum delivery percentage — a name where almost nothing is delivered is being churned, and churn produces false breaks.',
   'Intraday', 'intraday/scanner.py', 'float', 'TUNING'),
  ('intraday_skip_flagged', 'true',
   'Exclude ASM and F&O-ban names. ASM widens spreads and imposes margin penalties; an F&O ban distorts the cash market for exactly those names.',
   'Intraday', 'intraday/scanner.py', 'bool', 'TUNING')
ON CONFLICT (key) DO NOTHING;


DO $$
BEGIN
  RAISE NOTICE 'Intraday universe and scorecard ready.';
  RAISE NOTICE 'Resolve a session:  python -m intraday.outcomes --date YYYY-MM-DD';
  RAISE NOTICE 'Engine evidence:    SELECT * FROM v_intraday_engine_scorecard;';
END $$;
