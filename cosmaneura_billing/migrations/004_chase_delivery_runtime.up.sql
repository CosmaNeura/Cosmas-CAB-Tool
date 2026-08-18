-- 004_chase_delivery_runtime.up.sql
-- Durable, fail-closed scheduling state for the patient-contact chase.
--
-- This migration does not seed a policy, approve a policy, schedule a chase, or
-- reserve a delivery. Existing chase rows therefore remain inert after upgrade.
-- Contact routes use typed columns and are never copied into generic JSON.
-- Rendered messages, template bodies, subjects, and upload URLs are not stored.

BEGIN;

-- A contact snapshot is an immutable assertion about the routes observed for
-- one chase item. New source data creates a new snapshot; old evidence is never
-- overwritten. Staff verification records an immutable Entra identity, while
-- an authoritative source assertion records the named/versioned source only.
CREATE TABLE chase_contact_snapshots (
    practice_id             text        NOT NULL,
    contact_snapshot_id     uuid        NOT NULL DEFAULT gen_random_uuid(),
    chase_id                text        NOT NULL,
    source_system           text        NOT NULL,
    source_version          text        NOT NULL,
    observed_at             timestamptz NOT NULL,
    verified_at             timestamptz NOT NULL,
    verification_method     text        NOT NULL,
    verified_by_tenant_id   uuid,
    verified_by_user_id     uuid,
    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, contact_snapshot_id),
    CONSTRAINT chase_contact_snapshots_item_unique
        UNIQUE (practice_id, contact_snapshot_id, chase_id),
    CONSTRAINT chase_contact_snapshots_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_contact_snapshots_verifier_fk
        FOREIGN KEY (
            practice_id, verified_by_tenant_id, verified_by_user_id
        ) REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_contact_snapshots_source_valid CHECK (
        char_length(source_system) BETWEEN 1 AND 128
        AND source_system = btrim(source_system)
        AND source_system !~ '[[:cntrl:]]'
        AND char_length(source_version) BETWEEN 1 AND 256
        AND source_version = btrim(source_version)
        AND source_version !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_contact_snapshots_verification_valid CHECK (
        (
            verification_method = 'staff'
            AND verified_by_tenant_id IS NOT NULL
            AND verified_by_user_id IS NOT NULL
        ) OR (
            verification_method = 'authoritative_source'
            AND verified_by_tenant_id IS NULL
            AND verified_by_user_id IS NULL
        )
    ),
    CONSTRAINT chase_contact_snapshots_times_valid CHECK (
        verified_at >= observed_at AND created_at >= verified_at
    )
);

CREATE INDEX chase_contact_snapshots_item_idx
    ON chase_contact_snapshots (
        practice_id, chase_id, verified_at DESC, contact_snapshot_id
    );

