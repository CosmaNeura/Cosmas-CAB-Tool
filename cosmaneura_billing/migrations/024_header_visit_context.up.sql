-- Add only optional source-owned serviceDate to the existing visit manifest.
-- The public020 admission wrapper, ACLs, hashes and stored receipts are retained.
BEGIN;
CREATE OR REPLACE FUNCTION public.cosmaneura_commit_provider_handoff_019(command jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE p text:=command->>'practice'; k text:=command->>'recordKey';
 r jsonb:=command->'receipt'; b jsonb:=command->'bundle'; actor jsonb:=command->'actor';
 h public.provider_handoff_heads%ROWTYPE; e public.encounters%ROWTYPE;
 revision bigint:=(command->>'sourceRevision')::bigint; v bigint:=(r->>'encounterVersion')::bigint;
 uid uuid:=(command->>'resourceId')::uuid; op uuid:=(r->>'operationId')::uuid;
 fixture boolean:=(command->>'fixture')::boolean; actual_bundle jsonb; m jsonb:=command->'manifest';
 doc jsonb; seen text[]:=ARRAY[]::text[]; ids text[]; expected_visit jsonb;
BEGIN
 IF p IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
    OR NOT public.cosmaneura_practice_allowed(p)
    OR actor->>'role' NOT IN ('Provider','Admin')
    OR NOT public.cosmaneura_billing_header_editor_allowed((actor->>'tenant_id')::uuid,(actor->>'user_id')::uuid,actor->>'role')
    OR fixture IS NULL OR revision NOT BETWEEN 1 AND 9007199254740991 THEN
   RAISE EXCEPTION 'provider receiver authority unavailable'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('provider-handoff:'||p||':operation:'||op,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('provider-handoff:'||p||':record:'||k,0));
 IF EXISTS(SELECT 1 FROM public.provider_handoff_operations WHERE practice_id=p AND operation_id=op) THEN
   RAISE EXCEPTION 'provider operation already recorded'; END IF;
 SELECT * INTO h FROM public.provider_handoff_heads WHERE practice_id=p AND record_key=k FOR UPDATE;
 SELECT * INTO e FROM public.encounters WHERE practice_id=p AND external_id=b->>'encounter_id' FOR UPDATE;
 IF r->>'encounterId' IS DISTINCT FROM b->>'encounter_id' OR r->>'sourceRevision' IS DISTINCT FROM revision::text
    OR r->>'sourceManifestSha256' IS DISTINCT FROM command->>'manifestSha256'
    OR command->>'manifestSha256' IS DISTINCT FROM encode(sha256(convert_to(public.cosmaneura_handoff_canonical(command->'manifest'),'UTF8')),'hex')
    OR b->'submitted_em_code' IS DISTINCT FROM 'null'::jsonb OR b->'submitted_icd10' IS DISTINCT FROM '[]'::jsonb THEN
   RAISE EXCEPTION 'invalid provider source receipt'; END IF;
 IF h.record_key IS NOT NULL THEN
   IF e.encounter_uid IS DISTINCT FROM h.encounter_uid OR e.version::text IS DISTINCT FROM h.header_source->>'encounterVersion'
      OR public.cosmaneura_handoff_canonical(public.cosmaneura_handoff_source(e)) IS DISTINCT FROM public.cosmaneura_handoff_canonical(h.header_source)
      OR public.cosmaneura_handoff_canonical(command->'expectedSource') IS DISTINCT FROM public.cosmaneura_handoff_canonical(h.header_source) OR revision<h.source_revision
      OR (revision=h.source_revision AND command->>'manifestSha256' IS DISTINCT FROM h.manifest_sha256) THEN
     RAISE EXCEPTION 'provider source changed'; END IF;
 ELSIF command->'expectedSource' IS DISTINCT FROM 'null'::jsonb THEN
   RAISE EXCEPTION 'initial provider source conflict';
 END IF;
 IF fixture THEN
   IF m IS DISTINCT FROM jsonb_build_object('fixtureKey',b->>'encounter_id',
        'bundleSha256',encode(sha256(convert_to(public.cosmaneura_handoff_canonical(b),'UTF8')),'hex'),
        'completion','completed synthetic fixture convention','source','synthetic_demo_release_candidate')
      OR k IS DISTINCT FROM 'fixture:'||(b->>'encounter_id') OR e.source IS DISTINCT FROM 'synthetic_demo_release_candidate'
      OR uid IS DISTINCT FROM e.encounter_uid OR v IS DISTINCT FROM e.version OR revision<>1
      OR r->'headerSource'->>'ingestionSource' IS DISTINCT FROM e.source
      OR b IS DISTINCT FROM jsonb_build_object('encounter_id',e.external_id,'patient_age',e.patient_age,
          'patient_sex',e.patient_sex,'encounter_type',e.encounter_type,'place_of_service',e.place_of_service,
          'provider_specialty',e.provider_specialty,'signed_note',e.signed_note,'transcript',e.transcript,
          'intake_text',e.intake_text,'submitted_em_code',e.submitted_em_code,'submitted_icd10',to_jsonb(e.submitted_icd10)) THEN
     RAISE EXCEPTION 'fixed provider fixture unavailable'; END IF;
 ELSE
   IF k IS DISTINCT FROM b->>'encounter_id' OR k !~ '^source-encounter-v1:[a-f0-9]{64}$'
      OR r->'headerSource'->>'ingestionSource' IS DISTINCT FROM 'provider_completed_note'
      OR (h.record_key IS NULL AND e.encounter_uid IS NOT NULL)
      OR (h.record_key IS NOT NULL AND e.source IS DISTINCT FROM 'provider_completed_note') THEN
     RAISE EXCEPTION 'provider source identity conflict'; END IF;
   expected_visit:=jsonb_build_object('patientAge',b->'patient_age',
     'patientSex',CASE b->>'patient_sex' WHEN 'male' THEN 'M' WHEN 'female' THEN 'F' ELSE b->>'patient_sex' END,
     'encounterType',b->>'encounter_type','placeOfService',b->>'place_of_service','providerSpecialty',b->>'provider_specialty');
   IF m - ARRAY['schemaVersion','sourceRecordId','sourceRevision','completion','note','supportingDocuments','visit','versionKey'] IS DISTINCT FROM '{}'::jsonb
      OR m->>'schemaVersion' IS DISTINCT FROM 'cab.completed-note-handoff.v1'
      OR COALESCE(m->>'sourceRecordId','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
      OR (m->'completion') - ARRAY['status','confirmedByProvider','completedAt'] IS DISTINCT FROM '{}'::jsonb
      OR (m->'note') - ARRAY['id','version','sha256'] IS DISTINCT FROM '{}'::jsonb
      OR m->>'sourceRevision' IS DISTINCT FROM revision::text
      OR k IS DISTINCT FROM 'source-encounter-v1:'||encode(sha256(convert_to(array_to_json(ARRAY[p,'provider_platform','encounters',m->>'sourceRecordId'])::text,'UTF8')),'hex')
      OR m->>'versionKey' IS DISTINCT FROM 'source-import-v1:'||encode(sha256(convert_to(array_to_json(ARRAY[p,'provider_platform','encounters',m->>'sourceRecordId',revision::text,'provider-handoff.v1'])::text,'UTF8')),'hex')
      OR public.cosmaneura_handoff_canonical((m->'visit')-'serviceDate') IS DISTINCT FROM public.cosmaneura_handoff_canonical(expected_visit)
      OR m->'completion'->'confirmedByProvider' IS DISTINCT FROM 'true'::jsonb
      OR COALESCE(m->'completion'->>'status','') NOT IN ('completed','signed')
      OR COALESCE(m->'completion'->>'completedAt','') !~ '^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z|[+-]([01]\d|2[0-3]):[0-5]\d)$'
      OR m->'note'->>'sha256' IS DISTINCT FROM encode(sha256(convert_to(b->>'signed_note','UTF8')),'hex')
      OR COALESCE(m->'note'->>'id','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
      OR COALESCE(m->'note'->>'version','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
      OR jsonb_typeof(m->'supportingDocuments') IS DISTINCT FROM 'array' THEN
     RAISE EXCEPTION 'provider manifest does not bind source'; END IF;
   IF (m->'visit') ? 'serviceDate' THEN
     IF jsonb_typeof(m->'visit'->'serviceDate') IS DISTINCT FROM 'string'
        OR m->'visit'->>'serviceDate' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
       RAISE EXCEPTION 'provider service date must be an ISO calendar date'; END IF;
     PERFORM (m->'visit'->>'serviceDate')::date;
   END IF;
   PERFORM (m->'completion'->>'completedAt')::timestamptz;
   IF jsonb_array_length(m->'supportingDocuments')>2 THEN RAISE EXCEPTION 'provider document bounds'; END IF;
   ids:=ARRAY[m->'note'->>'id'];
   FOR doc IN SELECT value FROM jsonb_array_elements(m->'supportingDocuments') LOOP
     IF doc - ARRAY['kind','id','version','sha256'] IS DISTINCT FROM '{}'::jsonb
        OR COALESCE(doc->>'kind','') NOT IN ('transcript','intake') OR doc->>'kind'=ANY(seen)
        OR doc->>'id'=ANY(ids) OR COALESCE(doc->>'id','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        OR COALESCE(doc->>'version','') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
        OR doc->>'sha256' IS DISTINCT FROM encode(sha256(convert_to(b->>CASE WHEN doc->>'kind'='intake' THEN 'intake_text' ELSE 'transcript' END,'UTF8')),'hex') THEN
       RAISE EXCEPTION 'provider supporting manifest mismatch'; END IF;
     seen:=array_append(seen,doc->>'kind'); ids:=array_append(ids,doc->>'id');
   END LOOP;
   IF (b->>'transcript'<>'') IS DISTINCT FROM ('transcript'=ANY(seen))
      OR (b->>'intake_text'<>'') IS DISTINCT FROM ('intake'=ANY(seen)) THEN
     RAISE EXCEPTION 'provider supporting source omitted'; END IF;
   IF h.record_key IS NULL OR revision>h.source_revision THEN
     IF v<>COALESCE(e.version,0)+1 OR (e.encounter_uid IS NOT NULL AND uid<>e.encounter_uid) THEN
       RAISE EXCEPTION 'provider version conflict'; END IF;
     INSERT INTO public.encounters(practice_id,encounter_uid,external_id,patient_age,patient_sex,encounter_type,
       place_of_service,provider_specialty,signed_note,transcript,intake_text,submitted_em_code,submitted_icd10,
       source,version,ingested_by_tenant_id,ingested_by_user_id)
     VALUES(p,uid,k,(b->>'patient_age')::int,b->>'patient_sex',b->>'encounter_type',b->>'place_of_service',
       b->>'provider_specialty',b->>'signed_note',b->>'transcript',b->>'intake_text',NULL,ARRAY[]::text[],
       'provider_completed_note',v,(actor->>'tenant_id')::uuid,(actor->>'user_id')::uuid)
     ON CONFLICT(practice_id,external_id) DO UPDATE SET patient_age=EXCLUDED.patient_age,patient_sex=EXCLUDED.patient_sex,
       encounter_type=EXCLUDED.encounter_type,place_of_service=EXCLUDED.place_of_service,provider_specialty=EXCLUDED.provider_specialty,
       signed_note=EXCLUDED.signed_note,transcript=EXCLUDED.transcript,intake_text=EXCLUDED.intake_text,version=EXCLUDED.version,
       ingested_by_tenant_id=EXCLUDED.ingested_by_tenant_id,ingested_by_user_id=EXCLUDED.ingested_by_user_id,updated_at=now();
   ELSIF uid IS DISTINCT FROM e.encounter_uid OR v IS DISTINCT FROM e.version OR r->'headerSource' IS DISTINCT FROM h.header_source THEN
     RAISE EXCEPTION 'same revision must preserve source'; END IF;
 END IF;
 SELECT * INTO e FROM public.encounters WHERE practice_id=p AND encounter_uid=uid;
 actual_bundle:=jsonb_build_object('encounter_id',e.external_id,'patient_age',e.patient_age,
   'patient_sex',e.patient_sex,'encounter_type',e.encounter_type,'place_of_service',e.place_of_service,
   'provider_specialty',e.provider_specialty,'signed_note',e.signed_note,'transcript',e.transcript,
   'intake_text',e.intake_text,'submitted_em_code',e.submitted_em_code,'submitted_icd10',to_jsonb(e.submitted_icd10));
 IF e.encounter_uid IS NULL OR actual_bundle IS DISTINCT FROM b OR e.version IS DISTINCT FROM v THEN
   RAISE EXCEPTION 'persisted provider source mismatch'; END IF;
 b:=actual_bundle;
 IF public.cosmaneura_handoff_canonical(r->'headerSource') IS DISTINCT FROM
    public.cosmaneura_handoff_canonical(public.cosmaneura_handoff_source(e)) THEN
   RAISE EXCEPTION 'persisted provider source pin mismatch'; END IF;
 -- Completion and reservation must belong to this actor and this transaction.
 IF NOT EXISTS(SELECT 1 FROM public.access_audit_completions c JOIN public.access_audit_attempts a USING(practice_id,event_id)
   WHERE c.practice_id=p AND c.event_id=(command->>'eventId')::uuid AND c.xmin=pg_current_xact_id()::xid
     AND c.resource_id=uid::text AND c.status=200 AND c.outcome='allowed' AND a.method='POST'
     AND a.actor_tenant_id=(actor->>'tenant_id')::uuid AND a.actor_user_id=(actor->>'user_id')::uuid
     AND actor->>'role'=ANY(a.actor_roles)
     AND a.action=CASE WHEN fixture THEN 'provider_handoff.fixture' ELSE 'provider_handoff.accept' END) THEN
   RAISE EXCEPTION 'atomic provider audit required'; END IF;
 INSERT INTO public.provider_handoff_heads VALUES(p,k,revision,command->>'manifestSha256',command->'manifest',uid,r->'headerSource')
 ON CONFLICT(practice_id,record_key) DO UPDATE SET source_revision=EXCLUDED.source_revision,
   manifest_sha256=EXCLUDED.manifest_sha256,manifest=EXCLUDED.manifest,header_source=EXCLUDED.header_source;
 INSERT INTO public.provider_handoff_operations VALUES(p,op,k,command->>'requestSha256',r,
   (actor->>'tenant_id')::uuid,(actor->>'user_id')::uuid,actor->>'role',(command->>'eventId')::uuid);
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_commit_provider_handoff_019(jsonb) FROM PUBLIC;
COMMIT;
