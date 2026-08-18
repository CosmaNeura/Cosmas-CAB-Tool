# Azure pilot deployment

The deployment is split deliberately: create private foundation resources,
build an immutable image in the new registry, then deploy the application, its
one-shot migration job, and the isolated chase-worker job. Nothing in these
templates authorizes real PHI.
Keep `externalIngress=false` until Entra authentication, role assignment, and
the spoof-resistance acceptance checks are deployed and verified.

## Resources

- VNet-injected Azure Container Apps environment
- Azure Database for PostgreSQL Flexible Server 16 on a delegated subnet
- Premium ACR and Key Vault with public access disabled and private endpoints
- separate user-assigned identities for the API, migration job, and chase
  worker, each with ACR pull and only its required Key Vault secret grants
- Log Analytics and workspace-based Application Insights (P5 wires telemetry)
- a manual Container Apps migration job, the same-origin API/frontend app, and
  a no-ingress chase-worker job that is manual by default and scheduled only
  when explicitly enabled

## P4 parameters and secret boundary

Keep `namePrefix`, `postgresAppLogin`, and `postgresChaseWorkerLogin` identical
between the foundation and app parameter files. The worker login must differ
from both the PostgreSQL administrator and API runtime logins.

The foundation owns credentials and creates these additional P4 inputs:

- `postgresChaseWorkerLogin` and secure `postgresChaseWorkerPassword`
- secure `chaseGateHmacKey` (at least 32 bytes)
- optional secure `acsEmailConnectionString`, empty until the real-email gates
  are satisfied

The app deployment owns behavior and receives these additional P4 inputs:

- `postgresChaseWorkerLogin`, `enableChaseScheduler`,
  `enablePatientMessaging`, and `activatePatientMessaging`
- non-secret `acsEmailEndpoint`, `acsEmailSenderAddress`, and
  `acsEmailReplyToAddress`, which are fingerprinted identically by the API and
  worker; the endpoint must match the endpoint in the worker credential
- `practiceTimezone`, `contactSnapshotMaxAgeHours`,
  `chaseWorkerBatchSize`, `chaseWorkerLeaseSeconds`, and
  `chaseWorkerCronExpression`
- `uploadUrl`, `directContractListPath`, and `chaseApprovalMode`, which are also
  part of the server-configured policy/evidence boundary

The two patient-messaging latches must change together. Enabling messaging also
requires the scheduler and all ACS metadata; otherwise Bicep fails before
deployment. With messaging disabled the transport is always `record_only`.

Secret access is intentionally partitioned:

| Principal | May read | Must not read |
|---|---|---|
| API identity | `database-url`, Entra client secret, `chase-gate-hmac-key` | migration-owner DSN, runtime/worker DB passwords, worker DSN, ACS connection string |
| Migration identity | `migration-database-url`, `database-runtime-password`, `chase-worker-database-password` | API DSN, worker DSN, gate key, ACS connection string |
| Chase-worker identity | `chase-worker-database-url`, `chase-gate-hmac-key`, and (only when configured) `acs-email-connection-string` | API/owner DSNs, raw DB passwords, Entra client secret |

The gate key is the only Key Vault secret shared by API and worker. ACS
endpoint/sender/reply values are shared non-secret fingerprint metadata; the
ACS connection string is worker-only. Do not compensate for a missing secret
grant with a vault-scoped role assignment.

## Deployment sequence

1. Create a single-tenant Microsoft Entra app registration for staff. Define
   the `Admin`, `BillingStaff`, `Coder`, and `Provider` app roles, set the
   enterprise application to **Assignment required = Yes**, and assign only
   the pilot staff/groups. Enable ID-token issuance (the Container Apps
   server-directed sign-in requires it; with Azure CLI:
   `az ad app update --id <application-client-id> --enable-id-token-issuance true`).
   Create a client secret, retain it in the approved password manager, and
   never put it in a tracked file or shell history.
