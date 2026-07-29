# Runbook: the network path from the internet to the app, and how to debug each hop

Every request to `https://driftscope.justmakeitgo.net` traverses seven hops. This runbook walks
each one: **what it does**, **what breaks there**, **the tool you reach for**, and **how to read the
output**. It's ordered the way a request flows, which is also the order you bisect a problem.

```
[0] Client + DNS
      │  resolve driftscope.justmakeitgo.net -> Cloudflare edge IP
[1] Cloudflare edge            TLS termination · WAF · DDoS
      │  HTTPS
[2] Cloudflare Tunnel          cloudflared pods (outbound QUIC, no public origin)
      │  plain HTTP, in-cluster
[3] Istio internal gateway     Service aks-istio-ingressgateway-internal:80
      │
[4] Gateway policy             Host match · VirtualService · ext_authz (SSO) · AuthorizationPolicy
      ├─[4b] oauth2-proxy  ---- OIDC ----> Entra
      │  mTLS + workload authz
[5] App sidecar                STRICT mTLS · driftscope-allow-gateway-only
      │  localhost
[6] App container              driftscope (port 8080)
```

**Golden rule of bisection:** find the *last* hop that looks healthy and the *first* that doesn't;
the fault is on the edge between them. The tools below let you probe each edge independently.

---

## [0] Client + DNS

**Does:** the browser resolves the hostname to a Cloudflare edge IP (the record is proxied /
"orange-cloud", so you get Cloudflare's anycast IP, never the origin).

**Breaks as:** `NXDOMAIN`, `could not resolve host`, or — the one that bit us — a **stale negative DNS
cache** on the client after the record was first created, so the machine keeps answering NXDOMAIN
while the record is actually live.

**Tool → command → read:**
```sh
dig +short driftscope.justmakeitgo.net @1.1.1.1     # authoritative answer, bypasses local cache
dig +short driftscope.justmakeitgo.net              # what THIS machine resolves
```
If `@1.1.1.1` returns an IP but the local resolver returns nothing, it's a local cache problem, not a
DNS problem. Prove the rest of the stack works by forcing the IP:
```sh
curl -sI --resolve driftscope.justmakeitgo.net:443:<edge-ip> https://driftscope.justmakeitgo.net/
```
(Flush with `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder` on macOS.) The two IPs
returned (`104.21.x` / `172.67.x`) are Cloudflare's — if you ever see a non-Cloudflare IP, the record
was un-proxied and the origin is exposed.

---

## [1] Cloudflare edge — TLS, WAF, DDoS

**Does:** terminates TLS, runs WAF/DDoS, then hands the request to the tunnel. This is where the
public HTTPS actually ends.

**Breaks as:** a Cloudflare-generated error page. The status code tells you *which* hop is actually
sick — this is the single most useful signal in the whole path:
| Code | Means |
|---|---|
| `502 / 521` | origin (the tunnel) refused / down |
| `522` | connection timed out reaching the origin |
| `523` | origin unreachable (routing/DNS inside CF) |
| `524` | origin accepted but didn't respond in time (long request — relevant to fleet scans) |
| `1033` | Argo/tunnel error — no healthy connector |
| `530` | usually a tunnel/DNS config error |

**Tool → command → read:**
```sh
curl -sv --resolve driftscope.justmakeitgo.net:443:<edge-ip> https://driftscope.justmakeitgo.net/ 2>&1 \
  | grep -Ei 'server:|cf-ray|cf-cache|HTTP/|SSL connection|subject:|issuer:'
```
- `server: cloudflare` + a `cf-ray:` header confirm you reached the edge.
- Inspect the cert without a browser: `openssl s_client -connect <edge-ip>:443 -servername driftscope.justmakeitgo.net </dev/null | openssl x509 -noout -subject -issuer -dates`.
- A `5xx` **with** `cf-ray` = the edge is up and the problem is downstream (tunnel/origin). A failure
  with **no** `cf-ray` = you didn't reach Cloudflare (DNS/network/TLS, i.e. hop 0).
- Deeper: Cloudflare dashboard → the zone → **Analytics/Logs**, and **Security → Events** for WAF
  blocks. The API works too (we already hold a token): `GET /zones/<id>/analytics`.

---

## [2] Cloudflare Tunnel — cloudflared

**Does:** `cloudflared` pods dial *out* to Cloudflare over QUIC and register as connectors; the edge
forwards matched hostnames down that tunnel. There is no inbound origin.

**Breaks as:** `1033`/`502`/`523` at the edge — usually the connectors dropped, or the tunnel's
ingress config points at the wrong in-cluster service (this is exactly what a bad *retarget* looks
like).

**Tool → command → read:**
```sh
kubectl -n driftscope logs deploy/cloudflared --tail=50 | grep -Ei 'registered|connection|error|unregist'
#   healthy: "Registered tunnel connection ... connIndex=0..3"  (expect ~4)
kubectl -n driftscope get pods -l app=cloudflared      # both replicas Running

# what does the tunnel actually forward to? (the ingress rules live at Cloudflare, not in-cluster)
CF=$(az keyvault secret show --vault-name dscopeclddev01 -n cloudflare-api-token --query value -o tsv)
curl -s -H "Authorization: Bearer $CF" \
  https://api.cloudflare.com/client/v4/accounts/<acct>/cfd_tunnel/<tunnel>/configurations \
  | python3 -m json.tool          # the `service:` must be the Istio gateway, not the app
```
- The connectors on the edge side: Cloudflare dashboard → **Zero Trust → Networks → Tunnels** → the
  tunnel shows **HEALTHY / DEGRADED / DOWN** and per-connector status.
- `cloudflared` is distroless — no shell/curl inside it. To test what it can reach, use an ephemeral
  pod (hop 3), don't try to `exec` into cloudflared.

---

## [3] cloudflared → Istio internal ingress gateway

**Does:** cloudflared forwards plain HTTP to `aks-istio-ingressgateway-internal.aks-istio-ingress.svc:80`.

**Breaks as:** `523`/`502` (service has no endpoints, wrong name/port, or the gateway pods are down).

**Tool → command → read:**
```sh
kubectl -n aks-istio-ingress get svc,endpoints aks-istio-ingressgateway-internal
#   EXTERNAL-IP must be a PRIVATE 10.x (internal LB); ENDPOINTS non-empty
kubectl -n aks-istio-ingress get pods -l istio=aks-istio-ingressgateway-internal   # Running

# reproduce cloudflared's own hop with a throwaway pod (do NOT curl from your laptop — it's internal):
kubectl -n default run t --image=curlimages/curl:8.10.1 --restart=Never --command -- sleep 3600
kubectl -n default exec t -- curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'Host: driftscope.justmakeitgo.net' \
  http://aks-istio-ingressgateway-internal.aks-istio-ingress.svc.cluster.local:80/
#   302 = gateway alive AND SSO firing (see hop 4).  000/timeout = gateway down or wrong port.
```
The ephemeral `curlimages/curl` pod is the workhorse for every in-cluster hop, because the real
components (cloudflared, the gateway, the app) are all distroless.

---

## [4] Gateway policy — routing + SSO enforcement

**Does:** on the gateway's Envoy: match `Host`, apply the `VirtualService` (peel `/oauth2/*` to
oauth2-proxy, rest to the app), and run the **CUSTOM `AuthorizationPolicy`** → ext_authz call to
oauth2-proxy for every non-`/oauth2` path.

