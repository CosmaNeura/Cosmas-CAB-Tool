-- Durable communications preparation and inbound ledger. No dispatch authority.
-- Existing policy/outbox tables and their email-only provider constraint stay intact.
BEGIN;

-- A configured account/sender belongs to exactly one practice. Provider callbacks
-- must resolve this server-owned binding before selecting a practice repository.
CREATE TABLE chase_communication_senders (
    practice_id text NOT NULL REFERENCES practices(practice_id),
    account_sid text NOT NULL CHECK (account_sid ~ '^AC[0-9a-fA-F]{32}$'),
    sender text NOT NULL CHECK (sender ~ '^\+[1-9][0-9]{7,14}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_sid, sender),
    UNIQUE (practice_id, account_sid, sender)
);

-- Versioned, immutable references to existing contact evidence. This migration
-- neither imports nor overwrites contacts and does not infer voice consent.
CREATE TABLE chase_communication_bindings (
    practice_id text NOT NULL,
    binding_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('sms', 'automated_voice')),
    contact_snapshot_id uuid NOT NULL,
    route_channel text NOT NULL,
    account_sid text NOT NULL,
    sender text NOT NULL,
    phone text NOT NULL CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
    patient_ref text NOT NULL,
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, binding_id),
    UNIQUE (practice_id, binding_id, case_id, channel),
    UNIQUE (practice_id, case_id, channel, contact_snapshot_id, account_sid, sender),
    FOREIGN KEY (practice_id, contact_snapshot_id, case_id)
        REFERENCES chase_contact_snapshots(practice_id, contact_snapshot_id, chase_id),
    FOREIGN KEY (practice_id, contact_snapshot_id, route_channel)
        REFERENCES chase_contact_routes(practice_id, contact_snapshot_id, channel),
    FOREIGN KEY (practice_id, account_sid, sender)
        REFERENCES chase_communication_senders(practice_id, account_sid, sender),
    FOREIGN KEY (practice_id, actor_tenant_id, actor_id)
        REFERENCES practice_memberships(practice_id, tenant_id, user_id),
    CHECK ((channel = 'sms' AND route_channel = 'sms') OR
           (channel = 'automated_voice' AND route_channel = 'phone'))
);
CREATE INDEX chase_communication_bindings_case_idx
    ON chase_communication_bindings(practice_id, case_id, channel, created_at DESC);
CREATE INDEX chase_communication_bindings_route_idx
    ON chase_communication_bindings(practice_id, account_sid, sender, phone, channel);

-- These are reviewable preparations, not the scheduler's outbox. There is no
-- lease, dispatch, provider SID, or worker claim. Preparing does not add an Attempt.
CREATE TABLE chase_communication_operations (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    case_id text NOT NULL,
    binding_id uuid NOT NULL,
    channel text NOT NULL CHECK (channel IN ('sms', 'automated_voice')),
    text text NOT NULL CHECK (char_length(text) BETWEEN 1 AND 1600),
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'prepared' CHECK (status = 'prepared'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, operation_id),
    UNIQUE (practice_id, operation_id, case_id, channel),
    FOREIGN KEY (practice_id, binding_id, case_id, channel)
        REFERENCES chase_communication_bindings(practice_id, binding_id, case_id, channel),
    FOREIGN KEY (practice_id, actor_tenant_id, actor_id)
        REFERENCES practice_memberships(practice_id, tenant_id, user_id)
);
CREATE INDEX chase_communication_operations_case_idx
    ON chase_communication_operations(practice_id, case_id, created_at, operation_id);