2. Copy both `*.parameters.example.json` files to untracked
   `*.parameters.json` files and replace placeholders. Never put a real password
   or client secret in a tracked file or command history. The app template's
   secret parameter is secure and writes the value directly to Key Vault; the
   application container never receives it as an environment variable. Supply
   distinct PostgreSQL administrator, API, and chase-worker login names and
   passwords (the foundation rejects conflicting login names), generate a
   stable random `chaseGateHmacKey` of at least 32 bytes, and set
   `initialAdminObjectId` to the reviewed immutable Entra object UUID of the
   first pilot administrator—not an email address or display name. Keep
   `acsEmailConnectionString` and both app messaging latches empty/false for the
   initial synthetic deployment.
3. Create/select a resource group and deploy `foundation.bicep`. Keep
   `registryPublicNetworkAccess` set to its secure default, `Disabled`. This
   deployment must precede `app.bicep`: it creates all three identities, the
   worker DSN/password secrets, the shared gate-key secret, and their
   secret-scoped grants. An ACS secret/grant is created only when the foundation
   receives a non-empty connection string.
4. For the first image only, temporarily set
   `registryPublicNetworkAccess` to `Enabled`, redeploy the foundation, and
   build from the repository root with ACR Tasks:

   ```bash
   az acr build --registry <foundation-output-registryName> \
     --image cosmaneura-billing:<immutable-tag> \
     --file cosmaneura_billing/Dockerfile cosmaneura_billing
   ```

   ACR Tasks are not on Azure's trusted-services list for a registry whose
   public endpoint is disabled. The temporary endpoint still requires Azure
   authentication, and the registry admin account remains disabled. As soon as
   the build completes, restore `registryPublicNetworkAccess` to `Disabled`,
   redeploy `foundation.bicep`, and verify the registry reports
   `publicNetworkAccess: Disabled`. Do not proceed while it is enabled.
5. Deploy `app.bicep` with the exact immutable image tag,
   `externalIngress=false`, `enableChaseScheduler=false`, and both messaging
   latches false only after that verification. Add the output FQDN as the Entra
   web redirect URI:
   `https://<app-fqdn>/.auth/login/aad/callback`.

   The chase job is still deployed in this state, but its trigger is `Manual`
   and its worker rejects execution while the scheduler is disabled. Setting
   `enableChaseScheduler=true` changes the same no-ingress job to `Schedule`
   with the configured UTC cron; it does not create a second job. Messaging
   remains `record_only` unless both messaging latches are also true.

   If this updates a foundation deployed before the API and migration identities
   were separated, an incremental Bicep deployment does **not** remove the old
   vault-scoped role assignment. First verify that the API identity has
   secret-scoped `Key Vault Secrets User` assignments on only `database-url`,
   the Entra client secret, and `chase-gate-hmac-key`; the migration identity
   has them on only `migration-database-url`, `database-runtime-password`, and
   `chase-worker-database-password`; and the chase identity has only the worker
   DSN, gate key, and optional ACS secret grants described above. Then resolve
   the old assignment exactly and remove only that vault-scoped grant:

   ```bash
   az role assignment list \
     --assignee-object-id <api-identity-principal-id> \
     --scope <key-vault-resource-id> \
     --role "Key Vault Secrets User" \
     --query "[].{id:id,scope:scope}" --output table

   az role assignment delete \
     --ids <verified-legacy-vault-scope-role-assignment-id>
   ```

   Do not delete any of the new secret-scoped assignments. A fresh deployment
   has no legacy grant to remove.
6. Start the migration job once and require a successful execution before
   considering the app healthy:

   ```bash
   az containerapp job start --resource-group <resource-group> \
     --name <app-output-migrationJobName>
   ```

   The job receives the migration-owner DSN and the separate API/worker role
   passwords. It applies checksum-locked migrations, provisions/rotates both
   least-privilege `postgresAppLogin` and `postgresChaseWorkerLogin`, binds each
   runtime role to the configured practice, binds the practice to the Entra
   tenant, and creates only the explicitly named initial `Admin` membership.
   The API receives only the runtime DSN; the chase job receives only the worker
   DSN. Never copy the owner DSN or either runtime password into application
   configuration.

The API startup performs its own database/schema and access-audit checks before `/api/ready`
returns 200, so a missing migration, unreachable database, or omitted bundled
NCCI/coverage data fails closed. The app has no public ingress at this stage.

