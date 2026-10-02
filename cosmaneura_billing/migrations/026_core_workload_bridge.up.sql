-- Additive Core authority; empty by default. No credentials or activation.
BEGIN;
ALTER TABLE public.users ADD COLUMN identity_kind text NOT NULL DEFAULT 'entra'
 CHECK(identity_kind IN ('entra','core'));
COMMENT ON COLUMN public.users.identity_kind IS
 'entra: user_id is an Entra object ID; core: random internal projection, never an Entra OID. tenant_id remains the CAB practice tenant partition.';
CREATE FUNCTION public.cosmaneura_identity_kind_immutable() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN IF NEW.identity_kind IS DISTINCT FROM OLD.identity_kind THEN
 RAISE EXCEPTION 'identity kind is immutable'; END IF; RETURN NEW; END $$;
CREATE TRIGGER users_identity_kind_immutable BEFORE UPDATE ON public.users
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_identity_kind_immutable();
REVOKE ALL ON FUNCTION public.cosmaneura_identity_kind_immutable() FROM PUBLIC;

-- Extend the one existing source family; old connection documents are unchanged.
DO $$DECLARE constraint_name name; matches integer; BEGIN
 SELECT count(*),min(conname::text)::name INTO matches,constraint_name FROM pg_constraint
 WHERE conrelid='public.provider_source_connections'::regclass AND contype='c'
 AND pg_get_constraintdef(oid) LIKE '%sourceMode%';
 IF matches<>1 THEN RAISE EXCEPTION 'provider source constraint mismatch'; END IF;
 EXECUTE format('ALTER TABLE public.provider_source_connections DROP CONSTRAINT %I',constraint_name);
END $$;
ALTER TABLE public.provider_source_connections ADD CONSTRAINT provider_source_connection_kind
 CHECK(jsonb_typeof(document)='object' AND octet_length(document::text)<=2048
 AND (document->>'connectionId') IS NOT DISTINCT FROM connection_id::text
 AND COALESCE(document->>'sourceMode','') IN ('cab_provider_attested','core_workload_verified')
 AND COALESCE(document->>'dataClass','') IN ('synthetic','deidentified'));
