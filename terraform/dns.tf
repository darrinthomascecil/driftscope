# Azure DNS is authoritative for your domain. After apply, add the 4 name servers
# (output `dns_name_servers`) at your registrar to delegate the zone.
resource "azurerm_dns_zone" "this" {
  name                = var.base_domain
  resource_group_name = azurerm_resource_group.this.name
}

# Front Door managed-certificate validation record (TXT at _dnsauth.<subdomain>).
resource "azurerm_dns_txt_record" "fd_validation" {
  name                = "_dnsauth.${local.subdomain}"
  zone_name           = azurerm_dns_zone.this.name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record {
    value = azurerm_cdn_frontdoor_custom_domain.this.validation_token
  }
}

# The app hostname points at the Front Door endpoint.
resource "azurerm_dns_cname_record" "app" {
  name                = local.subdomain
  zone_name           = azurerm_dns_zone.this.name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  record              = azurerm_cdn_frontdoor_endpoint.this.host_name
}
