# Athena reviewed billing import and separate model admission

Risk: Tier 2 (source data, model disclosure and persisted billing evidence).
Base: PR75 0187258704d0aee9c45b2c55121a06def1904d63.

## Outcome
Mount the reviewed billing discovery/import routes and advertise billingAvailable.
Staff must explicitly confirm patient match, finished source and clinical context;
source hashes and metadata are checked again before immutable local import.
Athena remains read-only. Source read/storage approval does not grant model access.

## Separate disclosure contract
Port the exact final model_admission module and complete Athena model/read tests.
An immutable, empty-by-default deployment JSON policy binds practice, tenant,
deployment/storage references, actual database/login, source and exact Azure
endpoint/deployment/model/API version/managed identity. No browser policy selector.
Only the full pipeline receives a source/bundle-bound permit. The actual Azure
adapter checks it before each attempt/retry; TA4H, secondary engines and shadow
disclosure remain excluded. Expiry/member/source/runtime are checked again inside
the existing source-locked header insert. New operational headers retain proof.
Exact header replay precedes a new permit, preserving saved edits after expiry.
Provider v2 policy parsing remains compatible, but its absent025 reader prevents
provider admission/readiness on this sparse stack; no provider route is added.

## Preservation and verification
Preserve the repaired Athena mapper, exact core and all17 canonical SQL migrations
(001–009,011,013,014,019–023); no additional schema or deployment change.
Keep current clinical qualifiers, header time/evidence and signoff/edit behavior.
Use a new owned loopback database and the full locked migration runner; verify
checksums and no-op replay. Exercise complete read/model policy tests, real
restricted-role authenticated billing import/header and existing header controls.
All source/model responses are fake; Python external activity is denied.
Run relevant checks/guardrails once and preserve failures plus targeted repairs.
Frontend proof display and note-only policy holds are a separate parent-owned
integration. This branch does not activate a hosted cohort or model deployment.

## Recovery
Disable Athena review to prevent new reads/imports. Clear model policy and retire
old application replicas to revoke that deployment policy; expiry remains live.
Historical sources/headers stay readable/editable subject to existing auth.
No schema rollback or deletion. Independent review follows final substantive push.
