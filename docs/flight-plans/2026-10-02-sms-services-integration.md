# SMS service composition — Tier 2

Base: `8e68f52e6c443f1839a01c7ca46194542a20776b` (SMS dispatch).
Scope: isolated worker/callback composition, SMS configuration, dormant staff
runtime module, and the worker's existing narrow communication grants.

## Outcome and boundaries

Reuse the complete final `config.py`, `runtime.py`, `worker.py`, and `webhooks.py`
from preserved `8d0caca22aaed8001aaeec955c81c3f7ecc11bd4`. The repaired parent
dispatcher retains its transaction-local database clock. Worker composition
reads only its bound database URL and sender credentials; callbacks have no
outbound transport. The API still does not mount the staff communication router.
SMS is the default channel; voice remains disabled. No latch is enabled.

The next independent slice mounts authenticated staff routes and their API role
grants, then runs the complete API/worker/replica/inbox acceptance files. This
split retains all guards rather than trimming runtime behavior for line budget.
Root's separate UI branch owns the same immutable DTO helper bytes.

## Implementation and verification

- Port only SMS Settings fields/parsers/validation and legacy ACS isolation.
- Provision communication reads and dispatch/callback effect writes only for
  the existing practice-bound worker login; no staff approvals or broad grants.
- Retain all existing SMS configuration, recipient-allowlist and draft checks.
  The unrelated Athena definitions from the mixed settings test file are out of
  scope; they require a different integration stack.
- Rehearse the actual locked sparse migration chain and real worker grants in
  a new disposable database on the existing local pgvector instance. Exercise
  signed inbound callback persistence/readiness and a dark worker batch with
  fake transport only. Verify worker rejection of staff input writes and
  encounter/identity/audit reads. Preserve failure evidence.
- Run focused local checks and required anti-upcode invariants. No new Actions
  job, cloud request, vendor request, credential read, or staging change.

## Release and recovery

Default-off configuration stays default-off. Existing live database roles are
not changed: the provisioning code is newly available, exercised only in the
owned disposable DB. A separately reviewed operator rollout must apply current
schema/grants, callback origin/credentials and every consent/activation gate.
This PR does not provide those approvals or claim live texting readiness.
Recovery is reverting this unactivated application slice while leaving the
additive schema dormant. Do not drop immutable communication history.

## Review record

Exact source hashes, source fidelity and local results are retained under
`outputs/mcd-training-2026-10-02/sms-services-integration/`. Independent agent
review must bind the final substantive pushed commit before this outcome is
considered reviewed. That review is not a GitHub approval or deployment.
