-- Empty-by-default operator registry. No tenant/connection/grant is seeded.
BEGIN;
CREATE TABLE public.provider_source_connections (
 practice_id text NOT NULL REFERENCES public.practices(practice_id), connection_id uuid NOT NULL,
 document jsonb NOT NULL CHECK(jsonb_typeof(document)='object' AND octet_length(document::text)<=2048
   AND document->>'connectionId'=connection_id::text
   AND document->>'sourceMode'='cab_provider_attested' AND document->>'dataClass' IN ('synthetic','deidentified')),
 revoked_at timestamptz, PRIMARY KEY(practice_id,connection_id)
);
CREATE TABLE public.provider_source_admissions (
 practice_id text NOT NULL, operation_id uuid NOT NULL, connection_id uuid NOT NULL,
 document jsonb NOT NULL CHECK(jsonb_typeof(document)='object' AND octet_length(document::text)<=16384
   AND document->>'operationId'=operation_id::text AND document->>'connectionId'=connection_id::text),
 revoked_at timestamptz, PRIMARY KEY(practice_id,operation_id),
 FOREIGN KEY(practice_id,connection_id) REFERENCES public.provider_source_connections(practice_id,connection_id)
);
CREATE TABLE public.provider_source_bindings (
 practice_id text NOT NULL, record_key text NOT NULL, connection_id uuid NOT NULL,
 PRIMARY KEY(practice_id,record_key),
 FOREIGN KEY(practice_id,record_key) REFERENCES public.provider_handoff_heads(practice_id,record_key),
 FOREIGN KEY(practice_id,connection_id) REFERENCES public.provider_source_connections(practice_id,connection_id)
);
CREATE TABLE public.provider_source_acceptances (
 practice_id text NOT NULL, operation_id uuid NOT NULL,
 provenance jsonb NOT NULL CHECK(jsonb_typeof(provenance)='object' AND octet_length(provenance::text)<=24576),
 PRIMARY KEY(practice_id,operation_id),
 FOREIGN KEY(practice_id,operation_id) REFERENCES public.provider_handoff_operations(practice_id,operation_id),
 FOREIGN KEY(practice_id,operation_id) REFERENCES public.provider_source_admissions(practice_id,operation_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['provider_source_connections','provider_source_admissions','provider_source_bindings','provider_source_acceptances'] LOOP
   EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY provider_source_scope ON public.%I USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id))',t);
   EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t);
 END LOOP;
END $$;
CREATE FUNCTION public.cosmaneura_source_registration_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP<>'UPDATE' OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
    OR (to_jsonb(OLD)-'revoked_at') IS DISTINCT FROM (to_jsonb(NEW)-'revoked_at') THEN
   RAISE EXCEPTION 'source registration is immutable except first revocation'; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_source_registration_immutable() FROM PUBLIC;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['provider_source_connections','provider_source_admissions'] LOOP
   EXECUTE format('CREATE TRIGGER source_registration_immutable BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_source_registration_immutable()',t);
   EXECUTE format('CREATE TRIGGER source_registration_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_handoff_receipt_immutable()',t);
   EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER source_registration_immutable',t);
   EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER source_registration_no_truncate',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['provider_source_bindings','provider_source_acceptances'] LOOP
   EXECUTE format('CREATE TRIGGER source_provenance_immutable BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_handoff_receipt_immutable()',t);
   EXECUTE format('CREATE TRIGGER source_provenance_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_handoff_receipt_immutable()',t);
   EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER source_provenance_immutable',t);
   EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER source_provenance_no_truncate',t);
 END LOOP;
END $$;

-- Retain the reviewed019 validator but remove every nonowner direct invocation.
ALTER FUNCTION public.cosmaneura_commit_provider_handoff(jsonb) RENAME TO cosmaneura_commit_provider_handoff_019;
DO $$ DECLARE a record; BEGIN
 FOR a IN SELECT DISTINCT acl.grantee FROM pg_proc p,
   LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl
   WHERE p.oid='public.cosmaneura_commit_provider_handoff_019(jsonb)'::regprocedure AND acl.grantee<>p.proowner LOOP
   IF a.grantee=0 THEN REVOKE ALL ON FUNCTION public.cosmaneura_commit_provider_handoff_019(jsonb) FROM PUBLIC;
   ELSE EXECUTE format('REVOKE ALL ON FUNCTION public.cosmaneura_commit_provider_handoff_019(jsonb) FROM %I',pg_get_userbyid(a.grantee)); END IF;
 END LOOP;
