# Cosmas pilot operations runbook

Status: operator checklist for the synthetic pilot. This runbook does not
authorize real PHI, patient messaging, Athena writes, or a live billing import.
The detailed infrastructure rationale remains in [`../../infra/README.md`](../../infra/README.md).

For staff training, start with the [MCD quickstart](MCD_TRAINING_QUICKSTART.md).
Use the [current deployment guide](../../infra/BETA_DEPLOYMENT.md) for the full
001–026 migration sequence, editable header, staff Athena review, SMS services
and separate Core receiver. The background-sync procedures below describe that
older job; they do not replace the current staff review workflow.

## Owners and escalation

Fill these before a pilot user is invited. Do not put credentials, object IDs,
patient identifiers, or tokens in this file or an incident ticket.

| Responsibility | Primary | Backup | Contact path |
|---|---|---|---|
| Incident commander | `<owner>` | `<backup>` | `<approved channel>` |
| Azure deployment/database | `<owner>` | `<backup>` | `<approved channel>` |
| Entra roles and offboarding | `<owner>` | `<backup>` | `<approved channel>` |
| Athena billing integration | `<owner>` | `<backup>` | `<approved channel>` |
| Sovereign provider-labs integration | `<owner>` | `<backup>` | `<approved channel>` |
| Clinical/coding validation | `<owner>` | `<backup>` | `<approved channel>` |
| Privacy/security | `<owner>` | `<backup>` | `<approved channel>` |
| Practice operations | `<owner>` | `<backup>` | `<approved channel>` |

Escalate immediately for an unexpected write, a possible credential or PHI
disclosure, cross-practice access, an unauthorized tab/action, or an ambiguous
patient-message submission. Preserve identifiers only in the approved incident
system; the routine evidence record remains count/code-only.

## Known-safe baseline and kill switches

Use these values for a synthetic-only deployment or incident shutdown:

| Capability | Bicep parameter | Runtime value | Safe state |
|---|---|---|---|
| External access | `externalIngress` | platform-owned | `false` during maintenance or investigation |
| Billing import database material | `provisionAthenaImportDatabase` | none | `false` unless deliberately provisioning the dormant, dedicated import role |
| Billing Athena infrastructure | `provisionAthenaSync` | none | `false` unless the dormant job is deliberately provisioned |
| Billing Athena execution | `enableAthenaSync` | `COSMANEURA_ATHENA_SYNC_ENABLED=false`, `COSMANEURA_ATHENA_MODE=disabled` | `false` |
| Card Chase scheduler | `enableChaseScheduler` | `COSMANEURA_CHASE_SCHEDULER_ENABLED=false` | `false` |
| Patient messaging | `enablePatientMessaging` | `COSMANEURA_PATIENT_MESSAGING_ENABLED=false` | `false` |
| Messaging activation | `activatePatientMessaging` | `COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED=false` | `false` |
| Chase transport | derived while messaging is off | `COSMANEURA_CHASE_TRANSPORT=record_only` | `record_only` |
| Primary model inference | `enableAzureOpenAIInference` | `COSMANEURA_AZURE_OPENAI_ENABLED=false` | `false` unless a bounded synthetic acceptance is active |
| Synthetic-only model gate | `restrictAzureOpenAIToSyntheticDemo` | `COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY=false` | set `false` with inference off; set `true` whenever public synthetic inference is on |
| Active second extractor | `enableAzureLanguageExtraction` | `COSMANEURA_AZURE_LANGUAGE_ENABLED=false` | `false` |
| Second-model shadow | `enableAzureLanguageShadow` | `COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED=false` | `false` unless a reviewed synthetic shadow run is active |
| Language infrastructure | `provisionAzureLanguage` | none | `false` for a new dormant deployment |

`provisionAthenaSync=false`, `provisionAzureLanguage=false`, and other
conditional-resource flags do not delete resources created by an earlier
incremental deployment. For an incident, disable execution first. Delete only
exactly resolved resources and grants through a separately reviewed
decommissioning change.

