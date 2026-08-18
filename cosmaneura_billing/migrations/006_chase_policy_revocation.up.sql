-- 006_chase_policy_revocation.up.sql
-- Revoke queued outreach without granting the API direct outbox authority.
-- Accepted, submitting, and ambiguous deliveries are never made retryable by
-- this migration: only reservations with no provider call are cancellable.

BEGIN;

-- A cancelled or definitively failed reservation produced no patient touch.
-- A later exact approval may therefore reserve the same cadence step/day
-- again. In-flight, ambiguous, and accepted rows remain uniqueness barriers.
ALTER TABLE chase_delivery_outbox
    DROP CONSTRAINT chase_delivery_outbox_item_step_unique,
    DROP CONSTRAINT chase_delivery_outbox_item_local_day_unique;

CREATE UNIQUE INDEX chase_delivery_outbox_live_item_step_unique
    ON chase_delivery_outbox (
        practice_id, chase_id, policy_revision_id, policy_step_ordinal
    )
    WHERE state NOT IN ('cancelled', 'permanent_failure');

CREATE UNIQUE INDEX chase_delivery_outbox_live_item_local_day_unique
    ON chase_delivery_outbox (
        practice_id, chase_id, local_delivery_date
    )
    WHERE state NOT IN ('cancelled', 'permanent_failure');

CREATE FUNCTION cosmaneura_cancel_revoked_chase_policy(
    target_policy_revision_id uuid,
    target_policy_fingerprint text
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET timezone = 'UTC'
AS $$
DECLARE
    scoped_practice_id text;
    latest_event_type text;
    delivery record;
    cancelled_deliveries integer := 0;
    deleted_tasks integer := 0;
    event_time timestamptz := statement_timestamp();
    next_event_no integer;
BEGIN
    scoped_practice_id := NULLIF(
        current_setting('cosmaneura.practice_id', true), ''
    );
    IF scoped_practice_id IS NULL
       OR NOT public.cosmaneura_practice_allowed(scoped_practice_id) THEN
        RAISE EXCEPTION 'revoked chase policy cleanup is unavailable'
            USING ERRCODE = '42501';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(
        scoped_practice_id || chr(31) || 'active-policy', 0
    ));

    IF target_policy_fingerprint IS NULL
       OR target_policy_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'revoked chase policy cleanup is unavailable'
            USING ERRCODE = '22023';
    END IF;

    PERFORM 1
      FROM public.chase_policy_revisions AS revision
     WHERE revision.practice_id = scoped_practice_id
       AND revision.policy_revision_id = target_policy_revision_id
       AND revision.policy_fingerprint = target_policy_fingerprint
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'revoked chase policy cleanup is unavailable'
            USING ERRCODE = '55000';
    END IF;

    SELECT approval.event_type
      INTO latest_event_type
      FROM public.chase_policy_approval_events AS approval
     WHERE approval.practice_id = scoped_practice_id
       AND approval.policy_revision_id = target_policy_revision_id
       AND approval.policy_fingerprint = target_policy_fingerprint
     ORDER BY approval.occurred_at DESC, approval.approval_event_id DESC
     LIMIT 1;
    IF latest_event_type IS DISTINCT FROM 'revoked' THEN
        RAISE EXCEPTION 'chase policy is not currently revoked'
            USING ERRCODE = '55000';
    END IF;

    FOR delivery IN
        SELECT outbox.delivery_id
          FROM public.chase_delivery_outbox AS outbox
         WHERE outbox.practice_id = scoped_practice_id
           AND outbox.policy_revision_id = target_policy_revision_id
           AND outbox.policy_fingerprint = target_policy_fingerprint
           AND outbox.state = 'reserved'
         ORDER BY outbox.delivery_id
         FOR UPDATE
    LOOP
        UPDATE public.chase_delivery_outbox
           SET state = 'cancelled',
               finalized_at = event_time,
               error_code = 'policy_revoked',
               updated_at = event_time
         WHERE practice_id = scoped_practice_id
           AND delivery_id = delivery.delivery_id
           AND state = 'reserved';
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        SELECT COALESCE(MAX(event.event_no), 0) + 1
          INTO next_event_no
          FROM public.chase_delivery_events AS event
         WHERE event.practice_id = scoped_practice_id
           AND event.delivery_id = delivery.delivery_id;
        INSERT INTO public.chase_delivery_events (
            practice_id,
            delivery_id,
            event_no,
            event_type,
            occurred_at,
            error_code
        ) VALUES (
            scoped_practice_id,
            delivery.delivery_id,
            next_event_no,
            'cancelled',
            event_time,
            'policy_revoked'
        );
        cancelled_deliveries := cancelled_deliveries + 1;
    END LOOP;

    DELETE FROM public.chase_pending_tasks AS task
     WHERE task.practice_id = scoped_practice_id
       AND task.payload ->> 'schema' = 'cosmaneura.chase-staff-task.v1'
       AND task.payload ->> 'policy_revision_id' =
           target_policy_revision_id::text
       AND task.payload ->> 'policy_fingerprint' =
           target_policy_fingerprint;
    GET DIAGNOSTICS deleted_tasks = ROW_COUNT;

    RETURN cancelled_deliveries + deleted_tasks;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_cancel_revoked_chase_policy(uuid, text)
    FROM PUBLIC;

