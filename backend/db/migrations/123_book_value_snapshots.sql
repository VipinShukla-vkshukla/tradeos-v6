-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 123: daily book-value snapshots
-- ═══════════════════════════════════════════════════════════════════════════
--
-- 26-Aug-2026. Swing framework evolution blueprint — Daily Summary
-- Dashboard. One row per (date, framework): sleeve, cumulative realized
-- P&L, live unrealized P&L, and the resulting book value at the time this
-- ran. tools/snapshot_book_value.py writes it; the frontend reads the most
-- recent prior row to compute a genuine day-over-day delta rather than
-- approximating "today's change" from today's realized P&L alone.
--
-- No config switch — a pure read/write utility, no live-money behaviour
-- gated by it.

CREATE TABLE IF NOT EXISTS public.book_value_snapshots (
    id               bigserial PRIMARY KEY,
    date             date NOT NULL,
    framework        text NOT NULL CHECK (framework IN ('SWING', 'INTRADAY')),
    sleeve           numeric NOT NULL,
    realized_pnl_cum numeric NOT NULL,
    unrealized_pnl   numeric NOT NULL,
    book_value       numeric NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (date, framework)
);
