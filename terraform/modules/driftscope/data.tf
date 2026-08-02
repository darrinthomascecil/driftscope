# Current caller identity (inherited azurerm/azuread providers): the tenant id default and the
# object id used as owner on the Entra app/group + the Key Vault Secrets Officer grant.
data "azurerm_client_config" "current" {}
data "azuread_client_config" "current" {}
