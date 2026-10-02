-- Practice-wide source review; no source mutation, dispatch or consent authority.
BEGIN;
ALTER TABLE chase_communication_event_reviews ALTER COLUMN case_id DROP NOT NULL;
ALTER TABLE chase_communication_event_reviews ADD CONSTRAINT communication_review_null_scope
    CHECK (case_id IS NOT NULL OR (decision='leave_unassigned' AND contact_snapshot_id IS NULL));
CREATE UNIQUE INDEX communication_review_one_global_completion
    ON chase_communication_event_reviews(practice_id,source_ref,source_hash) WHERE case_id IS NULL;

CREATE VIEW chase_communication_unassigned_sources WITH (security_invoker=true) AS
WITH inbound AS (
    SELECT practice_id,'provider'::text AS source_kind,account_sid,event_id AS source_id,body_hash AS source_hash,
        text,status,disposition,received_at AS at,media_count,sender,recipient
    FROM chase_communication_provider_events WHERE kind='sms_inbound' AND case_id IS NULL
    UNION ALL
    SELECT practice_id,'legacy',account_sid,event_id,body_hash,text,status,disposition,occurred_at,0,sender,recipient
    FROM chase_communication_events WHERE kind='inbound_sms' AND case_id IS NULL
), referenced AS (
    SELECT e.*,encode(sha256(convert_to(source_kind || ':' || account_sid || ':' || source_id,'UTF8')),'hex') AS source_ref
    FROM inbound e
)
SELECT e.*,a.case_id AS attributed_case_id,
    CASE WHEN a.action_id IS NOT NULL THEN 'attributed' WHEN d.action_id IS NOT NULL THEN 'left_unassigned' ELSE 'pending' END AS review_state,
    COALESCE(a.created_at,d.created_at) AS reviewed_at
FROM referenced e
LEFT JOIN chase_communication_event_reviews a ON a.practice_id=e.practice_id AND a.source_ref=e.source_ref
    AND a.source_hash=e.source_hash AND a.decision='attribute'
LEFT JOIN chase_communication_event_reviews d ON d.practice_id=e.practice_id AND d.source_ref=e.source_ref
    AND d.source_hash=e.source_hash AND d.case_id IS NULL AND d.decision='leave_unassigned';
REVOKE ALL ON chase_communication_unassigned_sources FROM PUBLIC;

