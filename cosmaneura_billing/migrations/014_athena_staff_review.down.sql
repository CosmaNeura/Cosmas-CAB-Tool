-- Reviewed expand rung is left dormant on rollback; no patient evidence deleted.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION '014 expand-only: disable Athena review; retain evidence'; END $$;
COMMIT;