CREATE TABLE public.core_workload_connections (
 practice_id text NOT NULL REFERENCES public.practices(practice_id),
 connection_id uuid NOT NULL, version integer NOT NULL CHECK(version>0),
 runtime_role name NOT NULL, core_practice_id text NOT NULL,
 actor_issuer text NOT NULL, workload_tenant uuid NOT NULL, workload_caller uuid NOT NULL,
 workload_audience text NOT NULL, workload_role text NOT NULL,
 not_before timestamptz NOT NULL, expires_at timestamptz NOT NULL,
 review_ref text NOT NULL, revoked_at timestamptz,
 PRIMARY KEY(practice_id,connection_id), UNIQUE(runtime_role),
 CHECK(not_before<expires_at),
 CHECK(length(core_practice_id) BETWEEN 1 AND 128 AND core_practice_id !~ '[[:cntrl:]]'),
 CHECK(length(actor_issuer) BETWEEN 1 AND 256 AND actor_issuer !~ '[[:cntrl:]]'),
 CHECK(length(workload_audience) BETWEEN 1 AND 200 AND workload_audience !~ '[[:cntrl:]]'),
 CHECK(length(workload_role) BETWEEN 1 AND 100 AND workload_role !~ '[[:cntrl:]]'),
 CHECK(length(review_ref) BETWEEN 1 AND 160 AND review_ref !~ '[[:cntrl:]]'),
 FOREIGN KEY(practice_id,connection_id) REFERENCES public.provider_source_connections(practice_id,connection_id)
);
CREATE TABLE public.core_actor_projections (
 practice_id text NOT NULL, projection_id uuid NOT NULL DEFAULT gen_random_uuid(),
 connection_id uuid NOT NULL, issuer text NOT NULL, subject text NOT NULL,
 tenant_id uuid NOT NULL, user_id uuid NOT NULL, version integer NOT NULL DEFAULT 1 CHECK(version>0),
 revoked_at timestamptz,
 PRIMARY KEY(practice_id,projection_id),
 UNIQUE(practice_id,connection_id,issuer,subject), UNIQUE(tenant_id,user_id),
 FOREIGN KEY(practice_id,connection_id) REFERENCES public.core_workload_connections(practice_id,connection_id),
 FOREIGN KEY(practice_id,tenant_id,user_id) REFERENCES public.practice_memberships(practice_id,tenant_id,user_id),
 CHECK(length(issuer) BETWEEN 1 AND 256 AND issuer !~ '[[:cntrl:]]'),
 CHECK(length(subject) BETWEEN 1 AND 256 AND subject !~ '[[:cntrl:]]')
);
CREATE FUNCTION public.cosmaneura_core_projection_valid() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_TABLE_NAME='core_workload_connections' THEN
  IF NOT EXISTS(SELECT 1 FROM public.provider_source_connections p WHERE p.practice_id=NEW.practice_id
   AND p.connection_id=NEW.connection_id AND p.document->>'sourceMode'='core_workload_verified'
   AND p.document->>'version'=NEW.version::text AND p.revoked_at IS NULL) THEN
   RAISE EXCEPTION 'Core source connection mismatch'; END IF;
 ELSE
  IF NOT EXISTS(SELECT 1 FROM public.users u JOIN public.practice_memberships m
   ON m.tenant_id=u.tenant_id AND m.user_id=u.user_id
   JOIN public.core_workload_connections c ON c.practice_id=m.practice_id AND c.connection_id=NEW.connection_id
   WHERE m.practice_id=NEW.practice_id AND u.tenant_id=NEW.tenant_id AND u.user_id=NEW.user_id
   AND u.identity_kind='core' AND m.roles=ARRAY['Provider']::text[] AND c.actor_issuer=NEW.issuer) THEN
   RAISE EXCEPTION 'Core identity projection mismatch'; END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_projection_valid() FROM PUBLIC;
CREATE TRIGGER core_connection_valid BEFORE INSERT ON public.core_workload_connections
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_projection_valid();
CREATE TRIGGER core_projection_valid BEFORE INSERT ON public.core_actor_projections
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_projection_valid();
CREATE FUNCTION public.cosmaneura_core_registry_immutable() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='UPDATE' AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
    AND (to_jsonb(OLD)-'revoked_at')=(to_jsonb(NEW)-'revoked_at') THEN RETURN NEW; END IF;
 RAISE EXCEPTION 'Core registry identity is immutable';
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_registry_immutable() FROM PUBLIC;
DO $$DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['core_workload_connections','core_actor_projections'] LOOP
  EXECUTE format('CREATE TRIGGER core_registry_immutable BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_registry_immutable()',t);
  EXECUTE format('CREATE TRIGGER core_registry_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_core_registry_immutable()',t);
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY core_registry_scope ON public.%I USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id))',t);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t);
 END LOOP;
END $$;

