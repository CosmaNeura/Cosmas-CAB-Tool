targetScope = 'resourceGroup'

@description('Short, lowercase deployment prefix used in resource names.')
@minLength(3)
@maxLength(20)
param namePrefix string = 'cosmaneura-pilot'

@description('Azure region for the pilot resources.')
param location string = resourceGroup().location

@description('PostgreSQL administrator login. This is not an application user.')
param postgresAdminLogin string = 'cosmaneura_admin'

@secure()
@description('PostgreSQL administrator password. Supply it from an untracked parameter file.')
param postgresAdminPassword string

@description('Least-privilege PostgreSQL login used only by the running API.')
param postgresAppLogin string = 'cosmaneura_app'

@secure()
@description('Password for the least-privilege runtime login. Supply it from an untracked parameter file.')
param postgresAppPassword string

@description('Least-privilege PostgreSQL login used only by the scheduled chase worker.')
param postgresChaseWorkerLogin string = 'cosmaneura_chase_worker'

@secure()
@description('Password for the least-privilege chase worker login. Supply it from an untracked parameter file.')
param postgresChaseWorkerPassword string

@secure()
@minLength(32)
@description('Stable HMAC key shared only by the API and chase worker for direct-contract gate evidence.')
param chaseGateHmacKey string

@secure()
@description('Optional Azure Communication Services email connection string. It is stored for the chase worker only; leave empty until BAA, P5, and messaging activation review are complete.')
param acsEmailConnectionString string = ''

@secure()
@description('Optional Anthropic API credential for the API only. Before an Anthropic BAA/retention approval it may be used only for synthetic or de-identified acceptance.')
param anthropicApiKey string = ''

@description('Application database created on the Flexible Server.')
param databaseName string = 'cosmaneura'

@allowed([
  'Disabled'
  'Enabled'
])
@description('ACR public endpoint state. Keep Disabled except for the documented, authenticated bootstrap build window.')
param registryPublicNetworkAccess string = 'Disabled'

@description('Tags applied to every supported resource.')
param tags object = {
  application: 'cosmaneura-cab'
  environment: 'pilot'
  dataClassification: 'phi-capable-no-real-phi-yet'
}

var suffix = take(uniqueString(subscription().id, resourceGroup().id, namePrefix), 8)
var validatedPostgresAppLogin = toLower(postgresAdminLogin) == toLower(postgresAppLogin)
  ? fail('postgresAppLogin must differ from postgresAdminLogin')
  : postgresAppLogin
var postgresChaseWorkerLoginConflicts = toLower(postgresChaseWorkerLogin) == toLower(postgresAdminLogin) || toLower(postgresChaseWorkerLogin) == toLower(postgresAppLogin)
var validatedPostgresChaseWorkerLogin = postgresChaseWorkerLoginConflicts
  ? fail('postgresChaseWorkerLogin must differ from both PostgreSQL administrator and API runtime logins')
  : postgresChaseWorkerLogin
var validatedChaseGateHmacKey = chaseGateHmacKey == 'REPLACE_IN_UNTRACKED_PARAMETERS_FILE_32_BYTES_MINIMUM'
  ? fail('chaseGateHmacKey placeholder must be replaced with a generated secret')
  : chaseGateHmacKey
var compactPrefix = replace(toLower(namePrefix), '-', '')
var logAnalyticsName = '${namePrefix}-logs-${suffix}'
var appInsightsName = '${namePrefix}-insights-${suffix}'
var vnetName = '${namePrefix}-vnet-${suffix}'
var environmentName = '${namePrefix}-env-${suffix}'
var identityName = '${namePrefix}-identity-${suffix}'
var migrationIdentityName = '${namePrefix}-migration-identity-${suffix}'
var chaseWorkerIdentityName = '${namePrefix}-chase-identity-${suffix}'
var acrName = take('${compactPrefix}${suffix}', 50)
var keyVaultName = take('${compactPrefix}${suffix}', 24)
var postgresName = take('${namePrefix}-pg-${suffix}', 63)
var postgresHost = '${postgresName}.postgres.database.azure.com'
var encodedAdminPassword = uriComponent(postgresAdminPassword)
var encodedAppPassword = uriComponent(postgresAppPassword)
var encodedChaseWorkerPassword = uriComponent(postgresChaseWorkerPassword)
var migrationDatabaseUrl = 'postgresql://${postgresAdminLogin}:${encodedAdminPassword}@${postgresHost}:5432/${databaseName}?sslmode=require'
var runtimeDatabaseUrl = 'postgresql://${validatedPostgresAppLogin}:${encodedAppPassword}@${postgresHost}:5432/${databaseName}?sslmode=require'
var chaseWorkerDatabaseUrl = 'postgresql://${validatedPostgresChaseWorkerLogin}:${encodedChaseWorkerPassword}@${postgresHost}:5432/${databaseName}?sslmode=require'
var acsEmailConfigured = !empty(acsEmailConnectionString)
var anthropicConfigured = !empty(anthropicApiKey)

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    sku: {
      name: 'PerGB2018'
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: logAnalytics.id
    DisableLocalAuth: true
    SamplingPercentage: 100
  }
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.20.0.0/16'
      ]
    }
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'container-apps'
  properties: {
    addressPrefix: '10.20.0.0/23'
    delegations: [
      {
        name: 'container-apps-delegation'
        properties: {
          serviceName: 'Microsoft.App/environments'
        }
      }
    ]
  }
}

