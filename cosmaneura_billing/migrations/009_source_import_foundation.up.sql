-- Shared, source-agnostic import state with an Athena Preview registration.
-- The import login is provisioned after this migration by the locked runner;
-- SQL here owns provenance, RLS, replay identity, and append-only history.

BEGIN;

ALTER TABLE public.encounters
    DROP CONSTRAINT encounters_source_valid;

ALTER TABLE public.encounters
    ADD CONSTRAINT encounters_source_valid CHECK (
        source IN (
            'synthetic',
            'deidentified',
            'manual',
            'athena',
            'explicit_import',
            'synthetic_demo_release_candidate',
            'athena_preview_synthetic'
        )
    );

ALTER TABLE public.runtime_practice_bindings
    ADD CONSTRAINT runtime_practice_bindings_role_practice_unique
    UNIQUE (role_name, practice_id);

-- A role has one immutable server-owned source/provenance registration and is
-- already bound to the same practice in runtime_practice_bindings. Keeping the
-- import registration separate prevents an API or chase login from acquiring
-- import authority merely because it is practice-bound.
CREATE TABLE public.source_import_role_bindings (
    role_name      name        PRIMARY KEY,
    practice_id    text        NOT NULL,
    source_system  text        NOT NULL,
    provenance     text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT source_import_role_bindings_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES public.practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT source_import_role_bindings_runtime_fk
        FOREIGN KEY (role_name, practice_id)
        REFERENCES public.runtime_practice_bindings (role_name, practice_id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    CONSTRAINT source_import_role_bindings_source_valid CHECK (
        source_system ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    CONSTRAINT source_import_role_bindings_provenance_valid CHECK (
        provenance ~ '^[a-z][a-z0-9_]{0,127}$'
    ),
    CONSTRAINT source_import_role_bindings_source_unique
        UNIQUE (practice_id, source_system),
    CONSTRAINT source_import_role_bindings_provenance_unique
        UNIQUE (practice_id, provenance),
    CONSTRAINT source_import_role_bindings_receipt_unique
        UNIQUE (practice_id, source_system, provenance)
);

REVOKE ALL ON public.source_import_role_bindings FROM PUBLIC;

-- A provenance remains reserved for as long as any encounter still carries it.
-- This prevents deleting a future adapter registration from silently turning
-- its rows into ordinary mutable encounters. The reviewed down migration drops
-- this guard only after its per-binding deletion check has succeeded.
CREATE FUNCTION public.cosmaneura_guard_source_import_binding()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'source-import bindings require reviewed row cleanup'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.role_name IS DISTINCT FROM OLD.role_name
           OR NEW.practice_id IS DISTINCT FROM OLD.practice_id
           OR NEW.source_system IS DISTINCT FROM OLD.source_system
           OR NEW.provenance IS DISTINCT FROM OLD.provenance THEN
            RAISE EXCEPTION 'source-import binding identity is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.created_at := OLD.created_at;
        RETURN NEW;
    END IF;

    PERFORM set_config('cosmaneura.practice_id', OLD.practice_id, true);
    IF EXISTS (
        SELECT 1
          FROM public.encounters AS encounter
         WHERE encounter.practice_id = OLD.practice_id
           AND encounter.source = OLD.provenance
    ) THEN
        RAISE EXCEPTION 'source-import binding has reserved encounters'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_guard_source_import_binding()
    FROM PUBLIC;

CREATE TRIGGER source_import_role_bindings_guard
BEFORE UPDATE OR DELETE ON public.source_import_role_bindings
FOR EACH ROW
EXECUTE FUNCTION public.cosmaneura_guard_source_import_binding();

ALTER TABLE public.source_import_role_bindings
    ENABLE ALWAYS TRIGGER source_import_role_bindings_guard;

CREATE TRIGGER source_import_role_bindings_truncate_guard
BEFORE TRUNCATE ON public.source_import_role_bindings
FOR EACH STATEMENT
EXECUTE FUNCTION public.cosmaneura_guard_source_import_binding();

ALTER TABLE public.source_import_role_bindings
    ENABLE ALWAYS TRIGGER source_import_role_bindings_truncate_guard;

-- One row is the last committed boundary for one source feed. Cursors are
-- deliberately opaque; the database bounds them but never parses or logs them.
CREATE TABLE public.source_import_checkpoints (
    practice_id   text        NOT NULL,
    source_system text        NOT NULL,
    feed          text        NOT NULL,
    mapping_version text      NOT NULL,
    checkpoint    text,
    next_cursor   text,
    cursor_history text[]     NOT NULL DEFAULT ARRAY[]::text[],
    version       bigint      NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, source_system, feed),
    CONSTRAINT source_import_checkpoints_source_fk
        FOREIGN KEY (practice_id, source_system)
        REFERENCES public.source_import_role_bindings (
            practice_id, source_system
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT source_import_checkpoints_feed_valid CHECK (
        feed ~ '^[a-z][a-z0-9_.-]{0,63}$'
    ),
    CONSTRAINT source_import_checkpoints_mapping_version_valid CHECK (
        mapping_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    CONSTRAINT source_import_checkpoints_checkpoint_valid CHECK (
        checkpoint IS NULL
        OR octet_length(checkpoint) BETWEEN 1 AND 512
    ),
    CONSTRAINT source_import_checkpoints_next_cursor_valid CHECK (
        next_cursor IS NULL
        OR octet_length(next_cursor) BETWEEN 1 AND 512
    ),
    CONSTRAINT source_import_checkpoints_position_valid CHECK (
        checkpoint IS NOT NULL OR next_cursor IS NOT NULL
    ),
    CONSTRAINT source_import_checkpoints_history_bounded CHECK (
        cardinality(cursor_history) <= 100
        AND array_position(cursor_history, NULL) IS NULL
        AND octet_length(array_to_string(cursor_history, '')) <= 51200
    ),
    CONSTRAINT source_import_checkpoints_version_valid CHECK (version > 0),
    CONSTRAINT source_import_checkpoints_timestamps_valid CHECK (
        updated_at >= created_at
    )
);

REVOKE ALL ON public.source_import_checkpoints FROM PUBLIC;

-- The receipt FK includes source so a recorded import cannot later be
-- reclassified. encounter_uid is already unique within a practice; this adds
-- the exact key shape needed by the provenance-bearing foreign key.
ALTER TABLE public.encounters
    ADD CONSTRAINT encounters_uid_source_unique
    UNIQUE (practice_id, encounter_uid, source);

-- A receipt proves exactly which immutable source identity and payload was
-- committed. Encounter imports link their target row; other feed types may
-- leave encounter_uid NULL and keep their domain linkage in their own table.
-- idempotency_key is a namespaced digest of opaque source resource/version and
-- bounded normalizer mapping version; payload_sha256 detects an altered replay.
CREATE TABLE public.source_import_receipts (
    practice_id     text        NOT NULL,
    source_system   text        NOT NULL,
    feed            text        NOT NULL,
    idempotency_key text        NOT NULL,
    mapping_version text        NOT NULL,
    payload_sha256  text        NOT NULL,
    encounter_uid   uuid,
    provenance      text        NOT NULL,
    imported_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        practice_id, source_system, feed, idempotency_key
    ),
    CONSTRAINT source_import_receipts_source_fk
        FOREIGN KEY (practice_id, source_system, provenance)
        REFERENCES public.source_import_role_bindings (
            practice_id, source_system, provenance
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT source_import_receipts_encounter_fk
        FOREIGN KEY (practice_id, encounter_uid, provenance)
        REFERENCES public.encounters (practice_id, encounter_uid, source)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT source_import_receipts_feed_valid CHECK (
        feed ~ '^[a-z][a-z0-9_.-]{0,63}$'
    ),
    CONSTRAINT source_import_receipts_encounter_link_required CHECK (
        feed <> 'encounters' OR encounter_uid IS NOT NULL
    ),
    CONSTRAINT source_import_receipts_idempotency_key_valid CHECK (
        idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
    ),
    CONSTRAINT source_import_receipts_mapping_version_valid CHECK (
        mapping_version ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    ),
    CONSTRAINT source_import_receipts_payload_sha256_valid CHECK (
        payload_sha256 ~ '^[0-9a-f]{64}$'
    )
);

REVOKE ALL ON public.source_import_receipts FROM PUBLIC;

CREATE INDEX source_import_receipts_encounter_idx
    ON public.source_import_receipts (
        practice_id, encounter_uid, imported_at DESC
    );

CREATE FUNCTION public.cosmaneura_source_import_session()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = off
AS $$
    SELECT
        -- Binding loss must never turn an already-privileged importer into an
        -- ordinary encounter reader. Table ownership is excluded so reviewed
        -- owner migration/cleanup remains possible under FORCE RLS.
        session_user <> (
            SELECT pg_catalog.pg_get_userbyid(relation.relowner)
              FROM pg_catalog.pg_class AS relation
             WHERE relation.oid = 'public.encounters'::regclass
        )
        AND (
            EXISTS (
                SELECT 1
                  FROM public.source_import_role_bindings AS binding
                 WHERE binding.role_name = session_user::name
            )
            OR pg_catalog.has_table_privilege(
                session_user,
                'public.source_import_checkpoints',
                'SELECT,INSERT,UPDATE'
            )
            OR pg_catalog.has_table_privilege(
                session_user,
                'public.source_import_receipts',
                'SELECT,INSERT'
            )
            OR pg_catalog.has_any_column_privilege(
                session_user,
                'public.source_import_checkpoints',
                'SELECT,INSERT,UPDATE'
            )
            OR pg_catalog.has_any_column_privilege(
                session_user,
                'public.source_import_receipts',
                'SELECT,INSERT,UPDATE'
            )
            OR COALESCE(
                pg_catalog.has_function_privilege(
                    session_user,
                    pg_catalog.to_regprocedure(
                        'public.cosmaneura_commit_source_encounter_page('
                        'text,text,text,text,text,bigint,jsonb,text,text)'
                    ),
                    'EXECUTE'
                ),
                false
            )
        )
$$;

-- These policy helpers reveal only whether the calling session's own binding
-- matches caller-supplied values. Existing encounter readers must be able to
-- evaluate the restrictive policy immediately when this migration lands.
GRANT EXECUTE ON FUNCTION public.cosmaneura_source_import_session() TO PUBLIC;

CREATE FUNCTION public.cosmaneura_source_import_owner()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
    SELECT session_user = current_user
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_source_import_owner() FROM PUBLIC;

CREATE FUNCTION public.cosmaneura_source_import_allowed(
    candidate_practice_id text,
    candidate_source_system text,
    candidate_provenance text
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
SET row_security = off
AS $$
    SELECT
        candidate_practice_id = NULLIF(
            current_setting('cosmaneura.practice_id', true), ''
        )
        AND EXISTS (
            SELECT 1
              FROM public.source_import_role_bindings AS binding
             WHERE binding.role_name = session_user::name
               AND binding.practice_id = candidate_practice_id
               AND (
                   candidate_source_system IS NULL
                   OR binding.source_system = candidate_source_system
               )
               AND (
                   candidate_provenance IS NULL
                   OR binding.provenance = candidate_provenance
               )
        )
$$;

GRANT EXECUTE ON FUNCTION public.cosmaneura_source_import_allowed(
    text, text, text
) TO PUBLIC;

-- Import sessions receive an additional restrictive policy on encounters.
-- Other application sessions retain the existing practice-scoped policy, but
-- the dedicated importer can see and mutate only its registered provenance.
CREATE POLICY encounters_source_import_restriction
    ON public.encounters
    AS RESTRICTIVE
    USING (
        NOT public.cosmaneura_source_import_session()
        OR public.cosmaneura_source_import_allowed(
            practice_id, NULL::text, source
        )
    )
    WITH CHECK (
        NOT public.cosmaneura_source_import_session()
        OR public.cosmaneura_source_import_allowed(
            practice_id, NULL::text, source
        )
    );

CREATE POLICY source_import_checkpoints_isolation
    ON public.source_import_checkpoints
    USING (
        public.cosmaneura_source_import_owner()
        OR public.cosmaneura_source_import_allowed(
               practice_id, source_system, NULL::text
           )
    )
    WITH CHECK (
        public.cosmaneura_source_import_owner()
        OR public.cosmaneura_source_import_allowed(
               practice_id, source_system, NULL::text
           )
    );
ALTER TABLE public.source_import_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_import_checkpoints FORCE ROW LEVEL SECURITY;

CREATE POLICY source_import_receipts_isolation
    ON public.source_import_receipts
    USING (
        public.cosmaneura_source_import_owner()
        OR public.cosmaneura_source_import_allowed(
               practice_id, source_system, provenance
           )
    )
    WITH CHECK (
        public.cosmaneura_source_import_owner()
        OR public.cosmaneura_source_import_allowed(
               practice_id, source_system, provenance
           )
    );
ALTER TABLE public.source_import_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_import_receipts FORCE ROW LEVEL SECURITY;

-- This owner-executed trigger supplies both practice and provenance for the
-- dedicated importer. Callers never obtain INSERT/UPDATE authority over those
-- two columns, and non-import sessions cannot claim a registered provenance.
CREATE FUNCTION public.cosmaneura_guard_source_import_encounter()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    binding public.source_import_role_bindings%ROWTYPE;
    table_owner name;
    reserved_source boolean := false;
BEGIN
    SELECT * INTO binding
      FROM public.source_import_role_bindings AS candidate
     WHERE candidate.role_name = session_user::name;

    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
      INTO table_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF table_owner IS NOT NULL AND session_user = table_owner THEN
        IF TG_OP = 'TRUNCATE' THEN
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'source-import encounters require the table owner'
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'INSERT' AND binding.role_name IS NOT NULL THEN
        IF NEW.practice_id IS NOT NULL
           AND NEW.practice_id IS DISTINCT FROM binding.practice_id THEN
            RAISE EXCEPTION 'source-import practice is server-owned'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.source IS NOT NULL
           AND NEW.source IS DISTINCT FROM binding.provenance THEN
            RAISE EXCEPTION 'source-import provenance is server-owned'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.practice_id := binding.practice_id;
        NEW.source := binding.provenance;
        NEW.ingested_by_tenant_id := NULL;
        NEW.ingested_by_user_id := NULL;
        RETURN NEW;
    END IF;

    IF TG_OP = 'UPDATE' AND binding.role_name IS NOT NULL THEN
        IF OLD.practice_id IS DISTINCT FROM binding.practice_id
           OR OLD.source IS DISTINCT FROM binding.provenance
           OR NEW.practice_id IS DISTINCT FROM OLD.practice_id
           OR NEW.source IS DISTINCT FROM OLD.source THEN
            RAISE EXCEPTION 'source importer cannot mutate an unbound encounter'
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.ingested_by_tenant_id := OLD.ingested_by_tenant_id;
        NEW.ingested_by_user_id := OLD.ingested_by_user_id;
        IF ROW(
            NEW.patient_age, NEW.patient_sex, NEW.encounter_type,
            NEW.place_of_service, NEW.intake_text, NEW.transcript,
            NEW.signed_note, NEW.provider_specialty,
            NEW.submitted_em_code, NEW.submitted_icd10
        ) IS DISTINCT FROM ROW(
            OLD.patient_age, OLD.patient_sex, OLD.encounter_type,
            OLD.place_of_service, OLD.intake_text, OLD.transcript,
            OLD.signed_note, OLD.provider_specialty,
            OLD.submitted_em_code, OLD.submitted_icd10
        ) THEN
            NEW.version := OLD.version + 1;
            NEW.updated_at := now();
        ELSE
            NEW.version := OLD.version;
            NEW.updated_at := OLD.updated_at;
        END IF;
        NEW.created_at := OLD.created_at;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        SELECT EXISTS (
            SELECT 1
              FROM public.source_import_role_bindings AS registered
             WHERE registered.practice_id = OLD.practice_id
               AND registered.provenance = OLD.source
        ) INTO reserved_source;
    ELSE
        SELECT EXISTS (
            SELECT 1
              FROM public.source_import_role_bindings AS registered
             WHERE (
                       registered.practice_id = NEW.practice_id
                       AND registered.provenance = NEW.source
                   )
                OR (
                       TG_OP = 'UPDATE'
                       AND registered.practice_id = OLD.practice_id
                       AND registered.provenance = OLD.source
                   )
        ) INTO reserved_source;
    END IF;

    -- The fixed Preview value is reserved even before the first role is
    -- provisioned, closing the migration/provisioning interval.
    IF reserved_source
       OR (TG_OP <> 'DELETE' AND NEW.source = 'athena_preview_synthetic')
       OR (TG_OP IN ('UPDATE', 'DELETE')
           AND OLD.source = 'athena_preview_synthetic') THEN
        RAISE EXCEPTION 'reserved source-import provenance requires its importer'
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_guard_source_import_encounter()
    FROM PUBLIC;

CREATE TRIGGER encounters_source_import_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.encounters
FOR EACH ROW
EXECUTE FUNCTION public.cosmaneura_guard_source_import_encounter();

ALTER TABLE public.encounters
    ENABLE ALWAYS TRIGGER encounters_source_import_guard;

CREATE TRIGGER encounters_z_source_import_truncate_guard
BEFORE TRUNCATE ON public.encounters
FOR EACH STATEMENT
EXECUTE FUNCTION public.cosmaneura_guard_source_import_encounter();

ALTER TABLE public.encounters
    ENABLE ALWAYS TRIGGER encounters_z_source_import_truncate_guard;

CREATE FUNCTION public.cosmaneura_guard_source_import_checkpoint()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    binding public.source_import_role_bindings%ROWTYPE;
    table_owner name;
BEGIN
    SELECT * INTO binding
      FROM public.source_import_role_bindings AS candidate
     WHERE candidate.role_name = session_user::name;
    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
      INTO table_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF table_owner IS NOT NULL AND session_user = table_owner THEN
        IF TG_OP = 'TRUNCATE' THEN
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF binding.role_name IS NULL THEN
        RAISE EXCEPTION 'source-import checkpoint requires its importer'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION 'source-import checkpoints require owner cleanup'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.practice_id IS DISTINCT FROM binding.practice_id
       OR NEW.source_system IS DISTINCT FROM binding.source_system THEN
        RAISE EXCEPTION 'source-import checkpoint is not bound to this importer'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.version := 1;
        NEW.created_at := now();
        NEW.cursor_history := CASE
            WHEN NEW.next_cursor IS NULL THEN ARRAY[]::text[]
            ELSE ARRAY[NEW.next_cursor]
        END;
    ELSE
        IF NEW.practice_id IS DISTINCT FROM OLD.practice_id
           OR NEW.source_system IS DISTINCT FROM OLD.source_system
           OR NEW.feed IS DISTINCT FROM OLD.feed
           OR NEW.mapping_version IS DISTINCT FROM OLD.mapping_version THEN
            RAISE EXCEPTION 'source-import checkpoint identity is immutable'
                USING ERRCODE = 'check_violation';
        END IF;
        IF ROW(NEW.checkpoint, NEW.next_cursor)
           IS DISTINCT FROM ROW(OLD.checkpoint, OLD.next_cursor) THEN
            IF OLD.next_cursor IS NULL THEN
                NEW.cursor_history := ARRAY[]::text[];
            ELSE
                NEW.cursor_history := OLD.cursor_history;
            END IF;
            IF NEW.next_cursor IS NOT NULL
               AND NEW.next_cursor = ANY(NEW.cursor_history) THEN
                RAISE EXCEPTION 'source-import cursor cycle is not allowed'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.next_cursor IS NOT NULL THEN
                NEW.cursor_history := array_append(
                    NEW.cursor_history, NEW.next_cursor
                );
            END IF;
            NEW.version := OLD.version + 1;
            NEW.updated_at := now();
        ELSE
            NEW.version := OLD.version;
            NEW.updated_at := OLD.updated_at;
            NEW.cursor_history := OLD.cursor_history;
        END IF;
        NEW.created_at := OLD.created_at;
    END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_guard_source_import_checkpoint()
    FROM PUBLIC;

CREATE TRIGGER source_import_checkpoints_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.source_import_checkpoints
FOR EACH ROW
EXECUTE FUNCTION public.cosmaneura_guard_source_import_checkpoint();

ALTER TABLE public.source_import_checkpoints
    ENABLE ALWAYS TRIGGER source_import_checkpoints_guard;

CREATE TRIGGER source_import_checkpoints_truncate_guard
BEFORE TRUNCATE ON public.source_import_checkpoints
FOR EACH STATEMENT
EXECUTE FUNCTION public.cosmaneura_guard_source_import_checkpoint();

ALTER TABLE public.source_import_checkpoints
    ENABLE ALWAYS TRIGGER source_import_checkpoints_truncate_guard;

CREATE FUNCTION public.cosmaneura_guard_source_import_receipt()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    binding public.source_import_role_bindings%ROWTYPE;
    table_owner name;
BEGIN
    SELECT * INTO binding
      FROM public.source_import_role_bindings AS candidate
     WHERE candidate.role_name = session_user::name;
    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
      INTO table_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF table_owner IS NOT NULL AND session_user = table_owner THEN
        IF TG_OP = 'TRUNCATE' THEN
            RETURN NULL;
        ELSIF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF binding.role_name IS NULL THEN
        RAISE EXCEPTION 'source-import receipt requires its importer'
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'source-import receipts are append-only'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.practice_id IS DISTINCT FROM binding.practice_id
       OR NEW.source_system IS DISTINCT FROM binding.source_system THEN
        RAISE EXCEPTION 'source-import receipt is not bound to this importer'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.provenance IS NOT NULL
       AND NEW.provenance IS DISTINCT FROM binding.provenance THEN
        RAISE EXCEPTION 'source-import receipt provenance is server-owned'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.provenance := binding.provenance;
    NEW.imported_at := now();
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_guard_source_import_receipt()
    FROM PUBLIC;

CREATE TRIGGER source_import_receipts_guard
BEFORE INSERT OR UPDATE OR DELETE ON public.source_import_receipts
FOR EACH ROW
EXECUTE FUNCTION public.cosmaneura_guard_source_import_receipt();

ALTER TABLE public.source_import_receipts
    ENABLE ALWAYS TRIGGER source_import_receipts_guard;

CREATE TRIGGER source_import_receipts_truncate_guard
BEFORE TRUNCATE ON public.source_import_receipts
FOR EACH STATEMENT
EXECUTE FUNCTION public.cosmaneura_guard_source_import_receipt();

ALTER TABLE public.source_import_receipts
    ENABLE ALWAYS TRIGGER source_import_receipts_truncate_guard;

-- The import login receives EXECUTE on this primitive instead of direct DML.
-- One call serializes a feed, verifies the expected checkpoint version, applies
-- every encounter and receipt, and advances the checkpoint in one transaction.
CREATE FUNCTION public.cosmaneura_commit_source_encounter_page(
    candidate_practice_id text,
    candidate_source_system text,
    candidate_feed text,
    candidate_provenance text,
    candidate_mapping_version text,
    expected_checkpoint_version bigint,
    candidate_records jsonb,
    candidate_checkpoint text,
    candidate_next_cursor text
)
RETURNS TABLE (
    result_imported_count integer,
    result_replayed_count integer,
    result_checkpoint text,
    result_next_cursor text,
    result_mapping_version text,
    result_cursor_history text[],
    result_version bigint
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    item jsonb;
    receipt_row record;
    encounter_id uuid;
    checkpoint_row record;
    checkpoint_exists boolean;
    imported_total integer := 0;
    replayed_total integer := 0;
BEGIN
    IF candidate_source_system IS NULL OR candidate_provenance IS NULL THEN
        RAISE EXCEPTION 'source-import identity is required'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT public.cosmaneura_source_import_allowed(
        candidate_practice_id,
        candidate_source_system,
        candidate_provenance
    ) THEN
        RAISE EXCEPTION 'source-import binding does not match page'
            USING ERRCODE = 'check_violation';
    END IF;
    IF pg_catalog.jsonb_typeof(candidate_records) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'source-import records must be an array'
            USING ERRCODE = 'check_violation';
    END IF;
    IF pg_catalog.octet_length(candidate_records::text) > 16777216 THEN
        RAISE EXCEPTION 'source-import page payload is too large'
            USING ERRCODE = 'check_violation';
    END IF;
    IF pg_catalog.jsonb_array_length(candidate_records) > 100 THEN
        RAISE EXCEPTION 'source-import page is too large'
            USING ERRCODE = 'check_violation';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            pg_catalog.jsonb_build_array(
                candidate_practice_id,
                candidate_source_system,
                candidate_feed
            )::text,
            0
        )
    );
    SELECT checkpoint.mapping_version, checkpoint.version
      INTO checkpoint_row
      FROM public.source_import_checkpoints AS checkpoint
     WHERE checkpoint.practice_id = candidate_practice_id
       AND checkpoint.source_system = candidate_source_system
       AND checkpoint.feed = candidate_feed
     FOR UPDATE;
    checkpoint_exists := FOUND;
    IF (expected_checkpoint_version IS NULL AND checkpoint_exists)
       OR (expected_checkpoint_version IS NOT NULL AND (
               NOT checkpoint_exists
               OR checkpoint_row.version <> expected_checkpoint_version
           )) THEN
        RAISE EXCEPTION 'source-import checkpoint changed concurrently'
            USING ERRCODE = 'serialization_failure';
    END IF;
    IF checkpoint_exists
       AND checkpoint_row.mapping_version <> candidate_mapping_version THEN
        RAISE EXCEPTION 'source-import mapping version changed'
            USING ERRCODE = 'check_violation';
    END IF;

    FOR item IN SELECT value FROM pg_catalog.jsonb_array_elements(candidate_records)
    LOOP
        IF pg_catalog.jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR (SELECT count(*) FROM pg_catalog.jsonb_object_keys(item)) <> 14
           OR NOT item ?& ARRAY[
               'target_external_id', 'idempotency_key', 'mapping_version',
               'payload_sha256', 'patient_age', 'patient_sex',
               'encounter_type', 'place_of_service', 'intake_text',
               'transcript', 'signed_note', 'provider_specialty',
               'submitted_em_code', 'submitted_icd10'
           ]
           OR pg_catalog.jsonb_typeof(item->'target_external_id')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'idempotency_key')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'mapping_version')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'payload_sha256')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'patient_age')
              IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(item->'patient_sex')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'encounter_type')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'place_of_service')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'intake_text')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'transcript')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'signed_note')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'provider_specialty')
              IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(item->'submitted_em_code')
              NOT IN ('string', 'null')
           OR pg_catalog.jsonb_typeof(item->'submitted_icd10')
              IS DISTINCT FROM 'array'
           OR EXISTS (
               SELECT 1
                 FROM pg_catalog.jsonb_array_elements(
                          item->'submitted_icd10'
                      ) AS diagnosis(value)
                WHERE pg_catalog.jsonb_typeof(diagnosis.value) <> 'string'
           )
           OR item->>'mapping_version' IS DISTINCT FROM candidate_mapping_version
           OR item->>'target_external_id'
              !~ '^source-encounter-v1:[0-9a-f]{64}$'
           OR item->>'idempotency_key'
              !~ '^source-import-v1:[0-9a-f]{64}$'
           OR item->>'payload_sha256' !~ '^[0-9a-f]{64}$'
        THEN
            RAISE EXCEPTION 'source-import record mapping is invalid'
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT receipt.payload_sha256, receipt.provenance,
               receipt.mapping_version, encounter.external_id
          INTO receipt_row
          FROM public.source_import_receipts AS receipt
          LEFT JOIN public.encounters AS encounter
            ON encounter.practice_id = receipt.practice_id
           AND encounter.encounter_uid = receipt.encounter_uid
           AND encounter.source = receipt.provenance
         WHERE receipt.practice_id = candidate_practice_id
           AND receipt.source_system = candidate_source_system
           AND receipt.feed = candidate_feed
           AND receipt.idempotency_key = item->>'idempotency_key';

        IF FOUND THEN
            IF receipt_row.payload_sha256 IS DISTINCT FROM item->>'payload_sha256'
               OR receipt_row.provenance IS DISTINCT FROM candidate_provenance
               OR receipt_row.mapping_version
                  IS DISTINCT FROM candidate_mapping_version
               OR receipt_row.external_id
                  IS DISTINCT FROM item->>'target_external_id' THEN
                RAISE EXCEPTION 'source-import receipt conflicts with page'
                    USING ERRCODE = 'unique_violation';
            END IF;
            replayed_total := replayed_total + 1;
        ELSE
            INSERT INTO public.encounters (
                practice_id, external_id, patient_age, patient_sex,
                encounter_type, place_of_service, intake_text, transcript,
                signed_note, provider_specialty, submitted_em_code,
                submitted_icd10
            ) VALUES (
                candidate_practice_id,
                item->>'target_external_id',
                (item->>'patient_age')::integer,
                item->>'patient_sex',
                item->>'encounter_type',
                item->>'place_of_service',
                item->>'intake_text',
                item->>'transcript',
                item->>'signed_note',
                item->>'provider_specialty',
                item->>'submitted_em_code',
                ARRAY(
                    SELECT diagnosis.value
                      FROM pg_catalog.jsonb_array_elements_text(
                               item->'submitted_icd10'
                           ) AS diagnosis(value)
                )
            )
            ON CONFLICT (practice_id, external_id) DO UPDATE SET
                patient_age = EXCLUDED.patient_age,
                patient_sex = EXCLUDED.patient_sex,
                encounter_type = EXCLUDED.encounter_type,
                place_of_service = EXCLUDED.place_of_service,
                intake_text = EXCLUDED.intake_text,
                transcript = EXCLUDED.transcript,
                signed_note = EXCLUDED.signed_note,
                provider_specialty = EXCLUDED.provider_specialty,
                submitted_em_code = EXCLUDED.submitted_em_code,
                submitted_icd10 = EXCLUDED.submitted_icd10
            WHERE encounters.source = EXCLUDED.source
            RETURNING encounters.encounter_uid INTO encounter_id;
            IF encounter_id IS NULL THEN
                RAISE EXCEPTION 'source-import encounter provenance conflicts'
                    USING ERRCODE = 'unique_violation';
            END IF;

            INSERT INTO public.source_import_receipts (
                practice_id, source_system, feed, idempotency_key,
                mapping_version, payload_sha256, encounter_uid
            ) VALUES (
                candidate_practice_id,
                candidate_source_system,
                candidate_feed,
                item->>'idempotency_key',
                candidate_mapping_version,
                item->>'payload_sha256',
                encounter_id
            );
            imported_total := imported_total + 1;
        END IF;
    END LOOP;

    INSERT INTO public.source_import_checkpoints (
        practice_id, source_system, feed, mapping_version,
        checkpoint, next_cursor
    ) VALUES (
        candidate_practice_id,
        candidate_source_system,
        candidate_feed,
        candidate_mapping_version,
        candidate_checkpoint,
        candidate_next_cursor
    )
    ON CONFLICT (practice_id, source_system, feed) DO UPDATE SET
        checkpoint = EXCLUDED.checkpoint,
        next_cursor = EXCLUDED.next_cursor
    RETURNING source_import_checkpoints.checkpoint,
              source_import_checkpoints.next_cursor,
              source_import_checkpoints.mapping_version,
              source_import_checkpoints.cursor_history,
              source_import_checkpoints.version
         INTO checkpoint_row;

    RETURN QUERY SELECT
        imported_total,
        replayed_total,
        checkpoint_row.checkpoint,
        checkpoint_row.next_cursor,
        checkpoint_row.mapping_version,
        checkpoint_row.cursor_history,
        checkpoint_row.version;
END
$$;

REVOKE ALL ON FUNCTION public.cosmaneura_commit_source_encounter_page(
    text, text, text, text, text, bigint, jsonb, text, text
) FROM PUBLIC;

COMMIT;
