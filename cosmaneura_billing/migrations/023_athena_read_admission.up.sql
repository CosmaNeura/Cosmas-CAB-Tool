-- Empty operator-owned read/storage admission; no model/outreach authority.
BEGIN;
CREATE TABLE public.athena_staff_read_admissions (
 practice_id text NOT NULL, case_id text NOT NULL, admission_id uuid NOT NULL,
 admission_revision integer NOT NULL CHECK(admission_revision>0), document jsonb NOT NULL,
 database_name name NOT NULL DEFAULT current_database(), grant_sha256 text NOT NULL DEFAULT '',
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(), revoked_at timestamptz, revocation_ref text,
 PRIMARY KEY(practice_id,case_id,admission_revision), UNIQUE(practice_id,admission_id),
 FOREIGN KEY(practice_id,case_id) REFERENCES public.chase_items(practice_id,chase_id),
 CHECK(jsonb_typeof(document)='object' AND octet_length(document::text)<=4096),
 CHECK(grant_sha256 ~ '^[a-f0-9]{64}$')
);
ALTER TABLE public.athena_staff_read_admissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.athena_staff_read_admissions FORCE ROW LEVEL SECURITY;
CREATE POLICY athena_read_admission_scope ON public.athena_staff_read_admissions
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.athena_staff_read_admissions FROM PUBLIC;
CREATE FUNCTION public.cosmaneura_athena_admission_registration_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE previous integer; d jsonb; b public.athena_staff_case_bindings%ROWTYPE;
 c public.athena_staff_connections%ROWTYPE; patient text;
