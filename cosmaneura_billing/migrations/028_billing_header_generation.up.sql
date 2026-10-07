-- Additive durable, source-unique generation. Runtime gets only narrow EXECUTE.
BEGIN;
CREATE TABLE public.billing_header_generations (
 practice_id text NOT NULL, job_id uuid NOT NULL, encounter_uid uuid NOT NULL,
 encounter_version bigint NOT NULL CHECK(encounter_version>0), source jsonb NOT NULL,
 source_sha text NOT NULL CHECK(source_sha ~ '^[a-f0-9]{64}$'), owner_id uuid NOT NULL,
 actor_tenant_id uuid NOT NULL, actor_user_id uuid NOT NULL, actor_role text NOT NULL,
 operation_id uuid NOT NULL, audit_event_id uuid NOT NULL,
 deadline timestamptz NOT NULL, started boolean NOT NULL DEFAULT false,
 status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','succeeded','failed','unknown')),
 header_id uuid, created boolean NOT NULL DEFAULT false,
 PRIMARY KEY(practice_id,job_id), UNIQUE(practice_id,encounter_uid,encounter_version),
 FOREIGN KEY(practice_id,encounter_uid) REFERENCES public.encounters(practice_id,encounter_uid),
 FOREIGN KEY(practice_id,audit_event_id) REFERENCES public.access_audit_attempts(practice_id,event_id)
);
CREATE TABLE public.billing_header_generation_requests (
 practice_id text NOT NULL, operation_id uuid NOT NULL, job_id uuid NOT NULL,
 actor_tenant_id uuid NOT NULL, actor_user_id uuid NOT NULL, actor_role text NOT NULL,
 PRIMARY KEY(practice_id,operation_id),
 FOREIGN KEY(practice_id,job_id) REFERENCES public.billing_header_generations(practice_id,job_id)
);
CREATE FUNCTION public.cosmaneura_generation_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP<>'UPDATE' OR TG_TABLE_NAME<>'billing_header_generations'
 OR (to_jsonb(NEW)-ARRAY['status','started','header_id','created']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['status','started','header_id','created'])
 OR OLD.status<>'running' OR OLD.started AND NOT NEW.started
 OR NEW.status='failed' AND NEW.started THEN
  RAISE EXCEPTION 'generation history is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER generation_immutable BEFORE UPDATE OR DELETE ON public.billing_header_generations
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_generation_immutable();
CREATE TRIGGER generation_request_immutable BEFORE UPDATE OR DELETE ON public.billing_header_generation_requests
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_generation_immutable();
CREATE TRIGGER generation_no_truncate BEFORE TRUNCATE ON public.billing_header_generations
 FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_generation_immutable();
CREATE TRIGGER generation_request_no_truncate BEFORE TRUNCATE ON public.billing_header_generation_requests
 FOR EACH STATEMENT EXECUTE FUNCTION public.cosmaneura_generation_immutable();
ALTER TABLE public.billing_header_generations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_header_generations FORCE ROW LEVEL SECURITY;
CREATE POLICY generation_scope ON public.billing_header_generations
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
ALTER TABLE public.billing_header_generation_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.billing_header_generation_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY generation_request_scope ON public.billing_header_generation_requests
 USING(public.cosmaneura_practice_allowed(practice_id)) WITH CHECK(public.cosmaneura_practice_allowed(practice_id));
REVOKE ALL ON public.billing_header_generations,public.billing_header_generation_requests FROM PUBLIC;

CREATE FUNCTION public.cosmaneura_header_generation(c jsonb) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=NULLIF(current_setting('cosmaneura.practice_id',true),'');
 a text:=c->>'action'; op public.billing_header_operations; j public.billing_header_generations; r public.billing_header_generation_requests;
 e public.encounters; fresh boolean:=false; live boolean; h uuid; d timestamptz;
