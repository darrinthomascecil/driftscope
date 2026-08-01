# Azure DNS -- only created when you bring a custom domain (base_domain). Without one, the app
# runs on Front Door's default *.azurefd.net hostname and none of this exists.
# Delegate to Azure by adding the name servers (output `dns_name_servers`):
#   default              -> the whole base_domain, at your registrar.
#   delegate_subdomain   -> ONE `driftscope` NS record in your parent DNS (e.g. Cloudflare).
resource "azurerm_dns_zone" "this" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.zone_name
  resource_group_name = azurerm_resource_group.this.name
}

# Front Door managed-certificate validation TXT. The FQDN is always _dnsauth.driftscope.<base_domain>,
# but its NAME within the zone differs: `_dnsauth.driftscope` in the base_domain zone (default) vs
# `_dnsauth` at the apex of the driftscope.<base_domain> zone (delegate_subdomain).
resource "azurerm_dns_txt_record" "fd_validation" {
  count               = local.use_custom_domain ? 1 : 0
  name                = var.delegate_subdomain ? "_dnsauth" : "_dnsauth.${local.subdomain}"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record {
    value = azurerm_cdn_frontdoor_custom_domain.this[0].validation_token
  }
}

# The public app record -> Front Door endpoint.
# Default: a CNAME `driftscope` inside the base_domain zone.
resource "azurerm_dns_cname_record" "app" {
  count               = local.use_custom_domain && !var.delegate_subdomain ? 1 : 0
  name                = local.subdomain
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record              = azurerm_cdn_frontdoor_endpoint.this.host_name
}

# delegate_subdomain: the app FQDN is the zone APEX, where a CNAME is illegal. Azure DNS ALIAS
# records are allowed at the apex and can target a Front Door endpoint ("CNAME flattening"), which
# is how Front Door supports apex domains. Terraform manages it; Front Door's own IP is never pinned.
resource "azurerm_dns_a_record" "app_apex" {
  count               = local.use_custom_domain && var.delegate_subdomain ? 1 : 0
  name                = "@"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  target_resource_id  = azurerm_cdn_frontdoor_endpoint.this.id
}

# The ORIGIN hostname points straight at the gateway public IP (A record). This is the name
# Front Door dials for leg 2 and the name cert-manager issues the origin cert for. Distinct from
# the public app record above so Front Door doesn't resolve the origin back to itself (loop).
resource "azurerm_dns_a_record" "origin" {
  count               = local.use_custom_domain ? 1 : 0
  name                = "origin"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  records             = [azurerm_public_ip.gateway.ip_address]
}
