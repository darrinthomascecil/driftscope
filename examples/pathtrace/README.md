# pathtrace — a self-diagnostic of the edge→app network path

A tiny stdlib-only app that renders, for the very request you sent it, each hop from the
internet to the pod — with the **live evidence** that hop happened and the **command to
debug it** if it hadn't. It's the runnable companion to
[`docs/runbook-edge-to-app.md`](../../docs/runbook-edge-to-app.md).

Deployed behind the same edge as driftscope, at `https://driftscope.justmakeitgo.net/trace`
(so it travels the full path it describes: Cloudflare → tunnel → Istio gateway → SSO → mTLS
→ app). **Requires an Entra login** — the SSO gate applies to `/trace` too.

## How it works

Every hop leaves a fingerprint in the request headers. pathtrace reads them off the request
it just received and turns each into a verdict:

| Hop | Fingerprint it checks |
|---|---|
| 1 Cloudflare edge | `cf-ray`, `cf-connecting-ip`, `cf-visitor` |
| 2 Tunnel | `x-forwarded-for`, `x-forwarded-proto` |
| 3 Istio gateway | `x-request-id`, `x-envoy-external-address` (Envoy-stamped) |
| 4 SSO | `x-auth-request-email`, `authorization` (injected by oauth2-proxy on allow) |
| 5 mTLS + workload authz | `x-forwarded-client-cert` — the caller's SPIFFE URI (must be the gateway SA) |
| 6 App | pod name / namespace (env) |

Because hops 4 and 5 rely on headers only the gateway/oauth2-proxy can produce, a green
verdict is genuine proof — not a claim. `GET /trace?format=json` returns the raw evidence.

## Build & deploy

```sh
az acr login -n dscopeacr18860
docker buildx build --platform linux/amd64 -t dscopeacr18860.azurecr.io/pathtrace:v1 --push .

kubectl apply -f k8s.yaml                       # Deployment + Service + gateway-only authz (driftscope ns)
# add the /trace route into the live VirtualService:
kubectl -n driftscope apply -f ../../deploy/overlays/cloud/istio-routing.yaml
```

`k8s.yaml` gives pathtrace its own `pathtrace-allow-gateway-only` AuthorizationPolicy — the
same east-west lockdown proven for driftscope — so the demo app isn't itself an SSO bypass.

## Verify

```sh
# unauth in-cluster -> gateway: SSO gates /trace (302)
kubectl -n default run t --image=curlimages/curl --restart=Never -- sleep 300
kubectl -n default exec t -- curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: driftscope.justmakeitgo.net' \
  http://aks-istio-ingressgateway-internal.aks-istio-ingress.svc:80/trace   # 302

# then open https://driftscope.justmakeitgo.net/trace in a browser and sign in -> all hops green
```
