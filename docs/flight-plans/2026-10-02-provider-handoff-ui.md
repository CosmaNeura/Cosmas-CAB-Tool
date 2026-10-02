# Provider completed-note header interface

Tier 2. Base: PR79 55e28fcfc3bbf8125bec2732b3f21b3da1fb5fc2.

Outcome: accept a server-authorized provider_completed_note source in the standalone
header launch, decode its exact v2 historical model-input proof, and provide a
capability-gated synthetic fixture handoff for local training and receiver review.
The provider-confirmed label is an attestation, not independent EHR signature proof.
Core uses its own notes page and same-origin bridge; this PR adds no arbitrary-note
intake, Core identity mapping, EHR link, model authority, or hosted activation.

## Boundaries and dependencies

- The session must advertise provider, billingHeader, and (for fixture intake)
  providerHandoff. The receiver still authorizes every request. PR80 owns intake;
  PR81 owns provider model permission. A missing endpoint remains unavailable.
- The URL contains only a route and selected encounter ID. Exact original note
  and supporting-source digests are verified before the header mounts. Provider
  detail requires an explicit valid current inferencePolicy, as Athena does.
- The v2 proof is closed, source-specific historical metadata. Missing proof in
  an older header remains valid; expired permission does not block read/edit.
  Generate remains governed by current policy and the server's final admission.
- Keep existing launch/draft/request ownership and exact mutation retry guards.
  A fixture is never automatically selected. An unknown handoff response retains
  its operation/fixture; only a matching current receipt can navigate.

## Validation and recovery

Run focused source/receipt/proof and adjacent header Node contracts, build with
cached dependencies, and exercise the composed PR81 backend with synthetic
fixtures, actual restricted PostgreSQL storage, and a fake Azure transport.
Retain command/source pins, screenshots, failures, and implementer review under
outputs/mcd-training-2026-10-02/provider-handoff-ui-integration. No vendor requests.
No clinical engine changes; its existing invariant proof runs once as required.
Revert this frontend commit to remove the launch extension and fixture link;
immutable accepted notes and saved headers remain in storage. No migration.
