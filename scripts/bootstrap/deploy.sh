#!/usr/bin/env bash
# Apply the app + the gateway-namespace objects (mesh hardening, SSO gate, edge TLS + origin
# lock) in the right order, then print the URL. Assumes cluster-init + secret already ran.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# 1. App namespace: deployment, oauth2-proxy, routing, STRICT mTLS, gateway-only allows, and the
#    ESO SecretStore + ExternalSecret. (Namespace is already labeled for injection by cluster-init.)
#
# The overlay is a REPLACE-* template (ADR 0001); render-overlay.sh binds the real values from
# Terraform outputs -- the SAME renderer CI and Argo CD use, so bootstrap/CI/GitOps are identical.
log "rendering + applying app overlay (render-overlay.sh, values from terraform outputs)"
HOST="$(tf_out host)" \
TENANT_ID="$(tf_out tenant_id)" \
ALLOWED_GROUP_ID="$(tf_out allowed_group_id)" \
APP_CLIENT_ID="$(tf_out app_client_id)" \
ESO_CLIENT_ID="$(tf_out eso_client_id)" \
KEYVAULT="$(tf_out key_vault_name)" \
IMAGE_REPO="$(tf_out acr_login_server)/driftscope" \
DRIFTSCOPE_ENV="$ENV" \
  "$REPO/deploy/gitops/render-overlay.sh" | kubectl apply -f -

# 1a. ESO syncs the oauth2-proxy Secret from Key Vault (replaces `make secret`). Wait for it so
#     oauth2-proxy has its credentials before rollout. If it doesn't sync, check the SecretStore:
#     kubectl describe externalsecret oauth2-proxy -n driftscope
log "waiting for External Secrets to sync oauth2-proxy from Key Vault"
kubectl wait --for=condition=Ready externalsecret/oauth2-proxy -n driftscope --timeout=120s \
  && kubectl -n driftscope rollout restart deploy/oauth2-proxy >/dev/null 2>&1 \
  || warn "ExternalSecret not Ready -- check ESO + the driftscope-eso identity (Key Vault Secrets User). Fallback: make secret"
# ^ the restart is a nudge: oauth2-proxy pods scheduled before the Secret existed sit in
#   CreateContainerConfigError and self-heal on the kubelet's retry; this just makes pickup prompt.

# 2. Mesh-wide hardening. The app overlay's ALLOW policies exist now, so applying the
#    default-deny here won't cut off the gateway/app/oauth2-proxy.
log "applying mesh hardening + access logs + SSO gate"
kubectl apply -f "$OVERLAY/mesh/hardening.yaml"
kubectl apply -f "$OVERLAY/mesh/access-logs.yaml"
kubectl apply -f "$OVERLAY/mesh/require-sso.authorizationpolicy.yaml"

# 3. Edge (aks-istio-ingress). The edge manifests are committed REPLACE-* templates (ADR 0001);
#    render-edge.sh binds the values from Terraform outputs -- Terraform no longer writes these
#    files. Custom domain: issue the origin cert (DNS-01) and wait; the origin lock applies in BOTH
#    modes (it's what stops the internet reaching the origin IP and bypassing Front Door's WAF).
RENDER_EDGE="$REPO/deploy/gitops/render-edge.sh"
if custom_domain; then
  log "issuing origin certificate (DNS-01, render-edge.sh)"
  DNS_ZONE="$(tf_out zone_name)" \
  RESOURCE_GROUP="$(tf_out resource_group)" \
  SUBSCRIPTION_ID="$(tf_out subscription_id)" \
  CERTMANAGER_CLIENT_ID="$(tf_out certmanager_client_id)" \
  ACME_EMAIL="$(tf_out acme_email)" \
    "$RENDER_EDGE" "$OVERLAY/edge/cluster-issuer.yaml" | kubectl apply -f -
  ORIGIN_HOST="$(tf_out origin_host)" \
    "$RENDER_EDGE" "$OVERLAY/edge/origin-certificate.yaml" | kubectl apply -f -
  kubectl wait --for=condition=Ready certificate/driftscope-origin -n aks-istio-ingress --timeout=300s \
    || warn "origin cert not Ready yet -- check: kubectl describe certificate driftscope-origin -n aks-istio-ingress"
fi
log "applying X-Azure-FDID origin lock (render-edge.sh, both modes)"
FRONT_DOOR_GUID="$(tf_out frontdoor_id)" \
  "$RENDER_EDGE" "$OVERLAY/edge/origin-lock.authorizationpolicy.yaml" | kubectl apply -f -

# 4. Make sure oauth2-proxy picked up the secret.
kubectl -n driftscope rollout status deployment/oauth2-proxy --timeout=120s \
  || warn "oauth2-proxy not ready -- check the secret step and: kubectl -n driftscope logs deploy/oauth2-proxy"

log "deploy: OK"
log "app URL: $(tf_out app_url)"
