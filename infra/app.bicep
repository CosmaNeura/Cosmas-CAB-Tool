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

@description('Secure card-upload URL used by chase templates.')
param uploadUrl string = 'https://example.invalid/insurance'

@description('Remain false until P2 authentication is deployed and verified.')
param externalIngress bool = false

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
var environmentName = '${namePrefix}-env-${suffix}'
var identityName = '${namePrefix}-identity-${suffix}'
var acrName = take('${compactPrefix}${suffix}', 50)
var keyVaultName = take('${compactPrefix}${suffix}', 24)
var appName = '${namePrefix}-api-${suffix}'
var migrationJobName = '${namePrefix}-migrate-${suffix}'
var image = '${registry.properties.loginServer}/cosmaneura-billing:${imageTag}'

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource migrationJob 'Microsoft.App/jobs@2026-01-01' = {
  name: migrationJobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
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
          identity: identity.id
        }
      ]
      secrets: [
        {
          name: 'database-url'
          keyVaultUrl: 'https://${keyVault.name}${az.environment().suffixes.keyvaultDns}/secrets/database-url'
          identity: identity.id
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
              secretRef: 'database-url'
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
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: image
          env: [
            {
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'COSMANEURA_ENV'
              value: 'production'
            }
            {
              name: 'COSMANEURA_FRONTEND_DIST'
              value: '/app/static'
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
              value: 'per_policy'
            }
            {
              name: 'CPT_AUTO_FETCH'
              value: '0'
            }
            {
              name: 'COSMANEURA_FEEDBACK_PATH'
              value: '/tmp/coder_feedback.jsonl'
            }
            {
              name: 'COSMANEURA_DIRECT_CONTRACT_LIST'
              value: '/tmp/direct_contract_patients.csv'
            }
          ]
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
}

output applicationName string = application.name
output migrationJobName string = migrationJob.name
output ingressExternal bool = externalIngress
