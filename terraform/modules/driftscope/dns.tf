# Azure DNS RECORDS for this env, inside the SHARED zone (data.azurerm_dns_zone.shared, owned by
# terraform/shared). Only created with a custom domain; demo mode runs on Front Door's *.azurefd.net
# host and none of this exists. The zone itself is never touched here -- so an env teardown removes
# only these records, and the registrar NS delegation (on the shared zone) stays put.

# Front Door managed-certificate validation TXT. Distinct name per env so three envs validate
# independently under one zone: _dnsauth (prod, apex host) / _dnsauth.<env> (lower).
resource "azurerm_dns_txt_record" "fd_validation" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.dnsauth_name
  zone_name           = data.azurerm_dns_zone.shared[0].name
  resource_group_name = var.shared_resource_group
  ttl                 = 3600
  record {
    value = azurerm_cdn_frontdoor_custom_domain.this[0].validation_token
  }
}

# The public app record -> Front Door endpoint. Lower envs are a CNAME at the "<env>" label
# (dev.driftscope.<domain>). Prod's host IS the zone apex, where a CNAME is illegal -> an ALIAS A
# record (below) instead.
resource "azurerm_dns_cname_record" "app" {
  count               = local.use_custom_domain && !local.is_prod ? 1 : 0
  name                = var.environment
  zone_name           = data.azurerm_dns_zone.shared[0].name
  resource_group_name = var.shared_resource_group
  ttl                 = 3600
  record              = azurerm_cdn_frontdoor_endpoint.this.host_name
}

# Prod: the app FQDN is the zone APEX. Azure DNS ALIAS records are allowed at the apex and can target
# a Front Door endpoint ("CNAME flattening"), which is how Front Door supports apex domains.
resource "azurerm_dns_a_record" "app_apex" {
  count               = local.use_custom_domain && local.is_prod ? 1 : 0
  name                = "@"
  zone_name           = data.azurerm_dns_zone.shared[0].name
  resource_group_name = var.shared_resource_group
  ttl                 = 3600
  target_resource_id  = azurerm_cdn_frontdoor_endpoint.this.id
}

# The ORIGIN hostname points straight at this env's gateway public IP (A record). This is the name
# Front Door dials for leg 2 and the name cert-manager issues the origin cert for. Distinct from the
# public app record so Front Door doesn't resolve the origin back to itself (loop): origin (prod) /
# origin.<env> (lower).
resource "azurerm_dns_a_record" "origin" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.origin_record_name
  zone_name           = data.azurerm_dns_zone.shared[0].name
  resource_group_name = var.shared_resource_group
  ttl                 = 3600
  records             = [azurerm_public_ip.gateway.ip_address]
}
