# ---- Identity of the deployment (set these in terraform.tfvars) ----
variable "subscription_id" {
  type        = string
  description = "Azure subscription id to deploy into."
}


variable "shared_resource_group" {
  type        = string
  description = "Resource group of the SHARED, long-lived resources (terraform/shared): the ACR and the DNS zone. Referenced (data sources), never created by an env."
}

variable "name_prefix" {
  type        = string
  default     = "driftscope"
  description = "Prefix for resource names (RG, AKS, ...). Keep short; must be globally unique-ish for ACR/KV."
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region."
}

# ---- The public identity of YOUR deployment (drives the app config / params.yaml) ----
variable "base_domain" {
  type    = string
  default = null
  # LEAVE UNSET to run on Front Door's default *.azurefd.net hostname -- no domain ownership,
  # no Azure DNS zone, no registrar delegation needed (great for demos / clone-and-run).
  # SET it (e.g. "acme.com") to serve at driftscope.<base_domain> with a managed cert + Azure DNS.
  description = "Custom domain (optional). Unset => Front Door default *.azurefd.net hostname."
}

variable "acme_email" {
  type    = string
  default = ""
  # Used only in custom-domain mode, for the Let's Encrypt account that issues the ORIGIN cert
  # (cert-manager DNS-01). Optional -- Let's Encrypt allows an anonymous account -- but set it to
  # get expiry/renewal-failure notifications. The public (browser) cert is Front Door's, not this.
  description = "Email for the Let's Encrypt ACME account behind the origin cert (custom-domain mode only)."
}

variable "tenant_id" {
  type        = string
  default     = null
  description = "Entra tenant id for SSO. Defaults to the current az login tenant."
}

variable "allowed_group_name" {
  type        = string
  default     = "driftscope"
  description = "Display name of the access group Terraform creates (shown on the no-access page)."
}

variable "admin_members" {
  type        = list(string)
  default     = []
  description = "UPNs to add to the access group initially (e.g. [\"you@your-tenant.onmicrosoft.com\"]). Optional; membership can also be managed outside Terraform."
}

# ---- Shared GitHub App signing key (optional; enables private-repo scanning) ----
variable "github_app_key_vault_name" {
  type        = string
  default     = ""
  description = "Key Vault holding the shared GitHub App's signing key (gh-app-key). Empty -> no grant (public-repo scanning only). May be an EXISTING external vault (a shared secrets vault); the app identity is granted Key Vault Crypto User on it to KV-sign the App JWT -- the key never leaves the vault. Pair with GITHUB_APP_* in deploy/overlays/cloud/config.env."
}

variable "github_app_key_vault_rg" {
  type        = string
  default     = ""
  description = "Resource group of github_app_key_vault_name (may differ from the deployment RG)."
}

# ---- GitHub Actions deploy identity (OIDC federation, gh-deploy-identity.tf) ----
variable "github_repo" {
  type        = string
  description = "GitHub OWNER/REPO the Actions pipelines run from (e.g. \"acme/driftscope\"). The deploy identity's federated credential trusts repo:<this>:environment:production."
}

variable "github_repo_ids" {
  type        = string
  default     = ""
  description = "Optional immutable numeric-id form of github_repo (repo:OWNER@<id>/REPO@<id>). Registers a second federated credential that survives an owner/repo rename. Empty -> not created."
}

# ---- Resource names (must be globally unique for ACR + Key Vault) ----
variable "acr_name" {
  type        = string
  description = "Azure Container Registry name (alphanumeric, globally unique)."
}

variable "key_vault_name" {
  type        = string
  description = "Key Vault name (globally unique)."
}

variable "gateway_dns_label" {
  type        = string
  description = "DNS label for the gateway's public IP -> <label>.<region>.cloudapp.azure.com (unique per region). This is the demo-mode (HTTP) origin Front Door dials; custom-domain mode uses origin.<domain> with a DNS-01 cert instead."
}

# ---- Cluster shape ----
variable "kubernetes_version" {
  type        = string
  default     = null # null => AKS default. Pin for reproducibility once chosen.
  description = "Kubernetes version (e.g. 1.35)."
}

variable "node_vm_size" {
  type        = string
  default     = "Standard_B4ms"
  description = "Node VM size. B-series is burstable; D-series gives sustained network (see docs/design)."
}

variable "node_count" {
  type        = number
  default     = 1
  description = "Node count. 1 for dev; >=2 for HA + rollout headroom (see stress-test findings)."
}

variable "istio_revision" {
  type        = string
  default     = "asm-1-29"
  description = "Managed Istio (asm) revision. Bump on mesh upgrades."
}
