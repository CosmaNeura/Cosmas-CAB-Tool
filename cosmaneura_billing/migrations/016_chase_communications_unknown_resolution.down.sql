-- Empty local rehearsal only. Operational rollback retains investigation evidence.
BEGIN;
SET LOCAL row_security=off;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM chase_communication_unknown_resolutions) THEN
        RAISE EXCEPTION 'communications investigation evidence exists; leave expand rung dormant';
    END IF;
END $$;
DROP TRIGGER unknown_hold ON chase_communication_operations;
DROP TRIGGER unknown_hold ON chase_communication_authorizations;
DROP TRIGGER unknown_hold ON chase_communication_dispatch_starts;
DROP TRIGGER receipt_after_investigation_guard ON chase_communication_dispatch_receipts;
DROP TABLE chase_communication_unknown_resolutions;
DROP FUNCTION cosmaneura_guard_unknown_communication();
COMMIT;
