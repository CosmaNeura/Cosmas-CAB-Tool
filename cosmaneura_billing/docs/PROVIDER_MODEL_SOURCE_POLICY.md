# Provider source permission for coding

Intake and model disclosure are separate. A registered completed note is stored
without inference. Canonical025 supplies a narrow source-proof reader; it does
not authorize model egress. All existing CAB membership, source and header
controls continue to apply. The deployment policy defaults to empty.

The existing `COSMANEURA_MODEL_DATA_POLICY_JSON` accepts a closed
`cab-model-data-policy.v2` object. It has the v1 policyId, reviewRef,
serverPracticeId, tenantId, deploymentId, storageId, databaseName, runtimeRole,
notBefore, expiresAt and azureOpenAI fields. Instead of v1's `source`, use:

    "sources": {
      "provider_completed_note": {
        "connectionId": "<registered canonical UUID>",
        "connectionVersion": 1,
        "dataClass": "synthetic"
      }
    }

The optional `athena_staff_review_operational` entry must be `{}`. Provider class
is exactly synthetic or deidentified; synthetic-only deployments allow only the
registered synthetic class. A string label or caller-supplied source tag is not
permission. The actual runtime login/database, practice/member, deployment and
storage identifiers, Azure endpoint/deployment/model/API version/managed identity
must match the policy before each request and final insertion.

The receiver's sourceMode `cab_provider_attested` describes CAB-authenticated
provider confirmation. This is not a Core browser/delegated caller contract or
an independent EHR signature. A Core bridge must separately establish trusted
caller mapping and accepted source authority; it cannot invent Entra claims or
turn recording duration into personally performed clinician E/M time.

A generated provider Azure header contains `generation.modelInputProof` with
exactly schemaVersion (`cab-model-input-proof.v2`), policyId, policySha256,
sourceSha256, serviceSha256, expiresAt, sourceOperationId and
sourceAdmissionSha256. The server obtains both source fields from the accepted
immutable operation; callers must not supply them. sourceSha256 is
billing_header.digest(headerSource), the exact expectedSource snapshot. All
proof hashes are lowercase SHA256; IDs are canonical UUIDs.

Read/edit/replay do not call a model. Proof expiry is historical metadata on a
stored header, not current permission. Policy expiry/removal blocks new model
use while existing authorized header reads and edits remain available. Intake
grant expiry constrained acceptance; explicit grant/connection revocation still
blocks new model use. Latest matching source acceptance is used without fallback.

For recovery, remove provider model permission and retire old app revisions.
Preserve025, source records, receipts and saved headers. This source integration
registers no deployment policy and activates no hosted or vendor service.
