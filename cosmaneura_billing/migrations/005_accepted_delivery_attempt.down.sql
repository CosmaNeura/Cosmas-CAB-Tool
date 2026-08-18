-- 005_accepted_delivery_attempt.down.sql
-- Remove only the narrow accepted-delivery contact-log capability.

BEGIN;

DROP FUNCTION IF EXISTS cosmaneura_record_accepted_delivery_attempt(uuid);

COMMIT;