BEGIN
 IF current_user IS DISTINCT FROM pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid=TG_RELID))
 THEN RAISE EXCEPTION 'Athena admission requires migration owner'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'Athena admission history is immutable'; END IF;
 d:=NEW.document;
 IF NEW.practice_id IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
 OR NOT public.cosmaneura_practice_allowed(NEW.practice_id) THEN RAISE EXCEPTION 'Athena admission practice unavailable'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('athena-staff-binding:'||NEW.practice_id,0));
 IF TG_OP='UPDATE' THEN
  IF OLD.revoked_at IS NOT NULL OR OLD.revocation_ref IS NOT NULL OR NEW.revocation_ref IS NULL
  OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at OR (to_jsonb(OLD)-'revocation_ref') IS DISTINCT FROM (to_jsonb(NEW)-'revocation_ref')
  THEN RAISE EXCEPTION 'Athena admission is immutable except first revocation'; END IF;
  NEW.revoked_at:=clock_timestamp(); RETURN NEW;
 END IF;
 IF NEW.revoked_at IS NOT NULL OR NEW.revocation_ref IS NOT NULL OR jsonb_typeof(d) IS DISTINCT FROM 'object'
 OR (SELECT count(*) FROM jsonb_object_keys(d))<>13
 OR NOT d ?& ARRAY['schemaVersion','serverPracticeId','tenantId','reviewRef','admissionId','admissionRevision','caseId','scopeSha256','runtimeRole','deploymentId','storageId','notBefore','expiresAt']
 OR d->>'schemaVersion' IS DISTINCT FROM 'athena-staff-registration.v1'
 OR d->>'serverPracticeId' IS DISTINCT FROM NEW.practice_id OR d->>'caseId' IS DISTINCT FROM NEW.case_id
 OR d->>'admissionId' IS DISTINCT FROM NEW.admission_id::text OR jsonb_typeof(d->'admissionRevision') IS DISTINCT FROM 'number'
 OR d->>'admissionRevision' IS DISTINCT FROM NEW.admission_revision::text
 OR COALESCE(d->>'deploymentId','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
 OR COALESCE(d->>'storageId','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
 OR length(COALESCE(d->>'reviewRef','')) NOT BETWEEN 1 AND 128
 OR NOT COALESCE((d->>'notBefore')::timestamptz<(d->>'expiresAt')::timestamptz,false)
 THEN RAISE EXCEPTION 'Athena admission registration invalid'; END IF;
 SELECT * INTO c FROM public.athena_staff_connections WHERE practice_id=NEW.practice_id ORDER BY connection_revision DESC LIMIT 1 FOR SHARE;
 SELECT * INTO b FROM public.athena_staff_case_bindings WHERE practice_id=NEW.practice_id AND case_id=NEW.case_id ORDER BY binding_revision DESC LIMIT 1 FOR SHARE;
 SELECT patient_ref INTO patient FROM public.chase_items WHERE practice_id=NEW.practice_id AND chase_id=NEW.case_id FOR SHARE;
 IF c.environment IS DISTINCT FROM 'production' OR c.revoked_at IS NOT NULL OR b.revoked_at IS NOT NULL
 OR b.connection_revision IS DISTINCT FROM c.connection_revision OR b.patient_ref IS DISTINCT FROM patient
 OR b.scope_sha256 IS DISTINCT FROM d->>'scopeSha256' OR c.tenant_id::text IS DISTINCT FROM d->>'tenantId'
 OR NOT EXISTS(SELECT 1 FROM public.runtime_practice_bindings WHERE role_name=d->>'runtimeRole' AND practice_id=NEW.practice_id)
 THEN RAISE EXCEPTION 'Athena admission binding unavailable'; END IF;
 SELECT max(admission_revision) INTO previous FROM public.athena_staff_read_admissions WHERE practice_id=NEW.practice_id AND case_id=NEW.case_id;
 IF NEW.admission_revision::bigint<>COALESCE(previous::bigint,0)+1 THEN RAISE EXCEPTION 'Athena admission revision conflict'; END IF;
 NEW.database_name:=current_database(); NEW.registered_at:=clock_timestamp();
 NEW.grant_sha256:=encode(sha256(convert_to(public.cosmaneura_handoff_canonical(jsonb_build_array(d,NEW.database_name)),'UTF8')),'hex');
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_athena_admission_registration_guard() FROM PUBLIC;
CREATE TRIGGER athena_admission_registration_guard BEFORE INSERT OR UPDATE OR DELETE ON public.athena_staff_read_admissions
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_athena_admission_registration_guard();
CREATE TRIGGER athena_admission_no_truncate BEFORE TRUNCATE ON public.athena_staff_read_admissions
 FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_athena_admission_registration_guard();
ALTER TABLE public.athena_staff_read_admissions ENABLE ALWAYS TRIGGER athena_admission_registration_guard;
ALTER TABLE public.athena_staff_read_admissions ENABLE ALWAYS TRIGGER athena_admission_no_truncate;
CREATE FUNCTION public.cosmaneura_athena_read_admission(t uuid,u uuid,r text,k text,deployment text,storage text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE s jsonb; g public.athena_staff_read_admissions%ROWTYPE; p text:=NULLIF(current_setting('cosmaneura.practice_id',true),'');
BEGIN
 s:=public.cosmaneura_athena_staff_scope(t,u,r,k);
 IF s ? 'error' OR s->>'environment' IS DISTINCT FROM 'production' THEN RETURN jsonb_build_object('error','read_not_admitted'); END IF;
 SELECT * INTO g FROM public.athena_staff_read_admissions WHERE practice_id=p AND case_id=k ORDER BY admission_revision DESC LIMIT 1 FOR SHARE;
 IF g.admission_id IS NULL OR g.revoked_at IS NOT NULL OR g.database_name IS DISTINCT FROM current_database()
 OR g.document->>'runtimeRole' IS DISTINCT FROM session_user::text OR g.document->>'scopeSha256' IS DISTINCT FROM s->>'scopeSha256'
 OR g.document->>'deploymentId' IS DISTINCT FROM deployment OR g.document->>'storageId' IS DISTINCT FROM storage
 OR NOT COALESCE(clock_timestamp()>=(g.document->>'notBefore')::timestamptz AND clock_timestamp()<(g.document->>'expiresAt')::timestamptz,false)
 THEN RETURN jsonb_build_object('error','read_not_admitted'); END IF;
 RETURN jsonb_build_object('schemaVersion','athena-read-storage-admission.v1','admissionId',g.admission_id::text,
  'admissionRevision',g.admission_revision,'scopeSha256',g.document->'scopeSha256','grantSha256',g.grant_sha256,'expiresAt',g.document->'expiresAt');
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_athena_read_admission(uuid,uuid,text,text,text,text) FROM PUBLIC;
ALTER TABLE public.athena_case_review_events DROP CONSTRAINT athena_case_review_events_check;
ALTER TABLE public.athena_case_review_events ADD CONSTRAINT athena_case_review_events_check CHECK(
 jsonb_typeof(payload)='object' AND payload->>'schemaVersion' IN ('athena-case-review.v1','athena-case-review.v2','athena-case-review.v3')
 AND octet_length(payload::text)<=1048576 AND (payload->>'revision')::integer=revision);
CREATE OR REPLACE FUNCTION public.cosmaneura_guard_configured_athena_review() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE scope jsonb; binding jsonb; prior jsonb; candidate jsonb; expected_case text; history jsonb; production boolean; admission jsonb; g public.athena_staff_read_admissions%ROWTYPE;
BEGIN
 IF current_setting('transaction_isolation')<>'read committed'
 THEN RAISE EXCEPTION 'Athena review requires current transaction scope'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('athena-review-case:'||NEW.practice_id||':'||NEW.chase_id,0));
 SELECT payload INTO prior FROM public.athena_case_review_events
 WHERE practice_id=NEW.practice_id AND chase_id=NEW.chase_id ORDER BY revision DESC LIMIT 1;
 IF prior->>'schemaVersion'='athena-case-review.v3' AND NEW.payload->>'schemaVersion' IS DISTINCT FROM 'athena-case-review.v3'
 THEN RAISE EXCEPTION 'Athena operational review cannot downgrade'; END IF;
 IF NEW.payload->>'schemaVersion'='athena-case-review.v1' THEN
  IF prior->>'schemaVersion' IN ('athena-case-review.v2','athena-case-review.v3')
  THEN RAISE EXCEPTION 'Athena configured review cannot downgrade'; END IF;
  RETURN NEW;
 END IF;
 IF COALESCE(NEW.payload->>'schemaVersion','') NOT IN ('athena-case-review.v2','athena-case-review.v3')
 OR NEW.practice_id IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
 OR jsonb_typeof(NEW.payload->'revision') IS DISTINCT FROM 'number'
 OR NEW.payload->>'revision' IS DISTINCT FROM NEW.revision::text
 THEN RAISE EXCEPTION 'Athena configured review scope invalid'; END IF;
 scope:=public.cosmaneura_athena_staff_scope(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role,NEW.chase_id);
 production:=NEW.payload->>'schemaVersion'='athena-case-review.v3';
 IF jsonb_typeof(scope) IS DISTINCT FROM 'object' OR scope ? 'error'
 OR scope->>'environment' IS DISTINCT FROM (CASE WHEN production THEN 'production' ELSE 'preview' END)
 OR (NOT production AND (scope->>'vendorPracticeId' IS DISTINCT FROM '195900'
  OR COALESCE(scope->>'sourcePatientId','') NOT IN ('60178','60179','60180','60181','60182','60183','60184')))
 THEN RAISE EXCEPTION 'Athena configured read is not admitted'; END IF;
 IF production THEN
  SELECT * INTO g FROM public.athena_staff_read_admissions WHERE practice_id=NEW.practice_id AND case_id=NEW.chase_id ORDER BY admission_revision DESC LIMIT 1;
  admission:=public.cosmaneura_athena_read_admission(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role,NEW.chase_id,g.document->>'deploymentId',g.document->>'storageId');
  IF admission ? 'error' OR NEW.payload->'sourceAdmission' IS DISTINCT FROM admission
  OR NEW.payload->'candidate'->'sourceAdmission' IS DISTINCT FROM admission
  THEN RAISE EXCEPTION 'Athena source admission mismatch'; END IF;
 END IF;
 binding:=jsonb_build_object('schemaVersion','athena-source-binding.v1',
  'serverPracticeId',scope->'serverPracticeId','chaseId',scope->'caseId','environment',scope->'environment',
  'vendorPracticeId',scope->'vendorPracticeId','sourcePatientId',scope->'sourcePatientId',
  'connectionRevision',scope->'connectionRevision','bindingRevision',scope->'bindingRevision',
  'profileSha256',scope->'profileSha256','scopeSha256',scope->'scopeSha256');
 -- Legacy caseBinding uses UTF-8 JSON, unlike the registry's ASCII canonical
 -- fingerprints. Serialize these exact two strings without array whitespace.
 expected_case:=encode(sha256(convert_to('['||to_json(NEW.practice_id)::text||','||
  to_json(scope->>'patientRef')::text||']','UTF8')),'hex');
 candidate:=NEW.payload->'candidate';
 IF NEW.payload->'sourceBinding' IS DISTINCT FROM binding OR candidate->'sourceBinding' IS DISTINCT FROM binding
 OR NEW.payload->>'caseBinding' IS DISTINCT FROM expected_case OR NEW.payload->>'chaseId' IS DISTINCT FROM NEW.chase_id
 OR NEW.payload->>'mode' IS DISTINCT FROM scope->>'environment'
 OR candidate->'source'->>'system' IS DISTINCT FROM (CASE WHEN production THEN 'athena_production' ELSE 'athena_preview' END)
 OR candidate->'source'->'vendorPracticeId' IS DISTINCT FROM scope->'vendorPracticeId'
 OR candidate->'source'->>'serverPracticeId' IS DISTINCT FROM NEW.practice_id
 OR candidate->'source'->'syntheticOnly' IS DISTINCT FROM to_jsonb(NOT production)
 OR (production AND candidate->'source'->>'dataClass' IS DISTINCT FROM 'operational')
 OR candidate->'source'->'patientId' IS DISTINCT FROM binding->'sourcePatientId'
 OR NEW.revision IS DISTINCT FROM COALESCE((prior->>'revision')::integer,0)+1
 THEN RAISE EXCEPTION 'Athena configured evidence binding mismatch'; END IF;
 IF NEW.action='athena.refresh' THEN
  history:=CASE WHEN prior->>'caseBinding' IS DISTINCT FROM expected_case THEN NULL ELSE prior END;
  IF NEW.payload->>'reviewId' IS DISTINCT FROM NEW.operation_id::text
  OR NEW.payload->'applied' IS DISTINCT FROM COALESCE(history->'applied','null'::jsonb)
  OR NEW.payload->'billingImport' IS DISTINCT FROM COALESCE(history->'billingImport','null'::jsonb)
  THEN RAISE EXCEPTION 'Athena refresh changed historical evidence'; END IF;
 ELSE
  IF prior->>'schemaVersion' IS DISTINCT FROM NEW.payload->>'schemaVersion'
  OR (production AND prior->'sourceAdmission' IS DISTINCT FROM admission)
  OR prior->'sourceBinding' IS DISTINCT FROM binding OR NEW.payload->'candidate' IS DISTINCT FROM prior->'candidate'
  OR NEW.payload->'reviewId' IS DISTINCT FROM prior->'reviewId'
  THEN RAISE EXCEPTION 'Athena configured decision requires current review'; END IF;
  IF NEW.action='athena.apply' THEN
   IF (production AND NEW.payload->'applied'->'sourceAdmission' IS DISTINCT FROM admission)
   OR NEW.payload->'applied'->'sourceBinding' IS DISTINCT FROM binding
   OR NEW.payload->'applied'->'patient' IS DISTINCT FROM candidate->'patient'
   OR NEW.payload->'applied'->'sourceDigest' IS DISTINCT FROM candidate->'sourceDigest'
   OR NEW.payload->'applied'->'reviewId' IS DISTINCT FROM NEW.payload->'reviewId'
   OR NEW.payload->'applied'->>'operationId' IS DISTINCT FROM NEW.operation_id::text
   OR NEW.payload->'billingImport' IS DISTINCT FROM prior->'billingImport'
   THEN RAISE EXCEPTION 'Athena applied evidence binding mismatch'; END IF;
  ELSIF NEW.action='athena.billing_apply' THEN
   IF (production AND NEW.payload->'billingImport'->'sourceAdmission' IS DISTINCT FROM admission)
   OR NEW.payload->'applied' IS DISTINCT FROM prior->'applied'
   OR NEW.payload->'billingImport'->'sourceBinding' IS DISTINCT FROM binding
   OR NEW.payload->'billingImport'->'sourcePatientId' IS DISTINCT FROM binding->'sourcePatientId'
   OR NEW.payload->'billingImport'->>'source' IS DISTINCT FROM (CASE WHEN production THEN 'athena_staff_review_operational' ELSE 'athena_staff_review_preview_synthetic' END)
   OR NEW.payload->'billingImport'->>'encounterId' IS DISTINCT FROM 'athena-review-'||NEW.operation_id::text
   THEN RAISE EXCEPTION 'Athena imported evidence binding mismatch'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.cosmaneura_guard_athena_encounter() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='INSERT' THEN RETURN NEW; END IF;
 IF OLD.source IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock','athena_staff_review_operational')
 OR (TG_OP='UPDATE' AND NEW.source IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock','athena_staff_review_operational'))
 THEN RAISE EXCEPTION 'reviewed Athena encounters are immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE OR REPLACE FUNCTION public.cosmaneura_require_athena_encounter_receipt() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NEW.source NOT IN ('athena_staff_review_preview_synthetic','athena_staff_review_mock','athena_staff_review_operational') THEN RETURN NULL; END IF;
 IF NOT EXISTS (SELECT 1 FROM public.athena_case_review_events r
 WHERE r.practice_id=NEW.practice_id AND r.action='athena.billing_apply'
 AND r.xmin=pg_current_xact_id()::xid
 AND r.actor_tenant_id=NEW.ingested_by_tenant_id AND r.actor_user_id=NEW.ingested_by_user_id
 AND r.payload->'billingImport'->>'encounterId'=NEW.external_id
 AND r.payload->'billingImport'->>'source'=NEW.source
 AND ((NEW.source='athena_staff_review_operational' AND r.payload->>'schemaVersion'='athena-case-review.v3'
  AND r.payload->'candidate'->'source'->>'system'='athena_production'
  AND r.payload->'candidate'->'source'->'syntheticOnly'='false'::jsonb
  AND r.payload->'candidate'->'source'->>'dataClass'='operational') OR
 (NEW.source<>'athena_staff_review_operational' AND r.payload->'candidate'->'source'->>'system'='athena_preview'
  AND r.payload->'candidate'->'source'->>'vendorPracticeId'='195900'
  AND r.payload->'candidate'->'source'->'syntheticOnly'='true'::jsonb
  AND r.payload->'candidate'->'source'->>'patientId' IN ('60178','60179','60180','60181','60182','60183','60184')))
 AND r.payload->'candidate'->'source'->>'serverPracticeId'=NEW.practice_id
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
   OR (NEW.source='athena_staff_review_mock' AND r.payload->>'mode'='mock')
   OR (NEW.source='athena_staff_review_operational' AND r.payload->>'mode'='production')))
 THEN RAISE EXCEPTION 'reviewed Athena source requires its import receipt'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION public.cosmaneura_require_active_athena_admission() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE g public.athena_staff_read_admissions%ROWTYPE; a jsonb;
BEGIN
 IF NEW.payload->>'schemaVersion'<>'athena-case-review.v3' THEN RETURN NULL; END IF;
 SELECT * INTO g FROM public.athena_staff_read_admissions WHERE practice_id=NEW.practice_id AND case_id=NEW.chase_id ORDER BY admission_revision DESC LIMIT 1;
 a:=public.cosmaneura_athena_read_admission(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role,NEW.chase_id,g.document->>'deploymentId',g.document->>'storageId');
 IF a ? 'error' OR a IS DISTINCT FROM NEW.payload->'sourceAdmission' THEN RAISE EXCEPTION 'Athena admission expired before persistence'; END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_require_active_athena_admission() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER athena_review_active_admission AFTER INSERT ON public.athena_case_review_events
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_require_active_athena_admission();
ALTER TABLE public.athena_case_review_events ENABLE ALWAYS TRIGGER athena_review_active_admission;
ALTER TABLE public.encounters DROP CONSTRAINT encounters_source_valid;
ALTER TABLE public.encounters ADD CONSTRAINT encounters_source_valid CHECK(source IN (
 'synthetic','deidentified','manual','athena','explicit_import','synthetic_demo_release_candidate',
 'athena_preview_synthetic','athena_staff_review_preview_synthetic','athena_staff_review_mock',
 'provider_completed_note','athena_staff_review_operational'));
COMMIT;
