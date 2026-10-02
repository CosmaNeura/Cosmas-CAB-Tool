-- Additive modern billing headers. No note/feedback rewrite or feature activation.
-- This slice does not depend on 010 communications. Runtime grants are
-- provisioned separately by the locked runner.
BEGIN;

CREATE FUNCTION public.cosmaneura_billing_header_editor_allowed(
    candidate_tenant uuid, candidate_user uuid, candidate_role text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE allowed boolean := false;
BEGIN
    IF candidate_role NOT IN ('Provider', 'Coder', 'Admin') THEN RETURN false; END IF;
    SELECT true INTO allowed FROM public.practice_memberships m
      JOIN public.practices p ON p.practice_id=m.practice_id
      JOIN public.users u ON u.tenant_id=m.tenant_id AND u.user_id=m.user_id
     WHERE m.practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
       AND public.cosmaneura_practice_allowed(m.practice_id)
       AND m.tenant_id=candidate_tenant AND m.user_id=candidate_user
       AND m.active AND p.active AND u.active AND candidate_role=ANY(m.roles)
     FOR SHARE OF m,p,u;
    RETURN COALESCE(allowed,false);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_billing_header_editor_allowed(uuid,uuid,text) FROM PUBLIC;

CREATE TABLE public.billing_headers (
    practice_id text NOT NULL,
    header_id uuid NOT NULL,
    encounter_uid uuid NOT NULL,
    encounter_version bigint NOT NULL CHECK(encounter_version>0),
    source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[a-f0-9]{64}$'),
    baseline jsonb NOT NULL CHECK(jsonb_typeof(baseline)='object'
        AND baseline->>'schemaVersion'='provider-billing-header.v1'
        AND baseline->>'reviewStatus'='unreviewed'),
    baseline_sha256 text NOT NULL CHECK(baseline_sha256 ~ '^[a-f0-9]{64}$'),
    actor_tenant_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    actor_role text NOT NULL CHECK(actor_role IN ('Provider','Coder','Admin')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY(practice_id,header_id),
    UNIQUE(practice_id,encounter_uid,source_sha256),
    FOREIGN KEY(practice_id,encounter_uid) REFERENCES public.encounters(practice_id,encounter_uid),
    FOREIGN KEY(practice_id,actor_tenant_id,actor_user_id)
        REFERENCES public.practice_memberships(practice_id,tenant_id,user_id)
);
CREATE TABLE public.billing_header_revisions (
    practice_id text NOT NULL,
    header_id uuid NOT NULL,
    revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 100),
    revision_payload jsonb NOT NULL CHECK(jsonb_typeof(revision_payload)='object'
        AND revision_payload->>'reviewStatus'='unreviewed'),
    payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
    actor_tenant_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    actor_role text NOT NULL CHECK(actor_role IN ('Provider','Coder','Admin')),
    created_at timestamptz NOT NULL,
    PRIMARY KEY(practice_id,header_id,revision),
    FOREIGN KEY(practice_id,header_id) REFERENCES public.billing_headers(practice_id,header_id),
    FOREIGN KEY(practice_id,actor_tenant_id,actor_user_id)
        REFERENCES public.practice_memberships(practice_id,tenant_id,user_id)
);
CREATE TABLE public.billing_header_operations (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
    header_id uuid NOT NULL,
    revision bigint NOT NULL CHECK(revision BETWEEN 0 AND 100),
    actor_tenant_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    actor_role text NOT NULL CHECK(actor_role IN ('Provider','Coder','Admin')),
    access_event_id uuid NOT NULL,
    PRIMARY KEY(practice_id,operation_id),
    UNIQUE(practice_id,access_event_id),
    FOREIGN KEY(practice_id,header_id) REFERENCES public.billing_headers(practice_id,header_id),
    FOREIGN KEY(practice_id,actor_tenant_id,actor_user_id)
        REFERENCES public.practice_memberships(practice_id,tenant_id,user_id),
    FOREIGN KEY(practice_id,access_event_id)
        REFERENCES public.access_audit_completions(practice_id,event_id)
        DEFERRABLE INITIALLY DEFERRED
);

-- The same transaction serializes current source version and membership. Direct
-- SQL cannot bypass append-only history or write a stale encounter revision.
CREATE FUNCTION public.cosmaneura_guard_billing_header_write()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE target_uid uuid; target_version bigint; current_version bigint; prior bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'billing header records are append only'; END IF;
    IF NOT public.cosmaneura_billing_header_editor_allowed(
        NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role
    ) THEN RAISE EXCEPTION 'billing header editor unavailable'; END IF;
    IF TG_TABLE_NAME='billing_headers' THEN
        target_uid:=NEW.encounter_uid; target_version:=NEW.encounter_version;
    ELSE
        SELECT encounter_uid,encounter_version INTO target_uid,target_version
          FROM public.billing_headers WHERE practice_id=NEW.practice_id AND header_id=NEW.header_id;
    END IF;
    SELECT version INTO current_version FROM public.encounters
     WHERE practice_id=NEW.practice_id AND encounter_uid=target_uid FOR UPDATE;
    IF current_version IS NULL OR current_version<>target_version THEN
        RAISE EXCEPTION 'billing header source changed';
    END IF;
    IF TG_TABLE_NAME IN ('billing_header_revisions','billing_header_operations') THEN
        SELECT COALESCE(MAX(revision),0) INTO prior FROM public.billing_header_revisions
         WHERE practice_id=NEW.practice_id AND header_id=NEW.header_id;
        IF TG_TABLE_NAME='billing_header_revisions' AND NEW.revision<>prior+1
           OR TG_TABLE_NAME='billing_header_operations' AND NEW.revision<>prior THEN
            RAISE EXCEPTION 'billing header revision conflict';
        END IF;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_guard_billing_header_write() FROM PUBLIC;

-- Each operation has its own success audit completion inserted in this same
-- transaction. Pre-existing completions cannot authorize a new write. Ordinary
-- application transactions do not use savepoints for the completion insert.
CREATE FUNCTION public.cosmaneura_require_billing_header_audit()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,public AS $$
DECLARE expected_revision bigint; expected_action text; target_operation uuid;
BEGIN
    IF TG_TABLE_NAME='billing_headers' THEN
        expected_revision:=0; expected_action:='billing_header.compute';
    ELSIF TG_TABLE_NAME='billing_header_operations' THEN
        expected_revision:=NEW.revision; target_operation:=NEW.operation_id;
    ELSE
        expected_revision:=NEW.revision; expected_action:='billing_header.save';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.billing_header_operations op
        JOIN public.access_audit_completions c
          ON c.practice_id=op.practice_id AND c.event_id=op.access_event_id
        JOIN public.access_audit_attempts a
          ON a.practice_id=c.practice_id AND a.event_id=c.event_id
        JOIN public.billing_headers h
          ON h.practice_id=op.practice_id AND h.header_id=op.header_id
        WHERE op.practice_id=NEW.practice_id AND op.header_id=NEW.header_id
          AND op.revision=expected_revision AND op.actor_tenant_id=NEW.actor_tenant_id
          AND (target_operation IS NULL OR op.operation_id=target_operation)
          AND op.actor_user_id=NEW.actor_user_id AND op.actor_role=NEW.actor_role
          AND a.actor_tenant_id=NEW.actor_tenant_id AND a.actor_user_id=NEW.actor_user_id
          AND NEW.actor_role=ANY(a.actor_roles) AND c.resource_id=h.encounter_uid::text
          AND (a.action=expected_action OR expected_action IS NULL
               AND a.action IN ('billing_header.compute','billing_header.save'))
          AND a.method='POST' AND c.xmin=pg_current_xact_id()::xid
          AND c.outcome='allowed' AND c.status BETWEEN 200 AND 299
    ) THEN RAISE EXCEPTION 'billing header audit is required'; END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_require_billing_header_audit() FROM PUBLIC;

