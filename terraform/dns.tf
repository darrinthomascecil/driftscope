# Azure DNS -- only created when you bring a custom domain (base_domain). Without one, the app
# runs on Front Door's default *.azurefd.net hostname and none of this exists.
# After apply, add the 4 name servers (output `dns_name_servers`) at your registrar to delegate.
resource "azurerm_dns_zone" "this" {
  count               = local.use_custom_domain ? 1 : 0
  name                = var.base_domain
  resource_group_name = azurerm_resource_group.this.name
}

# Front Door managed-certificate validation record (TXT at _dnsauth.<subdomain>).
resource "azurerm_dns_txt_record" "fd_validation" {
  count               = local.use_custom_domain ? 1 : 0
  name                = "_dnsauth.${local.subdomain}"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record {
    value = azurerm_cdn_frontdoor_custom_domain.this[0].validation_token
  }
}

# The public app hostname points at the Front Door endpoint (CNAME -> *.azurefd.net).
resource "azurerm_dns_cname_record" "app" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.subdomain
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record              = azurerm_cdn_frontdoor_endpoint.this.host_name
}

# The ORIGIN hostname points straight at the gateway public IP (A record). This is the name
# Front Door dials for leg 2 and the name cert-manager issues the origin cert for. Distinct from
# the public CNAME above so Front Door doesn't resolve the origin back to itself (loop).
resource "azurerm_dns_a_record" "origin" {
  count               = local.use_custom_domain ? 1 : 0
  name                = "origin"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  records             = [azurerm_public_ip.gateway.ip_address]
}
