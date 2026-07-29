-- 025_trade_charges.sql
--
-- Record what a trade actually cost, so P&L is the number that reaches the
-- account rather than the number before the broker takes its share.
--
-- WHY
-- ---
-- paper_broker computed charges on every simulated fill and then threw them
-- away — they appeared in a Telegram string and nowhere else. realized_pnl has
-- always been gross: (exit - entry) * qty.
--
-- For swing that is a small distortion. For INTRADAY it is not. A round trip
-- costs about 0.21% of position value; on a Rs 5,000 intraday position that is
-- roughly Rs 10.50 against a 1% target worth Rs 50 gross. Paper would report
-- ~20% more profit than the account would ever see, in exactly the direction
-- that matters when deciding whether to promote intraday to live.
--
-- WHY realized_pnl IS NOT REDEFINED
-- ----------------------------------
-- 70 closed trades already exist with gross values and no way to reconstruct
-- their charges. Silently changing what the column means would make old and new
-- rows incomparable while looking identical. So charges are recorded alongside,
-- NULL where unknown, and net is computed where it can be.

ALTER TABLE open_positions
    ADD COLUMN IF NOT EXISTS charges NUMERIC;

COMMENT ON COLUMN open_positions.charges IS
    'Statutory + brokerage cost of the ENTRY leg, in rupees. Slippage is '
    'excluded because it is already reflected in entry_price. Written by '
    'execution/paper_broker.py on open.';

ALTER TABLE closed_positions
    ADD COLUMN IF NOT EXISTS charges NUMERIC;

COMMENT ON COLUMN closed_positions.charges IS
    'Full round-trip statutory + brokerage cost in rupees, entry leg plus exit '
    'leg. NULL on trades closed before this migration — their charges cannot be '
    'reconstructed. Net P&L is realized_pnl - charges wherever this is not NULL; '
    'realized_pnl itself stays GROSS so old and new rows keep the same meaning.';

CREATE INDEX IF NOT EXISTS idx_closed_positions_mode_exit
    ON closed_positions (mode, exit_date DESC);
