# DriftScope business-readiness roadmap

A multi-step project to take DriftScope from an impressive-but-hand-built artifact to an
operable, sellable, trustworthy product. **Safety, security, and best practices are the
governing constraints, not afterthoughts.**

## North star: reproducibility

> **Anyone should be able to clone this repo and stand up their own DriftScope, in their own
> domain, with the least friction possible.**

Target experience (the goal we build toward):
```sh
git clone https://github.com/<you>/driftscope && cd driftscope
cp config.example.tfvars config.tfvars     # set: your domain, Azure subscription, Cloudflare token, admin group
make up                                     # provision + deploy + wire DNS/SSO
# -> https://driftscope.<your-domain> is live, gated to YOUR Entra group
```
Nothing about the running system should be special to *this* deployment. Today it is riddled
with deployment-specific values — those are the first thing to fix.

### What blocks reproducibility today (grounded)
- **Everything is hand-built.** AKS, ACR, Key Vault, the Entra app *and* group, Cloudflare
  tunnel/DNS were created by `az`/`gh`/portal. No Infrastructure-as-Code. If the cluster dies,
  there is no button to rebuild it.
- **Hardcoded deployment values** in `deploy/` (`justmakeitgo.net`, `dscopeacr18860`,
  `dscopeclddev01`, tenant `f493fd7e…`, app `2fe61316…`, group `82cb7474…`). A second deployer
  can't reuse these; they must become parameters.
- **Imperative deploys.** Changes applied by hand (`kubectl apply`), not reconciled from git —
  not reproducible, not auditable, error-prone (stray-namespace mistakes happened twice).
- **Manual secrets.** Key Vault → `kubectl create secret` by hand; no automation, no rotation.

---

## Phase 0 — Reproducibility (current focus)

Goal: **clone → your own DriftScope in your own domain**, fully from code. This phase is itself
multi-step:

### 0.1 Parameterize (de-hardcode)
Extract every deployment-specific value into **one config surface** (a `tfvars` + a generated
kustomize/env layer). Parameters: base domain, Azure subscription/region/resource-group names,
ACR name, Key Vault name, Entra tenant id + app + group, Cloudflare account/zone/token.
Files to de-hardcode: `deploy/overlays/cloud/{oauth2-proxy.yaml,istio-routing.yaml,kustomization.yaml}`,
`deploy/overlays/kind/config.env`.

### 0.2 Terraform the cloud footprint (`azurerm`)
AKS (Overlay CNI, **network-policy engine on** — fixes the inert NetworkPolicy, managed Istio,
Workload Identity, OIDC issuer), ACR, Key Vault (soft-delete/purge protection), managed
identities + federated credentials, RBAC/role assignments. Everything we built by hand, as code.

### 0.3 Automate Entra (`azuread`)
App registration (redirect URIs derived from the domain), the access **group**, `groupMembershipClaims`,
the client secret into Key Vault, federated credentials. So a new tenant gets its own app + group
without portal clicks.

### 0.4 Terraform the edge — Azure DNS + Azure Front Door (re-imagined: was Cloudflare)
Decision: drop the Cloudflare dependency, go all-Azure. **Azure DNS** zone (name servers
delegated at the registrar) + **Azure Front Door Standard** — managed TLS (browser-facing, so
no cert-manager for the public cert) + WAF + DDoS + CDN, in front of the Istio gateway.
Chose Standard (~$35/mo, origin locked to Front Door via `X-Azure-FDID` + service tag) over
Premium (~$330/mo, Private Link). Front Door→origin (leg 2) runs HTTPS with a **real Let's Encrypt
cert** on the gateway, issued via **cert-manager DNS-01** for **`origin.<domain>`** (an A record →
the gateway IP, in the Azure DNS zone we own), and Front Door **validates it**
(`certificate_name_check = true`) — authenticated + encrypted, no disabled-cert-validation finding
(CWE-295; self-signed was rejected for the same reason). DNS-01 (not HTTP-01) because it's the
robust, unattended renewal path and doesn't need the awkward ACME-challenge routing a classic
Istio Gateway would require; the full analysis is in **`docs/design-edge-origin-cert.md`**. The
domain-less **demo mode** can't DNS-01 the Microsoft-owned `cloudapp.azure.com` zone, so there
leg 2 is **HTTP** over the Azure backbone (browser leg still HTTPS) — documented, demo-only.
**0.4a** = the Terraform (DNS + Front Door + WAF + BYO gateway IP with a DNS label). **0.4b** =
the overlay + edge side: external gateway on the BYO IP; **cert-manager** (Workload Identity) +
DNS-01 `ClusterIssuer` + origin `Certificate` → Istio Gateway `:443` HTTPS; the `X-Azure-FDID`
origin-lock policy; exempt `/healthz` from SSO (Front Door health probe); remove cloudflared.
The edge objects live in `deploy/overlays/cloud/edge/` (Terraform-generated, applied directly).

