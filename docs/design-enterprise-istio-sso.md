# Enterprise edge: managed Istio + Entra SSO on AKS

**Goal (verbatim intent):** *"set this up at the cluster level with Istio… only for enterprise-grade
security with the SSO piece. I want to be as professional and secure as possible."*

This documents the stack that now fronts driftscope at `https://driftscope.justmakeitgo.net`, one
decision at a time. Each step states **what** it does, **why** it's there, and **the runner-up I
rejected and why** — so the trade-offs are auditable, not just the result.

---

## What is running

```
Browser
  │  HTTPS (TLS terminated at Cloudflare edge, WAF, DDoS)
  ▼
Cloudflare edge ── named tunnel (outbound-only, no public origin) ──►  cloudflared pods
                                                                        (in-cluster, OUT of mesh)
  │  plain HTTP inside the cluster
  ▼
Istio internal ingress gateway  (aks-istio-ingressgateway-internal, internal LB only)
  │
  ├─ AuthorizationPolicy action: CUSTOM  ──► ext_authz check ──► oauth2-proxy ──► Entra ID (OIDC)
  │        (every path except /oauth2/*)                          (browser login, session cookie)
  │
  ▼  mTLS (STRICT) — gateway originates, app sidecar requires
driftscope app  (Envoy sidecar injected)
```

Live state at time of writing:

| Component | State |
|---|---|
| AKS mesh addon | `mode: Istio`, revision **`asm-1-29`** (managed) |
| Internal ingress gateway | `enabled, mode: Internal` — internal LB `10.224.0.4`, no public IP |
| Namespace injection | `driftscope` labelled `istio.io/rev=asm-1-29` |
| App | `driftscope` **2/2** (app + sidecar) |
| Tunnel | `cloudflared` **1/1** (deliberately no sidecar) |
| Auth | `oauth2-proxy` **2/2**, registered as ext_authz provider `oauth2-proxy` |
| mTLS | `PeerAuthentication default` = **STRICT** in `driftscope` |
| Enforcement | `AuthorizationPolicy driftscope-require-sso` = **CUSTOM**, on the gateway |
| Entra app | `2fe61316-6a56-4808-968c-0785ffa97aac`, single-tenant, callback pinned to `https://driftscope.justmakeitgo.net/oauth2/callback` |

Verified: an unauthenticated request to any non-`/oauth2` path returns **302 → login.microsoftonline.com**
with the correct `client_id`, `redirect_uri` (https), `scope=openid profile email`, and the original
path carried in `state`.

---

## Decisions

### 1. Managed Istio (AKS mesh addon) — not self-managed Istio, not Linkerd
**Why:** the addon (`az aks mesh enable`) gives an Azure-supported, auto-upgraded istiod with the CVE
patching and revision lifecycle handled by the platform. For a small team that wants to look and
operate like an enterprise, "the control plane is someone else's on-call" is the whole point.
**Runner-up — self-managed upstream Istio (istioctl/Helm):** more knobs (any mesh-config field, any
revision the day it ships). Rejected because you inherit istiod upgrades, cert-rotation, and CVE
response yourself; that is exactly the operational tax we were trying not to pay. **Linkerd** was the
other candidate — lighter and simpler mTLS — but it has no first-class AKS-managed offering and no
`ext_authz` story as clean as Envoy's, which decisions #6/#7 depend on.

### 2. Internal ingress gateway — not external
**Why:** the gateway's load balancer is **internal** (VNet IP `10.224.0.4`, no public IP). The only way
in is the Cloudflare tunnel, which dials *out*. There is no public origin to port-scan, and IP-based
attacks have nothing to hit.
**Runner-up — external gateway with a public IP + NSG allowlisting Cloudflare ranges:** simpler DNS,
one fewer moving part. Rejected because it re-introduces a public origin; NSG allowlists of Cloudflare
IP ranges drift and are a classic origin-bypass hole. Outbound-only tunnel is strictly the smaller
attack surface.

