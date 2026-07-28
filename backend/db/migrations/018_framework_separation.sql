-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 018: tell swing and intraday positions apart
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE HOLE
-- --------
-- open_positions has no column identifying which framework opened a row. Both
-- write to the same table, and control/position_lifecycle manages EVERY row
-- with the swing exit policy: partial at 1.5R, trail after 2R, hard target at
-- 3R, time stop at 15 SESSIONS.
--
-- Apply that to an intraday position and it is catastrophic in a quiet way. An
-- intraday trade must be flat by 15:20 — the broker squares off MIS itself, and
-- a CNC position simply becomes an unplanned overnight hold. The swing engine
-- would instead carry it for fifteen sessions waiting for 3R, on a setup whose
-- entire thesis had a four-hour horizon and a 0.5% stop.
--
-- Nothing would error. The position would just sit there being managed by rules
-- that were never meant for it.
--
-- PRODUCT IS A SEPARATE QUESTION FROM FRAMEWORK
-- ---------------------------------------------
-- These are often conflated and should not be:
--
--   framework  WHO decided the trade and WHOSE exit rules apply
--   product    HOW it is held at the broker: CNC (delivery, no leverage, can be
--              held indefinitely) or MIS (intraday margin, auto-squared-off by
--              the broker around 15:20)
--
-- An intraday setup taken as CNC is perfectly legitimate — no leverage, no
-- forced square-off, and you may choose to keep it. What must not happen is
-- the SWING exit engine deciding that for you because it could not tell the
-- difference. framework governs the rules; product governs the broker.
--
-- The intraday order and GTT managers currently place CNC deliberately: MIS
-- adds leverage and a forced exit to a system that has never placed a live
-- order. Product is recorded so the choice is explicit and reviewable rather
-- than implied.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.open_positions
    ADD COLUMN IF NOT EXISTS framework text NOT NULL DEFAULT 'SWING',
    ADD COLUMN IF NOT EXISTS product   text NOT NULL DEFAULT 'CNC',
    ADD COLUMN IF NOT EXISTS intraday_strategy text,
    ADD COLUMN IF NOT EXISTS must_exit_by time;

COMMENT ON COLUMN public.open_positions.framework IS
  'SWING | INTRADAY — whose exit rules apply. NOT the same as product: an '
  'intraday setup held as CNC is still governed by intraday rules.';
COMMENT ON COLUMN public.open_positions.product IS
  'CNC (delivery) | MIS (intraday margin, broker squares off ~15:20).';
COMMENT ON COLUMN public.open_positions.intraday_strategy IS
  'Which intraday engine produced it — ORB, GAP, PDL, VCE, PBK, VWR, RNG.';
COMMENT ON COLUMN public.open_positions.must_exit_by IS
  'Hard exit time for INTRADAY rows. NULL for swing, which has no such deadline.';

ALTER TABLE public.closed_positions
    ADD COLUMN IF NOT EXISTS framework text NOT NULL DEFAULT 'SWING',
    ADD COLUMN IF NOT EXISTS product   text NOT NULL DEFAULT 'CNC',
    ADD COLUMN IF NOT EXISTS intraday_strategy text;

COMMENT ON COLUMN public.closed_positions.framework IS
  'Attribution for performance. Mixing intraday and swing outcomes in one '
  'expectancy number describes neither — a 0.7% intraday win and a 9% swing '
  'win are not comparable observations.';

-- Everything that exists today was opened before intraday could open anything,
-- so the DEFAULT is already correct. Stated explicitly rather than assumed.
UPDATE public.open_positions   SET framework = 'SWING' WHERE framework IS NULL;
UPDATE public.closed_positions SET framework = 'SWING' WHERE framework IS NULL;

CREATE INDEX IF NOT EXISTS idx_open_positions_framework ON public.open_positions (framework);
CREATE INDEX IF NOT EXISTS idx_closed_positions_framework ON public.closed_positions (framework);


-- ── Performance, split by framework ────────────────────────────────────────
CREATE OR REPLACE VIEW public.v_performance_by_framework AS
SELECT
    framework,
    count(*)                                              AS trades,
    count(*) FILTER (WHERE realized_pnl > 0)              AS wins,
    round(100.0 * count(*) FILTER (WHERE realized_pnl > 0)
          / NULLIF(count(*), 0), 1)                       AS win_rate_pct,
    round(sum(realized_pnl)::numeric, 2)                  AS net_pnl,
    round(avg(pnl_pct)::numeric, 3)                       AS avg_pct,
    round(avg(r_multiple)::numeric, 3)                    AS avg_r,
    round(avg(hold_days)::numeric, 1)                     AS avg_hold_days
  FROM public.closed_positions
 GROUP BY framework;

COMMENT ON VIEW public.v_performance_by_framework IS
  'Never blend the two. A 0.7% intraday win and a 9% swing win are not the same '
  'kind of observation, and one expectancy number over both describes neither.';


-- ── Config ─────────────────────────────────────────────────────────────────
INSERT INTO public.system_config (key, value, description, category, subsystem, value_type, risk_level)
VALUES
  ('intraday_product', 'CNC',
   'Broker product for intraday orders. CNC = delivery, no leverage, no forced square-off. MIS = intraday margin, broker exits ~15:20. CNC is deliberate while order placement is new.',
   'Intraday', 'intraday/order_manager.py', 'string', 'CRITICAL'),
  ('intraday_must_exit_time', '15:15',
   'When intraday positions should be flat. Ahead of the broker MIS square-off so the exit is yours, not forced.',
   'Intraday', 'intraday/engine.py', 'string', 'CRITICAL'),
  ('swing_manages_intraday_rows', 'false',
   'Whether the swing exit engine may manage INTRADAY-framework positions. Off: a four-hour thesis must not inherit a fifteen-session time stop.',
   'Positions', 'control/position_lifecycle.py', 'bool', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


DO $$
DECLARE n_swing int; n_intra int;
BEGIN
  SELECT count(*) INTO n_swing FROM public.open_positions WHERE framework = 'SWING';
  SELECT count(*) INTO n_intra FROM public.open_positions WHERE framework = 'INTRADAY';
  RAISE NOTICE 'open_positions: % swing, % intraday', n_swing, n_intra;
  RAISE NOTICE 'position_lifecycle now skips INTRADAY rows unless swing_manages_intraday_rows is true.';
END $$;
