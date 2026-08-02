# Provider CONFIGURATION for this env root; the driftscope module inherits it.
provider "azurerm" {
  subscription_id = var.subscription_id
  features {
    key_vault {
      # Don't purge on destroy -- soft-delete + purge-protection keep secrets recoverable.
      purge_soft_delete_on_destroy = false
    }
  }
}

provider "azuread" {
  # Uses your az CLI login. The signed-in identity needs rights to create app
  # registrations and groups in the tenant.
}
