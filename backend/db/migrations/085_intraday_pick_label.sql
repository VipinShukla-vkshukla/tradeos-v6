-- 085 — the TOP_PICK / EXPLORATION label. 19-Aug-2026.
--
-- WHY, IN ONE PARAGRAPH
-- ---------------------
-- The operator's ask was specific: keep paper's trade VOLUME high (it is
-- free, and every engine's prior needs volume to stop being noise) while
-- still being able to tell, trade by trade, whether a given entry was a
-- genuine best-of-day pick or was kept mainly to keep a thin prior learning.
-- `alloc_edge_absolute_floor` already makes that distinction internally
-- (floor_only_rank / the EXPLORATION carve-out in engine.allocator_permits),
-- but the label lived only inside allocation_decisions.hurdle_inputs — a JSON
-- blob nobody queries — and was computed from a flat percentile that does not
-- know the hour. This migration adds a real column and a time-aware bar to
-- fill it, and changes NOTHING about which trades are taken: see
-- allocation/hurdle.py's own header on `label_bar` for why it is additive.

-- ── 1. THE COLUMN, ON THE TRADE ITSELF, NOT ON A JSON BLOB ─────────────────
-- On open_positions AND closed_positions (not just intraday_setups) because
-- the operator's own question is about ACTUAL TRADES, and intraday_setups
-- mixes those with every refused detection. close_position() carries it
-- through the same way `sector` already is (control/position_lifecycle.py).
ALTER TABLE open_positions   ADD COLUMN IF NOT EXISTS pick_label TEXT;
ALTER TABLE closed_positions ADD COLUMN IF NOT EXISTS pick_label TEXT;

-- ── 2. THE SWITCH — INERT BY DEFAULT ────────────────────────────────────────
-- Off until the arrival curve (allocation/hurdle.py::arrival_histogram) has
-- something real to read: it is built from TAKEN rows in intraday_setups, and
-- most of that history predates F-33's stop-geometry fix. Arming this before
-- then would label trades against a curve shaped by a strategy the book no
-- longer runs.
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('alloc_intraday_pick_label', 'false',
        'Stamps every TAKEN intraday position TOP_PICK or EXPLORATION, based '
        'on whether its edge cleared a TIME-AWARE bar built from how many more '
        'setups are typically still coming today (allocation/hurdle.py:: '
        'label_quantile). Additive only - it never changes which trades are '
        'taken, only how the ones already taken are labelled, so intraday_max_'
        'new_per_day stays the volume control and this stays the quality '
        'signal. Written onto open_positions/closed_positions.pick_label, not '
        'buried in allocation_decisions.hurdle_inputs. Inert by default: the '
        'arrival curve it reads is still mostly pre-F-33 history.',
        'Master controls', 'allocation/hurdle.py',
        'bool', 'false', 'SAFE')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, min_value, max_value, default_value,
                           risk_level)
VALUES ('alloc_arrival_curve_lookback_days', '20',
        'How many past sessions allocation/hurdle.py::arrival_histogram() '
        'reads to build "how many more setups are typically still coming '
        'today, from this hour on". Re-read and re-cached once per calendar '
        'day, so it tracks how the arrival shape changes as engines change '
        '(e.g. F-33''s stop fix) without a code deploy.',
        'Master controls', 'allocation/hurdle.py',
        'int', 5, 90, '20', 'SAFE')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();
