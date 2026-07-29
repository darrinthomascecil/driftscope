# Action plan: secure, multi-environment AKS for driftscope

Status: **action plan for review.** Values in `<ANGLE_BRACKETS>` are **slugs** — fill them
in when implementation comes. The pattern is instantiated **first on the current cheap
cluster** (`dev`), then reused for future clusters (`prod`, …) by filling their slugs.
Builds on `docs/design-multi-env-deploy.md` (Kustomize base + overlays) and
`docs/design-github-app-auth.md` (workload-identity GitHub auth).

## Goal

One repeatable, best-practices pattern for running driftscope on AKS that scales to N
environments — each its own cluster — fronted by **Cloudflare** (TLS/WAF/DNS on
`driftscope[-<env>].justmakeitgo.net`), gated by **Entra ID**, meshed with **Azure
managed Istio** (mTLS, ingress, authz), with **per-env workload identity** and
least-privilege. The current cheap cluster becomes environment #1; nothing about the
pattern is specific to it.

## Environment model

- **One cluster per environment.** Each env owns its Azure resources and its Cloudflare
  hostname. A shared **baseline** (this pattern) applies to all; a per-env **tier**
  varies the heavyweight, costly bits.
- **Constant where it can be, parameterized where it must be.** Namespace + ServiceAccount
  stay `driftscope` everywhere (so the federated subject is identical); the environment
  lives in the **Azure resource names** (`driftscope-<env>`) and the **hostname**.
