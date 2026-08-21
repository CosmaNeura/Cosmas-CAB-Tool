-- Enforce the reserved synthetic-demo provenance below the application layer.
-- Existing rows are untouched. Only the exact encounters table owner may
-- create, change, or remove rows carrying the fixed seed source or truncate
-- the encounters table.

BEGIN;

CREATE OR REPLACE FUNCTION public.cosmaneura_guard_synthetic_demo_encounter()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $$
DECLARE
    table_owner name;
    reserved_source constant text := 'synthetic_demo_release_candidate';
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        NULL;
    ELSIF TG_OP = 'INSERT' THEN
        IF NEW.source IS DISTINCT FROM reserved_source THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.source IS DISTINCT FROM reserved_source
           AND NEW.source IS DISTINCT FROM reserved_source THEN
            RETURN NEW;
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.source IS DISTINCT FROM reserved_source THEN
            RETURN OLD;
        END IF;
    ELSE
        RAISE EXCEPTION 'unsupported synthetic demo guard operation'
            USING ERRCODE = 'check_violation';
    END IF;

    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
      INTO table_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF table_owner IS NULL OR current_user IS DISTINCT FROM table_owner THEN
        RAISE EXCEPTION
            'reserved synthetic demo encounters require the table owner'
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'TRUNCATE' THEN
        RETURN NULL;
    ELSIF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_guard_synthetic_demo_encounter()
    FROM PUBLIC;

DROP TRIGGER IF EXISTS encounters_synthetic_demo_immutability
    ON public.encounters;

CREATE TRIGGER encounters_synthetic_demo_immutability
BEFORE INSERT OR UPDATE OR DELETE ON public.encounters
FOR EACH ROW
EXECUTE FUNCTION public.cosmaneura_guard_synthetic_demo_encounter();

ALTER TABLE public.encounters
    ENABLE ALWAYS TRIGGER encounters_synthetic_demo_immutability;

DROP TRIGGER IF EXISTS encounters_synthetic_demo_truncate_immutability
    ON public.encounters;

CREATE TRIGGER encounters_synthetic_demo_truncate_immutability
BEFORE TRUNCATE ON public.encounters
FOR EACH STATEMENT
EXECUTE FUNCTION public.cosmaneura_guard_synthetic_demo_encounter();

ALTER TABLE public.encounters
    ENABLE ALWAYS TRIGGER encounters_synthetic_demo_truncate_immutability;

COMMIT;
