# Provider source model permission integration

Tier 2. Base PR80 `ec271e10ef94bffcd9f6bc2e6ba0913b4c54b4be`.

Finish the dormant provider lane already implemented in the model policy, full
pipeline, per-Azure-attempt guard and header proof/insert path. Add exact final
canonical025 and only its runtime EXECUTE grant. Keep existing019/020/024 source
writers and clinical code unchanged. The complete sparse chain has19 rungs:
001–009,011,013,014,019–025. Verify exact locked-runner ledger/replay locally.

025 resolves the current accepted source, latest matching operation, exact
admission and immutable connection. It checks live revocation and current CAB
membership, takes only admission/connection locks in established order, and
never grants network permission. Separate immutable cab-model-data-policy.v2
binds the provider connection/version/class and actual deployment/storage/DB/
role/Azure service. Empty policy is closed. Synthetic-only requires exact
registered synthetic class; deidentified stays excluded in that mode.

Intake grant expiry controls acceptance; model policy expiry controls each new
model request and insertion. Explicit intake/connection revocation stops new
model use. Provider v2 proof binds accepted operation and admission hash to the
unchanged source snapshot. Athena v1 bytes and historical proof-less provider
headers remain compatible. Read/edit/replay of stored headers does not infer.

Existing sourceMode cab_provider_attested is authenticated CAB provider
confirmation, not Core caller authentication, delegated identity or an upstream
signature. Core bridge trust/mapping and time provenance are separate work. No
browser identifiers or caller proof fields grant authority here.

Verify the complete original provider model tests, actual restricted-role PG
and fake Azure full-pipeline API; add stored-header save/reload continuation and
worker/current-actor boundaries. Preserve original failures, candidate import
origins and exact hashes. No live vendors/models/credentials or hosted changes.
Follow Sept25 owner policy: applicable local checks plus recorded implementer
review and bounded PR; no independent review gate is asserted.

Recovery: remove provider model policy and retire prior app revisions; retain025
and immutable source/header history. No downgrade that bypasses admission.