## Evidence-backed deployment preflight

Record the following in a credential-free release record before deployment:

1. Full source commit, expected parent, PR, and immutable image digest. Require a
   clean release worktree and applicable recorded local checks under the owner
   local-check-and-merge policy; do not dispatch Actions for this training step.
2. Local/CI gates from the same commit:

   ```bash
   cd cosmaneura_billing
   python -m pytest -v
   python -m cosmaneura_billing.log_scanner --self-test
   python prove_no_upcode.py
   cd frontend
   npm ci
   npm test
   npm run build
   ```

   Return to the repository root and compile both templates as CI does:

   ```bash
   az bicep build --file infra/foundation.bicep --stdout
   az bicep build --file infra/app.bicep --stdout
   ```

3. A reviewed validation and `ResourceIdOnly` what-if for the exact foundation
   or app parameter file. Stop on an unexplained create, delete, identity, role,
   secret, ingress, database, or model change.
4. Read-only Azure evidence for the current revision/digest, ingress, Easy Auth,
   job trigger modes, identity assignments, secret *names/references*, ACR seal,
   model flags, and the kill-switch values above. Never query secret values.
5. If a new image is built, follow the bounded ACR build window in
   [`../../infra/README.md`](../../infra/README.md), resolve the manifest digest,
   and reseal ACR before application deployment. Deploy `repo@digest`, never a
   mutable tag alone.
6. Deploy with external ingress closed, run the migration job, and require its
   execution to succeed before health checks. For a synthetic demo, run only the
   twelve-case command in [MCD seed setup](MCD_TRAINING_SEED.md).
   The separate four-case CardChase setup is in the MCD quickstart.
7. Prove `/api/live` is public, protected readiness/health routes return 401
   when signed out, a forged `X-MS-CLIENT-PRINCIPAL` still returns 401, HTTPS is
   enforced, and every persona passes the matrix below. Open ingress only after
   those checks and PHI-safe log evidence are green.

## Rollback

1. Close external ingress and set every execution/outbound/model switch in the
   safe-state table above. Revoke the active Card Chase policy and wait for any
   running worker execution to finish. An ambiguous delivery is investigated;
   it is never blindly retried.
2. Capture the failing commit, image digest, revision, deployment name, UTC
   window, and fixed failure codes. Do not copy response bodies or raw logs.
3. Redeploy the last accepted immutable image digest through the same reviewed
   `app.bicep` path. Do not rebuild an old tag and do not change foundation state
   unless the incident requires it.
4. Database migrations are forward-only during normal rollback. The checked-in
   down migrations are destructive and are allowed only for an explicitly
   selected disposable database or a separately approved closed-ingress
   rollback following [`../migrations/README.md`](../migrations/README.md).
5. Rerun migration, readiness, authentication, persona, log-boundary, and
   synthetic workflow checks. Reopen ingress only after the incident commander
   records a go decision.

## Persona provisioning and offboarding

Authorization is the intersection of an Entra app-role assignment and an active
PostgreSQL membership for the same tenant, object ID, and practice:

| Persona | Role | Visible tabs | Card Chase mutation |
|---|---|---|---|
| Provider | `Provider` | Provider only | No |
| Medical coder | `Coder` | Provider, Coder review, Card Chase | No |
| Card Chase staff | `BillingStaff` | Card Chase only | Yes |
| Combined MCD staff | `Coder` and `BillingStaff` | All three | Yes |
| Administrator | `Admin` | All three | Yes, including admin gates |

Set **Assignment required = Yes** on the Entra enterprise application and use
immutable Entra object IDs. Run the existing owner-only membership command from
the private migration job; never pass a DSN in the override:

```bash
az containerapp job start \
  --resource-group <resource-group> \
  --name <app-output-migrationJobName> \
  --container-name migrate \
  --command "python" \
  --args "-m" "cosmaneura_billing.memberships" \
    "<immutable-entra-object-uuid>" "--role" "<Provider|Coder|BillingStaff|Admin>"
```