resource postgresSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'postgres'
  properties: {
    addressPrefix: '10.20.2.0/28'
    delegations: [
      {
        name: 'postgres-delegation'
        properties: {
          serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
        }
      }
    ]
  }
  // Azure serializes subnet mutations on a VNet, including no-op redeploys.
  dependsOn: [
    containerAppsSubnet
  ]
}

resource privateEndpointsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: 'private-endpoints'
  properties: {
    addressPrefix: '10.20.2.32/27'
    privateEndpointNetworkPolicies: 'Disabled'
  }
  // Keep sibling subnet PUTs ordered to avoid AnotherOperationInProgress.
  dependsOn: [
    postgresSubnet
  ]
}

resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource migrationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: migrationIdentityName
  location: location
  tags: tags
}

resource chaseWorkerIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: chaseWorkerIdentityName
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    publicNetworkAccess: registryPublicNetworkAccess
    zoneRedundancy: 'Disabled'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, workloadIdentity.id, 'acr-pull')
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource migrationAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, migrationIdentity.id, 'acr-pull')
  properties: {
    principalId: migrationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

resource chaseWorkerAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, chaseWorkerIdentity.id, 'acr-pull')
  properties: {
    principalId: chaseWorkerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

// The API publishes only the explicit numeric LLM instruments admitted by the
// application logging boundary. No worker/migration identity receives this
// permission and Azure auto-instrumentation remains disabled.
resource apiMetricsPublisher 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, workloadIdentity.id, 'monitoring-metrics-publisher')
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '3913510d-42f4-4e42-8a64-420c390055eb'
    )
  }
}

resource acrPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: tags
}

resource acrDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: acrPrivateDns
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource acrPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-acr-pe-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointsSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'acr'
        properties: {
          privateLinkServiceId: registry.id
          groupIds: [
            'registry'
          ]
        }
      }
    ]
  }
}

resource acrPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: acrPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'acr'
        properties: {
          privateDnsZoneId: acrPrivateDns.id
        }
      }
    ]
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    enablePurgeProtection: true
    enableRbacAuthorization: true
    enableSoftDelete: true
    publicNetworkAccess: 'Disabled'
    softDeleteRetentionInDays: 90
    sku: {
      family: 'A'
      name: 'standard'
    }
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

resource keyVaultPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.vaultcore.azure.net'
  location: 'global'
  tags: tags
}

resource keyVaultDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: keyVaultPrivateDns
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource keyVaultPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: '${namePrefix}-kv-pe-${suffix}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointsSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'vault'
        properties: {
          privateLinkServiceId: keyVault.id
          groupIds: [
            'vault'
          ]
        }
      }
    ]
  }
  // Both endpoints mutate the shared subnet while provisioning their NICs.
  dependsOn: [
    acrPrivateEndpoint
  ]
}

resource keyVaultPrivateDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: keyVaultPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'vault'
        properties: {
          privateDnsZoneId: keyVaultPrivateDns.id
        }
      }
    ]
  }
}