-- Destinations are PHI-bearing routing data. They live only in this typed,
-- RLS-protected table; no route is serialized into a JSON payload or outbox row.
-- SMS/phone consent is unknown unless an authoritative feed supplies a record.
CREATE TABLE chase_contact_routes (
    practice_id           text        NOT NULL,
    contact_snapshot_id   uuid        NOT NULL,
    channel               text        NOT NULL,
    destination           text        NOT NULL,
    consent_status        text        NOT NULL,
    consent_source        text,
    consent_recorded_at   timestamptz,
    PRIMARY KEY (practice_id, contact_snapshot_id, channel),
    CONSTRAINT chase_contact_routes_snapshot_fk
        FOREIGN KEY (practice_id, contact_snapshot_id)
        REFERENCES chase_contact_snapshots (practice_id, contact_snapshot_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_contact_routes_channel_valid CHECK (
        channel IN ('email', 'sms', 'portal', 'phone')
    ),
    CONSTRAINT chase_contact_routes_destination_valid CHECK (
        char_length(destination) BETWEEN 1 AND 512
        AND destination = btrim(destination)
        AND destination !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_contact_routes_consent_status_valid CHECK (
        consent_status IN ('not_required', 'granted', 'denied', 'unknown')
    ),
    CONSTRAINT chase_contact_routes_consent_source_valid CHECK (
        consent_source IS NULL OR (
            char_length(consent_source) BETWEEN 1 AND 128
            AND consent_source = btrim(consent_source)
            AND consent_source !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT chase_contact_routes_consent_shape_valid CHECK (
        (
            consent_status IN ('granted', 'denied')
            AND consent_source IS NOT NULL
            AND consent_recorded_at IS NOT NULL
        ) OR (
            consent_status IN ('not_required', 'unknown')
            AND consent_source IS NULL
            AND consent_recorded_at IS NULL
        )
    )
);

-- A policy revision is immutable. policy_fingerprint is the SHA-256 of the
-- complete canonical policy, including every full template body and subject,
-- practice name, secret-configured upload URL, cadence and approval mode,
-- quiet hours, timezone, provider/resource identity, and enabled channel set.
-- Sender and reply-to addresses, contact max-age, scheduler-enabled state, and
-- the explicit operational-enabled latch are also bound. Turning on a
-- preprovisioned transport requires a new exact revision and approval. Template text, URLs,
-- provider resources, addresses, and other transport configuration are
-- intentionally not persisted. The worker recomputes this exact fingerprint from current
-- config; any code, template, or secret-config drift therefore fails closed.
CREATE TABLE chase_policy_revisions (
    practice_id             text        NOT NULL,
    policy_revision_id      uuid        NOT NULL DEFAULT gen_random_uuid(),
    policy_name             text        NOT NULL,
    revision_no             bigint      NOT NULL,
    policy_fingerprint      text        NOT NULL,
    approval_mode           text        NOT NULL,
    practice_name           text        NOT NULL,
    practice_timezone       text        NOT NULL,
    quiet_hours_start       time        NOT NULL,
    quiet_hours_end         time        NOT NULL,
    max_touches             integer     NOT NULL,
    created_by_tenant_id    uuid        NOT NULL,
    created_by_user_id      uuid        NOT NULL,
    created_at              timestamptz NOT NULL,
    PRIMARY KEY (practice_id, policy_revision_id),
    CONSTRAINT chase_policy_revisions_name_revision_unique
        UNIQUE (practice_id, policy_name, revision_no),
    CONSTRAINT chase_policy_revisions_name_fingerprint_unique
        UNIQUE (practice_id, policy_name, policy_fingerprint),
    CONSTRAINT chase_policy_revisions_exact_unique
        UNIQUE (practice_id, policy_revision_id, policy_fingerprint),
    CONSTRAINT chase_policy_revisions_mode_unique
        UNIQUE (
            practice_id, policy_revision_id, policy_fingerprint, approval_mode
        ),
    CONSTRAINT chase_policy_revisions_practice_fk
        FOREIGN KEY (practice_id)
        REFERENCES practices (practice_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_policy_revisions_creator_fk
        FOREIGN KEY (
            practice_id, created_by_tenant_id, created_by_user_id
        ) REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_policy_revisions_name_valid CHECK (
        char_length(policy_name) BETWEEN 1 AND 128
        AND policy_name = btrim(policy_name)
        AND policy_name !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_policy_revisions_revision_valid CHECK (revision_no > 0),
    CONSTRAINT chase_policy_revisions_fingerprint_valid CHECK (
        policy_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chase_policy_revisions_approval_mode_valid CHECK (
        approval_mode IN ('per_policy', 'per_message')
    ),
    CONSTRAINT chase_policy_revisions_practice_name_valid CHECK (
        char_length(practice_name) BETWEEN 1 AND 256
        AND practice_name = btrim(practice_name)
        AND practice_name !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_policy_revisions_timezone_valid CHECK (
        char_length(practice_timezone) BETWEEN 1 AND 128
        AND practice_timezone = btrim(practice_timezone)
        AND practice_timezone !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_policy_revisions_quiet_hours_valid CHECK (
        quiet_hours_start <> quiet_hours_end
        AND extract(minute FROM quiet_hours_start) = 0
        AND extract(second FROM quiet_hours_start) = 0
        AND extract(minute FROM quiet_hours_end) = 0
        AND extract(second FROM quiet_hours_end) = 0
    ),
    CONSTRAINT chase_policy_revisions_touch_limit_valid CHECK (
        max_touches BETWEEN 1 AND 64
    )
);

CREATE INDEX chase_policy_revisions_name_idx
    ON chase_policy_revisions (
        practice_id, policy_name, revision_no DESC, policy_revision_id
    );

-- day_offset is an absolute number of local calendar days after detection, not
-- an offset from the previous attempted touch. A template fingerprint binds the
-- full, unpersisted subject/body so the worker refuses stale executable code.
CREATE TABLE chase_policy_steps (
    practice_id           text    NOT NULL,
    policy_revision_id    uuid    NOT NULL,
    step_ordinal          integer NOT NULL,
    channel               text    NOT NULL,
    template_id           text    NOT NULL,
    day_offset            integer NOT NULL,
    performer             text    NOT NULL,
    template_fingerprint  text    NOT NULL,
    PRIMARY KEY (practice_id, policy_revision_id, step_ordinal),
    CONSTRAINT chase_policy_steps_delivery_key_unique
        UNIQUE (
            practice_id, policy_revision_id, step_ordinal, channel, template_id
        ),
    CONSTRAINT chase_policy_steps_revision_fk
        FOREIGN KEY (practice_id, policy_revision_id)
        REFERENCES chase_policy_revisions (practice_id, policy_revision_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_policy_steps_ordinal_valid CHECK (step_ordinal >= 0),
    CONSTRAINT chase_policy_steps_channel_valid CHECK (
        channel IN ('email', 'sms', 'portal', 'phone')
    ),
    CONSTRAINT chase_policy_steps_template_valid CHECK (
        char_length(template_id) BETWEEN 1 AND 128
        AND template_id = btrim(template_id)
        AND template_id !~ '[[:cntrl:]]'
        AND template_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chase_policy_steps_day_offset_valid CHECK (
        day_offset BETWEEN 0 AND 365
    ),
    CONSTRAINT chase_policy_steps_performer_valid CHECK (
        (channel = 'phone' AND performer = 'staff')
        OR (
            channel IN ('email', 'sms', 'portal')
            AND performer = 'system'
        )
    )
);

-- Approval and revocation are immutable events bound redundantly to the exact
-- policy fingerprint. The migration inserts no event, so no policy is approved
-- merely because the schema exists.
CREATE TABLE chase_policy_approval_events (
    practice_id           text        NOT NULL,
    approval_event_id     uuid        NOT NULL,
    policy_revision_id    uuid        NOT NULL,
    policy_fingerprint    text        NOT NULL,
    event_type            text        NOT NULL,
    actor_tenant_id       uuid        NOT NULL,
    actor_user_id         uuid        NOT NULL,
    occurred_at           timestamptz NOT NULL,
    reason                text        NOT NULL,
    payload_hash          text        NOT NULL,
    PRIMARY KEY (practice_id, approval_event_id),
    CONSTRAINT chase_policy_approval_events_exact_unique
        UNIQUE (
            practice_id, approval_event_id, policy_revision_id,
            policy_fingerprint, event_type
        ),
    CONSTRAINT chase_policy_approval_events_revision_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, policy_fingerprint
        ) REFERENCES chase_policy_revisions (
            practice_id, policy_revision_id, policy_fingerprint
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_policy_approval_events_actor_fk
        FOREIGN KEY (practice_id, actor_tenant_id, actor_user_id)
        REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_policy_approval_events_type_valid CHECK (
        event_type IN ('approved', 'revoked')
    ),
    CONSTRAINT chase_policy_approval_events_reason_valid CHECK (
        char_length(reason) <= 2000
        AND reason !~ '[[:cntrl:]]'
        AND (event_type <> 'revoked' OR char_length(btrim(reason)) > 0)
    ),
    CONSTRAINT chase_policy_approval_events_payload_hash_valid CHECK (
        payload_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX chase_policy_approval_events_revision_idx
    ON chase_policy_approval_events (
        practice_id, policy_revision_id, occurred_at DESC, approval_event_id
    );

-- PER_MESSAGE approval is a separate, immutable release of one exact step to
-- one verified contact snapshot. It is not inferred from the mutable READY
-- state. The unique step key prevents duplicate releases/reservations.
CREATE TABLE chase_message_releases (
    practice_id          text        NOT NULL,
    release_id           uuid        NOT NULL,
    chase_id             text        NOT NULL,
    policy_revision_id   uuid        NOT NULL,
    policy_fingerprint   text        NOT NULL,
    approval_mode        text        NOT NULL DEFAULT 'per_message',
    step_ordinal         integer     NOT NULL,
    contact_snapshot_id  uuid        NOT NULL,
    actor_tenant_id      uuid        NOT NULL,
    actor_user_id        uuid        NOT NULL,
    reason               text        NOT NULL,
    released_at          timestamptz NOT NULL,
    payload_hash         text        NOT NULL,
    PRIMARY KEY (practice_id, release_id),
    CONSTRAINT chase_message_releases_step_unique
        UNIQUE (
            practice_id, chase_id, policy_revision_id, step_ordinal,
            contact_snapshot_id
        ),
    CONSTRAINT chase_message_releases_outbox_key_unique
        UNIQUE (
            practice_id, release_id, chase_id, policy_revision_id,
            policy_fingerprint, step_ordinal, contact_snapshot_id
        ),
    CONSTRAINT chase_message_releases_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_message_releases_policy_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, policy_fingerprint, approval_mode
        ) REFERENCES chase_policy_revisions (
            practice_id, policy_revision_id, policy_fingerprint, approval_mode
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_message_releases_step_fk
        FOREIGN KEY (practice_id, policy_revision_id, step_ordinal)
        REFERENCES chase_policy_steps (
            practice_id, policy_revision_id, step_ordinal
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_message_releases_contact_fk
        FOREIGN KEY (
            practice_id, contact_snapshot_id, chase_id
        ) REFERENCES chase_contact_snapshots (
            practice_id, contact_snapshot_id, chase_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_message_releases_actor_fk
        FOREIGN KEY (practice_id, actor_tenant_id, actor_user_id)
        REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_message_releases_mode_valid CHECK (
        approval_mode = 'per_message'
    ),
    CONSTRAINT chase_message_releases_reason_valid CHECK (
        char_length(btrim(reason)) BETWEEN 1 AND 2000
        AND reason !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_message_releases_payload_hash_valid CHECK (
        payload_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX chase_message_releases_item_idx
    ON chase_message_releases (
        practice_id, chase_id, released_at DESC, release_id
    );

-- A direct-contract or unknown-source review is released only for the exact
-- deterministic gate fingerprint a human saw. Any changed source/fingerprint
-- has no matching row and therefore fails closed during planning.
CREATE TABLE chase_review_releases (
    practice_id        text        NOT NULL,
    review_release_id  uuid        NOT NULL,
    chase_id           text        NOT NULL,
    gate_fingerprint   text        NOT NULL,
    actor_tenant_id    uuid        NOT NULL,
    actor_user_id      uuid        NOT NULL,
    reason             text        NOT NULL,
    released_at        timestamptz NOT NULL,
    payload_hash       text        NOT NULL,
    PRIMARY KEY (practice_id, review_release_id),
    CONSTRAINT chase_review_releases_gate_unique
        UNIQUE (practice_id, chase_id, gate_fingerprint),
    CONSTRAINT chase_review_releases_outbox_key_unique
        UNIQUE (
            practice_id, review_release_id, chase_id, gate_fingerprint
        ),
    CONSTRAINT chase_review_releases_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_review_releases_actor_fk
        FOREIGN KEY (practice_id, actor_tenant_id, actor_user_id)
        REFERENCES practice_memberships (practice_id, tenant_id, user_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_review_releases_gate_fingerprint_valid CHECK (
        gate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chase_review_releases_reason_valid CHECK (
        char_length(btrim(reason)) BETWEEN 1 AND 2000
        AND reason !~ '[[:cntrl:]]'
    ),
    CONSTRAINT chase_review_releases_payload_hash_valid CHECK (
        payload_hash ~ '^[0-9a-f]{64}$'
    )
);

CREATE INDEX chase_review_releases_item_idx
    ON chase_review_releases (
        practice_id, chase_id, released_at DESC, review_release_id
    );

-- Exactly one policy can be active for a practice. The checked literal in the
-- composite FK means a revocation event can never activate a policy. Approval
-- and revocation services lock and update/delete this pointer in the same
-- transaction in which they append their immutable event.
CREATE TABLE chase_active_policies (
    practice_id          text        NOT NULL,
    policy_revision_id   uuid        NOT NULL,
    policy_fingerprint   text        NOT NULL,
    approval_event_id    uuid        NOT NULL,
    approval_event_type  text        NOT NULL DEFAULT 'approved',
    activated_at         timestamptz NOT NULL,
    updated_at           timestamptz NOT NULL,
    version              bigint      NOT NULL DEFAULT 1,
    PRIMARY KEY (practice_id),
    CONSTRAINT chase_active_policies_revision_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, policy_fingerprint
        ) REFERENCES chase_policy_revisions (
            practice_id, policy_revision_id, policy_fingerprint
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_active_policies_approval_fk
        FOREIGN KEY (
            practice_id, approval_event_id, policy_revision_id,
            policy_fingerprint, approval_event_type
        ) REFERENCES chase_policy_approval_events (
            practice_id, approval_event_id, policy_revision_id,
            policy_fingerprint, event_type
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_active_policies_approved_event_valid CHECK (
        approval_event_type = 'approved'
    ),
    CONSTRAINT chase_active_policies_version_valid CHECK (version > 0),
    CONSTRAINT chase_active_policies_times_valid CHECK (
        updated_at >= activated_at
    )
);

-- A cadence row is deliberately optional. Existing chase items receive no row
-- and are therefore unscheduled after migration. next_step_ordinal names the
-- next exact policy step; it is independent of staff-call/contact-log counts.
CREATE TABLE chase_cadence_states (
    practice_id          text        NOT NULL,
    chase_id             text        NOT NULL,
    policy_revision_id   uuid        NOT NULL,
    contact_snapshot_id  uuid        NOT NULL,
    next_step_ordinal    integer     NOT NULL,
    next_due_at          timestamptz NOT NULL,
    version              bigint      NOT NULL DEFAULT 1,
    updated_at           timestamptz NOT NULL,
    PRIMARY KEY (practice_id, chase_id),
    CONSTRAINT chase_cadence_states_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_cadence_states_step_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, next_step_ordinal
        ) REFERENCES chase_policy_steps (
            practice_id, policy_revision_id, step_ordinal
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_cadence_states_contact_fk
        FOREIGN KEY (
            practice_id, contact_snapshot_id, chase_id
        ) REFERENCES chase_contact_snapshots (
            practice_id, contact_snapshot_id, chase_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_cadence_states_version_valid CHECK (version > 0)
);

CREATE INDEX chase_cadence_states_due_idx
    ON chase_cadence_states (
        practice_id, next_due_at, chase_id
    );

-- The outbox stores only immutable references needed to re-derive a message.
-- It never stores a destination, subject, body, template body, or upload URL.
-- The UUID provider_operation_id is generated before submission and reused for
-- every reconciliation attempt. The pilot reserves ACS email deliveries only;
-- SMS remains fail-closed until a consent feed and transport land.
CREATE TABLE chase_delivery_outbox (
    practice_id           text        NOT NULL,
    delivery_id           uuid        NOT NULL DEFAULT gen_random_uuid(),
    provider_operation_id uuid        NOT NULL DEFAULT gen_random_uuid(),
    chase_id               text        NOT NULL,
    policy_revision_id     uuid        NOT NULL,
    policy_step_ordinal    integer     NOT NULL,
    approval_event_id      uuid        NOT NULL,
    approval_event_type    text        NOT NULL DEFAULT 'approved',
    policy_fingerprint     text        NOT NULL,
    approval_mode          text        NOT NULL,
    message_release_id     uuid,
    gate_fingerprint       text        NOT NULL,
    gate_disposition       text        NOT NULL,
    review_release_id      uuid,
    contact_snapshot_id    uuid        NOT NULL,
    channel                text        NOT NULL,
    template_id            text        NOT NULL,
    provider               text        NOT NULL,
    local_delivery_date    date        NOT NULL,
    state                  text        NOT NULL,
    available_at           timestamptz NOT NULL,
    lease_token            uuid,
    lease_expires_at       timestamptz,
    submission_started_at  timestamptz,
    finalized_at           timestamptz,
    provider_message_id    text,
    error_code             text,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    PRIMARY KEY (practice_id, delivery_id),
    CONSTRAINT chase_delivery_outbox_item_identity_unique
        UNIQUE (practice_id, delivery_id, chase_id),
    CONSTRAINT chase_delivery_outbox_attempt_identity_unique
        UNIQUE (
            practice_id, delivery_id, chase_id, channel, template_id
        ),
    CONSTRAINT chase_delivery_outbox_operation_unique
        UNIQUE (practice_id, provider_operation_id),
    CONSTRAINT chase_delivery_outbox_item_step_unique
        UNIQUE (
            practice_id, chase_id, policy_revision_id, policy_step_ordinal
        ),
    CONSTRAINT chase_delivery_outbox_item_local_day_unique
        UNIQUE (practice_id, chase_id, local_delivery_date),
    CONSTRAINT chase_delivery_outbox_item_fk
        FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_step_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, policy_step_ordinal,
            channel, template_id
        ) REFERENCES chase_policy_steps (
            practice_id, policy_revision_id, step_ordinal,
            channel, template_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_approval_fk
        FOREIGN KEY (
            practice_id, approval_event_id, policy_revision_id,
            policy_fingerprint, approval_event_type
        ) REFERENCES chase_policy_approval_events (
            practice_id, approval_event_id, policy_revision_id,
            policy_fingerprint, event_type
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_policy_mode_fk
        FOREIGN KEY (
            practice_id, policy_revision_id, policy_fingerprint, approval_mode
        ) REFERENCES chase_policy_revisions (
            practice_id, policy_revision_id, policy_fingerprint, approval_mode
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_message_release_fk
        FOREIGN KEY (
            practice_id, message_release_id, chase_id, policy_revision_id,
            policy_fingerprint, policy_step_ordinal, contact_snapshot_id
        ) REFERENCES chase_message_releases (
            practice_id, release_id, chase_id, policy_revision_id,
            policy_fingerprint, step_ordinal, contact_snapshot_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_review_release_fk
        FOREIGN KEY (
            practice_id, review_release_id, chase_id, gate_fingerprint
        ) REFERENCES chase_review_releases (
            practice_id, review_release_id, chase_id, gate_fingerprint
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_route_fk
        FOREIGN KEY (practice_id, contact_snapshot_id, channel)
        REFERENCES chase_contact_routes (
            practice_id, contact_snapshot_id, channel
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_contact_item_fk
        FOREIGN KEY (
            practice_id, contact_snapshot_id, chase_id
        ) REFERENCES chase_contact_snapshots (
            practice_id, contact_snapshot_id, chase_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_outbox_approved_event_valid CHECK (
        approval_event_type = 'approved'
    ),
    CONSTRAINT chase_delivery_outbox_message_release_valid CHECK (
        (
            approval_mode = 'per_policy'
            AND message_release_id IS NULL
        ) OR (
            approval_mode = 'per_message'
            AND message_release_id IS NOT NULL
        )
    ),
    CONSTRAINT chase_delivery_outbox_gate_fingerprint_valid CHECK (
        gate_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT chase_delivery_outbox_gate_release_valid CHECK (
        (
            gate_disposition = 'clear'
            AND review_release_id IS NULL
        ) OR (
            gate_disposition = 'released'
            AND review_release_id IS NOT NULL
        )
    ),
    CONSTRAINT chase_delivery_outbox_provider_valid CHECK (
        provider = 'azure_communication_services_email'
        AND channel = 'email'
    ),
    CONSTRAINT chase_delivery_outbox_state_valid CHECK (
        state IN (
            'reserved', 'submitting', 'accepted', 'ambiguous',
            'permanent_failure', 'cancelled'
        )
    ),
    CONSTRAINT chase_delivery_outbox_lease_shape_valid CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)
        AND (
            (state = 'submitting' AND lease_token IS NOT NULL)
            OR (state <> 'submitting' AND lease_token IS NULL)
        )
    ),
    CONSTRAINT chase_delivery_outbox_state_shape_valid CHECK (
        (
            state = 'reserved'
            AND submission_started_at IS NULL
            AND finalized_at IS NULL
        ) OR (
            state = 'submitting'
            AND submission_started_at IS NOT NULL
            AND finalized_at IS NULL
        ) OR (
            state IN ('accepted', 'ambiguous')
            AND submission_started_at IS NOT NULL
            AND finalized_at IS NOT NULL
        ) OR (
            state = 'permanent_failure'
            AND finalized_at IS NOT NULL
        ) OR (
            state = 'cancelled'
            AND submission_started_at IS NULL
            AND finalized_at IS NOT NULL
        )
    ),
    CONSTRAINT chase_delivery_outbox_provider_message_valid CHECK (
        provider_message_id IS NULL OR (
            char_length(provider_message_id) BETWEEN 1 AND 512
            AND provider_message_id !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT chase_delivery_outbox_error_code_valid CHECK (
        error_code IS NULL OR (
            char_length(error_code) BETWEEN 1 AND 128
            AND error_code = btrim(error_code)
            AND error_code !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT chase_delivery_outbox_times_valid CHECK (
        updated_at >= created_at
        AND (lease_expires_at IS NULL OR lease_expires_at > submission_started_at)
        AND (finalized_at IS NULL OR finalized_at >= submission_started_at)
    )
);

CREATE INDEX chase_delivery_outbox_dispatch_idx
    ON chase_delivery_outbox (
        practice_id, available_at, created_at, delivery_id
    ) WHERE state = 'reserved';
CREATE INDEX chase_delivery_outbox_lease_idx
    ON chase_delivery_outbox (
        practice_id, lease_expires_at, delivery_id
    ) WHERE state = 'submitting';

-- Legacy/staff attempts remain valid with a NULL delivery_id. An accepted
-- automated delivery can append exactly one contact-log row in the same
-- transaction that finalizes its outbox row; all provider/cadence metadata
-- remains authoritative on the referenced outbox row rather than duplicated.
ALTER TABLE chase_attempts
    ADD COLUMN delivery_id uuid,
    ADD CONSTRAINT chase_attempts_delivery_unique
        UNIQUE (practice_id, delivery_id),
    ADD CONSTRAINT chase_attempts_delivery_fk
        FOREIGN KEY (
            practice_id, delivery_id, chase_id, channel, template_id
        ) REFERENCES chase_delivery_outbox (
            practice_id, delivery_id, chase_id, channel, template_id
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT;

-- A delivery-linked contact-log entry is the atomic finalization proof, never a
-- reservation/submission guess. The service updates the outbox to accepted and
-- then appends this row in the same transaction. Staff/legacy attempts keep a
-- NULL delivery_id and do not cross this check.
CREATE FUNCTION cosmaneura_require_accepted_delivery_attempt()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    IF NEW.delivery_id IS NULL THEN
        RETURN NEW;
    END IF;

    PERFORM 1
      FROM public.chase_delivery_outbox AS delivery
     WHERE delivery.practice_id = NEW.practice_id
       AND delivery.delivery_id = NEW.delivery_id
       AND delivery.chase_id = NEW.chase_id
       AND delivery.channel = NEW.channel
       AND delivery.template_id = NEW.template_id
       AND delivery.state = 'accepted'
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'contact attempt requires an accepted exact delivery'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_require_accepted_delivery_attempt()
    FROM PUBLIC;

CREATE TRIGGER chase_attempts_accepted_delivery_guard
BEFORE INSERT OR UPDATE ON chase_attempts
FOR EACH ROW EXECUTE FUNCTION cosmaneura_require_accepted_delivery_attempt();

-- Delivery events are the append-only transition/audit trail. They store only
-- bounded provider identifiers and error categories, never exception text.
CREATE TABLE chase_delivery_events (
    practice_id         text        NOT NULL,
    delivery_id         uuid        NOT NULL,
    event_no            integer     NOT NULL,
    event_id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    event_type          text        NOT NULL,
    occurred_at         timestamptz NOT NULL,
    provider_message_id text,
    error_code          text,
    PRIMARY KEY (practice_id, delivery_id, event_no),
    CONSTRAINT chase_delivery_events_id_unique
        UNIQUE (practice_id, event_id),
    CONSTRAINT chase_delivery_events_delivery_fk
        FOREIGN KEY (practice_id, delivery_id)
        REFERENCES chase_delivery_outbox (practice_id, delivery_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT,
    CONSTRAINT chase_delivery_events_number_valid CHECK (event_no > 0),
    CONSTRAINT chase_delivery_events_type_valid CHECK (
        event_type IN (
            'reserved', 'submitting', 'accepted', 'ambiguous',
            'permanent_failure', 'cancelled'
        )
    ),
    CONSTRAINT chase_delivery_events_provider_message_valid CHECK (
        provider_message_id IS NULL OR (
            char_length(provider_message_id) BETWEEN 1 AND 512
            AND provider_message_id !~ '[[:cntrl:]]'
        )
    ),
    CONSTRAINT chase_delivery_events_error_code_valid CHECK (
        error_code IS NULL OR (
            char_length(error_code) BETWEEN 1 AND 128
            AND error_code = btrim(error_code)
            AND error_code !~ '[[:cntrl:]]'
        )
    )
);

-- Immutable evidence tables reject owner mistakes as well as missing runtime
-- grants. New source/config/effect evidence is always appended as a new row.
CREATE FUNCTION cosmaneura_reject_immutable_chase_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'immutable chase history cannot be updated or deleted'
        USING ERRCODE = '55000';
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_reject_immutable_chase_mutation() FROM PUBLIC;

CREATE TRIGGER chase_contact_snapshots_immutable
BEFORE UPDATE OR DELETE ON chase_contact_snapshots
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_contact_routes_immutable
BEFORE UPDATE OR DELETE ON chase_contact_routes
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_policy_revisions_immutable
BEFORE UPDATE OR DELETE ON chase_policy_revisions
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_policy_steps_immutable
BEFORE UPDATE OR DELETE ON chase_policy_steps
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

-- Once any approval/revocation event exists, the exact revision is sealed: a
-- caller cannot append a new cadence step beneath an already approved hash.
CREATE FUNCTION cosmaneura_reject_approved_policy_step_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM 1
      FROM public.chase_policy_revisions AS revision
     WHERE revision.practice_id = NEW.practice_id
       AND revision.policy_revision_id = NEW.policy_revision_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'chase policy revision does not exist'
            USING ERRCODE = '23503';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM public.chase_policy_approval_events AS approval
         WHERE approval.practice_id = NEW.practice_id
           AND approval.policy_revision_id = NEW.policy_revision_id
    ) THEN
        RAISE EXCEPTION 'approved chase policy steps are sealed'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_reject_approved_policy_step_insert()
    FROM PUBLIC;

CREATE TRIGGER chase_policy_steps_sealed_after_approval
BEFORE INSERT ON chase_policy_steps
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_approved_policy_step_insert();

-- Approval/revocation takes the same revision-row lock as step insertion. This
-- makes the approval event the serialization boundary: a step either commits
-- before the approving transaction revalidates the canonical policy, or waits
-- and is rejected after the event exists.
CREATE FUNCTION cosmaneura_lock_policy_revision_for_approval()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
BEGIN
    PERFORM 1
      FROM public.chase_policy_revisions AS revision
     WHERE revision.practice_id = NEW.practice_id
       AND revision.policy_revision_id = NEW.policy_revision_id
       AND revision.policy_fingerprint = NEW.policy_fingerprint
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'exact chase policy revision does not exist'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_lock_policy_revision_for_approval()
    FROM PUBLIC;

CREATE TRIGGER chase_policy_approval_events_revision_lock
BEFORE INSERT ON chase_policy_approval_events
FOR EACH ROW EXECUTE FUNCTION cosmaneura_lock_policy_revision_for_approval();

CREATE TRIGGER chase_policy_approval_events_immutable
BEFORE UPDATE OR DELETE ON chase_policy_approval_events
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_message_releases_immutable
BEFORE UPDATE OR DELETE ON chase_message_releases
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_review_releases_immutable
BEFORE UPDATE OR DELETE ON chase_review_releases
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

CREATE TRIGGER chase_delivery_events_immutable
BEFORE UPDATE OR DELETE ON chase_delivery_events
FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_immutable_chase_mutation();

-- Outbox identity and evidence references never change after reservation. An
-- expired submission lease is quarantined as ambiguous and is never re-leased
-- or blindly resent. Reconciliation may only confirm accepted delivery or a
-- permanent failure, after which the row is terminal.
CREATE FUNCTION cosmaneura_validate_delivery_outbox_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF ROW(
        NEW.practice_id, NEW.delivery_id, NEW.provider_operation_id,
        NEW.chase_id, NEW.policy_revision_id, NEW.policy_step_ordinal,
        NEW.approval_event_id, NEW.approval_event_type,
        NEW.policy_fingerprint, NEW.approval_mode, NEW.message_release_id,
        NEW.gate_fingerprint, NEW.gate_disposition, NEW.review_release_id,
        NEW.contact_snapshot_id, NEW.channel,
        NEW.template_id, NEW.provider, NEW.local_delivery_date,
        NEW.available_at, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.practice_id, OLD.delivery_id, OLD.provider_operation_id,
        OLD.chase_id, OLD.policy_revision_id, OLD.policy_step_ordinal,
        OLD.approval_event_id, OLD.approval_event_type,
        OLD.policy_fingerprint, OLD.approval_mode, OLD.message_release_id,
        OLD.gate_fingerprint, OLD.gate_disposition, OLD.review_release_id,
        OLD.contact_snapshot_id, OLD.channel,
        OLD.template_id, OLD.provider, OLD.local_delivery_date,
        OLD.available_at, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'delivery outbox identity is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        (OLD.state = 'reserved' AND NEW.state IN (
            'submitting', 'permanent_failure', 'cancelled'
        ))
        OR (OLD.state = 'submitting' AND NEW.state IN (
            'accepted', 'ambiguous', 'permanent_failure'
        ))
        OR (OLD.state = 'ambiguous' AND NEW.state IN (
            'accepted', 'permanent_failure'
        ))
    ) THEN
        RAISE EXCEPTION 'invalid delivery outbox state transition: % to %',
            OLD.state, NEW.state USING ERRCODE = '55000';
    END IF;

    IF OLD.state = 'submitting'
       AND NEW.state = 'ambiguous'
       AND OLD.lease_expires_at > statement_timestamp() THEN
        RAISE EXCEPTION 'active delivery lease cannot be quarantined'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_validate_delivery_outbox_update() FROM PUBLIC;

CREATE TRIGGER chase_delivery_outbox_transition_guard
BEFORE UPDATE ON chase_delivery_outbox
FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_delivery_outbox_update();

-- Policy creation/approval/revocation is Admin-only. Like the P3 reviewer and
-- assignee checks, this function locks the three authorization rows so a
-- concurrent disable cannot race a policy action to commit.
CREATE FUNCTION cosmaneura_chase_admin_allowed(
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
       AND 'Admin' = ANY (membership.roles)
     LIMIT 1
     FOR SHARE OF membership, practice, app_user;
    RETURN COALESCE(allowed, false);
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_chase_admin_allowed(uuid, uuid) FROM PUBLIC;

-- Every new domain table is subject to the P3 runtime-role binding and fails
-- closed when the server-owned practice setting is absent or mismatched.
CREATE POLICY chase_contact_snapshots_practice_isolation
    ON chase_contact_snapshots
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_contact_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_contact_snapshots FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_contact_routes_practice_isolation
    ON chase_contact_routes
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_contact_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_contact_routes FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_policy_revisions_practice_isolation
    ON chase_policy_revisions
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_policy_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_policy_revisions FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_policy_steps_practice_isolation
    ON chase_policy_steps
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_policy_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_policy_steps FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_policy_approval_events_practice_isolation
    ON chase_policy_approval_events
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_policy_approval_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_policy_approval_events FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_message_releases_practice_isolation
    ON chase_message_releases
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_message_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_message_releases FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_review_releases_practice_isolation
    ON chase_review_releases
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_review_releases ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_review_releases FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_active_policies_practice_isolation
    ON chase_active_policies
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_active_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_active_policies FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_cadence_states_practice_isolation
    ON chase_cadence_states
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_cadence_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_cadence_states FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_delivery_outbox_practice_isolation
    ON chase_delivery_outbox
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_delivery_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_delivery_outbox FORCE ROW LEVEL SECURITY;

CREATE POLICY chase_delivery_events_practice_isolation
    ON chase_delivery_events
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_delivery_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_delivery_events FORCE ROW LEVEL SECURITY;

COMMIT;
