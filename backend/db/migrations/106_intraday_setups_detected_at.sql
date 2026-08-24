-- 106_intraday_setups_detected_at.sql
-- 24-Aug-2026 (Stage D3, docs/TRADEOS_ROADMAP.md Track D)
--
-- Found while building tools/event_core_compare.py for Gate D3's own
-- stated criterion, "measured latency improvement in seconds": intraday_
-- setups carries scored_at (when the OUTCOME was scored, end of day) but
-- NO detection timestamp at all -- there was never a way to know WHEN
-- the trusted polling loop first saw a setup, only that it did, on some
-- trade_date. Without this, "how many seconds sooner did the event-
-- driven shadow core react" is not a measurable comparison, it is a
-- guess -- the same "a check that cannot fail is not a check" principle
-- applied to a comparison instead of a gate.
--
-- Nullable, no default, no backfill. Existing rows honestly have no
-- detection time on record and are not getting one invented; every row
-- written from this migration forward will.
ALTER TABLE intraday_setups
  ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ;
