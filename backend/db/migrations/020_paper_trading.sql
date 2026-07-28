-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 020: paper trading
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Phase 3 is live: both frameworks can now sell holdings automatically. The
-- intraday engines have never placed a single trade, and the swing book has one
-- signal-attributed outcome out of seventy closed positions. Paper mode is how
-- seven untested engines and the new runner logic earn that permission on
-- evidence instead of on having compiled.
--
-- ONLY THE FILL IS SIMULATED
-- --------------------------
-- The universe scan, the engines, the index gate, the news gate, the cost
-- model, the exit ladder, the runner assessment, the alerts and the learning
-- loop are the identical code path. A paper result is therefore a real
-- statement about the decision logic — which a separate backtest harness could
-- never be, because a backtest is a second implementation and would drift from
-- the thing it is meant to measure. That is the same reason the intraday engine
-- calls evaluate_exit() rather than reimplementing it.
--
-- Paper positions live in open_positions with mode='PAPER', not in a separate
-- table, so everything downstream works on them with no special case.
--
-- PESSIMISTIC BY CONSTRUCTION
-- ---------------------------
-- Limit orders fill only when price trades THROUGH the limit, never on a touch.
-- Fills take the worse side of the spread. Full charges and slippage apply. A
-- paper system that flatters itself produces confidence rather than
-- information, which is worse than having none.
--
-- DEFAULTS: intraday PAPER, swing LIVE.
-- Swing has been trading real money for months and freezing it would be a
-- change, not a safeguard. Intraday has never traded, so it starts simulated.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'LIVE';
ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'LIVE';

COMMENT ON COLUMN public.open_positions.mode IS
  'LIVE | PAPER. Paper positions are managed by the same exit engine and feed '
  'the same learning loop; only the fill was simulated.';

CREATE INDEX IF NOT EXISTS idx_open_positions_mode ON public.open_positions (mode);
CREATE INDEX IF NOT EXISTS idx_closed_positions_mode ON public.closed_positions (mode);

-- Everything that exists predates paper mode and is real money.
UPDATE public.open_positions   SET mode = 'LIVE' WHERE mode IS NULL;
UPDATE public.closed_positions SET mode = 'LIVE' WHERE mode IS NULL;


-- ── Performance, split by mode AND framework ───────────────────────────────
CREATE OR REPLACE VIEW public.v_performance_by_mode AS
SELECT
    mode,
    framework,
    count(*)                                            AS trades,
    count(*) FILTER (WHERE realized_pnl > 0)            AS wins,
    round(100.0 * count(*) FILTER (WHERE realized_pnl > 0)
          / NULLIF(count(*), 0), 1)                     AS win_rate_pct,
    round(sum(realized_pnl)::numeric, 2)                AS net_pnl,
    round(avg(pnl_pct)::numeric, 3)                     AS avg_pct,
    round(avg(r_multiple)::numeric, 3)                  AS avg_r,
    round(avg(hold_days)::numeric, 1)                   AS avg_hold_days,
    min(exit_date)                                      AS since
  FROM public.closed_positions
 GROUP BY mode, framework;

COMMENT ON VIEW public.v_performance_by_mode IS
  'Paper and live results side by side. Never merge them into one number: paper '
  'has no queue position and no partial fills, so it is an upper bound on what '
  'the same decisions would have returned, not an estimate of it.';


INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_trading_mode', 'PAPER',
   'LIVE or PAPER for intraday. Starts PAPER: seven engines that have never placed a trade should earn live capital on measured outcomes, not on having compiled.',
   'Intraday', 'execution/gates.py', 'string', 'CRITICAL'),
  ('swing_trading_mode', 'LIVE',
   'LIVE or PAPER for swing. Starts LIVE because this book has been trading real money for months — freezing it would be a change, not a safeguard.',
   'Positions', 'execution/gates.py', 'string', 'CRITICAL'),
  ('paper_starting_capital', '100000',
   'Notional capital for paper accounting, independent of the real account so a simulated run is not constrained by cash that is already deployed.',
   'Positions', 'execution/paper_broker.py', 'float', 'SAFE')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE im text; sm text;
BEGIN
  SELECT value INTO im FROM public.system_config WHERE key = 'intraday_trading_mode';
  SELECT value INTO sm FROM public.system_config WHERE key = 'swing_trading_mode';
  RAISE NOTICE '';
  RAISE NOTICE 'intraday_trading_mode=% | swing_trading_mode=%', im, sm;
  RAISE NOTICE 'Promote when the evidence justifies it:';
  RAISE NOTICE '  UPDATE system_config SET value=''LIVE'' WHERE key=''intraday_trading_mode'';';
  RAISE NOTICE 'Check the evidence first:  SELECT * FROM v_performance_by_mode;';
END $$;
