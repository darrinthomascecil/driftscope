#!/usr/bin/env bash
# Local kind bootstrap: an EXISTING kind cluster -> running driftscope mesh, in one command.
# Installs the cloud-parity prerequisites (UPSTREAM Istio via Helm + cert-manager local-CA TLS +
# MetalLB) and applies the kustomize overlay with your values bound in. Idempotent (safe to re-run).
# This is the LOCAL flow -- it does NOT touch Azure/terraform (that's `make up`).
#
# OUT OF SCOPE (prerequisites this script does NOT create):
#   - the kind cluster + kubeconfig context (default kind-driftscope-wi)
#   - the driftscope app Deployment/image  -> use the `deploy-driftscope-kind` skill
#   - the Entra SSO app (tenant/client/group ids + client secret) -> put its values in kind.env
#   - host DNS: mapping $HOST -> the gateway LB IP (dnsmasq/hosts) is host-side, done after this runs
#
# Config: cp deploy/overlays/kind/kind.env.example deploy/overlays/kind/kind.env  (gitignored), fill in.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."     # repo root

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

KIND_ENV="${KIND_ENV:-deploy/overlays/kind/kind.env}"
[ -f "$KIND_ENV" ] || die "config not found: $KIND_ENV
  -> cp deploy/overlays/kind/kind.env.example $KIND_ENV  and fill in your values"
set -a
# shellcheck source=/dev/null
source "$KIND_ENV"
set +a

: "${CTX:=kind-driftscope-wi}"
: "${ISTIO_VERSION:=1.29.5}"
: "${CERT_MANAGER_VERSION:=v1.16.2}"
: "${METALLB_VERSION:=v0.14.9}"
: "${METALLB_POOL:=172.18.255.200-172.18.255.250}"   # inside the kind Docker subnet; override if yours differs
K="kubectl --context $CTX"
H="helm --kube-context $CTX"

for t in kubectl helm openssl curl sed; do require_cmd "$t"; done
for v in HOST TENANT_ID CLIENT_ID ALLOWED_GROUP_ID OAUTH2_CLIENT_SECRET; do
  [ -n "${!v:-}" ] || die "missing '$v' in $KIND_ENV"
done
$K cluster-info >/dev/null 2>&1 || die "kube context '$CTX' not reachable -- is the kind cluster up?"

log "1/6  Istio via Helm ($ISTIO_VERSION): base + istiod (ext_authz provider) + ingress gateway"
$H repo add istio https://istio-release.storage.googleapis.com/charts >/dev/null 2>&1 || true
$H repo update istio >/dev/null
$H upgrade --install istio-base istio/base -n istio-system --create-namespace --version "$ISTIO_VERSION" --wait
$H upgrade --install istiod istio/istiod -n istio-system --version "$ISTIO_VERSION" \
   -f deploy/overlays/kind/istiod-values.yaml --wait
# SA name must match the principal the mesh authz policies allow
# (cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account), else ext_authz fails closed (403).
$H upgrade --install istio-ingressgateway istio/gateway -n istio-system --version "$ISTIO_VERSION" \
   --set serviceAccount.name=istio-ingressgateway-service-account --wait
$K get ns driftscope >/dev/null 2>&1 || $K create ns driftscope
$K label ns driftscope istio-injection=enabled --overwrite >/dev/null

log "2/6  cert-manager ($CERT_MANAGER_VERSION): local self-signed CA -> gateway cert for $HOST"
$H repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
$H repo update jetstack >/dev/null
$H upgrade --install cert-manager jetstack/cert-manager -n cert-manager --create-namespace \
   --version "$CERT_MANAGER_VERSION" --set crds.enabled=true --wait
$K apply -f deploy/overlays/kind/edge/cluster-issuer.yaml
$K wait --for=condition=Ready certificate/driftscope-local-ca -n cert-manager --timeout=120s
$K wait --for=condition=Ready clusterissuer/driftscope-local-ca --timeout=60s
sed "s/REPLACE-HOST/$HOST/g" deploy/overlays/kind/edge/gateway-certificate.yaml | $K apply -f -
$K wait --for=condition=Ready certificate/driftscope-tls -n istio-system --timeout=120s

log "3/6  MetalLB ($METALLB_VERSION) + address pool ($METALLB_POOL)"
$K apply -f "https://raw.githubusercontent.com/metallb/metallb/$METALLB_VERSION/config/manifests/metallb-native.yaml"
$K -n metallb-system rollout status deploy/controller --timeout=180s
POOL_YAML="$(sed "s#172.18.255.200-172.18.255.250#$METALLB_POOL#" deploy/overlays/kind/metallb.yaml)"
for i in 1 2 3 4 5; do
  printf '%s\n' "$POOL_YAML" | $K apply -f - && break || { warn "metallb webhook not ready, retry $i/5"; sleep 5; }
done

log "4/6  oauth2-proxy creds secret (values not printed)"
$K -n driftscope create secret generic oauth2-proxy-creds \
  --from-literal=client-secret="$OAUTH2_CLIENT_SECRET" \
  --from-literal=cookie-secret="${OAUTH2_COOKIE_SECRET:-$(openssl rand -hex 16)}" \
  --dry-run=client -o yaml | $K apply -f -

log "5/6  render params + apply the overlay (kustomize)"
kubectl kustomize deploy/overlays/kind \
  | sed -e "s/REPLACE-HOST/$HOST/g" -e "s/REPLACE-TENANT-ID/$TENANT_ID/g" \
        -e "s/REPLACE-CLIENT-ID/$CLIENT_ID/g" -e "s/REPLACE-ALLOWED-GROUP-ID/$ALLOWED_GROUP_ID/g" \
  | $K apply -f -
$K -n driftscope rollout status deploy/oauth2-proxy --timeout=120s

log "6/6  verify"
LB="$($K -n istio-system get svc istio-ingressgateway -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
[ -n "$LB" ] || die "ingress gateway has no LoadBalancer IP -- MetalLB pool may not match your Docker subnet"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -k --resolve "$HOST:443:$LB" "https://$HOST/ui" || echo 000)"
if [ "$CODE" = "302" ]; then
  log "security invariant OK: unauthenticated https://$HOST/ui -> 302 (gated by SSO)"
else
  warn "unauthenticated https://$HOST/ui -> $CODE (expected 302). Check the Entra app + oauth2-proxy-creds."
fi
log "gateway LB IP: $LB   ->   map '$HOST' to it (dnsmasq/hosts, host-side) then browse https://$HOST/ui"
log "kind-up: done"
