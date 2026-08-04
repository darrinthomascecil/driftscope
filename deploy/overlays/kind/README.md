# Local kind cloud-parity overlay (Istio mesh + Entra SSO)

Brings a **local kind cluster** to the same security posture the AKS clusters run: an Istio
service mesh with STRICT mTLS, a mesh-wide deny-all, the app reachable **only** through the
ingress gateway, and real **Entra (Azure AD) SSO** in front — so the app is reachable in a
browser exactly as it is in the cloud.

This is a **development convenience, not a deployment requirement**. A normal deploy uses
`deploy/base` + the cloud overlays; nothing here is needed to run driftscope, and pulling the
repo does not imply you have kind. It exists so the mesh/SSO path can be exercised locally.

## Why a separate overlay (not the cloud one)

`deploy/overlays/cloud` is bound to the **AKS managed Istio addon** — selectors like
`istio: aks-istio-ingressgateway-external`, namespaces `aks-istio-ingress` / `aks-istio-system`.
Kind runs **upstream Istio**, so this overlay mirrors the cloud's *intent* with upstream naming:

| cloud (AKS addon) | this overlay (upstream) |
|---|---|
| `aks-istio-system` (root config ns) | `istio-system` |
| `aks-istio-ingress` (gateway ns) | `istio-system` |
| `istio: aks-istio-ingressgateway-external` | `istio: ingressgateway` |
| Front Door → gateway | `kubectl port-forward` → gateway (no FD locally) |

## Files
| file | what |
|---|---|
| `mesh.yaml` | Service + STRICT `PeerAuthentication` + deny-all + gateway/app allows + `Gateway` + `VirtualService` |
| `istiod-values.yaml` | Helm values that register oauth2-proxy as the ext_authz `extensionProvider` in istiod's meshConfig |
| `sso.yaml` | oauth2-proxy (Entra OIDC) + `/oauth2` route + `require-sso` CUSTOM authz. `REPLACE-*` per ADR 0001 |

## Prerequisites
- a kind cluster with the app deployed (`app: driftscope` in namespace `driftscope`) — see the
  `deploy-driftscope-kind` skill
- `helm` + the Istio chart repo (`istio-release.storage.googleapis.com/charts`), charts pinned to 1.29.x
- `az` logged in to the tenant that owns the SSO Entra app

## Setup

```sh
CTX=kind-<cluster>            # e.g. kind-driftscope-wi
PORT=8443                     # local gateway port-forward

# 1. Istio via Helm + the ext_authz provider (istiod-values.yaml)
helm repo add istio https://istio-release.storage.googleapis.com/charts && helm repo update istio
helm --kube-context $CTX upgrade --install istio-base istio/base -n istio-system --create-namespace --version 1.29.5
helm --kube-context $CTX upgrade --install istiod istio/istiod -n istio-system --version 1.29.5 \
  -f deploy/overlays/kind/istiod-values.yaml --wait
helm --kube-context $CTX upgrade --install istio-ingressgateway istio/gateway -n istio-system --version 1.29.5 \
  --set serviceAccount.name=istio-ingressgateway-service-account --wait   # SA must match the authz-policy principal

# 2. mesh: inject the app namespace, then apply + re-roll for the sidecar
kubectl --context $CTX label ns driftscope istio-injection=enabled --overwrite
kubectl --context $CTX apply -f deploy/overlays/kind/mesh.yaml
kubectl --context $CTX -n driftscope rollout restart deploy/driftscope

# 3. Entra app for the LOCAL redirect (dedicated, so cloud SSO apps stay untouched)
REDIRECT="http://localhost:$PORT/oauth2/callback"
CLIENT_ID=$(az ad app create --display-name driftscope-kind-sso \
  --web-redirect-uris "$REDIRECT" --enable-id-token-issuance true --query appId -o tsv)
az ad app update --id "$CLIENT_ID" --set groupMembershipClaims=SecurityGroup
az ad sp create --id "$CLIENT_ID"
SECRET=$(az ad app credential reset --id "$CLIENT_ID" --append --query password -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
GROUP_ID=$(az ad group list --filter "displayName eq 'driftscope'" --query '[0].id' -o tsv)

# 4. render placeholders + create the secret + apply
kubectl --context $CTX -n driftscope create secret generic oauth2-proxy-creds \
  --from-literal=client-secret="$SECRET" \
  --from-literal=cookie-secret="$(openssl rand -base64 24)" \
  --dry-run=client -o yaml | kubectl --context $CTX apply -f -
sed -e "s/REPLACE-TENANT-ID/$TENANT_ID/g" -e "s/REPLACE-CLIENT-ID/$CLIENT_ID/g" \
    -e "s/REPLACE-ALLOWED-GROUP-ID/$GROUP_ID/g" deploy/overlays/kind/sso.yaml \
  | kubectl --context $CTX apply -f -

# 5. restart the gateway to pick up the ext_authz cluster, then port-forward
kubectl --context $CTX -n istio-system rollout restart deploy/istio-ingressgateway
kubectl --context $CTX -n istio-system port-forward svc/istio-ingressgateway $PORT:80 &
```

## Verify
```sh
# through the mesh gateway: authenticated path
curl -sD - -o /dev/null http://localhost:8443/ui | grep -i location   # -> 302 login.microsoftonline.com
# the mesh: a direct in-cluster hit is denied
kubectl -n driftscope run p --rm -i --image=curlimages/curl -- \
  curl -so /dev/null -w '%{http_code}\n' http://driftscope.driftscope.svc/healthz  # -> 403
```
Browser: `http://localhost:8443/ui` → Entra login → app.

## Notes
- The **client secret is never committed** — it lives in the `oauth2-proxy-creds` Secret, created
  from `az` output above. Only the (non-secret) tenant / client / group IDs are bound into `sso.yaml`,
  and only at apply time (`REPLACE-*` in git).
- **Front Door has no local equivalent** — the `port-forward` is the local stand-in for the edge.
- Dashboards: apply `deploy/monitoring` (Prometheus + Grafana) the same way; on kind, substitute a
  local `grafana-admin` Secret for the ESO/Key-Vault one. Once mesh traffic flows, the "Mesh Security"
  dashboard populates from `istio_requests_total`.
