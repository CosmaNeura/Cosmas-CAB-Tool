BEGIN;
-- Fail closed if forced RLS would hide rows from the migration operator.
SET LOCAL row_security=off;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM chase_communication_event_reviews) THEN
        RAISE EXCEPTION 'communications review history exists; leave expand rung dormant';
    END IF;
END $$;
DROP TRIGGER review_evidence ON chase_communication_event_reviews;
DROP FUNCTION cosmaneura_validate_communication_review();
DROP VIEW chase_communication_review_sources;
DROP TABLE chase_communication_event_reviews;
COMMIT;