**Breaks as:** everything returns `302`-loops (ext_authz can't reach oauth2-proxy → fail-closed), or
a path 404s (VirtualService match wrong), or auth silently doesn't apply (policy selector wrong).

**Tool → command → read:** managed Istio may not ship `istioctl` locally — every check below has an
**istioctl-free** form via the sidecar's `pilot-agent`, which is what you'll actually use:
```sh
GW=$(kubectl -n aks-istio-ingress get pod -l istio=aks-istio-ingressgateway-internal -o name | head -1)

# routes the gateway actually has (istioctl-free):
kubectl -n aks-istio-ingress exec $GW -c istio-proxy -- pilot-agent request GET config_dump \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(json.dumps(d,indent=1))' | grep -A3 driftscope
#   istioctl form: istioctl proxy-config route $GW.aks-istio-ingress

# is ext_authz allowing/denying/erroring? (the smoking gun for SSO problems)
kubectl -n aks-istio-ingress exec $GW -c istio-proxy -- pilot-agent request GET stats \
  | grep -Ei 'ext_authz.*(ok|denied|error)'
#   ext_authz.error climbing = gateway can't reach oauth2-proxy (hop 4b), request fails closed.

# live access logs while you curl from the throwaway pod:
kubectl -n aks-istio-ingress logs $GW -c istio-proxy -f
kubectl analyze  # or: istioctl analyze -n driftscope   -- validates Gateway/VirtualService wiring
```
Confirm the objects exist and select correctly:
```sh
kubectl -n driftscope get gateway,virtualservice
kubectl -n aks-istio-ingress get authorizationpolicy driftscope-require-sso -o yaml   # action: CUSTOM, provider oauth2-proxy
```

---

## [4b] Gateway → oauth2-proxy → Entra

**Does:** oauth2-proxy answers the ext_authz check; on "no session" it 302s to Entra, on callback it
redeems the code and sets the session cookie.

**Breaks as:** the callback 500s (**`AADSTS7000215`** = wrong client secret — store the *value* not the
keyId), or the login loops (cookie/scheme), or ext_authz errors (istiod never loaded the provider).

**Tool → command → read:**
```sh
kubectl -n driftscope logs deploy/oauth2-proxy -c oauth2-proxy --tail=40 | grep -Ei 'error|callback|AADSTS|redeem|csrf'
kubectl -n driftscope get pods -l app=oauth2-proxy                 # 2/2 (app + sidecar)

# did istiod actually pick up the ext_authz provider?
kubectl -n aks-istio-system get configmap istio-shared-configmap-asm-1-29 -o jsonpath='{.data.mesh}'
#   after editing it you MUST: kubectl -n aks-istio-system rollout restart deploy/istiod-asm-1-29

# can oauth2-proxy reach Entra? (egress / NetworkPolicy / DNS)
kubectl -n driftscope exec deploy/oauth2-proxy -c oauth2-proxy -- \
  wget -qO- https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration | head -c 200
```
Read the OIDC redirect straight off the wire to verify client_id / redirect_uri / scope:
```sh
curl -sI --resolve driftscope.justmakeitgo.net:443:<edge-ip> https://driftscope.justmakeitgo.net/ \
  | grep -i location          # -> login.microsoftonline.com/...authorize?client_id=...&redirect_uri=https://.../oauth2/callback
```

---

## [5] Gateway → app: mTLS + workload authorization

**Does:** the gateway originates **mTLS** to the app sidecar (STRICT `PeerAuthentication`), and the
app's `driftscope-allow-gateway-only` policy admits **only** the gateway's SPIFFE identity.

**Breaks as:** `403` at the app (RBAC — the caller isn't the gateway principal, or the principal in the
policy is wrong after a gateway/namespace rename), or connection resets (mTLS handshake / cert issues).

**Tool → command → read:**
```sh
APP=$(kubectl -n driftscope get pod -l app.kubernetes.io/name=driftscope -o name | head -1)

# mTLS actually happening on inbound? (should be mutual_tls, never 'none')
kubectl -n driftscope exec $APP -c istio-proxy -- pilot-agent request GET stats/prometheus \
  | grep 'istio_requests_total' | grep -o 'connection_security_policy="[^"]*"' | sort -u

# who is the app allowed to accept? (confirms the ALLOW principal is exact)
kubectl -n driftscope exec $APP -c istio-proxy -- pilot-agent request GET config_dump \
  | grep -o 'cluster.local/ns/[^"]*'      # expect ...ns/aks-istio-ingress/sa/aks-istio-ingressgateway-internal

# turn on RBAC debug to SEE a 403 decision, then reproduce:
kubectl -n driftscope exec $APP -c istio-proxy -- pilot-agent request POST 'logging?rbac=debug'
kubectl -n driftscope logs $APP -c istio-proxy -f | grep -i 'rbac\|enforced denied'
```
Prove the door is *only* the gateway (the east-west check from the SSO-bypass fix): a plain in-mesh
pod hitting `driftscope.driftscope.svc:80` must get **`403`**, not `200`.

---

## [6] App container

**Does:** the actual driftscope server on `:8080` handles the request and streams the report over SSE.

**Breaks as:** app-level errors that have nothing to do with the mesh — e.g. `could not read issues`
(the pod has no GitHub token, a deliberate public-deploy tradeoff), or a scan timeout, or an SSE stall.

**Tool → command → read:**
```sh
kubectl -n driftscope logs $APP -c driftscope -f          # the app's own logs (note -c driftscope, not istio-proxy)
kubectl -n driftscope exec $APP -c driftscope -- wget -qO- http://localhost:8080/healthz   # app-local, bypasses the mesh
kubectl -n driftscope describe pod $APP | sed -n '/Events/,$p'   # OOMKilled, CrashLoop, probe failures
kubectl -n driftscope top pod -l app.kubernetes.io/name=driftscope   # memory pressure on fleet scans
```
Because this hop is reached over `localhost` from the sidecar, an app bug reproduces here with the
mesh entirely out of the picture — the fastest way to confirm "it's the app, not the plumbing."

---

## The toolbox (what you reach for, by layer)

| Layer | Tool | The one command |
|---|---|---|
| DNS | `dig` | `dig +short host @1.1.1.1` vs local |
| Edge / TLS | `curl -v`, `openssl s_client` | look for `cf-ray`, cert, `5xx` meaning |
| Tunnel | `cloudflared` logs + CF dashboard/API | `logs deploy/cloudflared \| grep Registered` |
| In-cluster reach | ephemeral `curlimages/curl` pod | `exec … curl -H Host: … <svc>` |
| Envoy (gw & app) | `pilot-agent request GET stats/config_dump` (istioctl-free) | ext_authz + rbac + mTLS stats |
| Mesh wiring | `istioctl analyze`, `kubectl get gateway,vs,ap` | validate selectors/routes |
| App | `kubectl logs -c driftscope`, `describe`, `top` | app-local `healthz` over localhost |

## Worked triage: a browser 502

1. `curl -sv …` → `502` **with** `cf-ray` ⇒ edge is fine, origin sick. (No `cf-ray` ⇒ back to DNS, hop 0.)
2. Cloudflare Tunnels page / `cloudflared` logs ⇒ connectors HEALTHY? If DOWN ⇒ hop 2 (restart/creds).
3. If tunnel healthy, check its ingress `service:` ⇒ pointing at the Istio gateway? If at the old app
   service, mTLS STRICT now rejects it ⇒ 502. Re-target. (This is a classic post-mesh regression.)
4. Ephemeral pod → gateway `:80` ⇒ `302`? Gateway fine, problem was the tunnel target. `000`? ⇒ hop 3/4.
5. Gateway Envoy `ext_authz.error` climbing ⇒ oauth2-proxy unreachable ⇒ hop 4b.
6. App `403` in gateway logs ⇒ workload-authz principal mismatch ⇒ hop 5.
