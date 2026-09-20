-- 20-Sep-2026. What "no opinion" means when the AI writes no chase allowance.
--
-- analysis/trade_decision.decide() reads max_chase_pct=None as NO LIMIT. All
-- three swing call sites resolved the plan's field with
--
--     p.get("ai_max_chase_pct") or None
--
-- and `or None` is false for 0.0, so an explicit 0 — "do not chase this one" —
-- arrived as None and the plan could be bought at any distance above the zone.
-- Since 01-Aug the AI wrote a literal 0 on 306 plans and NULL on 1,526; only
-- the 229 carrying a positive number were honoured. Fixed in code by
-- trade_decision.chase_limit(), which never returns None.
--
-- This key is what NULL resolves to. It is NOT a behaviour experiment: the
-- book-level sweep on the 16-Aug..17-Sep holdout, through current mechanics,
-- shows caps between 1% and 5% are indistinguishable from unlimited on the
-- trades actually taken, because only seven chased entries exist in that
-- window.
--
--   chase cap      holdout sumR    net
--   unlimited          -5.55      -48,426
--   5%  (this)         -5.22      -47,740
--   2%                 -5.09      -46,548
--   0%  (zone only)    -4.08      -44,749
--
-- 5.0 is chosen deliberately as the near-neutral value: the point of this
-- migration is that the policy becomes a switch instead of an accident. A
-- tighter value is a separate decision and needs an n bigger than seven.

INSERT INTO public.system_config (key, value, description)
VALUES ('swing_max_chase_pct', '5.0',
        'Swing: how far above the entry zone a plan may be bought, in percent, '
        'when ai_max_chase_pct is NULL. An explicit ai_max_chase_pct (including '
        '0) always wins. Never unlimited — decide() reads None as no limit.')
ON CONFLICT (key) DO NOTHING;
