provider "azurerm" {
  subscription_id = var.subscription_id
  features {
    key_vault {
      # Don't purge on destroy -- soft-delete + purge-protection keep secrets recoverable.
      purge_soft_delete_on_destroy = false
    }
  }
}

data "azurerm_client_config" "current" {}
