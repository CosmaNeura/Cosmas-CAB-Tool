-- 003_pilot_persistence.down.sql
-- Destructive rollback for P3-owned tables. The 001 pgvector and 002 chase
-- tables and their data are preserved; only the P3 chase FK and RLS controls
-- are removed from those existing tables.

BEGIN;

DROP VIEW IF EXISTS adaptive_preference_source;

DROP FUNCTION IF EXISTS cosmaneura_feedback_reviewer_allowed(uuid, uuid);
DROP FUNCTION IF EXISTS cosmaneura_staff_assignee_allowed(uuid, uuid);

DROP POLICY IF EXISTS chase_action_receipts_practice_isolation
    ON chase_action_receipts;
ALTER TABLE chase_action_receipts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chase_action_receipts DISABLE ROW LEVEL SECURITY;
DROP TABLE IF EXISTS chase_action_receipts;

DROP POLICY IF EXISTS chase_resolutions_practice_isolation
    ON chase_resolutions;
ALTER TABLE chase_resolutions NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chase_resolutions DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chase_pending_tasks_practice_isolation
    ON chase_pending_tasks;
ALTER TABLE chase_pending_tasks NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chase_pending_tasks DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chase_attempts_practice_isolation
    ON chase_attempts;
ALTER TABLE chase_attempts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chase_attempts DISABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chase_items_practice_isolation ON chase_items;
ALTER TABLE chase_items NO FORCE ROW LEVEL SECURITY;
ALTER TABLE chase_items DISABLE ROW LEVEL SECURITY;

ALTER TABLE chase_items DROP CONSTRAINT IF EXISTS chase_items_practice_fk;
ALTER TABLE chase_items DROP COLUMN IF EXISTS chase_uid;

DROP TABLE IF EXISTS access_audit_completions;
DROP TABLE IF EXISTS access_audit_attempts;
DROP TABLE IF EXISTS coder_feedback_decisions;
DROP TABLE IF EXISTS coder_feedback;
DROP TABLE IF EXISTS encounters;
DROP TABLE IF EXISTS practice_memberships;
DROP TABLE IF EXISTS users;
DROP POLICY IF EXISTS practices_practice_isolation ON practices;
DROP FUNCTION IF EXISTS cosmaneura_practice_allowed(text);
DROP TABLE IF EXISTS runtime_practice_bindings;
DROP TABLE IF EXISTS practices;

COMMIT;
