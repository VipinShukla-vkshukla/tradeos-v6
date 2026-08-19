-- 087 — SDN routed through base.risk_from_structure(). 19-Aug-2026.
--
-- Found during a full engine audit: SDN was the one engine of nine that built
-- its own stop directly instead of calling the shared primitive every sibling
-- engine uses. Consequence, real and live: F-33's anti-falsification fix and
-- intraday_min_risk_pct (armed the same session) both live inside that
-- function, so SDN — carrying the large majority of this book's live volume —
-- was exempt from both, by construction, not by choice. Not urgent by SDN's
-- own history (7 of 265 TAKEN rows ever sat under 0.6% risk), but real: any
-- future tightening of the shared stop logic would keep silently missing this
-- engine. Fixed by routing all three conditions (_vwap_rejection, _trap,
-- _range_breakdown) through risk_from_structure(). One side fix landed with
-- it: _trap's old stop applied a second buffer to the prev_high branch
-- whenever it won its own min() — small (buf is 0.12%) and now applied
-- exactly once, matching what the comment at that stop has always said.

INSERT INTO system_config (key, value, description, category, subsystem,
                           value_type, min_value, max_value, default_value,
                           risk_level)
VALUES ('intraday_short_max_risk_pct', '1.50',
        'The maximum-risk cap SDN never had, now enforced through the same '
        'base.risk_from_structure() every other engine uses. Set ABOVE SDN''s '
        'own empirically best band (n=80, +0.442R, >=0.9%% risk) deliberately, '
        'so it protects against a genuinely broken detection without cutting '
        'SDN''s strongest cohort. Under current settings this is mostly a '
        'SECONDARY safety net: intraday_short_min_rr (1.3) combined with '
        '_target()''s ATR-capped reward already refuses anything wider than '
        'roughly 0.9%% risk before this cap gets a chance to bind - real '
        'defense in depth, not the first gate a wide SDN stop meets today. '
        'intraday/strategies/short_distribution.py.',
        'Master controls', 'intraday/strategies/short_distribution.py',
        'float', 0, 5, '1.50', 'MEDIUM')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description,
                                updated_at = now();

-- intraday_min_risk_pct needs no new row here: it is the SAME shared key
-- armed earlier this session (0.6), and this migration is what makes SDN
-- reach it for the first time. Nothing to insert; recorded for the reader
-- who greps this file for "min_risk_pct" and expects to find it.
