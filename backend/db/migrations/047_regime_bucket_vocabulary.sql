-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v7 — Migration 047: regime_bucket() could never reach STRONG
-- ═══════════════════════════════════════════════════════════════════════════
--
-- Code-only defect, no schema change — this migration exists to record it in
-- the same sequence as the fix and give tools.health a version to check the
-- verdict was applied against.
--
-- WHAT WAS WRONG
--
-- allocation/hurdle.py::regime_bucket() was written to match the SWING regime
-- engine's states (compute_regime.py, market_regime table): TRENDING, RISK ON,
-- NEUTRAL, RECOVERING, RISK OFF — SPACE-separated, once a day, with hysteresis.
--
-- Its only live caller, intraday/engine.py's _allocate_shadow, has always
-- passed regime=mc.state — the INTRADAY market context's states
-- (intraday/market_context.py): RISK_ON, NEUTRAL, CAUTION, RISK_OFF —
-- UNDERSCORE-separated, recomputed every 15 seconds from the index alone.
--
-- "RISK ON" in "RISK_ON" is False. So for as long as this function has
-- existed, regime_bucket() returned WEAK on every real call — the STRONG
-- branch was unreachable from production. Migration 044's regime segmentation
-- (STRONG vs WEAK arrival distributions for the allocator's bar) was silently
-- pooling everything into WEAK, defeating the point of the split without
-- breaking anything loudly enough to be noticed in a session or two.
--
-- A second, independent defect sat in the same line: even the SWING vocabulary
-- fed in correctly, "TRENDING" — the regime engine's own strongest state —
-- does not contain the substring "RISK ON" and also returned WEAK.
--
-- FOUND DURING A TERMINOLOGY AUDIT, NOT A BUG REPORT
--
-- The operator asked a plain question — "what does RISK OFF mean, and is it
-- being read consistently across my scripts?" — and this is what answering it
-- honestly turned up. At least three unrelated vocabularies in this codebase
-- use overlapping words for different concepts:
--
--   swing regime        TRENDING / RISK ON / NEUTRAL / RECOVERING / RISK OFF
--                       (space-separated, daily, market_regime table)
--   intraday context     RISK_ON / NEUTRAL / CAUTION / RISK_OFF
--                       (underscore, every 15s, index-only, never persisted)
--   sector event bias    NEGATIVE / RISK_OFF / BEARISH / ...
--                       (underscore, per-sector, from ai_decision_engine's
--                        event classifier — a different axis entirely: this
--                        is about ONE SECTOR's news, not the whole market)
--
-- Only the first collision (regime_bucket) was a live defect. The others are
-- documented in docs/TERMINOLOGY.md so a future reader does not spend an
-- afternoon re-discovering that "RISK_OFF" means three different things
-- depending which file printed it.
--
-- THE FIX
--
-- regime_bucket() now names every value each real classifier can actually
-- emit, explicitly, instead of pattern-matching a substring against a fuzzy
-- superset. Two closed sets, not one leaky one. Verified against the literal
-- return values of compute_regime.py::apply_hysteresis() and
-- market_context.py::classify() — not against invented strings.
--
-- health's `hurdle` check gained a fourth assertion: regime_bucket("RISK_ON")
-- must be STRONG (the actual production call) and regime_bucket("TRENDING")
-- must be STRONG (the swing engine's own strongest state). Demonstrated
-- failing against the pre-fix function before being demonstrated passing
-- against the corrected one.
--
-- alerts/send_alerts.py::regime_icon() had the same shape of drift, cosmetic
-- rather than load-bearing: RECOVERING (a real, live state) had no icon and
-- fell through to ❓ every time the market was in it; CAUTION and BULLISH
-- (neither ever a live swing-regime value) had icons that could never be
-- shown. Fixed to cover exactly the five real states.
-- ═══════════════════════════════════════════════════════════════════════════

DO $$
BEGIN
  RAISE NOTICE '';
  RAISE NOTICE 'Migration 047: no schema change. Code fix in allocation/hurdle.py';
  RAISE NOTICE '(regime_bucket) and alerts/send_alerts.py (regime_icon).';
  RAISE NOTICE '';
  RAISE NOTICE 'Verify with:';
  RAISE NOTICE '    python -m tools.health         -> the `hurdle` row';
  RAISE NOTICE '    python -c "from allocation.hurdle import regime_bucket as r; '
               'print(r(''RISK_ON''), r(''TRENDING''))"   -> STRONG STRONG';
  RAISE NOTICE '';
  RAISE NOTICE 'Reference: docs/TERMINOLOGY.md — every regime/state vocabulary in';
  RAISE NOTICE 'this system, where each is defined, and who is allowed to read it.';
  RAISE NOTICE '';
END $$;