CREATE TABLE chase_communication_events (
    sequence_no bigint GENERATED ALWAYS AS IDENTITY,
    practice_id text NOT NULL REFERENCES practices(practice_id),
    event_id text NOT NULL CHECK (event_id ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    account_sid text NOT NULL,
    provider_sid text CHECK (provider_sid ~ '^(SM|MM|CA)[0-9a-fA-F]{32}$'),
    case_id text,
    operation_id uuid,
    sender text NOT NULL CHECK (sender ~ '^\+[1-9][0-9]{7,14}$'),
    recipient text NOT NULL CHECK (recipient ~ '^\+[1-9][0-9]{7,14}$'),
    channel text NOT NULL CHECK (channel IN ('sms', 'automated_voice')),
    kind text NOT NULL CHECK (kind IN ('outbound_prepared', 'inbound_sms', 'staff_suppression')),
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound', 'staff')),
    text text NOT NULL CHECK (char_length(text) <= 1600),
    status text NOT NULL,
    disposition text CHECK (disposition IN ('sms_opt_out', 'sms_opt_in_review',
        'help_requested', 'reply_received', 'stop_calls')),
    actor_tenant_id uuid,
    actor_id uuid,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (practice_id, account_sid, event_id),
    UNIQUE (practice_id, sequence_no),
    FOREIGN KEY (practice_id, case_id) REFERENCES chase_items(practice_id, chase_id),
    FOREIGN KEY (practice_id, operation_id, case_id, channel)
        REFERENCES chase_communication_operations(practice_id, operation_id, case_id, channel),
    FOREIGN KEY (practice_id, actor_tenant_id, actor_id)
        REFERENCES practice_memberships(practice_id, tenant_id, user_id),
    CHECK ((actor_id IS NULL) = (actor_tenant_id IS NULL)),
    CHECK (
        (kind = 'outbound_prepared' AND direction = 'outbound' AND status = 'prepared'
         AND operation_id IS NOT NULL AND case_id IS NOT NULL AND actor_id IS NOT NULL
         AND provider_sid IS NULL AND disposition IS NULL) OR
        (kind = 'inbound_sms' AND direction = 'inbound' AND channel = 'sms'
         AND status = 'received' AND operation_id IS NULL AND actor_id IS NULL
         AND provider_sid IS NOT NULL AND provider_sid ~ '^(SM|MM)[0-9a-fA-F]{32}$'
         AND disposition IS NOT NULL) OR
        (kind = 'staff_suppression' AND direction = 'staff' AND status = 'suppressed'
         AND operation_id IS NULL AND case_id IS NOT NULL AND actor_id IS NOT NULL
         AND provider_sid IS NULL AND disposition IS NOT NULL
         AND disposition IN ('sms_opt_out', 'stop_calls'))
    )
);
CREATE UNIQUE INDEX chase_communication_inbound_sid_idx
    ON chase_communication_events(practice_id, account_sid, provider_sid)
    WHERE kind = 'inbound_sms';
CREATE INDEX chase_communication_events_case_idx
    ON chase_communication_events(practice_id, case_id, sequence_no);

-- Immutable first suppression plus every request preserved in the event ledger.
-- START is an event for review; no deletion/unsuppression method is provided.
CREATE TABLE chase_communication_suppressions (
    practice_id text NOT NULL,
    account_sid text NOT NULL,
    sender text NOT NULL,
    phone text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('sms', 'automated_voice')),
    event_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, account_sid, sender, phone, channel),
    FOREIGN KEY (practice_id, account_sid, sender)
        REFERENCES chase_communication_senders(practice_id, account_sid, sender),
    FOREIGN KEY (practice_id, account_sid, event_id)
        REFERENCES chase_communication_events(practice_id, account_sid, event_id)
);

CREATE FUNCTION cosmaneura_reject_communication_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'communications records are append-only' USING ERRCODE = 'check_violation';
END
$$;
REVOKE ALL ON FUNCTION cosmaneura_reject_communication_mutation() FROM PUBLIC;

-- Enforce the source-to-route and opt-out-to-suppression links even for direct
-- SQL writers. Functions run with the caller's forced practice RLS boundary.
CREATE FUNCTION cosmaneura_validate_communication_evidence()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
BEGIN
    IF TG_TABLE_NAME = 'chase_communication_bindings' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.chase_contact_routes r
            JOIN public.chase_items i ON i.practice_id=r.practice_id AND i.chase_id=NEW.case_id
            WHERE r.practice_id=NEW.practice_id AND r.contact_snapshot_id=NEW.contact_snapshot_id
              AND r.channel=NEW.route_channel AND r.destination=NEW.phone AND i.patient_ref=NEW.patient_ref
        ) THEN
            RAISE EXCEPTION 'communications contact evidence mismatch' USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        IF NOT EXISTS (
            SELECT 1 FROM public.chase_communication_events e
            WHERE e.practice_id=NEW.practice_id AND e.account_sid=NEW.account_sid
              AND e.event_id=NEW.event_id AND e.channel=NEW.channel
              AND ((NEW.channel='sms' AND e.disposition='sms_opt_out')
                OR (NEW.channel='automated_voice' AND e.disposition='stop_calls'))
              AND ((e.direction='inbound' AND e.sender=NEW.phone AND e.recipient=NEW.sender)
                OR (e.direction='staff' AND e.sender=NEW.sender AND e.recipient=NEW.phone))
        ) THEN
            RAISE EXCEPTION 'communications suppression evidence mismatch' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION cosmaneura_validate_communication_evidence() FROM PUBLIC;
CREATE TRIGGER binding_evidence BEFORE INSERT ON chase_communication_bindings
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_communication_evidence();
CREATE TRIGGER suppression_evidence BEFORE INSERT ON chase_communication_suppressions
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_communication_evidence();
ALTER TABLE chase_communication_bindings ENABLE ALWAYS TRIGGER binding_evidence;
ALTER TABLE chase_communication_suppressions ENABLE ALWAYS TRIGGER suppression_evidence;

DO $$
DECLARE relation text;
BEGIN
    FOREACH relation IN ARRAY ARRAY['chase_communication_senders',
        'chase_communication_bindings', 'chase_communication_operations',
        'chase_communication_events', 'chase_communication_suppressions'] LOOP
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC', relation);
        EXECUTE format('CREATE POLICY practice_isolation ON public.%I USING '
            '(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK '
            '(public.cosmaneura_practice_allowed(practice_id))', relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', relation);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', relation);
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON '
            'public.%I FOR EACH ROW EXECUTE FUNCTION '
            'public.cosmaneura_reject_communication_mutation()', relation);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON '
            'public.%I FOR EACH STATEMENT EXECUTE FUNCTION '
            'public.cosmaneura_reject_communication_mutation()', relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER immutable_rows', relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER immutable_table', relation);
    END LOOP;
END
$$;
COMMIT;
