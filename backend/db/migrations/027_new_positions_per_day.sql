-- 027_new_positions_per_day.sql
--
-- Make the cap that actually limits new positions visible and editable.
--
-- WHY
-- ---
-- Two different caps were being confused, because only one of them existed
-- anywhere you could see:
--
--   swing_max_orders_per_day   how many ORDERS preflight will let through.
--                              Counts BUYs. A rail against a runaway loop.
--   swing_max_new_per_day      how many NEW POSITIONS the strategy opens.
--                              A portfolio decision, not a safety rail.
--
-- The second was read by control/paper_entry.py and intraday/engine.py with a
-- hardcoded default of 2 and no row in system_config at all — so raising the
-- visible "Orders/day" to 4 changed nothing about how many positions get taken.
-- The stricter cap binds, and the stricter one was invisible.

INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('swing_max_new_per_day', '2',
        'How many NEW swing positions may be opened in one day. Distinct from '
        'swing_max_orders_per_day, which caps orders placed. The stricter of the '
        'two binds. Read by control/paper_entry.py and '
        'intraday/engine.py::_maybe_enter_swing.',
        'RISK', 'CRITICAL')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description;

INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('swing_alert_top_n', '5',
        'How many of the day''s ranked plans may raise an alert. The price feed '
        'still carries every non-SKIP plan; this bounds only what interrupts '
        'you. Read by intraday/engine.py::_swing_contenders.',
        'ALERTS', 'NORMAL')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description;

INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('swing_replace_margin', '20',
        'Rank points by which a new plan must beat the weakest held position '
        'before it is surfaced as a SWAP_CANDIDATE. Below this, churning pays a '
        'round trip and a fresh stop for no edge.',
        'ALERTS', 'NORMAL')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description;

-- ── Intraday parity ────────────────────────────────────────────────────────
-- Same two concepts, same names, so one mental model covers both frameworks.
INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('intraday_max_new_per_day', '4',
        'How many NEW intraday positions may be opened in one day. Distinct '
        'from intraday_max_orders_per_day, which caps orders placed. Read by '
        'intraday/engine.py::_maybe_open_paper.',
        'RISK', 'CRITICAL')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description;

-- NOTE: this behaves differently from swing_alert_top_n, and deliberately.
-- Swing ranks a FIXED field — last night's plans and their scores — so the
-- day's top five is knowable at 09:15. An intraday setup exists only once price
-- has made it, so the day's best cannot be known until the day is over. This is
-- therefore a STREAMING top-N: alert freely until N have been sent, then only
-- for a setup that beats the weakest already sent. The bar rises through the
-- session instead of being fixed in advance.
INSERT INTO system_config (key, value, description, category, risk_level)
VALUES ('intraday_alert_top_n', '5',
        'How many intraday setups may alert per session. Streaming top-N: once '
        'N are sent, only a setup beating the weakest of them alerts. Read by '
        'intraday/engine.py::_intraday_alert_worthy.',
        'ALERTS', 'NORMAL')
ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description;
