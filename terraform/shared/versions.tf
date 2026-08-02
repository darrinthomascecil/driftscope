# Shared layer state: own key `shared.tfstate` in the same state storage account as the envs
# (make state-backend writes shared/backend.hcl). Kept separate so an env destroy never touches it.
terraform {
  required_version = ">= 1.5"
  backend "azurerm" {}
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}
