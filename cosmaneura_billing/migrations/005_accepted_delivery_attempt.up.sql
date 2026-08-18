-- 005_accepted_delivery_attempt.up.sql
-- Narrow worker capability for atomically recording one accepted automated
-- delivery in the existing chase contact log. Callers need EXECUTE only; the
-- function owns all validation, locking, field derivation, and insertion.

BEGIN;

CREATE FUNCTION cosmaneura_record_accepted_delivery_attempt(
    target_delivery_id uuid
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET timezone = 'UTC'
AS $$
DECLARE
    scoped_practice_id text;
    delivery record;
    existing_attempt record;
    allocated_attempt_no integer;
BEGIN
    scoped_practice_id := NULLIF(
        current_setting('cosmaneura.practice_id', true), ''
    );
    IF scoped_practice_id IS NULL
       OR NOT public.cosmaneura_practice_allowed(scoped_practice_id) THEN
        RAISE EXCEPTION 'accepted delivery attempt is unavailable'
            USING ERRCODE = '42501';
    END IF;

    SELECT
        outbox.practice_id,
        outbox.chase_id,
        outbox.channel,
        outbox.template_id,
        outbox.finalized_at
      INTO delivery
      FROM public.chase_delivery_outbox AS outbox
     WHERE outbox.practice_id = scoped_practice_id
       AND outbox.delivery_id = target_delivery_id
       AND outbox.state = 'accepted'
       AND outbox.finalized_at IS NOT NULL
     FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'accepted delivery attempt is unavailable'
            USING ERRCODE = '55000';
    END IF;

    -- Every writer of chase attempts serializes on the chase item. This makes
    -- MAX(attempt_no)+1 safe across worker replays and concurrent staff work.
    PERFORM 1
      FROM public.chase_items AS item
     WHERE item.practice_id = delivery.practice_id
       AND item.chase_id = delivery.chase_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'accepted delivery attempt is unavailable'
            USING ERRCODE = '55000';
    END IF;

    SELECT
        attempt.attempt_no,
        attempt.attempted_at,
        attempt.actor,
        attempt.outcome,
        attempt.minutes_spent,
        attempt.note
      INTO existing_attempt
      FROM public.chase_attempts AS attempt
     WHERE attempt.practice_id = delivery.practice_id
       AND attempt.delivery_id = target_delivery_id;
    IF FOUND THEN
        IF existing_attempt.attempted_at::timestamptz
               IS DISTINCT FROM delivery.finalized_at
           OR existing_attempt.actor <> 'system'
           OR existing_attempt.outcome <> 'accepted'
           OR existing_attempt.minutes_spent <> 0
           OR existing_attempt.note <> '' THEN
            RAISE EXCEPTION 'accepted delivery attempt conflicts with history'
                USING ERRCODE = '55000';
        END IF;
        RETURN existing_attempt.attempt_no;
    END IF;

    SELECT COALESCE(MAX(attempt.attempt_no), 0) + 1
      INTO allocated_attempt_no
      FROM public.chase_attempts AS attempt
     WHERE attempt.practice_id = delivery.practice_id
       AND attempt.chase_id = delivery.chase_id;

    INSERT INTO public.chase_attempts (
        practice_id,
        chase_id,
        attempt_no,
        channel,
        template_id,
        attempted_at,
        actor,
        outcome,
        minutes_spent,
        note,
        delivery_id
    ) VALUES (
        delivery.practice_id,
        delivery.chase_id,
        allocated_attempt_no,
        delivery.channel,
        delivery.template_id,
        delivery.finalized_at::text,
        'system',
        'accepted',
        0,
        '',
        target_delivery_id
    );

    RETURN allocated_attempt_no;
END
$$;

REVOKE ALL ON FUNCTION cosmaneura_record_accepted_delivery_attempt(uuid)
    FROM PUBLIC;

COMMIT;
