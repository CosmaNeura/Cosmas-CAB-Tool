-- 004_chase_delivery_runtime.down.sql
-- Destructive rollback for P4-owned delivery-runtime tables only. P3 practices,
-- identities, chase items, attempts, tasks, resolutions, and receipts survive.

BEGIN;

DROP TRIGGER IF EXISTS chase_attempts_accepted_delivery_guard ON chase_attempts;

ALTER TABLE chase_attempts
    DROP CONSTRAINT IF EXISTS chase_attempts_delivery_fk,
    DROP CONSTRAINT IF EXISTS chase_attempts_delivery_unique,
    DROP COLUMN IF EXISTS delivery_id;

DROP TABLE IF EXISTS chase_delivery_events;
DROP TABLE IF EXISTS chase_delivery_outbox;
DROP TABLE IF EXISTS chase_cadence_states;
DROP TABLE IF EXISTS chase_active_policies;
DROP TABLE IF EXISTS chase_review_releases;
DROP TABLE IF EXISTS chase_message_releases;
DROP TABLE IF EXISTS chase_policy_approval_events;
DROP TABLE IF EXISTS chase_policy_steps;
DROP TABLE IF EXISTS chase_policy_revisions;
DROP TABLE IF EXISTS chase_contact_routes;
DROP TABLE IF EXISTS chase_contact_snapshots;

DROP FUNCTION IF EXISTS cosmaneura_chase_admin_allowed(uuid, uuid);
DROP FUNCTION IF EXISTS cosmaneura_require_accepted_delivery_attempt();
DROP FUNCTION IF EXISTS cosmaneura_validate_delivery_outbox_update();
DROP FUNCTION IF EXISTS cosmaneura_lock_policy_revision_for_approval();
DROP FUNCTION IF EXISTS cosmaneura_reject_approved_policy_step_insert();
DROP FUNCTION IF EXISTS cosmaneura_reject_immutable_chase_mutation();

COMMIT;
