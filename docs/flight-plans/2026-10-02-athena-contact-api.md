# Athena configured contact/insurance API integration

Risk: Tier 2 (authenticated source reads, reviewed patient data and runtime grants).
Base: PR72 294ac74b682f302edb5b06946eab8716dce731dd.

## Outcome and boundary
Mount the existing reviewed contact current/read/apply handlers for Admin and
BillingStaff. Operator-owned connection/case mapping selects the source patient;
posted patient overrides are rejected in configured mode. Staff explicitly confirm
the match and select destinations/policies. Stored destinations retain unknown
consent. No EHR writes, automatic matching, model calls or outreach authority.
Session and task capability projections advertise only enabled staff access;
task athenaReview.billingAvailable is false. Public billing read/apply routes
remain unavailable until the separate import/model-input boundary is integrated.

## Composition
Port the final 8d0caca Athena-only settings and conditional API role grants.
Preserve existing mapper scope repair, every core/SQL byte, signer and clinical
pipeline. Keep the same worklist object across app startup/shutdown/re-entry.
Athena store health joins startup/readiness; disabled mode stays disabled.
No direct runtime/worker registry writes or registry SELECT grants are added.

## Validation and evidence
Use one new owned loopback PostgreSQL database, actual locked runner and all
17 sparse migrations (001–009,011,013,014,019–023), exact ledger/replay proof.
Complete configured suite remains byte-identical. Preserve complete binding PG
assertions; adapt only owned-host gate and require the pre-applied full ledger.
The configured fixture checks the last canonical function definitions through023.
Carry the complete Athena-only settings cases from the historical settings suite.
Actual authenticated API tests cover replicas/retry, selected fields, unknown
consent, tenant/roles/missing mapping, national phones, audit and billing404.
Run focused checks and required guardrail proof once; retain any failures.
External Python activity is denied; adapters use only invented source responses.

## Recovery and release
No migration/schema changes. Disable COSMANEURA_ATHENA_REVIEW_MODE to remove
capabilities and reads; historical receipts remain immutable. Runtime grants can
remain dormant. This does not activate hosted Athena, credentials or any cohort.
Parent assigns independent review after the final substantive push, before merge.
