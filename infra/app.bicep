targetScope = 'resourceGroup'

@description('Must match the foundation deployment prefix.')
@minLength(3)
@maxLength(20)
param namePrefix string = 'cosmaneura-pilot'

param location string = resourceGroup().location

@description('Commit-derived discovery tag that must resolve to imageDigest before deployment.')
param imageTag string

@description('Immutable OCI manifest digest resolved from imageTag, in sha256:<64-hex> form. The app, migration, and worker all deploy this digest.')
@minLength(71)
@maxLength(71)
param imageDigest string

@description('Stable pilot practice identifier used for every tenant key.')
param practiceId string

@description('Patient-facing practice name used by chase templates.')
param practiceName string = 'your care team'

@description('Immutable Entra object ID of the first pilot administrator. The migration job provisions this explicit membership.')
param initialAdminObjectId string

@description('Must match the least-privilege login configured by the foundation deployment.')
param postgresAppLogin string = 'cosmaneura_app'

@description('Must match the dedicated chase worker login configured by the foundation deployment.')
param postgresChaseWorkerLogin string = 'cosmaneura_chase_worker'

@description('Must match the dedicated Athena import login configured by the foundation deployment.')
param postgresAthenaImportLogin string = 'cosmaneura_athena_import'

@description('Secure card-upload URL used by chase templates.')
param uploadUrl string = 'https://example.invalid/insurance'

@description('Roster path used by both API and worker. An absent source fails the direct-contract gate closed.')
param directContractListPath string = '/var/lib/cosmaneura/direct_contract_patients.csv'

@allowed([
  'per_policy'
  'per_message'
])
@description('Human approval mode bound into the durable chase policy fingerprint.')
param chaseApprovalMode string = 'per_policy'

@description('Schedule the no-ingress chase job. False keeps the deployed job manual and non-scheduled.')
param enableChaseScheduler bool = false

@description('First explicit latch for outbound patient email. Keep false until BAA, P5, and acceptance review are complete.')
param enablePatientMessaging bool = false

@description('Second explicit operational latch for outbound patient email. It must exactly match enablePatientMessaging.')
param activatePatientMessaging bool = false

@description('Provision separate Twilio worker/callback identities and services. This does not activate outreach or public ingress.')
param provisionCommunications bool = false
param enableCommunications bool = false
param enableCommunicationsScheduler bool = false
param enableCommunicationsMessaging bool = false
param activateCommunicationsMessaging bool = false
param communicationsExternalIngress bool = false
param communicationsCronExpression string = '*/5 * * * *'
@allowed(['sms'])
@description('Outbound channels shared by API, worker and callback. Keep SMS for the current texting-only scope; calls require a separate explicit configuration decision.')
param communicationsEnabledChannels string = 'sms'
@description('Restrict outgoing SMS to the explicit test-recipient scope. Does not enable communications or consent.')
param communicationsTestMode bool = false
@secure()
@description('Comma-separated consenting tester E.164 numbers, protected as API/worker Container Apps secrets. Empty test scope blocks sends; never put real values in tracked examples, CLI arguments or outputs.')
param communicationsTestRecipients string = ''
param twilioAccountSid string = ''
param twilioFromNumber string = ''
@maxLength(34)
@description('Optional Messaging Service MG SID; the adapter still supplies the exact From number. Empty uses no service.')
param twilioMessagingServiceSid string = ''
@description('Operator-created dedicated Twilio account AuthToken secret; worker/callback only. No value is accepted here.')
param twilioAuthTokenSecretName string = 'twilio-account-auth-token'
@description('Optional operator-created JSON roster secret mounted identically and read-only by API and outbound worker. Empty retains the existing fail-closed file path.')
param directContractRosterSecretName string = ''
@description('Immutable Key Vault version of the reviewed roster. Required when a roster name is supplied; API and worker must read exactly the same version.')
param directContractRosterSecretVersion string = ''
@description('Enable the editable, evidence-linked header only after migration and source-bound acceptance.')
param enableBillingHeader bool = false
@description('Expose the existing provider completed-note handoff only with the header feature. Does not authorize operational data admission.')
param enableProviderHandoff bool = false

@description('Create the separate Core workload receiver only after its image contains migration026 and the dedicated login exists. Never mounts it in the staff API.')
param provisionCoreBridge bool = false
@description('Explicit reachability choice for authenticated Core workloads. Private ingress requires a verified cross-environment network path.')
param coreBridgeExternalIngress bool = false
param enableCoreBridgeInference bool = false
@description('Opt in to durable Core header generation only after canonical028, the dedicated runtime EXECUTE grant and capacity acceptance. Does not enable inference.')
param enableCoreBridgeAsyncGeneration bool = false
@description('Opt in to the existing Core provider-continuation contract only with async generation. Does not grant source or model authority.')
param enableCoreBridgeGenerationContinuation bool = false
param coreBridgeDatabaseRole string = ''
param coreBridgeDatabaseSecretName string = 'core-bridge-database-url'
@secure()
@maxLength(8192)
param coreBridgePolicyJson string = ''
@secure()
@maxLength(8192)
param coreBridgeModelDataPolicyJson string = ''
param coreBridgeDeploymentId string = ''
param coreBridgeStorageId string = ''

@description('Required operator-created shared 32–256 ASCII signing-secret reference for PostgreSQL coder review. API only, across all replicas.')
param reviewContextSigningKeySecretName string = 'billing-review-signing-key'
@allowed(['disabled', 'preview', 'configured'])
@description('Staff-reviewed CAB Athena reads, separate from sync. Configured mode resolves operator-owned case mappings; operational reads additionally require exact server-owned admission.')
param athenaReviewMode string = 'disabled'
@allowed(['', 'preview', 'production'])
@description('Explicit environment of the dedicated credential. Required for configured mode; empty preserves legacy Preview behavior.')
param athenaReviewCredentialEnvironment string = ''
@maxLength(128)
@description('Operator-reviewed deployment reference for exact Athena read/storage admission. Empty leaves operational reads unavailable.')
param athenaDeploymentId string = ''
@maxLength(128)
@description('Operator-reviewed storage reference paired with Athena read/storage admission. This is not a credential or permission.')
param athenaStorageId string = ''
param athenaReviewClientId string = ''
param athenaReviewClientSecretName string = 'athena-cab-preview-client-secret'

@description('Existing Azure Communication Services email endpoint; shared as non-secret policy metadata only.')
param acsEmailEndpoint string = ''

@description('Approved ACS sender address; shared as non-secret policy metadata only.')
param acsEmailSenderAddress string = ''

@description('Monitored reply-to address; shared as non-secret policy metadata only.')
param acsEmailReplyToAddress string = ''

@description('Enable API-only Azure OpenAI inference. Keep false until private networking, managed identity, telemetry, and synthetic model acceptance are complete.')
param enableAzureOpenAIInference bool = false

@description('Restrict model-backed API routes to reserved stored synthetic encounters. External Azure inference requires this restriction or a separately reviewed operational model-data policy.')
param restrictAzureOpenAIToSyntheticDemo bool = false

