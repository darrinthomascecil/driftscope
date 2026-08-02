variable "subscription_id" {
  type        = string
  description = "Azure subscription id to deploy the shared resources into (same as the envs)."
}

variable "name_prefix" {
  type        = string
  default     = "driftscope"
  description = "Prefix for shared resource names. The shared RG is ${name_prefix}-shared."
}

variable "location" {
  type        = string
  default     = "eastus"
  description = "Azure region for the shared RG + ACR."
}

variable "acr_name" {
  type        = string
  description = "The ONE shared Azure Container Registry name (alphanumeric, globally unique). Every env references this by name + shared_resource_group."
}

variable "base_domain" {
  type        = string
  default     = null
  description = "Custom domain (optional). Set (e.g. \"acme.com\") to create the shared zone driftscope.<base_domain> that all envs put records in. Unset => no zone (envs run on *.azurefd.net)."
}
