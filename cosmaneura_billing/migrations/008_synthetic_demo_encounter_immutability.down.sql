-- Remove the database-level reserved-source guards. This migration cannot see
-- safely across forced-RLS practice boundaries to prove that every reserved
-- row is gone. Before running it, close API/worker/sync ingress and scheduling,
-- then use the exact table owner to remove and verify reserved rows separately
-- in every bound practice. Keep ingress closed through rollback of 007. Running
-- this file first can otherwise leave reserved rows without database guards.

BEGIN;

DROP TRIGGER IF EXISTS encounters_synthetic_demo_immutability
    ON public.encounters;

DROP TRIGGER IF EXISTS encounters_synthetic_demo_truncate_immutability
    ON public.encounters;

DROP FUNCTION IF EXISTS public.cosmaneura_guard_synthetic_demo_encounter();

COMMIT;
