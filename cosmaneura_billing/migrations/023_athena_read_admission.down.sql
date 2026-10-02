-- Retain admission/operational evidence and its guards; dark rollback uses flags.
DO $$ BEGIN RAISE EXCEPTION 'Athena read admission requires forward recovery; disable configured mode'; END $$;
