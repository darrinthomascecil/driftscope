# Child module: declare provider requirements only. Provider CONFIGURATION (subscription, features,
# az-CLI auth) is inherited from the env root that calls this module (terraform/envs/<env>). No
# backend here -- state lives in the env root.
terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0" # 4.x: Cilium data plane, service-mesh revisions pinning, requires subscription_id
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = "~> 3.0" # Entra app registration + security group
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11" # rotate the app client secret on a schedule
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6" # generate the oauth2-proxy cookie secret (stored in Key Vault, read by ESO)
    }
  }
}