For offboarding, rerun the same command with the member's reviewed role set and
append `"--disable"`; also remove the Entra assignment and revoke active Entra
sessions. Verify the job succeeded:

```bash
az containerapp job execution list \
  --resource-group <resource-group> \
  --name <app-output-migrationJobName> \
  --output table
```

Then test each persona in a private browser session. Record only pass/fail,
HTTP status, test-account label, image digest, and UTC time. The full checks are
in [`PILOT_PERSONA_PROVISIONING.md`](PILOT_PERSONA_PROVISIONING.md).

## Separate Athena credentials

Provider labs and billing/Card Chase are different systems and must never share
an OAuth client, secret, token cache, runtime identity, or Key Vault grant:

| Workload | Repository/owner | Expected read boundary |
|---|---|---|
| Provider lab chatbot/dashboard | Cosmas Sovereign | Certified FHIR R4 `system/DiagnosticReport.read` and `system/Observation.read`; Sovereign `ATHENA_*` configuration only |
| Billing/Card Chase import | This repository | Certified reads where sufficient; otherwise the still-unapproved GET-only use of `athena/service/Athenanet.MDP.*`; `COSMANEURA_ATHENA_*` job configuration only |

Before either is enabled, record client ID, owner, environment, exact scopes,
expiration, and owning vault outside source control, and prove the client IDs
differ. If only one Preview application exists, create the missing application;
do not broaden or reuse the existing credential. See
[`ATHENA_SYSTEM_BOUNDARIES.md`](ATHENA_SYSTEM_BOUNDARIES.md).

For rotation: disable the owning workload, wait for active executions, create a
new vendor-secret version through an approved private channel, update only that
workload's vault secret, verify only its identity can resolve it, restart or
redeploy that workload, run a synthetic Preview acceptance, and only then revoke
the old vendor secret. Record metadata and pass/fail only. Never place a secret
in a command argument, parameter file, ticket, screenshot, or log.

## Billing Athena mock, replay, and checkpoint handling

Run the current mock locally with no live Athena variables or credentials set:

```bash
cd cosmaneura_billing
COSMANEURA_ATHENA_SYNC_ENABLED=true \
COSMANEURA_ATHENA_MODE=mock \
COSMANEURA_PRACTICE_ID=synthetic-pilot \
python -m cosmaneura_billing.athena_sync --once
```

The expected ephemeral result is `ATHENA_MOCK_COMPLETE`, two pages, four
fetched records, four imported records, and zero replayed records. This command
uses an in-memory store and proves the adapter contract without creating
durable data.
Mock mode rejects live connection settings.

Migration `009_source_import_foundation` provides the durable, source-neutral
receipt/checkpoint layer and the dedicated practice/source-bound import role.
Prove durable replay only against a disposable migrated PostgreSQL database:
configure `COSMANEURA_ATHENA_IMPORT_DB_ROLE` and
`COSMANEURA_ATHENA_IMPORT_DATABASE_URL` for that dedicated login, run once and
require four imports, then run again and require zero imports and four exact
replays. A failed page, stale concurrent writer, cursor cycle, or normalizer
mapping-version mismatch must leave the affected page uncommitted.

There is still no approved checkpoint reset, skip, edit, or live-job start
procedure. Live mode returns `ATHENA_LIVE_NOT_IMPLEMENTED`. Keep
`provisionAthenaImportDatabase`, `provisionAthenaSync`, and `enableAthenaSync`
false in the pilot until the external access and live-acceptance gates below
are complete.

## Vendor outage

For the current release, leave Athena disabled; an outage cannot justify
enabling the incomplete live path. After live import exists:

1. Disable `enableAthenaSync`, leave the job manual, and do not alter a cursor or
   replay window by hand.
2. Keep patient messaging and the chase scheduler off. Imported data must never
   trigger contact merely because a vendor recovers.
