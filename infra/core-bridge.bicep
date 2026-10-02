// Separate app-only Core receiver. No browser/session or outreach authority.
targetScope = 'resourceGroup'
param location string
param environmentName string
param registryName string
param keyVaultName string
param appName string
param identityName string
param image string
param practiceId string
param tenantId string
@minLength(1)
param databaseRole string
@minLength(1)
param databaseSecretName string
param reviewSigningSecretName string
@secure()
@maxLength(8192)
param policyJson string = ''
@secure()
@maxLength(8192)
param modelDataPolicyJson string = ''
param deploymentId string = ''
param storageId string = ''
param externalIngress bool = false
param enableInference bool = false
param azureOpenAIAccountName string
param azureOpenAIDeployment string
param azureOpenAIModel string
param azureOpenAIApiVersion string
param tags object = {}

var forbiddenDatabaseSecrets = ['database-url', 'migration-database-url', 'chase-worker-database-url', 'athena-import-database-url']
var validatedDatabaseSecret = contains(forbiddenDatabaseSecrets, toLower(databaseSecretName)) || toLower(databaseSecretName) == toLower(reviewSigningSecretName)
  ? fail('Core bridge requires its dedicated database secret, separate from API, owner, worker, importer and signer')
  : databaseSecretName
var validatedIngress = externalIngress && empty(policyJson)
  ? fail('Core bridge public ingress requires its exact workload policy')
  : externalIngress
var validatedInference = enableInference && (empty(policyJson) || empty(modelDataPolicyJson) || empty(deploymentId) || empty(storageId))
  ? fail('Core bridge inference requires separate exact workload/model policies and deployment/storage references')
  : enableInference
var validatedPolicy = empty(policyJson) ? ''
  : json(policyJson).managedIdentityClientId != identity.properties.clientId
    ? fail('Core bridge policy managed identity must match this dedicated service')
    : policyJson
var healthPath = empty(policyJson) ? '/api/live' : '/api/ready'
var secretReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = { name: environmentName }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: keyVaultName }
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}
resource imageReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, 'acrpull')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource databaseSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: validatedDatabaseSecret
}
resource signingSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: reviewSigningSecretName
}
resource databaseReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: databaseSecret
  name: guid(databaseSecret.id, identity.id, 'key-vault-secrets-user')
  properties: { principalId: identity.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}
resource signingReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: signingSecret
  name: guid(signingSecret.id, identity.id, 'key-vault-secrets-user')
  properties: { principalId: identity.properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}
resource azureOpenAI 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (validatedInference) {
  name: azureOpenAIAccountName
}
resource modelReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (validatedInference) {
  scope: azureOpenAI
  name: guid(azureOpenAI.id, identity.id, 'cognitive-services-openai-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}
resource application 'Microsoft.App/containerApps@2026-01-01' = {
  name: appName
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identity.id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: validatedIngress, allowInsecure: false, targetPort: 8000, transport: 'http', traffic: [{ latestRevision: true, weight: 100 }] }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [
        { name: 'bridge-database-url', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedDatabaseSecret}', identity: identity.id }
        { name: 'review-signing-key', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/${reviewSigningSecretName}', identity: identity.id }
      ]
    }
    template: {
      containers: [{
        name: 'core-bridge'
        image: image
        command: ['python']
        args: ['-m', 'uvicorn', 'cosmaneura_billing.core_bridge:app', '--host', '0.0.0.0', '--port', '8000', '--no-access-log']
        env: concat([
          { name: 'DATABASE_URL', secretRef: 'bridge-database-url' }
          { name: 'COSMANEURA_DB_RUNTIME_ROLE', value: databaseRole }
          { name: 'COSMANEURA_ENV', value: 'production' }
          { name: 'COSMANEURA_STATE_STORE', value: 'postgres' }
          { name: 'COSMANEURA_CHASE_STORE', value: 'postgres' }
          { name: 'COSMANEURA_AUTH_MODE', value: 'container_apps' }
          { name: 'COSMANEURA_ENTRA_TENANT_ID', value: tenantId }
          { name: 'COSMANEURA_PRACTICE_ID', value: practiceId }
          { name: 'COSMANEURA_ALLOWED_ROLES', value: 'Provider,Admin' }
          { name: 'COSMANEURA_BILLING_HEADER_ENABLED', value: 'true' }
          { name: 'COSMANEURA_PROVIDER_HANDOFF_ENABLED', value: 'true' }
          { name: 'COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY', secretRef: 'review-signing-key' }
          { name: 'COSMANEURA_CORE_BRIDGE_POLICY_JSON', value: validatedPolicy }
          { name: 'COSMANEURA_MODEL_DATA_POLICY_JSON', value: modelDataPolicyJson }
          { name: 'COSMANEURA_ATHENA_DEPLOYMENT_ID', value: deploymentId }
          { name: 'COSMANEURA_ATHENA_STORAGE_ID', value: storageId }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'COSMANEURA_AZURE_OPENAI_ENABLED', value: string(validatedInference) }
          { name: 'COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY', value: 'false' }
          { name: 'COSMANEURA_AZURE_LANGUAGE_ENABLED', value: 'false' }
          { name: 'COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED', value: 'false' }
          { name: 'COSMANEURA_ATHENA_REVIEW_MODE', value: 'disabled' }
          { name: 'COSMANEURA_COMMUNICATIONS_ENABLED', value: 'false' }
          { name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED', value: 'false' }
          { name: 'COSMANEURA_PATIENT_MESSAGING_ENABLED', value: 'false' }
          { name: 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED', value: 'false' }
          { name: 'COSMANEURA_CHASE_TRANSPORT', value: 'record_only' }
          { name: 'CPT_AUTO_FETCH', value: 'false' }
        ], validatedInference ? [
          { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAI!.properties.endpoint }
          { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAIDeployment }
          { name: 'AZURE_OPENAI_MODEL', value: azureOpenAIModel }
          { name: 'AZURE_OPENAI_API_VERSION', value: azureOpenAIApiVersion }
        ] : [])
        resources: { cpu: json('0.5'), memory: '1Gi' }
        probes: [
          { type: 'Startup', httpGet: { path: healthPath, port: 8000, scheme: 'HTTP' }, initialDelaySeconds: 5, periodSeconds: 5, timeoutSeconds: 4, failureThreshold: 36 }
          { type: 'Readiness', httpGet: { path: healthPath, port: 8000, scheme: 'HTTP' }, periodSeconds: 10, timeoutSeconds: 4, failureThreshold: 3 }
          { type: 'Liveness', httpGet: { path: '/api/live', port: 8000, scheme: 'HTTP' }, initialDelaySeconds: 60, periodSeconds: 30, timeoutSeconds: 4, failureThreshold: 3 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
  dependsOn: [imageReader, databaseReader, signingReader, modelReader]
}
output applicationFqdn string = application.properties.configuration.ingress.fqdn
output managedIdentityClientId string = identity.properties.clientId