### 3. Cloudflare Tunnel in front of the gateway — not Azure Front Door / public LB
**Why:** keeps TLS termination, WAF, and DDoS at Cloudflare (already the domain's authority), and
pairs with the internal gateway (#2) for a zero-public-origin design. It's also portable.
**Runner-up — Azure Front Door / Application Gateway:** tighter Azure integration and a WAF that lives
next to the workload. Rejected on cost and lock-in for a single small app, and because it still fronts
a public origin. This continues the edge decision already recorded in `design-secure-aks-multienv.md`.

### 4. STRICT mTLS in the app namespace — not PERMISSIVE
**Why:** `PeerAuthentication: STRICT` means the app sidecar refuses any non-mTLS connection. Even an
attacker who lands a pod in the cluster cannot talk plaintext to the app; they'd need a mesh identity.
This is the "assume breach" posture.
**Runner-up — PERMISSIVE (accept both mTLS and plaintext):** zero risk of accidentally locking
something out during rollout. Rejected because "as secure as possible" means no plaintext path to the
workload, full stop. The one consequence we handled deliberately: cloudflared can no longer hit the app
directly, which forces all traffic through the gateway (see #5) — a feature, not a bug.

### 5. cloudflared kept OUT of the mesh — app kept IN
**Why:** the app gets a sidecar (mTLS, policy enforcement, telemetry). cloudflared is annotated
`sidecar.istio.io/inject: "false"` because its tunnel to Cloudflare is a long-lived QUIC/HTTP2 session
that an intercepting sidecar would only complicate, and it sits *in front of* the gateway — it should
be a dumb pipe to the mesh entry point, not a mesh participant.
**Runner-up — cloudflared inside the mesh too:** uniformity, and mTLS on the cloudflared→gateway hop.
Rejected: that hop is already inside the cluster and terminates at the gateway; meshing the tunnel adds
egress-interception risk to the one component whose entire job is a stable outbound connection, for no
real security gain.

### 6. oauth2-proxy as an Istio `ext_authz` provider — not an inline reverse proxy
**Why:** oauth2-proxy runs *beside* the data path. Envoy calls it as an authorization check
(`envoyExtAuthzHttp`); on "no session" it returns a 302 that Envoy relays to the browser, and on
"valid session" the request flows straight to the app. oauth2-proxy is never in the bytes-through path
of an authenticated request, so it can't become a latency/throughput bottleneck, and it scales
independently.
**Runner-up — oauth2-proxy as an in-line reverse proxy (`--upstream=<app>`, all traffic flows through
it):** simpler mental model, one fewer Istio concept. Rejected because every request — including large
report payloads and SSE streams — would transit oauth2-proxy, coupling app throughput to the auth
component and undermining the SSE heartbeat/streaming work already done. ext_authz keeps auth on the
control path, not the data path.

### 7. oauth2-proxy for the login itself — not Cloudflare Access, not native Istio JWT
This is the "why oauth2-proxy?" decision specifically.
**Why oauth2-proxy:** it performs the full OIDC *browser* login (redirect to Entra, code exchange,
session cookie) and enforces it **at the cluster/mesh level**. Enforcement is independent of the edge:
anyone who reaches the internal gateway — including from a VNet foothold or a future non-Cloudflare
ingress — is still challenged. It's open-source, portable, and speaks Envoy ext_authz natively.

**Runner-up — Cloudflare Access (Zero Trust), Entra as the IdP:** this is the closest comparable and
genuinely attractive: **no in-cluster auth component at all** — Cloudflare does the OIDC dance at the
edge and only lets authenticated requests into the tunnel. Simpler to run. I rejected it as the
*primary* control for two reasons: (a) enforcement lives **only at the edge**, so it's a single layer —
if traffic ever reaches the gateway by another path, there's no second gate; (b) it couples
authentication to Cloudflare, against the portability goal. It remains the best-in-class *second* layer
and can be switched on in front of this with zero app changes (defense in depth).

**Also-rans:**
- **Native Istio `RequestAuthentication` + `AuthorizationPolicy` (JWT):** validates a bearer token but
  does **not** run a login flow — it assumes something already minted the JWT. Wrong tool for
  browser SSO; right tool for service-to-service or API clients. Could be layered on later for API auth.
- **Authelia / Authentik / Keycloak:** full IdP/portal features (MFA policies, user DB). Overkill when
  Entra *is* the IdP; we only need an OIDC relying-party, which is exactly oauth2-proxy's scope.
- **Envoy Gateway / Gateway API native OIDC filter:** promising and would remove oauth2-proxy, but the
  managed AKS mesh addon standardizes on Istio ext_authz today; this is the supported path.

### 8. Single-tenant Entra app (`AzureADMyOrg`) — not multi-tenant
**Why:** only identities in your tenant (`f493fd7e…`) can authenticate. Least privilege on *who* can
even attempt login.
**Runner-up — multi-tenant / personal Microsoft accounts:** needed only if you want external orgs to
sign in. Rejected as strictly more exposure than the requirement. `--email-domain` / group claims can
tighten this further to specific users.

### 9. Secrets in Key Vault, cookie secret generated in-cluster — never in git
**Why:** the Entra client secret lives in Key Vault (`dscopeclddev01/oauth2-proxy-client-secret`) and is
injected into a Kubernetes Secret at deploy time; the oauth2-proxy cookie secret is generated fresh
(`openssl rand -hex 16`, 32 bytes) and never leaves the cluster. Nothing sensitive is echoed or
committed.
**Runner-up — CSI Secrets Store driver mounting Key Vault directly into the pod:** the cleanest
long-term answer (no Kubernetes Secret at rest, auto-rotation). Rejected *for now* only to keep this
first cut moving; it's the recommended hardening follow-up.

---

## How to sign in

1. Open `https://driftscope.justmakeitgo.net`.
2. You're redirected to Microsoft; sign in with an account in tenant `f493fd7e-e41b-4f64-ad3d-a9704bbfb5f8`.
3. On success oauth2-proxy sets the session cookie and returns you to the page you asked for.

To confirm enforcement without logging in:
```
curl -sI https://driftscope.justmakeitgo.net/healthz   # → 302 to login.microsoftonline.com
```

---

## Proving it works

"Applied" is not "enforced." Each security control is proven by a **negative test** — showing the
thing it's supposed to block is actually blocked, from the position a real attacker would occupy — not
by observing that the happy path redirects. A 302 proves a redirect exists; it does not prove the app
is unreachable. Two throwaway pods (one in the mesh, one out) plus external curls cover it:

| # | Claim | Test | Result |
|---|---|---|---|
| A | STRICT mTLS blocks plaintext | out-of-mesh pod → `app:80` | `000` / curl exit 56 (reset) — **blocked** |
| B | App unreachable bypassing the SSO gate | in-mesh pod → app **direct** | `200` **→ fixed to `403`** (see below) |
| C | SSO enforced *in the mesh*, not only at the edge | in-cluster pod → gateway `/report` | `302` → Entra |
| D | Unauthenticated users get no app bytes | external `/`, `/report`, forged cookie | `302`, never `200` |
| E | No public exposure | Services with a public LB IP | none; gateway LB is internal, app is ClusterIP |
| F | gateway→app actually runs over mTLS | app sidecar Envoy telemetry | 4/4 `connection_security_policy="mutual_tls"` |
| G | An authenticated user *does* get in | real browser login → app | *requires an interactive Entra login* |

**What the test caught (B).** With only STRICT mTLS + gateway-side SSO, an in-mesh probe reached the
app **directly with `200`, no login** — because the `AuthorizationPolicy` enforcing SSO lives on the
*gateway*, while the app accepted any mТLS caller. STRICT mTLS only proves the caller has *some* mesh
identity, not that it's the gateway. Closed with `driftscope-allow-gateway-only` (an app-namespace
`AuthorizationPolicy`, `action: ALLOW`) restricting the app's inbound to the single principal
`cluster.local/ns/aks-istio-ingress/sa/aks-istio-ingressgateway-internal`. Re-tested: the same probe
now gets **`403`**, while the gateway path (C) and mTLS (F) are unaffected. This is the whole point of
proving rather than asserting — the test turned a latent bypass into a closed one.

**G is the one piece that needs a human:** completing an Entra login (credentials + MFA) and confirming
the app returns `200` with a session. The negative controls above need no credentials and are
reproducible any time by re-creating the two probe pods.

## Operational notes & known caveats

- **`/healthz` is gated too.** Most secure, but external uptime checks will see 302. If you want a
  public health probe, add `/healthz` to the AuthorizationPolicy `notPaths`. In-cluster readiness
  probes hit the pod directly and are unaffected.
- **Scheme behind two proxies.** TLS terminates at Cloudflare and re-enters the cluster as HTTP, so
  Envoy sees `http`. We handle this by **pinning** `--redirect-url` to the `https` callback (so the
  OIDC `redirect_uri` is always correct) and `--cookie-secure=true`; Cloudflare "Always Use HTTPS"
  upgrades any post-login `rd`. If a redirect ever lands on `http://`, that's the knob.
- **Cookie secret length.** oauth2-proxy measures the *string* length; a base64 of 32 bytes is 44
  chars and is rejected. Use a 32-char value (`rand -hex 16`). (This is why the first rollout
  CrashLooped.)
- **Secret rotation.** The Entra client secret is a 1-year credential; rotate in Key Vault and
  re-create the Kubernetes Secret. The CSI driver (#9 runner-up) removes this manual step.
- **Codify the manifests.** The Gateway / VirtualService / PeerAuthentication / AuthorizationPolicy /
  oauth2-proxy objects were applied imperatively to stand this up; fold them into
  `deploy/overlays/cloud/` so the cluster is reproducible from git.
