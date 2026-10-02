-- Configured Preview review receipts. No production admission or new grants.
BEGIN;
ALTER TABLE public.athena_case_review_events DROP CONSTRAINT athena_case_review_events_check;
ALTER TABLE public.athena_case_review_events ADD CONSTRAINT athena_case_review_events_check CHECK(
 jsonb_typeof(payload)='object' AND payload->>'schemaVersion' IN ('athena-case-review.v1','athena-case-review.v2')
 AND octet_length(payload::text)<=1048576 AND (payload->>'revision')::integer=revision);
CREATE FUNCTION public.cosmaneura_guard_configured_athena_review() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE scope jsonb; binding jsonb; prior jsonb; candidate jsonb; expected_case text; history jsonb;
BEGIN
 IF current_setting('transaction_isolation')<>'read committed'
 THEN RAISE EXCEPTION 'Athena review requires current transaction scope'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('athena-review-case:'||NEW.practice_id||':'||NEW.chase_id,0));
 SELECT payload INTO prior FROM public.athena_case_review_events
 WHERE practice_id=NEW.practice_id AND chase_id=NEW.chase_id ORDER BY revision DESC LIMIT 1;
 IF NEW.payload->>'schemaVersion'='athena-case-review.v1' THEN
  IF prior->>'schemaVersion'='athena-case-review.v2'
  THEN RAISE EXCEPTION 'Athena configured review cannot downgrade'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.payload->>'schemaVersion' IS DISTINCT FROM 'athena-case-review.v2'
 OR NEW.practice_id IS DISTINCT FROM NULLIF(current_setting('cosmaneura.practice_id',true),'')
 OR jsonb_typeof(NEW.payload->'revision') IS DISTINCT FROM 'number'
 OR NEW.payload->>'revision' IS DISTINCT FROM NEW.revision::text
 THEN RAISE EXCEPTION 'Athena configured review scope invalid'; END IF;
 scope:=public.cosmaneura_athena_staff_scope(NEW.actor_tenant_id,NEW.actor_user_id,NEW.actor_role,NEW.chase_id);
 IF jsonb_typeof(scope) IS DISTINCT FROM 'object' OR scope ? 'error'
 OR scope->>'environment' IS DISTINCT FROM 'preview' OR scope->>'vendorPracticeId' IS DISTINCT FROM '195900'
 OR COALESCE(scope->>'sourcePatientId','') NOT IN ('60178','60179','60180','60181','60182','60183','60184')
 THEN RAISE EXCEPTION 'Athena configured read is not admitted'; END IF;
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
 OR NEW.payload->>'mode' IS DISTINCT FROM 'preview'
 OR candidate->'source'->>'system' IS DISTINCT FROM 'athena_preview'
 OR candidate->'source'->>'vendorPracticeId' IS DISTINCT FROM '195900'
 OR candidate->'source'->>'serverPracticeId' IS DISTINCT FROM NEW.practice_id
 OR candidate->'source'->'syntheticOnly' IS DISTINCT FROM 'true'::jsonb
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
  IF prior->>'schemaVersion' IS DISTINCT FROM 'athena-case-review.v2'
  OR prior->'sourceBinding' IS DISTINCT FROM binding OR NEW.payload->'candidate' IS DISTINCT FROM prior->'candidate'
  OR NEW.payload->'reviewId' IS DISTINCT FROM prior->'reviewId'
  THEN RAISE EXCEPTION 'Athena configured decision requires current review'; END IF;
  IF NEW.action='athena.apply' THEN
   IF NEW.payload->'applied'->'sourceBinding' IS DISTINCT FROM binding
   OR NEW.payload->'applied'->'patient' IS DISTINCT FROM candidate->'patient'
   OR NEW.payload->'applied'->'sourceDigest' IS DISTINCT FROM candidate->'sourceDigest'
   OR NEW.payload->'applied'->'reviewId' IS DISTINCT FROM NEW.payload->'reviewId'
   OR NEW.payload->'applied'->>'operationId' IS DISTINCT FROM NEW.operation_id::text
   OR NEW.payload->'billingImport' IS DISTINCT FROM prior->'billingImport'
   THEN RAISE EXCEPTION 'Athena applied evidence binding mismatch'; END IF;
  ELSIF NEW.action='athena.billing_apply' THEN
   IF NEW.payload->'applied' IS DISTINCT FROM prior->'applied'
   OR NEW.payload->'billingImport'->'sourceBinding' IS DISTINCT FROM binding
   OR NEW.payload->'billingImport'->'sourcePatientId' IS DISTINCT FROM binding->'sourcePatientId'
   OR NEW.payload->'billingImport'->>'source' IS DISTINCT FROM 'athena_staff_review_preview_synthetic'
   OR NEW.payload->'billingImport'->>'encounterId' IS DISTINCT FROM 'athena-review-'||NEW.operation_id::text
   THEN RAISE EXCEPTION 'Athena imported evidence binding mismatch'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_guard_configured_athena_review() FROM PUBLIC;
CREATE TRIGGER athena_configured_review_guard BEFORE INSERT ON public.athena_case_review_events
 FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_configured_athena_review();
ALTER TABLE public.athena_case_review_events ENABLE ALWAYS TRIGGER athena_configured_review_guard;
COMMIT;
