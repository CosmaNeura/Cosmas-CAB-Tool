-- Additive current-header coder receipts; no clinical-note or claim write.
-- Depends on 003 feedback/audit and 011 headers; 012 transport is independent.
BEGIN;
CREATE TABLE public.billing_header_signoffs (
    practice_id text NOT NULL,
    review_id uuid NOT NULL,
    header_id uuid NOT NULL,
    header_revision bigint NOT NULL CHECK(header_revision BETWEEN 0 AND 100),
    source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[a-f0-9]{64}$'),
    recommendation_sha256 text NOT NULL CHECK(recommendation_sha256 ~ '^[a-f0-9]{64}$'),
    request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
    feedback_sha256 text NOT NULL CHECK(feedback_sha256 ~ '^[a-f0-9]{64}$'),
    receipt jsonb NOT NULL CHECK(jsonb_typeof(receipt)='object'
        AND receipt ?& ARRAY['reviewId','headerId','headerRevision','status']
        AND receipt->>'reviewId'=review_id::text AND receipt->>'headerId'=header_id::text
        AND receipt->>'headerRevision'=header_revision::text AND receipt->>'status'='recorded'),
    payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
    actor_tenant_id uuid NOT NULL,
    actor_user_id uuid NOT NULL,
    actor_role text NOT NULL CHECK(actor_role IN ('Coder','Admin')),
    created_at timestamptz NOT NULL,
    access_event_id uuid NOT NULL,
    PRIMARY KEY(practice_id,review_id),
    UNIQUE(practice_id,header_id,header_revision),
    UNIQUE(practice_id,access_event_id),
    FOREIGN KEY(practice_id,header_id) REFERENCES public.billing_headers(practice_id,header_id),
    FOREIGN KEY(practice_id,review_id) REFERENCES public.coder_feedback(practice_id,review_id),
    FOREIGN KEY(practice_id,actor_tenant_id,actor_user_id)
        REFERENCES public.practice_memberships(practice_id,tenant_id,user_id),
    FOREIGN KEY(practice_id,access_event_id)
        REFERENCES public.access_audit_completions(practice_id,event_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE FUNCTION public.cosmaneura_guard_header_signoff()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE source_version bigint; source_digest text; current_version bigint; current_revision bigint; uid uuid;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'header signoffs are append only'; END IF;
    IF NOT public.cosmaneura_billing_header_editor_allowed(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role)
       OR NOT public.cosmaneura_feedback_reviewer_allowed(NEW.actor_tenant_id,NEW.actor_user_id) THEN
        RAISE EXCEPTION 'header reviewer unavailable';
    END IF;
    SELECT encounter_uid,encounter_version,source_sha256 INTO uid,source_version,source_digest
      FROM public.billing_headers WHERE practice_id=NEW.practice_id AND header_id=NEW.header_id;
    SELECT version INTO current_version FROM public.encounters
      WHERE practice_id=NEW.practice_id AND encounter_uid=uid FOR UPDATE;
    SELECT COALESCE(MAX(revision),0) INTO current_revision FROM public.billing_header_revisions
      WHERE practice_id=NEW.practice_id AND header_id=NEW.header_id;
    IF current_version IS NULL OR current_version<>source_version
       OR source_digest<>NEW.source_sha256 OR current_revision<>NEW.header_revision THEN
        RAISE EXCEPTION 'header review source or revision changed';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_guard_header_signoff() FROM PUBLIC;
CREATE TRIGGER header_signoffs_guard BEFORE INSERT OR UPDATE OR DELETE ON public.billing_header_signoffs
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_header_signoff();
CREATE TRIGGER header_signoffs_truncate_guard BEFORE TRUNCATE ON public.billing_header_signoffs
 FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_guard_header_signoff();
ALTER TABLE public.billing_header_signoffs ENABLE ALWAYS TRIGGER header_signoffs_guard;
ALTER TABLE public.billing_header_signoffs ENABLE ALWAYS TRIGGER header_signoffs_truncate_guard;

-- A receipt cannot promote old feedback or reuse an old successful access log.
-- Both rows must be inserted in this same top-level transaction. Savepoint-only
-- completion inserts fail closed; application stores do not use that pattern.
CREATE FUNCTION public.cosmaneura_require_header_signoff_audit()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.coder_feedback f
        JOIN public.billing_headers h ON h.practice_id=f.practice_id AND h.encounter_uid=f.encounter_uid
        JOIN public.access_audit_completions c ON c.practice_id=f.practice_id AND c.event_id=NEW.access_event_id
        JOIN public.access_audit_attempts a ON a.practice_id=c.practice_id AND a.event_id=c.event_id
        WHERE f.practice_id=NEW.practice_id AND f.review_id=NEW.review_id AND h.header_id=NEW.header_id
          AND f.payload_hash=NEW.feedback_sha256 AND f.encounter_version=h.encounter_version
          AND f.coder_tenant_id=NEW.actor_tenant_id AND f.coder_user_id=NEW.actor_user_id
          AND a.actor_tenant_id=NEW.actor_tenant_id AND a.actor_user_id=NEW.actor_user_id
          AND NEW.actor_role=ANY(a.actor_roles) AND a.action='billing_header.signoff' AND a.method='POST'
          AND c.resource_id=h.encounter_uid::text AND c.status BETWEEN 200 AND 299 AND c.outcome='allowed'
          AND c.xmin=pg_current_xact_id()::xid AND f.xmin=pg_current_xact_id()::xid
    ) THEN RAISE EXCEPTION 'atomic header review and audit are required'; END IF;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_require_header_signoff_audit() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER header_signoffs_audit AFTER INSERT ON public.billing_header_signoffs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_header_signoff_audit();

ALTER TABLE public.billing_header_signoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_header_signoffs FORCE ROW LEVEL SECURITY;
CREATE POLICY header_signoffs_scope ON public.billing_header_signoffs
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.billing_header_signoffs FROM PUBLIC;
COMMIT;
