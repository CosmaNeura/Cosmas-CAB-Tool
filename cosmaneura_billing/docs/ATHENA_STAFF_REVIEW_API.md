# Staff contact and insurance review API

This integration exposes POST /api/athena/card-chase,
/api/athena/card-chase/read and /api/athena/card-chase/apply. Admin and BillingStaff
need current practice membership and the existing configured Entra session.
Coder/Provider roles do not gain access. All three routes use the normal
minimum-necessary access audit.

COSMANEURA_ATHENA_REVIEW_MODE defaults to disabled. Mock is local only; preview
retains the bounded legacy synthetic cohort. The staff deployment mode is
configured with COSMANEURA_STATE_STORE=postgres, COSMANEURA_CHASE_STORE=postgres,
DATABASE_URL, COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT=preview|production,
COSMANEURA_ATHENA_CLIENT_ID, COSMANEURA_ATHENA_CLIENT_SECRET, and the existing
shared COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY. Secrets use the approved deployment
secret store, never request JSON. Production additionally needs the separate
owner-managed read/storage admission and exact COSMANEURA_ATHENA_DEPLOYMENT_ID
and COSMANEURA_ATHENA_STORAGE_ID; setting a mode never grants admission.

Use the existing owner-only python -m cosmaneura_billing.athena_bindings_cli
registration/revocation commands and their exact manifest contract to register
a practice connection and explicitly confirm a case's opaque patient reference
against an Athena source patient. Runtime credentials cannot do that registration.
An absent/revoked/stale binding is returned as an unavailable connection, with no
fallback patient. The configured read request uses the returned binding digest,
an operation ID and expected revision, never a supplied source patient.

Review returns unresolved/unknown values explicitly. Apply requires human
confirmation of the patient match and explicit field/policy selections. National
US phone numbers require explicit US country confirmation. Selected contact
destinations always have unknown consent; they neither enroll nor send messages.
Retries retain the same exact operation payload and current server revision.

Task athenaReview is {available:true, mode:"configured", billingAvailable:false}
when enabled for authorized staff. Session capabilities.athenaReview is true for
those same staff. Billing discovery/import routes are not mounted in this slice;
importing a note and admitting its model input is a separate integration. This
API does not declare a note finished or signed and never writes back to Athena.
The dashboard contact controls are a separate frontend integration.

Local proof uses synthetic fixtures, a new owned database and fake read-only
source transport. Hosted configuration, secrets, data-handling approval and
actual Athena activation remain deployment work.