@secure()
@maxLength(8192)
@description('Immutable reviewed model-data policy JSON. Empty by default; API verifies exact source, runtime and Azure identities. Never contains patient data or credentials.')
param modelDataPolicyJson string = ''

@description('Must match the immutable deployment created by the foundation template.')
param azureOpenAIDeploymentName string = 'gpt-5-6-sol-2026-07-09'

@description('Underlying model/version label reported in PHI-safe inference metadata.')
param azureOpenAIModelName string = 'gpt-5.6-sol'

@description('Azure OpenAI Responses data-plane API version used by the official OpenAI SDK.')
param azureOpenAIApiVersion string = '2025-04-01-preview'

@description('Provision a dedicated private Azure AI Language F0 account. This does not enable application extraction.')
param provisionAzureLanguage bool = false

@description('Enable API-only Azure AI Language extraction. Keep false until Azure OpenAI inference and the isolated shadow milestone are approved.')
param enableAzureLanguageExtraction bool = false

@description('Run Azure AI Language in an isolated, non-influencing shadow lane. Requires primary Azure OpenAI and the provisioned Language account; mutually exclusive with active extraction.')
param enableAzureLanguageShadow bool = false

@description('Azure Text Analytics for health data-plane API version.')
param azureLanguageApiVersion string = '2023-04-01'

@description('Provision a dedicated user-assigned identity and no-ingress manual job for Athena sandbox sync. This does not enable sync or make an Athena request.')
param provisionAthenaSync bool = false

@description('Operator assertion that foundation provisionAthenaImportDatabase completed and its two Key Vault secrets exist. Required before provisioning the sync job.')
param athenaImportDatabaseProvisioned bool = false

@description('Enable the manually triggered Athena sandbox sync lane. Keep false until synthetic/read-only access, exact endpoints/scopes, egress, import-role, and acceptance review are complete.')
param enableAthenaSync bool = false

@description('Operator-created Key Vault secret name containing only the dedicated Athena sandbox OAuth client secret. No secret value is accepted by this template.')
@minLength(1)
param athenaClientSecretKeyVaultSecretName string = 'athena-sandbox-client-secret'

@description('Approved Athena sandbox API base URL. Keep empty while sync is disabled; do not guess vendor paths.')
param athenaBaseUrl string = ''

@description('Approved Athena sandbox OAuth token URL. Keep empty while sync is disabled; do not guess OAuth behavior.')
param athenaTokenUrl string = ''

@description('Dedicated synthetic/read-only Athena sandbox application client ID. Keep empty while sync is disabled.')
param athenaClientId string = ''

@description('Exact approved synthetic/read-only Athena OAuth scopes. Keep empty while sync is disabled.')
param athenaScopes string = ''

@description('IANA timezone used for chase cadence and quiet-hour calculations.')
param practiceTimezone string = 'UTC'

@minValue(1)
@maxValue(168)
@description('Maximum age in hours of contact evidence before delivery fails closed.')
param contactSnapshotMaxAgeHours int = 24

@minValue(1)
@maxValue(500)
@description('Maximum chase items processed by one bounded scheduled execution.')
param chaseWorkerBatchSize int = 25

@minValue(30)
@maxValue(3600)
@description('Lease duration in seconds for one claimed delivery.')
param chaseWorkerLeaseSeconds int = 300

@description('UTC cron expression for the Container Apps scheduled job.')
param chaseWorkerCronExpression string = '*/15 * * * *'

@description('Remain false until Entra assignment and the acceptance checks are verified.')
param externalIngress bool = false

@description('Microsoft Entra tenant that owns the single-tenant staff application.')
param entraTenantId string = tenant().tenantId

@description('Application (client) ID of the staff-facing Entra app registration.')
param entraClientId string

@secure()
@description('Entra client secret. Supplied only through a secure untracked parameter and stored in Key Vault.')
param entraClientSecret string

@description('Key Vault secret name used by Container Apps built-in authentication.')
param entraClientSecretKeyVaultSecretName string = 'entra-client-secret'

@minValue(1)
@maxValue(3)
param minReplicas int = 1

@minValue(1)
@maxValue(5)
param maxReplicas int = 2

param tags object = {
  application: 'cosmaneura-cab'
  environment: 'pilot'
  dataClassification: 'phi-capable-no-real-phi-yet'
}

var suffix = take(uniqueString(subscription().id, resourceGroup().id, namePrefix), 8)
var compactPrefix = replace(toLower(namePrefix), '-', '')
var containerNamePrefix = take(namePrefix, 18)
var environmentName = '${namePrefix}-env-${suffix}'
var appInsightsName = '${namePrefix}-insights-${suffix}'
var identityName = '${namePrefix}-identity-${suffix}'
var migrationIdentityName = '${namePrefix}-migration-identity-${suffix}'
var chaseWorkerIdentityName = '${namePrefix}-chase-identity-${suffix}'
var athenaSyncIdentityName = '${namePrefix}-athena-identity-${suffix}'
var acrName = take('${compactPrefix}${suffix}', 50)
var keyVaultName = take('${compactPrefix}${suffix}', 24)
var azureOpenAIName = take('${compactPrefix}oai${suffix}', 64)
var azureLanguageName = take('${compactPrefix}lang${suffix}', 64)
var vnetName = '${namePrefix}-vnet-${suffix}'
var appName = '${containerNamePrefix}-api-${suffix}'
var migrationJobName = '${containerNamePrefix}-mig-${suffix}'
var chaseWorkerJobName = '${containerNamePrefix}-wrk-${suffix}'
var athenaSyncJobName = '${containerNamePrefix}-ath-${suffix}'
var communicationsWorkerName = '${containerNamePrefix}-sms-${suffix}'
var communicationsCallbackName = '${containerNamePrefix}-cb-${suffix}'
var normalizedImageDigest = toLower(imageDigest)
var imageDigestHex = substring(normalizedImageDigest, 7, 64)
var imageDigestNonHex = reduce([
  '0'
  '1'
  '2'
  '3'
  '4'
  '5'
  '6'
  '7'
  '8'
  '9'
  'a'
  'b'
  'c'
  'd'
  'e'
  'f'
], imageDigestHex, (remaining, allowedCharacter) => replace(remaining, allowedCharacter, ''))
var validatedImageDigest = startsWith(normalizedImageDigest, 'sha256:') && empty(imageDigestNonHex)
  ? normalizedImageDigest
  : fail('imageDigest must use sha256:<64-hex> form')
var image = '${registry.properties.loginServer}/cosmaneura-billing@${validatedImageDigest}'
var entraSecretSettingName = 'microsoft-provider-authentication-secret'
var entraIssuer = '${az.environment().authentication.loginEndpoint}${entraTenantId}/v2.0'
var validatedSchedulerEnabled = enablePatientMessaging && !enableChaseScheduler
  ? fail('enablePatientMessaging requires enableChaseScheduler')
  : enableChaseScheduler
var validatedPatientMessagingEnabled = enablePatientMessaging != activatePatientMessaging
  ? fail('enablePatientMessaging and activatePatientMessaging must be changed together after operational review')
  : enablePatientMessaging
