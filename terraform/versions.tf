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
  # Remote azurerm state (shared, locked, versioned) is the DEFAULT: `make up` runs `make state`,
  # which provisions the backend and generates backend.tf + backend.hcl. Opt out with LOCAL_STATE=1
  # (local state). A bare `terraform` run with no generated backend.tf still defaults to local.
  # See docs/remote-state.md.
}
