# driftscope staging environment root. Isolated state (backend.hcl key=driftscope-staging.tfstate),
# isolated tfvars (staging.tfvars). Run explicitly: `terraform -chdir=terraform/envs/staging apply`
# (or `make up ENV=staging`) -- there is no hidden workspace to fat-finger prod.
module "driftscope" {
  source = "../../modules/driftscope"

  environment               = "staging"
  subscription_id           = var.subscription_id
  shared_resource_group     = var.shared_resource_group
  name_prefix               = var.name_prefix
  location                  = var.location
  base_domain               = var.base_domain
  acme_email                = var.acme_email
  tenant_id                 = var.tenant_id
  allowed_group_name        = var.allowed_group_name
  admin_members             = var.admin_members
  github_app_key_vault_name = var.github_app_key_vault_name
  github_app_key_vault_rg   = var.github_app_key_vault_rg
  github_repo               = var.github_repo
  github_repo_ids           = var.github_repo_ids
  acr_name                  = var.acr_name
  key_vault_name            = var.key_vault_name
  gateway_dns_label         = var.gateway_dns_label
  kubernetes_version        = var.kubernetes_version
  node_vm_size              = var.node_vm_size
  node_count                = var.node_count
  istio_revision            = var.istio_revision
}