CREATE TRIGGER billing_headers_guard BEFORE INSERT OR UPDATE OR DELETE ON public.billing_headers
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_billing_header_write();
CREATE TRIGGER billing_header_revisions_guard BEFORE INSERT OR UPDATE OR DELETE ON public.billing_header_revisions
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_billing_header_write();
CREATE TRIGGER billing_header_operations_guard BEFORE INSERT OR UPDATE OR DELETE ON public.billing_header_operations
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_billing_header_write();
CREATE CONSTRAINT TRIGGER billing_headers_audit AFTER INSERT ON public.billing_headers
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_billing_header_audit();
CREATE CONSTRAINT TRIGGER billing_header_revisions_audit AFTER INSERT ON public.billing_header_revisions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_billing_header_audit();
CREATE CONSTRAINT TRIGGER billing_header_operations_audit AFTER INSERT ON public.billing_header_operations
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_billing_header_audit();

ALTER TABLE public.billing_headers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_headers FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_headers_scope ON public.billing_headers
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
ALTER TABLE public.billing_header_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_header_revisions FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_header_revisions_scope ON public.billing_header_revisions
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
ALTER TABLE public.billing_header_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_header_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_header_operations_scope ON public.billing_header_operations
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.billing_headers,public.billing_header_revisions,public.billing_header_operations FROM PUBLIC;
COMMIT;
