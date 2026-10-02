-- Append-only staff handling and explicit attribution; never sends or clears STOP.
BEGIN;
CREATE TABLE chase_communication_event_reviews (
    practice_id text NOT NULL,
    action_id uuid NOT NULL,
    case_id text NOT NULL,
    source_ref text NOT NULL CHECK (source_ref ~ '^[0-9a-f]{64}$'),
    source_hash text NOT NULL CHECK (source_hash ~ '^[0-9a-f]{64}$'),
    decision text NOT NULL CHECK (decision IN ('handled','attribute','leave_unassigned')),
    contact_snapshot_id uuid,
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    body_hash text NOT NULL CHECK (body_hash ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,action_id),
    UNIQUE (practice_id,case_id,source_ref,source_hash,decision),
    FOREIGN KEY (practice_id,case_id) REFERENCES chase_items(practice_id,chase_id),
    FOREIGN KEY (practice_id,contact_snapshot_id,case_id)
        REFERENCES chase_contact_snapshots(practice_id,contact_snapshot_id,chase_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id),
    CHECK ((decision='attribute') = (contact_snapshot_id IS NOT NULL))
);
CREATE UNIQUE INDEX communication_review_one_attribution
    ON chase_communication_event_reviews(practice_id,source_ref) WHERE decision='attribute';
REVOKE ALL ON chase_communication_event_reviews FROM PUBLIC;
CREATE POLICY practice_isolation ON chase_communication_event_reviews
    USING (cosmaneura_practice_allowed(practice_id)) WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_communication_event_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_communication_event_reviews FORCE ROW LEVEL SECURITY;
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chase_communication_event_reviews
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chase_communication_event_reviews
    FOR EACH STATEMENT EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
ALTER TABLE chase_communication_event_reviews ENABLE ALWAYS TRIGGER immutable_rows;
ALTER TABLE chase_communication_event_reviews ENABLE ALWAYS TRIGGER immutable_table;

-- Invoker security retains the underlying tenant RLS. No bounded history window
-- participates in pending counts. Phone routes are internal evidence only.
CREATE VIEW chase_communication_review_sources WITH (security_invoker=true) AS
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
LEFT JOIN chase_communication_event_reviews d ON d.practice_id=s.practice_id AND d.case_id=s.case_id
    AND d.source_ref=s.source_ref AND d.source_hash=s.source_hash AND d.decision IN ('handled','leave_unassigned')
LEFT JOIN LATERAL (SELECT contact_snapshot_id FROM chase_contact_snapshots c
    WHERE c.practice_id=s.practice_id AND c.chase_id=s.case_id
    ORDER BY c.verified_at DESC,c.created_at DESC,c.contact_snapshot_id DESC LIMIT 1) snap ON s.held
LEFT JOIN chase_contact_routes route ON route.practice_id=s.practice_id AND route.contact_snapshot_id=snap.contact_snapshot_id AND route.channel='sms';
REVOKE ALL ON chase_communication_review_sources FROM PUBLIC;

CREATE FUNCTION cosmaneura_validate_communication_review() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE source record;
BEGIN
    IF public.cosmaneura_practice_allowed(NEW.practice_id) IS NOT TRUE
       OR public.cosmaneura_staff_assignee_allowed(NEW.actor_tenant_id,NEW.actor_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'communications staff unavailable' USING ERRCODE='insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('communications-review:' || NEW.practice_id || ':' || NEW.source_ref,0));
    PERFORM 1 FROM public.chase_items WHERE practice_id=NEW.practice_id AND chase_id=NEW.case_id FOR UPDATE;
    SELECT * INTO source FROM public.chase_communication_review_sources
        WHERE practice_id=NEW.practice_id AND case_id=NEW.case_id AND source_ref=NEW.source_ref AND source_hash=NEW.source_hash;
    IF NOT FOUND OR source.completed_decision IS NOT NULL THEN
        RAISE EXCEPTION 'communications review source stale' USING ERRCODE='check_violation';
    END IF;
    IF (NEW.decision='handled' AND source.held)
       OR (NEW.decision IN ('attribute','leave_unassigned') AND NOT source.held)
       OR (NEW.decision='attribute' AND (source.attributable_snapshot_id IS NULL
           OR source.attributable_snapshot_id<>NEW.contact_snapshot_id)) THEN
        RAISE EXCEPTION 'communications review evidence mismatch' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION cosmaneura_validate_communication_review() FROM PUBLIC;
CREATE TRIGGER review_evidence BEFORE INSERT ON chase_communication_event_reviews
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_validate_communication_review();
ALTER TABLE chase_communication_event_reviews ENABLE ALWAYS TRIGGER review_evidence;
COMMIT;
