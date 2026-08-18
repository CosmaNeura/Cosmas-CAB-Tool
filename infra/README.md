# Azure pilot deployment

The deployment is split deliberately: create private foundation resources,
build an immutable image in the new registry, then deploy the application and
its one-shot migration job. Nothing in these templates authorizes real PHI.
Keep `externalIngress=false` until Entra authentication, role assignment, and
the spoof-resistance acceptance checks are deployed and verified.

## Resources

- VNet-injected Azure Container Apps environment
- Azure Database for PostgreSQL Flexible Server 16 on a delegated subnet
- Premium ACR and Key Vault with public access disabled and private endpoints
- user-assigned managed identity for ACR pull and Key Vault secret access
- Log Analytics and workspace-based Application Insights (P5 wires telemetry)
- a manual Container Apps migration job and the same-origin API/frontend app

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
   application container never receives it as an environment variable.
3. Create/select a resource group and deploy `foundation.bicep`. Keep
   `registryPublicNetworkAccess` set to its secure default, `Disabled`.
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
5. Deploy `app.bicep` with the exact immutable image tag and
   `externalIngress=false` only after that verification. Add the output FQDN as
   the Entra web redirect URI:
   `https://<app-fqdn>/.auth/login/aad/callback`.
6. Start the migration job once and require a successful execution before
   considering the app healthy:

   ```bash
   az containerapp job start --resource-group <resource-group> \
     --name <app-output-migrationJobName>
   ```

The API startup performs its own database/schema and access-audit checks before `/api/ready`
returns 200, so a missing migration, unreachable database, or omitted bundled
NCCI/coverage data fails closed. The app has no public ingress at this stage.

7. After staff assignments and the redirect URI are complete, redeploy with
   `externalIngress=true` and immediately run the acceptance checks below. If
   any protected request reaches the application without the expected result,
   restore `externalIngress=false` before debugging.

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

Actual Azure deployment creates billable resources and is intentionally not
performed by repository tests. Run it only after reviewing the plan, cost, and
region and explicitly authorizing the external change.
