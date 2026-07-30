#!/usr/bin/env bash
# Apply the app + the gateway-namespace objects (mesh hardening, SSO gate, edge TLS + origin
# lock) in the right order, then print the URL. Assumes cluster-init + secret already ran.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# 1. App namespace: deployment, oauth2-proxy, routing, STRICT mTLS, gateway-only allows, and the
#    ESO SecretStore + ExternalSecret. (Namespace is already labeled for injection by cluster-init.)
log "applying app overlay (kustomize)"
kubectl apply -k "$OVERLAY"

# 1a. ESO syncs the oauth2-proxy Secret from Key Vault (replaces `make secret`). Wait for it so
#     oauth2-proxy has its credentials before rollout. If it doesn't sync, check the SecretStore:
#     kubectl describe externalsecret oauth2-proxy -n driftscope
log "waiting for External Secrets to sync oauth2-proxy from Key Vault"
kubectl wait --for=condition=Ready externalsecret/oauth2-proxy -n driftscope --timeout=120s \
  || warn "ExternalSecret not Ready -- check ESO + the driftscope-eso identity (Key Vault Secrets User). Fallback: make secret"

# 2. Mesh-wide hardening. The app overlay's ALLOW policies exist now, so applying the
#    default-deny here won't cut off the gateway/app/oauth2-proxy.
log "applying mesh hardening + access logs + SSO gate"
kubectl apply -f "$OVERLAY/mesh/hardening.yaml"
kubectl apply -f "$OVERLAY/mesh/access-logs.yaml"
kubectl apply -f "$OVERLAY/mesh/require-sso.authorizationpolicy.yaml"

# 3. Edge. Custom domain: issue the origin cert and wait for it; then the origin lock (both modes).
if custom_domain; then
  log "issuing origin certificate (DNS-01)"
  kubectl apply -f "$OVERLAY/edge/cluster-issuer.yaml"
  kubectl apply -f "$OVERLAY/edge/origin-certificate.yaml"
  kubectl wait --for=condition=Ready certificate/driftscope-origin -n aks-istio-ingress --timeout=300s \
    || warn "origin cert not Ready yet -- check: kubectl describe certificate driftscope-origin -n aks-istio-ingress"
fi
log "applying X-Azure-FDID origin lock"
kubectl apply -f "$OVERLAY/edge/origin-lock.authorizationpolicy.yaml"

# 4. Make sure oauth2-proxy picked up the secret.
kubectl -n driftscope rollout status deployment/oauth2-proxy --timeout=120s \
  || warn "oauth2-proxy not ready -- check the secret step and: kubectl -n driftscope logs deploy/oauth2-proxy"

log "deploy: OK"
log "app URL: $(tf_out app_url)"
