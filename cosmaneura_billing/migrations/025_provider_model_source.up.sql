-- Read existing accepted provider provenance without granting registry UPDATE.
-- The caller must separately hold model-service authority; this function does
-- not authorize network/model use. Locks live only in the caller transaction.
BEGIN;
CREATE FUNCTION public.cosmaneura_provider_model_source(actor_tenant uuid,actor_user uuid,actor_role text,encounter_id text)
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
    OR c.document->>'sourceMode' IS DISTINCT FROM 'cab_provider_attested'
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
      'connectionVersion',c.document->'version','dataClass',c.document->'dataClass'));
END $$;
REVOKE ALL ON FUNCTION public.cosmaneura_provider_model_source(uuid,uuid,text,text) FROM PUBLIC;
COMMIT;
