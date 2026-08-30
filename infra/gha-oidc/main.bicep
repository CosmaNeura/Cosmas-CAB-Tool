targetScope = 'resourceGroup'

metadata name = 'Cosmas CAB GitHub Actions OIDC identity'
metadata description = '''
Creates the dedicated, secretless Azure principal used only by the
CosmaNeura/Cosmas-CAB-Tool Build & Deploy workflow. The identity trusts the
workflow's main-branch build job and pilot-environment deploy job and receives
RBAC only on the registry and three workload resources that workflow operates.
'''

var identityName = 'cosmaneura-pilot-gha-identity-i4akwqry'
var registryName = 'cosmaneurapiloti4akwqry'
var apiAppName = 'cosmaneura-pilot-api-i4akwqry'
var migrationJobName = 'cosmaneura-pilot-mig-i4akwqry'
var workerJobName = 'cosmaneura-pilot-wrk-i4akwqry'

var githubOidcIssuer = 'https://token.actions.githubusercontent.com'
var azureTokenExchangeAudience = 'api://AzureADTokenExchange'
var mainBranchSubject = 'repo:CosmaNeura/Cosmas-CAB-Tool:ref:refs/heads/main'
var pilotEnvironmentSubject = 'repo:CosmaNeura/Cosmas-CAB-Tool:environment:pilot'

var acrPushRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '8311e382-0749-4cb8-b61a-304f252e45ec'
)
var containerAppsContributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '358470bc-b998-42bd-ab17-a7e34c199c0f'
)
var containerAppsJobsContributorRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '4e3d2b60-56ae-4dc6-a233-09c8e5a82e68'
)

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource apiApp 'Microsoft.App/containerApps@2026-01-01' existing = {
  name: apiAppName
}

resource migrationJob 'Microsoft.App/jobs@2026-01-01' existing = {
  name: migrationJobName
}

resource workerJob 'Microsoft.App/jobs@2026-01-01' existing = {
  name: workerJobName
}

resource githubActionsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: identityName
  location: resourceGroup().location
  tags: union(resourceGroup().tags, {
    component: 'github-actions-identity'
    purpose: 'cab-build-and-deploy'
  })
}

resource mainBranchCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: githubActionsIdentity
  name: 'github-main-build-publish'
  properties: {
    audiences: [
      azureTokenExchangeAudience
    ]
    issuer: githubOidcIssuer
    subject: mainBranchSubject
  }
}

resource pilotEnvironmentCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2024-11-30' = {
  parent: githubActionsIdentity
  name: 'github-pilot-deploy'
  properties: {
    audiences: [
      azureTokenExchangeAudience
    ]
    issuer: githubOidcIssuer
    subject: pilotEnvironmentSubject
  }
}

resource registryPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, githubActionsIdentity.id, acrPushRoleDefinitionId)
  properties: {
    description: 'Push reviewed Cosmas CAB images from GitHub Actions.'
    principalId: githubActionsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPushRoleDefinitionId
  }
}

// The workflow updates the API image. Container Apps Operator is read/action
// only and cannot perform that write, so use the resource-specific contributor
// role at the exact app scope rather than broad Contributor at resource-group scope.
resource apiAppOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: apiApp
  name: guid(apiApp.id, githubActionsIdentity.id, containerAppsContributorRoleDefinitionId)
  properties: {
    description: 'Update and inspect only the Cosmas CAB pilot API app.'
    principalId: githubActionsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: containerAppsContributorRoleDefinitionId
  }
}

// The workflow updates and starts this job. Container Apps Jobs Operator can
// start it but cannot update its image, so the job-specific contributor role is
// required and remains scoped to this one job.
resource migrationJobOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: migrationJob
  name: guid(migrationJob.id, githubActionsIdentity.id, containerAppsJobsContributorRoleDefinitionId)
  properties: {
    description: 'Update, start, and inspect only the Cosmas CAB migration job.'
    principalId: githubActionsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: containerAppsJobsContributorRoleDefinitionId
  }
}

resource workerJobOperator 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workerJob
  name: guid(workerJob.id, githubActionsIdentity.id, containerAppsJobsContributorRoleDefinitionId)
  properties: {
    description: 'Update and inspect only the Cosmas CAB chase-worker job.'
    principalId: githubActionsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: containerAppsJobsContributorRoleDefinitionId
  }
}

output azureClientId string = githubActionsIdentity.properties.clientId
output principalId string = githubActionsIdentity.properties.principalId
output identityResourceId string = githubActionsIdentity.id
output federatedSubjects array = [
  mainBranchCredential.properties.subject
  pilotEnvironmentCredential.properties.subject
]
output assignedRoles array = [
  {
    role: 'AcrPush'
    scope: registry.id
  }
  {
    role: 'Container Apps Contributor'
    scope: apiApp.id
  }
  {
    role: 'Container Apps Jobs Contributor'
    scope: migrationJob.id
  }
  {
    role: 'Container Apps Jobs Contributor'
    scope: workerJob.id
  }
]
