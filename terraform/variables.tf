# ---- Identity of the deployment (set these in terraform.tfvars) ----
variable "subscription_id" {
  type        = string
  description = "Azure subscription id to deploy into."
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
  type        = string
  description = "Your domain. The app is served at driftscope.<base_domain> (e.g. acme.com)."
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

# ---- Resource names (must be globally unique for ACR + Key Vault) ----
variable "acr_name" {
  type        = string
  description = "Azure Container Registry name (alphanumeric, globally unique)."
}

variable "key_vault_name" {
  type        = string
  description = "Key Vault name (globally unique)."
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
