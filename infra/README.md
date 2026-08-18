# Azure pilot deployment

The deployment is split deliberately: create private foundation resources,
build an immutable image in the new registry, then deploy the application and
its one-shot migration job. Nothing in these templates authorizes real PHI.
Keep `externalIngress=false` until P2 authentication is deployed and verified.

## Resources

- VNet-injected Azure Container Apps environment
- Azure Database for PostgreSQL Flexible Server 16 on a delegated subnet
- Premium ACR and Key Vault with public access disabled and private endpoints
- user-assigned managed identity for ACR pull and Key Vault secret access
- Log Analytics and workspace-based Application Insights (P5 wires telemetry)
- a manual Container Apps migration job and the same-origin API/frontend app

## Deployment sequence

1. Copy both `*.parameters.example.json` files to untracked
   `*.parameters.json` files and replace placeholders. Never put a real password
   in a tracked file or command history.
2. Create/select a resource group and deploy `foundation.bicep`. Keep
   `registryPublicNetworkAccess` set to its secure default, `Disabled`.
3. For the first image only, temporarily set
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
4. Deploy `app.bicep` with the exact immutable image tag only after that
   verification.
5. Start the migration job once and require a successful execution before
   considering the app healthy:

   ```bash
   az containerapp job start --resource-group <resource-group> \
     --name <app-output-migrationJobName>
   ```

The API startup performs its own database/schema read before `/api/ready`
returns 200, so a missing migration, unreachable database, or omitted bundled
NCCI/coverage data fails closed. The app has no public ingress at this stage.

Actual Azure deployment creates billable resources and is intentionally not
performed by repository tests. Run it only after reviewing the plan, cost, and
region and explicitly authorizing the external change.
