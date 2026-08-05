-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 046: the sign convention shorting actually needs
-- ═══════════════════════════════════════════════════════════════════════════
--
-- `direction` has been a column on intraday_setups since migration 014 and a
-- field on Setup since the beginning. Every engine hardcoded 'LONG' and NOTHING
-- READ IT — the dominant failure mode in this project, a field nobody consumes.
--
-- That made shorting look like "add an engine". It is not. Eleven modules were
-- written assuming the field could not vary, and each fails differently and
-- silently on a short:
--
--   exit_policy       risk = entry - stop is NEGATIVE when the stop sits above
--                     entry, so the guard sent it to a 0.5% fallback and threw
--                     the real stop away. gain_r then ROSE as price rose: a
--                     short down 2% measured as +4.00R. It would have booked a
--                     partial, moved the stop to the wrong side of entry, and
--                     trailed away from the trade as the loss grew.
--   outcomes          `lo <= stop` is true on the FIRST BAR for a short, so
--                     every short resolved as an instant STOP at its own stop
--                     price. intraday_priors and the allocator's hurdle are
--                     both built from that table, so a short engine would have
--                     earned a catastrophic measured prior made entirely of an
--                     arithmetic error — and been retired on it.
--   scoring.score     treated stop >= entry as incoherent, which is the
--                     definition of a correct short, so the allocator DECLINED
--                     every one before any engine was consulted.
--
-- WHAT THIS MIGRATION ADDS
--
--   intraday_allow_shorts        the switch. OFF, and it must stay off until
--                                health's `shorts` check reports the spine
--                                complete. That check FAILS if the switch is on
--                                while any consumer is still long-only, because
--                                a half-converted spine does not refuse shorts —
--                                it mis-prices them.
--
--   intraday_short_cover_lead_min  how many minutes BEFORE the long deadline a
--                                short is covered. See below; this is the one
--                                genuinely asymmetric rule.
--
-- WHY A SHORT COVERS EARLIER, AND WHY IT IS ANCHORED TO must_exit_time
--
-- A long left open at the close becomes delivery: you need the cash, which is
-- inconvenient and recoverable. A short left open is SHORT DELIVERY — the
-- exchange buys it back in the auction session and the penalty runs to roughly
-- 20% of the trade value. It is the one outcome in this system an order of
-- magnitude worse than being wrong about direction, so covering is a safety
-- control rather than a policy.
--
-- The first implementation inflated `intraday_squareoff_buffer` for shorts.
-- That key is measured against the 15:30 close, and SQUARE_OFF phase begins at
-- 15:20 — i.e. minutes_to_close = 10 — so any buffer at or under ten minutes
-- was unreachable and the phase rung fired first, every time. It would have
-- been a CRITICAL switch that did nothing, which is this project's most-repeated
-- defect. Caught by testing it rather than by reading it.
--
-- Anchored to intraday_must_exit_time instead, so a short is always covered N
-- minutes before a long would be exited, wherever the operator moves that
-- deadline. At the default 15:15 exit, shorts cover at 15:05.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('intraday_allow_shorts', 'false',
   'Whether the intraday book may take SHORT setups at all. OFF. Shorting is a '
   'sign convention, not an engine: eleven modules were written assuming '
   'direction could not vary, and a partially converted spine mis-prices shorts '
   'rather than refusing them. Run tools/health — the `shorts` check reports how '
   'many sites are converted and FAILS if this is on while any is missing. '
   'Turning this on does not by itself make any engine emit a short.',
   'Intraday', 'intraday/engine.py', 'bool', 'false', 'CRITICAL'),

  ('intraday_short_cover_lead_min', '10',
   'Minutes before intraday_must_exit_time that a SHORT is covered. Longs exit '
   'at the deadline; shorts exit this many minutes earlier. Not symmetry — an '
   'uncovered short is SHORT DELIVERY, bought back for you in the auction '
   'session at a penalty of roughly 20% of the trade value, which dwarfs any '
   'stop this system would set. A few minutes of give-back on every short is a '
   'cheap premium against one un-covered position. Anchored to the exit '
   'deadline rather than to the close, because the close-relative version was '
   'unreachable behind the 15:20 SQUARE_OFF phase.',
   'Intraday', 'intraday/exit_policy.py', 'int', '10', 'CRITICAL')
ON CONFLICT (key) DO NOTHING;


-- Direction is written on every row from here on. Rows predating this are LONG,
-- which is what intraday.direction.normalise() returns for NULL — deliberately,
-- because reading an unlabelled historical row as SHORT would invert the sign
-- on the entire detection history at once.
UPDATE public.intraday_setups SET direction = 'LONG' WHERE direction IS NULL;

CREATE INDEX IF NOT EXISTS idx_intraday_setups_direction
    ON public.intraday_setups (direction, strategy, trade_date DESC);

COMMENT ON COLUMN public.intraday_setups.direction IS
  'LONG | SHORT. Read by intraday/direction.py, which is the single definition '
  'of the sign convention. NULL reads as LONG. Priors are keyed ENGINE/SHORT so '
  'the two directions never average together: they have different base rates, '
  'different failure modes (a short squeezes; a long does not) and different '
  'tails, and outcome_pct is signed in the trade''s favour.';


DO $$
DECLARE spine text;
BEGIN
  SELECT value INTO spine FROM public.system_config WHERE key = 'intraday_allow_shorts';
  RAISE NOTICE '';
  RAISE NOTICE 'Migration 046 applied. intraday_allow_shorts = %', spine;
  RAISE NOTICE '';
  RAISE NOTICE 'The DIRECTION SPINE is partially built. Converted so far:';
  RAISE NOTICE '  exit ladder · cover deadline · invalidation · cost model';
  RAISE NOTICE '  allocator scorer · priors · outcome resolver · setup levels';
  RAISE NOTICE '';
  RAISE NOTICE 'STILL LONG-ONLY, so no engine may emit a short yet:';
  RAISE NOTICE '  market context (allow_shorts) · structure gate (gate_short)';
  RAISE NOTICE '  engine excursion / entry side / cover on square-off';
  RAISE NOTICE '  shortability filter (circuit-band and squeeze proxies)';
  RAISE NOTICE '';
  RAISE NOTICE 'Check progress at any time with:';
  RAISE NOTICE '    python -m tools.health        -> the `shorts` row';
  RAISE NOTICE '';
END $$;