- **Shared across envs:** the Cloudflare zone (`justmakeitgo.net`), the Entra tenant, and
  the one GitHub App (its key imported into each env's own Key Vault).

| Tier | Cluster (current) | Prod (future) |
|---|---|---|
| Env name | `dev` | `prod` |
| AKS SKU | Free control plane, 1× small node | Standard tier (uptime SLA), HA node pools, zones |
| API server | Public + authorized IP ranges | **Private cluster** |
| Repo scope | public-only (or private behind auth) | private (behind Entra) |
| WAF / policy | baseline | strict WAF, tighter Azure Policy |
| Hostname | `driftscope.justmakeitgo.net` | `driftscope-prod.justmakeitgo.net` |

## Architecture (per env)

```
you → https://driftscope[-<env>].justmakeitgo.net
   │  Cloudflare edge:  TLS (free) · WAF · Access ── federated to ── Entra ID
   ▼  (Cloudflare Tunnel — cloudflared pod, outbound; no public origin)
 Istio ingress gateway (managed, INTERNAL)         ← mesh entry, mTLS
   ▼  Gateway + VirtualService
 driftscope Service → pod (Istio sidecar, mTLS STRICT)
   │
   └─ workload identity → per-env Key Vault (the shared GitHub App's key)
```

## Edge & DNS (decided: Cloudflare fronting Azure)

**Decided — Cloudflare is the edge, not Azure-native (no Front Door/AGC).** Cloudflare
provides TLS, WAF, Access↔Entra, and the Tunnel; Azure runs the workload. The tradeoff is
accepted deliberately: cheapest and least-config, at the cost of Cloudflare terminating
TLS (fine here; would warrant review for a regulated buyer, who would go Azure-native).

**DNS operational rule that follows from this:** keep **Cloudflare authoritative** for
`justmakeitgo.net` (Cloudflare's nameservers at the registrar) and **proxy** the app
record (`driftscope[-<env>]`, orange cloud). Do **not** create an Azure DNS zone for the
app name or delegate it to Azure DNS — that would pull the name *off* Cloudflare's edge
and forfeit the TLS/WAF/Access/Tunnel this plan depends on. (Azure DNS is fine for
*unrelated* records, just not the proxied app hostname.)

## How it scales (the repeatable machinery)

- **Kustomize:** the existing `base/` + one overlay per env. Each overlay adds the env's
  image (ACR), config, SA client-id, plus the `cloudflared` Deployment and the Istio
  `Gateway`/`VirtualService`. Adding an env = a new overlay.
- **Per-env infra script:** extend `deploy/aks/setup-aks-wi.sh` to also enable managed
  Istio + its internal ingress gateway, create the Cloudflare tunnel, and register the
  Entra/Access app. Running it for a new env is "fill slugs → run."
- **Per-env identity isolation:** own UAMI + federated credential + Key Vault per env
  (constant subject). Managed Istio needs **no** separate user identity (unlike AGC's ALB
  Controller) — the addon is Microsoft-managed.
- **GitOps (recommended at 2+ envs):** Flux/Argo CD reconciles each env's overlay from
  git; promote the same image **digest** dev→prod.

## The layers (best practices)

### 1. Cluster (AKS)
- **Entra-integrated + Azure RBAC for Kubernetes** (operator `kubectl` authz via Entra
  groups, not local admin).
- **Workload identity + OIDC issuer** enabled (already on for `dev`).
- **Azure CNI overlay + network policy** (Cilium) for pod-level segmentation; default-deny.
- **System + user node pools** separated; cluster autoscaler; **availability zones** (prod).
- **Azure Linux** nodes; **auto-upgrade** channel (patch + node image) with maintenance
  windows.
- **API server** per-env: private (prod) vs public + authorized IP ranges (dev).
- **Defender for Containers** + **Azure Policy for AKS** (Gatekeeper guardrails).

### 2. Mesh (Azure managed Istio)
- `az aks mesh enable` + `--enable-ingress-gateway Internal`.
- **PeerAuthentication STRICT** (mesh-wide mTLS).
- Sidecar injection on the `driftscope` namespace (managed-revision label).
- **AuthorizationPolicy**: default-deny, allow only ingress-gateway → app.
- Managed-revision **canary upgrades**.

### 3. Edge (Cloudflare)
- **Cloudflare Tunnel** (`cloudflared`) per env → the Istio **internal** ingress gateway.
  No public origin, no LoadBalancer, nothing to bypass.
- **DNS** `driftscope[-<env>].justmakeitgo.net`, proxied. **Universal SSL** (TLS). **WAF**
  + rate limiting.
- **Cloudflare Access** application on the hostname → **federated to Entra ID** →
  restricted to an allowed Entra group. *(This is the recommended Entra enforcement point;
  see Decisions.)*

### 4. App identity (workload identity)
- Per-env **UAMI** + **federated credential** (subject
  `system:serviceaccount:driftscope:driftscope`, issuer = that env's AKS OIDC issuer).
- Per-env **Key Vault**; import the **shared GitHub App** key; grant the UAMI
  `Key Vault Crypto User`.
- **Least-privilege (audit fixes baked in):** `AcrPull`; Azure DevOps **read-only**
  (Readers, *not* Contributors); **no** subscription-wide Reader.

### 5. Entra ID (app protection + platform)
- **User auth:** Cloudflare Access ↔ Entra (OIDC/SAML) gates the hostname. Optional Istio
  `RequestAuthentication` (Entra JWT) as in-cluster defense-in-depth.
- **Operator auth:** Entra-integrated AKS RBAC; an Entra **admin group** for cluster ops.
- **App-user group:** an Entra group whose members may reach the app.

### 6. Secrets & config
- **Key Vault** per env (App key + any future secrets), reached via workload identity /
  Secrets Store CSI. **No secrets in git**; non-secret config via Kustomize ConfigMap.

### 7. Observability
- **Managed Prometheus + Managed Grafana**, **Container Insights**, a **Log Analytics**
  workspace (shared or per env), plus Istio mesh telemetry.

### 8. CI/CD & supply chain
- Build → push to **ACR** (digest-pinned) → **image scan** gate (Defender/ACR) → GitOps
  reconcile. Promote the **same digest** across envs.

## Entra ID protection: cluster-level vs app-level

"Protect the app with Entra" means *only an authenticated Entra user (in an allowed
group) can use it.* There are two places to enforce that, and the choice matters.

### Cluster-level (a shared gate in front of the app) — recommended
Auth is enforced at a shared choke point, so the app carries **no auth code**. Two forms
in this stack:
- **Edge — Cloudflare Access ↔ Entra** (outermost): Access federates to Entra as the IdP;
  the user completes an Entra login at Cloudflare's edge before any traffic reaches the
  cluster. **Un-bypassable** because the origin is private (tunnel + ClusterIP). This is
  where the plan already lands.
- **Mesh — Istio `RequestAuthentication` + `AuthorizationPolicy`** (inside the cluster):
  the Istio ingress gateway validates the Entra-issued JWT and denies unauthenticated
  requests. Keeps enforcement in-cluster; good as defense-in-depth.

*Pros:* one config protects **every** app (deploy a new app → automatically gated — it
matches the auto-subdomain pattern); one consistent policy (allowed groups, MFA, session
length); apps stay **auth-agnostic** — no libraries, no token handling, so driftscope
keeps its stdlib-only, zero-dependency design; central audit/governance.
*Cons:* coarse by default (same policy for all apps unless you add per-hostname/route
rules); the app doesn't know *who* the user is unless the layer **forwards identity**
(Cloudflare Access injects a signed `Cf-Access-Jwt-Assertion` header; Istio can forward
the JWT claims).

### App-level (Entra built into each app)
Each app implements Entra OIDC itself (MSAL / an OIDC middleware): it runs the login
flow, validates tokens, manages sessions, and can do per-user authorization internally.
*Pros:* fine-grained — per-user/role authz, the app natively knows the identity, per-app
registration/scopes/consent; works even if the app is reached directly (not dependent on
an upstream gate).
*Cons:* **every** app must build and maintain auth (code, libraries, sessions) —
duplication and more bug surface; it **breaks driftscope's zero-dependency design** (MSAL/
OIDC is a real dependency + session/state complexity); slower to onboard each app;
inconsistent if every app rolls its own.

### Side by side
| | Cluster-level (edge/mesh) | App-level (in-app) |
|---|---|---|
| Enforced at | Cloudflare Access / Istio gateway | inside each app |
| Per-app effort | zero (inherited) | a full auth implementation each |
| Granularity | coarse (per host/route) | fine (per user/role) |
| App knows the user | only if identity is forwarded | yes, natively |
| driftscope fit | ✅ stays stdlib-only | ❌ adds MSAL/OIDC + sessions |
| New-app onboarding | automatic | re-implement auth |
| Bypass risk | none if the origin is private | none (the app enforces) |

### Recommendation
**Cluster-level, at the edge — Cloudflare Access ↔ Entra.** A scanner needs
"authenticated Entra user in an allowed group," not per-user authorization, so the coarse
gate is exactly right and keeps driftscope auth-free. It also composes with auto-
subdomains: every `*.justmakeitgo.net` app is gated identically with no per-app work.

If a future app genuinely needs **per-user authorization**, don't move authentication into
the app — keep the cluster gate and have the layer **forward the verified identity** (the
Access JWT header, or Istio's forwarded claims) so the app does only the fine-grained
authz while the gate still owns authentication. That hybrid is the best of both, and it's
the ceiling driftscope should ever need.

## Per-environment parameters (slugs)

Fill one column per environment. `dev` is pre-filled with what's already provisioned;
`<slugs>` are still to decide.

| Parameter | `dev` (current) | `prod` (future) |
|---|---|---|
| Subscription | `b491bf98-6ea2-4a07-a641-200d61d7a735` | `<SUBSCRIPTION_ID>` |
| Entra tenant | `f493fd7e-e41b-4f64-ad3d-a9704bbfb5f8` | `<TENANT_ID>` |
| Region | `eastus` | `<REGION>` |
| Resource group | `driftscope-cloud` | `<RESOURCE_GROUP>` |
| AKS cluster | `driftscope-aks` | `<AKS_NAME>` |
| Tier / API mode | Free / public+`<AUTHORIZED_IP_RANGES>` | Standard / **private** |
| Node SKU · min/max | `Standard_B2s` · 1/`<MAX>` | `<NODE_SKU>` · `<MIN>`/`<MAX>` |
| ACR | `dscopeacr18860` | `<ACR_NAME>` (or shared) |
| Key Vault | `<DEV_KEYVAULT_NAME>` | `<PROD_KEYVAULT_NAME>` |
| UAMI | `<DEV_UAMI_NAME>` (`driftscope-dev`) | `driftscope-prod` |
| Hostname | `driftscope.justmakeitgo.net` | `driftscope-prod.justmakeitgo.net` |
| Cloudflare zone | `justmakeitgo.net` | `justmakeitgo.net` (shared) |
| Cloudflare tunnel token | `<DEV_CF_TUNNEL_TOKEN>` | `<PROD_CF_TUNNEL_TOKEN>` |
| Entra app (Access) client id | `<ENTRA_APP_CLIENT_ID>` | `<ENTRA_APP_CLIENT_ID>` (shared or per-env) |
| Allowed Entra group | `<ALLOWED_ENTRA_GROUP_ID>` | `<ALLOWED_ENTRA_GROUP_ID>` |
| Cluster-admin Entra group | `<AKS_ADMIN_GROUP_ID>` | `<AKS_ADMIN_GROUP_ID>` |
| Repo scope | `<public | private>` | `private` |
| GitHub App id / installation | `4425305` / `149853299` | `4425305` / `149853299` (shared) |
| ADO org (read-only) | `darrinthomascecil` | `<ADO_ORG>` |

## Decisions to confirm (on review)

1. **Entra enforcement point** — recommend **Cloudflare Access ↔ Entra** (edge); optionally
   add Istio `RequestAuthentication` for defense-in-depth. (Alt: enforce only in-cluster.)
2. **API server** — per-env: **private** for prod, **public + authorized IPs** for dev.
3. **Repo scope on `dev`** — keep the cheap cluster **public-only**, or wire **private**
   scanning now that Entra will gate it? (Prod = private.)
4. **ACR** — one **shared** registry across envs (promote digests) vs per-env registries.
5. **Log Analytics / Grafana** — shared workspace vs per-env.
6. **GitOps tool** — Flux vs Argo CD (vs a plain pipeline for now).

## Phased rollout (start on the current cheap cluster)

- **Phase 0** — confirm the decisions above; fill the `dev` slugs (Cloudflare token, Entra
  app + groups).
- **Phase 1 — edge + auth on `dev`:** Cloudflare Tunnel + DNS + Universal SSL + **Access ↔
  Entra**. Flip the Service to ClusterIP. → `https://driftscope.justmakeitgo.net`, TLS +
  Entra login, no public origin.
- **Phase 2 — managed Istio on `dev`:** enable mesh + internal ingress gateway, STRICT
  mTLS, sidecar injection, retarget the tunnel at the gateway, add `AuthorizationPolicy`.
- **Phase 3 — (optional) private scanning on `dev`:** wire workload identity + the GitHub
  App (behind the Entra gate) → issue-history + private repos.
- **Phase 4 — hardening baseline:** network policy, Defender, Azure Policy, observability.
- **Phase 5 — templatize:** fold Cloudflare/Istio/identity into the Kustomize overlay +
  `setup-aks-wi.sh` so **env #2 (prod) is "fill slugs → run."**

## Non-goals / follow-ups
- The **async job model** (for long/fleet scans vs any request timeout) — separate track.
- Cluster **DR/backup** — driftscope is stateless (cache is disposable); nothing to back up.
