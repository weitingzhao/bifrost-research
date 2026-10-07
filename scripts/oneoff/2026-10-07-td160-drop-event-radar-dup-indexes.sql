-- TD-160 apply. Own psql session against bifrost_golden_source. No -1:
-- DROP INDEX CONCURRENTLY cannot run inside a transaction block.
--
-- Read 2026-10-07 on the replica. Both old indexes duplicate the current ones
-- (same keys, same predicate, same size class):
--   event_radar_batch_collected              160 kB  (batch_id, collected_at DESC)
--   event_signal_radar_daily_batch_collected 160 kB  same
--   event_radar_importance                   160 kB  (collected_at DESC, importance DESC)
--   event_signal_radar_daily_importance      160 kB  same
-- event_radar_pkey is the primary key. Do not drop it.
--
-- Before: scripts/oneoff/2026-10-07-td160-event-radar-index-check.sql
-- Rollback: scripts/oneoff/2026-10-07-td160-drop-event-radar-dup-indexes-rollback.sql

DROP INDEX CONCURRENTLY IF EXISTS features.event_radar_batch_collected;
DROP INDEX CONCURRENTLY IF EXISTS features.event_radar_importance;
