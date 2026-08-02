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

# ---- Monitoring stack secrets (deploy/monitoring: Prometheus/Grafana/Alertmanager) ----
# Same ESO-from-Key-Vault pattern as oauth2-proxy: the values live in KV and ESO syncs them into the
# `monitoring` namespace via Workload Identity, so nothing secret is committed (ADR 0001).

# Grafana admin password: generated once, stored in KV. Grafana runs anonymous Viewer for read-only,
# so this only guards admin login. Alphanumeric to stay shell/URL-safe.
resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

resource "azurerm_key_vault_secret" "grafana_admin_password" {
  name         = "grafana-admin-password"
  value        = random_password.grafana_admin.result
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.kv_secrets_officer]
}

# Alertmanager webhook: seeded with a placeholder so ESO always has something to sync (delivery fails
# harmlessly until it is real). Set the real Slack/PagerDuty URL by updating the VALUE in Key Vault;
# ignore_changes keeps a later `terraform apply` from clobbering it.
resource "azurerm_key_vault_secret" "alertmanager_webhook_url" {
  name         = "alertmanager-webhook-url"
  value        = "https://example.invalid/set-me-in-key-vault"
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.kv_secrets_officer]
  lifecycle {
    ignore_changes = [value] # a human sets the real webhook directly in KV
  }
}

# ESO also runs in the `monitoring` namespace (deploy/monitoring/external-secrets.yaml uses a
# driftscope-eso SA there). Federate the SAME eso identity to that SA -- it already holds Key Vault
# Secrets User above, so the monitoring ExternalSecrets need no new grant.
resource "azurerm_federated_identity_credential" "eso_monitoring" {
  name      = "eso-monitoring"
  parent_id = azurerm_user_assigned_identity.eso.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject   = "system:serviceaccount:monitoring:driftscope-eso"
}