var validatedAcsEmailEndpoint = validatedPatientMessagingEnabled && empty(acsEmailEndpoint)
  ? fail('enabled patient messaging requires acsEmailEndpoint')
  : acsEmailEndpoint
var validatedAcsEmailSenderAddress = validatedPatientMessagingEnabled && empty(acsEmailSenderAddress)
  ? fail('enabled patient messaging requires acsEmailSenderAddress')
  : acsEmailSenderAddress
var validatedAcsEmailReplyToAddress = validatedPatientMessagingEnabled && empty(acsEmailReplyToAddress)
  ? fail('enabled patient messaging requires acsEmailReplyToAddress')
  : acsEmailReplyToAddress
var schedulerEnabledValue = validatedSchedulerEnabled ? 'true' : 'false'
var patientMessagingEnabledValue = validatedPatientMessagingEnabled ? 'true' : 'false'
var validatedAzureOpenAISyntheticOnly = restrictAzureOpenAIToSyntheticDemo && !enableAzureOpenAIInference
  ? fail('restrictAzureOpenAIToSyntheticDemo requires enableAzureOpenAIInference')
  : restrictAzureOpenAIToSyntheticDemo
var validatedAzureOpenAIInferenceEnabled = enableAzureOpenAIInference && externalIngress && !validatedAzureOpenAISyntheticOnly && empty(modelDataPolicyJson)
  ? fail('externally accessible Azure OpenAI inference requires restrictAzureOpenAIToSyntheticDemo or a reviewed modelDataPolicyJson')
  : enableAzureOpenAIInference
var azureOpenAIEnabledValue = validatedAzureOpenAIInferenceEnabled ? 'true' : 'false'
var azureOpenAISyntheticOnlyValue = validatedAzureOpenAISyntheticOnly ? 'true' : 'false'
var validatedAzureLanguageExtractionEnabled = enableAzureLanguageExtraction && !provisionAzureLanguage
  ? fail('enableAzureLanguageExtraction requires provisionAzureLanguage')
  : enableAzureLanguageExtraction && !validatedAzureOpenAIInferenceEnabled
    ? fail('enableAzureLanguageExtraction requires enableAzureOpenAIInference')
    : enableAzureLanguageExtraction
var validatedAzureLanguageShadowEnabled = enableAzureLanguageShadow && enableAzureLanguageExtraction
  ? fail('enableAzureLanguageShadow and enableAzureLanguageExtraction are mutually exclusive')
  : enableAzureLanguageShadow && !provisionAzureLanguage
    ? fail('enableAzureLanguageShadow requires provisionAzureLanguage')
    : enableAzureLanguageShadow && !validatedAzureOpenAIInferenceEnabled
      ? fail('enableAzureLanguageShadow requires enableAzureOpenAIInference')
      : enableAzureLanguageShadow && !validatedAzureOpenAISyntheticOnly
        ? fail('enableAzureLanguageShadow requires restrictAzureOpenAIToSyntheticDemo')
      : enableAzureLanguageShadow
var azureLanguageEnabledValue = validatedAzureLanguageExtractionEnabled ? 'true' : 'false'
var azureLanguageShadowEnabledValue = validatedAzureLanguageShadowEnabled ? 'true' : 'false'
var athenaConfigurationFieldsPresent = startsWith(toLower(athenaBaseUrl), 'https://') && startsWith(toLower(athenaTokenUrl), 'https://') && !empty(athenaClientId) && !empty(athenaScopes)
var athenaReservedSecretNames = [
  'migration-database-url'
  'database-runtime-password'
  'chase-worker-database-password'
  'athena-import-database-password'
  'database-url'
  'chase-worker-database-url'
  'athena-import-database-url'
  'chase-gate-hmac-key'
  'acs-email-connection-string'
]
var athenaClientSecretNameConflicts = contains(athenaReservedSecretNames, toLower(athenaClientSecretKeyVaultSecretName)) || toLower(athenaClientSecretKeyVaultSecretName) == toLower(entraClientSecretKeyVaultSecretName)
var validatedAthenaClientSecretName = athenaClientSecretNameConflicts
  ? fail('athenaClientSecretKeyVaultSecretName must not alias any platform or Entra secret')
  : athenaClientSecretKeyVaultSecretName
var entraClientSecretNameConflicts = contains(athenaReservedSecretNames, toLower(entraClientSecretKeyVaultSecretName)) || toLower(entraClientSecretKeyVaultSecretName) == toLower(athenaClientSecretKeyVaultSecretName)
var validatedEntraClientSecretName = entraClientSecretNameConflicts
  ? fail('entraClientSecretKeyVaultSecretName must not alias any platform or Athena secret')
  : entraClientSecretKeyVaultSecretName
var validatedAthenaImportDatabase = provisionAthenaSync && !athenaImportDatabaseProvisioned
  ? fail('provisionAthenaSync requires athenaImportDatabaseProvisioned after the foundation import secrets exist')
  : athenaImportDatabaseProvisioned
var validatedAthenaSyncEnabled = enableAthenaSync && !provisionAthenaSync
  ? fail('enableAthenaSync requires provisionAthenaSync')
  : enableAthenaSync && !athenaConfigurationFieldsPresent
    ? fail('enableAthenaSync requires explicit HTTPS base/token URLs, client ID, and scope values')
    : enableAthenaSync
var athenaSyncEnabledValue = validatedAthenaSyncEnabled ? 'true' : 'false'
var athenaSyncMode = validatedAthenaSyncEnabled ? 'live' : 'disabled'
var chaseTransport = validatedCommunicationsEnabled ? 'twilio' : validatedPatientMessagingEnabled ? 'acs_email' : 'record_only'
var betaSecretNames = [toLower(reviewContextSigningKeySecretName), toLower(twilioAuthTokenSecretName), toLower(athenaReviewClientSecretName)]
var betaSecretNameConflict = length(union(betaSecretNames, [])) != length(betaSecretNames) || contains(betaSecretNames, toLower(directContractRosterSecretName)) || contains(betaSecretNames, toLower(entraClientSecretKeyVaultSecretName)) || contains(betaSecretNames, toLower(athenaClientSecretKeyVaultSecretName))
var validatedReviewSigningSecretName = empty(reviewContextSigningKeySecretName) || contains(athenaReservedSecretNames, toLower(reviewContextSigningKeySecretName)) || betaSecretNameConflict
  ? fail('Beta credential names must be nonempty, distinct, and separate from platform/Entra/sync/roster secrets')
  : reviewContextSigningKeySecretName
var athenaReviewCredentialsRequired = contains(['preview', 'configured'], athenaReviewMode)
var validatedAthenaReviewCredentialEnvironment = (athenaReviewMode == 'configured' && empty(athenaReviewCredentialEnvironment)) || (athenaReviewMode == 'preview' && athenaReviewCredentialEnvironment == 'production')
  ? fail('Configured Athena review requires an explicit credential environment; Preview mode cannot use Production credentials')
  : athenaReviewMode == 'preview' && empty(athenaReviewCredentialEnvironment) ? 'preview' : athenaReviewCredentialEnvironment
