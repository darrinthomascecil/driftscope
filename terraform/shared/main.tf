# terraform/shared -- the long-lived resources common to ALL environments. Applied ONCE
# (`make shared-up`), never torn down casually. Kept separate (own state key `shared.tfstate`) so an
# env teardown/rebuild (`make down ENV=dev`) can never touch these:
#
#   * ACR      -- one registry. The deploy pipeline builds+signs ONCE; promotion moves the SAME
#                 digest dev->staging->prod (every env's kubelet has AcrPull on it), so there is no
#                 cross-registry copy on promotion.
#   * DNS zone -- driftscope.<base_domain>, delegated ONCE at the registrar/Cloudflare (one NS
#                 record that must never change). Per-env RECORDS live in each env's own state.
#
# The tfstate storage account is NOT managed here (a backend can't manage its own storage -- the
# bootstrap paradox); scripts/bootstrap/state-backend.sh creates that ONE shared account
# imperatively and writes per-env `backend.hcl` keys. This layer stays pure Azure infra.

locals {
  use_custom_domain = var.base_domain != null && var.base_domain != ""
  subdomain         = "driftscope"
  zone_name         = local.use_custom_domain ? "${local.subdomain}.${var.base_domain}" : ""
}

resource "azurerm_resource_group" "shared" {
  name     = "${var.name_prefix}-shared"
  location = var.location
}

# One registry for every environment. Build once, promote the digest -- see the module's aks_acr_pull
# grant (each env's kubelet gets AcrPull on THIS registry).
resource "azurerm_container_registry" "this" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.shared.name
  location            = azurerm_resource_group.shared.location
  sku                 = "Basic"
  admin_enabled       = false
}

# The shared, long-lived DNS zone. Delegate ONCE: add ONE `driftscope` NS record at your parent
# DNS/Cloudflare pointing at `dns_name_servers` (output). Per-env records (app/origin/_dnsauth) are
# created by each env in its own state, so this zone -- and its NS delegation -- outlives any env.
resource "azurerm_dns_zone" "this" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.zone_name
  resource_group_name = azurerm_resource_group.shared.name
}
