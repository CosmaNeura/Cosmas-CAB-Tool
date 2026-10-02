-- Dormant expand rung: investigated outcomes never alter a permanent start.
BEGIN;
CREATE TABLE chase_communication_unknown_resolutions (
    practice_id text NOT NULL,
    operation_id uuid NOT NULL,
    decision text NOT NULL CHECK (decision IN ('confirmed_delivered','confirmed_not_sent','confirmed_attempted')),
    vendor_reference text NOT NULL CHECK (vendor_reference ~ '^[A-Za-z0-9_.:/-]{1,200}$'),
    actor_tenant_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id,operation_id),
    FOREIGN KEY (practice_id,operation_id)
        REFERENCES chase_communication_dispatch_starts(practice_id,operation_id),
    FOREIGN KEY (practice_id,actor_tenant_id,actor_id)
        REFERENCES practice_memberships(practice_id,tenant_id,user_id)
);
REVOKE ALL ON chase_communication_unknown_resolutions FROM PUBLIC;
CREATE POLICY practice_isolation ON chase_communication_unknown_resolutions
    USING (cosmaneura_practice_allowed(practice_id))
    WITH CHECK (cosmaneura_practice_allowed(practice_id));
ALTER TABLE chase_communication_unknown_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chase_communication_unknown_resolutions FORCE ROW LEVEL SECURITY;
CREATE TRIGGER immutable_rows BEFORE UPDATE OR DELETE ON chase_communication_unknown_resolutions
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
CREATE TRIGGER immutable_table BEFORE TRUNCATE ON chase_communication_unknown_resolutions
    FOR EACH STATEMENT EXECUTE FUNCTION cosmaneura_reject_communication_mutation();
ALTER TABLE chase_communication_unknown_resolutions ENABLE ALWAYS TRIGGER immutable_rows;
ALTER TABLE chase_communication_unknown_resolutions ENABLE ALWAYS TRIGGER immutable_table;

CREATE FUNCTION cosmaneura_guard_unknown_communication() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE target_case text;
BEGIN
    IF public.cosmaneura_practice_allowed(NEW.practice_id) IS NOT TRUE THEN
        RAISE EXCEPTION 'communications practice unavailable' USING ERRCODE='insufficient_privilege';
    END IF;
    IF TG_TABLE_NAME IN ('chase_communication_unknown_resolutions','chase_communication_dispatch_receipts') THEN
        -- Never wait for I/O while holding a case/practice lock. This is the
        -- same execution key held by the separately credentialed worker.
        IF NOT pg_try_advisory_xact_lock(hashtextextended(
                'communications-send:' || NEW.practice_id || ':' || NEW.operation_id::text,0)) THEN
            RAISE EXCEPTION 'communications dispatch in progress' USING ERRCODE='lock_not_available';
        END IF;
    END IF;
    IF TG_TABLE_NAME='chase_communication_dispatch_receipts' THEN
        IF EXISTS (SELECT 1 FROM public.chase_communication_unknown_resolutions
                   WHERE practice_id=NEW.practice_id AND operation_id=NEW.operation_id) THEN
            RAISE EXCEPTION 'communications outcome already investigated' USING ERRCODE='check_violation';
        END IF;
        RETURN NEW;
    ELSIF TG_TABLE_NAME='chase_communication_unknown_resolutions' THEN
        IF public.cosmaneura_staff_assignee_allowed(NEW.actor_tenant_id,NEW.actor_id) IS NOT TRUE THEN
            RAISE EXCEPTION 'communications staff unavailable' USING ERRCODE='insufficient_privilege';
        END IF;
        SELECT o.case_id INTO target_case FROM public.chase_communication_operations o
            JOIN public.chase_communication_dispatch_starts d USING(practice_id,operation_id)
            LEFT JOIN public.chase_communication_dispatch_receipts r USING(practice_id,operation_id)
            WHERE o.practice_id=NEW.practice_id AND o.operation_id=NEW.operation_id
              AND (r.operation_id IS NULL OR r.outcome='unknown');
        IF target_case IS NULL THEN
            RAISE EXCEPTION 'communications outcome not unknown' USING ERRCODE='check_violation';
        END IF;
    ELSIF TG_TABLE_NAME='chase_communication_operations' THEN
        target_case := NEW.case_id;
    ELSE
        SELECT case_id INTO target_case FROM public.chase_communication_operations
            WHERE practice_id=NEW.practice_id AND operation_id=NEW.operation_id;
    END IF;
    PERFORM 1 FROM public.chase_items
        WHERE practice_id=NEW.practice_id AND chase_id=target_case FOR UPDATE;
    IF TG_TABLE_NAME <> 'chase_communication_unknown_resolutions' AND EXISTS (
        SELECT 1 FROM public.chase_communication_dispatch_starts d
        JOIN public.chase_communication_operations o USING(practice_id,operation_id)
        LEFT JOIN public.chase_communication_dispatch_receipts r USING(practice_id,operation_id)
        LEFT JOIN public.chase_communication_unknown_resolutions u USING(practice_id,operation_id)
        WHERE d.practice_id=NEW.practice_id AND o.case_id=target_case AND u.operation_id IS NULL
          AND (r.operation_id IS NULL OR r.outcome='unknown')
    ) THEN
        RAISE EXCEPTION 'communications unknown requires resolution' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION cosmaneura_guard_unknown_communication() FROM PUBLIC;
CREATE TRIGGER resolve_unknown BEFORE INSERT ON chase_communication_unknown_resolutions
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_guard_unknown_communication();
ALTER TABLE chase_communication_unknown_resolutions ENABLE ALWAYS TRIGGER resolve_unknown;
CREATE TRIGGER receipt_after_investigation_guard BEFORE INSERT ON chase_communication_dispatch_receipts
    FOR EACH ROW EXECUTE FUNCTION cosmaneura_guard_unknown_communication();
ALTER TABLE chase_communication_dispatch_receipts ENABLE ALWAYS TRIGGER receipt_after_investigation_guard;
DO $$ DECLARE relation text; BEGIN
    FOREACH relation IN ARRAY ARRAY['chase_communication_operations','chase_communication_authorizations',
                                   'chase_communication_dispatch_starts'] LOOP
        EXECUTE format('CREATE TRIGGER unknown_hold BEFORE INSERT ON public.%I '
            'FOR EACH ROW EXECUTE FUNCTION public.cosmaneura_guard_unknown_communication()',relation);
        EXECUTE format('ALTER TABLE public.%I ENABLE ALWAYS TRIGGER unknown_hold',relation);
    END LOOP;
END $$;
COMMIT;
