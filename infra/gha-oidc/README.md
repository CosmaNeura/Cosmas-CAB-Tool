# CAB GitHub Actions OIDC identity

This is the Tier 2 flight plan and independently deployable template for the
dedicated Azure principal used by this repository's `Build & Deploy` workflow.
It does not deploy an application, change a GitHub variable, or dispatch a
workflow. Tier 2 applies because the template defines a production identity and
authorization boundary.

## Defined boundary

`main.bicep` creates one user-assigned managed identity in
`cosmaneura-cab-pilot`. It has no client secret and trusts exactly these GitHub
OIDC subjects:

- `repo:CosmaNeura/Cosmas-CAB-Tool:ref:refs/heads/main` for `build-publish`
- `repo:CosmaNeura/Cosmas-CAB-Tool:environment:pilot` for `deploy`

The identity receives exactly four role assignments:

| Target | Built-in role | Why |
|---|---|---|
| ACR `cosmaneurapiloti4akwqry` | `AcrPush` | Push the reviewed image |
| Container App `cosmaneura-pilot-api-i4akwqry` | `Container Apps Contributor` | Read and update the API image |
| Container App Job `cosmaneura-pilot-mig-i4akwqry` | `Container Apps Jobs Contributor` | Read, update, and start the migration job |
| Container App Job `cosmaneura-pilot-wrk-i4akwqry` | `Container Apps Jobs Contributor` | Read and update the worker image |

The built-in `Container Apps Operator` and `Container Apps Jobs Operator` roles
do not grant the write operations used by the workflow. The contributor roles
above are the narrowest built-ins that do, and every assignment is scoped to
one exact resource rather than the resource group.

No runtime managed identity is reused. No role is assigned to the Container
Apps environment, subscription, or resource group. This template does not
alter `CAB_DEPLOY_ENABLED` or any patient-messaging, inference, Athena, Stripe,
or application setting.

## Current-state reconciliation (2026-08-30)

- The target resource group, registry, API app, migration job, and worker job
  exist in subscription `bab2a709-1fcb-4c87-a9fc-802f7477ec13`; no dedicated
  CAB GitHub Actions identity exists.
- The repository's current `AZURE_CLIENT_ID` points to a non-CAB principal. It
  is intentionally not changed before the new identity exists. Do not dispatch
  the workflow in this state.
- Both CAB subjects also exist on a principal owned outside this repository.
  Removing those foreign credentials requires a separate reviewed cleanup and
  is a blocker for the client-ID handoff.
- `CAB_DEPLOY_ENABLED` is `true` and remains unchanged.
- The registry has public network access disabled and a private endpoint. This
  identity resolves authorization only, not runner network reachability.

## Review and plan (non-mutating)

Use the same tenant and subscription already selected by Azure CLI, then run:

```bash
az bicep build --file infra/gha-oidc/main.bicep --stdout >/dev/null
az deployment group what-if \
  --name cab-gha-oidc \
  --resource-group cosmaneura-cab-pilot \
  --template-file infra/gha-oidc/main.bicep \
  --result-format FullResourcePayloads
```

The plan must contain one identity, two federated credentials, and four scoped
role assignments. It must contain no deletion or modification and no resource
outside `cosmaneura-cab-pilot`.

Abort if the plan differs from that seven-create contract, if any target ID is
outside the named resource group, or if either OIDC subject differs by case or
content. A plan does not authorize an Apply.

## Ordered rollout and controls

1. Merge only after CI, the what-if, independent-agent review tied to the exact
   commit, and required human review are complete.
2. For Apply, use a clean checkout of that reviewed commit after it is merged
   and verify it is the exact commit currently at `origin/main`. If `main`
   advances or the tree changes, stop and repeat review.
3. Immediately rerun the seven-create/no-change what-if from that exact commit.
   A human reviews the fresh plan and explicitly names **Apply** in a new
   instruction.
4. Apply only this template to `cosmaneura-cab-pilot`; do not deploy either
   application template in the same operation.
5. For 15 minutes after Apply, dispatch nothing. At the start and end of that
   window, verify the identity, its exact two subjects, and its exact four role
   assignments. Abort handoff on any mismatch or broader assignment.
