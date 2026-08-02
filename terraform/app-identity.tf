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

# The app UAMI client id is exposed as the `app_client_id` OUTPUT (outputs.tf). The cloud overlay's
# patch-sa.yaml is a committed REPLACE-* template; render-overlay.sh binds this value at deploy time
# (ADR 0001). Terraform no longer writes into the git tree -- it ends at outputs.

# Optional: grant the app identity Crypto User on the vault holding the shared GitHub App signing
# key, so the pod can KV-SIGN the App JWT (docs/forge-auth.md) -- the private key never leaves the
# vault. The vault may be EXTERNAL (a shared/permanent secrets vault in another RG), so it's a data
# source, not a managed resource. Set github_app_key_vault_name to enable private-repo scanning;
# empty -> skipped (public-repo only). Pairs with GITHUB_APP_* in the cloud overlay's config.env.
data "azurerm_key_vault" "github_app" {
  count               = var.github_app_key_vault_name == "" ? 0 : 1
  name                = var.github_app_key_vault_name
  resource_group_name = var.github_app_key_vault_rg
}

resource "azurerm_role_assignment" "app_github_app_kv_crypto" {
  count                = var.github_app_key_vault_name == "" ? 0 : 1
  scope                = data.azurerm_key_vault.github_app[0].id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}
