# Entra (Azure AD) automation: the SSO app registration + the access group. Replaces the
# by-hand `az ad app create` / `az ad group create` steps so a new tenant is set up from code.

# ---- The access group; its object id flows straight into params.yaml (params.tf) ----
resource "azuread_group" "access" {
  display_name     = var.allowed_group_name
  security_enabled = true
  mail_enabled     = false
  owners           = [data.azuread_client_config.current.object_id]
}

# Optional initial members (UPNs). Membership can also be managed outside Terraform.
data "azuread_user" "admins" {
  for_each            = toset(var.admin_members)
  user_principal_name = each.value
}
resource "azuread_group_member" "admins" {
  for_each         = data.azuread_user.admins
  group_object_id  = azuread_group.access.object_id
  member_object_id = each.value.object_id
}

# ---- The SSO app registration (single-tenant, emits security-group claims) ----
resource "azuread_application" "sso" {
  display_name            = "${var.name_prefix}-sso"
  sign_in_audience        = "AzureADMyOrg"
  owners                  = [data.azuread_client_config.current.object_id]
  group_membership_claims = ["SecurityGroup"] # so tokens carry the user's group object ids

  web {
    redirect_uris = ["https://${local.host}/oauth2/callback"]
  }

  # Microsoft Graph delegated scopes for OIDC login.
  required_resource_access {
    resource_app_id = "00000003-0000-0000-c000-000000000000" # Microsoft Graph
    resource_access {
      id   = "37f7f235-527c-4136-accd-4a02d197296e" # openid
      type = "Scope"
    }
    resource_access {
      id   = "14dad69e-099b-42c9-810b-d002981feec1" # profile
      type = "Scope"
    }
    resource_access {
      id   = "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0" # email
      type = "Scope"
    }
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "sso" {
  client_id = azuread_application.sso.client_id
  owners    = [data.azuread_client_config.current.object_id]
}

# Rotate the client secret every 300 days; end_date is 1 year out from each rotation.
resource "time_rotating" "sso_secret" {
  rotation_days = 300
}

resource "azuread_application_password" "sso" {
  application_id      = azuread_application.sso.id
  display_name        = "oauth2-proxy"
  end_date            = timeadd(time_rotating.sso_secret.rfc3339, "8760h")
  rotate_when_changed = { rotation = time_rotating.sso_secret.id }
}

# ---- Stash the app credentials in Key Vault (oauth2-proxy reads them from there) ----
# The signed-in identity needs Key Vault Secrets Officer to write; grant it, then write.
resource "azurerm_role_assignment" "kv_secrets_officer" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_key_vault_secret" "oauth2_client_id" {
  name         = "oauth2-proxy-client-id"
  value        = azuread_application.sso.client_id
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.kv_secrets_officer]
}

resource "azurerm_key_vault_secret" "oauth2_client_secret" {
  name         = "oauth2-proxy-client-secret"
  value        = azuread_application_password.sso.value
  key_vault_id = azurerm_key_vault.this.id
  depends_on   = [azurerm_role_assignment.kv_secrets_officer]
}
