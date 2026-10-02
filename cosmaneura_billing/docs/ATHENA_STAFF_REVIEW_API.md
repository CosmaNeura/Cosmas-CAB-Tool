# Staff contact and insurance review API

This integration exposes POST /api/athena/card-chase,
/api/athena/card-chase/read, /api/athena/card-chase/apply,
/api/athena/billing/read and /api/athena/billing/apply. Admin and BillingStaff
need current practice membership and the existing configured Entra session.
Coder/Provider roles alone do not grant access. All five routes use the normal
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

Task athenaReview advertises billingAvailable:true for enabled staff. After
reviewing contacts, staff can discover encounters, review an exact note and confirm
a finished note/context before local import. A source status is an observation,
not a CAB declaration that a clinician signed the note. No EHR write occurs.

Operational imported text cannot reach a model without a separate
COSMANEURA_MODEL_DATA_POLICY_JSON. Its closed v1/v2 contract is parsed by
model_admission.parse_policy; no HTTP request can supply policy authority.
The exact policy binds actual database/login, member, source and Azure service.
Source read/storage grants never transfer this authority. An empty, expired or
mismatched policy reports inferencePolicy.allowed:false while preserving source
and existing header access. The global synthetic-only latch still blocks Athena
operational text. New headers retain modelInputProof; exact replay requires no
new model call. Policy removal requires retiring old replicas that loaded it.
Provider model-source verification is implemented through migration025; Core
workload intake additionally uses026. Those separate permissions do not follow
from Athena access. See [provider policy](PROVIDER_MODEL_SOURCE_POLICY.md).

Local proof uses synthetic fixtures, a new owned database and fake read-only
source transport. Hosted configuration, secrets, data-handling approval and
actual Athena activation remain deployment work.
