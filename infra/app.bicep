targetScope = 'resourceGroup'

@description('Must match the foundation deployment prefix.')
@minLength(3)
@maxLength(20)
param namePrefix string = 'cosmaneura-pilot'

param location string = resourceGroup().location

@description('Immutable image tag previously built in the foundation ACR.')
param imageTag string

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

@description('Existing Azure Communication Services email endpoint; shared as non-secret policy metadata only.')
param acsEmailEndpoint string = ''

@description('Approved ACS sender address; shared as non-secret policy metadata only.')
param acsEmailSenderAddress string = ''

@description('Monitored reply-to address; shared as non-secret policy metadata only.')
param acsEmailReplyToAddress string = ''

@description('Enable API-only Azure OpenAI inference. Keep false until private networking, managed identity, telemetry, and synthetic model acceptance are complete.')
param enableAzureOpenAIInference bool = false

@description('Must match the immutable deployment created by the foundation template.')
param azureOpenAIDeploymentName string = 'gpt-5-6-sol-2026-07-09'

@description('Underlying model/version label reported in PHI-safe inference metadata.')
param azureOpenAIModelName string = 'gpt-5.6-sol'

@description('Azure OpenAI Responses data-plane API version used by the official OpenAI SDK.')
param azureOpenAIApiVersion string = '2025-04-01-preview'

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
var acrName = take('${compactPrefix}${suffix}', 50)
var keyVaultName = take('${compactPrefix}${suffix}', 24)
var azureOpenAIName = take('${compactPrefix}oai${suffix}', 64)
var appName = '${containerNamePrefix}-api-${suffix}'
var migrationJobName = '${containerNamePrefix}-mig-${suffix}'
var chaseWorkerJobName = '${containerNamePrefix}-wrk-${suffix}'
var image = '${registry.properties.loginServer}/cosmaneura-billing:${imageTag}'
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
var azureOpenAIEnabledValue = enableAzureOpenAIInference ? 'true' : 'false'
var chaseTransport = validatedPatientMessagingEnabled ? 'acs_email' : 'record_only'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource azureOpenAI 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (enableAzureOpenAIInference) {
  name: azureOpenAIName
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

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource entraClientSecretVault 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: entraClientSecretKeyVaultSecretName
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
      secrets: [
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
      ]
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
          env: [
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
          ]
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
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/database-url'
          identity: identity.id
        }
        {
          name: entraSecretSettingName
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/${entraClientSecretKeyVaultSecretName}'
          identity: identity.id
        }
        {
          name: 'chase-gate-hmac-key'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/chase-gate-hmac-key'
          identity: identity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          env: concat([
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
              value: directContractListPath
            }
          ], enableAzureOpenAIInference ? [
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
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
  dependsOn: [
    entraClientSecretVault
    entraClientSecretReader
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
        '/api/ready'
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
