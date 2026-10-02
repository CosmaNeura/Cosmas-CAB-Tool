-- Dormant reviewed Athena evidence. No read/transport/source/consent activation.
BEGIN;
ALTER TABLE public.encounters DROP CONSTRAINT encounters_source_valid;
ALTER TABLE public.encounters ADD CONSTRAINT encounters_source_valid CHECK(source IN (
 'synthetic','deidentified','manual','athena','explicit_import','synthetic_demo_release_candidate',
 'athena_preview_synthetic','athena_staff_review_preview_synthetic','athena_staff_review_mock'));
CREATE FUNCTION public.cosmaneura_athena_reviewer_allowed(candidate_tenant uuid,candidate_user uuid,candidate_role text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE allowed boolean:=false;
BEGIN
 IF candidate_role NOT IN ('BillingStaff','Admin') THEN RETURN false; END IF;
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
REVOKE ALL ON FUNCTION public.cosmaneura_athena_reviewer_allowed(uuid,uuid,text) FROM PUBLIC;
CREATE TABLE public.athena_case_review_events (
 practice_id text NOT NULL, chase_id text NOT NULL,
 revision integer NOT NULL CHECK(revision BETWEEN 1 AND 10000), operation_id uuid NOT NULL,
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object' AND payload->>'schemaVersion'='athena-case-review.v1'
   AND octet_length(payload::text)<=1048576 AND (payload->>'revision')::integer=revision),
 actor_tenant_id uuid NOT NULL, actor_user_id uuid NOT NULL,
 actor_role text NOT NULL CHECK(actor_role IN ('BillingStaff','Admin')),
 access_event_id uuid NOT NULL, action text NOT NULL CHECK(action IN ('athena.refresh','athena.apply','athena.billing_apply')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(practice_id,chase_id,revision), UNIQUE(practice_id,operation_id), UNIQUE(practice_id,access_event_id),
 FOREIGN KEY(practice_id,chase_id) REFERENCES public.chase_items(practice_id,chase_id),
 FOREIGN KEY(practice_id,actor_tenant_id,actor_user_id) REFERENCES public.practice_memberships(practice_id,tenant_id,user_id),
 FOREIGN KEY(practice_id,access_event_id) REFERENCES public.access_audit_completions(practice_id,event_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE FUNCTION public.cosmaneura_guard_athena_review() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Athena review evidence is immutable'; END IF;
 IF NOT public.cosmaneura_athena_reviewer_allowed(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role)
 THEN RAISE EXCEPTION 'Athena reviewer is not authorized'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.cosmaneura_require_athena_review_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM public.access_audit_completions c
 JOIN public.access_audit_attempts a ON a.practice_id=c.practice_id AND a.event_id=c.event_id
 JOIN public.chase_items i ON i.practice_id=NEW.practice_id AND i.chase_id=NEW.chase_id
 WHERE c.practice_id=NEW.practice_id AND c.event_id=NEW.access_event_id
 AND a.actor_tenant_id=NEW.actor_tenant_id AND a.actor_user_id=NEW.actor_user_id
 AND NEW.actor_role=ANY(a.actor_roles) AND a.action=NEW.action AND a.method='POST'
 AND c.resource_id=i.chase_uid::text AND c.outcome='allowed' AND c.status BETWEEN 200 AND 299
 AND c.xmin=pg_current_xact_id()::xid)
 THEN RAISE EXCEPTION 'Athena review audit is required'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_guard_athena_review() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cosmaneura_require_athena_review_audit() FROM PUBLIC;
CREATE TRIGGER athena_review_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.athena_case_review_events
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_athena_review();
CREATE CONSTRAINT TRIGGER athena_review_audit AFTER INSERT ON public.athena_case_review_events
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_athena_review_audit();
ALTER TABLE public.athena_case_review_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.athena_case_review_events FORCE ROW LEVEL SECURITY;
CREATE POLICY athena_review_scope ON public.athena_case_review_events
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.athena_case_review_events FROM PUBLIC;
-- Source classification is reserved to a reviewed, same-transaction import.
-- The receipt proves the server-chosen lane/binding; it is not an Athena-signed
-- attestation or permission to activate the Preview client.
CREATE FUNCTION public.cosmaneura_guard_athena_encounter() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='INSERT' THEN RETURN NEW; END IF;
 IF OLD.source IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock')
 OR (TG_OP='UPDATE' AND NEW.source IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock'))
 THEN RAISE EXCEPTION 'reviewed Athena encounters are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.cosmaneura_require_athena_encounter_receipt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NEW.source NOT IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock') THEN RETURN NULL; END IF;
 IF NOT EXISTS (SELECT 1 FROM public.athena_case_review_events r
 WHERE r.practice_id=NEW.practice_id AND r.action='athena.billing_apply'
 AND r.xmin=pg_current_xact_id()::xid
 AND r.actor_tenant_id=NEW.ingested_by_tenant_id AND r.actor_user_id=NEW.ingested_by_user_id
 AND r.payload->'billingImport'->>'encounterId'=NEW.external_id
 AND r.payload->'billingImport'->>'source'=NEW.source
 AND r.payload->'candidate'->'source'->>'system'='athena_preview'
 AND r.payload->'candidate'->'source'->>'vendorPracticeId'='195900'
 AND r.payload->'candidate'->'source'->>'serverPracticeId'=NEW.practice_id
 AND r.payload->'candidate'->'source'->>'syntheticOnly'='true'
 AND r.payload->'candidate'->'source'->>'patientId' IN ('60178','60179','60180','60181','60182','60183','60184')
 AND r.payload->'billingImport'->>'sourcePatientId'=r.payload->'candidate'->'source'->>'patientId'
 AND r.payload->'billingImport'->>'contextOrigin'='staff_confirmed'
 AND r.payload->'billingImport'->>'plainTextSha256'=encode(sha256(convert_to(NEW.signed_note,'UTF8')),'hex')
 AND r.payload->'billingImport'->'clinicalContext'->>'patientAge'=NEW.patient_age::text
 AND r.payload->'billingImport'->'clinicalContext'->>'patientSex'=NEW.patient_sex
 AND r.payload->'billingImport'->'clinicalContext'->>'encounterType'=NEW.encounter_type
 AND r.payload->'billingImport'->'clinicalContext'->>'placeOfService'=NEW.place_of_service
 AND r.payload->'billingImport'->'clinicalContext'->>'providerSpecialty'=NEW.provider_specialty
 AND NEW.intake_text='' AND NEW.transcript='' AND NEW.submitted_em_code IS NULL
 AND cardinality(NEW.submitted_icd10)=0
 AND ((NEW.source='athena_staff_review_preview_synthetic' AND r.payload->>'mode'='preview')
   OR (NEW.source='athena_staff_review_mock' AND r.payload->>'mode'='mock')))
 THEN RAISE EXCEPTION 'reviewed Athena source requires its import receipt'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_guard_athena_encounter() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cosmaneura_require_athena_encounter_receipt() FROM PUBLIC;
CREATE TRIGGER athena_encounter_immutable BEFORE INSERT OR UPDATE OR DELETE ON public.encounters
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_athena_encounter();
CREATE CONSTRAINT TRIGGER athena_encounter_receipt AFTER INSERT ON public.encounters
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_athena_encounter_receipt();
COMMIT;