CREATE OR REPLACE VIEW chase_communication_review_sources WITH (security_invoker=true) AS
WITH events AS (
    SELECT e.practice_id,'provider'::text AS source_kind,e.account_sid,e.event_id AS source_id,
        e.body_hash AS source_hash,e.channel,e.text,e.status,e.disposition,e.received_at AS at,e.media_count,
        e.sender,e.recipient,COALESCE(e.case_id,o.case_id) AS original_case_id,r.operation_id,
        CASE WHEN e.kind='sms_inbound' THEN 'sms_reply' ELSE 'call_keypad' END AS kind
    FROM chase_communication_provider_events e
    LEFT JOIN chase_communication_dispatch_receipts r ON r.practice_id=e.practice_id
        AND r.account_sid=e.account_sid AND r.provider_sid=e.provider_sid AND e.kind='voice_gather'
    LEFT JOIN chase_communication_operations o ON o.practice_id=r.practice_id AND o.operation_id=r.operation_id
    LEFT JOIN chase_communication_bindings b ON b.practice_id=o.practice_id AND b.binding_id=o.binding_id
    WHERE e.kind='sms_inbound' OR (e.kind='voice_gather' AND e.sender=b.sender AND e.recipient=b.phone)
    UNION ALL
    SELECT practice_id,'legacy',account_sid,event_id,body_hash,channel,text,status,disposition,
        occurred_at,0,sender,recipient,case_id,operation_id,'sms_reply'
    FROM chase_communication_events WHERE kind='inbound_sms'
), referenced AS (
    SELECT e.*,encode(sha256(convert_to(source_kind || ':' || account_sid || ':' || source_id,'UTF8')),'hex') AS source_ref
    FROM events e
), event_cases AS (
    SELECT e.*,c.case_id,(e.original_case_id IS NULL AND a.case_id IS NULL) AS held,
        a.case_id IS NOT NULL AS attributed
    FROM referenced e
    LEFT JOIN chase_communication_event_reviews a ON a.practice_id=e.practice_id AND a.source_ref=e.source_ref AND a.decision='attribute'
    CROSS JOIN LATERAL (
        SELECT COALESCE(e.original_case_id,a.case_id) AS case_id WHERE COALESCE(e.original_case_id,a.case_id) IS NOT NULL
        UNION
        SELECT b.case_id FROM chase_communication_bindings b
        WHERE e.original_case_id IS NULL AND a.case_id IS NULL AND e.kind='sms_reply'
          AND b.practice_id=e.practice_id AND b.channel='sms' AND b.account_sid=e.account_sid
          AND b.sender=e.recipient AND b.phone=e.sender
    ) c
), operation_states AS (
    SELECT o.*,r.received_at,
        CASE WHEN r.outcome='rejected' THEN 'rejected' ELSE state.status END AS current_status,
        COALESCE(state.sequence_no,0) AS status_sequence
    FROM chase_communication_operations o
    JOIN chase_communication_dispatch_receipts r USING(practice_id,operation_id)
    JOIN chase_communication_bindings b ON b.practice_id=o.practice_id AND b.binding_id=o.binding_id
    LEFT JOIN LATERAL (
        SELECT status,sequence_no FROM (
            SELECT r.status,0::bigint AS sequence_no
            UNION ALL
            SELECT e.status,e.sequence_no FROM chase_communication_provider_events e
            WHERE e.practice_id=r.practice_id AND e.account_sid=r.account_sid AND e.provider_sid=r.provider_sid
              AND e.kind IN ('sms_status','voice_status')
              AND (e.sender IS NULL OR e.sender=b.sender) AND (e.recipient IS NULL OR e.recipient=b.phone)
        ) choices
        ORDER BY CASE
            WHEN status='read' THEN 6 WHEN status IN ('delivered','completed') THEN 5
            WHEN status IN ('failed','undelivered','busy','no-answer','canceled') THEN 4
            WHEN status IN ('sent','in-progress') THEN 3 WHEN status IN ('sending','ringing') THEN 2
            WHEN status='initiated' OR (o.channel='sms' AND status='queued') THEN 1 ELSE 0 END DESC,sequence_no DESC
        LIMIT 1
    ) state ON r.outcome='accepted'
    WHERE r.outcome IN ('accepted','rejected')
), sources AS (
    SELECT practice_id,source_ref,source_hash,source_kind,source_id,account_sid,case_id,operation_id,
        kind,channel,text,status,disposition,at,media_count,sender,recipient,held,attributed
    FROM event_cases
    UNION ALL
    SELECT practice_id,encode(sha256(convert_to('operation:' || operation_id::text,'UTF8')),'hex'),
        encode(sha256(convert_to(body_hash || ':' || current_status || ':' || status_sequence::text,'UTF8')),'hex'),
        'operation',operation_id::text,NULL,case_id,operation_id,'delivery_failure',channel,'',current_status,NULL,
        received_at,0,NULL,NULL,false,false
    FROM operation_states WHERE current_status IN ('failed','undelivered','rejected','busy','no-answer','canceled')
)
SELECT s.*,d.decision AS completed_decision,d.created_at AS reviewed_at,
    CASE WHEN s.held AND route.destination=s.sender THEN snap.contact_snapshot_id ELSE NULL END AS attributable_snapshot_id
