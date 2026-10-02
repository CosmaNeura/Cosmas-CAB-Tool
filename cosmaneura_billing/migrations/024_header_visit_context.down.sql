-- Metadata and receipt history remain valid during application rollback.
-- Reverting the reader/UI requires no schema or data removal.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION 'retain optional visit context; rollback application code without deleting source history'; END $$;
COMMIT;
