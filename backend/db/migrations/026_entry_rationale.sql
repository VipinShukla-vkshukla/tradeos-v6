-- 026_entry_rationale.sql
--
-- Record WHY a position was chosen, on the position itself.
--
-- WHY
-- ---
-- The daily cap means auto-entry picks two names out of eight buyable plans.
-- Which two, and why, was decided by analysis/entry_ranking.py and then thrown
-- away — the trade appeared in the book with no trace of what beat what.
--
-- That matters twice. When a trade goes wrong the useful question is whether
-- the reasoning was wrong or the market was, and that is unanswerable without
-- the reasoning. And when the ranking weights are tuned, the only honest test is
-- whether the names it preferred actually did better, which needs the score
-- recorded at entry rather than recomputed later from data that has moved.

ALTER TABLE open_positions
    ADD COLUMN IF NOT EXISTS entry_rationale TEXT;

COMMENT ON COLUMN open_positions.entry_rationale IS
    'Why this plan was chosen over the others available that day: the composite '
    'rank and its largest components. Written by control/paper_entry.py from '
    'analysis/entry_ranking.py. NULL for positions entered manually or before '
    'ranking existed.';

ALTER TABLE closed_positions
    ADD COLUMN IF NOT EXISTS entry_rationale TEXT;

COMMENT ON COLUMN closed_positions.entry_rationale IS
    'Carried through from open_positions at close, so the outcome can be read '
    'against the reasoning that produced it.';
