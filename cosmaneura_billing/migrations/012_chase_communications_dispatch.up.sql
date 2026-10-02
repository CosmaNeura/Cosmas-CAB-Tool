-- Additive, dormant SMS/scripted-call lane. No data rewrite or activation.
BEGIN;
CREATE TABLE chase_communication_policy_decisions (
    sequence_no bigint GENERATED ALWAYS AS IDENTITY,
    practice_id text NOT NULL REFERENCES practices(practice_id),
    decision_id uuid NOT NULL,
    policy_hash text NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    approved boolean NOT NULL,
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,decision_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id)
);
CREATE INDEX communication_policy_latest ON chase_communication_policy_decisions(practice_id,sequence_no DESC);

-- Purpose-specific consent for this verified contact binding; PHONE consent
-- alone never authorizes automated voice. Revocation is another immutable row.
CREATE TABLE chase_communication_consent_decisions (
    sequence_no bigint GENERATED ALWAYS AS IDENTITY,
    practice_id text NOT NULL,
    decision_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    granted boolean NOT NULL,
    source_reference text NOT NULL CHECK (source_reference ~ '^[A-Za-z0-9_.:/-]{1,200}$'),
    purpose text NOT NULL DEFAULT 'insurance_followup' CHECK (purpose='insurance_followup'),
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,decision_id),
    UNIQUE (practice_id,decision_id,binding_id),
    FOREIGN KEY (practice_id,binding_id) REFERENCES chase_communication_bindings(practice_id,binding_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id)
);
CREATE INDEX communication_consent_latest ON chase_communication_consent_decisions(practice_id,binding_id,sequence_no DESC);

-- One exact approval per immutable prepared operation. Configuration, content,
-- contact, actor and purpose consent are bound before a worker may reserve it.
CREATE TABLE chase_communication_authorizations (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    policy_decision_id uuid NOT NULL,
    consent_decision_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    direct_contract_hash text NOT NULL CHECK (direct_contract_hash ~ '^[0-9a-f]{64}$'),
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,operation_id),
    FOREIGN KEY (practice_id,operation_id) REFERENCES chase_communication_operations(practice_id,operation_id),
    FOREIGN KEY (practice_id,policy_decision_id) REFERENCES chase_communication_policy_decisions(practice_id,decision_id),
    FOREIGN KEY (practice_id,consent_decision_id,binding_id)
        REFERENCES chase_communication_consent_decisions(practice_id,decision_id,binding_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id)
);
CREATE TABLE chase_communication_dispatch_starts (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,operation_id),
    FOREIGN KEY (practice_id,operation_id) REFERENCES chase_communication_authorizations(practice_id,operation_id)
);
CREATE TABLE chase_communication_dispatch_checks (
    sequence_no bigint GENERATED ALWAYS AS IDENTITY,
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    reason text NOT NULL CHECK (reason ~ '^communications_[a-z_]{1,80}$'),
    checked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,sequence_no),
    FOREIGN KEY (practice_id,operation_id) REFERENCES chase_communication_authorizations(practice_id,operation_id)
);
CREATE INDEX communication_dispatch_check_latest
    ON chase_communication_dispatch_checks(practice_id,operation_id,checked_at DESC);
-- The start commits BEFORE I/O. No receipt means uncertain after a crash; that
-- operation cannot be claimed again. An operator creates a new approval only
-- after investigating it; there is no automatic retry of an uncertain send.
CREATE TABLE chase_communication_dispatch_receipts (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    account_sid text NOT NULL CHECK (account_sid ~ '^AC[0-9a-fA-F]{32}$'),
    outcome text NOT NULL CHECK (outcome IN ('accepted','rejected','unknown')),
    provider_sid text CHECK (provider_sid ~ '^(SM|CA)[0-9a-fA-F]{32}$'),
    status text,
    error_code text CHECK (error_code ~ '^[a-z0-9_]{1,64}$'),
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,operation_id),
    UNIQUE (account_sid,provider_sid),
    FOREIGN KEY (practice_id,operation_id) REFERENCES chase_communication_dispatch_starts(practice_id,operation_id),
    CHECK ((outcome='accepted') = (provider_sid IS NOT NULL)),
    CHECK ((outcome='accepted') = (status IS NOT NULL))
);