resource postgresPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource postgresDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: postgresPrivateDns
  name: '${vnetName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2025-08-01' = {
  name: postgresName
  location: location
  tags: tags
  sku: {
    name: 'Standard_D2ds_v5'
    tier: 'GeneralPurpose'
  }
  properties: {
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: postgresSubnet.id
      privateDnsZoneArmResourceId: postgresPrivateDns.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      autoGrow: 'Enabled'
      storageSizeGB: 32
    }
    version: '16'
  }
  dependsOn: [
    postgresDnsLink
  ]
}

resource applicationDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2025-08-01' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

resource postgresExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2025-08-01' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    source: 'user-override'
    value: 'VECTOR'
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'database-url'
  properties: {
    contentType: 'Least-privilege PostgreSQL connection string for the CAB runtime'
    value: runtimeDatabaseUrl
  }
  dependsOn: [
    applicationDatabase
  ]
}

resource migrationDatabaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'migration-database-url'
  properties: {
    contentType: 'PostgreSQL owner connection string for the manual migration job only'
    value: migrationDatabaseUrl
  }
  dependsOn: [
    applicationDatabase
  ]
}

resource runtimeDatabasePasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'database-runtime-password'
  properties: {
    contentType: 'Password used only by the migration job to provision the runtime role'
    value: postgresAppPassword
  }
  dependsOn: [
    applicationDatabase
  ]
}

resource chaseWorkerDatabaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'chase-worker-database-url'
  properties: {
    contentType: 'Practice-bound PostgreSQL connection string for the scheduled chase worker only'
    value: chaseWorkerDatabaseUrl
  }
  dependsOn: [
    applicationDatabase
  ]
}

resource chaseWorkerDatabasePasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'chase-worker-database-password'
  properties: {
    contentType: 'Password used only by the migration job to provision the chase worker role'
    value: postgresChaseWorkerPassword
  }
  dependsOn: [
    applicationDatabase
  ]
}

resource chaseGateHmacKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'chase-gate-hmac-key'
  properties: {
    contentType: 'Stable keyed identity for direct-contract roster evidence; shared by API and chase worker'
    value: validatedChaseGateHmacKey
  }
}

resource acsEmailConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (acsEmailConfigured) {
  parent: keyVault
  name: 'acs-email-connection-string'
  properties: {
    contentType: 'Azure Communication Services email credential for the chase worker only'
    value: acsEmailConnectionString
  }
}

resource anthropicApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (anthropicConfigured) {
  parent: keyVault
  name: 'anthropic-api-key'
  properties: {
    contentType: 'External model credential for the API only; real PHI requires an Anthropic BAA'
    value: anthropicApiKey
  }
}

resource runtimeDatabaseSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: databaseUrlSecret
  name: guid(databaseUrlSecret.id, workloadIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource migrationDatabaseSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: migrationDatabaseUrlSecret
  name: guid(migrationDatabaseUrlSecret.id, migrationIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: migrationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource migrationRuntimePasswordSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: runtimeDatabasePasswordSecret
  name: guid(runtimeDatabasePasswordSecret.id, migrationIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: migrationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource migrationChaseWorkerPasswordSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: chaseWorkerDatabasePasswordSecret
  name: guid(chaseWorkerDatabasePasswordSecret.id, migrationIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: migrationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource chaseWorkerDatabaseSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: chaseWorkerDatabaseUrlSecret
  name: guid(chaseWorkerDatabaseUrlSecret.id, chaseWorkerIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: chaseWorkerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource apiChaseGateSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: chaseGateHmacKeySecret
  name: guid(chaseGateHmacKeySecret.id, workloadIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource chaseWorkerGateSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: chaseGateHmacKeySecret
  name: guid(chaseGateHmacKeySecret.id, chaseWorkerIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: chaseWorkerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource chaseWorkerAcsEmailSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (acsEmailConfigured) {
  scope: acsEmailConnectionStringSecret
  name: guid(acsEmailConnectionStringSecret.id, chaseWorkerIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: chaseWorkerIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource apiAnthropicSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (anthropicConfigured) {
  scope: anthropicApiKeySecret
  name: guid(anthropicApiKeySecret.id, workloadIdentity.id, 'key-vault-secrets-user')
  properties: {
    principalId: workloadIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
    zoneRedundant: false
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output workloadIdentityName string = workloadIdentity.name
output migrationIdentityName string = migrationIdentity.name
output chaseWorkerIdentityName string = chaseWorkerIdentity.name
output keyVaultName string = keyVault.name
output postgresServerName string = postgres.name
output applicationInsightsName string = appInsights.name
