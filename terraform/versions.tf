terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0" # 4.x: Cilium data plane, service-mesh revisions pinning, requires subscription_id
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.4"
    }
  }
  # State is local for now. For a team/business, move to a remote azurerm backend:
  #   backend "azurerm" { resource_group_name = "tfstate-rg" storage_account_name = "..." container_name = "tfstate" key = "driftscope.tfstate" }
}
