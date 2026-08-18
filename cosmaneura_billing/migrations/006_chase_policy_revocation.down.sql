-- 006_chase_policy_revocation.down.sql
-- Roll back the narrow revocation capability. This rollback can only restore
-- the former unconditional uniqueness constraints when no replacement rows
-- were created after a cancelled/permanent-failure reservation.

BEGIN;

DROP FUNCTION IF EXISTS cosmaneura_cancel_stale_chase_work(text);
DROP FUNCTION IF EXISTS cosmaneura_cancel_revoked_chase_policy(uuid, text);

DROP INDEX IF EXISTS chase_delivery_outbox_live_item_step_unique;
DROP INDEX IF EXISTS chase_delivery_outbox_live_item_local_day_unique;

ALTER TABLE chase_delivery_outbox
    ADD CONSTRAINT chase_delivery_outbox_item_step_unique
        UNIQUE (
            practice_id, chase_id, policy_revision_id, policy_step_ordinal
        ),
    ADD CONSTRAINT chase_delivery_outbox_item_local_day_unique
        UNIQUE (practice_id, chase_id, local_delivery_date);

COMMIT;