7. After staff assignments and the redirect URI are complete, redeploy with
   `externalIngress=true` and immediately run the acceptance checks below. If
   any protected request reaches the application without the expected result,
   restore `externalIngress=false` before debugging.

8. Validate the scheduler with synthetic/de-identified rows before considering
   any messaging configuration. Redeploy `app.bicep` with
   `enableChaseScheduler=true` while both messaging latches remain false. This
   changes the job to `Schedule`, but keeps `COSMANEURA_CHASE_TRANSPORT` at
   `record_only` and supplies no ACS credential. Because scheduler state is
   fingerprinted, create, approve, and activate the newly configured durable
   policy as an `Admin`; an older approval cannot authorize it. Post only
   synthetic coverage snapshots, confirm durable detection returns counts, and
   fetch the resulting rows from the worklist. A deployment operator may start
   one bounded execution immediately rather than wait for cron:

   ```bash
   az containerapp job start --resource-group <resource-group> \
     --name <app-output-chaseWorkerJobName>
   ```

   Require a successful execution, PHI-free count/status logs, no claimed ACS
   delivery, and the expected durable manual-phone task/cadence behavior. The
   absence of an email is the expected result. If an unresolved `ambiguous`
   delivery exists, the worker must exit non-zero and perform no new outreach;
   investigate rather than rerun it blindly.

## Enabling ACS email

Real patient email remains prohibited until the Microsoft/Azure BAAs are in
place, P5 logging and redaction are deployed and verified, deployment/auth
acceptance is green, the secure upload route is approved, and the practice has
completed operational review. Until then use synthetic/de-identified recipients
only and keep both messaging latches false.

When those gates are satisfied, use this order:

1. Revoke the currently active policy while its exact configuration is still
   loaded. Then pause the chase schedule by redeploying the app with
   `enableChaseScheduler=false` and both messaging latches false. Wait for any
   active execution to finish.
2. Redeploy the **foundation first** with the reviewed
   `acsEmailConnectionString`. Verify Key Vault contains
   `acs-email-connection-string` and only the chase-worker identity has its
   secret-scoped reader assignment. The API and migration identities must not
   gain it.
3. Redeploy the app with `enableChaseScheduler=true`, both messaging latches
   true, and the reviewed ACS endpoint/sender/reply metadata. The connection
   string's endpoint must exactly match `acsEmailEndpoint`; the worker rejects a
   mismatch. The API receives only the metadata, never the credential.
4. Treat the resulting policy fingerprint as new. Create its immutable policy
   revision, have a named `Admin` approve it, and activate that exact approval.
   Transport, scheduler/latch state, endpoint/sender/reply, templates, upload
   URL, cadence, timezone, and approval mode changes all require a new exact
   approval; never edit or reuse an older approval.
5. Run a controlled synthetic/de-identified email through the full acceptance
   checks before authorizing real recipients. Confirm the worker records one
   accepted provider operation and never retries an ambiguous one.

Deploying the app before the foundation secret/grant exists will leave the job
with an unresolved Key Vault reference. Do not broaden Key Vault access to make
that ordering error disappear.

## Authentication acceptance checks

Run these against the Container Apps FQDN, not a direct local container:

- `/api/live` and `/api/ready` are anonymous; `/api/health` returns 401 when
  signed out.
- A caller-supplied, syntactically valid `X-MS-CLIENT-PRINCIPAL` (including the
  configured tenant and an allowed role) still returns 401 when signed out.
  This proves the Container Apps auth sidecar is the only network path and
  strips/overrides spoofed trusted headers.
- An unassigned tenant user cannot enter the app. An assigned staff user can
  sign in at `/.auth/login/aad?post_login_redirect_uri=/` and receives only the
  API permissions for their app role.
- A `Provider` or `BillingStaff` receives 403 from coder feedback; a `Coder`
  receives 403 from chase actions; only `Admin` can read feedback summary.
- After `/.auth/logout`, protected requests return 401 again.

The application also validates tenant, immutable object ID, and app role. That
second check does not make trusted headers safe on a network path that bypasses
Container Apps. See `../cosmaneura_billing/docs/ACCESS_CONTROL.md`.

## P4 scheduler acceptance checks

Perform these checks with synthetic/de-identified data before enabling any real
email:

