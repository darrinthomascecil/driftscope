# Azure Front Door Standard: managed TLS (browser-facing) + WAF + DDoS + CDN, in front of the
# Istio ingress gateway. The origin is a public IP, locked to THIS Front Door (service tag +
# X-Azure-FDID header check on the gateway). Leg 2 (Front Door -> origin):
#   * custom domain -> HTTPS to origin.<domain>, a REAL cert-manager DNS-01 cert the gateway
#     presents and Front Door VALIDATES -- authenticated + encrypted, no CWE-295 finding.
#   * no domain (demo) -> HTTP to the cloudapp FQDN (DNS-01 can't cert a zone we don't own).
# Full rationale + the rejected alternatives: docs/design-edge-origin-cert.md.

# Static public IP the external Istio ingress gateway will use (BYO IP -> stable origin). The DNS
# label gives it a resolvable FQDN Front Door can dial as the demo-mode (HTTP) origin.
resource "azurerm_public_ip" "gateway" {
  name                = "${var.name_prefix}-${var.environment}-gw-pip"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  allocation_method   = "Static"
  sku                 = "Standard"
  domain_name_label   = var.gateway_dns_label # -> <label>.<region>.cloudapp.azure.com
}

# The AKS cluster identity needs to attach the BYO public IP to the gateway's load balancer.
resource "azurerm_role_assignment" "aks_gateway_pip" {
  scope                = azurerm_public_ip.gateway.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

resource "azurerm_cdn_frontdoor_profile" "this" {
  name                = "${var.name_prefix}-${var.environment}-fd"
  resource_group_name = azurerm_resource_group.this.name
  sku_name            = "Standard_AzureFrontDoor"
}

resource "azurerm_cdn_frontdoor_endpoint" "this" {
  name                     = "${var.name_prefix}-${var.environment}-ep"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id
}

resource "azurerm_cdn_frontdoor_origin_group" "gateway" {
  name                     = "gateway"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id

  load_balancing {
    sample_size                 = 4
    successful_samples_required = 2
  }

  health_probe {
    path                = "/healthz" # exempted from SSO in 0.4b so this returns 200
    protocol            = local.use_https_origin ? "Https" : "Http"
    interval_in_seconds = 100
    request_type        = "GET"
  }
}

resource "azurerm_cdn_frontdoor_origin" "gateway" {
  name                          = "gateway"
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.gateway.id
  enabled                       = true

  host_name          = local.origin_host # origin.<domain> (custom, DNS-01 cert) or cloudapp FQDN (demo, HTTP)
  origin_host_header = local.host        # HTTP Host for Istio routing (separate from the TLS/SNI name)
  http_port          = 80
  https_port         = 443
  priority           = 1
  weight             = 1000

  # Custom domain: gateway presents a real Let's Encrypt cert for origin.<domain> and Front Door
  # validates it. Demo (no domain): leg 2 is HTTP, so there is no cert to validate -- see
  # docs/design-edge-origin-cert.md for why the cloudapp FQDN can't get a DNS-01 cert.
  certificate_name_check_enabled = local.use_https_origin
}

resource "azurerm_cdn_frontdoor_custom_domain" "this" {
  count                    = local.use_custom_domain ? 1 : 0
  name                     = "driftscope"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id
  host_name                = local.host

  tls {
    certificate_type = "ManagedCertificate" # free, auto-renewed -- why no cert-manager for the public cert
  }
}

resource "azurerm_cdn_frontdoor_route" "this" {
  name                            = "default"
  cdn_frontdoor_endpoint_id       = azurerm_cdn_frontdoor_endpoint.this.id
  cdn_frontdoor_origin_group_id   = azurerm_cdn_frontdoor_origin_group.gateway.id
  cdn_frontdoor_origin_ids        = [azurerm_cdn_frontdoor_origin.gateway.id]
  cdn_frontdoor_custom_domain_ids = local.use_custom_domain ? [azurerm_cdn_frontdoor_custom_domain.this[0].id] : []

  supported_protocols    = ["Http", "Https"]
  patterns_to_match      = ["/*"]
  forwarding_protocol    = local.use_https_origin ? "HttpsOnly" : "HttpOnly" # leg 2: HTTPS with a real cert (custom) or HTTP (demo)
  https_redirect_enabled = true
  link_to_default_domain = local.use_custom_domain ? false : true # use the *.azurefd.net host when no custom domain
}

resource "azurerm_cdn_frontdoor_custom_domain_association" "this" {
  count                          = local.use_custom_domain ? 1 : 0
  cdn_frontdoor_custom_domain_id = azurerm_cdn_frontdoor_custom_domain.this[0].id
  cdn_frontdoor_route_ids        = [azurerm_cdn_frontdoor_route.this.id]
}

# WAF. Standard SKU = custom rules only (managed rulesets need Premium). A simple rate limit +
# Prevention mode to start; tighten as needed.
resource "azurerm_cdn_frontdoor_firewall_policy" "this" {
  name                = "${var.name_prefix}${var.environment}waf"
  resource_group_name = azurerm_resource_group.this.name
  sku_name            = azurerm_cdn_frontdoor_profile.this.sku_name
  enabled             = true
  mode                = "Prevention"

  custom_rule {
    name                           = "ratelimit"
    enabled                        = true
    priority                       = 1
    type                           = "RateLimitRule"
    action                         = "Block"
    rate_limit_duration_in_minutes = 1
    rate_limit_threshold           = 600

    match_condition {
      match_variable     = "RequestMethod"
      operator           = "Equal"
      negation_condition = false
      match_values       = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    }
  }
}

resource "azurerm_cdn_frontdoor_security_policy" "this" {
  name                     = "waf-assoc"
  cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.this.id

  security_policies {
    firewall {
      cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.this.id

      association {
        patterns_to_match = ["/*"]
        domain {
          # associate the custom domain if there is one, else the default endpoint
          cdn_frontdoor_domain_id = local.use_custom_domain ? azurerm_cdn_frontdoor_custom_domain.this[0].id : azurerm_cdn_frontdoor_endpoint.this.id
        }
      }
    }
  }
}
