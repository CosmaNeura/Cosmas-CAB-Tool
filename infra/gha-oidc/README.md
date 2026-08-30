# CAB GitHub Actions OIDC identity

This is the Tier 2 flight plan and independently deployable template for the
dedicated Azure principal used by this repository's `Build & Deploy` workflow.
It does not deploy an application, change a GitHub variable, or dispatch a
workflow.

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

The registry currently has public network access disabled and a private
endpoint. This identity changes authorization only; it does not make a
GitHub-hosted runner network-reachable. Resolve and verify the runner-to-ACR
network path in a separately reviewed change before expecting image publish to
succeed.

## Recovery

Before removing the identity, disable its use by clearing or replacing this
repository's `AZURE_CLIENT_ID`. Then, under a separately approved recovery
operation, remove the four exact role assignments, the two federated
credentials, and finally `cosmaneura-pilot-gha-identity-i4akwqry`. Verify no
workflow is running first. Removing this template from a later incremental ARM
deployment does not delete resources that were already created.
