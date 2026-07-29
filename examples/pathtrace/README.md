# pathtrace — a self-diagnostic of the edge→app network path

A tiny stdlib-only app that renders, for the very request you sent it, each hop from the
internet to the pod — with the **live evidence** that hop happened and the **command to
debug it** if it hadn't. It's the runnable companion to
[`docs/runbook-edge-to-app.md`](../../docs/runbook-edge-to-app.md).

Deployed behind the same edge as driftscope, at `https://driftscope.justmakeitgo.net/trace`
(so it travels the full path it describes: Cloudflare → tunnel → Istio gateway → SSO → mTLS
→ app). **Requires an Entra login** — the SSO gate applies to `/trace` too.

## How it works

Each hop is a **role**, not a product. For every role pathtrace tries an ordered list of
**vendor detectors**, then falls back to **vendor-neutral signals** — and reports *which*
one matched, so the page self-identifies the stack instead of assuming one. This is a
signature registry (like Wappalyzer/`whatweb`): adding an environment means adding a row,
not changing logic.

| Role | Vendor detectors (first match wins) | Generic fallback |
|---|---|---|
| Edge / CDN | Cloudflare `cf-ray` · CloudFront `x-amz-cf-id` · Front Door `x-azure-ref` · Fastly `x-served-by` · Akamai `akamai-grn` | `Via`, `X-Cache` |
| Forwarding chain | — (counts the chain) | `Forwarded` (RFC 7239), `X-Forwarded-For` depth |
| Ingress / gateway | Istio/Envoy `x-envoy-*` | `x-request-id`, `traceparent`, `x-b3-traceid` |
| Auth (IAP/SSO) | oauth2-proxy · Cloudflare Access · Google IAP · ALB OIDC · Azure Easy Auth | `Authorization` |
| Service identity / mTLS | Istio `x-forwarded-client-cert` (SPIFFE) · Linkerd `l5d-client-id` | — |
| App | this pod | — |

Each hop reports one of three states:
- **detected** (green) — a specific vendor signal matched; the `via` chip names it.
- **generic** (blue) — only a vendor-neutral signal was present; "some proxy, unidentified".
- **not seen** (gray, dashed) — no fingerprint. Deliberately *not* "failed": a transparent
  L4 balancer or NAT leaves no header, so absence of evidence isn't evidence of a broken hop.

Because the auth and mTLS hops rely on headers only the proxy/gateway can produce, a green
verdict there is genuine proof — not a claim. `GET /trace?format=json` returns the raw
evidence and the reconstructed path. The header-based view is complete only for hops that
stamp a header; for full ground truth across any topology, pair it with distributed tracing
(W3C `traceparent` / OpenTelemetry) and client-side vantage (`traceroute`, `dig +trace`).

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
