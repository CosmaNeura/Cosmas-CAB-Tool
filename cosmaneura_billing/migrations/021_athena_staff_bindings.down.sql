-- Identity history and revocations must survive code rollback. Keep dormant.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION 'Athena binding rollback requires separately reviewed recovery; retain registration history'; END $$;
COMMIT;