var validatedAthenaReviewSecretName = contains(athenaReservedSecretNames, toLower(athenaReviewClientSecretName)) || (athenaReviewCredentialsRequired && (empty(athenaReviewClientId) || empty(athenaReviewClientSecretName)))
  ? fail('Athena review requires an explicit client ID and dedicated secret name')
  : athenaReviewClientSecretName
var validatedRosterSecretName = !empty(directContractRosterSecretName) && (contains(athenaReservedSecretNames, toLower(directContractRosterSecretName)) || toLower(directContractRosterSecretName) == toLower(entraClientSecretKeyVaultSecretName) || toLower(directContractRosterSecretName) == toLower(athenaClientSecretKeyVaultSecretName) || enableChaseScheduler || enablePatientMessaging || activatePatientMessaging)
  ? fail('The beta roster requires legacy outreach disabled and must not alias any credential secret')
  : directContractRosterSecretName
var validatedRosterSecretVersion = !empty(validatedRosterSecretName) && length(directContractRosterSecretVersion) != 32
  ? fail('The approved direct-contract roster requires its immutable 32-character Key Vault version')
  : directContractRosterSecretVersion
var twilioAccountNonHex = reduce(split('0,1,2,3,4,5,6,7,8,9,a,b,c,d,e,f', ','), toLower(skip(twilioAccountSid, 2)), (remaining, character) => replace(remaining, character, ''))
var twilioMessagingServiceNonHex = reduce(split('0,1,2,3,4,5,6,7,8,9,a,b,c,d,e,f', ','), toLower(skip(twilioMessagingServiceSid, 2)), (remaining, character) => replace(remaining, character, ''))
var validatedTwilioMessagingServiceSid = !empty(twilioMessagingServiceSid) && (length(twilioMessagingServiceSid) != 34 || !contains(['MG'], take(twilioMessagingServiceSid, 2)) || !empty(twilioMessagingServiceNonHex))
  ? fail('twilioMessagingServiceSid must be empty or MG followed by 32 hexadecimal characters')
  : twilioMessagingServiceSid
var twilioSenderNonDigits = reduce(split('0,1,2,3,4,5,6,7,8,9', ','), skip(twilioFromNumber, 1), (remaining, character) => replace(remaining, character, ''))
var validatedTwilioAccountSid = provisionCommunications && (length(twilioAccountSid) != 34 || !startsWith(twilioAccountSid, 'AC') || !empty(twilioAccountNonHex))
  ? fail('provisionCommunications requires an explicit Twilio AC account SID')
  : twilioAccountSid
var validatedTwilioFromNumber = provisionCommunications && (length(twilioFromNumber) < 9 || length(twilioFromNumber) > 16 || !startsWith(twilioFromNumber, '+') || startsWith(twilioFromNumber, '+0') || !empty(twilioSenderNonDigits))
  ? fail('provisionCommunications requires an explicit E.164 sender number')
  : twilioFromNumber
var validatedCommunicationsEnabled = enableCommunications && (!provisionCommunications || enableChaseScheduler || enablePatientMessaging || activatePatientMessaging)
  ? fail('Communications requires its provisioned services and the legacy ACS scheduler/messaging disabled')
  : enableCommunications
var validatedProviderHandoff = enableProviderHandoff && !enableBillingHeader
  ? fail('Provider handoff requires enableBillingHeader; operational admission remains separate')
  : enableProviderHandoff
var validatedCommunicationsTestMode = (!communicationsTestMode && !empty(communicationsTestRecipients)) || (communicationsTestMode && communicationsEnabledChannels != 'sms')
  ? fail('Test recipients require test mode, and test mode permits SMS only')
  : communicationsTestMode
var communicationsTestRecipientEnv = empty(communicationsTestRecipients)
  ? { name: 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', value: '' }
  : { name: 'COSMANEURA_COMMUNICATIONS_TEST_RECIPIENTS', secretRef: 'communications-test-recipients' }
var validatedCommunicationsScheduler = enableCommunicationsScheduler && !validatedCommunicationsEnabled
  ? fail('Communications scheduler requires enableCommunications')
  : enableCommunicationsScheduler
var validatedCommunicationsMessaging = enableCommunicationsMessaging != activateCommunicationsMessaging || (enableCommunicationsMessaging && (!validatedCommunicationsScheduler || !communicationsExternalIngress))
  ? fail('Communications messaging requires matching activation, its scheduler, and approved public callback ingress')
  : enableCommunicationsMessaging
var validatedCommunicationsIngress = communicationsExternalIngress && !provisionCommunications
  ? fail('Communications ingress requires provisionCommunications')
  : communicationsExternalIngress
var effectiveRosterPath = empty(validatedRosterSecretName) ? directContractListPath : '/var/lib/cosmaneura/roster/direct_contract_patients.json'
var communicationsCallbackOrigin = 'https://${communicationsCallbackName}.${environment.properties.defaultDomain}'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource azureOpenAI 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (enableAzureOpenAIInference) {
  name: azureOpenAIName
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource privateEndpointsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: 'private-endpoints'
}

var applicationOrigin = 'https://${appName}.${environment.properties.defaultDomain}'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource migrationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: migrationIdentityName
}

resource chaseWorkerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: chaseWorkerIdentityName
}

