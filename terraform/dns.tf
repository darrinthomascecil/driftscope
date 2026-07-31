# Azure DNS -- AZURE mode only (dns_provider = "azure"). Terraform owns the whole zone + records;
# after apply, delegate the 4 name servers (output `dns_name_servers`) at your registrar.
# In CLOUDFLARE mode none of this exists: DNS stays external and the records are added by hand
# (output `cloudflare_records`); only the `acme` delegation zone below is created.
resource "azurerm_dns_zone" "this" {
  count               = local.dns_in_azure ? 1 : 0
  name                = var.base_domain
  resource_group_name = azurerm_resource_group.this.name
}

# Front Door managed-certificate validation record (TXT at _dnsauth.<subdomain>).
resource "azurerm_dns_txt_record" "fd_validation" {
  count               = local.dns_in_azure ? 1 : 0
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
  count               = local.dns_in_azure ? 1 : 0
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
  count               = local.dns_in_azure ? 1 : 0
  name                = "origin"
  zone_name           = azurerm_dns_zone.this[0].name
  resource_group_name = azurerm_resource_group.this.name
  ttl                 = 3600
  records             = [azurerm_public_ip.gateway.ip_address]
}

# CLOUDFLARE mode only: a small delegated zone so cert-manager's DNS-01 challenge for the ORIGIN
# cert can be answered in Azure without any Cloudflare API access. You add ONE static NS record in
# Cloudflare -- `acme` -> these name servers (output `acme_delegation_name_servers`) -- and one
# static CNAME (`_acme-challenge.origin` -> origin.acme.<domain>). cert-manager (cnameStrategy:
# Follow) then writes the TXT here via Workload Identity. Cloudflare stays authoritative for the
# apex and never needs a token.
resource "azurerm_dns_zone" "acme" {
  count               = local.dns_in_cloudflare ? 1 : 0
  name                = local.acme_zone
  resource_group_name = azurerm_resource_group.this.name
}
