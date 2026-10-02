-- Keep the reader and all source history during application rollback.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION 'retain provider source provenance; revoke model policy and roll application code back'; END $$;
COMMIT;
