# cert-manager's Azure identity for the ORIGIN certificate (custom-domain mode only).
#
# cert-manager proves control of <base_domain> to Let's Encrypt by writing _acme-challenge TXT
# records into the Azure DNS zone (DNS-01). It authenticates to Azure with WORKLOAD IDENTITY:
# a user-assigned managed identity federated to the cert-manager Kubernetes service account, so
# there is NO client secret anywhere. The identity holds DNS Zone Contributor on the zone only.
#
# None of this exists in demo mode -- without a domain there is no zone, no DNS-01, no origin cert
# (leg 2 is HTTP). See docs/design-edge-origin-cert.md.

resource "azurerm_user_assigned_identity" "certmanager" {
  count               = local.use_custom_domain ? 1 : 0
  name                = "${var.name_prefix}-${var.environment}-certmanager"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
}

# Trust the cluster's OIDC issuer to mint tokens for the cert-manager service account. The
# subject MUST match the SA the cert-manager pods run as (cert-manager/cert-manager by default);
# the helm install must set that SA + the workload-identity pod label (see edge/README.md).
resource "azurerm_federated_identity_credential" "certmanager" {
  count     = local.use_custom_domain ? 1 : 0
  name      = "certmanager"
  parent_id = azurerm_user_assigned_identity.certmanager[0].id
  audience  = ["api://AzureADTokenExchange"]
  issuer    = azurerm_kubernetes_cluster.this.oidc_issuer_url
  subject   = "system:serviceaccount:cert-manager:cert-manager"
}

# Write the DNS-01 challenge TXT records in the SHARED zone. Azure's DNS Zone Contributor is
# zone-scoped (no per-record-prefix grant), so each env's cert-manager can write anywhere in the
# shared zone -- an accepted widening under the shared-zone decision, bounded by the zone holding
# only driftscope records and the identity being per-env (federated to this cluster's issuer).
resource "azurerm_role_assignment" "certmanager_dns" {
  count                = local.use_custom_domain ? 1 : 0
  scope                = data.azurerm_dns_zone.shared[0].id
  role_definition_name = "DNS Zone Contributor"
  principal_id         = azurerm_user_assigned_identity.certmanager[0].principal_id
}
