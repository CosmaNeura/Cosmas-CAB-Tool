-- Development-only reversal of an EMPTY rung. Keep evidence when disabling.
BEGIN;
SET LOCAL row_security=off;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM chase_communication_cancellations) THEN
        RAISE EXCEPTION 'communications cancellation evidence exists; leave expand rung dormant';
    END IF;
END $$;
DROP TRIGGER start_unless_canceled ON chase_communication_dispatch_starts;
DROP TABLE chase_communication_cancellations;
DROP FUNCTION cosmaneura_guard_communication_cancellation();
COMMIT;
