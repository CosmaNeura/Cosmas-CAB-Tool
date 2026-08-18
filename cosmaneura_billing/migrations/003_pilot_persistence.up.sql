-- 003_pilot_persistence.up.sql
-- Practice-scoped pilot persistence for identities, encounters, coder feedback,
-- adaptive-preference source data, and access audit records.
-- PostgreSQL 16. Existing chase data is retained and brought under the same
-- practice foreign key and row-level-security boundary.

BEGIN;

-- A practice normally has an immutable Microsoft Entra tenant binding. Chase
-- rows created before this migration do not carry a tenant, so their practices
-- are backfilled with a NULL tenant rather than a fabricated UUID. Such a row
-- cannot have a user or membership until the explicit bootstrap binds it.
CREATE TABLE practices (
    practice_id       text        PRIMARY KEY,
    entra_tenant_id   uuid,
    display_name      text,
    active            boolean     NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT practices_id_valid CHECK (
        char_length(practice_id) BETWEEN 1 AND 128
        AND practice_id = btrim(practice_id)
        AND practice_id !~ '[[:cntrl:]]'
    ),
    CONSTRAINT practices_display_name_valid CHECK (
        display_name IS NULL OR (
            char_length(display_name) BETWEEN 1 AND 256
            AND display_name = btrim(display_name)
            AND display_name !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT practices_timestamps_valid CHECK (updated_at >= created_at),
    CONSTRAINT practices_id_tenant_unique UNIQUE (practice_id, entra_tenant_id)
);

INSERT INTO practices (practice_id, entra_tenant_id)
SELECT DISTINCT practice_id, NULL::uuid
FROM chase_items
ON CONFLICT (practice_id) DO NOTHING;

ALTER TABLE chase_items
    ADD CONSTRAINT chase_items_practice_fk
    FOREIGN KEY (practice_id)
    REFERENCES practices (practice_id)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT;

-- Public chase IDs are deterministic for detector idempotency and may be
-- derived from low-entropy EHR identifiers. Audit only this random identity.
ALTER TABLE chase_items
    ADD COLUMN chase_uid uuid NOT NULL DEFAULT gen_random_uuid(),
    ADD CONSTRAINT chase_items_uid_unique UNIQUE (practice_id, chase_uid);

CREATE INDEX practices_tenant_idx
    ON practices (entra_tenant_id, practice_id)
    WHERE entra_tenant_id IS NOT NULL;

-- Entra object IDs are unique only inside a tenant. Practice membership is a
-- separate explicit record so one identity can belong to multiple practices.
CREATE TABLE users (
    tenant_id       uuid        NOT NULL,
    user_id         uuid        NOT NULL,
    active          boolean     NOT NULL DEFAULT true,
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT users_timestamps_valid CHECK (last_seen_at >= first_seen_at)
);

CREATE INDEX users_last_seen_idx
    ON users (tenant_id, last_seen_at DESC, user_id);

-- Entra remains the authorization source. This table records the explicit,
-- current practice membership and only the four application roles admitted by
-- the API. Role snapshots on access-audit rows preserve historical context.
CREATE TABLE practice_memberships (
    practice_id       text        NOT NULL,
    tenant_id         uuid        NOT NULL,
    user_id           uuid        NOT NULL,
    active            boolean     NOT NULL DEFAULT true,
    roles             text[]      NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, tenant_id, user_id),
    CONSTRAINT practice_memberships_practice_tenant_fk
        FOREIGN KEY (practice_id, tenant_id)
        REFERENCES practices (practice_id, entra_tenant_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT practice_memberships_user_fk
        FOREIGN KEY (tenant_id, user_id)
        REFERENCES users (tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT practice_memberships_roles_valid CHECK (
        cardinality(roles) BETWEEN 1 AND 4
        AND roles <@ ARRAY['Admin', 'BillingStaff', 'Coder', 'Provider']::text[]
        AND cardinality(roles) =
            (CASE WHEN 'Admin' = ANY (roles) THEN 1 ELSE 0 END)
          + (CASE WHEN 'BillingStaff' = ANY (roles) THEN 1 ELSE 0 END)
          + (CASE WHEN 'Coder' = ANY (roles) THEN 1 ELSE 0 END)
          + (CASE WHEN 'Provider' = ANY (roles) THEN 1 ELSE 0 END)
    ),
    CONSTRAINT practice_memberships_timestamps_valid CHECK (
        updated_at >= created_at
    )
);

CREATE INDEX practice_memberships_active_idx
    ON practice_memberships (practice_id, user_id)
    WHERE active = true;
CREATE INDEX practice_memberships_roles_gin_idx
    ON practice_memberships USING gin (roles);

-- One immutable internal UUID decouples database relationships from the EHR or
-- demo encounter identifier. The remaining columns are the exact VisitBundle
-- inputs; raw documentation is stored unchanged.
CREATE TABLE encounters (
    practice_id            text        NOT NULL,
    encounter_uid          uuid        NOT NULL DEFAULT gen_random_uuid(),
    external_id            text        NOT NULL,
    patient_age            integer     NOT NULL,
    patient_sex            text        NOT NULL,
    encounter_type         text        NOT NULL,
    place_of_service       text        NOT NULL,
    intake_text            text        NOT NULL DEFAULT '',
    transcript             text        NOT NULL DEFAULT '',
    signed_note            text        NOT NULL DEFAULT '',
    provider_specialty     text        NOT NULL DEFAULT 'Family Medicine',
    submitted_em_code      text,
    submitted_icd10        text[]      NOT NULL DEFAULT ARRAY[]::text[],
    source                 text        NOT NULL,
    ingested_by_tenant_id  uuid,
    ingested_by_user_id    uuid,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    version                bigint      NOT NULL DEFAULT 1,
    PRIMARY KEY (practice_id, encounter_uid),
    CONSTRAINT encounters_external_id_unique UNIQUE (practice_id, external_id),
    CONSTRAINT encounters_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT encounters_ingested_by_fk
        FOREIGN KEY (
            practice_id, ingested_by_tenant_id, ingested_by_user_id
        ) REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT encounters_external_id_valid CHECK (
        char_length(external_id) BETWEEN 1 AND 128
        AND external_id = btrim(external_id)
        AND external_id !~ '[[:cntrl:]]'
    ),
    CONSTRAINT encounters_patient_age_valid CHECK (patient_age BETWEEN 0 AND 130),
    CONSTRAINT encounters_patient_sex_valid CHECK (
        char_length(patient_sex) BETWEEN 1 AND 32
        AND patient_sex !~ '[[:cntrl:]]'
    ),
    CONSTRAINT encounters_type_valid CHECK (
        char_length(encounter_type) BETWEEN 1 AND 64
        AND encounter_type !~ '[[:cntrl:]]'
    ),
    CONSTRAINT encounters_place_of_service_valid CHECK (
        char_length(place_of_service) BETWEEN 1 AND 8
        AND place_of_service !~ '[[:cntrl:]]'
    ),
    CONSTRAINT encounters_document_size_valid CHECK (
        char_length(intake_text) <= 250000
        AND char_length(transcript) <= 500000
        AND char_length(signed_note) <= 250000
    ),
    CONSTRAINT encounters_specialty_valid CHECK (
        char_length(provider_specialty) BETWEEN 1 AND 128
        AND provider_specialty !~ '[[:cntrl:]]'
    ),
    CONSTRAINT encounters_submitted_em_valid CHECK (
        submitted_em_code IS NULL
        OR char_length(submitted_em_code) BETWEEN 1 AND 32
    ),
    CONSTRAINT encounters_submitted_icd10_valid CHECK (
        cardinality(submitted_icd10) <= 500
        AND octet_length(array_to_string(submitted_icd10, ',')) <= 16500
    ),
    CONSTRAINT encounters_source_valid CHECK (
        source IN (
            'synthetic', 'deidentified', 'manual', 'athena', 'explicit_import'
        )
    ),
    CONSTRAINT encounters_ingested_by_valid CHECK (
        (ingested_by_tenant_id IS NULL AND ingested_by_user_id IS NULL)
        OR (ingested_by_tenant_id IS NOT NULL AND ingested_by_user_id IS NOT NULL)
    ),
    CONSTRAINT encounters_version_valid CHECK (version > 0),
    CONSTRAINT encounters_timestamps_valid CHECK (updated_at >= created_at)
);

CREATE INDEX encounters_recent_idx
    ON encounters (practice_id, created_at DESC, encounter_uid);
CREATE INDEX encounters_type_idx
    ON encounters (practice_id, encounter_type, created_at DESC);

-- Feedback headers and decisions are append-only domain records. review_id is
-- the public idempotency identity; payload_hash proves whether a retry carrying
-- the same review UUID is byte-for-byte equivalent after canonicalization.
CREATE TABLE coder_feedback (
    practice_id            text             NOT NULL,
    feedback_id            bigint           GENERATED ALWAYS AS IDENTITY,
    review_id              uuid             NOT NULL,
    encounter_uid          uuid             NOT NULL,
    coder_tenant_id        uuid             NOT NULL,
    coder_user_id          uuid             NOT NULL,
    engine_version         text             NOT NULL,
    submitted_em           text,
    engine_em              text,
    final_em               text,
    encounter_version      bigint           NOT NULL,
    time_spent_seconds     double precision,
    overall_note           text             NOT NULL DEFAULT '',
    submitted_at           timestamptz      NOT NULL,
    recorded_at            timestamptz      NOT NULL DEFAULT now(),
    payload_hash           text             NOT NULL,
    import_source_sha256   bytea,
    import_line            integer,
    PRIMARY KEY (practice_id, feedback_id),
    CONSTRAINT coder_feedback_review_unique
        UNIQUE (practice_id, review_id),
    CONSTRAINT coder_feedback_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT coder_feedback_encounter_fk
        FOREIGN KEY (practice_id, encounter_uid)
        REFERENCES encounters (practice_id, encounter_uid)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT coder_feedback_reviewer_fk
        FOREIGN KEY (
            practice_id, coder_tenant_id, coder_user_id
        ) REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT coder_feedback_payload_hash_valid CHECK (
        payload_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT coder_feedback_engine_version_valid CHECK (
        char_length(engine_version) BETWEEN 1 AND 256
        AND engine_version !~ '[[:cntrl:]]'
    ),
    CONSTRAINT coder_feedback_em_codes_valid CHECK (
        (submitted_em IS NULL OR char_length(submitted_em) BETWEEN 1 AND 32)
        AND (engine_em IS NULL OR char_length(engine_em) BETWEEN 1 AND 32)
        AND (final_em IS NULL OR char_length(final_em) BETWEEN 1 AND 32)
    ),
    CONSTRAINT coder_feedback_time_spent_valid CHECK (
        time_spent_seconds IS NULL
        OR time_spent_seconds BETWEEN 0 AND 86400
    ),
    CONSTRAINT coder_feedback_encounter_version_valid CHECK (
        encounter_version > 0
    ),
    CONSTRAINT coder_feedback_overall_note_valid CHECK (
        char_length(overall_note) <= 4000
    ),
    CONSTRAINT coder_feedback_import_provenance_valid CHECK (
        (import_source_sha256 IS NULL AND import_line IS NULL)
        OR (
            import_source_sha256 IS NOT NULL
            AND octet_length(import_source_sha256) = 32
            AND import_line > 0
        )
    ),
    CONSTRAINT coder_feedback_import_line_unique
        UNIQUE (practice_id, import_source_sha256, import_line)
);

CREATE INDEX coder_feedback_reviewer_idx
    ON coder_feedback (
        practice_id, coder_user_id, feedback_id
    );
CREATE INDEX coder_feedback_encounter_idx
    ON coder_feedback (
        practice_id, encounter_uid, feedback_id
    );

CREATE TABLE coder_feedback_decisions (
    practice_id    text        NOT NULL,
    feedback_id    bigint      NOT NULL,
    ordinal        integer     NOT NULL,
    kind           text        NOT NULL,
    engine_code    text,
    action         text        NOT NULL,
    final_code     text,
    reason_code    text,
    note           text        NOT NULL DEFAULT '',
    PRIMARY KEY (practice_id, feedback_id, ordinal),
    CONSTRAINT coder_feedback_decisions_review_fk
        FOREIGN KEY (practice_id, feedback_id)
        REFERENCES coder_feedback (practice_id, feedback_id)
        ON UPDATE RESTRICT
        ON DELETE CASCADE,
    CONSTRAINT coder_feedback_decisions_number_valid CHECK (
        ordinal BETWEEN 0 AND 499
    ),
    CONSTRAINT coder_feedback_decisions_kind_valid CHECK (
        kind IN ('EM', 'ICD10', 'PROCEDURE', 'MODIFIER')
    ),
    CONSTRAINT coder_feedback_decisions_action_valid CHECK (
        action IN ('accept', 'modify', 'reject', 'add')
    ),
    CONSTRAINT coder_feedback_decisions_codes_valid CHECK (
        (engine_code IS NULL OR char_length(engine_code) BETWEEN 1 AND 32)
        AND (final_code IS NULL OR char_length(final_code) BETWEEN 1 AND 32)
    ),
    CONSTRAINT coder_feedback_decisions_action_shape_valid CHECK (
        (action = 'add' AND engine_code IS NULL AND final_code IS NOT NULL)
        OR (action = 'reject' AND engine_code IS NOT NULL AND final_code IS NULL)
        OR (
            action IN ('accept', 'modify')
            AND engine_code IS NOT NULL
            AND final_code IS NOT NULL
        )
    ),
    CONSTRAINT coder_feedback_decisions_reason_valid CHECK (
        reason_code IS NULL OR reason_code IN (
            'em_doc_supports_higher_level',
            'em_doc_supports_lower_level',
            'em_mdm_element_disagreement',
            'em_time_based_leveling',
            'dx_not_supported_by_doc',
            'dx_contradicted_by_doc',
            'dx_needs_more_specific_code',
            'dx_primary_secondary_swap',
            'dx_missed_by_engine',
            'modifier_required',
            'procedure_bundling_ncci',
            'medical_necessity_coverage',
            'other'
        )
    ),
    CONSTRAINT coder_feedback_decisions_note_valid CHECK (
        char_length(note) <= 4000
    )
);

CREATE INDEX coder_feedback_decisions_signal_idx
    ON coder_feedback_decisions (
        practice_id, kind, action, engine_code, final_code, reason_code
    );

-- One deterministic row per review, with decisions ordered exactly as the
-- reviewer submitted them. The view executes with the caller's privileges so
-- the underlying forced RLS policies cannot be bypassed through the view owner.
CREATE VIEW adaptive_preference_source
WITH (security_invoker = true)
AS
SELECT
    feedback.practice_id,
    feedback.feedback_id,
    feedback.review_id,
    feedback.encounter_uid,
    encounter.external_id AS encounter_id,
    feedback.coder_tenant_id::text,
    feedback.coder_user_id::text AS coder_id,
    feedback.engine_version,
    feedback.submitted_em,
    feedback.engine_em,
    feedback.final_em,
    feedback.encounter_version,
    COALESCE(
        jsonb_agg(
            jsonb_build_object(
                'kind', decision.kind,
                'engine_code', decision.engine_code,
                'action', decision.action,
                'final_code', decision.final_code,
                'reason_code', decision.reason_code,
                'note', decision.note
            ) ORDER BY decision.ordinal
        ) FILTER (WHERE decision.feedback_id IS NOT NULL),
        '[]'::jsonb
    ) AS decisions,
    feedback.time_spent_seconds,
    feedback.overall_note,
    extract(epoch FROM feedback.submitted_at)::double precision AS ts,
    feedback.payload_hash
FROM coder_feedback AS feedback
JOIN encounters AS encounter
    ON encounter.practice_id = feedback.practice_id
   AND encounter.encounter_uid = feedback.encounter_uid
LEFT JOIN coder_feedback_decisions AS decision
    ON decision.practice_id = feedback.practice_id
   AND decision.feedback_id = feedback.feedback_id
GROUP BY
    feedback.practice_id,
    feedback.feedback_id,
    feedback.review_id,
    feedback.encounter_uid,
    encounter.external_id,
    feedback.coder_tenant_id,
    feedback.coder_user_id,
    feedback.engine_version,
    feedback.submitted_em,
    feedback.engine_em,
    feedback.final_em,
    feedback.encounter_version,
    feedback.time_spent_seconds,
    feedback.overall_note,
    feedback.submitted_at,
    feedback.payload_hash
ORDER BY feedback.feedback_id;

-- Attempt reservations are committed before a protected route runs. A
-- completion is inserted in the same transaction as a successful mutation.
-- Keeping the phases separate makes both records append-only and leaves an
-- attempted row without a completion as an explicit reconciliation signal.
CREATE TABLE access_audit_attempts (
    practice_id       text        NOT NULL,
    event_id          uuid        NOT NULL,
    occurred_at       timestamptz NOT NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    actor_tenant_id   uuid,
    actor_user_id     uuid,
    actor_roles       text[]      NOT NULL DEFAULT ARRAY[]::text[],
    action            text        NOT NULL,
    route             text        NOT NULL,
    method            text        NOT NULL,
    source_ip         text        NOT NULL,
    PRIMARY KEY (practice_id, event_id),
    CONSTRAINT access_audit_attempts_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT access_audit_attempts_actor_valid CHECK (
        (
            actor_tenant_id IS NULL
            AND actor_user_id IS NULL
            AND cardinality(actor_roles) = 0
        ) OR (
            actor_tenant_id IS NOT NULL
            AND actor_user_id IS NOT NULL
            AND cardinality(actor_roles) BETWEEN 1 AND 16
        )
    ),
    CONSTRAINT access_audit_attempts_roles_size_valid CHECK (
        cardinality(actor_roles) <= 16
        AND octet_length(array_to_string(actor_roles, ',')) <= 8192
    ),
    CONSTRAINT access_audit_attempts_action_valid CHECK (
        char_length(action) BETWEEN 1 AND 128
        AND action !~ '[[:cntrl:]]'
    ),
    CONSTRAINT access_audit_attempts_route_valid CHECK (
        char_length(route) BETWEEN 1 AND 256
        AND route !~ '[[:cntrl:]]'
    ),
    CONSTRAINT access_audit_attempts_method_valid CHECK (
        method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')
    ),
    CONSTRAINT access_audit_attempts_source_ip_valid CHECK (
        char_length(source_ip) BETWEEN 1 AND 128
        AND source_ip !~ '[[:cntrl:]]'
    )
);

CREATE INDEX access_audit_attempts_time_idx
    ON access_audit_attempts (practice_id, occurred_at DESC, event_id);
CREATE INDEX access_audit_attempts_actor_idx
    ON access_audit_attempts (
        practice_id, actor_user_id, occurred_at DESC
    ) WHERE actor_user_id IS NOT NULL;
CREATE INDEX access_audit_attempts_action_idx
    ON access_audit_attempts (
        practice_id, action, occurred_at DESC
    );

CREATE TABLE access_audit_completions (
    practice_id    text        NOT NULL,
    event_id       uuid        NOT NULL,
    completed_at   timestamptz NOT NULL,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    status         integer     NOT NULL,
    outcome        text        NOT NULL,
    resource_id    text,
    PRIMARY KEY (practice_id, event_id),
    CONSTRAINT access_audit_completions_attempt_fk
        FOREIGN KEY (practice_id, event_id)
        REFERENCES access_audit_attempts (practice_id, event_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT access_audit_completions_status_valid CHECK (
        status BETWEEN 100 AND 599
    ),
    CONSTRAINT access_audit_completions_outcome_valid CHECK (
        outcome IN ('allowed', 'denied', 'throttled', 'error')
    ),
    CONSTRAINT access_audit_completions_resource_valid CHECK (
        resource_id IS NULL OR (
            char_length(resource_id) BETWEEN 1 AND 128
            AND resource_id !~ '[[:cntrl:]]'
        )
    )
);

CREATE INDEX access_audit_completions_time_idx
    ON access_audit_completions (practice_id, completed_at DESC, event_id);
CREATE INDEX access_audit_completions_outcome_idx
    ON access_audit_completions (
        practice_id, outcome, status, completed_at DESC
    );

-- Client-stable staff-action UUIDs make contact-log and state-transition
-- retries exactly-once. Receipts are append-only and contain no request body.
CREATE TABLE chase_action_receipts (
    practice_id   text        NOT NULL,
    action_id     uuid        NOT NULL,
    chase_id      text        NOT NULL,
    payload_hash  text        NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, action_id),
    CONSTRAINT chase_action_receipts_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_action_receipts_hash_valid CHECK (
        payload_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX chase_action_receipts_item_idx
    ON chase_action_receipts (practice_id, chase_id, recorded_at DESC);

-- A runtime login is bound to exactly one pilot practice. Policies require
-- both the server-selected GUC and this protected session-user binding, so a
-- leaked runtime credential cannot switch tenants by setting a custom GUC.
CREATE TABLE runtime_practice_bindings (
    role_name    name        PRIMARY KEY,
    practice_id  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT runtime_practice_bindings_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
);

REVOKE ALL ON runtime_practice_bindings FROM PUBLIC;

CREATE FUNCTION cosmaneura_practice_allowed(candidate_practice_id text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = off
AS $$
    SELECT
        candidate_practice_id = NULLIF(
            current_setting('cosmaneura.practice_id', true), ''
        )
        AND (
            session_user = current_user
            OR EXISTS (
                SELECT 1
                FROM public.runtime_practice_bindings AS binding
                WHERE binding.role_name = session_user::name
                  AND binding.practice_id = candidate_practice_id
            )
        )
$$;

REVOKE ALL ON FUNCTION cosmaneura_practice_allowed(text) FROM PUBLIC;

-- These SECURITY DEFINER checks lock the authorization rows in the caller's
-- transaction without granting the runtime login UPDATE on identity tables.
-- A concurrent disable must therefore serialize before or after the domain
-- mutation; it cannot slip between authorization and commit.
CREATE FUNCTION cosmaneura_feedback_reviewer_allowed(
    candidate_tenant_id uuid,
    candidate_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    allowed boolean := false;
BEGIN
    SELECT true INTO allowed
      FROM public.practice_memberships AS membership
      JOIN public.practices AS practice
        ON practice.practice_id = membership.practice_id
      JOIN public.users AS app_user
        ON app_user.tenant_id = membership.tenant_id
       AND app_user.user_id = membership.user_id
     WHERE membership.practice_id = NULLIF(
               current_setting('cosmaneura.practice_id', true), ''
           )
       AND public.cosmaneura_practice_allowed(membership.practice_id)
       AND membership.tenant_id = candidate_tenant_id
       AND membership.user_id = candidate_user_id
       AND membership.active AND practice.active AND app_user.active
       AND membership.roles && ARRAY['Admin', 'Coder']::text[]
     LIMIT 1
     FOR SHARE OF membership, practice, app_user;
    RETURN COALESCE(allowed, false);
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_feedback_reviewer_allowed(uuid, uuid)
    FROM PUBLIC;

CREATE FUNCTION cosmaneura_staff_assignee_allowed(
    candidate_tenant_id uuid,
    candidate_user_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    allowed boolean := false;
BEGIN
    SELECT true INTO allowed
      FROM public.practice_memberships AS membership
      JOIN public.practices AS practice
        ON practice.practice_id = membership.practice_id
      JOIN public.users AS app_user
        ON app_user.tenant_id = membership.tenant_id
       AND app_user.user_id = membership.user_id
     WHERE membership.practice_id = NULLIF(
               current_setting('cosmaneura.practice_id', true), ''
           )
       AND public.cosmaneura_practice_allowed(membership.practice_id)
       AND membership.tenant_id = candidate_tenant_id
       AND membership.user_id = candidate_user_id
       AND membership.active AND practice.active AND app_user.active
       AND membership.roles && ARRAY['Admin', 'BillingStaff']::text[]
     LIMIT 1
     FOR SHARE OF membership, practice, app_user;
    RETURN COALESCE(allowed, false);
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_staff_assignee_allowed(uuid, uuid)
    FROM PUBLIC;

-- Every tenant table is fail-closed when the custom setting is
-- absent. Runtime transactions must execute:
--   SET LOCAL cosmaneura.practice_id = '<validated server-owned practice id>';
-- FORCE also subjects the table owner to these policies. Migration execution
-- remains possible because PostgreSQL DDL is not filtered by row policies.
CREATE POLICY practices_practice_isolation ON practices
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE practices ENABLE ROW LEVEL SECURITY;
ALTER TABLE practices FORCE ROW LEVEL SECURITY;

CREATE POLICY users_practice_isolation ON users
    USING (
        EXISTS (
            SELECT 1
            FROM practices
            WHERE cosmaneura_practice_allowed(practices.practice_id)
              AND practices.entra_tenant_id = users.tenant_id
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM practices
            WHERE cosmaneura_practice_allowed(practices.practice_id)
              AND practices.entra_tenant_id = users.tenant_id
        )
    );
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;

CREATE POLICY practice_memberships_practice_isolation ON practice_memberships
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE practice_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE practice_memberships FORCE ROW LEVEL SECURITY;

CREATE POLICY encounters_practice_isolation ON encounters
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE encounters ENABLE ROW LEVEL SECURITY;
ALTER TABLE encounters FORCE ROW LEVEL SECURITY;

CREATE POLICY coder_feedback_practice_isolation ON coder_feedback
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE coder_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE coder_feedback FORCE ROW LEVEL SECURITY;

CREATE POLICY coder_feedback_decisions_practice_isolation
    ON coder_feedback_decisions
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE coder_feedback_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE coder_feedback_decisions FORCE ROW LEVEL SECURITY;

CREATE POLICY access_audit_attempts_practice_isolation
    ON access_audit_attempts
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE access_audit_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_audit_attempts FORCE ROW LEVEL SECURITY;

CREATE POLICY access_audit_completions_practice_isolation
    ON access_audit_completions
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE access_audit_completions ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_audit_completions FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_items_practice_isolation ON chase_items
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_items FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_attempts_practice_isolation ON chase_attempts
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_attempts FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_pending_tasks_practice_isolation ON chase_pending_tasks
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_pending_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_pending_tasks FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_resolutions_practice_isolation ON chase_resolutions
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_resolutions FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_action_receipts_practice_isolation
    ON chase_action_receipts
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_action_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_action_receipts FORCE ROW LEVEL SECURITY;

COMMIT;
