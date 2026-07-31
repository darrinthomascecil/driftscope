# Generate the edge (aks-istio-ingress) manifests from Terraform's values, the same way
# params.tf generates the app params. `terraform apply` regenerates them; with matching tfvars
# they reproduce the committed files. These live in aks-istio-ingress (not the app namespace),
# so they're applied directly, outside the kustomization -- see edge/README.md.

# DNS-01 issuer for the origin cert. Real content only in custom-domain mode (a no-op comment
# otherwise). client_id is read safely whether or not the identity exists (demo mode).
resource "local_file" "edge_cluster_issuer" {
  filename        = "${path.module}/../deploy/overlays/cloud/edge/cluster-issuer.yaml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/edge-cluster-issuer.yaml.tftpl", {
    enabled         = local.use_custom_domain
    zone_name       = local.use_custom_domain ? local.acme_zone : ""
    cname_follow    = local.cname_follow
    resource_group  = azurerm_resource_group.this.name
    subscription_id = var.subscription_id
    client_id       = try(azurerm_user_assigned_identity.certmanager[0].client_id, "")
    email           = var.acme_email
  })
}

# The origin Certificate (cert for origin.<domain> -> secret in aks-istio-ingress).
resource "local_file" "edge_origin_certificate" {
  filename        = "${path.module}/../deploy/overlays/cloud/edge/origin-certificate.yaml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/edge-origin-certificate.yaml.tftpl", {
    enabled     = local.use_custom_domain
    origin_host = local.origin_host
  })
}

# The X-Azure-FDID origin lock (applies in both modes).
resource "local_file" "edge_origin_lock" {
  filename        = "${path.module}/../deploy/overlays/cloud/edge/origin-lock.authorizationpolicy.yaml"
  file_permission = "0644"
  content = templatefile("${path.module}/templates/edge-origin-lock.yaml.tftpl", {
    frontdoor_id = azurerm_cdn_frontdoor_profile.this.resource_guid
  })
}
