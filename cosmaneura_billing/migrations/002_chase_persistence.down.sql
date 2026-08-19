-- 002_chase_persistence.down.sql
-- Destructive rollback for the durable insurance-data chase.

BEGIN;

DROP TABLE IF EXISTS chase_resolutions;
DROP TABLE IF EXISTS chase_pending_tasks;
DROP TABLE IF EXISTS chase_attempts;
DROP TABLE IF EXISTS chase_items;

COMMIT;