- Foundation outputs include the third chase-worker identity, and app outputs
  include `chaseWorkerJobName`, `chaseSchedulerEnabled`, and
  `patientMessagingEnabled` with the expected values.
- The worker job has the chase-worker UAMI, no ingress, parallelism `1`, retry
  limit `0`, and command
  `python -m cosmaneura_billing.chase.worker --once`. Its trigger is `Manual`
  when `chaseSchedulerEnabled=false` and `Schedule` with the reviewed UTC cron
  when true.
- List secret names/references and role assignments without printing secret
  values. The API, migration, and worker partitions must match the table above;
  no identity has a vault-scoped `Key Vault Secrets User` grant. In
  `record_only`, the worker job must have no `acs-email-connection-string`
  reference and the API must never have one.
- The migration execution succeeds with migrations `004`–`006 applied, and
  both runtime roles are practice-bound. The API role cannot claim/finalize
  outbox deliveries; the worker role cannot read identity, encounter, feedback,
  or access-audit tables or create policy approvals/releases.
- With messaging disabled, app and worker report scheduler enabled,
  `record_only`, and both patient-messaging latches false. An Admin can register,
  approve, and activate the exact server-configured policy. Durable detection
  returns only `checked`/`detected` counts; the authenticated worklist then shows
  the synthetic row.
- A bounded worker execution succeeds without an ACS call. A due manual-phone
  rung creates a durable staff task; logging that exact task records its minutes
  and advances cadence once. Replaying the same action is idempotent.
- Seed or exercise the ambiguous-submission fixture only in a controlled
  synthetic environment. The execution must stop non-zero before scheduling or
  sending new work, and the ambiguous delivery must not return to the send lane.
- When email is eventually enabled, the worker alone has the ACS credential,
  API/worker fingerprint metadata matches, the active approval matches the new
  fingerprint, and a controlled synthetic email is accepted exactly once.

Useful read-only inspection commands include:

```bash
az containerapp job show --resource-group <resource-group> \
  --name <app-output-chaseWorkerJobName> \
  --query '{trigger:properties.configuration.triggerType,identity:identity.userAssignedIdentities,secrets:properties.configuration.secrets[].name,command:properties.template.containers[0].command,args:properties.template.containers[0].args}'

az containerapp job execution list --resource-group <resource-group> \
  --name <app-output-chaseWorkerJobName> --output table