6. Complete the separately reviewed removal of the two CAB subjects from the
   foreign principal and verify no other principal trusts them.
7. Set this repository's `AZURE_CLIENT_ID` to the new `azureClientId` output.
8. Resolve and verify private ACR reachability from the selected runner.
9. Only then may a human explicitly authorize one new `Build & Deploy`
   dispatch. Never reuse an earlier run.

Owner: the repository and Azure owner (`JohnClem`) names Apply, approves the
client-ID handoff, and separately authorizes any later dispatch. There is no
release flag, cohort, schema/data rung, cross-repository contract, or security
risk acceptance in this change. The allowed boundary remains synthetic-only,
non-PHI planning until those later approvals; because no risk is accepted,
there is no acceptance expiry.

Completion means both read-only inventories match, foreign subject reuse is
absent, the CAB repository points to only the dedicated client ID, and the ACR
network path is proven. A successful identity deployment alone is not rollout
completion.

## Approval, application, and handoff

Stop after planning. Do not run an Azure deployment until the human explicitly
names **Apply**. Applying this template creates the identity and RBAC but does
not change GitHub.

After an approved Apply succeeds, read the `azureClientId` deployment output
and set this repository's `AZURE_CLIENT_ID` variable to that new value. Do not
copy a client ID from another repository and do not use an API, migration, or
worker runtime identity. `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` are a
separate repository-variable handoff and are not managed here.

Before that handoff, verify both CAB subjects exist only on this dedicated
identity. Removing a CAB subject from any foreign principal is a separate,
explicitly reviewed cleanup; this template intentionally cannot mutate an
identity owned by another repository.

Only after the identity, both federated credentials, all four role assignments,
and the repository variables are verified may a human explicitly authorize a
new `Build & Deploy` dispatch. Never rerun workflow run `33274587589`.

## Recovery

Before removing the identity, disable its use by clearing or replacing this
repository's `AZURE_CLIENT_ID`. Then, under a separately approved recovery
operation, verify no workflow is running and execute the following exact order.
Before each delete, show that exact ID or name; skip it only when Azure confirms
`ResourceNotFound`, which makes a repeated recovery safe.

```bash
az role assignment delete --ids /subscriptions/bab2a709-1fcb-4c87-a9fc-802f7477ec13/resourceGroups/cosmaneura-cab-pilot/providers/Microsoft.ContainerRegistry/registries/cosmaneurapiloti4akwqry/providers/Microsoft.Authorization/roleAssignments/ae5c3238-5618-56eb-a561-b1c163ed53f4
az role assignment delete --ids /subscriptions/bab2a709-1fcb-4c87-a9fc-802f7477ec13/resourceGroups/cosmaneura-cab-pilot/providers/Microsoft.App/containerApps/cosmaneura-pilot-api-i4akwqry/providers/Microsoft.Authorization/roleAssignments/60653f6c-6d62-5d4a-ba40-eb80cee8bcc0
az role assignment delete --ids /subscriptions/bab2a709-1fcb-4c87-a9fc-802f7477ec13/resourceGroups/cosmaneura-cab-pilot/providers/Microsoft.App/jobs/cosmaneura-pilot-mig-i4akwqry/providers/Microsoft.Authorization/roleAssignments/539042eb-4029-5c40-ad32-f2e9a1853b30
az role assignment delete --ids /subscriptions/bab2a709-1fcb-4c87-a9fc-802f7477ec13/resourceGroups/cosmaneura-cab-pilot/providers/Microsoft.App/jobs/cosmaneura-pilot-wrk-i4akwqry/providers/Microsoft.Authorization/roleAssignments/7a09235b-a2b3-5bfe-992a-bbda3984f612

az identity federated-credential delete --resource-group cosmaneura-cab-pilot --identity-name cosmaneura-pilot-gha-identity-i4akwqry --name github-pilot-deploy --yes
az identity federated-credential delete --resource-group cosmaneura-cab-pilot --identity-name cosmaneura-pilot-gha-identity-i4akwqry --name github-main-build-publish --yes
az identity delete --resource-group cosmaneura-cab-pilot --name cosmaneura-pilot-gha-identity-i4akwqry
```

Verify all seven resources are absent after recovery. Removing this template
from a later incremental ARM deployment does not delete resources that were
already created.
