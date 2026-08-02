locals {
  tenant_id         = coalesce(var.tenant_id, data.azurerm_client_config.current.tenant_id)
  use_custom_domain = var.base_domain != null && var.base_domain != ""
  subdomain         = "driftscope"
  is_prod           = var.environment == "prod"

  # The GitHub Environment this env's deploy job runs in (deploy.yml gates on it, and the gh-deploy
  # federated credential trusts environment:<this>). "prod" -> "production" (the conventional name
  # + optional required-reviewers gate); dev/staging keep their own names.
  github_environment = local.is_prod ? "production" : var.environment

  # The DNS zone is SHARED and long-lived (terraform/shared): always driftscope.<base_domain>,
  # delegated once at the registrar/Cloudflare (its NS must never change on an env teardown). Each
  # env owns only RECORDS in it (a data source here, never the zone resource) so
  # `terraform -chdir=envs/<env> destroy` removes this env's records, never the zone.
  zone_name = local.use_custom_domain ? "${local.subdomain}.${var.base_domain}" : ""

  # Public host: prod sits at the zone APEX (driftscope.<domain>); lower envs at <env>.<zone>
  # (dev.driftscope.<domain>, staging.driftscope.<domain>). No domain -> the *.azurefd.net host.
  host_prefix = local.is_prod ? "" : "${var.environment}."
  host        = local.use_custom_domain ? "${local.host_prefix}${local.zone_name}" : azurerm_cdn_frontdoor_endpoint.this.host_name

  # Record NAMES within the shared zone, distinct per env so three envs coexist under one zone:
  #   app     -> apex "@" (prod, ALIAS A) or the "<env>" label (lower, CNAME)
  #   origin  -> origin (prod) / origin.<env> (lower)  -- Front Door's leg-2 target (A -> gateway IP)
  #   dnsauth -> _dnsauth (prod) / _dnsauth.<env>      -- Front Door managed-cert validation TXT
  origin_record_name = local.is_prod ? "origin" : "origin.${var.environment}"
  dnsauth_name       = local.is_prod ? "_dnsauth" : "_dnsauth.${var.environment}"

  # The name Front Door DIALS to reach the origin (SNI + DNS target), distinct from the public
  # `host`. Always a child of the zone: origin[.<env>].<zone> (A -> gateway IP) with a cert-manager
  # DNS-01 cert. No domain: the gateway's cloudapp FQDN, reached over HTTP (no cert issuable there).
  origin_host      = local.use_custom_domain ? "${local.origin_record_name}.${local.zone_name}" : azurerm_public_ip.gateway.fqdn
  use_https_origin = local.use_custom_domain # Front Door -> origin is HTTPS only when we can issue a real cert
}

# The SHARED ACR + DNS zone (terraform/shared) are referenced, never recreated -- one registry so a
# promoted digest is reachable by every env's kubelet (no cross-registry copy), one zone so the
# registrar NS delegation is stable across env teardowns.
data "azurerm_container_registry" "shared" {
  name                = var.acr_name
  resource_group_name = var.shared_resource_group
}

data "azurerm_dns_zone" "shared" {
  count               = local.use_custom_domain ? 1 : 0
  name                = local.zone_name
  resource_group_name = var.shared_resource_group
}

resource "azurerm_resource_group" "this" {
  name     = "${var.name_prefix}-${var.environment}-cloud"
  location = var.location
}

resource "azurerm_key_vault" "this" {
  name                       = var.key_vault_name
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  tenant_id                  = local.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = true # improvement over the hand-built cluster (was off)
  soft_delete_retention_days = 7
}

resource "azurerm_kubernetes_cluster" "this" {
  name                = "${var.name_prefix}-${var.environment}-aks"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = "${var.name_prefix}-${var.environment}"
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"

  oidc_issuer_enabled       = true # for Workload Identity federated creds
  workload_identity_enabled = true

  default_node_pool {
    name            = "system"
    vm_size         = var.node_vm_size
    node_count      = var.node_count
    max_pods        = 250
    os_disk_size_gb = 128
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "cilium" # <-- enforces NetworkPolicy. The hand-built cluster had `none`,
    network_data_plane  = "cilium" #     which made the egress policy inert (stress-test finding T2).
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.0.0.0/16"
    dns_service_ip      = "10.0.0.10"
  }

  service_mesh_profile {
    mode      = "Istio"
    revisions = [var.istio_revision]
    # Front Door reaches the EXTERNAL gateway (public IP). The tunnel (internal gateway) is gone.
    external_ingress_gateway_enabled = true
  }
}

# Let this env's kubelet identity pull images from the SHARED ACR (no admin creds). The grant lives
# in the ENV module (not shared): scope is the shared ACR (data source), principal is THIS cluster's
# kubelet -- so the dependency runs env -> shared only, never shared -> every env's kubelet (a cycle).
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = data.azurerm_container_registry.shared.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}