> **Premium + Private Link would obviate the origin cert entirely** — the Front Door→origin hop
> would traverse a *private* connection (no public path), so there's no public MITM surface and no
> origin cert to issue/validate/renew. We're paying for a cert-manager origin cert precisely
> because Standard's origin is public. That's the ~$330/mo-vs-cert-manager trade.

### 0.5 Bootstrap: clone → running  ✅
`make up` runs the whole pipeline — `preflight → infra (terraform) → image (az acr build) →
cluster (ext_authz + namespace + BYO IP + cert-manager) → secret (from Key Vault, never printed)
→ deploy (app + mesh hardening + edge TLS/lock)` — and prints the URL. Each step is a re-runnable
target. Config is two files: `terraform.tfvars` + `az login`. Crisp guide in
`docs/quickstart-deploy.md`. Also closed a real gap: `.gitignore` now excludes Terraform state
(which holds the Entra client secret) + tfvars. This is the deliverable that makes the north star
real. (Remote state backend + External Secrets are 0.6.)

### 0.6 GitOps/CD + secrets automation
Delivered as sub-steps:
- **0.6a Remote Terraform state** ✅ — opt-in Azure Storage backend (shared, locked, versioned,
  AAD-only/keyless) via `make state-backend` + `backend.tf.example`; gets the secret-bearing
  state off local disk for team/CD use. Local state stays the default. See `docs/remote-state.md`.
- **0.6b External Secrets** — External Secrets Operator / CSI Secrets Store pulling the
  oauth2-proxy creds from Key Vault (retire the manual `make secret`).
- **0.6c GitOps** — ArgoCD or Flux reconciling the overlays (retire imperative `kubectl`).
- **0.6d Environments** — real dev/staging/prod separation.

This is what makes *ongoing* change reproducible and safe, not just the first install.

**Phase 0 done when:** a fresh clone + a filled-in config produces a working, SSO-gated DriftScope
on a *different* domain/tenant with no manual portal/CLI steps, and day-2 changes flow git → CD.

---

## Later phases (summary; each becomes its own tracked effort)

**Phase 1 — Security hardening.** Enable Cilium (egress policy actually enforces; default-deny L3;
Hubble). Admission control (Kyverno/Gatekeeper + Pod Security `restricted`). Supply chain: image
scanning (Defender/Trivy), pin-by-digest, cosign signing + Ratify admission, SBOMs. RBAC
least-privilege + PIM/just-in-time + access reviews.

**Phase 2 — Product & data.** Database + org/multi-tenancy model + scan history + **audit log**
(who scanned what). Data-handling/privacy policy + retention (critical for a tool reading private
repos). Async job model for fleet scans.

**Phase 3 — Operability.** Durable observability: ship logs (Loki/Azure Monitor) + traces
(Tempo/App Insights via the `traceparent` already flowing) + exemplars; define SLOs; real alert
delivery + on-call. Backup/DR (Velero + *tested* restore, RTO/RPO). Governance: Standard-tier
control plane, ResourceQuota/LimitRange, budgets, tagging. (Also: multi-node/HA — tracked
separately per the stress-test findings.)

**Phase 4 — Assurance/compliance.** Security in CI (SAST, dependency/secret/container scanning),
pen test, DAST against staging. SOC2 trajectory: change management, access reviews, incident
runbooks, vendor list.

## Backlog (parked, each its own feature)
- Denied-login spike alert (page on a burst of `403 UAEX`).
- Network prevention plan from the stress tests: multi-node HA (PDBs + anti-affinity), NAT
  Gateway for SNAT, Cilium/Hubble.

## Principle
Reproducibility and auditable change come **first** because every other guarantee — security,
DR, compliance, safe iteration — depends on being able to rebuild the system from code and know
exactly what is running. Build the foundation, then compound on it.
