output "cluster_name" {
  value       = azurerm_kubernetes_cluster.this.name
  description = "AKS cluster name (for `az aks get-credentials`)."
}

output "resource_group" {
  value = azurerm_resource_group.this.name
}

output "host" {
  value       = local.host
  description = "Public hostname the app is served at."
}

output "oidc_issuer_url" {
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
  description = "Cluster OIDC issuer (for Workload Identity federated credentials, Phase 0.3)."
}

output "acr_login_server" {
  value       = azurerm_container_registry.this.login_server
  description = "Set this as the image registry in deploy/overlays/cloud/kustomization.yaml."
}

output "key_vault_name" {
  value = azurerm_key_vault.this.name
}

output "key_vault_uri" {
  value = azurerm_key_vault.this.vault_uri
}

output "kubelet_identity_object_id" {
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  description = "Kubelet identity granted AcrPull."
}

output "sso_client_id" {
  value       = azuread_application.sso.client_id
  description = "Entra app (client) id used by oauth2-proxy."
}

output "allowed_group_id" {
  value       = azuread_group.access.object_id
  description = "Object id of the access group (flows into params.yaml)."
}
