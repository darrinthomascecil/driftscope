# External Secrets: let the cluster pull the oauth2-proxy credentials from Key Vault itself,
# instead of a human running `kubectl create secret` (Phase 0.6b). The External Secrets Operator
# authenticates to Key Vault with WORKLOAD IDENTITY (a managed identity federated to the
# driftscope-eso service account) holding Key Vault Secrets User -- no client secret, no keys.
#
# The cookie secret is generated HERE and stored in Key Vault too, so all three oauth2-proxy
# secret keys come from one source (and the cookie secret is now STABLE across deploys, instead
# of being regenerated every `make secret` -- which used to invalidate all live sessions).

# 16 bytes -> 32 hex chars, exactly the 32-byte string oauth2-proxy accepts as its cookie secret.
resource "random_id" "cookie" {
  byte_length = 16
}

resource "azurerm_key_vault_secret" "oauth2_cookie_secret" {
  name         = "oauth2-proxy-cookie-secret"
  value        = random_id.cookie.hex
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.kv_secrets_officer]
}

# ---- ESO's identity: federated to the driftscope-eso SA, read-only on Key Vault secrets ----
resource "azurerm_user_assigned_identity" "eso" {
  name                = "${var.name_prefix}-${var.environment}-eso"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
}

resource "azurerm_federated_identity_credential" "eso" {
  name      = "eso"
  parent_id = azurerm_user_assigned_identity.eso.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject   = "system:serviceaccount:driftscope:driftscope-eso"
}

resource "azurerm_role_assignment" "eso_kv_secrets_user" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets User" # read secret VALUES, nothing else
  principal_id         = azurerm_user_assigned_identity.eso.principal_id
}

# The ESO client id + vault are exposed as OUTPUTS (eso_client_id, key_vault_name/uri; outputs.tf).
# The in-cluster wiring (external-secrets.yaml) is a committed REPLACE-* template; render-overlay.sh
# binds these at deploy time (ADR 0001). Terraform no longer writes into the git tree.