FROM sources s
LEFT JOIN LATERAL (SELECT decision,created_at FROM chase_communication_event_reviews d
    WHERE d.practice_id=s.practice_id AND (d.case_id=s.case_id OR d.case_id IS NULL)
      AND d.source_ref=s.source_ref AND d.source_hash=s.source_hash AND d.decision IN ('handled','leave_unassigned')
    ORDER BY (d.case_id IS NULL) DESC,d.created_at DESC LIMIT 1) d ON true
LEFT JOIN LATERAL (SELECT contact_snapshot_id FROM chase_contact_snapshots c
    WHERE c.practice_id=s.practice_id AND c.chase_id=s.case_id
    ORDER BY c.verified_at DESC,c.created_at DESC,c.contact_snapshot_id DESC LIMIT 1) snap ON s.held
LEFT JOIN chase_contact_routes route ON route.practice_id=s.practice_id AND route.contact_snapshot_id=snap.contact_snapshot_id AND route.channel='sms';

CREATE OR REPLACE FUNCTION cosmaneura_validate_communication_review() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE source record; inbox record; snapshot uuid;
BEGIN
    IF public.cosmaneura_practice_allowed(NEW.practice_id) IS NOT TRUE
       OR public.cosmaneura_staff_assignee_allowed(NEW.actor_tenant_id,NEW.actor_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'communications staff unavailable' USING ERRCODE='insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('communications-review:' || NEW.practice_id || ':' || NEW.source_ref,0));
    IF NEW.case_id IS NULL THEN
        SELECT * INTO inbox FROM public.chase_communication_unassigned_sources
            WHERE practice_id=NEW.practice_id AND source_ref=NEW.source_ref AND source_hash=NEW.source_hash AND review_state='pending';
        IF NOT FOUND OR NEW.decision<>'leave_unassigned' OR NEW.contact_snapshot_id IS NOT NULL THEN
            RAISE EXCEPTION 'communications review source stale' USING ERRCODE='check_violation';
        END IF;
        RETURN NEW;
    END IF;
    PERFORM 1 FROM public.chase_items WHERE practice_id=NEW.practice_id AND chase_id=NEW.case_id FOR UPDATE;
    SELECT * INTO source FROM public.chase_communication_review_sources
        WHERE practice_id=NEW.practice_id AND case_id=NEW.case_id AND source_ref=NEW.source_ref AND source_hash=NEW.source_hash;
    IF FOUND THEN
        IF source.completed_decision IS NOT NULL OR (NEW.decision='handled' AND source.held)
           OR (NEW.decision IN ('attribute','leave_unassigned') AND NOT source.held)
           OR (NEW.decision='attribute' AND (source.attributable_snapshot_id IS NULL
               OR source.attributable_snapshot_id<>NEW.contact_snapshot_id)) THEN
            RAISE EXCEPTION 'communications review evidence mismatch' USING ERRCODE='check_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- New case attribution from the inbox requires current contact evidence;
    -- a previous outbound binding is not patient identity or consent evidence.
    SELECT * INTO inbox FROM public.chase_communication_unassigned_sources
        WHERE practice_id=NEW.practice_id AND source_ref=NEW.source_ref AND source_hash=NEW.source_hash AND review_state='pending';
    IF NOT FOUND OR NEW.decision<>'attribute' THEN
        RAISE EXCEPTION 'communications review source stale' USING ERRCODE='check_violation';
    END IF;
    SELECT c.contact_snapshot_id INTO snapshot FROM public.chase_contact_snapshots c
        WHERE c.practice_id=NEW.practice_id AND c.chase_id=NEW.case_id
        ORDER BY c.verified_at DESC,c.created_at DESC,c.contact_snapshot_id DESC LIMIT 1;
    IF snapshot IS NULL OR snapshot<>NEW.contact_snapshot_id OR NOT EXISTS (
        SELECT 1 FROM public.chase_contact_routes r WHERE r.practice_id=NEW.practice_id
            AND r.contact_snapshot_id=snapshot AND r.channel='sms' AND r.destination=inbox.sender) THEN
        RAISE EXCEPTION 'communications review evidence mismatch' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
COMMIT;
