-- Preserve registrations, revocations and accepted provenance. Roll back code
-- with handoff disabled; removing an admission boundary is not safe rollback.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION 'provider admission rollback requires a separately reviewed recovery; retain registry history'; END $$;
COMMIT;