```

Do not query or print Key Vault secret values during acceptance.

## State, membership, and credential boundary

Staging and production run with `COSMANEURA_STATE_STORE=postgres` and
`COSMANEURA_CHASE_STORE=postgres`. Encounters, feedback, adaptive-preference
source rows, explicit memberships, access audit, and chase state share the
runtime `DATABASE_URL`; a separate chase database is intentionally rejected so
a mutation and its audit completion can commit atomically. JSONL and in-memory
stores are local development/test seams only.

Entra assignment is necessary but not sufficient for access. There is no
just-in-time membership creation: the authenticated token roles are intersected
with an active database membership for the same tenant, object ID, and
practice. After bootstrap, an operator adds, changes, or disables members only
through `python -m cosmaneura_billing.memberships` with the migration-owner DSN
from a private, controlled execution environment. See
`../cosmaneura_billing/migrations/README.md` for the exact command and role
rules. A missing/inactive member is denied, and a database outage fails closed.

The API and worker runtime logins are deliberately unable to bypass row-level
security, change schema, or manage roles; their table/function permissions are
different and non-interchangeable. The migration owner is confined to the
manual job and controlled membership administration. Owner, API, and worker
DSNs plus both runtime-role passwords remain separate Key Vault secrets. All
three workloads use different managed identities with secret-scoped grants, so
the API identity cannot read the owner/worker credentials and the worker cannot
read API/owner credentials.

## Rotation and decommissioning

Treat database-password and gate-key changes as maintenance operations, not
ordinary parameter edits.

For an administrator, API, or worker database-password rotation:

1. Revoke the active chase policy, redeploy the app with the scheduler and both
   messaging latches false, wait for active worker executions to finish, and
   make ingress private if rotating the administrator or API credential.
2. Redeploy the foundation with the new password. This updates the Key Vault
   DSN/password versions before the application role is necessarily rotated, so
   a temporary readiness/connection failure is expected and must not be hidden.
3. Wait for and verify the versionless Container Apps Key Vault references have
   refreshed to the new foundation secret versions without reading their
   values. If the platform requires a new app/job revision to refresh them,
   redeploy the reviewed app template in the same maintenance-safe state. Do not
   treat the API as ready yet.
4. Immediately start the migration job. This validates the rotated owner
   connection and rotates the exact API and worker PostgreSQL roles to their
   configured passwords.
5. Redeploy the app once more, require migration success, `/api/ready`, and one
   synthetic `record_only` worker execution, then restore ingress/scheduling.
   Reapprove the resulting policy if the pause/resume changed its fingerprint.

Do not rotate a password by changing a login name. The migration runner creates
or rotates the configured role but does not silently drop an old login; a name
change therefore requires a separate reviewed privilege/connection audit and
explicit retirement of the old role.

For a gate-key rotation, revoke the active policy first, stop the schedule, and
wait for all executions. Redeploy the foundation with one new key, then
redeploy the app so API and worker move together. Refresh synthetic
detection/direct-contract evidence and review every existing direct-contract
release: the old keyed evidence must not authorize the new gate. The key itself
is not patient data, but running API and worker with different versions fails
exact-evidence checks. Any accompanying change to scheduler/latches, policy,
transport, templates, ACS metadata, upload URL, cadence, timezone, or approval
mode changes the policy fingerprint and requires a new immutable revision plus
named Admin approval/activation.

Incremental Azure deployments do not remove resources that disappear or become
conditional. In particular:

- `enableChaseScheduler=false` changes the existing worker job to `Manual`; it
  does not delete the job, worker identity, ACR pull grant, database role, or
  durable chase state.
- Clearing `acsEmailConnectionString` later does not reliably delete the prior
  Key Vault secret or its secret-scoped worker assignment in incremental mode.
- Old vault-scoped grants, superseded identities/role assignments, and an old
  PostgreSQL login can survive a template update until explicitly removed.
- Migrations, outbox/events, attempts, and audit history are durable records;
  disabling or decommissioning the worker must not drop them without a reviewed
  retention/export decision.

For decommissioning, revoke the active policy, disable messaging and scheduling,
wait for all executions, reconcile every ambiguous/in-flight delivery, remove
job-start RBAC, and then resolve and delete only the exact worker job, identity,
secret assignments/secrets, ACR grant, and database login approved for removal.
Remove the job from the managed template first or a later app deployment will
recreate it. Preserve the API gate key if the direct-contract API remains in
service. Never use broad resource-group, vault, or role-assignment deletion as
cleanup.

### Private membership administration

PostgreSQL has no public endpoint. Use the existing migration job as the
controlled private execution environment for reviewed membership changes; it
already has the owner DSN through its migration-only identity. This per-run
override leaves the checked-in job template unchanged:

```bash
az containerapp job start \
  --resource-group <resource-group> \
  --name <app-output-migrationJobName> \
  --container-name migrate \
  --command "python" \
  --args "-m" "cosmaneura_billing.memberships" \
    "<immutable-entra-object-uuid>" "--role" "Coder"
```

Repeat the `"--role" "<role>"` pair for another reviewed role. To disable the
membership, include its reviewed role set and append `"--disable"`. Require a
successful job execution before telling the staff member access is available.
Never place a DSN or password in the override; the job receives its secrets by
reference.

```bash
az containerapp job execution list \
  --resource-group <resource-group> \
  --name <app-output-migrationJobName> \
  --output table
```

Permission to start a Container Apps job also permits command, image, and
environment overrides. Grant it only to the small deployment-operator group.
The migration job has the database-owner credential; the chase job has the
worker DSN, gate key, and potentially the ACS credential. Either can be abused
through an override even though its checked-in command is narrow. Do not grant
job-start/override rights to the API identity or ordinary staff, and review
every manual or scheduled execution plus every override in Azure activity and
job-execution logs.

Actual Azure deployment creates billable resources and is intentionally not
performed by repository tests. Run it only after reviewing the plan, cost, and
region and explicitly authorizing the external change.
