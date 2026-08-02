output "shared_resource_group" {
  value       = azurerm_resource_group.shared.name
  description = "Pass to each env as var.shared_resource_group."
}

output "acr_name" {
  value       = azurerm_container_registry.this.name
  description = "The shared ACR name -- set as the repo variable ACR_NAME and as each env's var.acr_name."
}

output "acr_login_server" {
  value       = azurerm_container_registry.this.login_server
  description = "<name>.azurecr.io -- the registry every env's kubelet pulls from."
}

output "dns_zone_name" {
  value       = local.zone_name
  description = "The shared zone (driftscope.<base_domain>), or \"\" in demo mode."
}

output "dns_name_servers" {
  value       = local.use_custom_domain ? azurerm_dns_zone.this[0].name_servers : []
  description = "Delegate ONCE: add ONE `driftscope` NS record at your registrar/Cloudflare pointing here. Never changes on an env teardown."
}
