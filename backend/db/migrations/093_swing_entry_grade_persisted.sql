-- 093_swing_entry_grade_persisted.sql
-- 24-Aug-2026 (F-68 follow-up, Track E Stage E2 -> E6 prerequisite)
--
-- ai/post_trade_analysis.py::grade_trade_entry() has computed an A-F grade
-- for every analysed closed trade since this file existed — confirmed by
-- reading the code, not assumed. The grade was used ONLY to word the
-- generated lesson's prose (generate_rule_based_lesson()) and tallied into
-- a run-level grade_dist {"A":0,...} log line, then discarded. Checked
-- directly: the `lessons` table (19 columns) has never had a grade field
-- of any kind. Stage E2's own quantify pass (F-68) went looking for
-- whether the lesson engine's grade predicts forward outcome and found the
-- question unanswerable for exactly this reason — there was nothing to
-- correlate against.
--
-- SWING ONLY, deliberately narrower than the pre-existing loop's own
-- scope. main() in post_trade_analysis.py already processes BOTH
-- frameworks' closed trades without discriminating (an intraday row with
-- no signal_log match falls through grade_trade_entry()'s own
-- `if not signal_ctx: return "C"` default) — pre-existing behaviour, not
-- introduced here. The new write-back is scoped to framework='SWING' only,
-- a deliberate choice to keep this addition strictly within Track E's own
-- charter even though the surrounding loop is not itself framework-aware.
ALTER TABLE closed_positions
  ADD COLUMN IF NOT EXISTS entry_grade text;

COMMENT ON COLUMN closed_positions.entry_grade IS
  'A/B/C/D/F, from ai.post_trade_analysis.grade_trade_entry() at the time '
  'the trade was analysed. SWING rows only. Written going forward by '
  'main()''s own loop; backfilled once for existing closed trades by '
  'ai.post_trade_analysis.backfill_entry_grades() — see F-68 follow-up, '
  'docs/FINDINGS.md.';
