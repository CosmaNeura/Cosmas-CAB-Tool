-- Ordinary recovery disables the feature and preserves history.
BEGIN;
SET LOCAL row_security=off;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.billing_header_signoffs) THEN
  RAISE EXCEPTION 'header signoff evidence exists; leave migration 013 dormant';
 END IF;
END $$;
DROP TABLE public.billing_header_signoffs;
DROP FUNCTION public.cosmaneura_require_header_signoff_audit();
DROP FUNCTION public.cosmaneura_guard_header_signoff();
COMMIT;