3. Preserve the last committed checkpoint; never reset or edit it by hand, and
   do not present stale data as fresh. The product still lacks an accepted
   user-facing staleness indicator, so that remains a release blocker for live
   import.
4. Record only fixed error/status codes, attempt counts, and the UTC outage
   window. Do not retain raw vendor errors, URLs, payloads, or identifiers.
5. Resume with one bounded synthetic run, then an exact replay proving zero
   duplicate effects, before restoring the normal cadence.

## PHI-safe log evidence

Before ingress or any external data path is enabled:

```bash
cd cosmaneura_billing
python -m cosmaneura_billing.log_scanner --self-test
python -m cosmaneura_billing.log_scanner \
  --deployment-count-report /secure/path/deployment-log-counts.json
```

The scoped report must bind the exact digest, revision, execution names, and UTC
window; every required stream must have a nonzero row count and zero prohibited
canary matches. Query and aggregate in Log Analytics. Do not download or print
raw `Log_s` values. The current five-source validator covers API, Easy Auth,
migration, chase worker, and platform-system rows. It does not yet cover an
Athena job stream; extend and accept that closed schema before the first live
Athena execution.

## Disposable database restore drill

The current PostgreSQL template retains seven days of backup and has high
availability and geo-redundant backup disabled. The repository has no automated
restore command. Before real PHI:

1. Select a disposable database/environment whose name clearly contains
   `test`; confirm it contains synthetic data only.
2. Record the source server, backup timestamp, target name, expected migration
   ledger, and expected synthetic counts without credentials or row content.
3. Restore to a new isolated target using the reviewed Azure PostgreSQL restore
   procedure. Never overwrite the active pilot database.
4. Point only a controlled migration/acceptance environment at the restored
   target. Run the migration checksum check, readiness, forced-RLS/cross-practice
   tests, persona denials, and synthetic encounter/Card Chase checks.
5. Record measured recovery point, recovery time, count mismatches, and pass/fail.
   Resolve and remove only the disposable target under a separate approved
   cleanup; do not use migration down scripts as a backup test.

Define approved RPO/RTO and decide whether seven-day, non-geo, non-HA storage is
acceptable before real-PHI pilot launch.

## Shutdown/recovery tabletop

Run once with synthetic data before pilot launch:

1. Declare a simulated credential/PHI incident and name the incident commander.
2. Apply the safe-state table: close ingress; disable Athena, scheduler,
   messaging, primary inference, active Language extraction, and Language
   shadow; retain `record_only`.
3. Confirm no scheduled or manual job is running, no new outbound action occurs,
   protected routes are unreachable, and ACR remains sealed.
4. Simulate revoking one persona in both Entra and PostgreSQL and rotating one
   workload-specific Athena credential without exposing it.
5. Restore the last accepted digest, run migrations and all synthetic acceptance
   checks, then reopen only the explicitly approved capabilities.
6. Record timestamps, owner handoffs, evidence locations, unmet RPO/RTO, and the
   go/no-go decision. Any failed control keeps the affected capability off.

## Open release blockers

- Staff Athena OAuth, bounded reads, decoders, explicit field review and billing
  note import are implemented. Retained synthetic Preview evidence covers
  contact/insurance, document listing and a nonempty open-note summary; it does
  not establish a completed source note or hosted availability for training.
  Configure the reviewed credentials, case mappings and applicable source/model
  permissions before enabling the relevant workflow. No EHR writes occur.
- Athena's combined Read/Write grant is used only within the owner's approved
  bounded read scope. It does not establish permission for writes or new cohorts.
- Athena-job log-stream coverage is not in the deployment count-report schema.
- The repository has no automated PostgreSQL restore workflow, and pilot RPO/RTO
  are not approved.
- Live persona acceptance needs the reviewed test-account object IDs and matching
  Entra/database assignments.
- Real chart/model use still requires the applicable service agreements, BAA and
  data-handling approvals, plus the documented model/gold-set acceptance.