END $$;
CREATE FUNCTION public.cosmaneura_commit_provider_handoff(command jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=command->>'practice'; k text:=command->>'recordKey';
 op uuid:=(command->'receipt'->>'operationId')::uuid; actor jsonb:=command->'actor';
 c public.provider_source_connections%ROWTYPE; g public.provider_source_admissions%ROWTYPE;
 binding uuid; request jsonb; docs jsonb; b jsonb:=command->'bundle'; m jsonb:=command->'manifest';
BEGIN
 IF p IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
    OR NOT public.cosmaneura_practice_allowed(p)
    OR NOT public.cosmaneura_billing_header_editor_allowed((actor->>'tenant_id')::uuid,(actor->>'user_id')::uuid,actor->>'role') THEN
   RAISE EXCEPTION 'provider admission unavailable'; END IF;
 IF command->'fixture'='true'::jsonb THEN
   PERFORM public.cosmaneura_commit_provider_handoff_019(command); RETURN; END IF;
 IF command->'fixture' IS DISTINCT FROM 'false'::jsonb THEN RAISE EXCEPTION 'invalid provider admission'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('provider-handoff:'||p||':operation:'||op,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('provider-handoff:'||p||':record:'||k,0));
 SELECT * INTO g FROM public.provider_source_admissions WHERE practice_id=p AND operation_id=op FOR SHARE;
 SELECT * INTO c FROM public.provider_source_connections WHERE practice_id=p AND connection_id=g.connection_id FOR SHARE;
 IF g.operation_id IS NULL OR c.connection_id IS NULL OR g.revoked_at IS NOT NULL OR c.revoked_at IS NOT NULL
    OR c.document->>'connectionId' IS DISTINCT FROM c.connection_id::text
    OR g.document->>'connectionId' IS DISTINCT FROM g.connection_id::text
    OR g.document->>'operationId' IS DISTINCT FROM g.operation_id::text
    OR c.document->>'sourceMode' IS DISTINCT FROM 'cab_provider_attested'
    OR COALESCE(c.document->>'dataClass','') NOT IN ('synthetic','deidentified')
    OR COALESCE(c.document->>'version','') !~ '^[1-9][0-9]{0,9}$'
    OR COALESCE(g.document->>'admissionId','') !~ '^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$'
    OR g.document->>'connectionVersion' IS DISTINCT FROM c.document->>'version'
    OR g.document->>'actorTenantId' IS DISTINCT FROM actor->>'tenant_id'
    OR c.document->>'tenantId' IS DISTINCT FROM actor->>'tenant_id'
    OR g.document->>'actorUserId' IS DISTINCT FROM actor->>'user_id'
    OR g.document->>'actorRole' IS DISTINCT FROM actor->>'role'
    OR g.document->>'recordKey' IS DISTINCT FROM k
    OR g.document->>'sourceRevision' IS DISTINCT FROM command->>'sourceRevision'
    OR g.document->>'manifestSha256' IS DISTINCT FROM command->>'manifestSha256'
    OR public.cosmaneura_handoff_canonical(g.document->'expectedSource') IS DISTINCT FROM public.cosmaneura_handoff_canonical(command->'expectedSource')
    OR NOT COALESCE(clock_timestamp()>=(g.document->>'notBefore')::timestamptz AND clock_timestamp()<(g.document->>'expiresAt')::timestamptz,false) THEN
   RAISE EXCEPTION 'provider admission unavailable'; END IF;
 SELECT COALESCE(jsonb_agg(doc||jsonb_build_object('text',b->>CASE WHEN doc->>'kind'='intake' THEN 'intake_text' ELSE 'transcript' END) ORDER BY n),'[]'::jsonb)
   INTO docs FROM jsonb_array_elements(m->'supportingDocuments') WITH ORDINALITY AS x(doc,n);
 request:=(m-'versionKey')||jsonb_build_object('operationId',op::text,'expectedSource',command->'expectedSource',
   'note',(m->'note')||jsonb_build_object('text',b->>'signed_note'),'supportingDocuments',docs);
 IF g.document->>'requestSha256' IS DISTINCT FROM encode(sha256(convert_to(public.cosmaneura_handoff_canonical(request),'UTF8')),'hex') THEN
   RAISE EXCEPTION 'provider request is not admitted'; END IF;
 IF command->>'requestSha256' IS DISTINCT FROM encode(sha256(convert_to(public.cosmaneura_handoff_canonical(
   jsonb_build_object('request',request,'actor',actor,'practice',p)),'UTF8')),'hex') THEN
   RAISE EXCEPTION 'provider request actor binding mismatch'; END IF;
 SELECT connection_id INTO binding FROM public.provider_source_bindings WHERE practice_id=p AND record_key=k;
 IF (binding IS NOT NULL AND binding<>c.connection_id) OR (binding IS NULL AND EXISTS(
   SELECT 1 FROM public.provider_handoff_heads WHERE practice_id=p AND record_key=k)) THEN
   RAISE EXCEPTION 'provider source connection mismatch'; END IF;
 -- The019 routine checks all source hashes/CAS/current membership and inserts
 -- source+receipt+audit atomically. Shared locks prevent concurrent revocation.
 PERFORM public.cosmaneura_commit_provider_handoff_019(command);
 IF NOT COALESCE(clock_timestamp()<(g.document->>'expiresAt')::timestamptz,false) THEN
   RAISE EXCEPTION 'provider admission expired before persistence'; END IF;
 INSERT INTO public.provider_source_bindings VALUES(p,k,c.connection_id) ON CONFLICT DO NOTHING;
 INSERT INTO public.provider_source_acceptances VALUES(p,op,jsonb_build_object(
   'connection',c.document,'admission',g.document,'requestSha256',g.document->>'requestSha256'));
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_commit_provider_handoff(jsonb) FROM PUBLIC;
COMMIT;
