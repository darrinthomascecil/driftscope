locals {
  tenant_id         = coalesce(var.tenant_id, data.azurerm_client_config.current.tenant_id)
  use_custom_domain = var.base_domain != null && var.base_domain != ""
  subdomain         = "driftscope"
  # With a custom domain: driftscope.<domain>. Without: Front Door's default *.azurefd.net host.
  host = local.use_custom_domain ? "${local.subdomain}.${var.base_domain}" : azurerm_cdn_frontdoor_endpoint.this.host_name

  # The name Front Door DIALS to reach the origin (SNI + DNS target), distinct from the public
  # `host` above. Custom domain: origin.<domain> (A -> gateway IP) with a cert-manager DNS-01
  # cert. No domain: the gateway's cloudapp FQDN, reached over HTTP (no cert issuable there).
  origin_host      = local.use_custom_domain ? "origin.${var.base_domain}" : azurerm_public_ip.gateway.fqdn
  use_https_origin = local.use_custom_domain # Front Door -> origin is HTTPS only when we can issue a real cert
}

resource "azurerm_resource_group" "this" {
  name     = "${var.name_prefix}-cloud"
  location = var.location
}

resource "azurerm_container_registry" "this" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic"
  admin_enabled       = false
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
  name                = "${var.name_prefix}-aks"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  dns_prefix          = var.name_prefix
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

# Let the cluster's kubelet identity pull images from the ACR (no admin creds).
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                            = azurerm_container_registry.this.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
  skip_service_principal_aad_check = true
}
