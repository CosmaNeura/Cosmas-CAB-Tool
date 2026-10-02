-- Destructive, explicitly disposable/approved rollback only. Ordinary behavior
-- rollback leaves this additive history dormant. Stop header writers first.
BEGIN;
DROP TABLE public.billing_header_operations;
DROP TABLE public.billing_header_revisions;
DROP TABLE public.billing_headers;
DROP FUNCTION public.cosmaneura_require_billing_header_audit();
DROP FUNCTION public.cosmaneura_guard_billing_header_write();
DROP FUNCTION public.cosmaneura_billing_header_editor_allowed(uuid,uuid,text);
COMMIT;