-- Detector refreshes can invalidate queued work without revoking the policy.
-- Derive the new gate from the locked item rather than accepting it from the
-- caller, then retire only no-provider-call work bound to older evidence.
CREATE FUNCTION cosmaneura_cancel_stale_chase_work(
    target_chase_id text
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
SET timezone = 'UTC'
AS $$
DECLARE
    scoped_practice_id text;
    current_gate_fingerprint text;
    delivery record;
    cancelled_deliveries integer := 0;
    deleted_tasks integer := 0;
    event_time timestamptz := statement_timestamp();
    next_event_no integer;
BEGIN
    scoped_practice_id := NULLIF(
        current_setting('cosmaneura.practice_id', true), ''
    );
    IF scoped_practice_id IS NULL
       OR NOT public.cosmaneura_practice_allowed(scoped_practice_id) THEN
        RAISE EXCEPTION 'stale chase work cleanup is unavailable'
            USING ERRCODE = '42501';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(
        scoped_practice_id || chr(31) || 'active-policy', 0
    ));

    SELECT item.direct_contract ->> 'gate_fingerprint'
      INTO current_gate_fingerprint
      FROM public.chase_items AS item
     WHERE item.practice_id = scoped_practice_id
       AND item.chase_id = target_chase_id
     FOR UPDATE;
    IF NOT FOUND
       OR current_gate_fingerprint IS NULL
       OR current_gate_fingerprint !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'stale chase work cleanup is unavailable'
            USING ERRCODE = '55000';
    END IF;

    FOR delivery IN
        SELECT outbox.delivery_id
          FROM public.chase_delivery_outbox AS outbox
         WHERE outbox.practice_id = scoped_practice_id
           AND outbox.chase_id = target_chase_id
           AND outbox.state = 'reserved'
           AND outbox.gate_fingerprint <> current_gate_fingerprint
         ORDER BY outbox.delivery_id
         FOR UPDATE
    LOOP
        UPDATE public.chase_delivery_outbox
           SET state = 'cancelled',
               finalized_at = event_time,
               error_code = 'gate_evidence_changed',
               updated_at = event_time
         WHERE practice_id = scoped_practice_id
           AND delivery_id = delivery.delivery_id
           AND state = 'reserved';
        IF NOT FOUND THEN
            CONTINUE;
        END IF;

        SELECT COALESCE(MAX(event.event_no), 0) + 1
          INTO next_event_no
          FROM public.chase_delivery_events AS event
         WHERE event.practice_id = scoped_practice_id
           AND event.delivery_id = delivery.delivery_id;
        INSERT INTO public.chase_delivery_events (
            practice_id,
            delivery_id,
            event_no,
            event_type,
            occurred_at,
            error_code
        ) VALUES (
            scoped_practice_id,
            delivery.delivery_id,
            next_event_no,
            'cancelled',
            event_time,
            'gate_evidence_changed'
        );
        cancelled_deliveries := cancelled_deliveries + 1;
    END LOOP;

    DELETE FROM public.chase_pending_tasks AS task
     WHERE task.practice_id = scoped_practice_id
       AND task.chase_id = target_chase_id
       AND task.payload ->> 'schema' = 'cosmaneura.chase-staff-task.v1'
       AND task.payload ->> 'gate_fingerprint'
           IS DISTINCT FROM current_gate_fingerprint;
    GET DIAGNOSTICS deleted_tasks = ROW_COUNT;

    RETURN cancelled_deliveries + deleted_tasks;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_cancel_stale_chase_work(text) FROM PUBLIC;

COMMIT;
