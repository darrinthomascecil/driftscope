# GitHub Actions deploy identity (OIDC, keyless). The deploy + terraform pipelines federate as this
# User-Assigned Managed Identity to reach Azure with NO stored secret -- a federated credential
# trusts this repo's Actions OIDC token. Previously created by hand; codified here so a teardown +
# rebuild recreates it (end-to-end IaC). Terraform stays pure-Azure (ADR 0001): it makes the
# identity + grants + trust; setting the GitHub repo `AZURE_CLIENT_ID` variable from the output
# (gh_deploy_client_id) is a bootstrap/gh step, not a Terraform GitHub-provider dependency.

resource "azurerm_user_assigned_identity" "gh_deploy" {
  name                = "${var.name_prefix}-${var.environment}-gh-deploy"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
}

# Contributor on the deployment RG: (terraform.yml) manages this env's resources. Scoped to the RG,
# not the subscription -- the pipeline only ever touches this RG. NOTE: 0.6d moved the ACR OUT of this
# RG into the shared RG, so this grant no longer covers image push -- the shared-ACR grant below does.
resource "azurerm_role_assignment" "gh_deploy_contributor" {
  scope                = azurerm_resource_group.this.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.gh_deploy.principal_id
}

# Contributor on the SHARED ACR so this env's BUILD job can `az acr build` there. 0.6d relocated the
# ACR into driftscope-shared, so the deployment-RG grant above stopped covering it -- without this the
# build fails "registry could not be found" (the identity can't even see it). `az acr build` queues an
# ACR Task, which needs Contributor on the registry (AcrPush alone can't schedule a run). Scoped to the
# one shared registry; principal is THIS env's deploy identity, so per-env isolation still holds.
resource "azurerm_role_assignment" "gh_deploy_acr" {
  scope                = data.azurerm_container_registry.shared.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.gh_deploy.principal_id
}

# Trust the GitHub Actions OIDC issuer for THIS env's GitHub Environment (deploy.yml's per-env job
# gates on `environment: <github_environment>` -- dev/staging/production). Each env has its OWN
# gh-deploy identity, so one env's promotion job can never authenticate to another env's Azure. Two
# subjects because GitHub can emit either the name-based `sub` or its immutable numeric-id form;
# registering both means the token matches whichever is presented.
resource "azurerm_federated_identity_credential" "gh_deploy_prod" {
  name      = "gh-deploy-${var.environment}"
  parent_id = azurerm_user_assigned_identity.gh_deploy.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = "https://token.actions.githubusercontent.com"
  subject   = "repo:${var.github_repo}:environment:${local.github_environment}"
}

# Immutable numeric-id subject (repo:OWNER@<id>/REPO@<id>:...). Optional: created only when
# github_repo_ids is set. It survives an owner/repo RENAME (the numeric ids never change).
resource "azurerm_federated_identity_credential" "gh_deploy_prod_ids" {
  count     = var.github_repo_ids == "" ? 0 : 1
  name      = "gh-deploy-${var.environment}-ids"
  parent_id = azurerm_user_assigned_identity.gh_deploy.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = "https://token.actions.githubusercontent.com"
  subject   = "repo:${var.github_repo_ids}:environment:${local.github_environment}"
}

# The terraform.yml DRIFT job runs on schedule/workflow_dispatch (no `environment:` gate), so its
# OIDC subject is ref:refs/heads/main -- a distinct subject from deploy.yml's environment:production.
# Registering it makes the drift guardrail reproducibly authenticate when TF_DRIFT_ENABLED=true.
# The job is READ-ONLY (`terraform plan -lock=false`); only workflows running on main can assume it.
resource "azurerm_federated_identity_credential" "gh_deploy_main" {
  name      = "gh-deploy-${var.environment}-main"
  parent_id = azurerm_user_assigned_identity.gh_deploy.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = "https://token.actions.githubusercontent.com"
  subject   = "repo:${var.github_repo}:ref:refs/heads/main"
}

resource "azurerm_federated_identity_credential" "gh_deploy_main_ids" {
  count     = var.github_repo_ids == "" ? 0 : 1
  name      = "gh-deploy-${var.environment}-main-ids"
  parent_id = azurerm_user_assigned_identity.gh_deploy.id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = "https://token.actions.githubusercontent.com"
  subject   = "repo:${var.github_repo_ids}:ref:refs/heads/main"
}
