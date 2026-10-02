-- Deliberately no destructive rollback. Disable the feature; preserve receipts
-- and source protection. A reviewed disposal procedure is required for removal.
DO $$ BEGIN RAISE EXCEPTION 'provider handoff history is retained; disable the feature instead'; END $$;