-- Verified provider callbacks are append-only, including early status events
-- received before the POST receipt. Only exact SID + account + route receipts
-- attach status/call events to an operation. No phone-number guess does so.
CREATE TABLE chase_communication_provider_events (
    sequence_no bigint GENERATED ALWAYS AS IDENTITY,
    practice_id text NOT NULL REFERENCES practices(practice_id),
    account_sid text NOT NULL CHECK (account_sid ~ '^AC[0-9a-fA-F]{32}$'),
    event_id text NOT NULL CHECK (event_id ~ '^[0-9a-f]{64}$'),
    provider_sid text NOT NULL CHECK (provider_sid ~ '^(SM|CA)[0-9a-fA-F]{32}$'),
    kind text NOT NULL CHECK (kind IN ('sms_inbound','sms_status','voice_status','voice_gather')),
    channel text NOT NULL CHECK (channel IN ('sms','automated_voice')),
    sender text CHECK (sender ~ '^\+[1-9][0-9]{7,14}$'),
    recipient text CHECK (recipient ~ '^\+[1-9][0-9]{7,14}$'),
    case_id text,
    text text NOT NULL DEFAULT '' CHECK (char_length(text)<=1600),
    status text NOT NULL,
    disposition text,
    opt_out text CHECK (opt_out IN ('STOP','START','HELP')),
    media_count integer NOT NULL DEFAULT 0 CHECK (media_count BETWEEN 0 AND 10),
    provider_sequence integer CHECK (provider_sequence BETWEEN 0 AND 999999999),
    body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,account_sid,event_id),
    FOREIGN KEY (practice_id,case_id) REFERENCES chase_items(practice_id,chase_id),
    CHECK ((kind='sms_inbound' AND channel='sms' AND sender IS NOT NULL AND recipient IS NOT NULL AND status='received')
        OR (kind='sms_status' AND channel='sms' AND case_id IS NULL AND text='' AND disposition IS NULL)
        OR (kind IN ('voice_status','voice_gather') AND channel='automated_voice' AND case_id IS NULL AND text=''
            AND sender IS NOT NULL AND recipient IS NOT NULL)),
    CHECK ((channel='sms' AND provider_sid ~ '^SM') OR (channel='automated_voice' AND provider_sid ~ '^CA')),
    CHECK ((kind IN ('sms_inbound','voice_gather')) = (disposition IS NOT NULL)),
    CHECK ((kind='sms_inbound' AND disposition IN ('sms_opt_out','sms_opt_in_review','help_requested','reply_received'))
        OR (kind='sms_status' AND status IN ('accepted','scheduled','queued','sending','sent','delivered','undelivered','failed','canceled','read'))
        OR (kind IN ('voice_status','voice_gather') AND status IN ('queued','initiated','ringing','in-progress','completed','busy','failed','no-answer','canceled'))),
    CHECK ((kind='voice_gather' AND disposition IN ('callback_requested','reports_submitted','stop_calls','no_input','invalid_input'))
        OR kind='sms_inbound' OR (kind IN ('sms_status','voice_status') AND disposition IS NULL))
);
CREATE UNIQUE INDEX communication_provider_inbound_sid ON chase_communication_provider_events(practice_id,account_sid,provider_sid)
    WHERE kind='sms_inbound';
CREATE INDEX communication_provider_sid ON chase_communication_provider_events(practice_id,account_sid,provider_sid,sequence_no);
CREATE INDEX communication_provider_route ON chase_communication_provider_events(practice_id,account_sid,sender,recipient,channel);

CREATE FUNCTION cosmaneura_validate_communication_dispatch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
    IF TG_TABLE_NAME='chase_communication_authorizations' THEN
        IF NOT EXISTS (SELECT 1 FROM public.chase_communication_operations o
            JOIN public.chase_communication_policy_decisions p ON p.practice_id=o.practice_id AND p.decision_id=NEW.policy_decision_id
            JOIN public.chase_communication_consent_decisions c ON c.practice_id=o.practice_id AND c.decision_id=NEW.consent_decision_id
            WHERE o.practice_id=NEW.practice_id AND o.operation_id=NEW.operation_id
              AND o.binding_id=NEW.binding_id AND c.binding_id=o.binding_id AND c.granted AND p.approved) THEN
            RAISE EXCEPTION 'communications approval evidence mismatch' USING ERRCODE='check_violation';
        END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM public.chase_communication_operations o
        JOIN public.chase_communication_bindings b ON b.practice_id=o.practice_id AND b.binding_id=o.binding_id
        WHERE o.practice_id=NEW.practice_id AND o.operation_id=NEW.operation_id AND b.account_sid=NEW.account_sid
          AND (NEW.provider_sid IS NULL OR (o.channel='sms' AND NEW.provider_sid ~ '^SM')
               OR (o.channel='automated_voice' AND NEW.provider_sid ~ '^CA'))) THEN
        RAISE EXCEPTION 'communications receipt evidence mismatch' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION cosmaneura_validate_communication_dispatch() FROM PUBLIC;
CREATE TRIGGER authorization_evidence BEFORE INSERT ON chase_communication_authorizations
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_communication_dispatch();
CREATE TRIGGER receipt_evidence BEFORE INSERT ON chase_communication_dispatch_receipts
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_communication_dispatch();
ALTER TABLE chase_communication_authorizations ENABLE ALWAYS TRIGGER authorization_evidence;
ALTER TABLE chase_communication_dispatch_receipts ENABLE ALWAYS TRIGGER receipt_evidence;

DO $$ DECLARE relation text; BEGIN
    FOREACH relation IN ARRAY ARRAY['chase_communication_policy_decisions','chase_communication_consent_decisions',
        'chase_communication_authorizations','chase_communication_dispatch_starts',
        'chase_communication_dispatch_receipts','chase_communication_provider_events','chase_communication_dispatch_checks'] LOOP
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',relation);
        EXECUTE format('CREATE POLICY practice_isolation ON public.%I USING '
            '(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK '
            '(public.cosmaneura_practice_allowed(practice_id))',relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',relation);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',relation);
        EXECUTE format('CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON public.%I '
            'FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_reject_communication_mutation()',relation);
        EXECUTE format('CREATE TRIGGER immutable_table BEFORE TRUNCATE ON public.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_reject_communication_mutation()',relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER immutable_rows',relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER immutable_table',relation);
    END LOOP;
END $$;
COMMIT;
