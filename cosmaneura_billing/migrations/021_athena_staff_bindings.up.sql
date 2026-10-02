-- Dormant operator-owned identity mappings; no data/network admission or seed.
BEGIN;
CREATE TABLE public.athena_staff_connections (
 practice_id text NOT NULL, tenant_id uuid NOT NULL,
 connection_revision integer NOT NULL CHECK(connection_revision>0),
 environment text NOT NULL CHECK(environment IN ('preview','production')),
 vendor_practice_id text NOT NULL CHECK(vendor_practice_id ~ '^[1-9][0-9]{0,17}$'),
 profile_sha256 text NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
 review_ref text NOT NULL CHECK(length(review_ref) BETWEEN 1 AND 160 AND review_ref=btrim(review_ref) AND review_ref !~ '[[:cntrl:]]'),
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(), registered_by name NOT NULL DEFAULT current_user,
 revoked_at timestamptz, revoked_by name, revocation_ref text,
 PRIMARY KEY(practice_id,connection_revision),
 FOREIGN KEY(practice_id,tenant_id) REFERENCES public.practices(practice_id,entra_tenant_id),
 CHECK((revoked_at IS NULL AND revoked_by IS NULL AND revocation_ref IS NULL) OR
       (revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND revocation_ref IS NOT NULL
        AND length(revocation_ref) BETWEEN 1 AND 160 AND revocation_ref=btrim(revocation_ref)
        AND revocation_ref !~ '[[:cntrl:]]'))
);
CREATE TABLE public.athena_staff_case_bindings (
 practice_id text NOT NULL, case_id text NOT NULL CHECK(length(case_id) BETWEEN 1 AND 128 AND case_id=btrim(case_id) AND case_id !~ '[[:cntrl:]]'),
 binding_revision integer NOT NULL CHECK(binding_revision>0), connection_revision integer NOT NULL,
 patient_ref text NOT NULL CHECK(length(patient_ref) BETWEEN 1 AND 128 AND patient_ref=btrim(patient_ref) AND patient_ref !~ '[[:cntrl:]]'),
 source_patient_id text NOT NULL CHECK(source_patient_id ~ '^[1-9][0-9]{0,17}$'),
 scope_sha256 text NOT NULL CHECK(scope_sha256 ~ '^[a-f0-9]{64}$'),
 review_ref text NOT NULL CHECK(length(review_ref) BETWEEN 1 AND 160 AND review_ref=btrim(review_ref) AND review_ref !~ '[[:cntrl:]]'),
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(), registered_by name NOT NULL DEFAULT current_user,
 revoked_at timestamptz, revoked_by name, revocation_ref text,
 PRIMARY KEY(practice_id,case_id,binding_revision),
 FOREIGN KEY(practice_id,case_id) REFERENCES public.chase_items(practice_id,chase_id),
 FOREIGN KEY(practice_id,connection_revision) REFERENCES public.athena_staff_connections(practice_id,connection_revision),
 CHECK((revoked_at IS NULL AND revoked_by IS NULL AND revocation_ref IS NULL) OR
       (revoked_at IS NOT NULL AND revoked_by IS NOT NULL AND revocation_ref IS NOT NULL
        AND length(revocation_ref) BETWEEN 1 AND 160 AND revocation_ref=btrim(revocation_ref)
        AND revocation_ref !~ '[[:cntrl:]]'))
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['athena_staff_connections','athena_staff_case_bindings'] LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY athena_registration_scope ON public.%I USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id))',t);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t);
 END LOOP;
END $$;

