-- TD-160 rollback: put the pre-rename indexes back. Own psql session. No -1.
-- Definitions copied from pg_get_indexdef on 2026-10-07.

CREATE INDEX CONCURRENTLY IF NOT EXISTS event_radar_batch_collected
    ON features.event_signal_radar_daily (batch_id, collected_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS event_radar_importance
    ON features.event_signal_radar_daily (collected_at DESC, importance DESC);