BEGIN
 IF NOT public.cosmaneura_practice_allowed(p) OR a IS NULL OR a NOT IN ('claim','status','check','start','finish')
 OR jsonb_typeof(c) IS DISTINCT FROM 'object' OR c - ARRAY['action','tenant','user','role','encounter','source','sourceSha','operation','job','owner','deadline','audit','status','header','created'] <> '{}'::jsonb THEN RAISE EXCEPTION 'generation unavailable'; END IF;
 -- Claim/check/status revalidate actual current membership and source; failure finalization
 -- remains possible after revocation, but only with this job's private owner capability.
 IF a<>'finish' THEN
  IF NOT public.cosmaneura_billing_header_editor_allowed((c->>'tenant')::uuid,(c->>'user')::uuid,c->>'role')
   THEN RAISE EXCEPTION 'generation not authorized'; END IF;
  IF a='claim' THEN PERFORM pg_advisory_xact_lock(hashtextextended('header-generation:'||p,0)); END IF;
  SELECT * INTO e FROM public.encounters WHERE practice_id=p AND external_id=c->>'encounter' FOR SHARE;
  IF NOT FOUND OR public.cosmaneura_handoff_canonical(c->'source') IS DISTINCT FROM
    public.cosmaneura_handoff_canonical(public.cosmaneura_handoff_source(e))
   OR c->>'sourceSha' IS DISTINCT FROM encode(sha256(convert_to(public.cosmaneura_handoff_canonical(c->'source'),'UTF8')),'hex')
   THEN RAISE EXCEPTION 'generation source changed'; END IF;
 END IF;
 IF a IN ('claim','status') THEN
  SELECT * INTO r FROM public.billing_header_generation_requests WHERE practice_id=p AND operation_id=(c->>'operation')::uuid;
  IF FOUND THEN
   IF r.actor_tenant_id IS DISTINCT FROM (c->>'tenant')::uuid OR r.actor_user_id IS DISTINCT FROM (c->>'user')::uuid
    OR r.actor_role IS DISTINCT FROM c->>'role' THEN RAISE EXCEPTION 'generation operation conflict'; END IF;
   SELECT * INTO j FROM public.billing_header_generations WHERE practice_id=p AND job_id=r.job_id;
  ELSIF a='status' THEN
   SELECT * INTO op FROM public.billing_header_operations WHERE practice_id=p AND operation_id=(c->>'operation')::uuid;
   IF FOUND THEN
    IF op.actor_tenant_id IS DISTINCT FROM (c->>'tenant')::uuid OR op.actor_user_id IS DISTINCT FROM (c->>'user')::uuid
     OR op.actor_role IS DISTINCT FROM c->>'role' OR NOT EXISTS(SELECT 1 FROM public.billing_headers
      WHERE practice_id=p AND header_id=op.header_id AND encounter_uid=e.encounter_uid AND source_sha256=c->>'sourceSha')
     THEN RAISE EXCEPTION 'generation operation conflict'; END IF;
    RETURN jsonb_build_object('status','succeeded');
   END IF;
   RETURN jsonb_build_object('status','unknown');
  ELSE
   SELECT * INTO j FROM public.billing_header_generations WHERE practice_id=p AND encounter_uid=e.encounter_uid AND encounter_version=e.version;
   IF NOT FOUND THEN
    SELECT header_id INTO h FROM public.billing_headers WHERE practice_id=p AND encounter_uid=e.encounter_uid AND encounter_version=e.version;
    IF FOUND THEN RETURN jsonb_build_object('status','succeeded'); END IF;
    d:=LEAST(COALESCE((c->>'deadline')::timestamptz,clock_timestamp()+interval '360 seconds'),clock_timestamp()+interval '360 seconds');
    IF d<=clock_timestamp()
     OR (SELECT count(*) FROM public.billing_header_generations WHERE practice_id=p AND status='running' AND deadline>clock_timestamp())>=2
     THEN RAISE EXCEPTION 'generation capacity unavailable'; END IF;
    IF NOT EXISTS(SELECT 1 FROM public.access_audit_attempts WHERE practice_id=p AND event_id=(c->>'audit')::uuid
      AND actor_tenant_id=(c->>'tenant')::uuid AND actor_user_id=(c->>'user')::uuid
      AND c->>'role'=ANY(actor_roles) AND action='billing_header.compute' AND method='POST')
     THEN RAISE EXCEPTION 'generation audit required'; END IF;
    INSERT INTO public.billing_header_generations(practice_id,job_id,encounter_uid,encounter_version,source,source_sha,owner_id,
      actor_tenant_id,actor_user_id,actor_role,operation_id,audit_event_id,deadline)
     VALUES(p,(c->>'job')::uuid,e.encounter_uid,e.version,c->'source',c->>'sourceSha',(c->>'owner')::uuid,
      (c->>'tenant')::uuid,(c->>'user')::uuid,c->>'role',(c->>'operation')::uuid,(c->>'audit')::uuid,d)
     RETURNING * INTO j;
    fresh:=true;
   END IF;
   INSERT INTO public.billing_header_generation_requests VALUES(p,(c->>'operation')::uuid,j.job_id,
    (c->>'tenant')::uuid,(c->>'user')::uuid,c->>'role');
  END IF;
 ELSE
  SELECT * INTO j FROM public.billing_header_generations WHERE practice_id=p AND job_id=(c->>'job')::uuid FOR UPDATE;
  IF NOT FOUND OR j.owner_id IS DISTINCT FROM (c->>'owner')::uuid THEN RAISE EXCEPTION 'generation owner mismatch'; END IF;
 END IF;
 IF a<>'finish' AND (j.encounter_uid IS DISTINCT FROM e.encounter_uid OR j.source IS DISTINCT FROM c->'source'
   OR j.source_sha IS DISTINCT FROM c->>'sourceSha') THEN RAISE EXCEPTION 'generation operation conflict'; END IF;
 live:=j.status='running' AND j.deadline>clock_timestamp();
 IF a IN ('check','start') THEN
  IF NOT live THEN RAISE EXCEPTION 'generation no longer running'; END IF;
  IF a='start' THEN
   IF j.started THEN RAISE EXCEPTION 'generation already started'; END IF;
   UPDATE public.billing_header_generations SET started=true WHERE practice_id=p AND job_id=j.job_id;
  END IF;
 ELSIF a='finish' AND j.status='running' THEN
  IF c->>'status'='succeeded' THEN
   IF NOT live OR NOT EXISTS(SELECT 1 FROM public.billing_headers h
      JOIN public.billing_header_operations o ON o.practice_id=h.practice_id AND o.header_id=h.header_id
      JOIN public.access_audit_completions ac ON ac.practice_id=o.practice_id AND ac.event_id=o.access_event_id
      WHERE h.practice_id=p AND h.header_id=(c->>'header')::uuid AND h.encounter_uid=j.encounter_uid
      AND h.source_sha256=j.source_sha AND o.operation_id=j.operation_id AND o.access_event_id=j.audit_event_id
      AND ac.outcome='allowed' AND ac.xmin=pg_current_xact_id()::xid)
    THEN RAISE EXCEPTION 'generation success requires atomic header audit'; END IF;
  ELSIF c->>'status' NOT IN ('failed','unknown') OR c->>'status'='failed' AND j.started
   THEN RAISE EXCEPTION 'invalid generation terminal state'; END IF;
  IF c->>'status'<>'succeeded' THEN
   INSERT INTO public.access_audit_completions(practice_id,event_id,completed_at,status,outcome,resource_id)
    VALUES(p,j.audit_event_id,clock_timestamp(),503,'error',j.encounter_uid::text) ON CONFLICT DO NOTHING;
  END IF;
  UPDATE public.billing_header_generations SET status=c->>'status',header_id=(c->>'header')::uuid,
   created=COALESCE((c->>'created')::boolean,false) WHERE practice_id=p AND job_id=j.job_id RETURNING * INTO j;
 END IF;
 RETURN jsonb_build_object('status',CASE WHEN j.status='running' AND NOT live THEN 'unknown' ELSE j.status END,
   'job',j.job_id,'deadline',j.deadline,'remaining',GREATEST(0,extract(epoch FROM j.deadline-clock_timestamp())),
   'new',fresh,'started',j.started);
END $$;
CREATE FUNCTION public.cosmaneura_generation_commit_deadline() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$ BEGIN
 IF NEW.status='succeeded' AND NEW.deadline<=clock_timestamp() THEN
  RAISE EXCEPTION 'generation deadline expired before commit';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER generation_commit_deadline AFTER UPDATE ON public.billing_header_generations
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_generation_commit_deadline();
REVOKE ALL ON FUNCTION public.cosmaneura_generation_commit_deadline() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cosmaneura_header_generation(jsonb),public.cosmaneura_generation_immutable() FROM PUBLIC;
COMMIT;