-- Invoker security: even an accidentally granted runtime INSERT/UPDATE cannot
-- register. The migration owner is a separate, controlled operator authority.
CREATE FUNCTION public.cosmaneura_athena_registration_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE c public.athena_staff_connections%ROWTYPE; previous integer; patient text; expected text;
BEGIN
 IF current_user IS DISTINCT FROM pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid=TG_RELID))
 THEN RAISE EXCEPTION 'Athena registration requires migration owner'; END IF;
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'Athena registration history is immutable'; END IF;
 IF NEW.practice_id IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
    OR NOT public.cosmaneura_practice_allowed(NEW.practice_id)
 THEN RAISE EXCEPTION 'Athena registration practice unavailable'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('athena-staff-binding:'||NEW.practice_id,0));
 IF TG_OP='UPDATE' THEN
  IF OLD.revoked_at IS NOT NULL OR OLD.revocation_ref IS NOT NULL OR NEW.revocation_ref IS NULL
     OR NEW.revoked_at IS DISTINCT FROM OLD.revoked_at OR NEW.revoked_by IS DISTINCT FROM OLD.revoked_by
     OR (to_jsonb(OLD)-'revocation_ref') IS DISTINCT FROM (to_jsonb(NEW)-'revocation_ref')
  THEN RAISE EXCEPTION 'Athena registration is immutable except first revocation'; END IF;
  NEW.revoked_at:=clock_timestamp(); NEW.revoked_by:=current_user; RETURN NEW;
 END IF;
 IF NEW.revoked_at IS NOT NULL OR NEW.revoked_by IS NOT NULL OR NEW.revocation_ref IS NOT NULL
 THEN RAISE EXCEPTION 'Athena registration must begin unrevoked'; END IF;
 NEW.registered_at:=clock_timestamp(); NEW.registered_by:=current_user;
 IF TG_TABLE_NAME='athena_staff_connections' THEN
  IF NOT EXISTS(SELECT 1 FROM public.practices WHERE practice_id=NEW.practice_id AND entra_tenant_id=NEW.tenant_id AND active)
  THEN RAISE EXCEPTION 'Athena registration tenant unavailable'; END IF;
  SELECT max(connection_revision) INTO previous FROM public.athena_staff_connections WHERE practice_id=NEW.practice_id;
  IF NEW.connection_revision::bigint<>COALESCE(previous::bigint,0)+1 THEN RAISE EXCEPTION 'Athena connection revision conflict'; END IF;
  expected:=encode(sha256(convert_to(public.cosmaneura_handoff_canonical(jsonb_build_array(
   'athena-staff-profile.v1',NEW.practice_id,NEW.environment,NEW.vendor_practice_id,NEW.connection_revision)),'UTF8')),'hex');
  IF NEW.profile_sha256 IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Athena profile fingerprint mismatch'; END IF;
 ELSE
  SELECT * INTO c FROM public.athena_staff_connections WHERE practice_id=NEW.practice_id ORDER BY connection_revision DESC LIMIT 1 FOR SHARE;
  IF c.connection_revision IS NULL OR c.revoked_at IS NOT NULL OR c.connection_revision<>NEW.connection_revision
  THEN RAISE EXCEPTION 'Athena connection unavailable'; END IF;
  SELECT patient_ref INTO patient FROM public.chase_items WHERE practice_id=NEW.practice_id AND chase_id=NEW.case_id FOR SHARE;
  IF patient IS NULL OR patient IS DISTINCT FROM NEW.patient_ref THEN RAISE EXCEPTION 'Athena case patient mismatch'; END IF;
  SELECT max(binding_revision) INTO previous FROM public.athena_staff_case_bindings WHERE practice_id=NEW.practice_id AND case_id=NEW.case_id;
  IF NEW.binding_revision::bigint<>COALESCE(previous::bigint,0)+1 THEN RAISE EXCEPTION 'Athena binding revision conflict'; END IF;
  expected:=encode(sha256(convert_to(public.cosmaneura_handoff_canonical(jsonb_build_array(
   'athena-staff-scope.v1',c.profile_sha256,NEW.case_id,NEW.patient_ref,NEW.source_patient_id,NEW.binding_revision)),'UTF8')),'hex');
  IF NEW.scope_sha256 IS DISTINCT FROM expected THEN RAISE EXCEPTION 'Athena scope fingerprint mismatch'; END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_athena_registration_guard() FROM PUBLIC;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['athena_staff_connections','athena_staff_case_bindings'] LOOP
  EXECUTE format('CREATE TRIGGER athena_registration_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_athena_registration_guard()',t);
  EXECUTE format('CREATE TRIGGER athena_registration_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_athena_registration_guard()',t);
  EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER athena_registration_guard',t);
  EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER athena_registration_no_truncate',t);
 END LOOP;
END $$;

-- Caller receives only a single authorized case scope. Membership/registry/case
-- locks last until the CALLER transaction ends. No mutation grants are needed.
CREATE FUNCTION public.cosmaneura_athena_staff_scope(candidate_tenant uuid,candidate_user uuid,candidate_role text,candidate_case text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=NULLIF(current_setting('cosmaneura.practice_id',true),''); patient text;
 c public.athena_staff_connections%ROWTYPE; b public.athena_staff_case_bindings%ROWTYPE;
BEGIN
 IF current_setting('transaction_isolation')<>'read committed'
 THEN RETURN jsonb_build_object('error','binding_invalid'); END IF;
 IF p IS NULL OR NOT public.cosmaneura_practice_allowed(p)
    OR NOT public.cosmaneura_athena_reviewer_allowed(candidate_tenant,candidate_user,candidate_role)
 THEN RETURN jsonb_build_object('error','not_authorized'); END IF;
 PERFORM pg_advisory_xact_lock_shared(hashtextextended('athena-staff-binding:'||p,0));
 SELECT patient_ref INTO patient FROM public.chase_items WHERE practice_id=p AND chase_id=candidate_case FOR SHARE;
 IF patient IS NULL THEN RETURN jsonb_build_object('error','not_found'); END IF;
 SELECT * INTO c FROM public.athena_staff_connections WHERE practice_id=p ORDER BY connection_revision DESC LIMIT 1 FOR SHARE;
 IF c.connection_revision IS NULL THEN RETURN jsonb_build_object('error','connection_missing'); END IF;
 IF c.revoked_at IS NOT NULL THEN RETURN jsonb_build_object('error','connection_revoked'); END IF;
 IF c.tenant_id IS DISTINCT FROM candidate_tenant THEN RETURN jsonb_build_object('error','not_authorized'); END IF;
 SELECT * INTO b FROM public.athena_staff_case_bindings WHERE practice_id=p AND case_id=candidate_case ORDER BY binding_revision DESC LIMIT 1 FOR SHARE;
 IF b.binding_revision IS NULL THEN RETURN jsonb_build_object('error','binding_missing'); END IF;
 IF b.revoked_at IS NOT NULL THEN RETURN jsonb_build_object('error','binding_revoked'); END IF;
 IF b.connection_revision<>c.connection_revision OR b.patient_ref IS DISTINCT FROM patient
 THEN RETURN jsonb_build_object('error','binding_stale'); END IF;
 RETURN jsonb_build_object('tenantId',c.tenant_id::text,'serverPracticeId',p,'environment',c.environment,
   'vendorPracticeId',c.vendor_practice_id,'connectionRevision',c.connection_revision,'profileSha256',c.profile_sha256,
   'caseId',b.case_id,'patientRef',b.patient_ref,'sourcePatientId',b.source_patient_id,
   'bindingRevision',b.binding_revision,'scopeSha256',b.scope_sha256);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_athena_staff_scope(uuid,uuid,text,text) FROM PUBLIC;
COMMIT;
