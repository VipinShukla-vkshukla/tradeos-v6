-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 120: zone-relative dynamic entry slip (Phase 3a)
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. Swing framework evolution blueprint, Phase 3a. RKFORGE
-- complaint: `_maybe_enter_swing` pays the same `swing_entry_slip_bps`
-- premium whether the live price sits at the favourable edge of the plan's
-- own entry zone (plenty of runway, no reason to chase) or right at the
-- unfavourable edge next to max_entry (no room left to be patient). See
-- intraday/engine.py::_zone_aware_slip_bps() for the pure math.
--
-- Ships shadow-first: swing_entry_slip_zone_aware is 'false', so today's
-- flat behaviour is unchanged. The zone-aware figure is logged every live
-- entry cycle regardless of the switch, so real evidence accumulates
-- before arming.
--
-- Idempotent — ON CONFLICT DO NOTHING, safe to re-run.

INSERT INTO public.system_config
    (key, value, description, is_secret, category, subsystem,
     value_type, min_value, max_value, default_value, risk_level, deprecated)
VALUES
    ('swing_entry_slip_zone_aware', 'false',
     'When true, the live swing entry chase limit prices its slip off where '
     'ltp sits inside the plan''s own entry zone — tight near the favourable '
     'edge, swing_entry_slip_bps near the unfavourable edge close to '
     'max_entry — instead of always paying the flat swing_entry_slip_bps '
     'premium. Shadow-logged unconditionally regardless of this switch; arm '
     'only once the logged evidence shows real fill-price improvement.',
     false, 'Execution', 'intraday/engine.py (_maybe_enter_swing)', 'bool',
     NULL, NULL, 'false', 'MEDIUM', false),

    ('swing_entry_slip_tight_bps', '5',
     'The slip (in basis points) used at the FAVOURABLE edge of the entry '
     'zone when swing_entry_slip_zone_aware is on. swing_entry_slip_bps '
     'remains the figure used at the unfavourable edge and is what '
     'swing_entry_slip_zone_aware=false always uses.',
     false, 'Execution', 'intraday/engine.py (_zone_aware_slip_bps)', 'int',
     0, NULL, '5', 'MEDIUM', false)
ON CONFLICT (key) DO NOTHING;
