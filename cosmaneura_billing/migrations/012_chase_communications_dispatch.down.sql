-- Development-only reversal of an EMPTY expand rung. Operational rollback
-- disables the dispatcher and preserves its immutable outreach evidence.
BEGIN;
SET LOCAL row_security=off;
DO $$ DECLARE relation text; populated boolean; BEGIN
    FOREACH relation IN ARRAY ARRAY['chase_communication_policy_decisions','chase_communication_consent_decisions',
        'chase_communication_authorizations','chase_communication_dispatch_starts',
        'chase_communication_dispatch_receipts','chase_communication_provider_events','chase_communication_dispatch_checks'] LOOP
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I)',relation) INTO populated;
        IF populated THEN RAISE EXCEPTION 'communications evidence exists; leave expand rung dormant'; END IF;
    END LOOP;
END $$;
DROP TABLE chase_communication_provider_events,chase_communication_dispatch_receipts,chase_communication_dispatch_checks,
    chase_communication_dispatch_starts,chase_communication_authorizations,
    chase_communication_consent_decisions,chase_communication_policy_decisions;
DROP FUNCTION cosmaneura_validate_communication_dispatch();
COMMIT;
