# Per-env root: the azurerm remote backend (state key is per-env, set in backend.hcl:
# driftscope-<env>.tfstate) + provider requirements. `make up ENV=<env>` runs
# `terraform -chdir=terraform/envs/<env> init -backend-config=backend.hcl` against this.
terraform {
  required_version = ">= 1.5"
  backend "azurerm" {}
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
    azuread = { source = "hashicorp/azuread", version = "~> 3.0" }
    time    = { source = "hashicorp/time", version = "~> 0.11" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
