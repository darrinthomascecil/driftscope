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

# The app hostname points at the Front Door endpoint.
resource "azurerm_dns_cname_record" "app" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.subdomain
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record              = azurerm_cdn_frontdoor_endpoint.this.host_name
}
