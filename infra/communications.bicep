// Dormant addon called by app.bicep. Provisioning is not outbound activation.
targetScope = 'resourceGroup'

param location string
param environmentName string
param registryName string
param keyVaultName string
param workerName string
param callbackName string
param workerIdentityName string
param callbackIdentityName string
param image string
param practiceId string
param practiceName string
param practiceTimezone string
param contactSnapshotMaxAgeHours int
param workerDatabaseRole string
param accountSid string
param fromNumber string
@allowed(['sms'])
@description('Must match the API channel configuration; current texting-only scope uses the SMS default.')
param communicationsEnabledChannels string = 'sms'
@description('Must match the API test-mode flag. Does not activate messaging.')
param communicationsTestMode bool = false
@secure()
@description('Exact protected API test-recipient input; outgoing worker only. Runtime validates E.164, duplicates and twenty-recipient limit. Empty blocks test sends.')
param communicationsTestRecipients string = ''
@maxLength(34)
@description('Optional Messaging Service MG SID; never replaces the fixed From number.')
param twilioMessagingServiceSid string = ''
param callbackOrigin string
param authTokenSecretName string
param directContractListPath string
param rosterSecretName string = ''
param rosterSecretVersion string = ''
param enableScheduler bool = false
param enableMessaging bool = false
param activateMessaging bool = false
param externalIngress bool = false
param cronExpression string = '*/5 * * * *'
param tags object = {}

var validatedMessaging = enableMessaging != activateMessaging
  ? fail('Twilio messaging and operational activation must change together')
  : enableMessaging && (!enableScheduler || !externalIngress)
    ? fail('Twilio messaging requires its scheduler and approved public callback ingress')
    : enableMessaging
var validatedCommunicationsTestMode = (!communicationsTestMode && !empty(communicationsTestRecipients)) || (communicationsTestMode && communicationsEnabledChannels != 'sms')
  ? fail('Test recipients require test mode, and test mode permits SMS only')
  : communicationsTestMode
var communicationsTestRecipientEnv = empty(communicationsTestRecipients)
  ? { name: 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', value: '' }
  : { name: 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', secretRef: 'communications-test-recipients' }
var twilioMessagingServiceNonHex = reduce(split('0,1,2,3,4,5,6,7,8,9,a,b,c,d,e,f', ','), toLower(skip(twilioMessagingServiceSid, 2)), (remaining, character) => replace(remaining, character, ''))
var validatedTwilioMessagingServiceSid = !empty(twilioMessagingServiceSid) && (length(twilioMessagingServiceSid) != 34 || !contains(['MG'], take(twilioMessagingServiceSid, 2)) || !empty(twilioMessagingServiceNonHex))
  ? fail('twilioMessagingServiceSid must be empty or MG followed by 32 hexadecimal characters')
  : twilioMessagingServiceSid
var forbiddenSecretNames = [
  'database-url'
  'migration-database-url'
  'database-runtime-password'
  'chase-worker-database-url'
  'chase-worker-database-password'
  'athena-import-database-url'
  'athena-import-database-password'
  'chase-gate-hmac-key'
  'acs-email-connection-string'
]
var validatedAuthTokenSecretName = empty(authTokenSecretName) || contains(forbiddenSecretNames, toLower(authTokenSecretName)) || toLower(authTokenSecretName) == toLower(rosterSecretName)
  ? fail('Twilio credential must have a dedicated Key Vault secret name')
  : authTokenSecretName
var validatedRosterSecretName = !empty(rosterSecretName) && contains(forbiddenSecretNames, toLower(rosterSecretName))
  ? fail('The direct-contract roster must have a dedicated Key Vault secret name')
  : rosterSecretName
var validatedRosterVersion = !empty(validatedRosterSecretName) && length(rosterSecretVersion) != 32
  ? fail('The roster requires an immutable Key Vault version')
  : rosterSecretVersion
var secretReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var commonEnv = [
  { name: 'COSMANEURA_ENV', value: 'production' }
  { name: 'COSMANEURA_COMMUNICATIONS_ENABLED', value: 'true' }
  { name: 'COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS', value: communicationsEnabledChannels }
  { name: 'COSMANEURA_COMMUNICATIONS_TEST_MODE', value: string(validatedCommunicationsTestMode) }
  { name: 'COSMANEURA_CHASE_TRANSPORT', value: 'twilio' }
  { name: 'COSMANEURA_PRACTICE_ID', value: practiceId }
  { name: 'COSMANEURA_PRACTICE_NAME', value: practiceName }
  { name: 'COSMANEURA_PRACTICE_TIMEZONE', value: practiceTimezone }
  { name: 'COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS', value: string(contactSnapshotMaxAgeHours) }
  { name: 'COSMANEURA_CHASE_WORKER_DB_ROLE', value: workerDatabaseRole }
  { name: 'COSMANEURA_CHASE_WORKER_DATABASE_URL', secretRef: 'worker-database-url' }
  { name: 'COSMANEURA_TWILIO_ACCOUNT_SID', value: accountSid }
  { name: 'COSMANEURA_TWILIO_FROM_NUMBER', value: fromNumber }
  { name: 'COSMANEURA_TWILIO_MESSAGING_SERVICE_SID', value: validatedTwilioMessagingServiceSid }
  { name: 'COSMANEURA_TWILIO_CALLBACK_BASE_URL', value: callbackOrigin }
  { name: 'COSMANEURA_TWILIO_AUTH_TOKEN', secretRef: 'twilio-auth-token' }
]

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = { name: environmentName }
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: registryName }
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: keyVaultName }
resource identities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for name in [workerIdentityName, callbackIdentityName]: {
  name: name
  location: location
  tags: tags
}]
resource imageReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: registry
  name: guid(registry.id, identities[i].id, 'acrpull')
  properties: {
    principalId: identities[i].properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}]
