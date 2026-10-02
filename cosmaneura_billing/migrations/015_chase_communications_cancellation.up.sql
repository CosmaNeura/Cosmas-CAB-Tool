-- Dormant expand rung: cancellation is local evidence, never a vendor recall.
BEGIN;
CREATE TABLE chase_communication_cancellations (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,operation_id),
    FOREIGN KEY (practice_id,operation_id)
        REFERENCES chase_communication_operations(practice_id,operation_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id)
);
REVOKE ALL ON chase_communication_cancellations FROM PUBLIC;
CREATE POLICY practice_isolation ON chase_communication_cancellations
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_communication_cancellations ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_communication_cancellations FORCE ROW LEVEL SECURITY;
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chase_communication_cancellations
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chase_communication_cancellations
    FOR EACH STATEMENT EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
ALTER TABLE chase_communication_cancellations ENABLE ALWAYS TRIGGER immutable_rows;
ALTER TABLE chase_communication_cancellations ENABLE ALWAYS TRIGGER immutable_table;

-- Both writers lock the same case before inspecting the opposite ledger.
-- This also protects direct SQL writes that did not take the application's
-- practice advisory lock. A committed start is permanent even without receipt.
-- SECURITY DEFINER permits the worker's SELECT-only cancellation grant; tenant
-- authority is explicitly checked against session_user before any row access.
CREATE FUNCTION cosmaneura_guard_communication_cancellation() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE target_case text;
BEGIN
    IF public.cosmaneura_practice_allowed(NEW.practice_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'communications practice unavailable' USING ERRCODE='insufficient_privilege';
    END IF;
    IF TG_TABLE_NAME='chase_communication_cancellations' THEN
        IF public.cosmaneura_staff_assignee_allowed(NEW.actor_tenant_id,NEW.actor_id) IS NOT TRUE THEN
            RAISE EXCEPTION 'communications staff unavailable' USING ERRCODE='insufficient_privilege';
        END IF;
    END IF;
    SELECT o.case_id INTO target_case FROM public.chase_communication_operations o
        WHERE o.practice_id=NEW.practice_id AND o.operation_id=NEW.operation_id;
    IF target_case IS NULL THEN
        RAISE EXCEPTION 'communications operation unavailable' USING ERRCODE='foreign_key_violation';
    END IF;
    PERFORM 1 FROM public.chase_items
        WHERE practice_id=NEW.practice_id AND chase_id=target_case FOR UPDATE;
    IF TG_TABLE_NAME='chase_communication_cancellations' THEN
        IF EXISTS (SELECT 1 FROM public.chase_communication_dispatch_starts
                   WHERE practice_id=NEW.practice_id AND operation_id=NEW.operation_id) THEN
            RAISE EXCEPTION 'communications dispatch already started' USING ERRCODE='check_violation';
        END IF;
    ELSIF EXISTS (SELECT 1 FROM public.chase_communication_cancellations
                 WHERE practice_id=NEW.practice_id AND operation_id=NEW.operation_id) THEN
        RAISE EXCEPTION 'communications operation canceled' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION cosmaneura_guard_communication_cancellation() FROM PUBLIC;
CREATE TRIGGER cancellation_before_start BEFORE INSERT ON chase_communication_cancellations
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_guard_communication_cancellation();
CREATE TRIGGER start_unless_canceled BEFORE INSERT ON chase_communication_dispatch_starts
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_guard_communication_cancellation();
ALTER TABLE chase_communication_cancellations ENABLE ALWAYS TRIGGER cancellation_before_start;
ALTER TABLE chase_communication_dispatch_starts ENABLE ALWAYS TRIGGER start_unless_canceled;
COMMIT;
