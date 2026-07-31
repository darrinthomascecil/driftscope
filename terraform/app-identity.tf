# The driftscope APPLICATION's Azure identity: Workload Identity for the app pod itself.
#
# Two things need the pod to authenticate to Azure as itself, with NO stored secret:
#
#   1. Secretless Azure DevOps scanning -- `source.py` exchanges the pod's projected federated
#      token for an ADO-scoped AAD token (no PAT); the path the base Deployment was built for.
#   2. Bring-your-own forge identities (docs/forge-auth.md) -- the server signs each GitHub
#      App's JWT with Key Vault's `sign` (Crypto User) and reads each ADO Entra app's client
#      secret from Key Vault (Secrets User), reaching the vault as THIS identity.
#
# Federated to the `driftscope` service account, so annotating that SA with
# `azure.workload.identity/client-id = app_client_id` is the only in-cluster wiring the pod
# needs (see the `app_client_id` output). The grants below are on THIS deployment's own vault; a
# tenant who stores their GitHub App key / ADO secret in a different vault grants this same
# identity Crypto User / Secrets User there too.

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.name_prefix}-app"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
}

# Trust the cluster's OIDC issuer to mint tokens for the driftscope service account. The subject
# MUST match the SA the app pods run as (base/serviceaccount.yaml: driftscope/driftscope), which
# is stable across overlays so this federated subject never changes.
resource "azurerm_federated_identity_credential" "app" {
  name      = "app"
  parent_id = azurerm_user_assigned_identity.app.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject   = "system:serviceaccount:driftscope:driftscope"
}

# Sign GitHub App JWTs with keys held in the vault (the private key never leaves it). Crypto User
# is the least-privilege data-plane role that permits `sign` -- it cannot create, import, or
# export keys, only use them.
resource "azurerm_role_assignment" "app_kv_crypto_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Read ADO Entra apps' client secrets (bring-your-own `client_secret` identities). Read secret
# VALUES only -- it cannot list, set, or delete.
resource "azurerm_role_assignment" "app_kv_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Generate the cloud overlay's SA patch with the real client id, the same way params.tf /
# secrets.tf generate their manifests -- so the annotation is always correct and can never drift
# out of sync with the UAMI (which is how it got silently stripped before). `terraform apply`
# reproduces the committed reference identically when tfvars match.
resource "local_file" "patch_sa" {
  filename        = "${path.module}/../deploy/overlays/cloud/patch-sa.yaml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/patch-sa.yaml.tftpl", {
    app_client_id = azurerm_user_assigned_identity.app.client_id
  })
}
