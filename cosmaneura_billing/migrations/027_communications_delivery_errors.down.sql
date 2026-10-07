-- Earlier applications tolerate the nullable column; preserve immutable history.
BEGIN;
DO $$ BEGIN RAISE EXCEPTION 'retain delivery error evidence; roll back application code without deleting callback history'; END $$;
COMMIT;
