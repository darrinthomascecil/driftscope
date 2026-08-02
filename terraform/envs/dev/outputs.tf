# Re-export the module outputs so the bootstrap (scripts/bootstrap/*, `terraform output -raw <x>`)
# and `make url` read them from this env root. Generated pass-throughs -- identical in every env.
output "cluster_name" {
  value = module.driftscope.cluster_name
}

output "resource_group" {
  value = module.driftscope.resource_group
}

output "host" {
  value = module.driftscope.host
}

output "tenant_id" {
  value = module.driftscope.tenant_id
}

output "oidc_issuer_url" {
  value = module.driftscope.oidc_issuer_url
}

output "acr_login_server" {
  value = module.driftscope.acr_login_server
}

output "environment" {
  value = module.driftscope.environment
}

output "key_vault_name" {
  value = module.driftscope.key_vault_name
}

output "key_vault_uri" {
  value = module.driftscope.key_vault_uri
}

output "kubelet_identity_object_id" {
  value = module.driftscope.kubelet_identity_object_id
}

output "sso_client_id" {
  value = module.driftscope.sso_client_id
}

output "allowed_group_id" {
  value = module.driftscope.allowed_group_id
}

output "app_url" {
  value = module.driftscope.app_url
}

output "dns_name_servers" {
  value = module.driftscope.dns_name_servers
}

output "frontdoor_endpoint_host" {
  value = module.driftscope.frontdoor_endpoint_host
}

output "gateway_public_ip" {
  value = module.driftscope.gateway_public_ip
}

output "frontdoor_id" {
  value = module.driftscope.frontdoor_id
}

output "origin_fqdn" {
  value = module.driftscope.origin_fqdn
}

output "origin_host" {
  value = module.driftscope.origin_host
}

output "certmanager_client_id" {
  value = module.driftscope.certmanager_client_id
}

output "zone_name" {
  value = module.driftscope.zone_name
}

output "subscription_id" {
  value = module.driftscope.subscription_id
}

output "acme_email" {
  value = module.driftscope.acme_email
}

output "use_custom_domain" {
  value = module.driftscope.use_custom_domain
}

output "istio_revision" {
  value = module.driftscope.istio_revision
}

output "gateway_pip_name" {
  value = module.driftscope.gateway_pip_name
}

output "eso_client_id" {
  value = module.driftscope.eso_client_id
}

output "gh_deploy_client_id" {
  value = module.driftscope.gh_deploy_client_id
}

output "app_client_id" {
  value = module.driftscope.app_client_id
}