resource athenaSyncIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (provisionAthenaSync) {
  name: athenaSyncIdentityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource reviewSigningSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: validatedReviewSigningSecretName
}
resource reviewSigningReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: reviewSigningSecret
  name: guid(reviewSigningSecret.id, identity.id, 'key-vault-secrets-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
resource reviewAthenaSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = if (athenaReviewCredentialsRequired) {
  parent: keyVault
  name: validatedAthenaReviewSecretName
}
resource reviewAthenaReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (athenaReviewCredentialsRequired) {
  scope: reviewAthenaSecret
  name: guid(reviewAthenaSecret.id, identity.id, 'key-vault-secrets-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}
resource rosterSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = if (!empty(validatedRosterSecretName)) {
  parent: keyVault
  name: validatedRosterSecretName
}
resource apiRosterReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(validatedRosterSecretName)) {
  scope: rosterSecret
  name: guid(rosterSecret.id, identity.id, 'key-vault-secrets-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}


var validatedCoreDatabaseRole = provisionCoreBridge && (empty(coreBridgeDatabaseRole) || contains([toLower(postgresAppLogin), toLower(postgresChaseWorkerLogin), toLower(postgresAthenaImportLogin), 'postgres', 'cosmaneura_admin'], toLower(coreBridgeDatabaseRole)))
  ? fail('Core bridge requires its separate owner-registered runtime login')
  : coreBridgeDatabaseRole
var validatedCoreDatabaseSecret = provisionCoreBridge && (empty(coreBridgeDatabaseSecretName) || contains(union(athenaReservedSecretNames, betaSecretNames, [toLower(entraClientSecretKeyVaultSecretName), toLower(athenaClientSecretKeyVaultSecretName)]), toLower(coreBridgeDatabaseSecretName)))
  ? fail('Core bridge database secret must not alias any API, owner, worker, importer or credential secret')
  : coreBridgeDatabaseSecretName
var validatedCoreGenerationContinuation = enableCoreBridgeGenerationContinuation && !enableCoreBridgeAsyncGeneration
  ? fail('Core bridge continuation requires async generation')
  : enableCoreBridgeGenerationContinuation
var validatedCoreProvisioning = (coreBridgeExternalIngress || enableCoreBridgeInference || enableCoreBridgeAsyncGeneration || enableCoreBridgeGenerationContinuation || !empty(coreBridgePolicyJson) || !empty(coreBridgeModelDataPolicyJson)) && !provisionCoreBridge
  ? fail('Core bridge policy, ingress, inference and generation options require explicit provisionCoreBridge')
  : provisionCoreBridge

module coreBridge './core-bridge.bicep' = if (validatedCoreProvisioning) {
  name: 'core-workload-runtime'
  params: {
    location: location
    environmentName: environmentName
    registryName: acrName
    keyVaultName: keyVaultName
    appName: '${containerNamePrefix}-core-${suffix}'
    identityName: '${namePrefix}-core-identity-${suffix}'
    image: image
    practiceId: practiceId
    tenantId: entraTenantId
    databaseRole: validatedCoreDatabaseRole
    databaseSecretName: validatedCoreDatabaseSecret
    reviewSigningSecretName: validatedReviewSigningSecretName
    policyJson: coreBridgePolicyJson
    modelDataPolicyJson: coreBridgeModelDataPolicyJson
    deploymentId: coreBridgeDeploymentId
    storageId: coreBridgeStorageId
    externalIngress: coreBridgeExternalIngress
    enableInference: enableCoreBridgeInference
    enableAsyncGeneration: enableCoreBridgeAsyncGeneration
    enableGenerationContinuation: validatedCoreGenerationContinuation
    azureOpenAIAccountName: azureOpenAIName
    azureOpenAIDeployment: azureOpenAIDeploymentName
    azureOpenAIModel: azureOpenAIModelName
    azureOpenAIApiVersion: azureOpenAIApiVersion
    tags: tags
  }
}

module communications './communications.bicep' = if (provisionCommunications) {
  name: 'communications-runtime'
  params: {
    location: location
    environmentName: environmentName
    registryName: acrName
    keyVaultName: keyVaultName
    workerName: communicationsWorkerName
    callbackName: communicationsCallbackName
    workerIdentityName: '${namePrefix}-sms-identity-${suffix}'
    callbackIdentityName: '${namePrefix}-callback-identity-${suffix}'
    image: image
    practiceId: practiceId
    practiceName: practiceName
    practiceTimezone: practiceTimezone
    contactSnapshotMaxAgeHours: contactSnapshotMaxAgeHours
    workerDatabaseRole: postgresChaseWorkerLogin
    accountSid: validatedTwilioAccountSid
    fromNumber: validatedTwilioFromNumber
    communicationsEnabledChannels: communicationsEnabledChannels
    communicationsTestMode: validatedCommunicationsTestMode
    communicationsTestRecipients: communicationsTestRecipients
    twilioMessagingServiceSid: validatedTwilioMessagingServiceSid
    callbackOrigin: communicationsCallbackOrigin
    authTokenSecretName: twilioAuthTokenSecretName
    directContractListPath: effectiveRosterPath
    rosterSecretName: validatedRosterSecretName
    rosterSecretVersion: validatedRosterSecretVersion
    enableScheduler: validatedCommunicationsScheduler
    enableMessaging: validatedCommunicationsMessaging
    activateMessaging: activateCommunicationsMessaging
    externalIngress: validatedCommunicationsIngress
    cronExpression: communicationsCronExpression
    tags: tags
  }
}

resource athenaSyncAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (provisionAthenaSync) {
  scope: registry
  name: guid(registry.id, athenaSyncIdentity!.id, 'acrpull')
  properties: {
    principalId: athenaSyncIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource athenaClientSecretVault 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = if (validatedAthenaSyncEnabled) {
  parent: keyVault
  name: validatedAthenaClientSecretName
}

resource athenaClientSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (validatedAthenaSyncEnabled) {
  scope: athenaClientSecretVault
  name: guid(athenaClientSecretVault.id, athenaSyncIdentity!.id, 'key-vault-secrets-user')
  properties: {
    principalId: athenaSyncIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource athenaImportDatabaseUrlVault 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = if (validatedAthenaSyncEnabled) {
  parent: keyVault
  name: 'athena-import-database-url'
}

resource athenaImportDatabaseUrlReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (validatedAthenaSyncEnabled) {
  scope: athenaImportDatabaseUrlVault
  name: guid(athenaImportDatabaseUrlVault.id, athenaSyncIdentity!.id, 'key-vault-secrets-user')
  properties: {
    principalId: athenaSyncIdentity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource azureLanguage 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (provisionAzureLanguage) {
  name: azureLanguageName
  location: location
  tags: tags
  kind: 'TextAnalytics'
  sku: {
    name: 'F0'
  }
  properties: {
    customSubDomainName: azureLanguageName
    disableLocalAuth: true
    networkAcls: {
      defaultAction: 'Deny'
    }
    publicNetworkAccess: 'Disabled'
  }
}

resource apiAzureLanguageReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (provisionAzureLanguage) {
  scope: azureLanguage
  name: guid(azureLanguage.id, identity.id, 'cognitive-services-language-reader')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7628b7b8-a8b2-4cdc-b46f-e9b35248918e'
    )
  }
}

resource azureLanguagePrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = if (provisionAzureLanguage) {
  name: 'privatelink.cognitiveservices.azure.com'
  location: 'global'
  tags: tags
}

resource azureLanguageDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (provisionAzureLanguage) {
  parent: azureLanguagePrivateDns
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource azureLanguagePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = if (provisionAzureLanguage) {
  name: '${namePrefix}-lang-pe-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointsSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'account'
        properties: {
          privateLinkServiceId: azureLanguage.id
          groupIds: [
            'account'
          ]
        }
      }
    ]
  }
  // A private endpoint NIC mutates the shared foundation subnet. The
  // foundation deployment must finish before this app deployment starts; the
  // dependencies below keep this template's only subnet writer ordered after
  // its account, RBAC, DNS-zone, and VNet-link prerequisites.
  dependsOn: [
    apiAzureLanguageReader
    azureLanguageDnsLink
  ]
}

resource azureLanguagePrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = if (provisionAzureLanguage) {
  parent: azureLanguagePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'azure-language'
        properties: {
          privateDnsZoneId: azureLanguagePrivateDns.id
        }
      }
    ]
  }
}

resource entraClientSecretVault 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: validatedEntraClientSecretName
  properties: {
    contentType: 'Container Apps Microsoft Entra authentication client secret'
    value: entraClientSecret
  }
}

resource entraClientSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: entraClientSecretVault
  name: guid(entraClientSecretVault.id, identity.id, 'key-vault-secrets-user')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource migrationJob 'Microsoft.App/jobs@2026-01-01' = {
  name: migrationJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${migrationIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: migrationIdentity.id
        }
      ]
      secrets: concat([
        {
          name: 'migration-database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/migration-database-url'
          identity: migrationIdentity.id
        }
        {
          name: 'database-runtime-password'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/database-runtime-password'
          identity: migrationIdentity.id
        }
        {
          name: 'chase-worker-database-password'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-worker-database-password'
          identity: migrationIdentity.id
        }
      ], validatedAthenaImportDatabase ? [
        {
          name: 'athena-import-database-password'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/athena-import-database-password'
          identity: migrationIdentity.id
        }
      ] : [])
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: image
          command: [
            'python'
          ]
          args: [
            '-m'
            'cosmaneura_billing.migrations'
          ]
          env: concat([
            {
              name: 'DATABASE_URL'
              secretRef: 'migration-database-url'
            }
            {
              name: 'COSMANEURA_DB_RUNTIME_ROLE'
              value: postgresAppLogin
            }
            {
              name: 'COSMANEURA_DB_RUNTIME_PASSWORD'
              secretRef: 'database-runtime-password'
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_DB_ROLE'
              value: postgresChaseWorkerLogin
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_DB_PASSWORD'
              secretRef: 'chase-worker-database-password'
            }
            {
              name: 'COSMANEURA_ENTRA_TENANT_ID'
              value: entraTenantId
            }
            {
              name: 'COSMANEURA_PRACTICE_ID'
              value: practiceId
            }
            {
              name: 'COSMANEURA_PRACTICE_NAME'
              value: practiceName
            }
            {
              name: 'COSMANEURA_BOOTSTRAP_ADMIN_OID'
              value: initialAdminObjectId
            }
            {
              name: 'COSMANEURA_MIGRATIONS_DIR'
              value: '/app/migrations'
            }
          ], validatedAthenaImportDatabase ? [
            {
              name: 'COSMANEURA_ATHENA_IMPORT_DB_ROLE'
              value: postgresAthenaImportLogin
            }
            {
              name: 'COSMANEURA_ATHENA_IMPORT_DB_PASSWORD'
              secretRef: 'athena-import-database-password'
            }
          ] : [])
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
}

resource application 'Microsoft.App/containerApps@2026-01-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: externalIngress
        allowInsecure: false
        targetPort: 8000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: concat([
        {
          name: 'database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/database-url'
          identity: identity.id
        }
        {
          name: entraSecretSettingName
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedEntraClientSecretName}'
          identity: identity.id
        }
        {
          name: 'chase-gate-hmac-key'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-gate-hmac-key'
          identity: identity.id
        }
        {
          name: 'review-signing-key'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedReviewSigningSecretName}'
          identity: identity.id
        }
      ], athenaReviewCredentialsRequired ? [{
        name: 'athena-review-client-secret'
        keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedAthenaReviewSecretName}'
        identity: identity.id
      }] : [], empty(validatedRosterSecretName) ? [] : [{
        name: 'direct-contract-roster'
        keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedRosterSecretName}/${validatedRosterSecretVersion}'
        identity: identity.id
      }], empty(communicationsTestRecipients) ? [] : [{
        name: 'communications-test-recipients'
        value: communicationsTestRecipients
      }])
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          env: concat([
            { name: 'COSMANEURA_BILLING_HEADER_ENABLED', value: string(enableBillingHeader) }
            { name: 'COSMANEURA_PROVIDER_HANDOFF_ENABLED', value: string(validatedProviderHandoff) }
            { name: 'COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY', secretRef: 'review-signing-key' }
            { name: 'COSMANEURA_ATHENA_REVIEW_MODE', value: athenaReviewMode }
            { name: 'COSMANEURA_ATHENA_CREDENTIAL_ENVIRONMENT', value: validatedAthenaReviewCredentialEnvironment }
            { name: 'COSMANEURA_ATHENA_DEPLOYMENT_ID', value: athenaDeploymentId }
            { name: 'COSMANEURA_ATHENA_STORAGE_ID', value: athenaStorageId }
            { name: 'COSMANEURA_COMMUNICATIONS_ENABLED', value: string(validatedCommunicationsEnabled) }
            { name: 'COSMANEURA_COMMUNICATIONS_ENABLED_CHANNELS', value: communicationsEnabledChannels }
            { name: 'COSMANEURA_COMMUNICATIONS_TEST_MODE', value: string(validatedCommunicationsTestMode) }
            communicationsTestRecipientEnv
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'COSMANEURA_ENV'
              value: 'production'
            }
            {
              name: 'COSMANEURA_TELEMETRY_ENABLED'
              value: 'true'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsights.properties.ConnectionString
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: identity.properties.clientId
            }
            {
              name: 'COSMANEURA_AZURE_OPENAI_ENABLED'
              value: azureOpenAIEnabledValue
            }
            {
              name: 'COSMANEURA_AZURE_OPENAI_SYNTHETIC_ONLY'
              value: azureOpenAISyntheticOnlyValue
            }
            { name: 'COSMANEURA_MODEL_DATA_POLICY_JSON', value: modelDataPolicyJson }
            {
              name: 'COSMANEURA_AZURE_LANGUAGE_ENABLED'
              value: azureLanguageEnabledValue
            }
            {
              name: 'COSMANEURA_AZURE_LANGUAGE_SHADOW_ENABLED'
              value: azureLanguageShadowEnabledValue
            }
            {
              name: 'APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL'
              value: 'true'
            }
            {
              name: 'APPLICATIONINSIGHTS_SDKSTATS_DISABLED'
              value: 'true'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED'
              value: 'true'
            }
            {
              name: 'COSMANEURA_FRONTEND_DIST'
              value: '/app/static'
            }
            {
              name: 'COSMANEURA_STATE_STORE'
              value: 'postgres'
            }
            {
              name: 'COSMANEURA_DB_RUNTIME_ROLE'
              value: postgresAppLogin
            }
            {
              name: 'COSMANEURA_AUTH_MODE'
              value: 'container_apps'
            }
            {
              name: 'COSMANEURA_ENTRA_TENANT_ID'
              value: entraTenantId
            }
            {
              name: 'COSMANEURA_ALLOWED_ROLES'
              value: 'Admin,BillingStaff,Coder,Provider'
            }
            {
              name: 'COSMANEURA_CORS_ORIGINS'
              value: applicationOrigin
            }
            {
              name: 'COSMANEURA_MAX_REQUEST_BYTES'
              value: '1048576'
            }
            {
              name: 'COSMANEURA_INFERENCE_REQUESTS_PER_MINUTE'
              value: '10'
            }
            {
              name: 'COSMANEURA_GENERAL_REQUESTS_PER_MINUTE'
              value: '60'
            }
            {
              name: 'COSMANEURA_AUTH_FAILURES_PER_MINUTE'
              value: '30'
            }
            {
              name: 'COSMANEURA_MIGRATIONS_DIR'
              value: '/app/migrations'
            }
            {
              name: 'COSMANEURA_CHASE_STORE'
              value: 'postgres'
            }
            {
              name: 'COSMANEURA_PRACTICE_ID'
              value: practiceId
            }
            {
              name: 'COSMANEURA_PRACTICE_NAME'
              value: practiceName
            }
            {
              name: 'COSMANEURA_UPLOAD_URL'
              value: uploadUrl
            }
            {
              name: 'COSMANEURA_CHASE_APPROVAL'
              value: chaseApprovalMode
            }
            {
              name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED'
              value: validatedCommunicationsEnabled ? string(validatedCommunicationsScheduler) : schedulerEnabledValue
            }
            {
              name: 'COSMANEURA_PATIENT_MESSAGING_ENABLED'
              value: validatedCommunicationsEnabled ? string(validatedCommunicationsMessaging) : patientMessagingEnabledValue
            }
            {
              name: 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED'
              value: validatedCommunicationsEnabled ? string(validatedCommunicationsMessaging) : patientMessagingEnabledValue
            }
            {
              name: 'COSMANEURA_CHASE_TRANSPORT'
              value: chaseTransport
            }
            {
              name: 'COSMANEURA_CHASE_GATE_HMAC_KEY'
              secretRef: 'chase-gate-hmac-key'
            }
            {
              name: 'COSMANEURA_PRACTICE_TIMEZONE'
              value: practiceTimezone
            }
            {
              name: 'COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS'
              value: string(contactSnapshotMaxAgeHours)
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_BATCH_SIZE'
              value: string(chaseWorkerBatchSize)
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_LEASE_SECONDS'
              value: string(chaseWorkerLeaseSeconds)
            }
            {
              name: 'CPT_AUTO_FETCH'
              value: '0'
            }
            {
              name: 'COSMANEURA_DIRECT_CONTRACT_LIST'
              value: effectiveRosterPath
            }
          ], validatedCommunicationsEnabled ? [
            { name: 'COSMANEURA_TWILIO_ACCOUNT_SID', value: validatedTwilioAccountSid }
            { name: 'COSMANEURA_TWILIO_FROM_NUMBER', value: validatedTwilioFromNumber }
            { name: 'COSMANEURA_TWILIO_MESSAGING_SERVICE_SID', value: validatedTwilioMessagingServiceSid }
            { name: 'COSMANEURA_TWILIO_CALLBACK_BASE_URL', value: communicationsCallbackOrigin }
          ] : [], athenaReviewCredentialsRequired ? [
            { name: 'COSMANEURA_ATHENA_CLIENT_ID', value: athenaReviewClientId }
            { name: 'COSMANEURA_ATHENA_CLIENT_SECRET', secretRef: 'athena-review-client-secret' }
          ] : [], enableAzureOpenAIInference ? [
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: azureOpenAI!.properties.endpoint
            }
            {
              name: 'AZURE_OPENAI_DEPLOYMENT'
              value: azureOpenAIDeploymentName
            }
            {
              name: 'AZURE_OPENAI_MODEL'
              value: azureOpenAIModelName
            }
            {
              name: 'AZURE_OPENAI_API_VERSION'
              value: azureOpenAIApiVersion
            }
          ] : [], validatedAzureLanguageExtractionEnabled || validatedAzureLanguageShadowEnabled ? [
            {
              name: 'AZURE_LANGUAGE_ENDPOINT'
              value: azureLanguage!.properties.endpoint
            }
            {
              name: 'AZURE_LANGUAGE_API_VERSION'
              value: azureLanguageApiVersion
            }
          ] : [], validatedPatientMessagingEnabled ? [
            {
              name: 'COSMANEURA_ACS_EMAIL_ENDPOINT'
              value: validatedAcsEmailEndpoint
            }
            {
              name: 'COSMANEURA_ACS_EMAIL_SENDER_ADDRESS'
              value: validatedAcsEmailSenderAddress
            }
            {
              name: 'COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS'
              value: validatedAcsEmailReplyToAddress
            }
          ] : [])
          volumeMounts: empty(validatedRosterSecretName) ? [] : [{ volumeName: 'roster', mountPath: '/var/lib/cosmaneura/roster' }]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/api/ready'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 5
              failureThreshold: 36
              timeoutSeconds: 4
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/ready'
                port: 8000
                scheme: 'HTTP'
              }
              periodSeconds: 10
              failureThreshold: 3
              timeoutSeconds: 4
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/live'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 60
              periodSeconds: 30
              failureThreshold: 3
              timeoutSeconds: 4
            }
          ]
        }
      ]
      volumes: empty(validatedRosterSecretName) ? [] : [{
        name: 'roster'
        storageType: 'Secret'
        secrets: [{ secretRef: 'direct-contract-roster', path: 'direct_contract_patients.json' }]
      }]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    entraClientSecretVault
    entraClientSecretReader
    reviewSigningReader
    reviewAthenaReader
    apiRosterReader
    apiAzureLanguageReader
    azureLanguagePrivateDnsGroup
  ]
}

