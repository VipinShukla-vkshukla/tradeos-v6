-- 15-Sep-2026. Core-thesis reconsideration, not another parameter nudge.
--
-- IGN's own full resolved lifetime (n=789, cost_verdict=TAKEN,
-- 09-Sep..15-Sep -- every real detection this engine has ever made, not a
-- cherry-picked slice) splits by hour-of-day into a large, clean,
-- monotonic effect:
--
--   OPEN  (09:15-10:00)  n=361  win 34.6%  mean -0.43%  avg net -Rs61.62/trade
--   MID   (10:00-13:00)  n=335  win 60.6%  mean +0.22%  avg net  +Rs2.20/trade
--   LATE  (13:00-15:15)  n= 93  win 75.3%  mean +0.68%  avg net +Rs45.70/trade
--   ALL             n=789  win 50.4%  mean -0.02%  avg net -Rs21.87/trade
--   MID+LATE only   n=428  win 63.8%                 avg net +Rs11.65/trade
--
-- The engine's entire negative expectancy is concentrated in the 46% of
-- its own detections that fire in the first 45 minutes it is allowed to
-- trade. Removing that slice alone swings pooled average net P&L from
-- -Rs21.87/trade to +Rs11.65/trade on n=428 -- well past this project's
-- own n=100 sufficiency bar, and a materially larger, cleaner sample than
-- migration 138's n=52 (which a real single-day rupee check reverted,
-- migration 140).
--
-- WHY THIS IS A GATE, NOT A THRESHOLD. The opening hour is uniformly
-- documented, in this codebase's own words, as the worst-quality window
-- of the session (intraday/session.py: "Maximum volatility, widest
-- spreads, thinnest books... the opening range forms here -- it is
-- INPUT, not a signal"). A magnitude+volume trigger with no structural
-- anchor at all (IGN's own module docstring: the one engine deliberately
-- built to fire on "a move that was never compressed") has nothing else
-- to distinguish a genuine breakout from opening-auction noise in this
-- window -- every other engine already has SOME structural anchor
-- (opening range, gap-and-hold, prior-day level, compression release);
-- IGN's only available lever here is WHEN it is allowed to look at all.
--
-- Implemented in intraday/strategies/ignition.py::evaluate(), reading
-- ctx.as_of (correct in both live and replay, never the wall clock) via
-- intraday.session.hour_bucket() -- the exact boundaries
-- feature_edge_study.py's own OPEN/MID/LATE split already used,
-- canonicalised there 12-Sep-2026 for the priority-criteria fix. Ships
-- ARMED. One config row away from reversible if real trades disagree,
-- same as every other IGN-specific switch.
INSERT INTO public.system_config
  (key, value, description, category, subsystem, value_type, default_value, risk_level)
VALUES
  ('ign_exclude_open_hour_enabled', 'true',
   'IGN core-thesis gate, 15-Sep-2026: refuses every candidate detected '
   'in the 09:15-10:00 OPEN bucket outright, regardless of magnitude or '
   'volume. IGN''s own full resolved lifetime (n=789) shows this window '
   'alone accounts for its entire negative expectancy -- 34.6% win, avg '
   'net -Rs61.62/trade, vs 63.8% win / +Rs11.65/trade for everything '
   'after 10:00 (n=428). See intraday/strategies/ignition.py::evaluate() '
   'for exactly where this is checked and docs/FINDINGS.md, 15-Sep-2026, '
   'for the full numbers. false restores the pre-15-Sep behaviour exactly.',
   'Intraday engines', 'intraday/strategies/ignition.py', 'bool', 'true', 'MEDIUM')
ON CONFLICT (key) DO NOTHING;
