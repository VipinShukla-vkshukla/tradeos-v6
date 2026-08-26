-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 122: same-day swing setup discovery (Phase 4)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. Swing framework evolution blueprint, Phase 4 — narrow,
-- 3-engine (VBD/SBS/RSB only) same-day candidate detection, never a second
-- decision system. See swing/signals/same_day_discovery.py's own docstring
-- for the full reasoning and the honest delivery_pct-proxy limitation.
--
-- Two new, isolated tables — never signal_output_daily (stays immutable)
-- and never signal_log/signal_outcomes (avoids the documented "same-symbol/
-- date collision poisons the evening pipeline's learning loop" landmine by
-- construction).

CREATE TABLE IF NOT EXISTS public.swing_same_day_candidates (
    id                     bigserial PRIMARY KEY,
    date                   date NOT NULL,
    symbol                 text NOT NULL,
    strategy               text NOT NULL,
    entry_zone_low         numeric,
    entry_zone_high        numeric,
    planned_entry          numeric,
    planned_stop           numeric,
    planned_target         numeric,
    planned_risk_pct       numeric,
    expected_r             numeric,
    sector                 text,
    market_cap             numeric,
    live_price_at_discovery numeric,
    discovered_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (date, symbol)
);

CREATE TABLE IF NOT EXISTS public.same_day_outcomes (
    id            bigserial PRIMARY KEY,
    date          date NOT NULL,
    symbol        text NOT NULL,
    strategy      text,
    ret_fwd_5d    numeric,
    outcome_win   boolean,
    outcome_loss  boolean,
    expected_r    numeric,
    scored_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (date, symbol)
);

INSERT INTO public.system_config
    (key, value, description, is_secret, category, subsystem,
     value_type, min_value, max_value, default_value, risk_level, deprecated)
VALUES
    ('swing_same_day_discovery_shadow', 'true',
     'Stage 1 of same-day setup discovery — write-only, zero decision '
     'impact. When true, swing/signals/same_day_discovery.py scans watched '
     'symbols for VBD/SBS/RSB triggers not already in today''s evening '
     'plan and writes candidates to swing_same_day_candidates. Ships true '
     '(shadow is the intended default state, unlike Stage 2 below) — this '
     'is what accumulates the evidence Track E Stage E6''s planned '
     'discovery engine needs.',
     false, 'Discovery', 'swing/signals/same_day_discovery.py', 'bool',
     NULL, NULL, 'true', 'LOW', false),

    ('swing_same_day_discovery_enabled', 'false',
     'Stage 2 — when true, today''s not-yet-evening-listed '
     'swing_same_day_candidates rows are merged into evaluate_candidates()''s '
     'working set and flow through the SAME decide()/ranking/allocator '
     'chain evening candidates use. Ships false — arm only once Stage 1''s '
     'shadow evidence (same_day_outcomes) shows the VBD/SBS/RSB same-day '
     'trigger, including its delivery_pct proxy, predicts a real forward '
     'move.',
     false, 'Discovery', 'intraday/engine.py (evaluate_candidates)', 'bool',
     NULL, NULL, 'false', 'HIGH', false)
ON CONFLICT (key) DO NOTHING;