resource chaseWorkerJob 'Microsoft.App/jobs@2026-01-01' = {
  name: chaseWorkerJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${chaseWorkerIdentity.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: union({
      triggerType: validatedSchedulerEnabled ? 'Schedule' : 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      registries: [
        {
          server: registry.properties.loginServer
          identity: chaseWorkerIdentity.id
        }
      ]
      secrets: concat([
        {
          name: 'chase-worker-database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-worker-database-url'
          identity: chaseWorkerIdentity.id
        }
        {
          name: 'chase-gate-hmac-key'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-gate-hmac-key'
          identity: chaseWorkerIdentity.id
        }
      ], validatedPatientMessagingEnabled ? [
        {
          name: 'acs-email-connection-string'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/acs-email-connection-string'
          identity: chaseWorkerIdentity.id
        }
      ] : [])
    }, validatedSchedulerEnabled ? {
      scheduleTriggerConfig: {
        cronExpression: chaseWorkerCronExpression
        parallelism: 1
        replicaCompletionCount: 1
      }
    } : {
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
    })
    template: {
      containers: [
        {
          name: 'chase-worker'
          image: image
          command: [
            'python'
          ]
          args: [
            '-m'
            'cosmaneura_billing.chase.worker'
            '--once'
          ]
          env: concat([
            {
              name: 'COSMANEURA_ENV'
              value: 'production'
            }
            {
              name: 'COSMANEURA_LOG_LEVEL'
              value: 'INFO'
            }
            {
              name: 'COSMANEURA_PRACTICE_ID'
              value: practiceId
            }
            {
              name: 'COSMANEURA_PRACTICE_NAME'
              value: practiceName
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_DB_ROLE'
              value: postgresChaseWorkerLogin
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_DATABASE_URL'
              secretRef: 'chase-worker-database-url'
            }
            {
              name: 'COSMANEURA_CHASE_SCHEDULER_ENABLED'
              value: schedulerEnabledValue
            }
            {
              name: 'COSMANEURA_PATIENT_MESSAGING_ENABLED'
              value: patientMessagingEnabledValue
            }
            {
              name: 'COSMANEURA_PATIENT_MESSAGING_OPERATIONALLY_ACTIVATED'
              value: patientMessagingEnabledValue
            }
            {
              name: 'COSMANEURA_CHASE_TRANSPORT'
              value: chaseTransport
            }
            {
              name: 'COSMANEURA_UPLOAD_URL'
              value: uploadUrl
            }
            {
              name: 'COSMANEURA_CHASE_APPROVAL'
              value: chaseApprovalMode
            }
            {
              name: 'COSMANEURA_DIRECT_CONTRACT_LIST'
              value: directContractListPath
            }
            {
              name: 'COSMANEURA_CHASE_GATE_HMAC_KEY'
              secretRef: 'chase-gate-hmac-key'
            }
            {
              name: 'COSMANEURA_PRACTICE_TIMEZONE'
              value: practiceTimezone
            }
            {
              name: 'COSMANEURA_CONTACT_SNAPSHOT_MAX_AGE_HOURS'
              value: string(contactSnapshotMaxAgeHours)
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_BATCH_SIZE'
              value: string(chaseWorkerBatchSize)
            }
            {
              name: 'COSMANEURA_CHASE_WORKER_LEASE_SECONDS'
              value: string(chaseWorkerLeaseSeconds)
            }
          ], validatedPatientMessagingEnabled ? [
            {
              name: 'COSMANEURA_ACS_EMAIL_CONNECTION_STRING'
              secretRef: 'acs-email-connection-string'
            }
            {
              name: 'COSMANEURA_ACS_EMAIL_ENDPOINT'
              value: validatedAcsEmailEndpoint
            }
            {
              name: 'COSMANEURA_ACS_EMAIL_SENDER_ADDRESS'
              value: validatedAcsEmailSenderAddress
            }
            {
              name: 'COSMANEURA_ACS_EMAIL_REPLY_TO_ADDRESS'
              value: validatedAcsEmailReplyToAddress
            }
          ] : [])
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
}

resource athenaSyncJob 'Microsoft.App/jobs@2026-01-01' = if (provisionAthenaSync) {
  name: athenaSyncJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${athenaSyncIdentity!.id}': {}
    }
  }
  properties: {
    environmentId: environment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: athenaSyncIdentity!.id
        }
      ]
      secrets: validatedAthenaSyncEnabled ? [
        {
          name: 'athena-client-secret'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${validatedAthenaClientSecretName}'
          identity: athenaSyncIdentity!.id
        }
        {
          name: 'athena-import-database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/athena-import-database-url'
          identity: athenaSyncIdentity!.id
        }
      ] : []
    }
    template: {
      containers: [
        {
          name: 'athena-sync'
          image: image
          command: [
            'python'
          ]
          args: [
            '-m'
            'cosmaneura_billing.athena_sync'
            '--once'
          ]
          env: concat([
            {
              name: 'COSMANEURA_ENV'
              value: 'production'
            }
            {
              name: 'COSMANEURA_LOG_LEVEL'
              value: 'INFO'
            }
            {
              name: 'COSMANEURA_PRACTICE_ID'
              value: practiceId
            }
            {
              name: 'COSMANEURA_ATHENA_SYNC_ENABLED'
              value: athenaSyncEnabledValue
            }
            {
              name: 'COSMANEURA_ATHENA_MODE'
              value: athenaSyncMode
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: athenaSyncIdentity!.properties.clientId
            }
          ], validatedAthenaSyncEnabled ? [
            {
              name: 'COSMANEURA_ATHENA_BASE_URL'
              value: athenaBaseUrl
            }
            {
              name: 'COSMANEURA_ATHENA_TOKEN_URL'
              value: athenaTokenUrl
            }
            {
              name: 'COSMANEURA_ATHENA_CLIENT_ID'
              value: athenaClientId
            }
            {
              name: 'COSMANEURA_ATHENA_SCOPES'
              value: athenaScopes
            }
            {
              name: 'COSMANEURA_ATHENA_CLIENT_SECRET'
              secretRef: 'athena-client-secret'
            }
            {
              name: 'COSMANEURA_ATHENA_IMPORT_DB_ROLE'
              value: postgresAthenaImportLogin
            }
            {
              name: 'COSMANEURA_ATHENA_IMPORT_DATABASE_URL'
              secretRef: 'athena-import-database-url'
            }
          ] : [])
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    athenaSyncAcrPull
    athenaClientSecretReader
    athenaImportDatabaseUrlReader
  ]
}

resource applicationAuth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = {
  parent: application
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
      excludedPaths: [
        '/'
        '/index.html'
        '/favicon.ico'
        '/assets/*'
        '/api/live'
      ]
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: entraSecretSettingName
          openIdIssuer: entraIssuer
        }
      }
    }
  }
}

