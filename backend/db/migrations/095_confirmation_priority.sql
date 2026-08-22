-- 095_confirmation_priority.sql
-- 22-Aug-2026 (F-47)
--
-- alloc_intraday_confirmation_priority — a same-engine TIE-BREAK in
-- allocation/policies.py::_interleave_by_engine. When two candidates from
-- the same engine carry the same edge (the common case — edge is prior-
-- keyed, so same-engine candidates in one cycle differ only by cost_r),
-- prefer the one whose own detection confirmed itself (currently: ORB's
-- retest_confirmed, F-37) over one that didn't. Never changes edge, never
-- changes which bar a proposal must clear, never admits or declines
-- anything the plain edge sort would not have — only decides which of
-- several already-tied candidates gets a shared slot.
--
-- DEFAULT TRUE, unlike every other new rule this session
-- (intraday_giveback_pct's own history, intraday_volume_decay_enabled).
-- Those ship inert because they can ADMIT or DECLINE a trade with zero
-- calibration behind the threshold; this cannot, by construction — it
-- only reorders ties. It also has real, if thin, evidence behind it
-- already: of 21-Aug's 6 unconfirmed ORB trades, 0 won; the 1 confirmed
-- trade (POWERGRID) closed +1.65R, and the broader post-18-Aug sample
-- agrees in direction (33% win / +0.18% mean vs 0% / -0.45%, n=7 vs 17).
INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, default_value, risk_level)
VALUES ('alloc_intraday_confirmation_priority', 'true',
        'Same-engine tie-break in the exploration queue: prefer a '
        'candidate whose own detection confirmed itself (ORB''s '
        'retest_confirmed today) over one that did not, when edge alone '
        'cannot separate them. Never changes edge or the bar -- only '
        'which of several tied candidates gets a shared slot. '
        'allocation/policies.py::_confirmation_key.',
        'Master controls', 'allocation/policies.py',
        'bool', 'true', 'LOW')
ON CONFLICT (key) DO NOTHING;
