# Completed-note receiver

This CAB receiver accepts the closed `cab.completed-note-handoff.v1` contract.
It does not connect a provider host, write to an EHR, or infer on receipt.
Completion is the authenticated provider's confirmation, not independently
verified upstream signature status. The clinical note remains unchanged.

Deploy the current sparse 19-rung migration set with the locked migration runner
and provision its restricted API role. Keep a stable
`COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY` across durable replicas. Enable intake
only with both `COSMANEURA_PROVIDER_HANDOFF_ENABLED=true` and
`COSMANEURA_BILLING_HEADER_ENABLED=true`. Both default to false. Empty source
registries deny every new completed-note input; the feature flag is not admission.

Using an existing migration-owner environment for the configured practice/tenant,
register privately reviewed synthetic or de-identified authority:

    python -m cosmaneura_billing.provider_admission_cli register-connection /private/path/connection.json
    python -m cosmaneura_billing.provider_admission_cli register-grant /private/path/grant.json

`provider_admission.connection` defines the closed source contract;
`admission_for(prepare_handoff(HandoffRequest.model_validate(request), practice),
actor, source, not_before=..., expires_at=...)` prepares the exact operation grant.
Review the source and request before registration. Never place real chart data or
secrets in tracked files. Runtime logins can read these registrations but cannot
create them. Revocation uses `revoke-connection UUID` or `revoke-grant UUID`.

The authenticated Provider/Admin sends `POST /api/provider/encounters/handoff`.
Use the same operationId and exact request to recover an uncertain receipt;
new revisions require a new operationId, matching expectedSource and new grant.
Optional visit.serviceDate is an ISO calendar day. Omission preserves older
request identities; explicit null is rejected. Hashes cover exact UTF-8 document
text, including CRLF. Request completion/version/source identifiers are required.

Receipt generation is `not_started`. Its current headerSource pins the existing
header endpoint. Provider model-source025 resolves accepted provenance; a separate closed
model policy must match its connection/version/class before Azure use. Intake
approval alone cannot disclose the note to a model service.
Local fixed synthetic/offline tests exercise the full pipeline and editable
header. Retain historical source, receipts and headers on rollback; disable intake
and roll back application code instead of dropping migrations.