output applicationName string = application.name
output migrationJobName string = migrationJob.name
output chaseWorkerJobName string = chaseWorkerJob.name
output chaseSchedulerEnabled bool = validatedSchedulerEnabled
output patientMessagingEnabled bool = validatedPatientMessagingEnabled
output ingressExternal bool = externalIngress
output applicationFqdn string = application.properties.configuration.ingress.fqdn
output imageSourceTag string = imageTag
output deployedImageDigest string = validatedImageDigest
output azureOpenAIInferenceEnabled bool = validatedAzureOpenAIInferenceEnabled
output azureOpenAISyntheticOnly bool = validatedAzureOpenAISyntheticOnly
output azureLanguageProvisioned bool = provisionAzureLanguage
output azureLanguageExtractionEnabled bool = validatedAzureLanguageExtractionEnabled
output azureLanguageShadowEnabled bool = validatedAzureLanguageShadowEnabled
output azureLanguageAccountName string = provisionAzureLanguage ? azureLanguage!.name : ''
output azureLanguageEndpoint string = provisionAzureLanguage ? azureLanguage!.properties.endpoint : ''
output athenaSyncProvisioned bool = provisionAthenaSync
output athenaSyncEnabled bool = validatedAthenaSyncEnabled
output athenaSyncIdentityName string = provisionAthenaSync ? athenaSyncIdentity!.name : ''
output athenaSyncJobName string = provisionAthenaSync ? athenaSyncJob!.name : ''

output billingHeaderEnabled bool = enableBillingHeader
output providerHandoffEnabled bool = validatedProviderHandoff
output communicationsTestModeEnabled bool = validatedCommunicationsTestMode
output athenaReviewMode string = athenaReviewMode
output communicationsProvisioned bool = provisionCommunications
output communicationsEnabled bool = validatedCommunicationsEnabled
output communicationsWorkerJobName string = provisionCommunications ? communications!.outputs.workerJobName : ''
output communicationsCallbackOrigin string = provisionCommunications ? communicationsCallbackOrigin : ''
output communicationsSchedulerEnabled bool = validatedCommunicationsScheduler
output communicationsMessagingEnabled bool = validatedCommunicationsMessaging
output communicationsIngressExternal bool = validatedCommunicationsIngress

output coreBridgeProvisioned bool = validatedCoreProvisioning
output coreBridgeFqdn string = validatedCoreProvisioning ? coreBridge!.outputs.applicationFqdn : ''
output coreBridgeManagedIdentityClientId string = validatedCoreProvisioning ? coreBridge!.outputs.managedIdentityClientId : ''