-- Existing Entra authorizations stay unchanged. Core projections only work on
-- their distinct bound bridge login, never as accidental Entra OID aliases.
CREATE OR REPLACE FUNCTION public.cosmaneura_billing_header_editor_allowed(
 candidate_tenant uuid,candidate_user uuid,candidate_role text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE kind text; allowed boolean;
BEGIN
 IF candidate_role NOT IN ('Provider','Coder','Admin') THEN RETURN false; END IF;
 SELECT u.identity_kind INTO kind FROM public.practice_memberships m
 JOIN public.practices p ON p.practice_id=m.practice_id
 JOIN public.users u ON u.tenant_id=m.tenant_id AND u.user_id=m.user_id
 WHERE m.practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
 AND public.cosmaneura_practice_allowed(m.practice_id)
 AND m.tenant_id=candidate_tenant AND m.user_id=candidate_user
 AND m.active AND p.active AND u.active AND candidate_role=ANY(m.roles)
 FOR SHARE OF m,p,u;
 IF kind='entra' THEN RETURN true; END IF;
 IF kind IS DISTINCT FROM 'core' OR candidate_role<>'Provider' THEN RETURN false; END IF;
 SELECT true INTO allowed FROM public.core_actor_projections a
 JOIN public.core_workload_connections c ON c.practice_id=a.practice_id AND c.connection_id=a.connection_id
 JOIN public.provider_source_connections sc ON sc.practice_id=c.practice_id AND sc.connection_id=c.connection_id
 WHERE sc.revoked_at IS NULL AND a.practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
 AND a.tenant_id=candidate_tenant AND a.user_id=candidate_user
 AND a.issuer=c.actor_issuer AND a.revoked_at IS NULL AND c.revoked_at IS NULL
 AND c.runtime_role=session_user::name AND c.not_before<=clock_timestamp() AND clock_timestamp()<c.expires_at
 FOR SHARE OF a,c,sc;
 RETURN COALESCE(allowed,false);
END $$;

CREATE FUNCTION public.cosmaneura_resolve_core_actor(
 candidate_tenant uuid,candidate_caller uuid,candidate_audience text,candidate_role text,
 core_practice text,actor_issuer text,actor_subject text
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE a public.core_actor_projections%ROWTYPE; c public.core_workload_connections%ROWTYPE;
BEGIN
 SELECT * INTO c FROM public.core_workload_connections x
 WHERE x.practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
 AND public.cosmaneura_practice_allowed(x.practice_id) AND x.runtime_role=session_user::name
 AND EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname=session_user
  AND r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreaterole AND NOT r.rolcreatedb)
 AND x.workload_tenant=candidate_tenant AND x.workload_caller=candidate_caller
 AND x.workload_audience=candidate_audience AND x.workload_role=candidate_role
 AND x.core_practice_id=core_practice AND x.actor_issuer=cosmaneura_resolve_core_actor.actor_issuer FOR SHARE;
 IF c.connection_id IS NULL OR c.revoked_at IS NOT NULL OR clock_timestamp()<c.not_before
 OR clock_timestamp()>=c.expires_at THEN RETURN NULL; END IF;
 SELECT * INTO a FROM public.core_actor_projections x WHERE x.practice_id=c.practice_id
 AND x.connection_id=c.connection_id AND x.issuer=cosmaneura_resolve_core_actor.actor_issuer AND x.subject=actor_subject FOR SHARE;
 IF a.projection_id IS NULL OR a.revoked_at IS NOT NULL
 OR NOT public.cosmaneura_billing_header_editor_allowed(a.tenant_id,a.user_id,'Provider') THEN
 RETURN NULL; END IF;
 RETURN jsonb_build_object('practiceId',a.practice_id,'tenantId',a.tenant_id::text,'userId',a.user_id::text,
 'issuer',a.issuer,'subject',a.subject,'projectionId',a.projection_id::text,'mappingVersion',a.version,
 'connectionId',c.connection_id::text,'connectionVersion',c.version);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_resolve_core_actor(uuid,uuid,text,text,text,text,text) FROM PUBLIC;

-- Actor snapshots must identify their immutable Core projection. The existing
-- operation/audit FKs retain the same projection via tenant_id/user_id.
CREATE FUNCTION public.cosmaneura_core_header_provenance() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE expected jsonb; actual jsonb; kind text;
BEGIN
 SELECT identity_kind INTO kind FROM public.users
 WHERE tenant_id=NEW.actor_tenant_id AND user_id=NEW.actor_user_id;
 IF kind IS DISTINCT FROM 'core' THEN RETURN NEW; END IF;
 SELECT jsonb_build_object('kind','core','issuer',issuer,'subject',subject,
  'projectionId',projection_id::text,'mappingVersion',version) INTO expected
 FROM public.core_actor_projections WHERE practice_id=NEW.practice_id
 AND tenant_id=NEW.actor_tenant_id AND user_id=NEW.actor_user_id;
 IF TG_TABLE_NAME='billing_headers' THEN actual:=NEW.baseline#>'{createdBy,identity}';
 ELSE actual:=NEW.revision_payload#>'{actor,identity}'; END IF;
 IF expected IS NULL OR actual IS DISTINCT FROM expected THEN
 RAISE EXCEPTION 'Core actor provenance mismatch'; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_header_provenance() FROM PUBLIC;
CREATE TRIGGER billing_headers_core_provenance BEFORE INSERT ON public.billing_headers
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_header_provenance();
CREATE TRIGGER billing_revisions_core_provenance BEFORE INSERT ON public.billing_header_revisions
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_header_provenance();

CREATE TABLE public.core_workload_operations (
 practice_id text NOT NULL, operation_id uuid NOT NULL, projection_id uuid NOT NULL,
 connection_id uuid NOT NULL, operation_sha256 text NOT NULL CHECK(operation_sha256 ~ '^[a-f0-9]{64}$'),
 metadata jsonb NOT NULL CHECK(jsonb_typeof(metadata)='object' AND octet_length(metadata::text)<=32768
   AND NOT metadata ? 'noteText'),
 PRIMARY KEY(practice_id,operation_id),
 FOREIGN KEY(practice_id,projection_id) REFERENCES public.core_actor_projections(practice_id,projection_id),
 FOREIGN KEY(practice_id,operation_id) REFERENCES public.provider_source_admissions(practice_id,operation_id),
 FOREIGN KEY(practice_id,operation_id) REFERENCES public.provider_handoff_operations(practice_id,operation_id)
  DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE public.core_workload_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.core_workload_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY core_operation_scope ON public.core_workload_operations
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.core_workload_operations FROM PUBLIC;
CREATE FUNCTION public.cosmaneura_core_history_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Core operation history is immutable'; END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_history_immutable() FROM PUBLIC;
CREATE TRIGGER core_operation_immutable BEFORE UPDATE OR DELETE ON public.core_workload_operations
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_core_history_immutable();
CREATE TRIGGER core_operation_no_truncate BEFORE TRUNCATE ON public.core_workload_operations
 FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_core_history_immutable();

-- Only the distinct bridge login can register an exact authenticated operation.
-- TLS/token validation belongs to the bridge adapter. This function validates
-- its consumed source/actor/expiry against protected local authority, and never
-- grants arbitrary importer or identity-writing privilege to the runtime.
CREATE FUNCTION public.cosmaneura_prepare_core_handoff(command jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=NULLIF(current_setting('cosmaneura.practice_id',true),'');
 a public.core_actor_projections%ROWTYPE; c public.core_workload_connections%ROWTYPE;
 sc public.provider_source_connections%ROWTYPE;
 x jsonb:=command->'metadata'; v jsonb:=command->'authorization'; g jsonb:=command->'grant';
 op uuid:=(x->>'operationId')::uuid;
BEGIN
 SELECT * INTO a FROM public.core_actor_projections WHERE practice_id=p
 AND projection_id=(command->>'projectionId')::uuid FOR SHARE;
 SELECT * INTO c FROM public.core_workload_connections WHERE practice_id=p AND connection_id=a.connection_id FOR SHARE;
 SELECT * INTO sc FROM public.provider_source_connections WHERE practice_id=p AND connection_id=c.connection_id FOR SHARE;
 IF a.projection_id IS NULL OR c.connection_id IS NULL OR a.revoked_at IS NOT NULL OR c.revoked_at IS NOT NULL
 OR sc.connection_id IS NULL OR sc.revoked_at IS NOT NULL OR c.runtime_role IS DISTINCT FROM session_user::name
 OR NOT public.cosmaneura_billing_header_editor_allowed(a.tenant_id,a.user_id,'Provider')
 OR sc.document->>'sourceMode' IS DISTINCT FROM 'core_workload_verified'
 OR x->>'practiceId' IS DISTINCT FROM c.core_practice_id
 OR x#>>'{actor,issuer}' IS DISTINCT FROM a.issuer OR x#>>'{actor,subject}' IS DISTINCT FROM a.subject
 OR x#>>'{actor,role}' IS DISTINCT FROM 'Provider'
 OR x#>>'{admission,dataClass}' IS DISTINCT FROM sc.document->>'dataClass'
 OR x#>>'{admission,contentSha256}' IS DISTINCT FROM x#>>'{source,contentSha256}'
 OR x#>>'{completion,status}' IS DISTINCT FROM 'completed' OR x#>'{completion,confirmedByProvider}' IS DISTINCT FROM 'true'::jsonb
 OR v->>'schemaVersion' IS DISTINCT FROM 'core-cab-authorization.v1' OR v->'allowed' IS DISTINCT FROM 'true'::jsonb
 OR v->>'operationId' IS DISTINCT FROM op::text OR v->>'purpose' IS DISTINCT FROM 'completed-note.accept'
 OR v->>'operationSha256' IS DISTINCT FROM command->>'operationSha256'
 OR v->>'practiceId' IS DISTINCT FROM c.core_practice_id OR v->'source' IS DISTINCT FROM x->'source'
 OR v->'admission' IS DISTINCT FROM x->'admission'
 OR v->'actor' IS DISTINCT FROM ((x->'actor')-'sessionSha256')
 OR NOT COALESCE((v->>'authorizedAt')::timestamptz<=clock_timestamp()+interval '2 seconds'
    AND clock_timestamp()<(v->>'expiresAt')::timestamptz
    AND (v->>'expiresAt')::timestamptz-(v->>'authorizedAt')::timestamptz<=interval '30 seconds',false)
 OR g->>'operationId' IS DISTINCT FROM op::text OR g->>'connectionId' IS DISTINCT FROM c.connection_id::text
 OR g->>'connectionVersion' IS DISTINCT FROM c.version::text OR g->>'actorTenantId' IS DISTINCT FROM a.tenant_id::text
 OR g->>'actorUserId' IS DISTINCT FROM a.user_id::text OR g->>'actorRole' IS DISTINCT FROM 'Provider'
 OR g->>'sourceRevision' IS DISTINCT FROM x#>>'{source,revision}'
 OR NOT COALESCE((g->>'notBefore')::timestamptz>=(v->>'authorizedAt')::timestamptz-interval '2 seconds'
    AND (g->>'notBefore')::timestamptz<=clock_timestamp(),false)
 OR g->>'expiresAt' IS DISTINCT FROM v->>'expiresAt'
 THEN RAISE EXCEPTION 'Core source operation unavailable'; END IF;
 INSERT INTO public.provider_source_admissions VALUES(p,op,c.connection_id,g,NULL);
 INSERT INTO public.core_workload_operations VALUES(p,op,a.projection_id,c.connection_id,command->>'operationSha256',x);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_prepare_core_handoff(jsonb) FROM PUBLIC;

CREATE FUNCTION public.cosmaneura_core_operation(candidate_projection uuid,candidate_operation uuid)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE a public.core_actor_projections%ROWTYPE; result jsonb;
BEGIN
 SELECT * INTO a FROM public.core_actor_projections WHERE practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
 AND projection_id=candidate_projection;
 IF a.projection_id IS NULL OR NOT public.cosmaneura_billing_header_editor_allowed(a.tenant_id,a.user_id,'Provider') THEN
 RAISE EXCEPTION 'Core operation unavailable'; END IF;
 SELECT jsonb_build_object('metadata',c.metadata,'operationSha256',c.operation_sha256,'receipt',h.receipt) INTO result
 FROM public.core_workload_operations c JOIN public.provider_handoff_operations h
 ON h.practice_id=c.practice_id AND h.operation_id=c.operation_id
 WHERE c.practice_id=a.practice_id AND c.projection_id=a.projection_id AND c.operation_id=candidate_operation;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_operation(uuid,uuid) FROM PUBLIC;

-- A row lock does not grant the bridge UPDATE on any clinical source column.
CREATE FUNCTION public.cosmaneura_core_lock_encounter(candidate_projection uuid,candidate_encounter text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE a public.core_actor_projections%ROWTYPE; e public.encounters%ROWTYPE;
BEGIN
 SELECT * INTO a FROM public.core_actor_projections WHERE practice_id=NULLIF(current_setting('cosmaneura.practice_id',true),'')
 AND projection_id=candidate_projection;
 IF a.projection_id IS NULL OR NOT public.cosmaneura_billing_header_editor_allowed(a.tenant_id,a.user_id,'Provider') THEN
 RAISE EXCEPTION 'Core source unavailable'; END IF;
 SELECT * INTO e FROM public.encounters WHERE practice_id=a.practice_id AND external_id=candidate_encounter FOR UPDATE;
 IF e.encounter_uid IS NULL THEN RETURN NULL; END IF;
 IF e.source IS DISTINCT FROM 'provider_completed_note' OR NOT EXISTS(
  SELECT 1 FROM public.provider_handoff_heads h JOIN public.provider_source_bindings b
   ON b.practice_id=h.practice_id AND b.record_key=h.record_key
  WHERE h.practice_id=a.practice_id AND h.encounter_uid=e.encounter_uid AND b.connection_id=a.connection_id) THEN
 RAISE EXCEPTION 'Core source unavailable'; END IF;
 RETURN to_jsonb(e);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_core_lock_encounter(uuid,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.cosmaneura_commit_provider_handoff(command jsonb) RETURNS void
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
    OR COALESCE(c.document->>'sourceMode','') NOT IN ('cab_provider_attested','core_workload_verified')
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
 IF c.document->>'sourceMode'='core_workload_verified' AND NOT EXISTS(
 SELECT 1 FROM public.core_workload_operations x JOIN public.core_actor_projections a
 ON a.practice_id=x.practice_id AND a.projection_id=x.projection_id
 WHERE x.practice_id=p AND x.operation_id=op AND x.connection_id=c.connection_id
 AND a.tenant_id=(actor->>'tenant_id')::uuid AND a.user_id=(actor->>'user_id')::uuid
 AND actor->>'issuer'=a.issuer AND actor->>'subject'=a.subject
 AND actor->>'projection_id'=a.projection_id::text AND actor->>'mapping_version'=a.version::text
 AND x.metadata#>>'{source,noteId}'=m#>>'{note,id}'
 AND x.metadata#>>'{source,textSha256}'=m#>>'{note,sha256}'
 AND x.metadata#>>'{source,revision}'=command->>'sourceRevision'
 AND x.metadata->'visit'=m->'visit' AND x.metadata->'completion'=m->'completion'
 ) THEN RAISE EXCEPTION 'Core source binding mismatch'; END IF;
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

CREATE OR REPLACE FUNCTION public.cosmaneura_provider_model_source(actor_tenant uuid,actor_user uuid,actor_role text,encounter_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=NULLIF(current_setting('cosmaneura.practice_id',true),'');
 e public.encounters%ROWTYPE; h public.provider_handoff_heads%ROWTYPE;
 o public.provider_handoff_operations%ROWTYPE; g public.provider_source_admissions%ROWTYPE;
 c public.provider_source_connections%ROWTYPE; a jsonb; bound uuid;
BEGIN
 IF p IS NULL OR NOT public.cosmaneura_practice_allowed(p)
    OR NOT public.cosmaneura_billing_header_editor_allowed(actor_tenant,actor_user,actor_role) THEN
   RAISE EXCEPTION 'provider model source unavailable'; END IF;
 -- No encounter/head lock: header persistence already owns its encounter lock.
 -- Locking the head here would invert the provider writer's head/encounter order.
 SELECT * INTO e FROM public.encounters WHERE practice_id=p AND external_id=encounter_id;
 SELECT * INTO h FROM public.provider_handoff_heads WHERE practice_id=p AND encounter_uid=e.encounter_uid;
 IF e.source IS DISTINCT FROM 'provider_completed_note' OR h.record_key IS NULL
    OR h.record_key IS DISTINCT FROM e.external_id
    OR h.header_source IS DISTINCT FROM public.cosmaneura_handoff_source(e)
    OR h.manifest_sha256 IS DISTINCT FROM encode(sha256(convert_to(public.cosmaneura_handoff_canonical(h.manifest),'UTF8')),'hex') THEN
   RAISE EXCEPTION 'provider model source unavailable'; END IF;
 -- Select first, then inspect revocation: never fall back to an older grant.
 SELECT * INTO o FROM public.provider_handoff_operations
  WHERE practice_id=p AND record_key=h.record_key AND receipt->'headerSource'=h.header_source
    AND receipt->>'sourceManifestSha256'=h.manifest_sha256
    AND receipt->>'sourceRevision'=h.source_revision::text
  ORDER BY (receipt->>'acceptedAt')::timestamptz DESC,operation_id DESC LIMIT 1;
 IF o.operation_id IS NULL THEN RAISE EXCEPTION 'provider model source unavailable'; END IF;
 SELECT * INTO g FROM public.provider_source_admissions WHERE practice_id=p AND operation_id=o.operation_id FOR SHARE;
 SELECT * INTO c FROM public.provider_source_connections WHERE practice_id=p AND connection_id=g.connection_id FOR SHARE;
 SELECT connection_id INTO bound FROM public.provider_source_bindings WHERE practice_id=p AND record_key=h.record_key;
 SELECT provenance INTO a FROM public.provider_source_acceptances WHERE practice_id=p AND operation_id=o.operation_id;
 IF g.operation_id IS NULL OR c.connection_id IS NULL OR g.revoked_at IS NOT NULL OR c.revoked_at IS NOT NULL
    OR bound IS DISTINCT FROM c.connection_id
    OR c.document->>'connectionId' IS DISTINCT FROM c.connection_id::text
    OR c.document->>'tenantId' IS DISTINCT FROM actor_tenant::text
    OR COALESCE(c.document->>'sourceMode','') NOT IN ('cab_provider_attested','core_workload_verified')
    OR COALESCE(c.document->>'dataClass','') NOT IN ('synthetic','deidentified')
    OR g.document->>'operationId' IS DISTINCT FROM o.operation_id::text
    OR g.document->>'connectionId' IS DISTINCT FROM c.connection_id::text
    OR g.document->'connectionVersion' IS DISTINCT FROM c.document->'version'
    OR g.document->>'actorTenantId' IS DISTINCT FROM o.actor_tenant_id::text
    OR g.document->>'actorUserId' IS DISTINCT FROM o.actor_user_id::text
    OR g.document->>'actorRole' IS DISTINCT FROM o.actor_role
    OR g.document->>'recordKey' IS DISTINCT FROM h.record_key
    OR g.document->>'sourceRevision' IS DISTINCT FROM h.source_revision::text
    OR g.document->>'manifestSha256' IS DISTINCT FROM h.manifest_sha256
    OR a IS DISTINCT FROM jsonb_build_object('connection',c.document,'admission',g.document,'requestSha256',g.document->>'requestSha256') THEN
   RAISE EXCEPTION 'provider model source unavailable'; END IF;
 -- Grant expiry constrained the already-completed storage operation. A later
 -- model disclosure instead checks its separate model policy's live expiry.
 RETURN jsonb_build_object('headerSource',h.header_source,'sourceOperationId',o.operation_id::text,
   'sourceAdmissionSha256',encode(sha256(convert_to(public.cosmaneura_handoff_canonical(a),'UTF8')),'hex'),
   'connection',jsonb_build_object('connectionId',c.connection_id::text,
      'connectionVersion',c.document->'version','dataClass',c.document->'dataClass'))
   || CASE WHEN c.document->>'sourceMode'='core_workload_verified' THEN
    jsonb_build_object('coreOperationId',o.operation_id::text) ELSE '{}'::jsonb END;
END $$;
COMMIT;