resource workerDatabaseSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'chase-worker-database-url'
}
resource tokenSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: validatedAuthTokenSecretName
}
resource gateSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: 'chase-gate-hmac-key'
}
resource rosterSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = if (!empty(validatedRosterSecretName)) {
  parent: vault
  name: validatedRosterSecretName
}
resource databaseReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: workerDatabaseSecret
  name: guid(workerDatabaseSecret.id, identities[i].id, 'key-vault-secrets-user')
  properties: { principalId: identities[i].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}]
resource tokenReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for i in range(0, 2): {
  scope: tokenSecret
  name: guid(tokenSecret.id, identities[i].id, 'key-vault-secrets-user')
  properties: { principalId: identities[i].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}]
resource workerGateReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: gateSecret
  name: guid(gateSecret.id, identities[0].id, 'key-vault-secrets-user')
  properties: { principalId: identities[0].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}
resource workerRosterReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(validatedRosterSecretName)) {
  scope: rosterSecret
  name: guid(rosterSecret.id, identities[0].id, 'key-vault-secrets-user')
  properties: { principalId: identities[0].properties.principalId, principalType: 'ServicePrincipal', roleDefinitionId: secretReaderRole }
}
var serviceSecrets = [for i in range(0, 2): [
  { name: 'worker-database-url', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-worker-database-url', identity: identities[i].id }
  { name: 'twilio-auth-token', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedAuthTokenSecretName}', identity: identities[i].id }
]]

resource worker 'Microsoft.App/jobs@2026-01-01' = {
  name: workerName
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identities[0].id}': {} } }
  properties: {
    environmentId: environment.id
    configuration: union({
      triggerType: enableScheduler ? 'Schedule' : 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      registries: [{ server: registry.properties.loginServer, identity: identities[0].id }]
      secrets: concat(serviceSecrets[0], [
        { name: 'chase-gate-hmac-key', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-gate-hmac-key', identity: identities[0].id }
      ], empty(validatedRosterSecretName) ? [] : [
        { name: 'direct-contract-roster', keyVaultUrl: 'https://${vault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedRosterSecretName}/${validatedRosterVersion}', identity: identities[0].id }
      ], empty(communicationsTestRecipients) ? [] : [
        { name: 'communications-test-recipients', value: communicationsTestRecipients }
      ])
    }, enableScheduler ? {
      scheduleTriggerConfig: { cronExpression: cronExpression, parallelism: 1, replicaCompletionCount: 1 }
    } : {
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
    })
    template: {
      containers: [{
        name: 'communications-worker'
        image: image
        command: ['python']
        args: ['-m', 'cosmaneura_billing.chase.communications.worker']
        env: concat(commonEnv, [
          communicationsTestRecipientEnv
          { name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED', value: string(enableScheduler) }
          { name: 'COSMANEURA_PATIENT_MESSAGING_ENABLED', value: string(validatedMessaging) }
          { name: 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED', value: string(validatedMessaging) }
          { name: 'COSMANEURA_CHASE_GATE_HMAC_KEY', secretRef: 'chase-gate-hmac-key' }
          { name: 'COSMANEURA_DIRECT_CONTRACT_LIST', value: directContractListPath }
        ])
        volumeMounts: empty(validatedRosterSecretName) ? [] : [{ volumeName: 'roster', mountPath: '/var/lib/cosmaneura/roster' }]
        resources: { cpu: json('0.5'), memory: '1Gi' }
      }]
      volumes: empty(validatedRosterSecretName) ? [] : [{
        name: 'roster'
        storageType: 'Secret'
        secrets: [{ secretRef: 'direct-contract-roster', path: 'direct_contract_patients.json' }]
      }]
    }
  }
  dependsOn: [imageReaders, databaseReaders, tokenReaders, workerGateReader, workerRosterReader]
}

resource callback 'Microsoft.App/containerApps@2026-01-01' = {
  name: callbackName
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${identities[1].id}': {} } }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: externalIngress
        allowInsecure: false
        targetPort: 8000
        transport: 'http'
        traffic: [{ latestRevision: true, weight: 100 }]
      }
      registries: [{ server: registry.properties.loginServer, identity: identities[1].id }]
      secrets: serviceSecrets[1]
    }
    template: {
      containers: [{
        name: 'communications-callback'
        image: image
        command: ['python']
        args: ['-m', 'uvicorn', 'cosmaneura_billing.chase.communications.webhooks:create_app', '--factory', '--host', '0.0.0.0', '--port', '8000', '--no-access-log']
        env: concat(commonEnv, [
          // Callback parsing needs no dispatch authority, roster content or gate key.
          // It also needs no outgoing recipient list; keep late inbound/STOP handling.
          { name: 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', value: '' }
          { name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED', value: 'false' }
          { name: 'COSMANEURA_PATIENT_MESSAGING_ENABLED', value: 'false' }
          { name: 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED', value: 'false' }
          { name: 'COSMANEURA_DIRECT_CONTRACT_LIST', value: '/nonexistent/callback-no-roster.json' }
        ])
        resources: { cpu: json('0.5'), memory: '1Gi' }
        probes: [
          { type: 'Startup', httpGet: { path: '/ready', port: 8000, scheme: 'HTTP' }, initialDelaySeconds: 5, periodSeconds: 5, timeoutSeconds: 4, failureThreshold: 36 }
          { type: 'Readiness', httpGet: { path: '/ready', port: 8000, scheme: 'HTTP' }, periodSeconds: 10, timeoutSeconds: 4, failureThreshold: 3 }
          { type: 'Liveness', httpGet: { path: '/live', port: 8000, scheme: 'HTTP' }, initialDelaySeconds: 60, periodSeconds: 30, timeoutSeconds: 4, failureThreshold: 3 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 2 }
    }
  }
  dependsOn: [imageReaders, databaseReaders, tokenReaders]
}

output callbackFqdn string = callback.properties.configuration.ingress.fqdn
output workerJobName string = worker.name
output schedulerEnabled bool = enableScheduler
output messagingEnabled bool = validatedMessaging
