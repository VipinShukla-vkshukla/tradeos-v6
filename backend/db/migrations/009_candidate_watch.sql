-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 009: intraday candidate watch
-- ═══════════════════════════════════════════════════════════════════════════
--
-- The 30-minute monitor watched open_positions ONLY. Ranked candidates were
-- evaluated once, at 22:00 against the closing price, and then not again — so
-- a TIER_1 that traded into its entry zone at 11:15 produced no alert, and one
-- that gapped past its target produced no warning either. The morning brief
-- was the only candidate-facing message of the day and it described a price
-- that was already ~18 hours old.
--
-- This table is the alert state machine for candidates. It exists so the
-- monitor can tell "the decision changed" from "the decision is unchanged",
-- which is the difference between a useful alert and 13 identical ones per day.
--
-- Deliberately NOT stored on signal_output_daily: that table is the immutable
-- record of what was known at decision time, and mutating it every 30 minutes
-- would destroy the property that makes it usable for ML training.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.candidate_watch (
    trade_date      date        NOT NULL,
    symbol          text        NOT NULL,
    ai_tier         text,
    -- Last action the decision layer produced for this symbol today.
    last_action     text        NOT NULL,
    last_headline   text,
    last_price      numeric,
    last_rr         numeric,
    -- Only bumped when the ACTION changes, so repeat evaluations at the same
    -- state stay silent.
    alerts_sent     integer     DEFAULT 0,
    first_seen_at   timestamptz DEFAULT now(),
    last_eval_at    timestamptz DEFAULT now(),
    last_alert_at   timestamptz,
    price_source    text,
    PRIMARY KEY (trade_date, symbol)
);

CREATE INDEX IF NOT EXISTS candidate_watch_date_idx
    ON public.candidate_watch (trade_date DESC);

ALTER TABLE public.candidate_watch OWNER TO postgres;
GRANT ALL ON TABLE public.candidate_watch TO anon, authenticated, service_role;

COMMENT ON TABLE public.candidate_watch IS
  'Per-day alert state for ranked candidates. The 30-minute monitor writes the '
  'current decision here and only notifies when it CHANGES — entering the zone, '
  'losing its reward:risk, breaking its stop, or reaching target. Without this '
  'the monitor would re-send the same message every cycle.';

COMMENT ON COLUMN public.candidate_watch.last_action IS
  'BUY_NOW | CHASE_LIMIT | WAIT | SKIP | NO_DATA — from analysis/trade_decision.';


-- Watch scope and noise controls.
INSERT INTO public.system_config (key, value, description) VALUES
  ('candidate_watch_tiers', 'TIER_1,TIER_2',
   'Which AI tiers the intraday monitor watches for entry-zone touches. TIER_3 is monitor-only by design and would add noise; widen here if you want it.'),
  ('candidate_watch_enabled', 'true',
   'Master switch for intraday candidate monitoring (separate from position monitoring).'),
  ('candidate_alert_on_actions', 'BUY_NOW,CHASE_LIMIT',
   'Only these actions raise a Telegram/Discord alert when first reached. WAIT and SKIP are recorded silently — you do not need a notification telling you to do nothing.')
ON CONFLICT (key) DO NOTHING;
