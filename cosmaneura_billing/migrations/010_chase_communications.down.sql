-- Recovery normally leaves this additive structure dormant. Destructive removal
-- is allowed only before any communications evidence exists, across all practices.
BEGIN;
SET LOCAL row_security = off;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM chase_communication_senders)
       OR EXISTS (SELECT 1 FROM chase_communication_bindings)
       OR EXISTS (SELECT 1 FROM chase_communication_operations)
       OR EXISTS (SELECT 1 FROM chase_communication_events)
       OR EXISTS (SELECT 1 FROM chase_communication_suppressions) THEN
        RAISE EXCEPTION 'communications evidence exists; leave migration 010 dormant';
    END IF;
END
$$;
DROP TABLE chase_communication_suppressions;
DROP TABLE chase_communication_events;
DROP TABLE chase_communication_operations;
DROP TABLE chase_communication_bindings;
DROP TABLE chase_communication_senders;
DROP FUNCTION cosmaneura_validate_communication_evidence();
DROP FUNCTION cosmaneura_reject_communication_mutation();
COMMIT;
