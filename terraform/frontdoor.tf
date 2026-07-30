# Azure Front Door Standard: managed TLS (browser-facing) + WAF + DDoS + CDN, in front of the
# Istio ingress gateway. The origin is a public IP locked to THIS Front Door (service tag +
# X-Azure-FDID header check, wired on the gateway in 0.4b). Front Door -> origin runs HTTPS to a
# self-signed cert on the gateway (certificate_name_check disabled) so that hop is encrypted.

# Static public IP the external Istio ingress gateway will use (BYO IP -> stable origin).
resource "azurerm_public_ip" "gateway" {
  name                = "${var.name_prefix}-gw-pip"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

# The AKS cluster identity needs to attach the BYO public IP to the gateway's load balancer.
resource "azurerm_role_assignment" "aks_gateway_pip" {
  scope                = azurerm_public_ip.gateway.id
  role_definition_name = "Network Contributor"
  principal_id         = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

resource "azurerm_cdn_frontdoor_profile" "this" {
  name                = "${var.name_prefix}-fd"
  resource_group_name = azurerm_resource_group.this.name
  sku_name            = "Standard_AzureFrontDoor"
}

resource "azurerm_cdn_frontdoor_endpoint" "this" {
  name                     = "${var.name_prefix}-ep"
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
    protocol            = "Https"
    interval_in_seconds = 100
    request_type        = "GET"
  }
}

resource "azurerm_cdn_frontdoor_origin" "gateway" {
  name                          = "gateway"
  cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.gateway.id
  enabled                       = true

  host_name          = azurerm_public_ip.gateway.ip_address
  origin_host_header = local.host # so the Istio VirtualService matches on Host
  http_port          = 80
  https_port         = 443
  priority           = 1
  weight             = 1000

  certificate_name_check_enabled = false # origin serves a self-signed cert (encrypt the hop, don't validate name)
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
  forwarding_protocol    = "HttpsOnly" # Front Door -> origin encrypted
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
  name                = "${var.name_prefix}waf"
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
