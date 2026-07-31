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

output "app_url" {
  value       = "https://${local.host}"
  description = "Where the app is served -- your custom domain, or the Front Door *.azurefd.net default."
}

output "dns_name_servers" {
  value       = local.use_custom_domain ? azurerm_dns_zone.this[0].name_servers : []
  description = "Delegate to Azure with these: the whole base_domain at your registrar (default), or ONE `driftscope` NS record in your parent DNS/Cloudflare (delegate_subdomain). Empty on the *.azurefd.net default host."
}

output "frontdoor_endpoint_host" {
  value       = azurerm_cdn_frontdoor_endpoint.this.host_name
  description = "Front Door endpoint hostname (the app CNAME points here)."
}

output "gateway_public_ip" {
  value       = azurerm_public_ip.gateway.ip_address
  description = "BYO public IP for the external Istio gateway; annotate its Service to use it (0.4b)."
}

output "frontdoor_id" {
  value       = azurerm_cdn_frontdoor_profile.this.resource_guid
  description = "Front Door GUID = the X-Azure-FDID the gateway validates to lock the origin (0.4b)."
}

output "origin_fqdn" {
  value       = azurerm_public_ip.gateway.fqdn
  description = "Gateway's cloudapp.azure.com FQDN -- the demo-mode (HTTP) origin. Production uses origin_host."
}

output "origin_host" {
  value       = local.origin_host
  description = "Name Front Door dials for leg 2: origin.<domain> (custom, DNS-01 cert) or the cloudapp FQDN (demo, HTTP)."
}

output "certmanager_client_id" {
  value       = try(azurerm_user_assigned_identity.certmanager[0].client_id, null)
  description = "Managed-identity client id for cert-manager Workload Identity (custom-domain mode). Feed to the cert-manager helm install -- see edge/README.md."
}

# ---- Consumed by the bootstrap (scripts/bootstrap/*, `make up`) so nothing is hardcoded ----
output "use_custom_domain" {
  value       = local.use_custom_domain
  description = "Whether a custom domain is configured (gates cert-manager + the origin cert in the bootstrap)."
}

output "istio_revision" {
  value       = var.istio_revision
  description = "Managed-Istio revision (drives the namespace istio.io/rev label + the istiod-<rev> rollout)."
}

output "gateway_pip_name" {
  value       = azurerm_public_ip.gateway.name
  description = "Name of the BYO gateway public IP, for the external gateway service annotation."
}

output "eso_client_id" {
  value       = azurerm_user_assigned_identity.eso.client_id
  description = "Managed-identity client id for External Secrets (Workload Identity). Flows into external-secrets.yaml."
}

output "app_client_id" {
  value       = azurerm_user_assigned_identity.app.client_id
  description = "Managed-identity client id for the driftscope app pod (Workload Identity). Annotate the driftscope ServiceAccount with it (azure.workload.identity/client-id) -- powers secretless ADO scanning and forge-auth Key Vault signing/secret reads."
}
