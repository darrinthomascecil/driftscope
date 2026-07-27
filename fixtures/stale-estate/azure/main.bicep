param location string = resourceGroup().location

resource account 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: 'legacy-ai'
  location: location
  sku: { name: 'S0' }
  kind: 'AIServices'
}

resource vault 'Microsoft.KeyVault/vaults@2021-10-01' = {
  name: 'legacy-kv'
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
  }
}

resource preview 'Microsoft.App/containerApps@2023-04-01-preview' = {
  name: 'legacy-app'
  location: location
  properties: {}
}
