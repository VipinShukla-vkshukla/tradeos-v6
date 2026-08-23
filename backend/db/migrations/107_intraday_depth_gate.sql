-- 107_intraday_depth_gate.sql
-- 24-Aug-2026 (Stage D4, docs/TRADEOS_ROADMAP.md Track D, branch
-- feat/intraday-depth-gate)
--
-- Two SEPARATE switches, deliberately -- capture and gate are separate
-- commitments in this codebase (mirrors intraday_quote_mode vs the overlay
-- switches that read it):
--
--   intraday_depth_mode_enabled   -- may the daemon ask Kite for FULL-mode
--                                     (5-level depth) ticks at all, scoped
--                                     to context_symbols() only (positions +
--                                     the live universe, ~40-120 names, never
--                                     the ~270-name bench). Pure capture --
--                                     flips this on and nothing changes
--                                     downstream by itself.
--   overlay_depth_enabled         -- may analysis.overlays.depth_ok() ACT on
--                                     what was captured and refuse an entry.
--                                     Can only ever produce a value when the
--                                     capture switch above is also on and has
--                                     ticked at least once for that symbol --
--                                     otherwise SymbolContext.depth is None
--                                     and depth_ok() waves the trade through,
--                                     by design (capture-side plumbing must
--                                     never be why an entry is blocked).
--
-- Both ship FALSE. This is a NEW live data path (FULL mode was never
-- requested before) sitting in front of a book that decides real paper
-- trades every session; it goes through the same staged-arm discipline as
-- every other switch in this project rather than going live at the moment
-- the code merges.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES
  ('intraday_depth_mode_enabled', 'false',
   'Stage D4: subscribes context_symbols() (positions + live universe) to '
   'Kite FULL mode so SymbolContext.depth carries live 5-level order-book '
   'data. Pure capture -- ships FALSE, and even when true, overlay_depth_ '
   'enabled below is what decides whether anything ACTS on it.',
   'Master controls', 'intraday/price_feed.py', 'bool', 'false', 'LOW'),

  ('overlay_depth_enabled', 'false',
   'Stage D4: analysis.overlays.depth_ok() may refuse an already-decided '
   'intraday entry (BLOCKED_DEPTH) when the live book''s spread is too wide '
   'or its resting depth cannot absorb the planned quantity. Same shape as '
   'BLOCKED_LIQUIDITY/BLOCKED_STRUCTURE -- protection, not prediction. '
   'Advisory-only when no depth data has arrived yet for a symbol, never a '
   'block for lack of capture.',
   'Master controls', 'analysis/overlays.py', 'bool', 'false', 'LOW'),

  ('intraday_max_spread_pct', '0.25',
   'Stage D4: bid/ask spread, as a percent of mid, above which depth_ok() '
   'refuses an entry regardless of size. 0.25% is loose by design -- most '
   'liquid names in the live universe run well under this; it exists to '
   'catch the genuinely thin minute, not to tax the ordinary one.',
   'Master controls', 'analysis/overlays.py', 'float', '0.25', 'LOW'),

  ('intraday_depth_levels_checked', '3',
   'Stage D4: how many resting price levels on the consuming side of the '
   'book (asks for a BUY, bids for a SELL) depth_ok() sums before comparing '
   'against the planned quantity. Top-of-book alone understates what a '
   'market order can actually absorb; summing too many levels overstates '
   'it by counting depth the order would move the price well past.',
   'Master controls', 'analysis/overlays.py', 'int', '3', 'LOW')
ON CONFLICT (key) DO NOTHING;
