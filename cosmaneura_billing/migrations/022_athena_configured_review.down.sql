-- Retain immutable v2 evidence; use the application flag for a dark rollback.
BEGIN;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.athena_case_review_events WHERE payload->>'schemaVersion'='athena-case-review.v2')
 THEN RAISE EXCEPTION 'configured Athena history requires forward recovery'; END IF;
END $$;
DROP TRIGGER athena_configured_review_guard ON public.athena_case_review_events;
DROP FUNCTION public.cosmaneura_guard_configured_athena_review();
ALTER TABLE public.athena_case_review_events DROP CONSTRAINT athena_case_review_events_check;
ALTER TABLE public.athena_case_review_events ADD CONSTRAINT athena_case_review_events_check CHECK(
 jsonb_typeof(payload)='object' AND payload->>'schemaVersion'='athena-case-review.v1'
 AND octet_length(payload::text)<=1048576 AND (payload->>'revision')::integer=revision);
COMMIT;
